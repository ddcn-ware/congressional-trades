"""
Stage 3 — Market context ingestion.

SPY and QQQ via Finnhub (equity/ETF endpoint — the same endpoint used
everywhere else, works fine on the free tier).

VIX via yfinance specifically because Finnhub's free tier doesn't cover
raw index symbols like ^VIX, and VIXY/UVXY track VIX futures (not the
index itself), making them unsuitable proxies for implied volatility.
yfinance is an unofficial scraper so we wrap it defensively and fall back
to the last cached value rather than failing the whole stage.

All three are written to market_context so the dashboard reads from DB,
not from live API calls on page load.
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
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_finnhub_quote(symbol: str, retries: int = 3) -> dict | None:
    """
    Fetch a real-time quote from Finnhub.
    Returns the raw quote dict or None on failure so the caller can decide
    whether to skip or raise.
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
            # Finnhub returns {"c":0,"d":null,...} for unknown symbols — treat c==0 as failure
            if not data.get("c"):
                logger.warning("Finnhub returned empty/zero quote for %s", symbol)
                return None
            return data
        except requests.RequestException as exc:
            if attempt == retries:
                logger.error("Finnhub quote fetch failed for %s after %d attempts: %s", symbol, retries, exc)
                return None
            time.sleep(2 ** attempt)
    return None


def _fetch_vix_yfinance() -> tuple[float | None, float | None]:
    """
    Fetch the current VIX level via Yahoo Finance's v8 chart API directly.
    yfinance's wrapper is broken against Python 3.12/3.14 and Yahoo's scraping
    blocks, but the underlying JSON endpoint works fine with a browser UA.
    """
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "5d"},
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
    """
    Return the most recently cached (price, change_pct) for a symbol.
    Used as a fallback when the live fetch fails so the dashboard always
    has something to display rather than a blank chart.
    """
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

    rows = []
    now = datetime.now(timezone.utc)

    # ── SPY and QQQ via Finnhub ───────────────────────────────────────────────
    for symbol in MARKET_ETFS:
        quote = _fetch_finnhub_quote(symbol)
        if quote:
            price = quote["c"]                                  # current price
            prev_close = quote.get("pc")
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else None
            rows.append((symbol, price, change_pct, now, "finnhub"))
            logger.info("%s: %.2f (%.2f%%)", symbol, price, change_pct or 0)
        else:
            logger.warning("No quote for %s — skipping this symbol this run", symbol)

    # ── VIX via yfinance ──────────────────────────────────────────────────────
    vix_price, vix_change = _fetch_vix_yfinance()

    if vix_price is None:
        # Fall back to last cached value rather than writing a null row
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
