"""
Congressional Trading Tracker — the dashboard (the part you look at).

Start it with:  cd dashboard && streamlit run app.py
Then open http://localhost:8501 in your browser.

How Streamlit works, in one paragraph:
  Streamlit runs this file from top to bottom and turns each st.something()
  call into a piece of the web page. Every time you click a button or move a
  slider, it runs the WHOLE file again from the top with the new values.
  That's why all the data loading lives in db_queries.py behind a cache —
  otherwise every click would hit the database again.

Layout of the page (top to bottom):
  Sidebar  — sliders for how many days of data to show
  KPI row  — seven headline numbers (trades, members, portfolio value, VIX...)
  Row 1    — left:  my holdings + market news
             right: market chart, P&L tiles, and today's portfolio chart
  Row 2    — left:  congressional trades (tabs)   right: flagged anomalies

A lot of this file is HTML/CSS inside Python strings. Streamlit's built-in
widgets are quite plain, so st.markdown(..., unsafe_allow_html=True) lets us
draw custom-styled pieces of the page ourselves.
"""

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Make sure Python can find db_queries.py, which sits next to this file.
sys.path.insert(0, str(Path(__file__).parent))
import db_queries as db

# Trading 212 still lists some of my stocks under old tickers (e.g. Grab as
# "AGC", from before it merged with a SPAC). This maps the old ticker to the
# real company name and its current ticker (used to fetch the right logo and
# live prices). The same table lives in ingestion/portfolio.py.
_TICKER_DISPLAY: dict[str, tuple[str, str]] = {
    "FB":   ("Meta Platforms Inc",  "META"),
    "YNDX": ("Nebius Group N.V.",   "NBIS"),
    "TWTR": ("X Corp",              "X"),
    "AGC":  ("Grab Holdings Ltd",   "GRAB"),
    "NPA":  ("AST SpaceMobile Inc", "ASTS"),
    "OAC":  ("Hims & Hers Health",  "HIMS"),
}


def _display(pos) -> tuple[str, str]:
    """(company name, real market symbol) for a T212 position row."""
    ticker = pos["ticker"] or ""
    override = _TICKER_DISPLAY.get(ticker.upper())
    if override:
        return override
    name = pos.get("instrument_name")
    if not name or str(name) == "nan":
        name = ticker
    return name, ticker

# Browser tab title/icon and use the full width of the screen.
st.set_page_config(
    page_title="Congressional Trading Tracker",
    page_icon="📊",
    layout="wide",
)

# ── Styling ───────────────────────────────────────────────────────────────────
# All the custom CSS for the page, injected once. The class names defined here
# (kpi-card, alloc-pill, score-tile...) are used by the HTML snippets further down.
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  #MainMenu, footer, header { visibility: hidden; }
  .block-container { padding: 1.2rem 1.5rem 1rem !important; max-width: 100% !important; }

  section[data-testid="stSidebar"] {
    background: #080C14 !important;
    border-right: 1px solid #1A2535 !important;
  }
  section[data-testid="stSidebar"] .stSlider label,
  section[data-testid="stSidebar"] p,
  section[data-testid="stSidebar"] small { color: #64748B !important; font-size: 12px !important; }

  /* KPI cards */
  .kpi-card {
    background: #0D1826;
    border: 1px solid #1A2535;
    border-radius: 12px;
    padding: 14px 18px 12px;
  }
  .kpi-label { font-size: 10px; font-weight: 600; color: #475569; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4px; }
  .kpi-value { font-size: 26px; font-weight: 700; color: #E2E8F0; line-height: 1.1; }
  .kpi-sub   { font-size: 11px; color: #22C55E; margin-top: 3px; }
  .kpi-sub.neg { color: #EF4444; }

  /* Section headers */
  .card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .card-title  { font-size: 11px; font-weight: 600; color: #475569; text-transform: uppercase; letter-spacing: 0.08em; }
  .card-badge  { font-size: 10px; color: #334155; background: #0A1520; border: 1px solid #1A2535; border-radius: 5px; padding: 2px 7px; }

  /* Allocation pills */
  .alloc-pill { display: inline-block; border-radius: 5px; padding: 2px 8px; font-size: 10px; font-weight: 700; letter-spacing: 0.04em; margin: 2px 2px 2px 0; }

  /* Holding rows */
  .h-row { display: flex; align-items: center; gap: 10px; width: 100%; }
  .h-ticker { font-size: 13px; font-weight: 700; color: #E2E8F0; min-width: 52px; }
  .h-name   { font-size: 11px; color: #475569; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pos { color: #22C55E; }
  .neg { color: #EF4444; }

  /* Detail expander — sits flush under the HTML row, label is tiny + muted */
  div[data-testid="stExpander"] {
    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    padding: 0 !important;
    margin: 0 0 0 0 !important;
  }
  div[data-testid="stExpander"] summary {
    padding: 3px 4px 3px 6px !important;
    background: transparent !important;
    min-height: 0 !important;
  }
  div[data-testid="stExpander"] summary:hover { background: #0A1520 !important; border-radius: 4px; }
  /* Make the "details" label text tiny and muted so it reads as a subtle toggle */
  div[data-testid="stExpander"] summary p,
  div[data-testid="stExpander"] summary span {
    font-size: 10px !important;
    color: #334155 !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  div[data-testid="stExpander"] summary svg { color: #334155 !important; width: 12px !important; height: 12px !important; }
  div[data-testid="stExpander"] > div[data-testid="stExpanderDetails"] {
    background: #0A1520;
    border-radius: 0 0 8px 8px;
    border: 1px solid #1A2535;
    border-top: none;
    padding: 10px 12px 12px !important;
    margin-bottom: 4px;
  }

  /* Gradient score tiles (portfolio movers) */
  .score-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 4px; }
  .score-tile {
    border-radius: 10px;
    padding: 10px 8px 8px;
    text-align: center;
    cursor: default;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
  }
  .score-tile .st-logo { width: 28px; height: 28px; border-radius: 6px; object-fit: contain; background: rgba(0,0,0,0.25); }
  .score-tile .st-val  { font-size: 13px; font-weight: 700; }

  /* Anomaly rows */
  .anomaly-row { padding: 8px 10px; border-radius: 8px; background: #0A1520; border: 1px solid #1A2535; margin-bottom: 6px; }
  .anomaly-ticker { font-size: 12px; font-weight: 700; color: #00D4FF; }
  .anomaly-type   { font-size: 10px; color: #64748B; text-transform: uppercase; letter-spacing: 0.06em; }
  .anomaly-meta   { font-size: 11px; color: #475569; }

  /* News rows */
  .news-row { padding: 7px 0; border-bottom: 1px solid #131F2E; }
  .news-headline { font-size: 12px; font-weight: 500; color: #CBD5E1; line-height: 1.4; }
  .news-meta     { font-size: 10px; color: #334155; margin-top: 2px; }

  /* Scrollable */
  .scroll-inner { overflow-y: auto; max-height: 320px; padding-right: 4px; }
  .scroll-inner::-webkit-scrollbar { width: 3px; }
  .scroll-inner::-webkit-scrollbar-track { background: transparent; }
  .scroll-inner::-webkit-scrollbar-thumb { background: #1A2535; border-radius: 3px; }

  hr { border-color: #1A2535 !important; margin: 1rem 0 !important; }

  div[data-testid="metric-container"] { background: transparent !important; border: none !important; padding: 0 !important; }

  button[data-baseweb="tab"] {
    font-size: 11px !important; font-weight: 600 !important;
    text-transform: uppercase; letter-spacing: 0.07em;
    color: #475569 !important; padding: 6px 12px !important;
  }
  button[data-baseweb="tab"][aria-selected="true"] { color: #E2E8F0 !important; }

  div[data-testid="stRadio"] label { font-size: 11px !important; color: #475569 !important; }
  div[data-testid="stRadio"] label[data-checked="true"] { color: #00D4FF !important; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
# Anything inside "with st.sidebar:" appears in the collapsible left-hand panel.
# Each slider returns its current value, which we use when loading data below.
with st.sidebar:
    st.markdown(
        "<div style='font-size:15px;font-weight:700;color:#E2E8F0;padding:4px 0 12px'>📊 CongressTracker</div>",
        unsafe_allow_html=True,
    )
    st.divider()
    days_filter = st.slider("Trades window (days)",          7, 365, 90)
    lookback    = st.slider("Congressional lookback (days)", 7,  90, 30)
    chart_days  = st.slider("Market chart history (days)",   7, 365, 90)
    st.divider()
    st.caption("House disclosures · Finnhub · Trading 212")
    if st.button("↺  Refresh", use_container_width=True):
        # Throw away the cached query results and redraw, forcing fresh data.
        st.cache_data.clear()
        st.rerun()

# ── Load data ─────────────────────────────────────────────────────────────────
# Fetch everything the page needs up front. Each call is cached in db_queries.py.
with st.spinner(""):
    recent         = db.get_recent_trades(days=days_filter, limit=5000)
    top_tickers    = db.get_top_tickers(limit=20)
    anomalies_df   = db.get_anomalies()
    snapshot       = db.get_latest_market_snapshot()
    portfolio      = db.get_portfolio()
    portfolio_tots = db.get_portfolio_totals()
    news_df        = db.get_general_news(limit=18)

# Cross-reference: which of MY stocks have members of Congress traded recently?
# activity_counts ends up like {"NVDA": 3, "MSFT": 1} and drives the 🏛️ flags.
held_tickers = portfolio["ticker"].dropna().tolist() if not portfolio.empty else []
congress_activity = (
    db.get_congressional_activity_for_tickers(held_tickers, lookback_days=lookback)
    if held_tickers else pd.DataFrame()
)
activity_counts = (
    congress_activity.groupby("ticker").size().to_dict()
    if not congress_activity.empty else {}
)

port_total = portfolio_tots.get("PORTFOLIO_TOTAL")
port_ppl   = portfolio_tots.get("PORTFOLIO_PPL")
# Index the latest prices by symbol so we can look them up like snap_idx.loc["SPY"].
snap_idx   = snapshot.set_index("symbol") if not snapshot.empty else pd.DataFrame()

# ── KPI row ───────────────────────────────────────────────────────────────────
# "KPI" = key performance indicator: the big headline numbers across the top.

def _kpi(label, value, sub=None, sub_neg=False):
    """Build the HTML for one KPI card. `sub` is the small line underneath (red if sub_neg)."""
    sub_cls  = "neg" if sub_neg else ""
    sub_html = f"<div class='kpi-sub {sub_cls}'>{sub}</div>" if sub else ""
    return f"<div class='kpi-card'><div class='kpi-label'>{label}</div><div class='kpi-value'>{value}</div>{sub_html}</div>"

vix_val, vix_sub, vix_neg = "—", None, False
if "^VIX" in snap_idx.index:
    vr  = snap_idx.loc["^VIX"]
    chg = vr["change_pct"]
    vix_val = f"{float(vr['price']):.2f}"
    if chg is not None:
        vix_sub = f"{'▲' if float(chg) >= 0 else '▼'} {abs(float(chg)):.2f}%"
        vix_neg = float(chg) < 0

port_val        = f"£{float(port_total):,.0f}" if port_total else "—"
ppl_val, ppl_neg = "—", False
if port_ppl is not None:
    pf      = float(port_ppl)
    ppl_val = f"{'▲' if pf >= 0 else '▼'} £{abs(pf):,.0f}"
    ppl_neg = pf < 0

# Seven equal-width columns, one card in each.
k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
k1.markdown(_kpi("Trades",    f"{len(recent):,}"), unsafe_allow_html=True)
k2.markdown(_kpi("Members",   f"{recent['member_name'].nunique():,}" if not recent.empty else "—"), unsafe_allow_html=True)
k3.markdown(_kpi("Tickers",   f"{recent['ticker'].nunique():,}"      if not recent.empty else "—"), unsafe_allow_html=True)
k4.markdown(_kpi("Anomalies", f"{len(anomalies_df):,}"), unsafe_allow_html=True)
k5.markdown(_kpi("Portfolio", port_val), unsafe_allow_html=True)
k6.markdown(_kpi("P&L", ppl_val, sub_neg=ppl_neg), unsafe_allow_html=True)
k7.markdown(_kpi("VIX", vix_val, vix_sub, vix_neg), unsafe_allow_html=True)

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# ── Row 1: Portfolio + news (left) | Market Pulse, P&L tiles, Today chart (right)
# [5, 4] = the left column gets 5 parts of the width, the right gets 4.
r1a, r1b = st.columns([5, 4])

# ── LEFT: Portfolio + news feed ───────────────────────────────────────────────
with r1a:
    flagged = sum(1 for t in held_tickers if activity_counts.get(t, 0) > 0)
    st.markdown(
        f"<div class='card-header'>"
        f"<span class='card-title'>💼 Portfolio</span>"
        f"<span class='card-badge'>{len(portfolio)} positions · {flagged} congress-flagged</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    if portfolio.empty:
        st.info("No portfolio data — check T212 API key.")
    else:
        # Allocation pills — one coloured tag per holding showing its share of
        # the portfolio, e.g. "Meta Platforms Inc 29.0%". Value = price x shares.
        if "current_price" in portfolio.columns and "quantity" in portfolio.columns:
            pc = portfolio.copy()
            pc["value"] = pc["current_price"].astype(float) * pc["quantity"].astype(float)
            pc = pc[pc["value"] > 0].sort_values("value", ascending=False)
            pc["name"] = [_display(r)[0] for _, r in pc.iterrows()]
            if not pc.empty:
                total_v = pc["value"].sum()
                colors  = ["#00D4FF","#7C3AED","#059669","#D97706","#DC2626","#0891B2","#65A30D","#9333EA"]
                pills   = "".join(
                    f"<span class='alloc-pill' style='background:{colors[i%8]}18;border:1px solid {colors[i%8]}44;color:{colors[i%8]}'>"
                    f"{row['name']} {row['value']/total_v*100:.1f}%</span>"
                    for i, (_, row) in enumerate(pc.iterrows())
                )
                st.markdown(f"<div style='margin-bottom:10px'>{pills}</div>", unsafe_allow_html=True)

        # Holdings list — one row per stock: logo, company name, 🏛️ flag, P&L.
        # Under each row is a small "details" dropdown (st.expander) showing the
        # share count, prices, any congressional trades and recent news.
        # Why two pieces? st.expander's label can only be plain text, so the
        # nice-looking row is drawn with HTML just above it, and the expander's
        # own label is shrunk to a tiny "DETAILS" with CSS.
        for pi, pos in portfolio.iterrows():
            ticker = pos["ticker"]
            display_label, logo_ticker = _display(pos)
            pnl     = pos.get("pnl")
            pnl_pos = pnl is not None and float(pnl) >= 0
            pnl_str = f"{'▲' if pnl_pos else '▼'} £{abs(float(pnl)):,.2f}" if pnl is not None else "—"
            pnl_hex = "#22C55E" if pnl_pos else "#EF4444"
            count   = activity_counts.get(ticker, 0)
            flag    = " 🏛️" if count > 0 else ""
            logo    = f"https://financialmodelingprep.com/image-stock/{logo_ticker}.png"

            # Visual row — logo · company name · P&L
            st.markdown(
                f"<div class='h-row' style='border-bottom:1px solid #131F2E;padding:7px 2px 4px'>"
                f"<img src='{logo}' width='22' height='22' style='border-radius:4px;object-fit:contain;background:#131F2E;flex-shrink:0'>"
                f"<span style='font-size:13px;font-weight:600;color:#E2E8F0;min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
                f"{display_label}{flag}</span>"
                f"<span style='font-size:13px;font-weight:600;color:{pnl_hex};white-space:nowrap;margin-left:12px;padding-right:4px'>{pnl_str}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # Detail panel — plain-text expander label, visually flush under the row
            with st.expander("details", expanded=False):
                d1, d2, d3 = st.columns(3)
                d1.metric("Qty",   f"{pos.get('quantity'):,.2f}"       if pos.get("quantity")      else "—")
                d2.metric("Avg",   f"£{pos.get('avg_price'):,.2f}"     if pos.get("avg_price")     else "—")
                d3.metric("Price", f"£{pos.get('current_price'):,.2f}" if pos.get("current_price") else "—")
                if count > 0:
                    st.markdown(
                        f"<div style='font-size:12px;color:#00D4FF;margin:6px 0 4px'>🏛️ {count} congressional trade(s) in last {lookback} days</div>",
                        unsafe_allow_html=True,
                    )
                    tc = congress_activity[congress_activity["ticker"] == ticker]
                    st.dataframe(
                        tc[["member_name","transaction_type","amount_range","transaction_date"]].rename(
                            columns={"member_name":"Member","transaction_type":"Type",
                                     "amount_range":"Amount","transaction_date":"Date"}
                        ), use_container_width=True, hide_index=True,
                    )
                tn = db.get_ticker_news(ticker, limit=3)
                if not tn.empty:
                    for _, a in tn.iterrows():
                        pub = a.get("published_at")
                        ps  = pub.strftime("%d %b") if pub is not None and hasattr(pub, "strftime") else ""
                        url = a.get("url", "")
                        st.markdown(
                            f"<div style='font-size:12px;color:#94A3B8;padding:3px 0'>"
                            f"{'<a href=\"'+url+'\" target=\"_blank\" style=\"color:#00D4FF\">'+a['headline']+'</a>' if url else a['headline']}"
                            f" <span style='color:#475569'>· {ps}</span></div>",
                            unsafe_allow_html=True,
                        )

    # News feed — fills the space below holdings
    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='card-header'>"
        f"<span class='card-title'>📰 Market News</span>"
        f"<span class='card-badge'>{len(news_df)} headlines</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if news_df.empty:
        st.caption("No news yet.")
    else:
        news_html = "<div class='scroll-inner' style='max-height:240px'>"
        for _, a in news_df.iterrows():
            headline = a.get("headline", "")
            url      = a.get("url", "")
            source   = a.get("source_name", "")
            pub      = a.get("published_at")
            pub_str  = pub.strftime("%d %b, %H:%M") if pub is not None and hasattr(pub, "strftime") else ""
            hl = (
                f"<a href='{url}' target='_blank' style='color:#CBD5E1;text-decoration:none'>{headline}</a>"
                if url else f"<span style='color:#CBD5E1'>{headline}</span>"
            )
            news_html += (
                f"<div class='news-row'>"
                f"<div class='news-headline'>{hl}</div>"
                f"<div class='news-meta'>{source} · {pub_str}</div>"
                f"</div>"
            )
        news_html += "</div>"
        st.markdown(news_html, unsafe_allow_html=True)

# ── RIGHT: Market Pulse, P&L tiles, Today chart ───────────────────────────────
with r1b:
    st.markdown(
        "<div class='card-header'>"
        "<span class='card-title'>🌍 Market Pulse</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    if not snap_idx.empty:
        mc1, mc2, mc3 = st.columns(3)
        for col_obj, sym, label in [(mc1,"SPY","S&P 500"),(mc2,"QQQ","Nasdaq"),(mc3,"^VIX","VIX")]:
            if sym in snap_idx.index:
                r     = snap_idx.loc[sym]
                chg   = r["change_pct"]
                chg_f = float(chg) if chg is not None else None
                neg   = chg_f is not None and chg_f < 0
                sub   = f"{'▲' if not neg else '▼'} {abs(chg_f):.2f}%" if chg_f is not None else None
                col_obj.markdown(_kpi(label, f"{float(r['price']):.2f}", sub, neg), unsafe_allow_html=True)
            else:
                col_obj.markdown(_kpi(label, "—"), unsafe_allow_html=True)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    # Toggle between S&P 500, Nasdaq and VIX for the chart below.
    chart_sym = st.radio("", ["SPY","QQQ","^VIX"], horizontal=True, key="mkt_sym", label_visibility="collapsed")
    hist = db.get_market_context_history(chart_sym, days=chart_days)
    if hist.empty:
        st.caption("No history yet — run the backfill script.")
    else:
        st.line_chart(hist.sort_values("fetched_at").set_index("fetched_at")["price"], height=200)

    # P&L tiles — one tile per holding with its logo and total profit/loss,
    # green background if I'm up on it, red if I'm down.
    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='card-header'>"
        "<span class='card-title'>📊 Holdings P&amp;L</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    if not portfolio.empty and "pnl" in portfolio.columns:
        tiles_html = "<div class='score-grid'>"
        for _, pos in portfolio.iterrows():
            pnl    = pos.get("pnl")
            if pnl is None:
                continue
            tile_name, logo_tick = _display(pos)
            logo_url  = f"https://financialmodelingprep.com/image-stock/{logo_tick}.png"
            pnl_f     = float(pnl)
            pnl_pos   = pnl_f >= 0
            if pnl_pos:
                bg, border, val_col = "linear-gradient(135deg,#0D2318,#0D2B1E)", "#1A4D2E", "#22C55E"
            else:
                bg, border, val_col = "linear-gradient(135deg,#2B0D0D,#1E0D0D)", "#4D1A1A", "#EF4444"
            sign = "+" if pnl_pos else ""
            tiles_html += (
                f"<div class='score-tile' style='background:{bg};border:1px solid {border}'>"
                f"<img class='st-logo' src='{logo_url}' alt='{tile_name}' title='{tile_name}'>"
                f"<div class='st-val' style='color:{val_col}'>{sign}£{abs(pnl_f):,.0f}</div>"
                f"</div>"
            )
        tiles_html += "</div>"
        st.markdown(tiles_html, unsafe_allow_html=True)
    else:
        st.caption("No portfolio data.")

    # Today chart — how my whole portfolio has moved since yesterday's close,
    # in 5-minute steps. The line is green if I'm up on the day, red if down.
    # The prices come live from Yahoo (see get_portfolio_intraday in db_queries.py).
    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    holdings = tuple(
        (_display(pos)[1], float(pos["quantity"]))
        for _, pos in portfolio.iterrows()
        if pos.get("quantity")
    ) if not portfolio.empty else ()
    intraday = db.get_portfolio_intraday(holdings) if holdings else pd.DataFrame()

    if intraday.empty or len(intraday) < 2:
        st.markdown(
            "<div class='card-header'><span class='card-title'>📈 Today</span></div>",
            unsafe_allow_html=True,
        )
        st.caption("No intraday prices available right now.")
    else:
        last_pct = float(intraday["pct"].iloc[-1])
        up       = last_pct >= 0
        colour   = "#22C55E" if up else "#EF4444"
        # The chart works in %, but £ is easier to feel. Work backwards from my
        # current account value: if I'm worth £X now after moving p%, I was worth
        # X / (1 + p/100) at the open, and the difference is the £ move.
        # (Approximate — the account total also includes uninvested cash.)
        gbp_move = None
        if port_total:
            gbp_move = float(port_total) - float(port_total) / (1 + last_pct / 100)
            intraday["gbp"] = intraday["pct"] / 100 * (float(port_total) - gbp_move)
        session_day = intraday["time"].iloc[-1].tz_convert("Europe/London").date()
        title = "Today" if session_day == pd.Timestamp.now(tz="Europe/London").date() \
            else f"Last session · {session_day:%a %d %b}"
        badge = f"{'+' if up else '−'}{abs(last_pct):.2f}%"
        if gbp_move is not None:
            badge = f"{'+' if up else '−'}£{abs(gbp_move):,.0f} · {badge}"
        st.markdown(
            f"<div class='card-header'>"
            f"<span class='card-title'>📈 {title}</span>"
            f"<span class='card-badge' style='color:{colour};border-color:{colour}55'>{badge}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Build the chart with Altair (the charting library Streamlit uses).
        # It's three layers stacked on top of each other:
        #   area = soft coloured fill under the line
        #   zero = dashed line at 0% so you can see "up" vs "down" at a glance
        #   line = the actual portfolio line (hover it for the tooltip)
        tooltip = [alt.Tooltip("time:T", title="Time", format="%H:%M"),
                   alt.Tooltip("pct:Q", title="Change %", format="+.2f")]
        if "gbp" in intraday:
            tooltip.append(alt.Tooltip("gbp:Q", title="Change £", format="+,.0f"))
        base = alt.Chart(intraday).encode(
            x=alt.X("time:T", axis=alt.Axis(title=None, format="%H:%M", grid=False, labelColor="#475569")),
            y=alt.Y("pct:Q", axis=alt.Axis(title=None, format="+.1f", labelColor="#475569",
                                          gridColor="#131F2E", tickCount=4)),
        )
        # Fill fades toward the zero line from whichever side the day is on
        area = base.mark_area(
            color=alt.Gradient(
                gradient="linear", x1=1, x2=1, y1=1 if up else 0, y2=0 if up else 1,
                stops=[alt.GradientStop(color=f"{colour}00", offset=0),
                       alt.GradientStop(color=f"{colour}55", offset=1)],
            ),
        )
        line  = base.mark_line(color=colour, strokeWidth=2).encode(tooltip=tooltip)
        zero  = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#334155", strokeDash=[4, 4]).encode(y="y:Q")
        chart = (area + zero + line).properties(height=230).configure_view(strokeWidth=0)
        st.altair_chart(chart, use_container_width=True)

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

# ── Row 2: Congressional Trades + Anomalies ───────────────────────────────────
r2a, r2b = st.columns([3, 2])

# LEFT: three tabs of congressional trade data

with r2a:
    st.markdown(
        "<div class='card-header'>"
        "<span class='card-title'>🏛️ Congressional Trades</span>"
        "<span class='card-badge'>House · STOCK Act disclosures</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    t1, t2, t3 = st.tabs(["Top Tickers", "Weekly Volume", "Trade Table"])

    with t1:
        if top_tickers.empty:
            st.caption("No data yet.")
        else:
            st.bar_chart(top_tickers.set_index("ticker")[["buys","sells"]], color=["#00D4FF","#7C3AED"], height=300)

    with t2:
        opts = ["All tickers"] + (top_tickers["ticker"].dropna().tolist() if not top_tickers.empty else [])
        sel  = st.selectbox("", opts, key="vol_sel", label_visibility="collapsed")
        if sel == "All tickers":
            vdf = db.get_weekly_volume()
            if not vdf.empty:
                # Too many tickers to chart at once, so only show the 10 busiest.
                # pivot_table reshapes the data to one column per ticker, one row per week.
                top10 = vdf.groupby("ticker")["trade_count"].sum().nlargest(10).index
                pv    = vdf.pivot_table(index="week_start",columns="ticker",values="trade_count",aggfunc="sum").fillna(0)
                st.line_chart(pv[pv.columns.intersection(top10)], height=300)
        else:
            vdf = db.get_weekly_volume(ticker=sel)
            if not vdf.empty:
                st.line_chart(vdf.set_index("week_start")["trade_count"], height=300)

    with t3:
        if recent.empty:
            st.caption("No trades in this window.")
        else:
            st.dataframe(
                recent.rename(columns={"member_name":"Member","ticker":"Ticker",
                                       "transaction_type":"Type","amount_range":"Amount",
                                       "transaction_date":"Date"})[["Member","Ticker","Type","Amount","Date"]],
                use_container_width=True, hide_index=True, height=300,
            )

# RIGHT: the unusual patterns flagged by anomaly/anomaly_detection.py
with r2b:
    st.markdown(
        f"<div class='card-header'>"
        f"<span class='card-title'>🚨 Anomalies</span>"
        f"<span class='card-badge'>{len(anomalies_df)} flagged</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if anomalies_df.empty:
        st.caption("No anomalies detected.")
    else:
        anomaly_html = "<div class='scroll-inner' style='max-height:340px'>"
        for _, row in anomalies_df.head(35).iterrows():
            atype  = row["anomaly_type"].replace("_"," ").title()  # "volume_spike" -> "Volume Spike"
            ws     = str(row.get("window_start",""))[:10]
            tcount = row.get("trade_count","")
            anomaly_html += (
                f"<div class='anomaly-row'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<span class='anomaly-ticker'>{row['ticker']}</span>"
                f"<span class='anomaly-type'>{atype}</span>"
                f"</div>"
                f"<div class='anomaly-meta'>{ws} · {tcount} trades · {row.get('member_count','')} members</div>"
                f"</div>"
            )
        anomaly_html += "</div>"
        st.markdown(anomaly_html, unsafe_allow_html=True)
