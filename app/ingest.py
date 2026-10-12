import time
from pathlib import Path

import pandas as pd
import requests
from db import engine
from sqlalchemy import MetaData, Table, text
from sqlalchemy.dialects.mysql import insert as mysql_insert

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (FPL-Chatbot)"}
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch(path: str):  # return json
    resp = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=30)
    resp.raise_for_status()  # If HTTP error -> exception
    time.sleep(1)
    return resp.json()


def run_schema():
    schema_path = Path(__file__).with_name("schema.sql")
    with schema_path.open(encoding="utf-8") as f:
        statements = [s.strip() for s in f.read().split(";") if s.strip()]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    print("Table has been created")


def num(df, cols):  # Change data to number

    for c in cols:
        df[c] = pd.to_numeric(
            df[c], errors="coerce"
        )  # coerce: if cannot change to num -> NaN
    return df


def upsert_dataframe(df: pd.DataFrame, table_name: str):
    """Insert new rows and update existing rows by ID."""
    if df.empty:
        return

    table = Table(table_name, MetaData(), autoload_with=engine)

    # Only select columns appear in db
    columns = [column.name for column in table.columns if column.name in df.columns]

    if "id" not in columns:
        raise ValueError(f"Table {table_name} or DataFrame is missing 'id'")

    clean_df = df[columns].astype(object)
    clean_df = clean_df.where(pd.notna(clean_df), None)
    rows = clean_df.to_dict(orient="records")

    update_columns = {column: column for column in columns if column != "id"}

    with engine.begin() as conn:
        for start in range(0, len(rows), 500):
            batch = rows[start : start + 500]

            insert_stmt = mysql_insert(table).values(batch)

            updates = {
                column: insert_stmt.inserted[column] for column in update_columns
            }

            if updates:
                stmt = insert_stmt.on_duplicate_key_update(**updates)
            else:
                stmt = insert_stmt

            conn.execute(stmt)


def ingest_bootstrap():
    data = fetch("bootstrap-static/")

    # ---- teams ----
    teams = pd.DataFrame(data["teams"])[["id", "name", "short_name", "strength"]]
    upsert_dataframe(teams, "teams")

    # ---- players ----
    p = pd.DataFrame(data["elements"])
    p["position"] = p["element_type"].map(POS)
    p["price"] = p["now_cost"] / 10  # API stores price * 10
    p["team"] = p["team"].astype(int)  # team -> int
    p = num(
        p,
        [
            "form",
            "points_per_game",
            "selected_by_percent",
            "expected_goals",
            "expected_assists",
            "expected_goal_involvements",
            "ict_index",
        ],
    )
    cols = [
        "id",
        "web_name",
        "first_name",
        "second_name",
        "team",
        "position",
        "price",
        "total_points",
        "form",
        "points_per_game",
        "selected_by_percent",
        "minutes",
        "goals_scored",
        "assists",
        "clean_sheets",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "ict_index",
        "status",
        "chance_of_playing_next_round",
        "news",
    ]
    p = p[cols].astype({"chance_of_playing_next_round": "Int64"})
    upsert_dataframe(p, "players")

    # ---- events (gameweek) ----
    e = pd.DataFrame(data["events"])
    e["deadline_time"] = (
        pd.to_datetime(e["deadline_time"], utc=True)
        .dt.tz_convert("Asia/Ho_Chi_Minh")
        .dt.tz_localize(None)
    )
    e = e[
        [
            "id",
            "name",
            "deadline_time",
            "finished",
            "is_current",
            "is_next",
            "average_entry_score",
            "highest_score",
        ]
    ]
    e = e.astype({"highest_score": "Int64"})
    upsert_dataframe(e, "events")


def ingest_fixtures():
    fx = pd.DataFrame(fetch("fixtures/"))
    fx["kickoff_time"] = (
        pd.to_datetime(fx["kickoff_time"], utc=True)
        .dt.tz_convert("Asia/Ho_Chi_Minh")
        .dt.tz_localize(None)
    )
    fx = fx[
        [
            "id",
            "event",
            "team_h",
            "team_a",
            "kickoff_time",
            "team_h_difficulty",
            "team_a_difficulty",
            "team_h_score",
            "team_a_score",
            "finished",
        ]
    ]
    fx = fx.astype({"event": "Int64", "team_h_score": "Int64", "team_a_score": "Int64"})
    upsert_dataframe(fx, "fixtures")


if __name__ == "__main__":
    run_schema()
    ingest_bootstrap()
    ingest_fixtures()
