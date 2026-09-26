"""
The "main" script — runs every stage of the pipeline, one after another.

Think of it like a checklist:
  1. congressional_trades  — download the latest trade disclosures (the core data)
  2. portfolio             — take a snapshot of my Trading 212 holdings
  3. market_context        — record today's S&P 500 (SPY), Nasdaq (QQQ) and VIX
  4. news                  — grab headlines, including ones about stocks I hold
                             (needs step 2 to have run so it knows what I hold)
  5. anomaly_detection     — look through the trades for anything unusual

Each stage runs inside its own try/except. That means if one stage crashes
(say Trading 212's API is down), the rest still run — I still get fresh
congressional data even if my portfolio couldn't update.

At the end, if ANY stage failed, the script exits with code 1. GitHub Actions
treats a non-zero exit code as "this run failed" and shows a red cross, so I
notice problems instead of seeing a misleading green tick.

Run it with:  python ingestion/run_pipeline.py
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Python only looks for imports in certain folders. The stages live in
# ingestion/ and anomaly/, so add both to the search path before importing them.
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "anomaly"))
sys.path.insert(0, str(_ROOT / "ingestion"))

import congressional_trades
import market_context
import news
import portfolio
import anomaly_detection

# Logging is like print(), but every line gets a timestamp and a level
# (INFO, WARNING, ERROR), which makes the GitHub Actions log easy to read.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline")

# (name, function) pairs, in the order they should run. Each module has a
# run() function — note we pass the function itself (no brackets), not the
# result of calling it. The loop below calls them one by one.
STAGES = [
    ("congressional_trades", congressional_trades.run),
    ("portfolio",            portfolio.run),
    ("market_context",       market_context.run),
    ("news",                 news.run),
    ("anomaly_detection",    anomaly_detection.run),
]


def main():
    start = datetime.now(timezone.utc)
    results = {}    # stage name -> summary dict, printed at the end
    failures = []   # names of any stages that crashed

    for name, fn in STAGES:
        logger.info("━━━ Starting stage: %s ━━━", name)
        try:
            summary = fn()
            results[name] = summary or {"stage": name, "status": "ok"}
        except Exception as exc:
            # Log the full traceback (the "where did it crash" info) and carry
            # on to the next stage rather than stopping everything.
            logger.error("Stage %s FAILED: %s\n%s", name, exc, traceback.format_exc())
            results[name] = {"stage": name, "status": "failed", "error": str(exc)}
            failures.append(name)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info("━━━ Pipeline complete in %.1fs ━━━", elapsed)
    logger.info("Summary:\n%s", json.dumps(results, indent=2, default=str))

    if failures:
        logger.error("Failed stages: %s", failures)
        sys.exit(1)  # non-zero exit = red cross in GitHub Actions


# This block only runs when you execute the file directly
# (python run_pipeline.py), not when another file imports it.
if __name__ == "__main__":
    main()
