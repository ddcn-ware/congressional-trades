# Congressional Trading Tracker

A personal dashboard that shows which stocks US politicians are trading, whether any of them are stocks **I** own, and whether anything looks unusual.

I manage a personal stock portfolio — around £20k — and I pick individual companies rather than just buying ETFs. Which means I actually read through financials, watch sector movements, and think about what's moving prices. Congressional trading data kept coming up in my research. There's a lot of noise about it online but most takes are vague or cherry-picked, so I wanted to look at the actual numbers myself.

Members of the US House of Representatives have to publicly report their stock trades within 45 days (a law called the STOCK Act). This project pulls that real disclosure data and checks it against my own portfolio.

I'm a second-year Computer Science student at KCL, and I wanted to build something with a full data stack — collecting data, storing it in a real database, running it automatically every day, and showing it in a dashboard — rather than a notebook that runs once.

---

## What it does

- **Collects** every House trade disclosure (who, which stock, buy or sell, how much, when).
- **Stores** it in a PostgreSQL database, cleaned up and with no duplicates.
- **Flags anything unusual** — a stock suddenly getting traded much more than normal, or several politicians making the same bet within two weeks.
- **Checks my portfolio** (Trading 212) and puts a 🏛️ next to any stock I hold that a politician recently traded.
- **Adds context** — S&P 500, Nasdaq and VIX prices, plus market news and news about my holdings.
- **Runs itself** every morning using GitHub Actions.

---

## How it works

There are two halves: a **pipeline** that collects data once a day, and a **dashboard** that reads it.

```
          ┌──────────── PIPELINE (runs daily at 06:00 UTC on GitHub Actions) ────────────┐
          │                                                                              │
          │  1. congressional_trades.py  download trades CSV → clean → save              │
          │  2. portfolio.py             get my Trading 212 holdings → save              │
          │  3. market_context.py        get SPY, QQQ, VIX prices → save                 │
          │  4. news.py                  get headlines (general + my stocks) → save      │
          │  5. anomaly_detection.py     scan trades for unusual patterns → save         │
          │                                                                              │
          └──────────────────────────────────────┬───────────────────────────────────────┘
                                                 ▼
                                   ┌──────────────────────────┐
                                   │   PostgreSQL database    │
                                   └────────────┬─────────────┘
                                                ▼
                                   ┌──────────────────────────┐
                                   │  Streamlit dashboard     │  ← what I look at
                                   └──────────────────────────┘
```

A few things that make it reliable:

- **Stages are independent.** If one fails (say Trading 212 is down), the others still run. The run is still marked as failed in GitHub so I notice.
- **Re-running is safe.** Every trade and article has a unique ID, and the database skips anything it has already saved, so no duplicates.
- **The dashboard mostly reads from the database**, so it loads fast and doesn't depend on outside APIs being up. The one exception is the "Today" chart, which fetches live prices every 5 minutes.

### How the anomaly detection works

1. **Volume spike** — for each stock, count the trades per week and work out what's normal *for that stock*. A week is flagged if it's more than 2 standard deviations above normal (a z-score test). Comparing each stock to its own history means a quiet stock jumping to 5 trades gets flagged, but Apple at 5 trades doesn't.
2. **Cluster** — slide a 14-day window along each stock's history. Flag it if 3+ trades went the same way (all buys or all sells) by 2+ different politicians. It then tries to check whether the price moved 5%+ over the next 30 days.

---

## What's in each folder

| Folder / file | What it's for |
|---|---|
| `ingestion/` | The pipeline stages that fetch and save data |
| `ingestion/run_pipeline.py` | Runs every stage in order — **start here** |
| `ingestion/db.py` | Shared database connection code |
| `anomaly/anomaly_detection.py` | The two unusual-pattern detectors |
| `dashboard/app.py` | The dashboard page layout |
| `dashboard/db_queries.py` | Every database query the dashboard uses |
| `migrations/` | SQL that creates the database tables |
| `scripts/backfill_market_context.py` | One-off script to load 90 days of past market prices |
| `.github/workflows/pipeline.yml` | The daily schedule for GitHub Actions |
| `.env.example` | Template for your secret keys |

Each file starts with a comment explaining what it does and why.

---

## Tech used

| Job | Tool | Why |
|---|---|---|
| Language | Python | Everything, including the dashboard, in one language |
| Data cleaning | pandas | Standard tool for working with tables of data |
| Database | PostgreSQL (hosted free on Neon) | A real relational database with proper constraints |
| Scheduling | GitHub Actions | Free, and runs in the cloud without my laptop |
| Dashboard | Streamlit + Altair | Web dashboard written in pure Python |
| Trade data | [insiderwatch-data](https://github.com/saminjafari/insiderwatch-data) | Free CSV of House disclosures with tickers |
| Prices & news | Finnhub (free tier) | Documented API: 60 calls/minute |
| VIX & intraday prices | Yahoo Finance chart API | Finnhub's free tier doesn't include the VIX |
| Portfolio | Trading 212 API (read-only key) | Where my investments are |

---

## Running it yourself

You need Python 3.12 and a free Postgres database from [neon.tech](https://neon.tech).

**1. Install**

```bash
git clone https://github.com/ddcn-ware/congressional-trades
cd congressional-trades
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**2. Add your keys.** Copy the template and fill in the four values:

```bash
cp .env.example .env
```

**3. Load the keys into your terminal.** Run this in every new terminal window before the commands below:

```bash
set -a && source .env && set +a
```

**4. Create the tables, then load some price history (one time only)**

```bash
python migrations/apply_migrations.py
python scripts/backfill_market_context.py
```

**5. Run the pipeline**

```bash
python ingestion/run_pipeline.py
```

**6. Open the dashboard** at http://localhost:8501

```bash
cd dashboard && streamlit run app.py
```

**7. (Optional) Run it daily in the cloud.** In your GitHub repo go to *Settings → Secrets and variables → Actions* and add `DATABASE_URL`, `FINNHUB_API_KEY`, `T212_API_ID` and `T212_API_SECRET`. The workflow then runs every morning, and you can also trigger it from the *Actions* tab.

---

## Decisions I made along the way

**House only, not Senate.** The free Senate data source stopped updating. I'd rather show only live data than mix in old Senate data as if it were current. Quiver Quantitative has Senate data, but it's paid.

**The data source changed mid-build.** I planned to use House Stock Watcher, but it went offline while I was building this. The official House Clerk files only list filings, and the actual trades are inside PDFs. So I switched to insiderwatch-data, a community project that pulls the trades out of those PDFs.

**Finnhub for most data, Yahoo just for the VIX.** Finnhub has a proper, documented free tier. Yahoo's API is unofficial and can break without warning. But Finnhub's free tier doesn't include the VIX index, and the VIX funds you can buy (VIXY, UVXY) track futures rather than the index itself. So the VIX comes from Yahoo, and if Yahoo fails the pipeline reuses the last saved value.

**Streamlit instead of a custom website.** This project is about the data, not the UI. Streamlit let me build the whole dashboard in Python. For a real product I'd build a proper frontend.

---

## Limitations (being honest)

- **Disclosures are late.** Members have up to 45 days to report a trade, and in this data the average gap is about 50 days. So this tool shows what happened, not what's about to happen. It can't be used to trade ahead of politicians.
- **The price check on clusters usually doesn't run.** Finnhub's free tier blocks historical price requests, so most clusters are saved without the "did the price move?" check.
- **Re-running detection saves duplicate anomalies.** Each run adds its results again instead of skipping ones already found.
- **A few dates in the source data are wrong** (e.g. trade dates after the filing date). They're kept as-is for now.

---

## What I'd add next

- **Alerts** — a message when a new anomaly shows up, so I don't have to check the dashboard.
- **Anomaly de-duplication** — a unique rule on `(ticker, window_start, anomaly_type)` so re-runs don't save copies.
- **Member pages** — click a politician's name to see their full trading history. The data's already in the database.
- **Senate coverage** via Quiver Quantitative.
- **Sector breakdown** of my portfolio using Finnhub's company profiles.
