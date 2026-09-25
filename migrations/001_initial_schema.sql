-- Migration 001: initial schema
-- Run this against your Supabase/Neon instance before any ingestion runs.

-- ── Congressional trades ──────────────────────────────────────────────────────
-- Core table. Every row is one disclosed transaction from the House Stock Watcher.
-- Scoped to House-only: Senate Stock Watcher has gone stale so we don't silently
-- mix dead data in as if it were live.
CREATE TABLE IF NOT EXISTS congressional_trades (
    id                BIGSERIAL PRIMARY KEY,
    member_name       TEXT        NOT NULL,
    chamber           TEXT        NOT NULL DEFAULT 'House',  -- always House for now
    ticker            TEXT,                                   -- nullable: some disclosures list no ticker (e.g. mutual funds)
    asset_description TEXT,
    transaction_type  TEXT        NOT NULL,                   -- 'buy' | 'sell' | 'exchange'
    amount_range      TEXT,                                   -- raw string from disclosure e.g. '$1,001 - $15,000'
    amount_min        NUMERIC,                                -- parsed lower bound, useful for volume aggregations
    amount_max        NUMERIC,
    transaction_date  DATE,
    disclosure_date   DATE,
    filed_date        DATE,
    source            TEXT        NOT NULL DEFAULT 'house_stock_watcher',
    raw_id            TEXT,                                   -- source-side dedup key; unique per source
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, raw_id)
);

CREATE INDEX IF NOT EXISTS idx_ct_ticker          ON congressional_trades (ticker);
CREATE INDEX IF NOT EXISTS idx_ct_member          ON congressional_trades (member_name);
CREATE INDEX IF NOT EXISTS idx_ct_transaction_date ON congressional_trades (transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_ct_ticker_date     ON congressional_trades (ticker, transaction_date DESC);

-- ── Anomaly flags ─────────────────────────────────────────────────────────────
-- One row per detected anomaly. Keeps anomaly detection output separate from
-- raw trades so re-running detection doesn't touch the source data.
CREATE TABLE IF NOT EXISTS trade_anomalies (
    id              BIGSERIAL PRIMARY KEY,
    anomaly_type    TEXT        NOT NULL,   -- 'volume_spike' | 'pre_move_cluster'
    ticker          TEXT,
    window_start    DATE,
    window_end      DATE,
    trade_count     INT,
    member_count    INT,
    detail          JSONB,                  -- flexible bag for type-specific metrics
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_anomaly_ticker ON trade_anomalies (ticker);
CREATE INDEX IF NOT EXISTS idx_anomaly_type   ON trade_anomalies (anomaly_type);

-- ── Portfolio positions ───────────────────────────────────────────────────────
-- Read-only snapshot from Trading 212. Refreshed on each pipeline run.
-- We keep history by not deleting old rows — snapshotted_at is the key.
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
-- SPY and QQQ via Finnhub (equity/ETF endpoint); ^VIX via yfinance.
-- All three stored here so the dashboard reads from DB, not live APIs.
CREATE TABLE IF NOT EXISTS market_context (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,   -- 'SPY' | 'QQQ' | '^VIX'
    price        NUMERIC,
    change_pct   NUMERIC,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source       TEXT        NOT NULL    -- 'finnhub' | 'yfinance'
);

CREATE INDEX IF NOT EXISTS idx_mc_symbol     ON market_context (symbol);
CREATE INDEX IF NOT EXISTS idx_mc_fetched_at ON market_context (fetched_at DESC);

-- ── News ──────────────────────────────────────────────────────────────────────
-- Unified table for both general market news and per-ticker portfolio news.
-- ticker is NULL for general headlines, populated for portfolio-scoped ones.
-- Dedup on (source_id, source) so re-runs don't create duplicates.
CREATE TABLE IF NOT EXISTS news (
    id          BIGSERIAL PRIMARY KEY,
    headline    TEXT        NOT NULL,
    summary     TEXT,
    url         TEXT,
    source_name TEXT,
    ticker      TEXT,                           -- NULL = general market news
    published_at TIMESTAMPTZ,
    source      TEXT        NOT NULL DEFAULT 'finnhub',
    source_id   TEXT,                           -- finnhub article id for dedup
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_news_ticker       ON news (ticker);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);

-- ── Useful views ─────────────────────────────────────────────────────────────

-- Top tickers by total disclosed trade count
CREATE OR REPLACE VIEW vw_top_tickers AS
SELECT
    ticker,
    COUNT(*)                                                AS trade_count,
    COUNT(DISTINCT member_name)                             AS member_count,
    SUM(CASE WHEN transaction_type ILIKE 'buy'  THEN 1 ELSE 0 END) AS buys,
    SUM(CASE WHEN transaction_type ILIKE 'sell' THEN 1 ELSE 0 END) AS sells,
    MAX(transaction_date)                                   AS last_trade_date
FROM congressional_trades
WHERE ticker IS NOT NULL
GROUP BY ticker
ORDER BY trade_count DESC;

-- Weekly trade volume per ticker (useful for spike detection charts)
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

-- Latest portfolio snapshot (most recent snapshotted_at per ticker)
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
