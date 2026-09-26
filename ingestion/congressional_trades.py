"""
Stage 1 — downloads congressional trade disclosures and saves them to the database.

Background:
  Under the STOCK Act, members of the US House of Representatives must publicly
  report stock trades within 45 days. The official reports are PDFs, which are
  painful to read with code.

Where the data comes from:
  insiderwatch-data (saminjafari/insiderwatch-data on GitHub) — a community
  project that reads those PDFs and publishes the trades as one big CSV file,
  one row per trade, with the ticker included.

  I originally planned to use House Stock Watcher, but it went offline while I
  was building this. The official House Clerk ZIP files only list filings, not
  individual trades, so this CSV was the best free option.

The three steps (this is what "ETL" means):
  Extract   — fetch_all_trades() downloads the CSV
  Transform — clean() tidies it up (dates, buy/sell labels, blank tickers)
  Load      — load() inserts it into the congressional_trades table

Safe to re-run: every row gets an ID, and the database rejects any row whose
(source, raw_id) already exists, so running this twice never creates duplicates.
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


# ── Extract ───────────────────────────────────────────────────────────────────

def fetch_all_trades(retries: int = 3, backoff: float = 2.0) -> pd.DataFrame:
    """
    Download the CSV and return it as a pandas DataFrame (basically a table).

    Networks are flaky, so if the download fails we wait and try again, up to
    3 times. The wait grows each time (2s, then 4s) — this is called
    "exponential backoff" and stops us hammering a server that's struggling.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(CSV_URL, timeout=30)
            resp.raise_for_status()  # turns HTTP errors like 404/500 into exceptions
            df = pd.read_csv(pd.io.common.StringIO(resp.text))
            logger.info("Fetched %d rows from insiderwatch-data", len(df))
            return df
        except Exception as exc:
            if attempt == retries:
                raise  # out of retries — let the pipeline mark this stage as failed
            wait = backoff ** attempt
            logger.warning("Fetch attempt %d failed (%s), retrying in %.1fs", attempt, exc, wait)
            time.sleep(wait)
    return pd.DataFrame()


# ── Transform ─────────────────────────────────────────────────────────────────

def _parse_date(raw) -> date | None:
    """
    Turn a date string into a proper date object, or None if it's blank/unreadable.
    The source isn't consistent about format, so we try a few common ones.
    """
    if pd.isna(raw) or not str(raw).strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).date()
        except ValueError:
            continue  # wrong format — try the next one
    logger.debug("Unparseable date: %r", raw)
    return None


def _stable_id(row: pd.Series) -> str:
    """
    Make a unique ID for a row that doesn't have a filing_id.

    We glue together the fields that describe the trade and hash them with SHA-1.
    Hashing always gives the same output for the same input, so the same trade
    gets the same ID on every run — which is what lets us spot duplicates.
    """
    key = "|".join([
        str(row.get("member", "")),
        str(row.get("ticker", "")),
        str(row.get("transaction_date", "")),
        str(row.get("action", "")),
        str(row.get("filing_id", "")),
    ])
    return hashlib.sha1(key.encode()).hexdigest()


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Tidy the raw CSV into the shape our database table expects."""
    if df.empty:
        return df

    # Work on a copy so we don't accidentally change the original DataFrame.
    df = df.copy()

    # The source uses lots of different words for the same thing
    # ("purchase", "sale_partial"...). Squash them down to buy / sell / exchange.
    action_map = {
        "buy": "buy", "purchase": "buy",
        "sell": "sell", "sale": "sell", "sale_full": "sell", "sale_partial": "sell",
        "exchange": "exchange",
    }
    df.loc[:, "transaction_type"] = df["action"].str.lower().str.strip().map(action_map).fillna(
        df["action"].str.lower().str.strip()  # anything not in the map is kept as-is
    )

    # Some rows use "--" or "N/A" when there's no ticker (e.g. bonds, funds).
    # Replace those with a real empty value (None) so they don't look like tickers.
    df.loc[:, "ticker"] = df["ticker"].where(
        df["ticker"].notna() & ~df["ticker"].str.strip().str.upper().isin(["--", "N/A", "NA", ""]),
        other=None,
    )

    df.loc[:, "transaction_date"] = df["transaction_date"].apply(_parse_date)
    df.loc[:, "filed_date"]       = df["filed_date"].apply(_parse_date)
    df.loc[:, "chamber"]          = df["chamber"].str.strip().str.title()  # "house" -> "House"

    # Rename columns to match our database column names.
    df = df.rename(columns={"amount_min_usd": "amount_min", "amount_range": "amount_range_raw"})
    df.loc[:, "amount_max"] = None  # the source doesn't give an upper bound

    # Give every row a unique raw_id so re-runs can skip duplicates.
    # One filing (one PDF) can contain several trades, so filing_id alone isn't
    # unique — we add the row number on the end, e.g. "12345_0", "12345_1".
    df = df.reset_index(drop=True)
    df.loc[:, "raw_id"] = df.apply(
        lambda r: f"{r['filing_id']}_{r.name}" if pd.notna(r.get("filing_id")) and str(r.get("filing_id", "")).strip()
        else _stable_id(r),
        axis=1,  # axis=1 means "run this once per row"
    )

    df.loc[:, "source"]            = "insiderwatch"
    df.loc[:, "member_name"]       = df["member"].str.strip()
    df.loc[:, "asset_description"] = df.get("asset", pd.Series(dtype=str)).str.strip()
    df.loc[:, "disclosure_date"]   = df["filed_date"]

    # A trade with no member name is useless to us, so drop it.
    df = df[df["member_name"].notna() & (df["member_name"] != "")]
    logger.info("Cleaned %d rows", len(df))
    return df


# ── Load ──────────────────────────────────────────────────────────────────────

def load(df: pd.DataFrame) -> int:
    """Insert the cleaned rows into the database. Returns how many were new."""
    if df.empty:
        logger.info("No rows to load")
        return 0

    # The database columns we're filling, in order.
    cols = [
        "member_name", "chamber", "ticker", "asset_description",
        "transaction_type", "amount_range", "amount_min", "amount_max",
        "transaction_date", "disclosure_date", "filed_date", "source", "raw_id",
    ]

    # The DataFrame calls it "amount_range_raw" but the table calls it
    # "amount_range" — this map translates between the two names.
    col_map = {"amount_range": "amount_range_raw"}
    rows = []
    for _, row in df.iterrows():
        rows.append(tuple(
            row.get(col_map.get(c, c)) for c in cols
        ))

    # "ON CONFLICT ... DO NOTHING" = if a row with this (source, raw_id)
    # already exists, silently skip it instead of erroring. This is what makes
    # the stage safe to run every day.
    sql = f"""
        INSERT INTO congressional_trades ({', '.join(cols)})
        VALUES %s
        ON CONFLICT (source, raw_id) DO NOTHING
    """
    inserted = execute_values(sql, rows)
    logger.info("Loaded %d new rows into congressional_trades", inserted)
    return inserted


def run() -> dict[str, Any]:
    """Run Extract -> Transform -> Load and return a short summary for the logs."""
    logger.info("=== Congressional trades ingestion: start ===")
    raw_df = fetch_all_trades()
    df = clean(raw_df)
    inserted = load(df)

    # Count the total rows now in the table, just for the summary.
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


# Lets you run just this stage on its own: python ingestion/congressional_trades.py
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
