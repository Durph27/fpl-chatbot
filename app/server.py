"""FastAPI Web Server for the FPL AI Chatbot (`app/server.py`).

Provides:
  - Automatic MySQL schema creation for persistent chat history (`chat_sessions`,
    `chat_messages`).
  - Automatic Manager ID extraction from either a numeric ID or a pasted FPL URL
    (e.g., `https://fantasy.premierleague.com/entry/4791912/event/10`).
  - REST endpoints for chat sessions, message history, manager squad sync, and
    future sidebar modules (My Squad, Player Explorer).
  - Static file serving for the ChatGPT/Gemini/Claude-style Flexbox web UI in `static/`.

Run with:
  uvicorn app.server:app --reload --port 8000
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app.chatbot import run_web_chat_turn
from app.db import engine
from app.ingest import fetch as fetch_fpl_api
from app.rag import get_chroma_collection
from app.tools import _clean_records, get_my_squad, refresh_manager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


def init_chat_tables() -> None:
    """Create `chat_sessions` and `chat_messages` tables in MySQL if they do not exist."""
    create_sessions_sql = text("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id VARCHAR(64) PRIMARY KEY,
            title VARCHAR(255) NOT NULL DEFAULT 'New Chat',
            manager_id INT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    create_messages_sql = text("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(64) NOT NULL,
            role VARCHAR(16) NOT NULL,
            content LONGTEXT NOT NULL,
            tools_used TEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_session_created (session_id, created_at),
            CONSTRAINT fk_chat_session
                FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    with engine.begin() as conn:
        conn.execute(create_sessions_sql)
        conn.execute(create_messages_sql)


def extract_manager_id(raw_input: str | int) -> int:
    """
    Extract an integer FPL Manager ID from either a plain number ('4791912')
    or a full FPL URL ('https://fantasy.premierleague.com/entry/4791912/event/10').
    """
    text_val = str(raw_input).strip()
    if text_val.isdigit():
        return int(text_val)
    url_match = re.search(r"/entry/(\d+)", text_val)
    if url_match:
        return int(url_match.group(1))
    digits_match = re.search(r"\b(\d{4,10})\b", text_val)
    if digits_match:
        return int(digits_match.group(1))
    raise ValueError(f"Could not extract a valid FPL Manager ID from '{raw_input}'.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("  [Server] Ensuring MySQL chat history tables exist...")
    init_chat_tables()
    print("  [Server] Warming up ChromaDB RAG collection...")
    try:
        get_chroma_collection(force_rebuild=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  [Server Warning] ChromaDB warm-up skipped: {exc}")
    yield


app = FastAPI(title="FPL AI Assistant Web API", lifespan=lifespan)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# =====================================================================
# REQUEST / RESPONSE MODELS
# =====================================================================

class SyncManagerRequest(BaseModel):
    manager_input: str


class CreateSessionRequest(BaseModel):
    manager_id: int | None = None
    title: str = "New Chat"


class ChatMessageRequest(BaseModel):
    session_id: str | None = None
    manager_id: int | None = None
    message: str


# =====================================================================
# ROUTES: FRONTEND ENTRYPOINT
# =====================================================================

@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found.")
    return FileResponse(str(index_file), headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def serve_favicon():
    favicon_file = STATIC_DIR / "images" / "askFPL_avatar_64.png"
    if not favicon_file.exists():
        raise HTTPException(status_code=404, detail="Favicon not found.")
    return FileResponse(str(favicon_file), headers={"Cache-Control": "no-cache"})


# =====================================================================
# ROUTES: MANAGER SYNC & SQUAD VIEW
# =====================================================================

@app.post("/api/manager/sync")
def api_sync_manager(req: SyncManagerRequest) -> dict[str, Any]:
    try:
        manager_id = extract_manager_id(req.manager_input)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    sync_res = refresh_manager(manager_id)
    if not sync_res.get("synced"):
        raise HTTPException(
            status_code=400,
            detail=sync_res.get("error", f"Failed to sync Manager ID {manager_id}."),
        )

    squad_res = get_my_squad(manager_id)
    return {
        "manager_id": manager_id,
        "sync_summary": sync_res.get("sync_summary"),
        "squad": squad_res,
    }


@app.get("/api/manager/{manager_id}/squad")
def api_get_squad(manager_id: int) -> dict[str, Any]:
    return get_my_squad(manager_id)


PLAYER_EXPLORER_SORT_MAP: dict[str, str] = {
    "form": "CAST(p.form AS DECIMAL(10,2))",
    "ep_next": "CAST(p.ep_next AS DECIMAL(10,2))",
    "total_points": "p.total_points",
    "points": "p.total_points",
    "xg": "CAST(p.expected_goals AS DECIMAL(10,2))",
    "xa": "CAST(p.expected_assists AS DECIMAL(10,2))",
    "xgi": "CAST(p.expected_goal_involvements AS DECIMAL(10,2))",
    "xgc": "CAST(p.expected_goals_conceded AS DECIMAL(10,2))",
    "points_per_game": "CAST(p.points_per_game AS DECIMAL(10,2))",
    "ppg": "CAST(p.points_per_game AS DECIMAL(10,2))",
    "value": "(p.total_points / NULLIF(p.price, 0))",
    "value_points_per_m": "(p.total_points / NULLIF(p.price, 0))",
    "clean_sheets": "p.clean_sheets",
    "goals_scored": "p.goals_scored",
    "assists": "p.assists",
    "goals_conceded": "p.goals_conceded",
    "minutes": "p.minutes",
    "starts": "p.starts",
    "bonus": "p.bonus",
    "bps": "p.bps",
    "saves": "p.saves",
    "yellow_cards": "p.yellow_cards",
    "red_cards": "p.red_cards",
    "selected_by_percent": "CAST(p.selected_by_percent AS DECIMAL(10,2))",
    "ict_index": "CAST(p.ict_index AS DECIMAL(10,2))",
    "price": "p.price",
    "transfers_in_event": "p.transfers_in_event",
    "transfers_out_event": "p.transfers_out_event",
}


def _to_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(val: Any) -> int:
    try:
        return int(float(val)) if val is not None else 0
    except (TypeError, ValueError):
        return 0


@app.get("/api/players/top")
def api_top_players(
    position: str | None = None,
    max_price: float | None = None,
    team: str | None = None,
    sort_by: str = "total_points",
    limit: int = 1000,
    min_minutes: int = 0,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), 1500))
    safe_min_minutes = max(0, int(min_minutes))
    order_col = PLAYER_EXPLORER_SORT_MAP.get(
        sort_by.lower().strip(), "p.total_points"
    )

    conditions = ["p.minutes >= :min_minutes"]
    params: dict[str, Any] = {
        "min_minutes": safe_min_minutes,
        "lim": safe_limit,
    }

    if position:
        pos_clean = position.upper().strip()
        pos_aliases = {
            "FW": "FWD",
            "ST": "FWD",
            "GK": "GK",
            "GKP": "GK",
            "DEF": "DEF",
            "DF": "DEF",
            "MID": "MID",
            "MF": "MID",
        }
        pos_clean = pos_aliases.get(pos_clean, pos_clean)
        conditions.append("p.position = :pos")
        params["pos"] = pos_clean

    if max_price is not None:
        conditions.append("p.price <= :max_price")
        params["max_price"] = float(max_price)

    if team:
        conditions.append(
            "(UPPER(t.short_name) = UPPER(:team) OR LOWER(t.name) LIKE LOWER(:team_like))"
        )
        params["team"] = team.strip()
        params["team_like"] = f"%{team.strip()}%"

    where_clause = " AND ".join(conditions)
    sql = text(f"""
        SELECT
            p.id,
            p.web_name,
            CONCAT(p.first_name, ' ', p.second_name) AS full_name,
            t.short_name AS team,
            t.name AS team_name,
            p.position,
            p.price,
            p.selected_by_percent,
            p.total_points,
            p.form,
            p.points_per_game,
            p.ep_next,
            ROUND(p.total_points / NULLIF(p.price, 0), 2) AS value_points_per_m,
            p.minutes,
            p.starts,
            p.goals_scored,
            p.assists,
            p.clean_sheets,
            p.goals_conceded,
            p.expected_goals AS xG,
            p.expected_assists AS xA,
            p.expected_goal_involvements AS xGI,
            p.expected_goals_conceded AS xGC,
            p.ict_index,
            p.bonus,
            p.bps,
            p.yellow_cards,
            p.red_cards,
            p.saves,
            p.transfers_in_event,
            p.transfers_out_event,
            p.status,
            p.news,
            p.chance_of_playing_next_round
        FROM players p
        JOIN teams t ON t.id = p.team
        WHERE {where_clause}
        ORDER BY {order_col} DESC, p.total_points DESC, p.id ASC
        LIMIT :lim
    """)

    df = pd.read_sql(sql, engine, params=params)
    teams_df = pd.read_sql(
        text("SELECT DISTINCT short_name, name FROM teams ORDER BY short_name ASC"),
        engine,
    )

    return {
        "filters": {
            "position": position,
            "max_price": max_price,
            "team": team,
            "sort_by": sort_by,
            "min_minutes": safe_min_minutes,
            "limit": safe_limit,
        },
        "count": len(df),
        "teams": _clean_records(teams_df),
        "players": _clean_records(df),
    }


@app.get("/api/players/{player_id}/history")
def api_player_history(player_id: int) -> dict[str, Any]:
    player_sql = text("""
        SELECT
            p.id,
            p.web_name,
            CONCAT(p.first_name, ' ', p.second_name) AS full_name,
            t.short_name AS team,
            t.name AS team_name,
            p.position,
            p.price,
            p.selected_by_percent,
            p.total_points,
            p.form,
            p.points_per_game,
            p.ep_next,
            ROUND(p.total_points / NULLIF(p.price, 0), 2) AS value_points_per_m,
            p.minutes,
            p.starts,
            p.goals_scored,
            p.assists,
            p.clean_sheets,
            p.goals_conceded,
            p.expected_goals AS xG,
            p.expected_assists AS xA,
            p.expected_goal_involvements AS xGI,
            p.expected_goals_conceded AS xGC,
            p.ict_index,
            p.bonus,
            p.bps,
            p.yellow_cards,
            p.red_cards,
            p.saves,
            p.transfers_in_event,
            p.transfers_out_event,
            p.status,
            p.news,
            p.chance_of_playing_next_round
        FROM players p
        JOIN teams t ON t.id = p.team
        WHERE p.id = :pid
        LIMIT 1
    """)
    player_df = pd.read_sql(player_sql, engine, params={"pid": int(player_id)})
    if player_df.empty:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    player = _clean_records(player_df)[0]

    matches: list[dict[str, Any]] = []
    source = "mysql"

    try:
        hist_sql = text("""
            SELECT
                ph.round,
                ph.fixture,
                ph.opponent_team,
                t.short_name AS opponent_short,
                t.name AS opponent_name,
                ph.was_home,
                ph.kickoff_time,
                ph.team_h_score,
                ph.team_a_score,
                ph.total_points,
                ph.minutes,
                ph.goals_scored,
                ph.assists,
                ph.clean_sheets,
                ph.goals_conceded,
                ph.own_goals,
                ph.penalties_saved,
                ph.penalties_missed,
                ph.yellow_cards,
                ph.red_cards,
                ph.saves,
                ph.bonus,
                ph.bps,
                ph.ict_index,
                ph.expected_goals AS xG,
                ph.expected_assists AS xA,
                ph.expected_goal_involvements AS xGI,
                ph.expected_goals_conceded AS xGC,
                ph.value
            FROM player_history ph
            LEFT JOIN teams t ON t.id = ph.opponent_team
            WHERE ph.element = :pid
            ORDER BY ph.round ASC, ph.kickoff_time ASC
        """)
        hist_df = pd.read_sql(hist_sql, engine, params={"pid": int(player_id)})
        matches = _clean_records(hist_df)
    except Exception:  # noqa: BLE001
        matches = []

    if not matches:
        source = "fpl_api"
        try:
            teams_df = pd.read_sql(
                text("SELECT id, short_name, name FROM teams"), engine
            )
            team_map = {
                int(r["id"]): (r["short_name"], r["name"])
                for _, r in teams_df.iterrows()
            }
            api_data = fetch_fpl_api(f"element-summary/{int(player_id)}/")
            for item in api_data.get("history", []):
                opp_id = item.get("opponent_team")
                opp_short, opp_name = team_map.get(
                    int(opp_id) if opp_id is not None else -1,
                    (f"T{opp_id}", f"Team {opp_id}"),
                )
                matches.append({
                    "round": item.get("round"),
                    "fixture": item.get("fixture"),
                    "opponent_team": opp_id,
                    "opponent_short": opp_short,
                    "opponent_name": opp_name,
                    "was_home": item.get("was_home"),
                    "kickoff_time": item.get("kickoff_time"),
                    "team_h_score": item.get("team_h_score"),
                    "team_a_score": item.get("team_a_score"),
                    "total_points": item.get("total_points", 0),
                    "minutes": item.get("minutes", 0),
                    "goals_scored": item.get("goals_scored", 0),
                    "assists": item.get("assists", 0),
                    "clean_sheets": item.get("clean_sheets", 0),
                    "goals_conceded": item.get("goals_conceded", 0),
                    "own_goals": item.get("own_goals", 0),
                    "penalties_saved": item.get("penalties_saved", 0),
                    "penalties_missed": item.get("penalties_missed", 0),
                    "yellow_cards": item.get("yellow_cards", 0),
                    "red_cards": item.get("red_cards", 0),
                    "saves": item.get("saves", 0),
                    "bonus": item.get("bonus", 0),
                    "bps": item.get("bps", 0),
                    "ict_index": item.get("ict_index", 0),
                    "xG": item.get("expected_goals", 0),
                    "xA": item.get("expected_assists", 0),
                    "xGI": item.get("expected_goal_involvements", 0),
                    "xGC": item.get("expected_goals_conceded", 0),
                    "value": item.get("value"),
                })
        except Exception:  # noqa: BLE001
            matches = []

    gw_buckets: dict[int, dict[str, Any]] = {}
    for m in matches:
        gw = _to_int(m.get("round"))
        if gw <= 0:
            continue
        was_home = bool(m.get("was_home"))
        venue = "H" if was_home else "A"
        opp_short = m.get("opponent_short") or f"T{m.get('opponent_team', '')}"
        opp_label = f"{opp_short} ({venue})"

        if gw not in gw_buckets:
            gw_buckets[gw] = {
                "gw": gw,
                "opponents_list": [opp_label],
                "matches_count": 1,
                "total_points": _to_int(m.get("total_points")),
                "minutes": _to_int(m.get("minutes")),
                "goals_scored": _to_int(m.get("goals_scored")),
                "assists": _to_int(m.get("assists")),
                "clean_sheets": _to_int(m.get("clean_sheets")),
                "goals_conceded": _to_int(m.get("goals_conceded")),
                "saves": _to_int(m.get("saves")),
                "bonus": _to_int(m.get("bonus")),
                "bps": _to_int(m.get("bps")),
                "yellow_cards": _to_int(m.get("yellow_cards")),
                "red_cards": _to_int(m.get("red_cards")),
                "xG": _to_float(m.get("xG")),
                "xA": _to_float(m.get("xA")),
                "xGI": _to_float(m.get("xGI")),
                "ict_index": _to_float(m.get("ict_index")),
            }
        else:
            b = gw_buckets[gw]
            b["opponents_list"].append(opp_label)
            b["matches_count"] += 1
            b["total_points"] += _to_int(m.get("total_points"))
            b["minutes"] += _to_int(m.get("minutes"))
            b["goals_scored"] += _to_int(m.get("goals_scored"))
            b["assists"] += _to_int(m.get("assists"))
            b["clean_sheets"] += _to_int(m.get("clean_sheets"))
            b["goals_conceded"] += _to_int(m.get("goals_conceded"))
            b["saves"] += _to_int(m.get("saves"))
            b["bonus"] += _to_int(m.get("bonus"))
            b["bps"] += _to_int(m.get("bps"))
            b["yellow_cards"] += _to_int(m.get("yellow_cards"))
            b["red_cards"] += _to_int(m.get("red_cards"))
            b["xG"] += _to_float(m.get("xG"))
            b["xA"] += _to_float(m.get("xA"))
            b["xGI"] += _to_float(m.get("xGI"))
            b["ict_index"] += _to_float(m.get("ict_index"))

    by_gameweek = []
    for gw in sorted(gw_buckets):
        b = gw_buckets[gw]
        by_gameweek.append({
            "gw": gw,
            "opponent": " + ".join(b["opponents_list"]),
            "matches_count": b["matches_count"],
            "total_points": b["total_points"],
            "minutes": b["minutes"],
            "goals_scored": b["goals_scored"],
            "assists": b["assists"],
            "clean_sheets": b["clean_sheets"],
            "goals_conceded": b["goals_conceded"],
            "saves": b["saves"],
            "bonus": b["bonus"],
            "bps": b["bps"],
            "yellow_cards": b["yellow_cards"],
            "red_cards": b["red_cards"],
            "xG": round(b["xG"], 2),
            "xA": round(b["xA"], 2),
            "xGI": round(b["xGI"], 2),
            "ict_index": round(b["ict_index"], 1),
        })

    return {
        "player": player,
        "source": source,
        "by_gameweek": by_gameweek,
        "matches": matches,
    }


# =====================================================================
# ROUTES: CHAT SESSIONS & PERSISTENT HISTORY
# =====================================================================

@app.get("/api/sessions")
def api_list_sessions(manager_id: int | None = None) -> dict[str, Any]:
    if manager_id is not None:
        sql = text("""
            SELECT session_id, title, manager_id, created_at, updated_at
            FROM chat_sessions
            WHERE manager_id = :mid OR manager_id IS NULL
            ORDER BY updated_at DESC
            LIMIT 50
        """)
        params = {"mid": int(manager_id)}
    else:
        sql = text("""
            SELECT session_id, title, manager_id, created_at, updated_at
            FROM chat_sessions
            ORDER BY updated_at DESC
            LIMIT 50
        """)
        params = {}

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    sessions = [
        {
            "session_id": r["session_id"],
            "title": r["title"],
            "manager_id": r["manager_id"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
    return {"sessions": sessions}


@app.post("/api/sessions")
def api_create_session(req: CreateSessionRequest) -> dict[str, Any]:
    new_id = uuid.uuid4().hex[:16]
    title = (req.title or "New Chat").strip()[:250]
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO chat_sessions (session_id, title, manager_id)
                VALUES (:sid, :title, :mid)
            """),
            {"sid": new_id, "title": title, "mid": req.manager_id},
        )
    return {
        "session_id": new_id,
        "title": title,
        "manager_id": req.manager_id,
    }


@app.get("/api/sessions/{session_id}")
def api_get_session_messages(session_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        sess = conn.execute(
            text("SELECT session_id, title, manager_id FROM chat_sessions WHERE session_id = :sid"),
            {"sid": session_id},
        ).mappings().fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found.")

        msg_rows = conn.execute(
            text("""
                SELECT id, role, content, tools_used, created_at
                FROM chat_messages
                WHERE session_id = :sid
                ORDER BY id ASC
            """),
            {"sid": session_id},
        ).mappings().all()

    messages = []
    for r in msg_rows:
        tools_list = []
        if r["tools_used"]:
            try:
                tools_list = json.loads(r["tools_used"])
            except Exception:  # noqa: BLE001
                tools_list = []
        messages.append({
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "tools_used": tools_list,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {
        "session": dict(sess),
        "messages": messages,
    }


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str) -> dict[str, Any]:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM chat_sessions WHERE session_id = :sid"),
            {"sid": session_id},
        )
    return {"deleted": True, "session_id": session_id}


@app.post("/api/chat")
def api_chat(req: ChatMessageRequest) -> dict[str, Any]:
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    session_id = (req.session_id or "").strip() or uuid.uuid4().hex[:16]
    short_title = user_msg[:48] + ("..." if len(user_msg) > 48 else "")

    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT session_id, title, manager_id FROM chat_sessions WHERE session_id = :sid"),
            {"sid": session_id},
        ).mappings().fetchone()

        if not existing:
            conn.execute(
                text("""
                    INSERT INTO chat_sessions (session_id, title, manager_id)
                    VALUES (:sid, :title, :mid)
                """),
                {"sid": session_id, "title": short_title, "mid": req.manager_id},
            )
            active_mid = req.manager_id
        else:
            active_mid = req.manager_id if req.manager_id is not None else existing["manager_id"]
            new_title = short_title if existing["title"] == "New Chat" else existing["title"]
            conn.execute(
                text("""
                    UPDATE chat_sessions
                    SET title = :title, manager_id = :mid, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :sid
                """),
                {"sid": session_id, "title": new_title, "mid": active_mid},
            )

        prior_rows = conn.execute(
            text("""
                SELECT role, content
                FROM chat_messages
                WHERE session_id = :sid
                ORDER BY id DESC
                LIMIT 16
            """),
            {"sid": session_id},
        ).mappings().all()

    prior_messages = list(reversed([dict(r) for r in prior_rows]))

    result = run_web_chat_turn(
        user_message=user_msg,
        prior_messages=prior_messages,
        manager_id=active_mid,
    )
    reply_text = result["reply"]
    tools_used = result["tools_used"]

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO chat_messages (session_id, role, content, tools_used)
                VALUES (:sid, 'user', :content, NULL)
            """),
            {"sid": session_id, "content": user_msg},
        )
        conn.execute(
            text("""
                INSERT INTO chat_messages (session_id, role, content, tools_used)
                VALUES (:sid, 'assistant', :content, :tools)
            """),
            {
                "sid": session_id,
                "content": reply_text,
                "tools": json.dumps(tools_used),
            },
        )

    return {
        "session_id": session_id,
        "manager_id": active_mid,
        "reply": reply_text,
        "tools_used": tools_used,
    }