"""Shared utilities: data cleaning, auto table creation (with primary key + foreign key), upsert."""
import json

import pandas as pd
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.dialects.mysql import insert

from app.db import engine

# Date/time columns (API returns UTC, we store UTC; convert to VN time only when displaying)
DATE_COLS = {"news_added", "deadline_time", "kickoff_time",
             "provisional_start_time", "team_join_date", "birth_date"}

# Nullable integer columns (without forcing, pandas will convert them to floats)
FORCE_INT = {"chance_of_playing_this_round", "chance_of_playing_next_round",
             "squad_number", "corners_and_indirect_freekicks_order",
             "direct_freekicks_order", "penalties_order", "event",
             "team_h_score", "team_a_score", "highest_score",
             "highest_scoring_entry", "top_element", "most_selected",
             "most_transferred_in", "most_transferred_out", "pulse_id",
             "ranked_count"}


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize data types for insertion into MySQL."""
    df = df.copy()
    for c in df.columns:
        s = df[c]
        if c in DATE_COLS:
            df[c] = pd.to_datetime(s, utc=True, errors="coerce").dt.tz_localize(None)
        elif c in FORCE_INT:
            df[c] = pd.to_numeric(s, errors="coerce").astype("Int64")
        elif s.dtype == object or pd.api.types.is_string_dtype(s):
            # list/dict (chip_plays, overrides...) -> JSON string
            if s.map(lambda v: isinstance(v, (list, dict))).any():
                df[c] = s.map(lambda v: json.dumps(v, ensure_ascii=False)
                              if isinstance(v, (list, dict)) else v)
                continue
            non_null = s.dropna()
            if len(non_null) == 0:
                continue
            # bool column containing None
            if non_null.map(lambda v: isinstance(v, bool)).all():
                df[c] = s.astype("boolean")
                continue
            # string-formatted numbers ("5.2") -> float
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().sum() == len(non_null):
                df[c] = num.astype("float64")
    return df


def _sql_type(series: pd.Series) -> str:
    dt = series.dtype
    if pd.api.types.is_bool_dtype(dt):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dt):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dt):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dt):
        return "DATETIME"
    return "TEXT"


def ensure_table(name, df, pk, types=None, indexes=(), fks=(), uniques=()):
    """Create the table if it does not exist (with PRIMARY KEY, UNIQUE, FOREIGN KEY at creation time).
    If the table already exists, only add new columns that appear in the data.

    fks: [(column, parent_table, parent_column), ...]   -> parent table must be created first
    uniques: [column | (column, ...), ...]
    """
    types = dict(types or {})
    for col, _parent, _pcol in fks:          # FK columns are always BIGINT to match parent table
        types.setdefault(col, "BIGINT")
    cols = {c: types.get(c, _sql_type(df[c])) for c in df.columns}

    insp = inspect(engine)
    with engine.begin() as conn:
        if not insp.has_table(name):
            parts = [f"`{c}` {t}" for c, t in cols.items()]
            parts.append("PRIMARY KEY (" + ", ".join(f"`{c}`" for c in pk) + ")")
            for u in uniques:
                unique_cols = (u,) if isinstance(u, str) else tuple(u)
                unique_name = "_".join(unique_cols)
                quoted_cols = ", ".join(f"`{col}`" for col in unique_cols)
                parts.append(f"CONSTRAINT `uq_{name}_{unique_name}` UNIQUE ({quoted_cols})")
            for col, parent, pcol in fks:
                parts.append(
                    f"CONSTRAINT `fk_{name}_{col}` FOREIGN KEY (`{col}`) "
                    f"REFERENCES `{parent}` (`{pcol}`)")
            conn.execute(text(
                f"CREATE TABLE `{name}` ({', '.join(parts)}) CHARACTER SET utf8mb4"))
            for ix in indexes:
                conn.execute(text(
                    f"CREATE INDEX `idx_{name}_{ix}` ON `{name}` (`{ix}`)"))
            print(f"  + created table {name}")
        else:
            existing = {c["name"] for c in insp.get_columns(name)}
            for c, t in cols.items():
                if c not in existing:
                    conn.execute(text(
                        f"ALTER TABLE `{name}` ADD COLUMN `{c}` {t} NULL"))
                    print(f"  + {name}: added new column `{c}`")


def upsert(df: pd.DataFrame, name: str, conn, chunk: int = 200, key_columns=None):
    """Insert new rows; update when a primary or specified unique key conflicts."""
    table = Table(name, MetaData(), autoload_with=conn)
    pk = [c.name for c in table.primary_key.columns]
    key_columns = list(key_columns or pk)
    missing_keys = set(key_columns) - set(df.columns)
    if missing_keys:
        raise ValueError(f"{name}: missing upsert key columns {sorted(missing_keys)}")
    df = df.drop_duplicates(subset=key_columns, keep="last")
    df = df.astype(object).where(df.notna(), None)
    records = df.to_dict("records")
    for i in range(0, len(records), chunk):
        stmt = insert(table).values(records[i:i + chunk])
        update_cols = {c: stmt.inserted[c] for c in df.columns if c not in key_columns}
        if not update_cols:                       # table has only key columns
            update_cols = {key_columns[0]: stmt.inserted[key_columns[0]]}
        conn.execute(stmt.on_duplicate_key_update(**update_cols))
