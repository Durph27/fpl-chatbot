"""Synchronize an FPL manager's profile, squad, history, and chips to MySQL."""

from typing import Any

import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.dbutil import clean, ensure_table, upsert
from app.ingest import fetch

ALL_CHIPS = ("wildcard", "freehit", "bboost", "3xc")
MANAGER_TABLES = ("manager_squads", "manager_gw_history", "manager_chips", "managers")

MANAGER_SCHEMAS = (
    (
        "managers",
        ("manager_id", "player_first_name", "player_last_name", "team_name", "region_name",
         "current_gw", "overall_points", "overall_rank", "team_value", "bank_balance",
         "free_transfers", "available_chips", "updated_at"),
        ["manager_id"],
        {
            "manager_id": "INT", "player_first_name": "VARCHAR(100)",
            "player_last_name": "VARCHAR(100)", "team_name": "VARCHAR(150)",
            "region_name": "VARCHAR(100)", "current_gw": "INT", "overall_points": "INT",
            "overall_rank": "INT", "team_value": "DECIMAL(5,1)",
            "bank_balance": "DECIMAL(4,1)", "free_transfers": "INT",
            "available_chips": "JSON",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        },
        [], [], [],
    ),
    (
        "manager_squads",
        ("manager_id", "gw", "player_id", "squad_position", "multiplier", "is_captain",
         "is_vice_captain"),
        ["manager_id", "gw", "player_id"],
        {
            "manager_id": "INT", "gw": "BIGINT", "player_id": "BIGINT",
            "squad_position": "INT", "multiplier": "INT", "is_captain": "BOOLEAN",
            "is_vice_captain": "BOOLEAN",
        },
        [], [
            ("manager_id", "managers", "manager_id"),
            ("player_id", "players", "id"),
            ("gw", "events", "id"),
        ], [],
    ),
    (
        "manager_gw_history",
        ("manager_id", "gw", "points", "total_points", "overall_rank", "gw_rank", "bank",
         "team_value", "event_transfers", "event_transfers_cost", "points_on_bench"),
        ["manager_id", "gw"],
        {
            "manager_id": "INT", "gw": "BIGINT", "points": "INT", "total_points": "INT",
            "overall_rank": "INT", "gw_rank": "INT", "bank": "DECIMAL(4,1)",
            "team_value": "DECIMAL(5,1)", "event_transfers": "INT",
            "event_transfers_cost": "INT", "points_on_bench": "INT",
        },
        [], [("manager_id", "managers", "manager_id")], [],
    ),
    (
        "manager_chips",
        ("id", "manager_id", "gw", "chip_name", "used_time"),
        ["id"],
        {
            "id": "INT AUTO_INCREMENT", "manager_id": "INT", "gw": "INT",
            "chip_name": "VARCHAR(30)", "used_time": "VARCHAR(50)",
        },
        [], [("manager_id", "managers", "manager_id")], [("manager_id", "gw", "chip_name")],
    ),
)


def ensure_manager_tables() -> None:
    """Create manager tables in parent-to-child order with the shared helper."""
    for name, columns, pk, types, indexes, fks, uniques in MANAGER_SCHEMAS:
        ensure_table(name, pd.DataFrame(columns=columns), pk, types, indexes, fks, uniques)


def calculate_free_transfers(
    gw_history: list[dict[str, Any]], chips: list[dict[str, Any]]
) -> int:
    """Estimate the free transfers available for the next gameweek (maximum 5)."""
    chip_by_gw = {chip.get("event"): chip.get("name") for chip in chips}
    free_transfers = 1
    for row in sorted(gw_history, key=lambda item: item["event"]):
        chip = chip_by_gw.get(row["event"])
        if chip == "wildcard":
            free_transfers = 1
        elif chip != "freehit":
            free_transfers = min(
                5,
                max(0, free_transfers - int(row.get("event_transfers") or 0)) + 1,
            )
    return free_transfers


def build_manager_frames(
    manager_id: int,
    info: dict[str, Any],
    gw_history: list[dict[str, Any]],
    chips: list[dict[str, Any]],
    current_gw: int,
    picks_data: dict[str, Any],
) -> dict[str, pd.DataFrame]:
    """Convert FPL manager API responses into DataFrames for the shared writer."""
    entry_history = picks_data.get("entry_history", {})
    picks = picks_data.get("picks", [])
    used_chips = {chip.get("name") for chip in chips}
    available_chips = [chip for chip in ALL_CHIPS if chip not in used_chips]
    free_transfers = calculate_free_transfers(gw_history, chips)

    manager = clean(pd.DataFrame([{
        "manager_id": manager_id,
        "player_first_name": info.get("player_first_name", ""),
        "player_last_name": info.get("player_last_name", ""),
        "team_name": info.get("name", ""),
        "region_name": info.get("player_region_name", ""),
        "current_gw": current_gw,
        "overall_points": info.get("summary_overall_points") or 0,
        "overall_rank": info.get("summary_overall_rank") or 0,
        "team_value": entry_history.get("value", info.get("last_deadline_value", 1000)) / 10,
        "bank_balance": entry_history.get("bank", info.get("last_deadline_bank", 0)) / 10,
        "free_transfers": free_transfers,
        "available_chips": available_chips,
    }]))

    history = clean(pd.DataFrame([{
        "manager_id": manager_id,
        "gw": row["event"],
        "points": row.get("points", 0),
        "total_points": row.get("total_points", 0),
        "overall_rank": row.get("overall_rank", 0),
        "gw_rank": row.get("rank", 0),
        "bank": (row.get("bank") or 0) / 10,
        "team_value": (row.get("value") or 1000) / 10,
        "event_transfers": row.get("event_transfers", 0),
        "event_transfers_cost": row.get("event_transfers_cost", 0),
        "points_on_bench": row.get("points_on_bench", 0),
    } for row in gw_history]))

    squads = clean(pd.DataFrame([{
        "manager_id": manager_id,
        "gw": current_gw,
        "player_id": pick["element"],
        "squad_position": pick["position"],
        "multiplier": pick["multiplier"],
        "is_captain": pick["is_captain"],
        "is_vice_captain": pick["is_vice_captain"],
    } for pick in picks]))

    chip_rows = clean(pd.DataFrame([{
        "manager_id": manager_id,
        "gw": chip["event"],
        "chip_name": chip["name"],
        "used_time": chip.get("time", ""),
    } for chip in chips]))
    return {"managers": manager, "manager_gw_history": history,
            "manager_squads": squads, "manager_chips": chip_rows}


def sync_manager(manager_id: int) -> dict[str, Any]:
    """Fetch one FPL entry and persist it with the shared DataFrame utilities."""
    if manager_id < 1:
        raise ValueError("manager_id must be a positive FPL entry ID")

    info = fetch(f"entry/{manager_id}/")
    history_data = fetch(f"entry/{manager_id}/history/")
    gw_history = history_data.get("current", [])
    chips = history_data.get("chips", [])
    used_chips = {chip.get("name") for chip in chips}
    current_gw = info.get("current_event") or (gw_history[-1]["event"] if gw_history else 1)
    picks_data = fetch(f"entry/{manager_id}/event/{current_gw}/picks/")
    frames = build_manager_frames(
        manager_id, info, gw_history, chips, current_gw, picks_data,
    )

    ensure_manager_tables()
    with engine.begin() as conn:
        upsert(frames["managers"], "managers", conn)
        if not frames["manager_gw_history"].empty:
            upsert(frames["manager_gw_history"], "manager_gw_history", conn)
        if not frames["manager_chips"].empty:
            upsert(frames["manager_chips"], "manager_chips", conn,
                   key_columns=["manager_id", "gw", "chip_name"])
        # A squad is a snapshot, so replace the old snapshot for this gameweek.
        conn.execute(text("DELETE FROM manager_squads WHERE manager_id = :manager_id AND gw = :gw"),
                     {"manager_id": manager_id, "gw": current_gw})
        if not frames["manager_squads"].empty:
            upsert(frames["manager_squads"], "manager_squads", conn)

    manager = frames["managers"].iloc[0]
    return {
        "manager_id": manager_id,
        "team_name": manager["team_name"],
        "current_gw": current_gw,
        "squad_count": len(frames["manager_squads"]),
        "free_transfers": int(manager["free_transfers"]),
        "available_chips": [chip for chip in ALL_CHIPS if chip not in used_chips],
    }


if __name__ == "__main__":
    print(sync_manager(4903271))
