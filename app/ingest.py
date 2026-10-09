import time

import pandas as pd
import requests
from db import engine
from sqlalchemy import text

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (FPL-Chatbot)"}
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch(path: str):
    resp = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(1) 
    return resp.json()


def run_schema():
    
    with open("app/schema.sql", encoding="utf-8") as f:
        statements = [s.strip() for s in f.read().split(";") if s.strip()]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    print("Table has been created")


def num(df, cols):
    
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def ingest_bootstrap():
    data = fetch("bootstrap-static/")

    # ---- teams ----
    teams = pd.DataFrame(data["teams"])[["id", "name", "short_name", "strength"]]
    teams.to_sql("teams", engine, if_exists="append", index=False)

    # ---- players ----
    p = pd.DataFrame(data["elements"])
    p["position"] = p["element_type"].map(POS)
    p["price"] = p["now_cost"] / 10          #API stores price * 10
    p["team"] = p["team"].astype(int)
    p = num(p, ["form", "points_per_game", "selected_by_percent",
                "expected_goals", "expected_assists",
                "expected_goal_involvements", "ict_index"])
    cols = ["id", "web_name", "first_name", "second_name", "team", "position",
            "price", "total_points", "form", "points_per_game",
            "selected_by_percent", "minutes", "goals_scored", "assists",
            "expected_goals", "expected_assists", "expected_goal_involvements",
            "ict_index", "status", "chance_of_playing_next_round", "news"]
    p = p[cols].astype({"chance_of_playing_next_round": "Int64"})
    p.to_sql("players", engine, if_exists="append", index=False)

    # ---- events (gameweek) ----
    e = pd.DataFrame(data["events"])
    e["deadline_time"] = pd.to_datetime(e["deadline_time"]).dt.tz_localize(None)
    e = e[["id", "name", "deadline_time", "finished", "is_current",
           "is_next", "average_entry_score", "highest_score"]]
    e = e.astype({"highest_score": "Int64"})
    e.to_sql("events", engine, if_exists="append", index=False)



def ingest_fixtures():
    fx = pd.DataFrame(fetch("fixtures/"))
    fx["kickoff_time"] = pd.to_datetime(fx["kickoff_time"]).dt.tz_localize(None)
    fx = fx[["id", "event", "team_h", "team_a", "kickoff_time",
             "team_h_difficulty", "team_a_difficulty",
             "team_h_score", "team_a_score", "finished"]]
    fx = fx.astype({"event": "Int64", "team_h_score": "Int64",
                    "team_a_score": "Int64"})
    fx.to_sql("fixtures", engine, if_exists="append", index=False)


if __name__ == "__main__":
    run_schema()
    ingest_bootstrap()
    ingest_fixtures()