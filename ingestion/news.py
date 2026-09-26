"""
Stage 4 — downloads news headlines from Finnhub and saves them to the database.

Two kinds of news go into the same "news" table:
  - General market headlines           -> saved with ticker = NULL (empty)
  - News about each stock I hold       -> saved with that stock's ticker

This stage runs after the portfolio stage, because it reads my holdings from
the database to know which companies to fetch news for. No tickers are
hard-coded — if I buy a new stock, its news starts showing up automatically.

Rate limits: Finnhub's free tier allows 60 requests per minute. We wait just
over a second between each company, and cap the number of companies per run
at MAX_TICKERS_PER_RUN, so we never go over.

Safe to re-run: each article has an ID, and the database skips any article
it has already saved.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

# Settings — change these to tweak behaviour without touching the logic.
FINNHUB_BASE        = "https://finnhub.io/api/v1"
GENERAL_CAT         = "general"
NEWS_LOOKBACK       = 3           # fetch company news from the last 3 days
MAX_TICKERS_PER_RUN = 20          # safety cap; increase if the portfolio grows
INTER_REQUEST_SLEEP = 1.1         # seconds between company requests (~54 per minute)


def _headers() -> dict:
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_general_news(retries: int = 3) -> list[dict]:
    """Get the latest general market headlines. Returns [] if it keeps failing."""
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
    """Get news about one company between two dates (format YYYY-MM-DD)."""
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
            return resp.json()
        except requests.RequestException as exc:
            if attempt == retries:
                logger.error("Company news fetch failed for %s: %s", symbol, exc)
                return []
            time.sleep(2 ** attempt)
    return []


def _held_tickers(conn) -> list[str]:
    """
    Return the tickers I currently hold (from the latest portfolio snapshot).
    If the portfolio stage hasn't run yet this returns [], and we just save
    general news instead of failing.
    """
    with conn.cursor() as cur:
        # DISTINCT ON (ticker) + ORDER BY snapshotted_at DESC is a PostgreSQL
        # trick for "give me only the newest row for each ticker".
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
    """Convert Finnhub's article format into tuples matching our news table columns."""
    rows = []
    for a in articles:
        raw_id = a.get("id")
        # We need a unique ID per article to skip duplicates. Finnhub sometimes
        # gives general news an id of 0 — if we used that, every such article
        # would look like the same one. So in that case we make our own ID by
        # hashing the headline + timestamp.
        source_id = str(raw_id) if raw_id not in (None, 0, "", "0") else ""
        if not source_id:
            import hashlib
            fingerprint = f"{a.get('headline','')}{a.get('datetime','')}"
            source_id = "hash:" + hashlib.sha1(fingerprint.encode()).hexdigest()
        # Finnhub gives times as a Unix timestamp (seconds since 1 Jan 1970).
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

    # 1) General market news
    general = _fetch_general_news()
    all_rows.extend(_news_to_rows(general, ticker=None))

    # 2) Find out which stocks I hold
    conn = get_conn()
    try:
        tickers = _held_tickers(conn)
    finally:
        put_conn(conn)

    # 3) Fetch news for each of those stocks
    if not tickers:
        logger.info("No portfolio tickers found — storing general news only")
    else:
        today     = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=NEWS_LOOKBACK)).isoformat()
        to_date   = today.isoformat()

        tickers_to_fetch = tickers[:MAX_TICKERS_PER_RUN]
        if len(tickers) > MAX_TICKERS_PER_RUN:
            logger.warning(
                "Portfolio has %d tickers; capped at %d this run — rest picked up next time",
                len(tickers), MAX_TICKERS_PER_RUN,
            )

        for i, ticker in enumerate(tickers_to_fetch):
            articles = _fetch_company_news(ticker, from_date, to_date)
            all_rows.extend(_news_to_rows(articles, ticker=ticker))
            logger.debug("Fetched %d articles for %s", len(articles), ticker)
            # Pause between requests to respect the rate limit (no need after the last one).
            if i < len(tickers_to_fetch) - 1:
                time.sleep(INTER_REQUEST_SLEEP)

    if not all_rows:
        logger.info("No news rows to insert")
        return {"stage": "news", "inserted": 0}

    # 4) Save everything, skipping articles we already have.
    sql = """
        INSERT INTO news (headline, summary, url, source_name, ticker, published_at, source, source_id)
        VALUES %s
        ON CONFLICT (source, source_id) DO NOTHING
    """
    inserted = execute_values(sql, all_rows)
    logger.info(
        "News: %d candidates, %d inserted (%d duplicates skipped)",
        len(all_rows), inserted, len(all_rows) - inserted,
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
