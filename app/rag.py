"""Local Retrieval-Augmented Generation (RAG) module for the FPL Chatbot.

Builds and queries a semantic knowledge base from three local sources:
  1. Markdown knowledge files in `data/knowledge/*.md` (official FPL rules, scoring,
     chips, and strategy guides).
  2. Multi-season historical player summaries aggregated automatically from
     `data/all_seasonal_data.csv` (handling schema differences across older and newer
     FPL seasons such as `xG`, `xA`, `mng_*`, and `defensive_contribution`).
  3. Live injury, suspension, and availability bulletins aggregated from the
     `players` table in MySQL (`news`, `status`, `chance_of_playing_next_round`).

Uses Google's `gemini-embedding-001` model to compute embeddings and caches them in
`data/knowledge_index.json` with a content hash so embeddings are only recomputed
when the underlying files change. Falls back to TF/keyword scoring if offline or if
`GEMINI_API_KEY` is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text

from app.db import engine

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
SEASONAL_CSV_PATH = DATA_DIR / "all_seasonal_data.csv"
INDEX_CACHE_PATH = DATA_DIR / "knowledge_index.json"

EMBEDDING_MODEL = "gemini-embedding-001"

def _chunk_markdown_file(file_path: Path) -> list[dict[str, Any]]:
    """
    Split a Markdown file into semantic chunks by `## ` section headings.
    """
    if not file_path.exists():
        return []

    raw_text = file_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    sections = re.split(r"(?m)^##\s+", raw_text)
    chunks: list[dict[str, Any]] = []

    for idx, section in enumerate(sections):
        cleaned = section.strip()
        if not cleaned:
            continue
        lines = cleaned.splitlines()
        title = lines[0].lstrip("#").strip() if lines else f"{file_path.stem}#{idx}"
        content = cleaned if cleaned.startswith("#") else f"## {cleaned}"
        chunks.append({
            "id": f"md::{file_path.name}::{idx}",
            "source": f"data/knowledge/{file_path.name}",
            "category": "fpl_rules_and_strategy",
            "title": title,
            "text": content,
        })
    return chunks

def _build_chunks_from_seasonal_csv(
    csv_path: Path = SEASONAL_CSV_PATH,
    top_players_per_season: int = 35,
) -> list[dict[str, Any]]:
    """
    Read `data/all_seasonal_data.csv` and aggregate gameweek-level rows into
    season-level historical summaries:
      1. One 'Season Overview & Top Performers' chunk for every season in the CSV.
      2. Individual season profile chunks for the top players in each season.

    Handles missing/NaN columns across older and newer seasons gracefully (for example,
    `expected_goals`, `expected_assists`, `starts`, `defensive_contribution`, and
    `mng_*` columns that only exist in newer seasons).
    """
    if not csv_path.exists():
        return []

    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception:  # noqa: BLE001
        return []

    if df.empty or "season" not in df.columns or "name" not in df.columns:
        return []

    numeric_cols = [
        "total_points",
        "minutes",
        "goals_scored",
        "assists",
        "clean_sheets",
        "bonus",
        "bps",
        "goals_conceded",
        "saves",
        "penalties_saved",
        "penalties_missed",
        "yellow_cards",
        "red_cards",
        "value",
        "ict_index",
        "influence",
        "creativity",
        "threat",
        "xP",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "expected_goals_conceded",
        "starts",
        "key_passes",
        "big_chances_created",
        "clearances_blocks_interceptions",
        "recoveries",
        "tackles",
        "defensive_contribution",
        "mng_win",
        "mng_draw",
        "mng_loss",
        "mng_underdog_win",
        "mng_underdog_draw",
        "mng_goals_scored",
        "mng_clean_sheets",
    ]
    present_numeric = [c for c in numeric_cols if c in df.columns]
    for col in present_numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    for text_col in ("position", "team"):
        if text_col not in df.columns:
            df[text_col] = "Unknown"
        else:
            df[text_col] = df[text_col].fillna("Unknown").astype(str)

    agg_spec: dict[str, Any] = {
        "position": "last",
        "team": "last",
        "GW": "count" if "GW" in df.columns else ("round", "count"),
    }
    sum_cols = [c for c in present_numeric if c != "value"]
    for c in sum_cols:
        agg_spec[c] = "sum"
    if "value" in present_numeric:
        agg_spec["value"] = "max"

    grouped = (
        df.groupby(["season", "name"], as_index=False)
        .agg(
            position=("position", "last"),
            team=("team", "last"),
            matches_recorded=("total_points", "count"),
            **{c: (c, "sum") for c in sum_cols},
            **({"max_value": ("value", "max")} if "value" in present_numeric else {}),
        )
    )

    chunks: list[dict[str, Any]] = []

    # 1. Build a Season Overview Leaderboard chunk per season
    for season, season_df in grouped.groupby("season"):
        season_sorted = season_df.sort_values("total_points", ascending=False)
        top_15 = season_sorted.head(15)
        lines = [
            f"## FPL Historical Season Summary: {season}",
            f"Top 15 highest-scoring FPL players in the {season} season (from `all_seasonal_data.csv`):",
        ]
        for rank, (_, row) in enumerate(top_15.iterrows(), start=1):
            price_m = round(float(row.get("max_value", 0.0)) / 10.0, 1) if row.get("max_value", 0.0) > 25 else round(float(row.get("max_value", 0.0)), 1)
            extra = ""
            if "expected_goal_involvements" in row and float(row["expected_goal_involvements"]) > 0:
                extra += f", xG={row['expected_goals']:.2f}, xA={row['expected_assists']:.2f}, xGI={row['expected_goal_involvements']:.2f}"
            if "defensive_contribution" in row and float(row["defensive_contribution"]) > 0:
                extra += f", DefContrib={row['defensive_contribution']:.0f}"

            lines.append(
                f"{rank}. {row['name']} ({row['team']}, {row['position']}, Peak Price £{price_m}m): "
                f"{int(row['total_points'])} pts, {int(row.get('goals_scored', 0))} goals, "
                f"{int(row.get('assists', 0))} assists, {int(row.get('clean_sheets', 0))} CS, "
                f"{int(row.get('bonus', 0))} bonus, {int(row.get('minutes', 0))} mins{extra}."
            )

        chunks.append({
            "id": f"csv::season_overview::{season}",
            "source": "data/all_seasonal_data.csv",
            "category": "historical_season_leaderboard",
            "title": f"FPL {season} Season Top Scorers Leaderboard",
            "text": "\n".join(lines),
        })

        # 2. Build detailed player season cards for top players in each season
        top_n = season_sorted.head(top_players_per_season)
        for _, row in top_n.iterrows():
            p_name = str(row["name"])
            price_m = round(float(row.get("max_value", 0.0)) / 10.0, 1) if row.get("max_value", 0.0) > 25 else round(float(row.get("max_value", 0.0)), 1)
            details = [
                f"## Historical Player Profile: {p_name} ({season} Season)",
                f"- Player: {p_name} | Team: {row['team']} | Position: {row['position']} | Season: {season}",
                f"- Peak Price: £{price_m}m | Total Points: {int(row['total_points'])} | Minutes: {int(row.get('minutes', 0))}",
                f"- Attacking & Returns: {int(row.get('goals_scored', 0))} goals, {int(row.get('assists', 0))} assists, {int(row.get('clean_sheets', 0))} clean sheets, {int(row.get('bonus', 0))} bonus points (BPS: {int(row.get('bps', 0))}).",
            ]
            if "ict_index" in row and float(row["ict_index"]) > 0:
                details.append(
                    f"- ICT Index: {row['ict_index']:.1f} (Influence: {row.get('influence', 0.0):.1f}, "
                    f"Creativity: {row.get('creativity', 0.0):.1f}, Threat: {row.get('threat', 0.0):.1f})."
                )
            if "expected_goal_involvements" in row and (
                float(row.get("expected_goals", 0.0)) > 0 or float(row.get("expected_assists", 0.0)) > 0
            ):
                details.append(
                    f"- Expected Stats (Modern Season Schema): Starts={int(row.get('starts', 0))}, "
                    f"xG={row.get('expected_goals', 0.0):.2f}, xA={row.get('expected_assists', 0.0):.2f}, "
                    f"xGI={row.get('expected_goal_involvements', 0.0):.2f}, xGC={row.get('expected_goals_conceded', 0.0):.2f}."
                )
            if "key_passes" in row and (
                float(row.get("key_passes", 0.0)) > 0 or float(row.get("clearances_blocks_interceptions", 0.0)) > 0
            ):
                details.append(
                    f"- Underlying Playmaking & Defense: Key Passes={int(row.get('key_passes', 0))}, "
                    f"Big Chances Created={int(row.get('big_chances_created', 0))}, "
                    f"CBI (Clearances/Blocks/Interceptions)={int(row.get('clearances_blocks_interceptions', 0))}, "
                    f"Recoveries={int(row.get('recoveries', 0))}, Tackles={int(row.get('tackles', 0))}."
                )
            if "defensive_contribution" in row and float(row.get("defensive_contribution", 0.0)) > 0:
                details.append(
                    f"- Defensive Contribution Metric: {row['defensive_contribution']:.1f}."
                )
            if "mng_win" in row and (
                float(row.get("mng_win", 0.0)) > 0 or float(row.get("mng_draw", 0.0)) > 0 or float(row.get("mng_loss", 0.0)) > 0
            ):
                details.append(
                    f"- Assistant Manager Stats: Wins={int(row.get('mng_win', 0))}, Draws={int(row.get('mng_draw', 0))}, "
                    f"Losses={int(row.get('mng_loss', 0))}, Underdog Wins={int(row.get('mng_underdog_win', 0))}, "
                    f"Underdog Draws={int(row.get('mng_underdog_draw', 0))}."
                )

            chunks.append({
                "id": f"csv::player::{season}::{p_name}",
                "source": "data/all_seasonal_data.csv",
                "category": "historical_player_profile",
                "title": f"{p_name} ({season}) Historical Stats",
                "text": "\n".join(details),
            })

    return chunks


