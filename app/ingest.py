import gzip
import json
import pathlib
import time
from datetime import UTC, datetime

import pandas as pd
import requests

from app.db import engine
from app.dbutil import clean, ensure_table, upsert

BASE = "https://fantasy.premierleague.com/api"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (FPL chatbot project)"})
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch(path: str, retries: int = 3):
    """Call the API, automatically retry on network errors, 429 (rate limited) or 503 (FPL updating)."""
    for attempt in range(retries):
        try:
            r = SESSION.get(f"{BASE}/{path}", timeout=30)
            if r.status_code in (429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {path}")


def save_raw(name: str, obj):
    """Save raw JSON (gzip-compressed) to data/raw/ for future reconstruction."""
    d = pathlib.Path("data/raw")
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    with gzip.open(d / f"{name}_{stamp}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(obj, f)


def build_players(boot: dict) -> pd.DataFrame:
    p = pd.DataFrame(boot["elements"])
    p["position"] = p["element_type"].map(POS)
    p["price"] = p["now_cost"] / 10                  # API stores price x10
    p = clean(p)
    p["synced_at"] = pd.Timestamp.now("UTC").tz_localize(None)
    return p


def flatten_fixture_stats(fx_raw: list) -> pd.DataFrame:
    """fixtures[].stats is nested -> flatten into a table for SQL querying."""
    rows = []
    for f in fx_raw:
        for s in f.get("stats", []):
            for side in ("h", "a"):
                for item in s.get(side, []):
                    rows.append({"fixture_id": f["id"], "identifier": s["identifier"],
                                 "side": side, "element": item["element"],
                                 "value": item["value"]})
    return pd.DataFrame(rows)


def sync_bootstrap():
    # 1) Call API first, don't touch DB yet
    boot = fetch("bootstrap-static/")
    fx_raw = fetch("fixtures/")
    save_raw("bootstrap", boot)
    save_raw("fixtures", fx_raw)

    # 2) Build DataFrames
    players = build_players(boot)
    snap = players.copy()
    snap["snapshot_date"] = datetime.now(UTC).date()
    fx = clean(pd.DataFrame(fx_raw).drop(columns=["stats"], errors="ignore"))

    # 3) Table list IN PARENT -> CHILD ORDER
    # (name, data, primary key, forced types, indexes, foreign keys, unique)
    specs = [
        ("element_types", clean(pd.DataFrame(boot.get("element_types", []))),
         ["id"], {}, [], [], []),
        ("teams", clean(pd.DataFrame(boot["teams"])), ["id"], {}, [], [], []),
        ("events", clean(pd.DataFrame(boot["events"])), ["id"], {}, [], [], []),
        ("phases", clean(pd.DataFrame(boot.get("phases", []))), ["id"], {}, [], [], []),
        ("chips", clean(pd.DataFrame(boot.get("chips", []))), ["id"], {}, [], [], []),
        ("players", players, ["id"], {}, ["team", "element_type"],
         [("team", "teams", "id"), ("element_type", "element_types", "id")],
         ["code"]),
        ("fixtures", fx, ["id"], {}, ["event", "team_h", "team_a"],
         [("event", "events", "id"), ("team_h", "teams", "id"),
          ("team_a", "teams", "id")], []),
        ("player_snapshots", snap, ["id", "snapshot_date"],
         {"snapshot_date": "DATE"}, [], [("id", "players", "id")], []),
        ("fixture_stats", flatten_fixture_stats(fx_raw),
         ["fixture_id", "identifier", "side", "element"],
         {"identifier": "VARCHAR(50)", "side": "CHAR(1)"}, ["element"],
         [("fixture_id", "fixtures", "id"), ("element", "players", "id")], []),
    ]
    specs = [s for s in specs if not s[1].empty]

    # 4) Create tables / add new columns (DDL auto-commits, cannot rollback)
    for name, df, pk, types, idx, fks, uq in specs:
        ensure_table(name, df, pk, types, idx, fks, uq)

    # 5) Write data in ONE transaction: error midway -> rollback
    with engine.begin() as conn:
        for name, df, *_ in specs:
            upsert(df, name, conn)
            print(f"  {name}: {len(df)} rows")
    print("Bootstrap + fixtures sync complete.")


if __name__ == "__main__":
    sync_bootstrap()