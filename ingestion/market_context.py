"""
Stage 3 — records today's market "weather": SPY, QQQ and the VIX.

  SPY  = a fund that tracks the S&P 500 (the 500 biggest US companies)
  QQQ  = a fund that tracks the Nasdaq-100 (mostly big tech)
  ^VIX = the "fear index" — how much volatility traders expect. High = nervous market.

SPY and QQQ come from Finnhub, the same free API used for news.

VIX is trickier. Finnhub's free tier doesn't include raw indexes like ^VIX,
and the VIX funds you can buy (VIXY, UVXY) track VIX *futures*, which drift
away from the real index — so they're not a fair substitute. Instead we call
Yahoo Finance's chart API directly. It's unofficial, so it's wrapped in a
try/except: if Yahoo blocks us, we reuse the last VIX value we saved rather
than crashing the stage.

Everything is saved to the database, so the dashboard just reads from there
and never has to wait on these APIs when the page loads.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
MARKET_ETFS = ["SPY", "QQQ"]


def _finnhub_headers() -> dict:
    """Finnhub identifies us by an API key sent in a header."""
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_finnhub_quote(symbol: str, retries: int = 3) -> dict | None:
    """
    Get the latest price for a symbol from Finnhub.

    Finnhub's reply uses one-letter keys: "c" = current price,
    "pc" = previous day's closing price. Returns None if there's no data,
    and lets the caller decide what to do about it.
    """
    url = f"{FINNHUB_BASE}/quote"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                params={"symbol": symbol},
                headers=_finnhub_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            # A price of 0 means Finnhub doesn't know this symbol.
            if not data.get("c"):
                logger.warning("Finnhub returned empty quote for %s", symbol)
                return None
            return data
        except requests.RequestException as exc:
            if attempt == retries:
                logger.error("Finnhub quote fetch failed for %s after %d attempts: %s", symbol, retries, exc)
                return None
            time.sleep(2 ** attempt)  # wait 2s, then 4s, before retrying
    return None


def _fetch_vix_yfinance() -> tuple[float | None, float | None]:
    """
    Get the current VIX level from Yahoo Finance's chart API.
    Returns (price, % change since yesterday), or (None, None) if it fails.
    """
    try:
        resp = requests.get(
            # %5E is the URL-safe way of writing "^" (so this is ^VIX)
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "5d"},
            # Yahoo rejects requests that don't look like they come from a browser,
            # so we send a normal browser "User-Agent".
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        price = float(result["meta"]["regularMarketPrice"])
        prev = float(result["meta"].get("chartPreviousClose") or result["meta"].get("previousClose") or 0)
        change_pct = ((price - prev) / prev * 100) if prev else None
        return price, change_pct
    except Exception as exc:
        logger.warning("VIX fetch failed: %s", exc)
        return None, None


def _last_cached_value(symbol: str, conn) -> tuple[float | None, float | None]:
    """Look up the most recent price we saved for a symbol — the VIX backup plan."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT price, change_pct
            FROM market_context
            WHERE symbol = %s
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def run() -> dict[str, Any]:
    logger.info("=== Market context ingestion: start ===")

    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set — skipping market context stage")
        return {"stage": "market_context", "skipped": True}

    rows = []  # each row = (symbol, price, change_pct, time, source)
    now = datetime.now(timezone.utc)

    # 1) SPY and QQQ from Finnhub
    for symbol in MARKET_ETFS:
        quote = _fetch_finnhub_quote(symbol)
        if quote:
            price = quote["c"]
            prev_close = quote.get("pc")
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else None
            rows.append((symbol, price, change_pct, now, "finnhub"))
            logger.info("%s: %.2f (%.2f%%)", symbol, price, change_pct or 0)
        else:
            logger.warning("No quote for %s — skipping this run", symbol)

    # 2) VIX from Yahoo, falling back to the last saved value if Yahoo fails
    vix_price, vix_change = _fetch_vix_yfinance()

    if vix_price is None:
        conn = get_conn()
        try:
            vix_price, vix_change = _last_cached_value("^VIX", conn)
        finally:
            put_conn(conn)

        if vix_price is not None:
            logger.info("VIX: using last cached value %.2f", vix_price)
        else:
            logger.warning("VIX: no live data and no cached fallback — skipping")

    if vix_price is not None:
        rows.append(("^VIX", vix_price, vix_change, now, "yfinance"))
        logger.info("^VIX: %.2f", vix_price)

    if not rows:
        logger.warning("No market context rows to insert this run")
        return {"stage": "market_context", "inserted": 0}

    # 3) Save everything in one insert. We always add new rows (never update),
    # which builds up a price history the dashboard can chart.
    sql = """
        INSERT INTO market_context (symbol, price, change_pct, fetched_at, source)
        VALUES %s
    """
    inserted = execute_values(sql, rows)
    logger.info("Inserted %d market context rows", inserted)

    summary = {"stage": "market_context", "inserted": inserted}
    logger.info("=== Market context ingestion: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
