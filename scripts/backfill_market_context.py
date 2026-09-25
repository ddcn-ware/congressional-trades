"""
One-off backfill script — seeds market_context with 90 days of
historical daily closes for SPY, QQQ, and ^VIX.

Run once:
    python scripts/backfill_market_context.py

Uses Yahoo Finance v8 chart API (no key needed).
"""

import os
import sys
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import requests
from db import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

SYMBOLS = {
    "SPY":  "SPY",
    "QQQ":  "QQQ",
    "^VIX": "%5EVIX",
}

SOURCE_MAP = {
    "SPY":  "finnhub_backfill",
    "QQQ":  "finnhub_backfill",
    "^VIX": "yahoo_backfill",
}


def fetch_history(symbol_url: str, days: int = 90) -> list[tuple]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_url}"
    resp = requests.get(
        url,
        params={"interval": "1d", "range": f"{days}d"},
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamps"] if "timestamps" in result else result.get("timestamp", [])
    closes = result["indicators"]["quote"][0]["close"]
    return list(zip(timestamps, closes))


def run():
    rows = []
    for symbol, url_sym in SYMBOLS.items():
        logger.info("Fetching %s...", symbol)
        try:
            history = fetch_history(url_sym, days=90)
            source = SOURCE_MAP[symbol]
            prev_close = None
            for ts, close in history:
                if close is None:
                    prev_close = close
                    continue
                fetched_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                change_pct = ((close - prev_close) / prev_close * 100) if prev_close else None
                rows.append((symbol, close, change_pct, fetched_at, source))
                prev_close = close
            logger.info("  %d data points", len(history))
        except Exception as exc:
            logger.error("Failed for %s: %s", symbol, exc)

    if not rows:
        logger.error("No rows to insert")
        return

    sql = """
        INSERT INTO market_context (symbol, price, change_pct, fetched_at, source)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    # market_context has no unique constraint — insert all, duplicates will just
    # add extra rows which the dashboard handles fine (it uses DISTINCT ON symbol
    # for the snapshot and ORDER BY fetched_at for charts)
    inserted = execute_values(sql, rows)
    logger.info("Inserted %d historical market context rows", inserted)


if __name__ == "__main__":
    run()
