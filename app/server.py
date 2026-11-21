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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app.chatbot import run_web_chat_turn
from app.db import engine
from app.rag import get_chroma_collection
from app.tools import get_my_squad, refresh_manager, top_players

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


@app.get("/api/players/top")
def api_top_players(
    position: str | None = None,
    max_price: float | None = None,
    sort_by: str = "form",
    limit: int = 15,
) -> dict[str, Any]:
    return top_players(
        position=position,
        max_price=max_price,
        sort_by=sort_by,
        limit=limit,
    )


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