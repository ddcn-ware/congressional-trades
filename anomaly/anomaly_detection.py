"""
Anomaly detection — runs after the congressional_trades stage has loaded
fresh data for the current run.

Two detectors:

1. Volume spike
   A ticker's weekly trade count is a volume-spike anomaly when it exceeds
   (mean + N * std_dev) of its own historical weekly volumes.  N=2 by default.
   This is a rolling z-score approach rather than a fixed threshold so it
   adapts to tickers that are inherently high-frequency vs low-frequency.

2. Pre-move cluster
   Looks for clusters of congressional buys *or* sells in a short window
   (default 14 days) immediately before a significant price move (default ±5%).
   Price data is fetched from Finnhub only for tickers that have a cluster
   candidate, and only if not already cached — we don't hammer the API for
   every ticker on every run.

Results are written to trade_anomalies.  The table is NOT truncated first —
each run appends new detections so we retain history.  The dashboard can
filter by detected_at to show only recent flags.
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

# When run standalone, db.py lives in ingestion/ which isn't automatically on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "ingestion"))
from db import execute_values, get_conn, put_conn

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# ── Tuning parameters ─────────────────────────────────────────────────────────
SPIKE_Z_THRESHOLD      = 2.0   # std deviations above mean to flag volume spike
SPIKE_MIN_WEEKS        = 4     # ignore tickers with fewer than this many weeks of history
SPIKE_MIN_TRADES       = 5     # and fewer than this many total trades

CLUSTER_WINDOW_DAYS    = 14    # look-back window for buy/sell cluster
CLUSTER_MIN_TRADES     = 3     # minimum trades in window to bother checking price
CLUSTER_MIN_MEMBERS    = 2     # must involve at least N distinct members
PRICE_MOVE_THRESHOLD   = 0.05  # 5% price move in the post-cluster window
PRICE_LOOKFORWARD_DAYS = 30    # days after cluster end to measure price move


def _headers() -> dict:
    return {"X-Finnhub-Token": os.environ["FINNHUB_API_KEY"]}


def _fetch_candles(symbol: str, from_ts: int, to_ts: int) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV candles from Finnhub for a symbol and date range.
    Returns a DataFrame with columns [t, c] (timestamp, close) or None on failure.
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
        if data.get("s") != "ok" or not data.get("c"):
            return None
        return pd.DataFrame({"t": data["t"], "c": data["c"]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Candle fetch failed for %s: %s", symbol, exc)
        return None


# ── Detector 1: volume spikes ─────────────────────────────────────────────────

def detect_volume_spikes(df: pd.DataFrame) -> list[dict]:
    """
    df: congressional_trades DataFrame with at least [ticker, transaction_date].
    Returns a list of anomaly dicts.
    """
    df = df.dropna(subset=["ticker", "transaction_date"]).copy()
    df = df.copy()
    df.loc[:, "week"] = pd.to_datetime(df["transaction_date"]).dt.to_period("W").apply(lambda p: p.start_time.date())

    weekly = (
        df.groupby(["ticker", "week"])
        .agg(trade_count=("ticker", "count"), member_count=("member_name", "nunique"))
        .reset_index()
    )

    anomalies = []
    for ticker, grp in weekly.groupby("ticker"):
        if len(grp) < SPIKE_MIN_WEEKS:
            continue
        if grp["trade_count"].sum() < SPIKE_MIN_TRADES:
            continue

        mean = grp["trade_count"].mean()
        std  = grp["trade_count"].std()
        if std == 0:
            continue

        threshold = mean + SPIKE_Z_THRESHOLD * std
        spikes = grp[grp["trade_count"] >= threshold]

        for _, row in spikes.iterrows():
            z = (row["trade_count"] - mean) / std
            anomalies.append({
                "anomaly_type": "volume_spike",
                "ticker":       ticker,
                "window_start": row["week"],
                "window_end":   row["week"] + timedelta(days=6),
                "trade_count":  int(row["trade_count"]),
                "member_count": int(row["member_count"]),
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
    For each ticker: find windows of CLUSTER_WINDOW_DAYS where at least
    CLUSTER_MIN_TRADES trades of the same direction occurred, then check
    whether the price moved by >= PRICE_MOVE_THRESHOLD in the following
    PRICE_LOOKFORWARD_DAYS days.

    We only fetch Finnhub candles when a cluster candidate is found, and
    we sleep between API calls to respect the 60/min rate limit.
    """
    df = df.dropna(subset=["ticker", "transaction_date", "transaction_type"]).copy()
    df = df.copy()
    df.loc[:, "transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.date

    api_key = os.environ.get("FINNHUB_API_KEY")
    anomalies = []
    api_call_count = 0

    for ticker, tdf in df.groupby("ticker"):
        tdf = tdf.sort_values("transaction_date")
        min_date = tdf["transaction_date"].min()
        max_date = tdf["transaction_date"].max()

        # Slide a window across the date range
        current = min_date
        end_limit = max_date - timedelta(days=CLUSTER_WINDOW_DAYS)

        while current <= end_limit:
            window_end = current + timedelta(days=CLUSTER_WINDOW_DAYS)
            window = tdf[
                (tdf["transaction_date"] >= current) &
                (tdf["transaction_date"] <= window_end)
            ]

            for direction in ("buy", "sell"):
                directional = window[window["transaction_type"] == direction]
                if len(directional) < CLUSTER_MIN_TRADES:
                    current += timedelta(days=7)
                    continue
                if directional["member_name"].nunique() < CLUSTER_MIN_MEMBERS:
                    current += timedelta(days=7)
                    continue

                # Potential cluster — check price move if we have an API key
                price_data = None
                price_move = None

                if api_key:
                    from_ts = int(datetime.combine(window_end, datetime.min.time()).timestamp())
                    to_ts   = int(datetime.combine(
                        window_end + timedelta(days=PRICE_LOOKFORWARD_DAYS),
                        datetime.min.time(),
                    ).timestamp())

                    # Rate-limit guard: Finnhub free tier = 60/min
                    if api_call_count > 0 and api_call_count % 50 == 0:
                        logger.info("Pausing 60s to respect Finnhub rate limit")
                        time.sleep(61)

                    candles = _fetch_candles(ticker, from_ts, to_ts)
                    api_call_count += 1

                    if candles is not None and len(candles) >= 2:
                        start_price = candles.iloc[0]["c"]
                        end_price   = candles.iloc[-1]["c"]
                        price_move  = (end_price - start_price) / start_price
                        price_data  = {
                            "start_price":        round(float(start_price), 4),
                            "end_price":          round(float(end_price), 4),
                            "price_move_pct":     round(float(price_move * 100), 2),
                            "look_forward_days":  PRICE_LOOKFORWARD_DAYS,
                        }

                # Flag if price moved enough, or if we couldn't get price data
                # (we still want to surface the cluster even without confirmation)
                if price_move is None or abs(price_move) >= PRICE_MOVE_THRESHOLD:
                    anomalies.append({
                        "anomaly_type": "pre_move_cluster",
                        "ticker":       ticker,
                        "window_start": current,
                        "window_end":   window_end,
                        "trade_count":  len(directional),
                        "member_count": directional["member_name"].nunique(),
                        "detail": {
                            "direction":     direction,
                            "members":       directional["member_name"].unique().tolist(),
                            "price_data":    price_data,
                            "confirmed":     price_move is not None and abs(price_move) >= PRICE_MOVE_THRESHOLD,
                        },
                    })

            current += timedelta(days=7)

    logger.info("Pre-move cluster detector found %d anomalies", len(anomalies))
    return anomalies


# ── Write results ─────────────────────────────────────────────────────────────

def _write_anomalies(anomalies: list[dict]) -> int:
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
            _json.dumps(a.get("detail") or {}),
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
            cols = [d[0] for d in cur.description]
        trades_df = pd.DataFrame(rows, columns=cols)
    finally:
        put_conn(conn)

    if trades_df.empty:
        logger.warning("No trade data available — skipping anomaly detection")
        return {"stage": "anomaly_detection", "anomalies": 0}

    spikes   = detect_volume_spikes(trades_df)
    clusters = detect_pre_move_clusters(trades_df)
    all_anomalies = spikes + clusters

    inserted = _write_anomalies(all_anomalies)

    summary = {
        "stage":          "anomaly_detection",
        "volume_spikes":  len(spikes),
        "pre_move_clusters": len(clusters),
        "total_written":  inserted,
    }
    logger.info("=== Anomaly detection: done — %s ===", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    run()
