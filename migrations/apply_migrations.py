"""
Apply all SQL migration files in order.
Idempotent — every statement uses IF NOT EXISTS or CREATE OR REPLACE,
so re-running is safe and is how we ensure schema currency in CI.
"""

import os
import pathlib
import psycopg2
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).parent


def run():
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    conn.autocommit = True  # DDL statements can't run in a transaction block on some managed Postgres providers

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.warning("No .sql files found in %s", MIGRATIONS_DIR)
        return

    for path in migration_files:
        logger.info("Applying %s", path.name)
        sql = path.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)
        logger.info("Applied %s OK", path.name)

    conn.close()
    logger.info("All migrations applied")


if __name__ == "__main__":
    run()
