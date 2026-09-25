# Congressional Trading Tracker

I manage a personal stock portfolio — currently around £20k — and I pick individual companies rather than just buying ETFs. That means I actually spend time reading financials, watching sector movements, and thinking about what's driving prices. One thing that kept coming up in my research was congressional trading data. There's been a lot of noise about it online, but most of the takes I saw were either vague or cherry-picked. I wanted to look at the actual numbers myself.

So I built a pipeline that pulls the real disclosure data, stores it in a database, and surfaces it in a dashboard I can actually use — alongside my own portfolio and some market context. The idea was to see whether any of the stocks I'm holding have been recently traded by members of Congress, and whether there are any patterns worth paying attention to.

This is a proper personal project. I'm a first-year CS student at King's College London, and I wanted something that used the full stack — data engineering, a live database, scheduled automation, and a frontend — not just a notebook I run once.

---

## What it does

- Pulls House of Representatives stock trade disclosures daily (every member has to report within 45 days under the STOCK Act)
- Cleans and stores them in PostgreSQL — member name, ticker, buy/sell, amount range, dates
- Detects anomalies: tickers with unusual trade volume spikes, and clusters of buys/sells before significant price moves
- Tracks my own Trading 212 portfolio positions and cross-references each stock I hold against recent congressional activity
- Pulls SPY, QQQ, and VIX for market context
- Fetches market news and per-ticker news for whatever I'm holding
- Runs all of this on a daily schedule via GitHub Actions and serves it through a Streamlit dashboard

The dashboard opens on your portfolio and market charts first, then congressional data and anomalies below — because that's actually the order I care about when I check it.

---

## A few decisions I made along the way

**House-only, not House + Senate.** The Senate equivalent of the data source I wanted to use had gone stale — it wasn't being reliably updated anymore. Rather than mix dead Senate data in as if it were live, I scoped this to House-only. If I wanted Senate coverage I'd need to pay for it through a provider like Quiver Quantitative. That's a future thing.

**Why Finnhub and not just yfinance for everything.** Finnhub has a proper free tier with a documented API — 60 calls per minute, covers equity quotes, company news, and price candles. yfinance is an unofficial scraper that works until it doesn't. I used it specifically for VIX because Finnhub's free tier doesn't cover raw index symbols, but I wrapped it defensively so if Yahoo blocks the request the pipeline falls back to the last cached value rather than crashing.

**Why Streamlit and not a custom frontend.** Honestly, because the project is about the data, not the UI. Streamlit lets me write everything in Python, deploy for free on Streamlit Community Cloud, and have a usable dashboard without writing a single line of JavaScript. If this were a product I'd reconsider, but for a personal project it's the right call.

**The data source changed mid-build.** I originally planned to use House Stock Watcher, which was the main community mirror for this data. It went offline while I was building. I ended up pulling directly from the official House Clerk disclosure ZIPs first, but those only give you filing-level records with no ticker detail — the actual trade information is buried in PDFs. I found a community-maintained CSV (insiderwatch-data) that parses those PDFs and publishes ticker-level data. That's what the pipeline uses now.

---

## Dashboard

Single page, everything visible at a glance:

- **Top bar** — total trades, unique members, tickers, anomaly count, portfolio size, VIX
- **Row 1** — Portfolio positions with P&L (left) | SPY/QQQ/VIX charts (right)
- **Row 2** — Congressional trades with top tickers, weekly volume, and trade table (left) | Flagged anomalies (right)
- **Row 3** — Market news feed (collapsed by default, click to expand)

If a stock I'm holding has been traded by members of Congress in the last 30 days, it shows a 🏛️ flag next to it in the portfolio view. Click the row to see exactly who traded it and when.

---

## Tech stack

| Layer | Tool |
|---|---|
| Language | Python |
| Data cleaning | Pandas |
| Database | PostgreSQL (Neon — free managed instance) |
| Scheduling | GitHub Actions (daily cron) |
| Dashboard | Streamlit |
| Market data | Finnhub (SPY, QQQ, news) + Yahoo Finance v8 (VIX) |
| Portfolio | Trading 212 read-only API |
| Disclosure data | insiderwatch-data (community CSV, House Clerk source) |

---

## How it's built

```
GitHub Actions (runs daily at 06:00 UTC)
│
├── Stage 1: congressional_trades.py   — fetch CSV, clean, load to DB
├── Stage 2: portfolio.py              — fetch T212 positions, snapshot to DB
├── Stage 3: market_context.py         — fetch SPY, QQQ, VIX, store to DB
├── Stage 4: news.py                   — fetch Finnhub headlines, store to DB
└── Stage 5: anomaly_detection.py      — read trades, detect spikes/clusters, write flags

Streamlit dashboard
└── reads from DB only — no live API calls on page load
```

Each stage is isolated. If one fails, the others still run. The pipeline exits with a non-zero code if any stage fails, which makes failures visible in the GitHub Actions UI rather than silently passing.

---

## Setup

### 1. Database

Create a free Postgres instance at [neon.tech](https://neon.tech). Copy the connection string.

```bash
git clone https://github.com/YOUR_USERNAME/congressional-trades
cd congressional-trades
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in DATABASE_URL, FINNHUB_API_KEY, T212_API_KEY in .env
DATABASE_URL="your-connection-string" python3 migrations/apply_migrations.py
```

### 2. Seed historical market data (one-off)

```bash
set -a && source .env && set +a
python3 scripts/backfill_market_context.py
```

### 3. Run the pipeline

```bash
set -a && source .env && set +a
python3 ingestion/run_pipeline.py
```

### 4. Run the dashboard

```bash
cd dashboard && streamlit run app.py
```

### 5. GitHub Actions

Add `DATABASE_URL`, `FINNHUB_API_KEY`, and `T212_API_KEY` as repository secrets. The workflow in `.github/workflows/pipeline.yml` handles the rest — it runs daily, applies migrations, runs the pipeline, and verifies row counts after each run.

### 6. Streamlit Community Cloud

Point deployment at `dashboard/app.py`. Set the three env vars as Streamlit secrets.

---

## What I'd add next

- **Alerts** — email or Slack notification when a new anomaly is detected, rather than having to check the dashboard manually. The data is already there, it just needs a notifier step.
- **Senate coverage** — Quiver Quantitative has a Senate trading API. It's a paid tier but not expensive. Worth it if I keep using this regularly.
- **Member drill-down** — click a member's name and see their full trade history with a timeline. All the data is in the DB already, it's just a dashboard feature.
- **Proper dedup on anomalies** — right now re-running detection appends new rows even if the same anomaly was already flagged. Adding a unique constraint on `(ticker, window_start, anomaly_type)` would fix that.
- **More portfolio metrics** — sector exposure, concentration risk, that sort of thing. Would need a mapping from ticker to sector which Finnhub can provide.
