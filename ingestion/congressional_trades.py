"""
Stage 1 — Congressional trades ingestion.

Source: insiderwatch-data GitHub repo (saminjafari/insiderwatch-data)
  https://raw.githubusercontent.com/saminjafari/insiderwatch-data/master/data/congress-trades.csv

This is a community-maintained CSV of STOCK Act disclosures with ticker-level
detail (member, ticker, action, amount range, transaction date, filed date).
It's derived from the official House Clerk disclosures and is updated regularly.

House Stock Watcher (the original planned source) went offline — this is the
best free replacement that provides the same ticker-level data rather than
just filing-level index rows from the official House Clerk ZIP.

Idempotent: (source, raw_id) has a UNIQUE constraint so re-runs skip duplicates.
"""

import hashlib
import logging
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

CSV_URL = "https://raw.githubusercontent.com/saminjafari/insiderwatch-data/master/data/congress-trades.csv"


def fetch_all_trades(retries: int = 3, backoff: float = 2.0) -> pd.DataFrame:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(CSV_URL, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(pd.io.common.StringIO(resp.text))
            logger.info("Fetched %d rows from insiderwatch-data", len(df))
            return df
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff ** attempt
            logger.warning("Fetch attempt %d failed (%s), retrying in %.1fs", attempt, exc, wait)
            time.sleep(wait)
    return pd.DataFrame()


def _parse_date(raw) -> date | None:
    if pd.isna(raw) or not str(raw).strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).date()
        except ValueError:
            continue
    logger.debug("Unparseable date: %r", raw)
    return None


def _stable_id(row: pd.Series) -> str:
    """Deterministic dedup key from the fields that uniquely identify a trade row."""
    key = "|".join([
        str(row.get("member", "")),
        str(row.get("ticker", "")),
        str(row.get("transaction_date", "")),
        str(row.get("action", "")),
        str(row.get("filing_id", "")),
    ])
    return hashlib.sha1(key.encode()).hexdigest()


def clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    # Work on a copy to avoid pandas chained-assignment warnings
    df = df.copy()

    action_map = {
        "buy": "buy", "purchase": "buy",
        "sell": "sell", "sale": "sell", "sale_full": "sell", "sale_partial": "sell",
        "exchange": "exchange",
    }
    df.loc[:, "transaction_type"] = df["action"].str.lower().str.strip().map(action_map).fillna(
        df["action"].str.lower().str.strip()
    )

    df.loc[:, "ticker"] = df["ticker"].where(
        df["ticker"].notna() & ~df["ticker"].str.strip().str.upper().isin(["--", "N/A", "NA", ""]),
        other=None,
    )

    df.loc[:, "transaction_date"] = df["transaction_date"].apply(_parse_date)
    df.loc[:, "filed_date"]       = df["filed_date"].apply(_parse_date)
    df.loc[:, "chamber"]          = df["chamber"].str.strip().str.title()

    df = df.rename(columns={"amount_min_usd": "amount_min", "amount_range": "amount_range_raw"})
    df.loc[:, "amount_max"] = None

    # filing_id is per-filing, not per-trade row — multiple trades share the
    # same filing_id. Append a row index to make it unique per trade.
    df = df.reset_index(drop=True)
    df.loc[:, "raw_id"] = df.apply(
        lambda r: f"{r['filing_id']}_{r.name}" if pd.notna(r.get("filing_id")) and str(r.get("filing_id", "")).strip()
        else _stable_id(r),
        axis=1,
    )

    df.loc[:, "source"]            = "insiderwatch"
    df.loc[:, "member_name"]       = df["member"].str.strip()
    df.loc[:, "asset_description"] = df.get("asset", pd.Series(dtype=str)).str.strip()
    df.loc[:, "disclosure_date"]   = df["filed_date"]

    df = df[df["member_name"].notna() & (df["member_name"] != "")]
    logger.info("Cleaned %d rows", len(df))
    return df


def load(df: pd.DataFrame) -> int:
    if df.empty:
        logger.info("No rows to load")
        return 0

    cols = [
        "member_name", "chamber", "ticker", "asset_description",
        "transaction_type", "amount_range", "amount_min", "amount_max",
        "transaction_date", "disclosure_date", "filed_date", "source", "raw_id",
    ]

    # Map cleaned df columns to schema columns
    col_map = {"amount_range": "amount_range_raw"}
    rows = []
    for _, row in df.iterrows():
        rows.append(tuple(
            row.get(col_map.get(c, c)) for c in cols
        ))

    sql = f"""
        INSERT INTO congressional_trades ({', '.join(cols)})
        VALUES %s
        ON CONFLICT (source, raw_id) DO NOTHING
    """
    inserted = execute_values(sql, rows)
    logger.info("Loaded %d new rows into congressional_trades", inserted)
    return inserted


def run() -> dict[str, Any]:
    logger.info("=== Congressional trades ingestion: start ===")
    raw_df = fetch_all_trades()
    df = clean(raw_df)
    inserted = load(df)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM congressional_trades")
            total = cur.fetchone()[0]
    finally:
        put_conn(conn)

    summary = {
        "stage":    "congressional_trades",
        "fetched":  len(raw_df),
        "inserted": inserted,
        "db_total": total,
    }
    logger.info("=== Congressional trades ingestion: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
