"""
Sets up the database tables by running every .sql file in this folder, in order.

What's a "migration"?
  A migration is a file that describes a change to the database structure
  (e.g. "create this table"). Keeping them as numbered files (001_, 002_ ...)
  means anyone can rebuild the exact same database from scratch.

Every statement uses IF NOT EXISTS or CREATE OR REPLACE, so running this again
on a database that's already set up does nothing harmful. That's why GitHub
Actions can safely run it before every pipeline run.

Run it with:  python migrations/apply_migrations.py
"""

import os
import pathlib
import psycopg2
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).parent  # the folder this file is in


def run():
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    # Autocommit = save each statement immediately. Some hosted Postgres
    # providers don't allow table-creation inside a transaction.
    conn.autocommit = True

    # sorted() so 001_ runs before 002_ and so on.
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
