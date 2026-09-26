"""
All the database reads the dashboard needs, in one place.

Keeping the SQL here means app.py only deals with layout, and every query
is easy to find.

Caching:
  Streamlit re-runs the whole app.py script every time you click anything.
  Without caching, every click would re-query the database. The
  @st.cache_data(ttl=900) decorator remembers each function's result for 900
  seconds (15 minutes) — call it again with the same arguments inside that
  time and you get the saved answer instantly. The pipeline only runs once a
  day, so 15-minute-old data is perfectly fresh.
"""

import os
import pandas as pd
import psycopg2
import requests
import streamlit as st


@st.cache_resource
def _get_conn():
    """
    Open one database connection and keep reusing it.

    @st.cache_resource is like cache_data, but for things you shouldn't copy
    (connections, models) — it keeps the same object alive across every
    re-run instead of reconnecting each time.
    """
    return psycopg2.connect(os.environ["DATABASE_URL"])


def query(sql: str, params=None) -> pd.DataFrame:
    """
    Run a SQL query and return the result as a pandas DataFrame.

    If the saved connection has dropped (hosted databases close idle
    connections), open a fresh one and try once more.
    """
    conn = _get_conn()
    try:
        return _query_with_conn(sql, params, conn)
    except Exception:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        return _query_with_conn(sql, params, conn)


def _query_with_conn(sql: str, params, conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        # params fill in the %s placeholders safely. Never paste user input
        # straight into SQL — that's how SQL injection attacks happen.
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    df = pd.DataFrame(rows, columns=cols)
    # Postgres NUMERIC columns come back as Python Decimal objects, which the
    # charting library can't plot. Convert them to normal floats.
    for col in df.select_dtypes(include="object").columns:
        try:
            df[col] = pd.to_numeric(df[col], errors="ignore")
        except Exception:
            pass
    # Catch any Decimal columns the step above missed.
    import decimal
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, decimal.Decimal)).any():
            df[col] = df[col].astype(float)
    return df


# ── Congressional trades ──────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_top_tickers(limit: int = 20) -> pd.DataFrame:
    """The most-traded tickers by members of Congress (reads the vw_top_tickers view)."""
    return query(f"""
        SELECT ticker, trade_count, member_count, buys, sells, last_trade_date
        FROM vw_top_tickers
        LIMIT {limit}
    """)


@st.cache_data(ttl=900)
def get_weekly_volume(ticker: str | None = None) -> pd.DataFrame:
    """Trades per week — for one ticker if given, otherwise for all of them."""
    if ticker:
        return query(
            "SELECT * FROM vw_weekly_volume WHERE ticker = %s ORDER BY week_start",
            params=(ticker,),
        )
    return query("SELECT * FROM vw_weekly_volume ORDER BY ticker, week_start")


@st.cache_data(ttl=900)
def get_recent_trades(days: int = 30, limit: int = 200) -> pd.DataFrame:
    """Every trade made in the last `days` days, newest first."""
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
    """The most recently flagged anomalies from anomaly_detection.py."""
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
    """My current holdings, biggest profit or loss first."""
    return query("""
        SELECT ticker, instrument_name, quantity, avg_price,
               current_price, pnl, currency, snapshotted_at
        FROM vw_latest_portfolio
        ORDER BY ABS(pnl) DESC NULLS LAST
    """)


@st.cache_data(ttl=900)
def get_portfolio_totals() -> dict:
    """
    Total account value and overall P&L, e.g.
    {"PORTFOLIO_TOTAL": 17936.05, "PORTFOLIO_PPL": 2475.25}.
    The portfolio stage saves these in market_context under those made-up symbols.
    """
    df = query("""
        SELECT DISTINCT ON (symbol) symbol, price
        FROM market_context
        WHERE symbol IN ('PORTFOLIO_TOTAL', 'PORTFOLIO_PPL')
        ORDER BY symbol, fetched_at DESC
    """)
    if df.empty:
        return {}
    result = {}
    for _, row in df.iterrows():
        result[row["symbol"]] = row["price"]
    return result


@st.cache_data(ttl=300)
def get_portfolio_intraday(holdings: tuple[tuple[str, float], ...]) -> pd.DataFrame:
    """
    How much my portfolio has moved today, in %, every 5 minutes.

    This is the one place the dashboard calls an outside API instead of the
    database — the pipeline only runs once a day, so the database has no
    intraday prices. Cached for 5 minutes (ttl=300) so we don't spam Yahoo.

    `holdings` is ((ticker, number_of_shares), ...). It's a tuple rather than a
    list because Streamlit's cache needs arguments it can hash.

    The maths: for each 5-minute bar, add up (price x shares) for every stock
    to get the portfolio's value. Compare that with yesterday's closing value:
        pct = (value_now / value_at_yesterdays_close - 1) x 100
    """
    closes, base = {}, 0.0  # base = portfolio value at yesterday's close
    for symbol, qty in holdings:
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "5m", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            prev = float(result["meta"].get("chartPreviousClose") or result["meta"]["previousClose"])
            # A Series is a single column of values with an index — here the
            # index is the time of each 5-minute bar.
            s = pd.Series(
                result["indicators"]["quote"][0]["close"],
                index=pd.to_datetime(result["timestamp"], unit="s", utc=True),
                dtype="float64",
            )
        except Exception:
            continue  # skip a stock Yahoo can't find rather than breaking the chart
        closes[symbol] = (s * qty, prev * qty)
        base += prev * qty

    if not closes or base == 0:
        return pd.DataFrame(columns=["time", "pct"])

    # Put every stock side by side, one column each, lined up by time.
    # If a stock has a gap in its data, ffill() ("forward fill") reuses its
    # last known price, and before its first bar we use yesterday's close.
    # Without this, a missing bar would look like the stock dropped to zero.
    frame = pd.DataFrame({sym: v for sym, (v, _) in closes.items()}).sort_index().ffill()
    for sym, (_, prev_val) in closes.items():
        frame[sym] = frame[sym].fillna(prev_val)
    pct = (frame.sum(axis=1) / base - 1) * 100
    return pct.rename("pct").rename_axis("time").reset_index()


@st.cache_data(ttl=900)
def get_congressional_activity_for_tickers(tickers: list[str], lookback_days: int = 30) -> pd.DataFrame:
    """
    Any congressional trades in the stocks I hold, from the last `lookback_days` days.
    This is what powers the 🏛️ flag next to a holding.
    """
    if not tickers:
        return pd.DataFrame()
    # Build one "%s" placeholder per ticker: "%s, %s, %s" — then pass the
    # tickers as params so they're inserted safely.
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
    """Daily price history for SPY, QQQ or ^VIX — for the market chart."""
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
    """The newest saved price for every symbol (one row each)."""
    return query("""
        SELECT DISTINCT ON (symbol) symbol, price, change_pct, fetched_at, source
        FROM market_context
        ORDER BY symbol, fetched_at DESC
    """)


# ── News ──────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=900)
def get_general_news(limit: int = 30) -> pd.DataFrame:
    """General market headlines (the ones saved with no ticker)."""
    return query(f"""
        SELECT headline, summary, url, source_name, published_at
        FROM news
        WHERE ticker IS NULL
        ORDER BY published_at DESC
        LIMIT {limit}
    """)


@st.cache_data(ttl=900)
def get_ticker_news(ticker: str, limit: int = 10) -> pd.DataFrame:
    """Headlines about one specific stock I hold."""
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
