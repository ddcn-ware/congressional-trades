"""
Stage 2 — Portfolio positions ingestion (Trading 212).

Uses the read-only positions endpoint to snapshot current holdings.
This stage is entirely independent of the congressional-trades stages;
a failure here doesn't affect anything else in the pipeline run.

API docs: https://t212public-api-docs.redoc.ly/#operation/portfolio
"""

import logging
import os
import time
from typing import Any

import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

T212_BASE = "https://live.trading212.com/api/v0"
# Sandbox base if you want to test without hitting live:
# T212_BASE = "https://demo.trading212.com/api/v0"


def _headers() -> dict:
    api_key = os.environ["T212_API_KEY"].strip()
    # T212 docs show the key passed directly without a Bearer prefix,
    # but try stripping any accidental prefix the user may have copied
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:]
    return {"Authorization": api_key}


def fetch_positions(retries: int = 3, backoff: float = 2.0) -> list[dict]:
    """
    Fetch all current open positions from Trading 212.
    The endpoint returns a list of position objects; no pagination needed
    for typical retail portfolio sizes.
    """
    url = f"{T212_BASE}/equity/portfolio"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), timeout=20)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Fetched %d positions from Trading 212", len(data))
            return data
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            wait = backoff ** attempt
            logger.warning("T212 fetch attempt %d failed (%s), retrying in %.1fs", attempt, exc, wait)
            time.sleep(wait)
    return []


def load(positions: list[dict]) -> int:
    """
    Insert a fresh snapshot row for each position.
    We don't upsert/overwrite — each run appends a new timestamped snapshot
    so we retain history. The vw_latest_portfolio view returns only the
    most recent snapshot per ticker for the dashboard.
    """
    if not positions:
        logger.info("No positions to load")
        return 0

    rows = []
    for p in positions:
        ticker = (p.get("ticker") or "").replace("_EQ", "").strip()  # T212 appends _EQ to equity tickers
        rows.append((
            ticker,
            p.get("fullName") or p.get("name"),
            p.get("quantity"),
            p.get("averagePrice"),
            p.get("currentPrice"),
            p.get("ppl"),           # profit/loss in account currency
            p.get("fxPpl"),         # may be None for same-currency holdings
            p.get("currency") or "GBP",
        ))

    sql = """
        INSERT INTO portfolio_positions
            (ticker, instrument_name, quantity, avg_price, current_price, pnl, pnl_pct, currency)
        VALUES %s
    """
    # pnl_pct: T212 doesn't return this directly so we derive it at query time via the view,
    # but we store NULL here rather than computing it in Python to avoid stale avg_price drift.
    inserted = execute_values(sql, rows)
    logger.info("Inserted %d portfolio position rows", inserted)
    return inserted


def run() -> dict[str, Any]:
    logger.info("=== Portfolio ingestion: start ===")
    positions = fetch_positions()
    inserted = load(positions)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM portfolio_positions")
            total = cur.fetchone()[0]
    finally:
        put_conn(conn)

    summary = {
        "stage":    "portfolio",
        "fetched":  len(positions),
        "inserted": inserted,
        "db_total": total,
    }
    logger.info("=== Portfolio ingestion: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
