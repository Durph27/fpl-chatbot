import time

import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.dbutil import clean, ensure_table, upsert
from app.ingest import fetch


def known_ids(table: str, col: str = "id") -> set:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(f"SELECT `{col}` FROM `{table}`"))}


def write_batch(hist: list, past: list):
    if hist:
        df = clean(pd.DataFrame(hist))
        # Only keep rows whose fixture already exists (avoid foreign key violation)
        ok = df["fixture"].isin(known_ids("fixtures"))
        if (~ok).any():
            print(f"  ! skipping {int((~ok).sum())} rows with fixture not yet in DB "
                  f"(re-run after syncing bootstrap)")
        df = df[ok]
        if not df.empty:
            # A player can play 2 matches per round (double gameweek) -> key is (element, fixture)
            ensure_table(
                "player_history", df, ["element", "fixture"],
                indexes=["round", "opponent_team"],
                fks=[("element", "players", "id"), ("fixture", "fixtures", "id"),
                     ("opponent_team", "teams", "id"), ("round", "events", "id")])
            with engine.begin() as conn:
                upsert(df, "player_history", conn)
    if past:
        df = clean(pd.DataFrame(past))
        df = df[df["element_code"].isin(known_ids("players", "code"))]
        if not df.empty:
            ensure_table(
                "player_history_past", df, ["element_code", "season_name"],
                types={"season_name": "VARCHAR(20)"},
                fks=[("element_code", "players", "code")])
            with engine.begin() as conn:
                upsert(df, "player_history_past", conn)


def ingest_history(batch_size: int = 50, pause: float = 0.6):
    ids = sorted(known_ids("players"))
    hist, past, failed = [], [], []
    for i, pid in enumerate(ids, 1):
        try:
            data = fetch(f"element-summary/{pid}/")
            hist.extend(data.get("history", []))
            past.extend(data.get("history_past", []))
        except Exception as e:  # noqa: BLE001
            failed.append(pid)
            print(f"  ! error for player {pid}: {e}")
        time.sleep(pause)                

        if i % batch_size == 0 or i == len(ids):
            write_batch(hist, past)       # write in batches so a network drop doesn't lose everything
            hist.clear()
            past.clear()
            print(f"Processed {i}/{len(ids)} players")

    if failed:
        print("Failed players (re-run to retry):", failed)


if __name__ == "__main__":
    ingest_history()