"""
Dashboard DB helpers — read-only queries used across pages.

All heavy queries live here so pages stay thin view code.
Results are cached with st.cache_data (TTL 15 min) so repeated
rerenders / tab switches don't re-hit the database.
"""

import os
import pandas as pd
import psycopg2
import streamlit as st


@st.cache_resource
def _get_conn():
    """
    Single persistent read-only connection for the dashboard process.
    Cached as a resource (not data) so it survives across rerenders.
    Streamlit Community Cloud restarts the process infrequently enough
    that one connection is fine; swap for a pool if self-hosting.
    """
    return psycopg2.connect(os.environ["DATABASE_URL"])


def query(sql: str, params=None) -> pd.DataFrame:
    conn = _get_conn()
    try:
        return _query_with_conn(sql, params, conn)
    except Exception:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        return _query_with_conn(sql, params, conn)


def _query_with_conn(sql: str, params, conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    df = pd.DataFrame(rows, columns=cols)
    # Postgres NUMERIC columns come back as Python Decimal objects which
    # Altair can't infer a type for — cast them all to float64 up front.
    for col in df.select_dtypes(include="object").columns:
        try:
            df[col] = pd.to_numeric(df[col], errors="ignore")
        except Exception:
            pass
    # Explicit cast for any remaining Decimal columns
    import decimal
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, decimal.Decimal)).any():
            df[col] = df[col].astype(float)
    return df


# ── Congressional trades ──────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_top_tickers(limit: int = 20) -> pd.DataFrame:
    return query(f"""
        SELECT ticker, trade_count, member_count, buys, sells, last_trade_date
        FROM vw_top_tickers
        LIMIT {limit}
    """)


@st.cache_data(ttl=900)
def get_weekly_volume(ticker: str | None = None) -> pd.DataFrame:
    if ticker:
        return query(
            "SELECT * FROM vw_weekly_volume WHERE ticker = %s ORDER BY week_start",
            params=(ticker,),
        )
    return query("SELECT * FROM vw_weekly_volume ORDER BY ticker, week_start")


@st.cache_data(ttl=900)
def get_recent_trades(days: int = 30, limit: int = 200) -> pd.DataFrame:
    return query(f"""
        SELECT member_name, chamber, ticker, transaction_type,
               amount_range, transaction_date, disclosure_date, asset_description
        FROM congressional_trades
        WHERE transaction_date >= NOW() - INTERVAL '{days} days'
        ORDER BY transaction_date DESC
        LIMIT {limit}
    """)


@st.cache_data(ttl=900)
def get_anomalies(limit: int = 50) -> pd.DataFrame:
    return query(f"""
        SELECT anomaly_type, ticker, window_start, window_end,
               trade_count, member_count, detail, detected_at
        FROM trade_anomalies
        ORDER BY detected_at DESC
        LIMIT {limit}
    """)


# ── Portfolio ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_portfolio() -> pd.DataFrame:
    return query("""
        SELECT ticker, instrument_name, quantity, avg_price,
               current_price, pnl, currency, snapshotted_at
        FROM vw_latest_portfolio
        ORDER BY ABS(pnl) DESC NULLS LAST
    """)


@st.cache_data(ttl=900)
def get_congressional_activity_for_tickers(tickers: list[str], lookback_days: int = 30) -> pd.DataFrame:
    """
    For each ticker in the portfolio, return any congressional trades in the
    last lookback_days days.  Pure join against existing data — no new API calls.
    """
    if not tickers:
        return pd.DataFrame()
    placeholders = ", ".join(["%s"] * len(tickers))
    return query(
        f"""
        SELECT ticker, member_name, transaction_type, amount_range,
               transaction_date, disclosure_date
        FROM congressional_trades
        WHERE ticker IN ({placeholders})
          AND transaction_date >= NOW() - INTERVAL '{lookback_days} days'
        ORDER BY ticker, transaction_date DESC
        """,
        params=tuple(tickers),
    )


# ── Market context ────────────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_market_context_history(symbol: str, days: int = 90) -> pd.DataFrame:
    return query(
        """
        SELECT symbol, price, change_pct, fetched_at
        FROM market_context
        WHERE symbol = %s
          AND fetched_at >= NOW() - INTERVAL %s
        ORDER BY fetched_at
        """,
        params=(symbol, f"{days} days"),
    )


@st.cache_data(ttl=900)
def get_latest_market_snapshot() -> pd.DataFrame:
    return query("""
        SELECT DISTINCT ON (symbol) symbol, price, change_pct, fetched_at, source
        FROM market_context
        ORDER BY symbol, fetched_at DESC
    """)


# ── News ──────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_general_news(limit: int = 30) -> pd.DataFrame:
    return query(f"""
        SELECT headline, summary, url, source_name, published_at
        FROM news
        WHERE ticker IS NULL
        ORDER BY published_at DESC
        LIMIT {limit}
    """)


@st.cache_data(ttl=900)
def get_ticker_news(ticker: str, limit: int = 10) -> pd.DataFrame:
    return query(
        f"""
        SELECT headline, summary, url, source_name, published_at
        FROM news
        WHERE ticker = %s
        ORDER BY published_at DESC
        LIMIT {limit}
        """,
        params=(ticker,),
    )
