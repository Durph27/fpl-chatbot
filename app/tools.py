"""MySQL query tools for the FPL Chatbot.

Compatible with the table structure in `app/ingest.py` and the Google GenAI SDK.
Includes five specialized tools and the safe `run_readonly_sql` fallback for
unexpected statistical questions.
"""

import math
import re
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text

from app.db import engine


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
          AND f.event B ETWEEN :start_gw AND :end_gw
        ORDER BY f.event ASC, f.kickoff_time ASC
    """)
    df = pd.read_sql(
        sql,
        engine,
        params={"team_id": int(team_id), "start_gw": int(start_gw), "end_gw": int(end_gw)},
    )
    return _clean_records(df)


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


_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|CALL|EXEC|SHOW\s+GRANTS)\b",
    re.IGNORECASE,
)


def run_readonly_sql(sql_query: str) -> dict[str, Any]:
    """Execute a read-only MySQL SELECT when the request cannot be handled by a specialized tool.

    Main MySQL table structures available for SELECT queries:
      - teams(id, name, short_name, strength, strength_overall_home, strength_overall_away, strength_attack_home, strength_attack_away, strength_defence_home, strength_defence_away)
      - players(id, code, web_name, first_name, second_name, team, element_type, position, price, now_cost, total_points, form, points_per_game, ep_next, minutes, starts, goals_scored, assists, clean_sheets, goals_conceded, yellow_cards, red_cards, saves, bonus, bps, expected_goals, expected_assists, expected_goal_involvements, expected_goals_conceded, ict_index, selected_by_percent, status, news, chance_of_playing_next_round)
      - fixtures(id, event, team_h, team_a, team_h_score, team_a_score, team_h_difficulty, team_a_difficulty, kickoff_time, finished, started)
      - events(id, name, deadline_time, finished, is_current, is_next, average_entry_score, highest_score)
      - player_history(element, fixture, opponent_team, total_points, was_home, kickoff_time, round, minutes, goals_scored, assists, clean_sheets, goals_conceded, bps, value, selected)

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


ALL_TOOLS = [
    get_player_info,
    top_players,
    compare_players,
    fixture_difficulty,
    gameweek_summary,
    run_readonly_sql,
]

TOOL_MAP = {func.__name__: func for func in ALL_TOOLS}