"""
Stage 2 — takes a snapshot of my Trading 212 portfolio and saves it to the database.

How logging in to Trading 212 works:
  T212 uses "HTTP Basic auth". You join your API ID and secret with a colon
  ("ID:SECRET"), Base64-encode it, and send it in the Authorization header.
  Base64 isn't encryption — it's just a way of turning text into safe characters
  for a web request. The HTTPS connection is what keeps it private.

  Set T212_API_ID and T212_API_SECRET in your .env file (two separate values).
  The key only needs read-only permission — this code never places trades.

This stage doesn't depend on the congressional data at all, so if it fails
the rest of the pipeline carries on.
"""

import base64
import logging
import os
import time
from typing import Any

import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

T212_BASE = "https://live.trading212.com/api/v0"

# Some of my positions are still stored under old tickers. For example, I bought
# Grab when it was still a SPAC called Altimeter Growth Corp (AGC), and T212
# never updated the symbol. This table maps the old ticker to the real company
# name and its current ticker (used to fetch the right logo).
#   key   = ticker as T212 gives it
#   value = (company name, current ticker)
# It takes priority over Finnhub, which often doesn't know about renamed stocks.
_TICKER_RENAMES: dict[str, tuple[str, str]] = {
    # Renamed stocks
    "FB":   ("Meta Platforms Inc",  "META"),  # Facebook renamed to Meta
    "YNDX": ("Nebius Group N.V.",   "NBIS"),  # Yandex's international arm rebranded to Nebius
    "TWTR": ("X Corp",              "X"),     # Twitter renamed to X
    # SPAC → post-merger tickers (a SPAC is a shell company that merges with a real one)
    "AGC":  ("Grab Holdings Ltd",   "GRAB"),  # Altimeter Growth Corp → Grab
    "NPA":  ("AST SpaceMobile Inc", "ASTS"),  # New Providence Acquisition → AST SpaceMobile
    "OAC":  ("Hims & Hers Health",  "HIMS"),  # Oaktree Acquisition Corp → Hims & Hers
}


def _headers() -> dict:
    """Build the login header for Trading 212 (see the note at the top)."""
    api_id     = os.environ["T212_API_ID"].strip()
    api_secret = os.environ["T212_API_SECRET"].strip()
    encoded    = base64.b64encode(f"{api_id}:{api_secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def fetch_cash() -> dict:
    """
    Get the account summary — total value, overall profit/loss, free cash.
    Returns {} on failure so the rest of the stage can still run.
    """
    try:
        resp = requests.get(f"{T212_BASE}/equity/account/cash", headers=_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("T212 cash fetch failed: %s", exc)
        return {}


def _clean_ticker(raw: str) -> str:
    """
    T212 adds suffixes to tickers, e.g. "AAPL_US_EQ". Strip them to get "AAPL".
      "AAPL_US_EQ" -> remove "_EQ" -> "AAPL_US" -> remove "_US" -> "AAPL"
    """
    t = raw
    if t.endswith("_EQ"):
        t = t[:-3]
    if "_" in t:
        base = t.rsplit("_", 1)[0]
        suffix = t.rsplit("_", 1)[1]
        # Only strip short all-letter suffixes like "US" or "UK" (country codes).
        if len(suffix) <= 3 and suffix.isalpha():
            t = base
    return t.strip()


def _lookup_name(ticker: str, finnhub_key: str | None) -> str | None:
    """
    Find the company name for a ticker. T212's portfolio endpoint only gives
    tickers, not names, so we ask Finnhub's company-profile endpoint.

    Our _TICKER_RENAMES table is checked first, because Finnhub has no record
    of old/delisted tickers and would just return nothing.
    """
    if ticker.upper() in _TICKER_RENAMES:
        display_name, _ = _TICKER_RENAMES[ticker.upper()]
        return display_name

    if not finnhub_key:
        return None
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/profile2",
            params={"symbol": ticker},
            headers={"X-Finnhub-Token": finnhub_key},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("name") or None
    except Exception:
        pass  # a missing name isn't worth crashing over — the dashboard falls back to the ticker
    return None


def fetch_positions(retries: int = 3, backoff: float = 2.0) -> list[dict]:
    """Get every stock I currently hold. Retries with backoff if the request fails."""
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
    Save one new row per position.

    We never overwrite old rows — every run adds a fresh set. That keeps a
    history of my portfolio over time. The database view vw_latest_portfolio
    then picks out just the newest row per ticker for the dashboard.
    """
    if not positions:
        logger.info("No positions to load")
        return 0

    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    rows = []
    for p in positions:
        ticker = _clean_ticker(p.get("ticker") or "")
        name = _lookup_name(ticker, finnhub_key)
        rows.append((
            ticker,
            name,
            p.get("quantity"),
            p.get("averagePrice"),   # what I paid per share on average
            p.get("currentPrice"),
            p.get("ppl"),            # "ppl" = profit / loss in my account currency
            p.get("fxPpl"),          # profit / loss from currency exchange movements
            p.get("currency") or "GBP",
        ))
        # Finnhub's free tier allows 60 requests per minute. Pausing half a
        # second between lookups keeps us safely under that.
        time.sleep(0.5)

    sql = """
        INSERT INTO portfolio_positions
            (ticker, instrument_name, quantity, avg_price, current_price, pnl, pnl_pct, currency)
        VALUES %s
    """
    inserted = execute_values(sql, rows)
    logger.info("Inserted %d portfolio position rows", inserted)
    return inserted


def run() -> dict[str, Any]:
    logger.info("=== Portfolio ingestion: start ===")
    positions = fetch_positions()
    cash      = fetch_cash()
    inserted  = load(positions)

    # Save my total account value and overall P&L too. They're stored in the
    # market_context table under made-up symbols (PORTFOLIO_TOTAL / PORTFOLIO_PPL)
    # so the dashboard can read them from the database without calling T212.
    if cash:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO market_context (symbol, price, change_pct, fetched_at, source)
                    VALUES (%s, %s, %s, NOW(), %s)
                    """,
                    ("PORTFOLIO_TOTAL", cash.get("total"), None, "t212"),
                )
                cur.execute(
                    """
                    INSERT INTO market_context (symbol, price, change_pct, fetched_at, source)
                    VALUES (%s, %s, %s, NOW(), %s)
                    """,
                    ("PORTFOLIO_PPL", cash.get("ppl"), None, "t212"),
                )
            conn.commit()
        finally:
            put_conn(conn)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM portfolio_positions")
            total = cur.fetchone()[0]
    finally:
        put_conn(conn)

    summary = {
        "stage":       "portfolio",
        "fetched":     len(positions),
        "inserted":    inserted,
        "db_total":    total,
        "total_value": cash.get("total"),
        "total_ppl":   cash.get("ppl"),
    }
    logger.info("=== Portfolio ingestion: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
