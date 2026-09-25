"""
Congressional Trading Tracker — overview dashboard.

Layout:
  - Top: 4 KPI metrics across the full width
  - Row 1: Congressional Trades (left) | Anomalies (right)
  - Row 2: Market Pulse (left) | Portfolio (right)
  - Row 3: News (full width, collapsed by default)

Each section is an expander — collapsed by default so the whole
dashboard fits on one screen. Click any section to expand it.
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
import db_queries as db

st.set_page_config(
    page_title="Congressional Trading Tracker",
    page_icon="📊",
    layout="wide",
)

# ── Sidebar controls ──────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📊 Congressional Trading Tracker")
    st.divider()
    days_filter = st.slider("Trades window (days)",                    7, 365, 90)
    lookback    = st.slider("Congressional activity lookback (days)",  7,  90, 30)
    chart_days  = st.slider("Market chart history (days)",             7, 365, 90)
    st.divider()
    st.caption("House trades · Finnhub · Trading 212")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Load all data upfront so KPIs render immediately ─────────────────────────
with st.spinner("Loading…"):
    recent       = db.get_recent_trades(days=days_filter, limit=5000)
    top_tickers  = db.get_top_tickers(limit=20)
    anomalies_df = db.get_anomalies()
    snapshot     = db.get_latest_market_snapshot()
    portfolio    = db.get_portfolio()
    news_df      = db.get_general_news(limit=30)

held_tickers = portfolio["ticker"].dropna().tolist() if not portfolio.empty else []
congress_activity = (
    db.get_congressional_activity_for_tickers(held_tickers, lookback_days=lookback)
    if held_tickers else pd.DataFrame()
)
activity_counts = (
    congress_activity.groupby("ticker").size().to_dict()
    if not congress_activity.empty else {}
)

# ── Global KPI bar ────────────────────────────────────────────────────────────
st.markdown("## Overview")
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Trades",           f"{len(recent):,}")
k2.metric("Members",          f"{recent['member_name'].nunique():,}" if not recent.empty else "—")
k3.metric("Tickers",          f"{recent['ticker'].nunique():,}"      if not recent.empty else "—")
k4.metric("Anomalies",        f"{len(anomalies_df):,}")
k5.metric("Portfolio size",   f"{len(portfolio)}" if not portfolio.empty else "—")

# VIX inline in KPI bar
if not snapshot.empty and "^VIX" in snapshot.set_index("symbol").index:
    vix_row = snapshot.set_index("symbol").loc["^VIX"]
    chg = vix_row["change_pct"]
    k6.metric("VIX", f"{vix_row['price']:.2f}", f"{chg:+.2f}%" if chg is not None else None)
else:
    k6.metric("VIX", "—")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ROW 1 — Portfolio | Market Pulse
# ══════════════════════════════════════════════════════════════════════════════
row1_left, row1_right = st.columns(2)

with row1_left:
    with st.expander("💼 Portfolio", expanded=True):
        st.caption("Trading 212 · read-only snapshot")
        if portfolio.empty:
            st.info("No portfolio data yet — T212 API key needed.")
        else:
            total_pnl = portfolio["pnl"].sum() if "pnl" in portfolio.columns else 0
            flagged   = sum(1 for t in held_tickers if activity_counts.get(t, 0) > 0)
            pa, pb, pc = st.columns(3)
            pa.metric("Positions", len(portfolio))
            pb.metric("Total P&L", f"£{total_pnl:,.2f}" if total_pnl else "—")
            pc.metric("Congress-flagged", f"{flagged}/{len(portfolio)}")
            st.divider()
            for pi, pos in portfolio.iterrows():
                ticker  = pos["ticker"]
                name    = pos.get("instrument_name") or ticker
                pnl     = pos.get("pnl")
                pnl_str = f"£{pnl:+,.2f}" if pnl is not None else "—"
                count   = activity_counts.get(ticker, 0)
                flag    = " 🏛️" if count > 0 else ""
                label   = f"{ticker}{flag} — {name}  |  {pnl_str}"
                if st.checkbox(label, key=f"pos_{pi}"):
                    d1, d2, d3 = st.columns(3)
                    d1.metric("Qty",   f"{pos.get('quantity'):,.4f}" if pos.get("quantity") else "—")
                    d2.metric("Avg",   f"£{pos.get('avg_price'):,.4f}" if pos.get("avg_price") else "—")
                    d3.metric("Price", f"£{pos.get('current_price'):,.4f}" if pos.get("current_price") else "—")
                    if count > 0:
                        st.markdown(f"*{count} congressional trade(s) in last {lookback} days*")
                        tc = congress_activity[congress_activity["ticker"] == ticker]
                        st.dataframe(
                            tc[["member_name","transaction_type","amount_range","transaction_date"]].rename(
                                columns={"member_name":"Member","transaction_type":"Type",
                                         "amount_range":"Amount","transaction_date":"Date"}
                            ), use_container_width=True, hide_index=True,
                        )
                    ticker_news = db.get_ticker_news(ticker, limit=3)
                    if not ticker_news.empty:
                        for _, a in ticker_news.iterrows():
                            pub = a.get("published_at")
                            pub_str = pub.strftime("%d %b") if pub is not None and hasattr(pub, "strftime") else ""
                            url = a.get("url","")
                            if url:
                                st.markdown(f"- [{a['headline']}]({url}) *{pub_str}*")
                            else:
                                st.markdown(f"- {a['headline']} *{pub_str}*")

with row1_right:
    with st.expander("🌍 Market Pulse", expanded=True):
        st.caption("SPY & QQQ · Finnhub  |  VIX · Yahoo Finance")

        if not snapshot.empty:
            snap = snapshot.set_index("symbol")
            mc1, mc2, mc3 = st.columns(3)
            for col, sym, label in [(mc1, "SPY", "S&P 500"), (mc2, "QQQ", "Nasdaq"), (mc3, "^VIX", "VIX")]:
                if sym in snap.index:
                    r = snap.loc[sym]
                    chg = r["change_pct"]
                    col.metric(label, f"{r['price']:.2f}", f"{chg:+.2f}%" if chg is not None else None)
                else:
                    col.metric(label, "—")

        chart_sym = st.radio("Chart", ["SPY", "QQQ", "^VIX"], horizontal=True, key="mkt_sym")
        hist = db.get_market_context_history(chart_sym, days=chart_days)
        if hist.empty:
            st.caption("No history yet — run backfill script or wait for daily runs.")
        else:
            st.line_chart(hist.sort_values("fetched_at").set_index("fetched_at")["price"], height=200)

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ROW 2 — Congressional Trades | Anomalies
# ══════════════════════════════════════════════════════════════════════════════
row2_left, row2_right = st.columns([3, 2])

with row2_left:
    with st.expander("🏛️ Congressional Trades", expanded=True):
        st.caption("House-only · insiderwatch-data · STOCK Act disclosures")

        tab_chart, tab_volume, tab_table = st.tabs(["Top Tickers", "Weekly Volume", "Trade Table"])

        with tab_chart:
            if top_tickers.empty:
                st.info("No ticker data yet — run the pipeline first.")
            else:
                st.bar_chart(
                    top_tickers.set_index("ticker")[["buys", "sells"]],
                    color=["#4F8EF7", "#F06292"],
                    height=280,
                )

        with tab_volume:
            ticker_opts = ["All tickers"] + (top_tickers["ticker"].dropna().tolist() if not top_tickers.empty else [])
            sel = st.selectbox("Ticker", ticker_opts, key="vol_sel")
            if sel == "All tickers":
                vol_df = db.get_weekly_volume()
                if not vol_df.empty:
                    top10 = vol_df.groupby("ticker")["trade_count"].sum().nlargest(10).index
                    pivot = vol_df.pivot_table(index="week_start", columns="ticker", values="trade_count", aggfunc="sum").fillna(0)
                    st.line_chart(pivot[pivot.columns.intersection(top10)], height=260)
            else:
                vol_df = db.get_weekly_volume(ticker=sel)
                if not vol_df.empty:
                    st.line_chart(vol_df.set_index("week_start")["trade_count"], height=260)

        with tab_table:
            if recent.empty:
                st.info("No trades in this window.")
            else:
                st.dataframe(
                    recent.rename(columns={
                        "member_name": "Member", "ticker": "Ticker",
                        "transaction_type": "Type", "amount_range": "Amount",
                        "transaction_date": "Date",
                    })[["Member", "Ticker", "Type", "Amount", "Date"]],
                    use_container_width=True,
                    hide_index=True,
                    height=280,
                )

with row2_right:
    with st.expander("🚨 Anomalies", expanded=True):
        st.caption(f"{len(anomalies_df)} flagged — volume spikes & pre-move clusters")
        if anomalies_df.empty:
            st.success("No anomalies detected.")
        else:
            for i, row in anomalies_df.head(15).iterrows():
                label = (
                    f"**{row['anomaly_type'].replace('_',' ').title()}** · "
                    f"`{row['ticker']}` · {row['window_start']} → {row['window_end']} · "
                    f"{row['trade_count']} trades"
                )
                if st.checkbox(label, key=f"anomaly_{i}"):
                    detail = row["detail"]
                    if isinstance(detail, str):
                        try:
                            detail = json.loads(detail)
                        except Exception:
                            pass
                    st.json(detail)

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ROW 3 — News (full width, collapsed by default)
# ══════════════════════════════════════════════════════════════════════════════
with st.expander(f"📰 Market News ({len(news_df)} headlines)", expanded=False):
    st.caption("General market headlines · Finnhub")
    if news_df.empty:
        st.info("No news stored yet.")
    else:
        for ni, article in news_df.iterrows():
            headline = article.get("headline", "")
            url      = article.get("url", "")
            source   = article.get("source_name", "")
            pub      = article.get("published_at")
            pub_str  = pub.strftime("%d %b %Y, %H:%M") if pub is not None and hasattr(pub, "strftime") else str(pub or "")
            summary  = article.get("summary", "")
            st.markdown(f"**{headline}** · *{source}*, {pub_str}")
            if summary:
                st.caption(summary[:200] + ("…" if len(summary) > 200 else ""))
            if url:
                st.markdown(f"[Read full article ↗]({url})")
            st.divider()
