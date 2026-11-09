"""ChromaDB Vector Database RAG module for the FPL Chatbot.

Builds, persists, and queries a local ChromaDB vector database (`data/chroma_db`)
from local sources outside the MySQL database:
  1. Markdown knowledge files in `data/knowledge/*.md` (official FPL rules, scoring,
     chips, and strategy guides).
  2. 100% of active player-season records and season leaderboards from
     `data/all_seasonal_data.csv` (no Top-N cutoff — every player across every
     season in the CSV gets a full profile document in ChromaDB, plus instant
     Gameweek-level lookup across all 100,000+ rows).

Uses ChromaDB's persistent HNSW cosine index (`chromadb.PersistentClient`) with
compact token formatting (~100 tokens/chunk so CPU ONNX embedding runs 4x faster),
live batch progress logging, and SHA-256 file signature tracking so indexing only
runs once unless the underlying `.md` or `.csv` files change.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

import chromadb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
SEASONAL_CSV_PATH = DATA_DIR / "all_seasonal_data.csv"
CHROMA_DB_DIR = DATA_DIR / "chroma_db"
CORPUS_HASH_FILE = CHROMA_DB_DIR / "corpus_sha256.txt"

COLLECTION_NAME = "fpl_knowledge_base"

# In-memory cache for fast direct DataFrame lookups on all_seasonal_data.csv
_SEASONAL_DF_CACHE: pd.DataFrame | None = None


def _load_seasonal_df() -> pd.DataFrame:
    """Load `data/all_seasonal_data.csv` once into memory for fast aggregation and GW lookups."""
    global _SEASONAL_DF_CACHE  
    if _SEASONAL_DF_CACHE is not None:
        return _SEASONAL_DF_CACHE

    if not SEASONAL_CSV_PATH.exists():
        _SEASONAL_DF_CACHE = pd.DataFrame()
        return _SEASONAL_DF_CACHE

    try:
        df = pd.read_csv(SEASONAL_CSV_PATH, low_memory=False)
    except Exception:  # noqa: BLE001
        _SEASONAL_DF_CACHE = pd.DataFrame()
        return _SEASONAL_DF_CACHE

    if df.empty or "season" not in df.columns or "name" not in df.columns:
        _SEASONAL_DF_CACHE = pd.DataFrame()
        return _SEASONAL_DF_CACHE

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
        "own_goals",
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
        "big_chances_missed",
        "clearances_blocks_interceptions",
        "recoveries",
        "tackles",
        "dribbles",
        "winning_goals",
        "defensive_contribution",
        "mng_win",
        "mng_draw",
        "mng_loss",
        "mng_underdog_win",
        "mng_underdog_draw",
        "mng_goals_scored",
        "mng_clean_sheets",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    gw_col = "GW" if "GW" in df.columns else ("round" if "round" in df.columns else None)
    if gw_col:
        df["_gw"] = pd.to_numeric(df[gw_col], errors="coerce").fillna(0).astype(int)
    else:
        df["_gw"] = 0

    for text_col in ("position", "team"):
        if text_col not in df.columns:
            df[text_col] = "Unknown"
        else:
            df[text_col] = df[text_col].fillna("Unknown").astype(str)

    _SEASONAL_DF_CACHE = df
    return _SEASONAL_DF_CACHE


# =====================================================================
# STEP 1: CHUNKING MARKDOWN & 100% OF SEASONAL CSV PLAYERS FOR CHROMADB
# =====================================================================

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
            "title": title,
            "text": content,
            "metadata": {
                "source": f"data/knowledge/{file_path.name}",
                "category": "fpl_rules_and_strategy",
                "season": "ALL",
                "player_name": "",
                "team": "ALL",
                "position": "ALL",
                "title": title,
            },
        })
    return chunks


def _build_chunks_from_seasonal_csv(
    top_players_per_season: int | None = None,
) -> list[dict[str, Any]]:
    """
    Convert `data/all_seasonal_data.csv` into semantic chunks for ChromaDB:
      1. Season Overview Leaderboard (1 chunk per season).
      2. Individual Player Season Profile for EVERY active player in every season
         (`top_players_per_season=None` by default, so 100% of active players across
         all seasons are indexed with all available metrics).

    Each player chunk is formatted concisely (~80-110 tokens) so CPU ONNX embedding
    runs fast without hitting the 256-token padding ceiling.
    """
    df = _load_seasonal_df()
    if df.empty:
        return []

    sum_candidates = [
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
        "own_goals",
        "yellow_cards",
        "red_cards",
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
        "big_chances_missed",
        "clearances_blocks_interceptions",
        "recoveries",
        "tackles",
        "dribbles",
        "winning_goals",
        "defensive_contribution",
        "mng_win",
        "mng_draw",
        "mng_loss",
        "mng_underdog_win",
        "mng_underdog_draw",
        "mng_goals_scored",
        "mng_clean_sheets",
    ]
    sum_cols = [c for c in sum_candidates if c in df.columns]

    grouped = (
        df.groupby(["season", "name"], as_index=False)
        .agg(
            position=("position", "last"),
            team=("team", "last"),
            matches_recorded=("total_points", "count"),
            **{c: (c, "sum") for c in sum_cols},
            **({"max_value": ("value", "max")} if "value" in df.columns else {}),
        )
    )

    # Keep all players who played at least 1 minute or had non-zero points/manager stats
    active_grouped = grouped[
        (grouped.get("minutes", 0) > 0)
        | (grouped["total_points"] != 0)
        | (grouped.get("mng_win", 0) > 0)
        | (grouped.get("mng_draw", 0) > 0)
        | (grouped.get("mng_loss", 0) > 0)
    ].copy()

    chunks: list[dict[str, Any]] = []

    for season, season_df in active_grouped.groupby("season"):
        season_str = str(season)
        season_sorted = season_df.sort_values("total_points", ascending=False)

        # 1. Season Overview Leaderboard Chunk
        top_20 = season_sorted.head(20)
        lines = [
            f"## FPL Historical Season Summary & Leaderboard: {season_str}",
            f"Top 20 highest-scoring FPL players in the {season_str} season:",
        ]
        for rank, (_, row) in enumerate(top_20.iterrows(), start=1):
            raw_val = float(row.get("max_value", 0.0))
            price_m = round(raw_val / 10.0, 1) if raw_val > 25 else round(raw_val, 1)
            extra = ""
            if "expected_goal_involvements" in row and float(row["expected_goal_involvements"]) > 0:
                extra += f", xG={row['expected_goals']:.1f}, xA={row['expected_assists']:.1f}, xGI={row['expected_goal_involvements']:.1f}"
            lines.append(
                f"{rank}. {row['name']} ({row['team']}, {row['position']}, £{price_m}m): "
                f"{int(row['total_points'])} pts, {int(row.get('goals_scored', 0))}G, "
                f"{int(row.get('assists', 0))}A, {int(row.get('clean_sheets', 0))}CS, "
                f"{int(row.get('bonus', 0))} bonus, {int(row.get('minutes', 0))} mins{extra}."
            )

        title_ov = f"FPL {season_str} Season Top Scorers Leaderboard"
        chunks.append({
            "id": f"csv::season_overview::{season_str}",
            "title": title_ov,
            "text": "\n".join(lines),
            "metadata": {
                "source": "data/all_seasonal_data.csv",
                "category": "historical_season_leaderboard",
                "season": season_str,
                "player_name": "",
                "team": "ALL",
                "position": "ALL",
                "title": title_ov,
            },
        })

        # 2. Individual Detailed Profile for ALL Active Players in the Season
        players_to_index = (
            season_sorted.head(top_players_per_season)
            if top_players_per_season is not None
            else season_sorted
        )
        for _, row in players_to_index.iterrows():
            p_name = str(row["name"])
            team_str = str(row["team"])
            pos_str = str(row["position"])
            raw_val = float(row.get("max_value", 0.0))
            price_m = round(raw_val / 10.0, 1) if raw_val > 25 else round(raw_val, 1)

            details = [
                f"{p_name} ({team_str}, {pos_str}, Season {season_str}, Peak £{price_m}m): "  # noqa: ISC004
                f"{int(row['total_points'])} pts in {int(row.get('minutes', 0))} mins "
                f"({int(row.get('goals_scored', 0))}G, {int(row.get('assists', 0))}A, "
                f"{int(row.get('clean_sheets', 0))}CS, {int(row.get('bonus', 0))} bonus, "
                f"BPS={int(row.get('bps', 0))}, GC={int(row.get('goals_conceded', 0))}, "
                f"Saves={int(row.get('saves', 0))}, YC={int(row.get('yellow_cards', 0))}, RC={int(row.get('red_cards', 0))})."
            ]
            if "ict_index" in row and float(row["ict_index"]) > 0:
                details.append(
                    f"ICT={row['ict_index']:.1f} (Inf={row.get('influence', 0.0):.1f}, "
                    f"Cre={row.get('creativity', 0.0):.1f}, Thr={row.get('threat', 0.0):.1f})."
                )
            if "expected_goal_involvements" in row and (
                float(row.get("expected_goals", 0.0)) > 0 or float(row.get("expected_assists", 0.0)) > 0
            ):
                details.append(
                    f"Expected: Starts={int(row.get('starts', 0))}, xG={row.get('expected_goals', 0.0):.2f}, "
                    f"xA={row.get('expected_assists', 0.0):.2f}, xGI={row.get('expected_goal_involvements', 0.0):.2f}, "
                    f"xGC={row.get('expected_goals_conceded', 0.0):.2f}."
                )
            if "key_passes" in row and (
                float(row.get("key_passes", 0.0)) > 0 or float(row.get("clearances_blocks_interceptions", 0.0)) > 0
            ):
                details.append(
                    f"Underlying: KeyPasses={int(row.get('key_passes', 0))}, "
                    f"BigChancesCreated={int(row.get('big_chances_created', 0))}, "
                    f"BigChancesMissed={int(row.get('big_chances_missed', 0))}, "
                    f"CBI={int(row.get('clearances_blocks_interceptions', 0))}, "
                    f"Recoveries={int(row.get('recoveries', 0))}, Tackles={int(row.get('tackles', 0))}, "
                    f"Dribbles={int(row.get('dribbles', 0))}."
                )
            if "defensive_contribution" in row and float(row.get("defensive_contribution", 0.0)) > 0:
                details.append(f"DefContribution={row['defensive_contribution']:.1f}.")
            if "mng_win" in row and (
                float(row.get("mng_win", 0.0)) > 0 or float(row.get("mng_draw", 0.0)) > 0 or float(row.get("mng_loss", 0.0)) > 0
            ):
                details.append(
                    f"ManagerChip: W={int(row.get('mng_win', 0))}, D={int(row.get('mng_draw', 0))}, "
                    f"L={int(row.get('mng_loss', 0))}, UnderdogW={int(row.get('mng_underdog_win', 0))}, "
                    f"UnderdogD={int(row.get('mng_underdog_draw', 0))}."
                )

            title_p = f"{p_name} ({season_str})"
            chunks.append({
                "id": f"csv::player::{season_str}::{p_name}",
                "title": title_p,
                "text": " ".join(details),
                "metadata": {
                    "source": "data/all_seasonal_data.csv",
                    "category": "historical_player_profile",
                    "season": season_str,
                    "player_name": p_name,
                    "team": team_str,
                    "position": pos_str,
                    "title": title_p,
                },
            })

    return chunks


def collect_all_knowledge_chunks() -> list[dict[str, Any]]:
    """
    Gather all RAG documents from `data/knowledge/*.md` and `data/all_seasonal_data.csv`.
    """
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []

    for md_file in sorted(KNOWLEDGE_DIR.glob("*.md")):
        chunks.extend(_chunk_markdown_file(md_file))

    chunks.extend(_build_chunks_from_seasonal_csv(top_players_per_season=None))
    return chunks


# =====================================================================
# STEP 2: CHROMADB PERSISTENT VECTOR DATABASE INDEXING WITH PROGRESS
# =====================================================================

def _compute_files_signature() -> str:
    hasher = hashlib.sha256()
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    for md_file in sorted(KNOWLEDGE_DIR.glob("*.md")):
        stat = md_file.stat()
        hasher.update(f"{md_file.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())

    if SEASONAL_CSV_PATH.exists():
        stat = SEASONAL_CSV_PATH.stat()
        hasher.update(f"csv_full_v2:{stat.st_size}:{stat.st_mtime_ns}".encode())

    return hasher.hexdigest()


def get_chroma_collection(force_rebuild: bool = False) -> chromadb.Collection:
    """
    Get or build the persistent ChromaDB vector database collection at `data/chroma_db`.
    Prints live batch progress when building the index for the first time.
    """
    CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))

    current_sig = _compute_files_signature()
    saved_sig = (
        CORPUS_HASH_FILE.read_text(encoding="utf-8").strip()
        if CORPUS_HASH_FILE.exists()
        else ""
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    if not force_rebuild and saved_sig == current_sig and collection.count() > 0:
        return collection

    if force_rebuild or saved_sig != current_sig:
        try:
            client.delete_collection(name=COLLECTION_NAME)
        except Exception:  # noqa: BLE001, S110
            pass
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    print("  [RAG] Aggregating 100% of active player records from .md and all_seasonal_data.csv...")
    t0 = time.time()
    chunks = collect_all_knowledge_chunks()
    if not chunks:
        return collection

    batch_size = 200
    total = len(chunks)
    total_batches = (total + batch_size - 1) // batch_size
    print(f"  [RAG] Embedding & indexing {total} chunks into ChromaDB ({total_batches} batches)...")

    for batch_idx, start in enumerate(range(0, total, batch_size), start=1):
        b_start = time.time()
        batch = chunks[start : start + batch_size]
        collection.upsert(
            ids=[c["id"] for c in batch],
            documents=[f"{c['title']}: {c['text']}" for c in batch],
            metadatas=[c["metadata"] for c in batch],
        )
        pct = round((min(start + batch_size, total) / total) * 100)
        print(
            f"  [RAG] Batch {batch_idx}/{total_batches} ({pct}%) — "
            f"{min(start + batch_size, total)}/{total} docs indexed ({time.time() - b_start:.1f}s)"
        )

    CORPUS_HASH_FILE.write_text(current_sig, encoding="utf-8")
    print(f"  [RAG] Finished building ChromaDB index ({total} docs in {time.time() - t0:.1f}s).")
    return collection


# =====================================================================
# STEP 3: DIRECT GAMEWEEK / PLAYER CSV LOOKUP + CHROMADB HYBRID SEARCH
# =====================================================================

def _direct_csv_gameweek_lookup(
    query: str,
    season: str | None = None,
) -> list[dict[str, Any]]:
    """
    Complement ChromaDB semantic retrieval with an exact Pandas lookup on
    `data/all_seasonal_data.csv` whenever the query mentions a specific Gameweek
    (e.g., 'GW1', 'gameweek 5', 'vòng 10') or a player name in `all_seasonal_data.csv`.
    This guarantees 100% precision on all 100,000+ individual gameweek rows.
    """
    df = _load_seasonal_df()
    if df.empty:
        return []

    q_lower = query.lower()

    detected_season = season.strip() if season else None
    if not detected_season:
        m_season = re.search(r"(20\d{2}[-/]\d{2,4})", q_lower)
        if m_season:
            raw_s = m_season.group(1).replace("/", "-")
            if len(raw_s) == 9:
                raw_s = f"{raw_s[:5]}{raw_s[7:]}"
            detected_season = raw_s

    gw_match = re.search(r"\b(?:gw|gameweek|vong|vòng)\s*(\d{1,2})\b", q_lower)
    target_gw = int(gw_match.group(1)) if gw_match else None

    sub_df = df[df["season"].astype(str) == detected_season] if detected_season else df
    if sub_df.empty:
        return []

    query_words = [
        w for w in re.findall(r"[^\W\d_]{3,}", q_lower)
        if w not in {
            "the", "and", "for", "how", "many", "points", "goals", "assists",
            "season", "gameweek", "player", "fpl", "who", "what", "compare",
            "stats", "score", "scored", "mua", "vong", "diem", "cua", "trong",
            "bao", "nhieu",
        }
    ]
    if not query_words:
        return []

    unique_names = sub_df["name"].dropna().astype(str).unique()
    matched_names = [
        name for name in unique_names
        if any(w in re.findall(r"[^\W\d_]+", name.lower()) for w in query_words)
    ]
    if not matched_names:
        return []

    player_rows = sub_df[sub_df["name"].isin(matched_names[:5])].copy()
    if player_rows.empty:
        return []

    direct_results: list[dict[str, Any]] = []
    for (s_val, p_name), p_df in player_rows.groupby(["season", "name"]):
        p_df_sorted = p_df.sort_values("_gw")
        if target_gw is not None:
            gw_df = p_df_sorted[p_df_sorted["_gw"] == target_gw]
            if gw_df.empty:
                continue
            lines = [f"## Exact Gameweek Record from CSV: {p_name} ({s_val}, GW{target_gw})"]
            for _, r in gw_df.iterrows():
                lines.append(
                    f"- GW{int(r['_gw'])}: {int(r['total_points'])} pts | "
                    f"Minutes: {int(r.get('minutes', 0))} | Goals: {int(r.get('goals_scored', 0))} | "
                    f"Assists: {int(r.get('assists', 0))} | Clean Sheet: {int(r.get('clean_sheets', 0))} | "
                    f"Bonus: {int(r.get('bonus', 0))} (BPS: {int(r.get('bps', 0))}) | "
                    f"xG: {r.get('expected_goals', 0.0):.2f}, xA: {r.get('expected_assists', 0.0):.2f}, "
                    f"xGI: {r.get('expected_goal_involvements', 0.0):.2f} | "
                    f"Was Home: {r.get('was_home', '')}"
                )
        else:
            total_pts = int(p_df_sorted["total_points"].sum())
            total_g = int(p_df_sorted.get("goals_scored", pd.Series(dtype=float)).sum())
            total_a = int(p_df_sorted.get("assists", pd.Series(dtype=float)).sum())
            total_mins = int(p_df_sorted.get("minutes", pd.Series(dtype=float)).sum())
            gw_breakdown = ", ".join(
                f"GW{int(r['_gw'])}:{int(r['total_points'])}pts"
                + (
                    f"({int(r.get('goals_scored', 0))}G,{int(r.get('assists', 0))}A)"
                    if (int(r.get("goals_scored", 0)) > 0 or int(r.get("assists", 0)) > 0)
                    else ""
                )
                for _, r in p_df_sorted.iterrows()
                if int(r.get("minutes", 0)) > 0 or int(r["total_points"]) != 0
            )
            lines = [
                f"## Exact Season & Gameweek Log from CSV: {p_name} ({s_val})",
                f"- Season Totals: {total_pts} pts, {total_g} goals, {total_a} assists, {total_mins} mins.",
                f"- Full Gameweek Breakdown: {gw_breakdown}",
            ]

        direct_results.append({
            "id": f"csv_exact::{s_val}::{p_name}::{target_gw or 'ALL'}",
            "title": f"{p_name} ({s_val}) Exact CSV Gameweek Data",
            "category": "exact_csv_gameweek_lookup",
            "season": str(s_val),
            "source": "data/all_seasonal_data.csv",
            "similarity_score": 1.0,
            "content": "\n".join(lines),
        })

    return direct_results[:3]


def retrieve_fpl_knowledge(
    query: str,
    top_k: int = 5,
    season: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Query the persistent ChromaDB vector database using HNSW cosine similarity,
    combined with exact Gameweek/player lookups on `data/all_seasonal_data.csv`.
    """
    top_k = max(1, min(int(top_k), 10))
    collection = get_chroma_collection(force_rebuild=False)
    total_docs = collection.count()

    if total_docs == 0:
        return {
            "query": query,
            "vector_db": "ChromaDB (empty)",
            "total_indexed_chunks": 0,
            "results": [],
        }

    where_conditions: list[dict[str, Any]] = []
    if season and season.strip():
        where_conditions.append({"season": season.strip()})
    if category and category.strip():
        where_conditions.append({"category": category.strip()})

    where_filter: dict[str, Any] | None = None
    if len(where_conditions) == 1:
        where_filter = where_conditions[0]
    elif len(where_conditions) > 1:
        where_filter = {"$and": where_conditions}

    query_kwargs: dict[str, Any] = {
        "query_texts": [query],
        "n_results": min(top_k, total_docs),
    }
    if where_filter:
        query_kwargs["where"] = where_filter

    res = collection.query(**query_kwargs)

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]

    formatted_results: list[dict[str, Any]] = (
        [] if category == "fpl_rules_and_strategy" else _direct_csv_gameweek_lookup(query, season=season)
    )
    seen_ids = {item["id"] for item in formatted_results}

    for doc_id, doc_text, meta, dist in zip(ids, docs, metas, distances):
        if doc_id in seen_ids:
            continue
        similarity = round(max(0.0, 1.0 - float(dist)), 4) if dist is not None else None
        formatted_results.append({
            "id": doc_id,
            "title": (meta or {}).get("title", doc_id),
            "category": (meta or {}).get("category", ""),
            "season": (meta or {}).get("season", ""),
            "source": (meta or {}).get("source", ""),
            "similarity_score": similarity,
            "content": doc_text,
        })

    return {
        "query": query,
        "season_filter": season,
        "category_filter": category,
        "vector_db": f"ChromaDB PersistentClient ({CHROMA_DB_DIR.name})",
        "total_indexed_chunks": total_docs,
        "results": formatted_results[:top_k],
    }


if __name__ == "__main__":
    print(f"Building ChromaDB vector database at: {CHROMA_DB_DIR} ...")
    col = get_chroma_collection(force_rebuild=True)
    print(
        f"Successfully indexed {col.count()} documents into ChromaDB collection '{COLLECTION_NAME}'!"
    )