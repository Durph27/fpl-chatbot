"""MySQL query, squad-management, and real-time Google Search tools for the FPL Chatbot.

Compatible with the table structure in `app/ingest.py`, `app/ingest_manager.py`,
and the Google GenAI SDK. Includes:
  - General FPL tools: `get_player_info`, `top_players`, `compare_players`,
    `fixture_difficulty`, `gameweek_summary`
  - Personal manager tools: `refresh_manager`, `get_my_squad`, `analyze_my_squad`,
    `suggest_transfers`, `suggest_captain`
  - Fallback SQL tool: `run_readonly_sql`
  - Real-time Google Search tool: `search_premier_league_web` (for Premier League
    and FPL questions whose data is not stored in the MySQL database)
"""

from collections import Counter
from decimal import Decimal
import math
import os
import re
from typing import Any

from google import genai
from google.genai import types
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.ingest_manager import sync_manager


def _clean_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    Convert a DataFrame to a list of dictionaries safe for the Gemini API's JSON format.
    Handle Pandas NaN/Inf values and convert Decimal, Timestamp, and NumPy scalar values.
    """
    if df.empty:
        return []
    records = df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")
    for row in records:
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) or pd.isna(v):
                row[k] = None
            elif isinstance(v, Decimal):
                row[k] = float(v)
            elif hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            elif hasattr(v, "item"):
                val = v.item()
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    row[k] = None
                else:
                    row[k] = val
    return records


def _get_next_gw() -> int:
    """
    Find the next Gameweek.

    If none is marked as next, use the current Gameweek or the smallest unfinished
    Gameweek.
    """
    sql = text("""
        SELECT id
        FROM events
        WHERE is_next = 1
        UNION ALL
        SELECT id
        FROM events
        WHERE is_current = 1
        UNION ALL
        SELECT MIN(id) AS id
        FROM events
        WHERE finished = 0
        LIMIT 1
    """)
    with engine.connect() as conn:
        gw = conn.execute(sql).scalar()
    return int(gw) if gw is not None else 1


def _normalize_money(val: float | int | None) -> float:
    """
    Normalize FPL money values (`bank_balance` or `team_value`) to millions (£m).
    If the value is stored in tenths (for example, 15 for £1.5m or 1005 for £100.5m),
    divide by 10.
    """
    if val is None:
        return 0.0
    num = float(val)
    return round(num / 10.0, 1) if num > 35.0 else round(num, 1)


def _ensure_manager_synced(manager_id: int) -> dict[str, Any] | None:
    """
    Check whether `manager_id` exists in `managers` and `manager_squads`.
    If not present, automatically run `sync_manager(manager_id)` from `app.ingest_manager`.
    """
    target_id = int(manager_id)
    check_sql = text("""
        SELECT
            (SELECT COUNT(*) FROM managers WHERE manager_id = :mid) AS mgr_cnt,
            (SELECT COUNT(*) FROM manager_squads WHERE manager_id = :mid) AS sq_cnt
    """)
    try:
        with engine.connect() as conn:
            row = conn.execute(check_sql, {"mid": target_id}).fetchone()
        if row and int(row[0]) > 0 and int(row[1]) > 0:
            return None
    except Exception:  # noqa: BLE001
        pass

    return sync_manager(target_id)


def _find_player_rows(player_name: str, limit: int = 5) -> pd.DataFrame:
    """
    Find players by name, preferring an exact web_name match before partial matches
    against web_name, first_name, and second_name, ordered by total_points.
    """
    clean_name = player_name.strip()
    sql = text("""
        SELECT
            p.id,
            p.web_name,
            CONCAT(p.first_name, ' ', p.second_name) AS full_name,
            t.id AS team_id,
            t.name AS team_name,
            t.short_name AS team_short,
            p.position,
            p.price,
            p.total_points,
            p.form,
            p.points_per_game,
            p.ep_next,
            p.minutes,
            p.starts,
            p.goals_scored,
            p.assists,
            p.clean_sheets,
            p.bonus,
            p.bps,
            p.expected_goals AS xG,
            p.expected_assists AS xA,
            p.expected_goal_involvements AS xGI,
            p.expected_goals_conceded AS xGC,
            p.ict_index,
            p.selected_by_percent,
            p.transfers_in_event,
            p.transfers_out_event,
            p.status,
            p.news,
            p.chance_of_playing_next_round
        FROM players p
        JOIN teams t ON t.id = p.team
        WHERE LOWER(p.web_name) LIKE LOWER(:pattern)
           OR LOWER(CONCAT(p.first_name, ' ', p.second_name)) LIKE LOWER(:pattern)
           OR LOWER(p.second_name) LIKE LOWER(:pattern)
        ORDER BY
            CASE WHEN LOWER(p.web_name) = LOWER(:exact) THEN 0 ELSE 1 END,
            p.total_points DESC
        LIMIT :lim
    """)
    return pd.read_sql(
        sql,
        engine,
        params={"pattern": f"%{clean_name}%", "exact": clean_name, "lim": limit},
    )


def _get_team_upcoming_fixtures(team_id: int, start_gw: int, num_gws: int) -> list[dict[str, Any]]:
    """
    Get a team's fixtures from start_gw through start_gw + num_gws - 1.
    """
    end_gw = start_gw + num_gws - 1
    sql = text("""
        SELECT
            f.id AS fixture_id,
            f.event AS gw,
            f.kickoff_time,
            CASE WHEN f.team_h = :team_id THEN 'H' ELSE 'A' END AS venue,
            CASE WHEN f.team_h = :team_id THEN ta.short_name ELSE th.short_name END AS opponent,
            CASE WHEN f.team_h = :team_id THEN f.team_h_difficulty ELSE f.team_a_difficulty END AS difficulty
        FROM fixtures f
        JOIN teams th ON th.id = f.team_h
        JOIN teams ta ON ta.id = f.team_a
        WHERE (f.team_h = :team_id OR f.team_a = :team_id)
          AND f.event IS NOT NULL
          AND f.event BETWEEN :start_gw AND :end_gw
        ORDER BY f.event ASC, f.kickoff_time ASC
    """)
    df = pd.read_sql(
        sql,
        engine,
        params={"team_id": int(team_id), "start_gw": int(start_gw), "end_gw": int(end_gw)},
    )
    return _clean_records(df)


def _score_player_for_upcoming_gws(
    player: dict[str, Any],
    fixtures: list[dict[str, Any]],
    next_gw: int,
) -> dict[str, Any]:
    """
    Compute a composite score for a player based on `ep_next`, `form`, `FDR`,
    home/away venue, and availability (`status` and `chance_of_playing_next_round`).
    """
    ep_next = float(player.get("ep_next") or 0.0)
    form = float(player.get("form") or 0.0)
    ppg = float(player.get("points_per_game") or 0.0)
    next_gw_fixtures = [f for f in fixtures if f["gw"] == next_gw]

    avg_fdr = (
        round(sum(float(f["difficulty"]) for f in fixtures) / len(fixtures), 2)
        if fixtures
        else None
    )

    if not next_gw_fixtures:
        fixture_bonus = -3.0
    else:
        fixture_bonus = 0.0
        for f in next_gw_fixtures:
            fdr_adj = (3.5 - float(f["difficulty"])) * 0.75
            home_adj = 0.35 if f["venue"] == "H" else 0.0
            fixture_bonus += 0.8 + fdr_adj + home_adj

    chance = player.get("chance_of_playing_next_round")
    status = (player.get("status") or "a").lower()
    if status in {"i", "s", "u"} or (chance is not None and float(chance) == 0):
        availability_mult = 0.0
    elif chance is not None and float(chance) < 100:
        availability_mult = float(chance) / 100.0
    else:
        availability_mult = 1.0

    raw_score = (0.50 * ep_next) + (0.35 * form) + (0.15 * ppg) + fixture_bonus
    final_score = round(max(0.0, raw_score) * availability_mult, 2)

    return {
        "score": final_score,
        "ep_next": ep_next,
        "form": form,
        "avg_fdr": avg_fdr,
        "matches_next_gw": len(next_gw_fixtures),
        "availability_mult": availability_mult,
    }


# =====================================================================
# GENERAL FPL TOOLS
# =====================================================================

def get_player_info(player_name: str, num_gws: int = 3) -> dict[str, Any]:
    """Get detailed FPL player information, including form, expected statistics (xG, xA, xGI), injury status, and upcoming fixtures.

    Args:
        player_name: The player's common name or full name (for example, 'Haaland', 'Palmer', 'Salah', or 'Saka').
        num_gws: Number of upcoming Gameweeks to inspect (1-5, default 3).

    Returns:
        A dictionary containing detailed player information and upcoming fixtures.
    """
    num_gws = max(1, min(int(num_gws), 5))
    df = _find_player_rows(player_name, limit=5)

    if df.empty:
        return {
            "found": False,
            "error": f"No players found matching '{player_name}'.",
        }

    records = _clean_records(df)
    primary = records[0]
    next_gw = _get_next_gw()
    upcoming = _get_team_upcoming_fixtures(primary["team_id"], next_gw, num_gws)

    avg_fdr = (
        round(sum(f["difficulty"] for f in upcoming) / len(upcoming), 2)
        if upcoming
        else None
    )

    return {
        "found": True,
        "player": primary,
        "upcoming_fixtures": upcoming,
        "avg_fixture_difficulty": avg_fdr,
        "other_matches": [r["web_name"] for r in records[1:]] if len(records) > 1 else [],
    }


def top_players(
    position: str | None = None,
    max_price: float | None = None,
    team: str | None = None,
    sort_by: str = "form",
    limit: int = 5,
    min_minutes: int = 0,
) -> dict[str, Any]:
    """Filter and rank FPL players by position, maximum price, team, and sort criterion.

    Args:
        position: Position to filter by: 'GK', 'DEF', 'MID', or 'FWD'. Empty for all positions.
        max_price: Maximum price in millions (for example, 8.0 or 6.5). Empty for no limit.
        team: Team abbreviation or name (for example, 'ARS', 'LIV', 'MCI', 'Arsenal', or 'Liverpool'). Empty for all teams.
        sort_by: Sort criterion. Defaults to 'form'.
        limit: Number of players to return (1-20, default 5).
        min_minutes: Minimum minutes played this season (default 0).

    Returns:
        A dictionary containing the applied filters and the highest-ranked players.
    """
    limit = max(1, min(int(limit), 20))
    min_minutes = max(0, int(min_minutes))

    sort_map = {
        "form": "CAST(p.form AS DECIMAL(10,2))",
        "ep_next": "CAST(p.ep_next AS DECIMAL(10,2))",
        "total_points": "p.total_points",
        "points": "p.total_points",
        "xg": "CAST(p.expected_goals AS DECIMAL(10,2))",
        "xa": "CAST(p.expected_assists AS DECIMAL(10,2))",
        "xgi": "CAST(p.expected_goal_involvements AS DECIMAL(10,2))",
        "points_per_game": "CAST(p.points_per_game AS DECIMAL(10,2))",
        "ppg": "CAST(p.points_per_game AS DECIMAL(10,2))",
        "value": "(p.total_points / NULLIF(p.price, 0))",
        "clean_sheets": "p.clean_sheets",
        "goals_scored": "p.goals_scored",
        "assists": "p.assists",
        "bonus": "p.bonus",
        "bps": "p.bps",
        "selected_by_percent": "CAST(p.selected_by_percent AS DECIMAL(10,2))",
        "ict_index": "CAST(p.ict_index AS DECIMAL(10,2))",
        "price": "p.price",
    }
    order_col = sort_map.get(sort_by.lower().strip(), "CAST(p.form AS DECIMAL(10,2))")

    conditions = ["p.minutes >= :min_minutes"]
    params: dict[str, Any] = {"min_minutes": min_minutes, "lim": limit}

    if position:
        pos_clean = position.upper().strip()
        pos_aliases = {"FW": "FWD", "ST": "FWD", "GK": "GK", "GKP": "GK", "DEF": "DEF", "DF": "DEF", "MID": "MID", "MF": "MID"}
        pos_clean = pos_aliases.get(pos_clean, pos_clean)
        conditions.append("p.position = :pos")
        params["pos"] = pos_clean

    if max_price is not None:
        conditions.append("p.price <= :max_price")
        params["max_price"] = float(max_price)

    if team:
        conditions.append("(UPPER(t.short_name) = UPPER(:team) OR LOWER(t.name) LIKE LOWER(:team_like))")
        params["team"] = team.strip()
        params["team_like"] = f"%{team.strip()}%"

    where_clause = " AND ".join(conditions)
    sql = text(f"""
        SELECT
            p.web_name,
            t.short_name AS team,
            p.position,
            p.price,
            p.form,
            p.ep_next,
            p.total_points,
            p.points_per_game,
            ROUND(p.total_points / NULLIF(p.price, 0), 2) AS value_points_per_m,
            p.minutes,
            p.goals_scored,
            p.assists,
            p.clean_sheets,
            p.expected_goals AS xG,
            p.expected_assists AS xA,
            p.expected_goal_involvements AS xGI,
            p.ict_index,
            p.selected_by_percent,
            p.status,
            p.chance_of_playing_next_round
        FROM players p
        JOIN teams t ON t.id = p.team
        WHERE {where_clause}
        ORDER BY {order_col} DESC, p.total_points DESC
        LIMIT :lim
    """)

    df = pd.read_sql(sql, engine, params=params)
    return {
        "filters": {
            "position": position,
            "max_price": max_price,
            "team": team,
            "sort_by": sort_by,
            "min_minutes": min_minutes,
            "limit": limit,
        },
        "count": len(df),
        "players": _clean_records(df),
    }


def compare_players(player_names: list[str], num_gws: int = 3) -> dict[str, Any]:
    """Compare performance, form, expected statistics (xG, xA, xGI), and upcoming fixtures for two or more players.

    Args:
        player_names: Player names to compare (for example, ['Salah', 'Palmer', 'Saka']).
        num_gws: Number of upcoming Gameweeks to analyze (1-5, default 3).

    Returns:
        A dictionary containing detailed comparisons and any names that were not found.
    """
    num_gws = max(1, min(int(num_gws), 5))
    next_gw = _get_next_gw()

    comparison_list = []
    not_found = []

    for name in player_names:
        df = _find_player_rows(name, limit=1)
        if df.empty:
            not_found.append(name)
            continue

        player = _clean_records(df)[0]
        fixtures = _get_team_upcoming_fixtures(player["team_id"], next_gw, num_gws)
        avg_fdr = (
            round(sum(f["difficulty"] for f in fixtures) / len(fixtures), 2)
            if fixtures
            else None
        )
        fixture_summary = [
            f"GW{f['gw']}: {f['opponent']} ({f['venue']}, FDR={f['difficulty']})"
            for f in fixtures
        ]

        comparison_list.append({
            "web_name": player["web_name"],
            "team": player["team_short"],
            "position": player["position"],
            "price": player["price"],
            "form": player["form"],
            "total_points": player["total_points"],
            "points_per_game": player["points_per_game"],
            "ep_next": player["ep_next"],
            "minutes": player["minutes"],
            "goals_scored": player["goals_scored"],
            "assists": player["assists"],
            "clean_sheets": player["clean_sheets"],
            "bonus": player["bonus"],
            "xG": player["xG"],
            "xA": player["xA"],
            "xGI": player["xGI"],
            "ict_index": player["ict_index"],
            "selected_by_percent": player["selected_by_percent"],
            "status": player["status"],
            "news": player["news"],
            "avg_fdr_next_gws": avg_fdr,
            "upcoming_fixtures": fixture_summary,
        })

    return {
        "start_gw": next_gw,
        "num_gws": num_gws,
        "compared_players": comparison_list,
        "not_found": not_found,
    }


def fixture_difficulty(
    team_short_name: str | None = None,
    num_gws: int = 5,
    event: int | None = None,
) -> dict[str, Any]:
    """Analyze fixture difficulty (FDR), Blank Gameweeks, and Double Gameweeks for one or all teams.

    Args:
        team_short_name: Team abbreviation or full name (for example, 'LIV', 'ARS', or 'Liverpool'). Empty for all teams.
        num_gws: Number of consecutive Gameweeks to analyze (default 5).
        event: Starting Gameweek. Empty to use the next Gameweek.

    Returns:
        A dictionary containing fixture details, average difficulty, Blank Gameweeks, and Double Gameweeks.
    """
    start_gw = int(event) if event is not None else _get_next_gw()
    num_gws = max(1, min(int(num_gws), 10))
    end_gw = start_gw + num_gws - 1
    gw_range = list(range(start_gw, end_gw + 1))

    team_filter_sql = ""
    params: dict[str, Any] = {"start_gw": start_gw, "end_gw": end_gw}
    if team_short_name:
        team_filter_sql = "WHERE UPPER(t.short_name) = UPPER(:team) OR LOWER(t.name) LIKE LOWER(:team_like)"
        params["team"] = team_short_name.strip()
        params["team_like"] = f"%{team_short_name.strip()}%"

    teams_df = pd.read_sql(
        text(f"SELECT id, name, short_name FROM teams t {team_filter_sql} ORDER BY id"),
        engine,
        params=params,
    )
    if teams_df.empty:
        return {"error": f"No team found matching '{team_short_name}'."}

    team_reports = []
    for _, t_row in teams_df.iterrows():
        t_id = int(t_row["id"])
        fixtures = _get_team_upcoming_fixtures(t_id, start_gw, num_gws)

        gw_counts = {gw: 0 for gw in gw_range}
        for f in fixtures:
            if f["gw"] in gw_counts:
                gw_counts[f["gw"]] += 1

        blank_gws = [gw for gw, cnt in gw_counts.items() if cnt == 0]
        double_gws = [gw for gw, cnt in gw_counts.items() if cnt >= 2]
        avg_fdr = (
            round(sum(f["difficulty"] for f in fixtures) / len(fixtures), 2)
            if fixtures
            else None
        )

        team_reports.append({
            "team_name": t_row["name"],
            "short_name": t_row["short_name"],
            "matches_count": len(fixtures),
            "avg_fdr": avg_fdr,
            "blank_gws": blank_gws,
            "double_gws": double_gws,
            "fixtures": fixtures if team_short_name else [
                f"GW{f['gw']}: {f['opponent']}({f['venue']},{f['difficulty']})"
                for f in fixtures
            ],
        })

    team_reports.sort(key=lambda x: (x["avg_fdr"] is None, x["avg_fdr"] or 99.0))

    return {
        "start_gw": start_gw,
        "end_gw": end_gw,
        "teams": team_reports,
    }


def gameweek_summary(gw: int | None = None) -> dict[str, Any]:
    """Summarize a Gameweek: deadline, fixtures, Double Gameweek teams, and Blank Gameweek teams.

    Args:
        gw: Gameweek number (1-38). Empty to use the next Gameweek.

    Returns:
        A dictionary containing the deadline, Gameweek status, fixtures, and Double/Blank Gameweek teams.
    """
    target_gw = int(gw) if gw is not None else _get_next_gw()

    event_sql = text("""
        SELECT
            id AS gw,
            name,
            deadline_time,
            finished,
            is_current,
            is_next,
            average_entry_score,
            highest_score
        FROM events
        WHERE id = :gw
    """)
    event_df = pd.read_sql(event_sql, engine, params={"gw": target_gw})
    if event_df.empty:
        return {"error": f"No information found for Gameweek {target_gw}."}

    fixtures_sql = text("""
        SELECT
            f.id AS fixture_id,
            f.kickoff_time,
            th.name AS home_team,
            th.short_name AS home_short,
            ta.name AS away_team,
            ta.short_name AS away_short,
            f.team_h_difficulty AS home_fdr,
            f.team_a_difficulty AS away_fdr,
            f.started,
            f.finished,
            f.team_h_score,
            f.team_a_score
        FROM fixtures f
        JOIN teams th ON th.id = f.team_h
        JOIN teams ta ON ta.id = f.team_a
        WHERE f.event = :gw
        ORDER BY f.kickoff_time ASC, f.id ASC
    """)
    fixtures_df = pd.read_sql(fixtures_sql, engine, params={"gw": target_gw})
    all_teams_df = pd.read_sql(text("SELECT short_name, name FROM teams ORDER BY id"), engine)

    fixtures_list = _clean_records(fixtures_df)
    team_match_counts: dict[str, int] = {row["short_name"]: 0 for _, row in all_teams_df.iterrows()}
    for match in fixtures_list:
        team_match_counts[match["home_short"]] = team_match_counts.get(match["home_short"], 0) + 1
        team_match_counts[match["away_short"]] = team_match_counts.get(match["away_short"], 0) + 1

    blank_teams = [t for t, c in team_match_counts.items() if c == 0]
    double_teams = [t for t, c in team_match_counts.items() if c >= 2]

    return {
        "event": _clean_records(event_df)[0],
        "total_fixtures": len(fixtures_list),
        "blank_teams": blank_teams,
        "double_gameweek_teams": double_teams,
        "fixtures": fixtures_list,
    }


# =====================================================================
# PERSONAL MANAGER TOOLS (MULTI-USER SUPPORT)
# =====================================================================

def refresh_manager(manager_id: int) -> dict[str, Any]:
    """Synchronize an FPL manager's latest profile, squad, chips, and history from the official FPL API into MySQL using `sync_manager`.

    Call this tool when a user provides their Manager ID for the first time, switches
    to a different Manager ID, or asks to refresh/update their squad data.

    Args:
        manager_id: The FPL Manager ID (Entry ID) to synchronize.

    Returns:
        A dictionary containing the synchronization status and the updated manager profile.
    """
    target_id = int(manager_id)
    try:
        sync_info = sync_manager(target_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "synced": False,
            "manager_id": target_id,
            "error": f"Failed to synchronize manager {target_id}: {exc!s}",
        }

    mgr_df = pd.read_sql(
        text("SELECT * FROM managers WHERE manager_id = :mid"),
        engine,
        params={"mid": target_id},
    )
    return {
        "synced": True,
        "manager_id": target_id,
        "sync_summary": sync_info,
        "manager": _clean_records(mgr_df)[0] if not mgr_df.empty else None,
    }


def get_my_squad(manager_id: int) -> dict[str, Any]:
    """Get a manager's current 15-player squad, remaining bank balance, team value, free transfers, and available/used chips from `managers` and `manager_squads`.

    Automatically synchronizes the manager via `sync_manager` if their data is not yet in the database.

    Args:
        manager_id: The FPL Manager ID.

    Returns:
        A dictionary containing the manager's finances, chips, captain/vice-captain, starting XI, and bench players.
    """
    target_id = int(manager_id)
    try:
        _ensure_manager_synced(target_id)
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": f"Could not load or sync manager {target_id}: {exc!s}"}

    mgr_df = pd.read_sql(
        text("SELECT * FROM managers WHERE manager_id = :mid"),
        engine,
        params={"mid": target_id},
    )
    if mgr_df.empty:
        return {"found": False, "error": f"Manager {target_id} was not found in `managers`."}

    with engine.connect() as conn:
        latest_gw = conn.execute(
            text("SELECT MAX(gw) FROM manager_squads WHERE manager_id = :mid"),
            {"mid": target_id},
        ).scalar()

    if latest_gw is None:
        return {"found": False, "error": f"No squad found in `manager_squads` for manager {target_id}."}

    squad_sql = text("""
        SELECT
            ms.squad_position,
            ms.multiplier,
            ms.is_captain,
            ms.is_vice_captain,
            p.id AS player_id,
            p.web_name,
            CONCAT(p.first_name, ' ', p.second_name) AS full_name,
            t.id AS team_id,
            t.short_name AS team,
            p.position,
            p.price,
            p.form,
            p.ep_next,
            p.total_points,
            p.points_per_game,
            p.minutes,
            p.status,
            p.news,
            p.chance_of_playing_next_round
        FROM manager_squads ms
        JOIN players p ON p.id = ms.player_id
        JOIN teams t ON t.id = p.team
        WHERE ms.manager_id = :mid AND ms.gw = :gw
        ORDER BY ms.squad_position ASC
    """)
    squad_df = pd.read_sql(squad_sql, engine, params={"mid": target_id, "gw": int(latest_gw)})
    chips_df = pd.read_sql(
        text("SELECT gw, chip_name, used_time FROM manager_chips WHERE manager_id = :mid ORDER BY gw ASC"),
        engine,
        params={"mid": target_id},
    )

    mgr_info = _clean_records(mgr_df)[0]
    mgr_info["bank_balance_m"] = _normalize_money(mgr_info.get("bank_balance"))
    mgr_info["team_value_m"] = _normalize_money(mgr_info.get("team_value"))

    squad_players = _clean_records(squad_df)
    starting_xi = [p for p in squad_players if (p.get("squad_position") or 0) <= 11]
    bench = [p for p in squad_players if (p.get("squad_position") or 0) > 11]
    captain = next((p["web_name"] for p in squad_players if p.get("is_captain")), None)
    vice_captain = next((p["web_name"] for p in squad_players if p.get("is_vice_captain")), None)

    return {
        "found": True,
        "manager": mgr_info,
        "squad_gw": int(latest_gw),
        "bank_balance_m": mgr_info["bank_balance_m"],
        "team_value_m": mgr_info["team_value_m"],
        "free_transfers": mgr_info.get("free_transfers"),
        "available_chips": mgr_info.get("available_chips"),
        "chips_used": _clean_records(chips_df),
        "current_captain": captain,
        "current_vice_captain": vice_captain,
        "starting_xi": starting_xi,
        "bench": bench,
    }


def analyze_my_squad(manager_id: int, num_gws: int = 3) -> dict[str, Any]:
    """Analyze a manager's squad for injuries, suspensions, Blank/Double Gameweeks, poor form, and fixture difficulty using `manager_squads`, `players`, and `fixtures`.

    Args:
        manager_id: The FPL Manager ID.
        num_gws: Number of upcoming Gameweeks to inspect for fixture runs (1-5, default 3).

    Returns:
        A dictionary highlighting injured/doubtful players, players with Blank or Double Gameweeks, form warnings, and fixture runs.
    """
    num_gws = max(1, min(int(num_gws), 5))
    squad_data = get_my_squad(manager_id)
    if not squad_data.get("found"):
        return squad_data

    next_gw = _get_next_gw()
    all_players = squad_data["starting_xi"] + squad_data["bench"]

    injured_or_doubtful = []
    blank_next_gw = []
    double_next_gw = []
    low_form_players = []
    tough_fixture_runs = []
    easy_fixture_runs = []

    for p in all_players:
        fixtures = _get_team_upcoming_fixtures(p["team_id"], next_gw, num_gws)
        next_gw_matches = [f for f in fixtures if f["gw"] == next_gw]
        avg_fdr = (
            round(sum(float(f["difficulty"]) for f in fixtures) / len(fixtures), 2)
            if fixtures
            else None
        )
        fixture_str = [
            f"GW{f['gw']}: {f['opponent']} ({f['venue']}, FDR={f['difficulty']})"
            for f in fixtures
        ]

        role = "Starting XI" if (p.get("squad_position") or 0) <= 11 else f"Bench #{(p.get('squad_position') or 12) - 11}"
        status = (p.get("status") or "a").lower()
        chance = p.get("chance_of_playing_next_round")
        news = p.get("news")

        player_summary = {
            "web_name": p["web_name"],
            "team": p["team"],
            "position": p["position"],
            "role": role,
            "price": p["price"],
            "form": float(p.get("form") or 0.0),
            "ep_next": float(p.get("ep_next") or 0.0),
            "status": status,
            "chance_of_playing_next_round": chance,
            "news": news,
            "matches_in_next_gw": len(next_gw_matches),
            "avg_fdr": avg_fdr,
            "upcoming_fixtures": fixture_str,
        }

        if status != "a" or (chance is not None and float(chance) < 100) or (news and str(news).strip()):
            injured_or_doubtful.append(player_summary)

        if len(next_gw_matches) == 0:
            blank_next_gw.append(player_summary)
        elif len(next_gw_matches) >= 2:
            double_next_gw.append(player_summary)

        if float(p.get("form") or 0.0) < 3.0:
            low_form_players.append(player_summary)

        if avg_fdr is not None and avg_fdr >= 3.8:
            tough_fixture_runs.append(player_summary)
        elif avg_fdr is not None and avg_fdr <= 2.5:
            easy_fixture_runs.append(player_summary)

    club_counts = Counter(p["team"] for p in all_players)

    return {
        "manager": squad_data["manager"],
        "squad_gw": squad_data["squad_gw"],
        "upcoming_gw": next_gw,
        "num_gws_analyzed": num_gws,
        "injured_or_doubtful_players": injured_or_doubtful,
        "blank_gw_players_next_round": blank_next_gw,
        "double_gw_players_next_round": double_next_gw,
        "low_form_players": low_form_players,
        "tough_fixture_players": tough_fixture_runs,
        "favorable_fixture_players": easy_fixture_runs,
        "club_distribution": dict(club_counts),
    }


def suggest_transfers(
    manager_id: int,
    budget: float | None = None,
    position: str | None = None,
    num_gws: int = 3,
    limit: int = 5,
) -> dict[str, Any]:
    """Recommend who to sell and who to buy (`player_out` -> `player_in`) for a manager's squad.

    Enforces FPL rules:
      1. Same position (`GK`, `DEF`, `MID`, or `FWD`).
      2. Within available budget (`player_out.price + bank_balance`, or the custom `budget` override).
      3. Maximum 3 players from the same Premier League club.
      4. Accounts for a -4 point hit if `free_transfers == 0`.
      5. Ranks replacements by `form`, `ep_next`, and upcoming fixture difficulty (`FDR`).

    Args:
        manager_id: The FPL Manager ID.
        budget: Optional custom bank balance in millions (£m). Leave empty to use the manager's actual `bank_balance` from the database.
        position: Optional position filter ('GK', 'DEF', 'MID', 'FWD') if the user only wants to transfer a specific position.
        num_gws: Number of upcoming Gameweeks to evaluate fixtures over (1-5, default 3).
        limit: Number of transfer pairs to return (1-10, default 5).

    Returns:
        A dictionary containing transfer budget details, free transfer / point hit status, and ranked transfer recommendations.
    """
    num_gws = max(1, min(int(num_gws), 5))
    limit = max(1, min(int(limit), 10))

    squad_data = get_my_squad(manager_id)
    if not squad_data.get("found"):
        return squad_data

    next_gw = _get_next_gw()
    mgr = squad_data["manager"]
    bank_m = float(budget) if budget is not None else float(squad_data["bank_balance_m"] or 0.0)
    free_transfers = int(mgr.get("free_transfers") if mgr.get("free_transfers") is not None else 1)
    points_hit = 0 if free_transfers >= 1 else -4

    all_squad = squad_data["starting_xi"] + squad_data["bench"]
    squad_player_ids = {int(p["player_id"]) for p in all_squad}
    club_counts = Counter(int(p["team_id"]) for p in all_squad)

    pos_filter = None
    if position:
        pos_aliases = {"FW": "FWD", "ST": "FWD", "GK": "GK", "GKP": "GK", "DEF": "DEF", "DF": "DEF", "MID": "MID", "MF": "MID"}
        pos_filter = pos_aliases.get(position.upper().strip(), position.upper().strip())

    teams_df = pd.read_sql(text("SELECT id FROM teams"), engine)
    team_fixtures_map: dict[int, list[dict[str, Any]]] = {
        int(row["id"]): _get_team_upcoming_fixtures(int(row["id"]), next_gw, num_gws)
        for _, row in teams_df.iterrows()
    }

    squad_evaluated = []
    for p in all_squad:
        if pos_filter and p["position"] != pos_filter:
            continue
        t_fixtures = team_fixtures_map.get(int(p["team_id"]), [])
        metrics = _score_player_for_upcoming_gws(p, t_fixtures, next_gw)
        squad_evaluated.append({**p, **metrics, "fixtures": t_fixtures})

    squad_evaluated.sort(key=lambda x: (x["score"], x["ep_next"], x["form"]))
    weakest_players = squad_evaluated[:6]

    market_sql = text("""
        SELECT
            p.id AS player_id,
            p.web_name,
            t.id AS team_id,
            t.short_name AS team,
            p.position,
            p.price,
            p.form,
            p.ep_next,
            p.total_points,
            p.points_per_game,
            p.minutes,
            p.status,
            p.news,
            p.chance_of_playing_next_round
        FROM players p
        JOIN teams t ON t.id = p.team
        WHERE p.status = 'a'
          AND (p.chance_of_playing_next_round IS NULL OR p.chance_of_playing_next_round >= 75)
        ORDER BY CAST(p.ep_next AS DECIMAL(10,2)) DESC, CAST(p.form AS DECIMAL(10,2)) DESC
        LIMIT 120
    """)
    market_df = pd.read_sql(market_sql, engine)
    market_players = _clean_records(market_df)

    market_evaluated = []
    for mp in market_players:
        if int(mp["player_id"]) in squad_player_ids:
            continue
        if pos_filter and mp["position"] != pos_filter:
            continue
        t_fixtures = team_fixtures_map.get(int(mp["team_id"]), [])
        metrics = _score_player_for_upcoming_gws(mp, t_fixtures, next_gw)
        if metrics["matches_next_gw"] == 0:
            continue
        market_evaluated.append({**mp, **metrics, "fixtures": t_fixtures})

    recommendations = []
    used_pairs: set[tuple[int, int]] = set()

    for p_out in weakest_players:
        max_affordable = round(float(p_out["price"]) + bank_m, 1)
        out_team_id = int(p_out["team_id"])

        best_for_this_out = []
        for p_in in market_evaluated:
            if p_in["position"] != p_out["position"]:
                continue
            if float(p_in["price"]) > max_affordable + 1e-6:
                continue

            in_team_id = int(p_in["team_id"])
            current_in_team_count = club_counts[in_team_id] - (1 if in_team_id == out_team_id else 0)
            if current_in_team_count >= 3:
                continue

            score_gain = round(p_in["score"] - p_out["score"], 2)
            ep_gain_next_gw = round((p_in["ep_next"] - p_out["ep_next"]) + points_hit, 2)
            if score_gain <= 0.5:
                continue

            pair_key = (int(p_out["player_id"]), int(p_in["player_id"]))
            if pair_key in used_pairs:
                continue
            used_pairs.add(pair_key)

            best_for_this_out.append({
                "position": p_out["position"],
                "player_out": {
                    "web_name": p_out["web_name"],
                    "team": p_out["team"],
                    "price": p_out["price"],
                    "form": p_out["form"],
                    "ep_next": p_out["ep_next"],
                    "avg_fdr": p_out["avg_fdr"],
                    "status": p_out["status"],
                    "news": p_out.get("news"),
                },
                "player_in": {
                    "web_name": p_in["web_name"],
                    "team": p_in["team"],
                    "price": p_in["price"],
                    "form": p_in["form"],
                    "ep_next": p_in["ep_next"],
                    "avg_fdr": p_in["avg_fdr"],
                    "upcoming_fixtures": [
                        f"GW{f['gw']}: {f['opponent']} ({f['venue']}, FDR={f['difficulty']})"
                        for f in p_in["fixtures"]
                    ],
                },
                "max_budget_for_slot": max_affordable,
                "bank_remaining_after_transfer": round(max_affordable - float(p_in["price"]), 1),
                "free_transfers_available": free_transfers,
                "points_hit": points_hit,
                "net_ep_gain_next_gw": ep_gain_next_gw,
                "transfer_rating_gain": score_gain,
            })

        best_for_this_out.sort(key=lambda x: (x["transfer_rating_gain"], x["net_ep_gain_next_gw"]), reverse=True)
        recommendations.extend(best_for_this_out[:2])

    recommendations.sort(key=lambda x: (x["transfer_rating_gain"], x["net_ep_gain_next_gw"]), reverse=True)

    return {
        "manager_id": int(manager_id),
        "upcoming_gw": next_gw,
        "bank_balance_m": bank_m,
        "free_transfers": free_transfers,
        "points_hit_if_transfer_made": points_hit,
        "position_filter": pos_filter,
        "recommendations": recommendations[:limit],
    }


def suggest_captain(
    manager_id: int,
    num_gws: int = 1,
    limit: int = 5,
) -> dict[str, Any]:
    """Recommend the best captain and vice-captain from the manager's current squad based on `ep_next`, `FDR`, home/away venue, and playing chance.

    Args:
        manager_id: The FPL Manager ID.
        num_gws: Number of upcoming Gameweeks to inspect (1-3, default 1).
        limit: Number of ranked captain candidates to return (1-15, default 5).

    Returns:
        A dictionary containing the recommended captain, vice-captain, and ranked squad candidates.
    """
    num_gws = max(1, min(int(num_gws), 3))
    limit = max(1, min(int(limit), 15))

    squad_data = get_my_squad(manager_id)
    if not squad_data.get("found"):
        return squad_data

    next_gw = _get_next_gw()
    all_squad_players = squad_data["starting_xi"] + squad_data["bench"]

    candidates = []
    for p in all_squad_players:
        fixtures = _get_team_upcoming_fixtures(p["team_id"], next_gw, num_gws)
        metrics = _score_player_for_upcoming_gws(p, fixtures, next_gw)

        candidates.append({
            "web_name": p["web_name"],
            "team": p["team"],
            "position": p["position"],
            "price": p["price"],
            "squad_role": "Starting XI" if (p.get("squad_position") or 0) <= 11 else "Bench",
            "is_current_captain": bool(p.get("is_captain")),
            "is_current_vice_captain": bool(p.get("is_vice_captain")),
            "captain_score": metrics["score"],
            "ep_next": metrics["ep_next"],
            "form": metrics["form"],
            "total_points": p["total_points"],
            "status": p["status"],
            "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
            "news": p.get("news"),
            "matches_in_next_gw": metrics["matches_next_gw"],
            "avg_fdr": metrics["avg_fdr"],
            "upcoming_fixtures": [
                f"GW{f['gw']}: {f['opponent']} ({f['venue']}, FDR={f['difficulty']})"
                for f in fixtures
            ],
        })

    candidates.sort(key=lambda x: (x["captain_score"], x["ep_next"], x["form"]), reverse=True)
    top_list = candidates[:limit]

    return {
        "manager_id": int(manager_id),
        "squad_gw": squad_data["squad_gw"],
        "target_gw": next_gw,
        "current_captain": squad_data["current_captain"],
        "current_vice_captain": squad_data["current_vice_captain"],
        "recommended_captain": top_list[0]["web_name"] if top_list else None,
        "recommended_vice_captain": top_list[1]["web_name"] if len(top_list) > 1 else None,
        "candidates": top_list,
    }


# =====================================================================
# FALLBACK READ-ONLY SQL TOOL
# =====================================================================

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|CALL|EXEC|SHOW\s+GRANTS)\b",
    re.IGNORECASE,
)


def run_readonly_sql(sql_query: str) -> dict[str, Any]:
    """Execute a read-only MySQL SELECT when an IN-SCOPE FPL statistical request cannot be handled by a specialized tool.

    Main MySQL table structures available for SELECT queries:
      - teams(id, name, short_name, strength, strength_overall_home, strength_overall_away, strength_attack_home, strength_attack_away, strength_defence_home, strength_defence_away)
      - players(id, code, web_name, first_name, second_name, team, element_type, position, price, now_cost, total_points, form, points_per_game, ep_next, minutes, starts, goals_scored, assists, clean_sheets, goals_conceded, yellow_cards, red_cards, saves, bonus, bps, expected_goals, expected_assists, expected_goal_involvements, expected_goals_conceded, ict_index, selected_by_percent, status, news, chance_of_playing_next_round)
      - fixtures(id, event, team_h, team_a, team_h_score, team_a_score, team_h_difficulty, team_a_difficulty, kickoff_time, finished, started)
      - events(id, name, deadline_time, finished, is_current, is_next, average_entry_score, highest_score)
      - player_history(element, fixture, opponent_team, total_points, was_home, kickoff_time, round, minutes, goals_scored, assists, clean_sheets, goals_conceded, bps, value, selected)
      - managers(manager_id, player_first_name, player_last_name, team_name, region_name, current_gw, overall_points, overall_rank, team_value, bank_balance, free_transfers, available_chips, updated_at)
      - manager_gw_history(manager_id, gw, points, total_points, overall_rank, gw_rank, bank, team_value, event_transfers, event_transfers_cost, points_on_bench)
      - manager_squads(manager_id, gw, player_id, squad_position, multiplier, is_captain, is_vice_captain)
      - manager_chips(id, manager_id, gw, chip_name, used_time)

    IMPORTANT DATABASE SCOPE RULES:
      - The tables above DO NOT store real-life club head coaches/managers, real-life club captains, referee names, or external football news. Do NOT call `run_readonly_sql` for those questions; call `search_premier_league_web` instead.
      - When querying `fixtures` for a team `t.id`:
        * Clean sheet condition: `(f.team_h = t.id AND f.team_a_score = 0) OR (f.team_a = t.id AND f.team_h_score = 0)`
        * Goals scored by `t.id`: `CASE WHEN f.team_h = t.id THEN f.team_h_score ELSE f.team_a_score END`
        * Goals conceded by `t.id`: `CASE WHEN f.team_h = t.id THEN f.team_a_score ELSE f.team_h_score END`

    Args:
        sql_query: A valid MySQL SELECT query. Only SELECT or WITH ... SELECT queries are allowed, with one statement maximum.

    Returns:
        A dictionary containing up to 50 result rows or an SQL error for the model to correct.
    """
    cleaned_sql = sql_query.strip().rstrip(";")

    if ";" in cleaned_sql:
        return {"error": "Only one SELECT statement is allowed."}

    if not cleaned_sql.upper().startswith(("SELECT", "WITH")):
        return {"error": "The query must start with SELECT or WITH."}

    if _FORBIDDEN_SQL_PATTERN.search(cleaned_sql):
        return {"error": "Execution rejected: a forbidden SQL keyword was found in read-only mode."}

    if "LIMIT" not in cleaned_sql.upper():
        cleaned_sql = f"{cleaned_sql} LIMIT 50"

    try:
        df = pd.read_sql(text(cleaned_sql), engine)
        return {
            "sql_executed": cleaned_sql,
            "row_count": len(df),
            "rows": _clean_records(df),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "sql_executed": cleaned_sql,
            "error": f"SQL execution failed: {exc!s}. Check the table and column names, then try again.",
        }


# =====================================================================
# REAL-TIME GOOGLE SEARCH TOOL FOR RELATED PREMIER LEAGUE / FPL FACTS
# =====================================================================

def search_premier_league_web(question: str) -> dict[str, Any]:
    """Search Google in real time for Premier League and FPL-related questions whose answers are NOT stored in the MySQL database (for example, who is the current head coach/manager of Manchester United, who is a club's real-life captain, or latest press conference news).

    Do NOT call this tool for topics unrelated to football/Premier League/FPL.

    Args:
        question: The Premier League or FPL question to search on Google.

    Returns:
        A dictionary containing the real-time web search answer grounded on the current Premier League clubs in the database.
    """
    try:
        teams_df = pd.read_sql(text("SELECT name, short_name FROM teams ORDER BY name"), engine)
        current_teams = [f"{r['name']} ({r['short_name']})" for _, r in teams_df.iterrows()]
    except Exception:  # noqa: BLE001
        current_teams = []

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "in_database": False,
            "error": "GEMINI_API_KEY is missing; cannot run Google Search.",
        }

    teams_context = (
        f"The 20 Premier League clubs in the current season database are: {', '.join(current_teams)}. "
        if current_teams
        else ""
    )

    try:
        client = genai.Client(api_key=api_key)
        search_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                f"{teams_context}Use Google Search to find the most up-to-date, current information "
                f"to answer this Premier League / FPL question accurately (do not rely on outdated past seasons): {question}"
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
            ),
        )
        return {
            "in_database": False,
            "source": "Google Search (Real-time)",
            "current_season_clubs": current_teams,
            "answer": search_response.text,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "in_database": False,
            "error": f"Google Search lookup failed: {exc!s}",
        }


ALL_TOOLS = [
    get_player_info,
    top_players,
    compare_players,
    fixture_difficulty,
    gameweek_summary,
    refresh_manager,
    get_my_squad,
    analyze_my_squad,
    suggest_transfers,
    suggest_captain,
    run_readonly_sql,
    search_premier_league_web,
]

TOOL_MAP = {func.__name__: func for func in ALL_TOOLS}