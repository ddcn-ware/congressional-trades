"""
Pipeline orchestrator — runs all ingestion stages in order.

Stage isolation: each stage is called in a try/except so a failure in one
doesn't cancel the others. Summaries are collected and printed at the end
so CI logs give a clear picture of what succeeded and what didn't.

Order:
  1. congressional_trades  — core dataset
  2. portfolio             — own holdings (needs no data from stage 1)
  3. market_context        — SPY, QQQ, VIX
  4. news                  — general + portfolio-scoped (needs stage 2 rows)
  5. anomaly_detection     — reads congressional_trades, writes trade_anomalies

Run locally:
    python ingestion/run_pipeline.py

Run in CI:
    See .github/workflows/pipeline.yml
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Ensure both ingestion/ and anomaly/ are on the path regardless of cwd.
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "anomaly"))
sys.path.insert(0, str(_ROOT / "ingestion"))

# Stage imports — each module exposes a run() -> dict entry point
import congressional_trades
import market_context
import news
import portfolio
import anomaly_detection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pipeline")

STAGES = [
    ("congressional_trades", congressional_trades.run),
    ("portfolio",            portfolio.run),
    ("market_context",       market_context.run),
    ("news",                 news.run),
    ("anomaly_detection",    anomaly_detection.run),
]


def main():
    start = datetime.now(timezone.utc)
    results = {}
    failures = []

    for name, fn in STAGES:
        logger.info("━━━ Starting stage: %s ━━━", name)
        try:
            summary = fn()
            results[name] = summary or {"stage": name, "status": "ok"}
        except Exception as exc:  # noqa: BLE001
            # Log the full traceback so the GitHub Actions log is useful,
            # but continue running remaining stages.
            logger.error("Stage %s FAILED: %s\n%s", name, exc, traceback.format_exc())
            results[name] = {"stage": name, "status": "failed", "error": str(exc)}
            failures.append(name)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info("━━━ Pipeline complete in %.1fs ━━━", elapsed)
    logger.info("Summary:\n%s", json.dumps(results, indent=2, default=str))

    if failures:
        logger.error("Failed stages: %s", failures)
        # Exit non-zero so the GitHub Actions run is marked as failed,
        # making partial failures visible rather than silently green.
        sys.exit(1)


if __name__ == "__main__":
    main()
