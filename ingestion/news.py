"""
Stage 4 — News ingestion (Finnhub).

Two flavours stored in the same table:
  1. General market news  — top headlines, ticker=NULL
  2. Portfolio news       — company-specific headlines, ticker=<symbol>

Portfolio news is keyed off the tickers currently in portfolio_positions
(latest snapshot). This stage must run after the portfolio stage so those
rows exist.

Finnhub free tier: 60 calls/min.
We batch per-ticker requests with a short sleep between calls to stay
within that limit. For large portfolios we cap at MAX_TICKERS_PER_RUN
rather than assuming every ticker completes in time.

Dedup: (source, source_id) has a UNIQUE constraint so re-runs are safe.
"""

import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

FINNHUB_BASE   = "https://finnhub.io/api/v1"
GENERAL_CAT    = "general"         # Finnhub market news category
NEWS_LOOKBACK  = 3                 # days of news to fetch per ticker
MAX_TICKERS_PER_RUN = 20          # safety cap; raise if portfolio grows
# At 1 req/s we spend MAX_TICKERS_PER_RUN seconds on ticker news.
# Keep well under the 60/min rate limit even if the general-news call
# also runs in the same minute.
INTER_REQUEST_SLEEP = 1.1         # seconds between per-ticker Finnhub calls


def _headers() -> dict:
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_general_news(retries: int = 3) -> list[dict]:
    url = f"{FINNHUB_BASE}/news"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params={"category": GENERAL_CAT}, headers=_headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Fetched %d general market headlines", len(data))
            return data
        except requests.RequestException as exc:
            if attempt == retries:
                logger.error("General news fetch failed: %s", exc)
                return []
            time.sleep(2 ** attempt)
    return []


def _fetch_company_news(symbol: str, from_date: str, to_date: str, retries: int = 3) -> list[dict]:
    url = f"{FINNHUB_BASE}/company-news"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                params={"symbol": symbol, "from": from_date, "to": to_date},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data
        except requests.RequestException as exc:
            if attempt == retries:
                logger.error("Company news fetch failed for %s: %s", symbol, exc)
                return []
            time.sleep(2 ** attempt)
    return []


def _held_tickers(conn) -> list[str]:
    """
    Return tickers from the latest portfolio snapshot.
    If the portfolio stage failed or hasn't run yet, returns an empty list
    so news ingestion degrades gracefully rather than crashing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (ticker) ticker
            FROM portfolio_positions
            ORDER BY ticker, snapshotted_at DESC
            """
        )
        rows = cur.fetchall()
    return [r[0] for r in rows if r[0]]


def _news_to_rows(articles: list[dict], ticker: str | None) -> list[tuple]:
    """
    Map raw Finnhub article dicts into tuples matching the news table schema.
    ticker=None for general headlines.
    """
    rows = []
    for a in articles:
        raw_id = a.get("id")
        # Finnhub sometimes returns id=0 (integer zero) for general news — treat
        # that as missing since 0 isn't a useful dedup key and would cause all
        # such articles to conflict with each other on (source, source_id).
        source_id = str(raw_id) if raw_id not in (None, 0, "", "0") else ""
        if not source_id:
            # Fall back to a hash of headline + published timestamp so we still
            # get dedup without a real id.
            import hashlib
            fingerprint = f"{a.get('headline','')}{a.get('datetime','')}"
            source_id = "hash:" + hashlib.sha1(fingerprint.encode()).hexdigest()
        published_ts = a.get("datetime")
        published_at = (
            datetime.fromtimestamp(published_ts, tz=timezone.utc) if published_ts else None
        )
        rows.append((
            a.get("headline") or "",
            a.get("summary") or "",
            a.get("url") or "",
            a.get("source") or "",
            ticker,
            published_at,
            "finnhub",
            source_id,
        ))
    return rows


def run() -> dict[str, Any]:
    logger.info("=== News ingestion: start ===")

    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set — skipping news stage")
        return {"stage": "news", "skipped": True}

    all_rows: list[tuple] = []

    # ── General market news ───────────────────────────────────────────────────
    general = _fetch_general_news()
    all_rows.extend(_news_to_rows(general, ticker=None))

    # ── Portfolio-scoped news ─────────────────────────────────────────────────
    conn = get_conn()
    try:
        tickers = _held_tickers(conn)
    finally:
        put_conn(conn)

    if not tickers:
        logger.info("No portfolio tickers found — only general news will be stored")
    else:
        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=NEWS_LOOKBACK)).isoformat()
        to_date = today.isoformat()

        # Cap to avoid blowing through the rate limit on a large portfolio
        tickers_to_fetch = tickers[:MAX_TICKERS_PER_RUN]
        if len(tickers) > MAX_TICKERS_PER_RUN:
            logger.warning(
                "Portfolio has %d tickers but MAX_TICKERS_PER_RUN=%d; "
                "remaining tickers will be picked up on future runs",
                len(tickers), MAX_TICKERS_PER_RUN,
            )

        for i, ticker in enumerate(tickers_to_fetch):
            articles = _fetch_company_news(ticker, from_date, to_date)
            all_rows.extend(_news_to_rows(articles, ticker=ticker))
            logger.debug("Fetched %d articles for %s", len(articles), ticker)

            # Pace requests to stay within 60/min.
            # We don't need to sleep after the last ticker.
            if i < len(tickers_to_fetch) - 1:
                time.sleep(INTER_REQUEST_SLEEP)

    # ── Bulk insert ───────────────────────────────────────────────────────────
    if not all_rows:
        logger.info("No news rows to insert")
        return {"stage": "news", "inserted": 0}

    sql = """
        INSERT INTO news (headline, summary, url, source_name, ticker, published_at, source, source_id)
        VALUES %s
        ON CONFLICT (source, source_id) DO NOTHING
    """
    inserted = execute_values(sql, all_rows)
    logger.info(
        "News ingestion: %d candidate rows, %d newly inserted (rest were duplicates)",
        len(all_rows), inserted,
    )

    summary = {
        "stage":            "news",
        "general_fetched":  len(general),
        "ticker_count":     len(tickers_to_fetch) if tickers else 0,
        "total_candidates": len(all_rows),
        "inserted":         inserted,
    }
    logger.info("=== News ingestion: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
