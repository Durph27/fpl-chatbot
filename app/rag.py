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
