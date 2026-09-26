"""
One-off script: fills the database with the last 90 days of SPY, QQQ and VIX prices.

Why? The daily pipeline only saves one price per day, so on day one the market
charts would be empty. Run this once after setting up the database so the
charts have 90 days of history straight away.

    python scripts/backfill_market_context.py

Uses Yahoo Finance's chart API, which doesn't need an API key.
"""

import os
import sys
import logging
from datetime import datetime, timezone

# Let us import db.py from the ingestion/ folder.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import requests
from db import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

# Symbol as we store it -> symbol as it goes in the URL.
# "^" isn't allowed in URLs, so ^VIX is written as %5EVIX.
SYMBOLS = {
    "SPY":  "SPY",
    "QQQ":  "QQQ",
    "^VIX": "%5EVIX",
}

# Tag these rows so we can tell backfilled data apart from the daily pipeline's.
SOURCE_MAP = {
    "SPY":  "yahoo_backfill",
    "QQQ":  "yahoo_backfill",
    "^VIX": "yahoo_backfill",
}


def fetch_history(symbol_url: str, days: int = 90) -> list[tuple]:
    """Return a list of (unix_timestamp, closing_price) pairs, one per trading day."""
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
    # zip pairs up the two lists: [(t1, c1), (t2, c2), ...]
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
                if close is None:  # Yahoo sometimes has gaps — skip them
                    prev_close = close
                    continue
                fetched_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                # Daily % change compared with the day before
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
    inserted = execute_values(sql, rows)
    logger.info("Inserted %d historical market context rows", inserted)


if __name__ == "__main__":
    run()
