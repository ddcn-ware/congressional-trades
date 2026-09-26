-- Initial schema — creates every table the project uses.
--
-- Run it with: python migrations/apply_migrations.py
--
-- Every statement uses IF NOT EXISTS / CREATE OR REPLACE, so running this file
-- again on an existing database changes nothing. ("Idempotent" is the fancy
-- word for "safe to run more than once".)
--
-- Quick glossary:
--   BIGSERIAL PRIMARY KEY  an ID number the database fills in automatically (1, 2, 3...)
--   TIMESTAMPTZ            a date + time, including the time zone
--   NUMERIC                an exact decimal number (good for money)
--   JSONB                  a column that can hold a flexible JSON object
--   UNIQUE (a, b)          the database refuses two rows with the same a AND b
--   INDEX                  like the index at the back of a book — makes searching
--                          by that column fast instead of scanning every row
--   VIEW                   a saved query you can read like a table

-- ── Congressional trades ──────────────────────────────────────────────────────
-- One row per disclosed trade. House of Representatives only — the free Senate
-- data source stopped updating, so there's no reliable free live Senate feed.
CREATE TABLE IF NOT EXISTS congressional_trades (
    id                BIGSERIAL PRIMARY KEY,
    member_name       TEXT        NOT NULL,
    chamber           TEXT        NOT NULL DEFAULT 'House',
    ticker            TEXT,                                   -- nullable: mutual funds etc. don't have a ticker
    asset_description TEXT,
    transaction_type  TEXT        NOT NULL,                   -- 'buy' | 'sell' | 'exchange'
    amount_range      TEXT,                                   -- raw string from the disclosure e.g. '$1,001 - $15,000'
    amount_min        NUMERIC,
    amount_max        NUMERIC,
    transaction_date  DATE,
    disclosure_date   DATE,
    filed_date        DATE,
    source            TEXT        NOT NULL DEFAULT 'insiderwatch',
    raw_id            TEXT,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, raw_id)                                   -- stops the same trade being saved twice
);

-- Indexes on the columns the dashboard filters by most (ticker, member, date).
CREATE INDEX IF NOT EXISTS idx_ct_ticker           ON congressional_trades (ticker);
CREATE INDEX IF NOT EXISTS idx_ct_member           ON congressional_trades (member_name);
CREATE INDEX IF NOT EXISTS idx_ct_transaction_date ON congressional_trades (transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_ct_ticker_date      ON congressional_trades (ticker, transaction_date DESC);

-- ── Anomaly flags ─────────────────────────────────────────────────────────────
-- Written by anomaly_detection.py after each ingestion run.
-- Kept separate from raw trades so re-running detection doesn't touch source data.
CREATE TABLE IF NOT EXISTS trade_anomalies (
    id              BIGSERIAL PRIMARY KEY,
    anomaly_type    TEXT        NOT NULL,   -- 'volume_spike' | 'pre_move_cluster'
    ticker          TEXT,
    window_start    DATE,
    window_end      DATE,
    trade_count     INT,
    member_count    INT,
    detail          JSONB,                  -- type-specific metrics (z-score, members, price data etc.)
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_anomaly_ticker ON trade_anomalies (ticker);
CREATE INDEX IF NOT EXISTS idx_anomaly_type   ON trade_anomalies (anomaly_type);

-- ── Portfolio positions ───────────────────────────────────────────────────────
-- Snapshot from Trading 212 on each pipeline run. Rows are never deleted so
-- history is preserved — vw_latest_portfolio returns the most recent per ticker.
CREATE TABLE IF NOT EXISTS portfolio_positions (
    id                BIGSERIAL PRIMARY KEY,
    ticker            TEXT        NOT NULL,
    instrument_name   TEXT,
    quantity          NUMERIC,
    avg_price         NUMERIC,
    current_price     NUMERIC,
    pnl               NUMERIC,
    pnl_pct           NUMERIC,
    currency          TEXT,
    snapshotted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pp_ticker         ON portfolio_positions (ticker);
CREATE INDEX IF NOT EXISTS idx_pp_snapshotted_at ON portfolio_positions (snapshotted_at DESC);

-- ── Market context ────────────────────────────────────────────────────────────
-- SPY and QQQ from Finnhub; ^VIX from Yahoo Finance v8 API.
-- Stored here so the dashboard doesn't make live API calls on page load.
-- Also used for PORTFOLIO_TOTAL and PORTFOLIO_PPL from T212's cash endpoint.
CREATE TABLE IF NOT EXISTS market_context (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,   -- 'SPY' | 'QQQ' | '^VIX' | 'PORTFOLIO_TOTAL' | 'PORTFOLIO_PPL'
    price        NUMERIC,
    change_pct   NUMERIC,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source       TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mc_symbol     ON market_context (symbol);
CREATE INDEX IF NOT EXISTS idx_mc_fetched_at ON market_context (fetched_at DESC);

-- ── News ──────────────────────────────────────────────────────────────────────
-- Single table for both general market news and portfolio-specific headlines.
-- ticker=NULL means general; ticker='NVDA' means news fetched for that holding.
CREATE TABLE IF NOT EXISTS news (
    id           BIGSERIAL PRIMARY KEY,
    headline     TEXT        NOT NULL,
    summary      TEXT,
    url          TEXT,
    source_name  TEXT,
    ticker       TEXT,
    published_at TIMESTAMPTZ,
    source       TEXT        NOT NULL DEFAULT 'finnhub',
    source_id    TEXT,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_news_ticker       ON news (ticker);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);

-- ── Views ─────────────────────────────────────────────────────────────────────
-- Saved queries the dashboard reads from, so the SQL lives in one place.

-- Most-traded tickers, with separate buy and sell counts

CREATE OR REPLACE VIEW vw_top_tickers AS
SELECT
    ticker,
    COUNT(*)                                                                 AS trade_count,
    COUNT(DISTINCT member_name)                                              AS member_count,
    SUM(CASE WHEN transaction_type ILIKE 'buy'  THEN 1 ELSE 0 END)          AS buys,
    SUM(CASE WHEN transaction_type ILIKE 'sell' THEN 1 ELSE 0 END)          AS sells,
    MAX(transaction_date)                                                    AS last_trade_date
FROM congressional_trades
WHERE ticker IS NOT NULL
GROUP BY ticker
ORDER BY trade_count DESC;

-- Number of trades per ticker per week (used for the weekly volume chart)
CREATE OR REPLACE VIEW vw_weekly_volume AS
SELECT
    ticker,
    DATE_TRUNC('week', transaction_date)::DATE AS week_start,
    COUNT(*)                                   AS trade_count,
    COUNT(DISTINCT member_name)                AS member_count
FROM congressional_trades
WHERE ticker IS NOT NULL
  AND transaction_date IS NOT NULL
GROUP BY ticker, DATE_TRUNC('week', transaction_date)
ORDER BY ticker, week_start;

-- Newest snapshot of each holding — what the portfolio panel reads.
-- DISTINCT ON (ticker) + ORDER BY snapshotted_at DESC keeps only the latest row per ticker.
CREATE OR REPLACE VIEW vw_latest_portfolio AS
SELECT DISTINCT ON (ticker)
    ticker,
    instrument_name,
    quantity,
    avg_price,
    current_price,
    pnl,
    pnl_pct,
    currency,
    snapshotted_at
FROM portfolio_positions
ORDER BY ticker, snapshotted_at DESC;
