"""
Anomaly detection — looks through the congressional trades for unusual patterns
and saves anything suspicious to the trade_anomalies table.

This runs last in the pipeline, after fresh trades have been loaded.

There are two detectors:

1. Volume spike — "a lot more trading than usual in this stock this week"
   For each ticker we count trades per week and work out the average and the
   standard deviation (how much the weekly count normally varies). A week is
   flagged if its count is more than 2 standard deviations above average.
   This is called a z-score test.

   Why not just "flag any week with 5+ trades"? Because popular stocks like
   Apple get traded all the time — 5 trades is normal for them. Comparing each
   ticker to its OWN history means a quiet stock jumping to 5 trades gets
   flagged, but Apple at 5 trades doesn't.

2. Pre-move cluster — "several politicians made the same bet at the same time"
   We slide a 14-day window along each ticker's history. A window counts as a
   cluster if it has at least 3 trades in the same direction (all buys or all
   sells) by at least 2 different members. We then check whether the stock
   moved 5%+ in the 30 days after the window.

   Price data comes from Finnhub, and we only ask for it when we've found a
   cluster (not for every ticker), to save API calls. Finnhub's free tier
   often refuses historical price requests (HTTP 403), so clusters are saved
   either way — the "confirmed" field just stays False when we couldn't check.

Results are appended (added on), never deleted, so the history is kept.
"""

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

# db.py lives in the ingestion/ folder, so add that folder to Python's search path.
sys.path.insert(0, str(Path(__file__).parent.parent / "ingestion"))
from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# Change these to make the detectors stricter (fewer flags) or looser (more flags).
SPIKE_Z_THRESHOLD      = 2.0   # how many standard deviations above average counts as a spike
SPIKE_MIN_WEEKS        = 4     # ignore tickers with fewer than 4 weeks of history (not enough to judge "normal")
SPIKE_MIN_TRADES       = 5     # ...or fewer than 5 trades in total

CLUSTER_WINDOW_DAYS    = 14    # size of the sliding window
CLUSTER_MIN_TRADES     = 3     # same-direction trades needed in the window
CLUSTER_MIN_MEMBERS    = 2     # different members needed (one person trading 3 times isn't a "cluster")
PRICE_MOVE_THRESHOLD   = 0.05  # 5% price move counts as "significant"
PRICE_LOOKFORWARD_DAYS = 30    # how far after the window to check the price


def _headers() -> dict:
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_candles(symbol: str, from_ts: int, to_ts: int) -> pd.DataFrame | None:
    """
    Get daily closing prices between two Unix timestamps.
    ("Candles" is trading slang for price bars.) Returns a table with
    columns t (time) and c (close price), or None if unavailable.
    """
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/candle",
            params={"symbol": symbol, "resolution": "D", "from": from_ts, "to": to_ts},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("s") != "ok" or not data.get("c"):  # "s" = status
            return None
        return pd.DataFrame({"t": data["t"], "c": data["c"]})
    except Exception as exc:
        logger.warning("Candle fetch failed for %s: %s", symbol, exc)
        return None


# ── Detector 1: volume spikes ─────────────────────────────────────────────────

def detect_volume_spikes(df: pd.DataFrame) -> list[dict]:
    """
    Find weeks where a ticker was traded far more than usual.
    df needs the columns ticker, transaction_date and member_name.
    Returns a list of anomaly dictionaries.
    """
    df = df.dropna(subset=["ticker", "transaction_date"]).copy()
    # Label every trade with the Monday of its week, so we can group by week.
    df.loc[:, "week"] = pd.to_datetime(df["transaction_date"]).dt.to_period("W").apply(lambda p: p.start_time.date())

    # One row per (ticker, week): how many trades, and how many different members.
    weekly = (
        df.groupby(["ticker", "week"])
        .agg(trade_count=("ticker", "count"), member_count=("member_name", "nunique"))
        .reset_index()
    )

    anomalies = []
    for ticker, grp in weekly.groupby("ticker"):
        # Skip tickers without enough history to know what "normal" looks like.
        if len(grp) < SPIKE_MIN_WEEKS:
            continue
        if grp["trade_count"].sum() < SPIKE_MIN_TRADES:
            continue

        mean = grp["trade_count"].mean()
        std  = grp["trade_count"].std()
        if std == 0:
            continue  # every week identical — nothing can stand out (and we'd divide by zero)

        threshold = mean + SPIKE_Z_THRESHOLD * std
        spikes = grp[grp["trade_count"] >= threshold]

        for _, row in spikes.iterrows():
            # z-score = how many standard deviations above average this week was
            z = (row["trade_count"] - mean) / std
            anomalies.append({
                "anomaly_type": "volume_spike",
                "ticker":       ticker,
                "window_start": row["week"],
                "window_end":   row["week"] + timedelta(days=6),
                "trade_count":  int(row["trade_count"]),
                "member_count": int(row["member_count"]),
                # Extra numbers saved as JSON so the dashboard can show why it was flagged.
                "detail": {
                    "z_score":         round(float(z), 2),
                    "weekly_mean":     round(float(mean), 2),
                    "weekly_std":      round(float(std), 2),
                    "spike_threshold": round(float(threshold), 2),
                },
            })

    logger.info("Volume spike detector found %d anomalies", len(anomalies))
    return anomalies


# ── Detector 2: pre-move clusters ─────────────────────────────────────────────

def detect_pre_move_clusters(df: pd.DataFrame) -> list[dict]:
    """
    Slide a 14-day window along each ticker's trades, one week at a time, and
    flag windows where several members traded the same direction. If we have a
    Finnhub key, also check whether the price moved afterwards.
    """
    df = df.dropna(subset=["ticker", "transaction_date", "transaction_type"]).copy()
    df.loc[:, "transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.date

    api_key = os.environ.get("FINNHUB_API_KEY")
    anomalies = []
    api_call_count = 0

    for ticker, tdf in df.groupby("ticker"):
        tdf = tdf.sort_values("transaction_date")
        min_date = tdf["transaction_date"].min()
        max_date = tdf["transaction_date"].max()

        # "current" is the start of the window; it moves forward through time.
        current = min_date
        end_limit = max_date - timedelta(days=CLUSTER_WINDOW_DAYS)

        while current <= end_limit:
            window_end = current + timedelta(days=CLUSTER_WINDOW_DAYS)
            window = tdf[
                (tdf["transaction_date"] >= current) &
                (tdf["transaction_date"] <= window_end)
            ]

            # Check buys and sells separately — we want trades that agree.
            for direction in ("buy", "sell"):
                directional = window[window["transaction_type"] == direction]
                if len(directional) < CLUSTER_MIN_TRADES:
                    current += timedelta(days=7)
                    continue
                if directional["member_name"].nunique() < CLUSTER_MIN_MEMBERS:
                    current += timedelta(days=7)
                    continue

                # We have a cluster — now try to see what the price did next.
                price_data = None
                price_move = None

                if api_key:
                    # Finnhub wants Unix timestamps (seconds since 1970).
                    from_ts = int(datetime.combine(window_end, datetime.min.time()).timestamp())
                    to_ts   = int(datetime.combine(
                        window_end + timedelta(days=PRICE_LOOKFORWARD_DAYS),
                        datetime.min.time(),
                    ).timestamp())

                    # Every 50 calls, pause for a minute so we stay under Finnhub's 60/min limit.
                    if api_call_count > 0 and api_call_count % 50 == 0:
                        logger.info("Pausing 60s for Finnhub rate limit")
                        time.sleep(61)

                    candles = _fetch_candles(ticker, from_ts, to_ts)
                    api_call_count += 1

                    if candles is not None and len(candles) >= 2:
                        # % change from the first to the last day of the look-forward period
                        start_price = candles.iloc[0]["c"]
                        end_price   = candles.iloc[-1]["c"]
                        price_move  = (end_price - start_price) / start_price
                        price_data  = {
                            "start_price":       round(float(start_price), 4),
                            "end_price":         round(float(end_price), 4),
                            "price_move_pct":    round(float(price_move * 100), 2),
                            "look_forward_days": PRICE_LOOKFORWARD_DAYS,
                        }

                # Save the cluster if either:
                #   - we couldn't get price data (still worth knowing about), or
                #   - the price really did move 5%+ afterwards.
                # Clusters where we checked and the price barely moved are dropped.
                if price_move is None or abs(price_move) >= PRICE_MOVE_THRESHOLD:
                    anomalies.append({
                        "anomaly_type": "pre_move_cluster",
                        "ticker":       ticker,
                        "window_start": current,
                        "window_end":   window_end,
                        "trade_count":  len(directional),
                        "member_count": directional["member_name"].nunique(),
                        "detail": {
                            "direction":  direction,
                            "members":    directional["member_name"].unique().tolist(),
                            "price_data": price_data,
                            # True only if we actually saw a 5%+ move
                            "confirmed":  price_move is not None and abs(price_move) >= PRICE_MOVE_THRESHOLD,
                        },
                    })

            # Slide the window forward by a week.
            current += timedelta(days=7)

    logger.info("Pre-move cluster detector found %d anomalies", len(anomalies))
    return anomalies


# ── Saving results ────────────────────────────────────────────────────────────

def _write_anomalies(anomalies: list[dict]) -> int:
    """Insert the anomalies into trade_anomalies. The "detail" dict is stored as JSON."""
    if not anomalies:
        return 0

    import json as _json

    rows = [
        (
            a["anomaly_type"],
            a.get("ticker"),
            a.get("window_start"),
            a.get("window_end"),
            a.get("trade_count"),
            a.get("member_count"),
            _json.dumps(a.get("detail") or {}),  # dict -> JSON text for the JSONB column
        )
        for a in anomalies
    ]

    sql = """
        INSERT INTO trade_anomalies
            (anomaly_type, ticker, window_start, window_end, trade_count, member_count, detail)
        VALUES %s
    """
    inserted = execute_values(sql, rows)
    logger.info("Wrote %d anomaly rows", inserted)
    return inserted


def run() -> dict[str, Any]:
    logger.info("=== Anomaly detection: start ===")

    # Load every trade that has a date into a pandas DataFrame.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT member_name, ticker, transaction_type, transaction_date
                FROM congressional_trades
                WHERE transaction_date IS NOT NULL
                """
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]  # column names from the query
        trades_df = pd.DataFrame(rows, columns=cols)
    finally:
        put_conn(conn)

    if trades_df.empty:
        logger.warning("No trade data — skipping anomaly detection")
        return {"stage": "anomaly_detection", "anomalies": 0}

    spikes   = detect_volume_spikes(trades_df)
    clusters = detect_pre_move_clusters(trades_df)
    inserted = _write_anomalies(spikes + clusters)

    summary = {
        "stage":             "anomaly_detection",
        "volume_spikes":     len(spikes),
        "pre_move_clusters": len(clusters),
        "total_written":     inserted,
    }
    logger.info("=== Anomaly detection: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
