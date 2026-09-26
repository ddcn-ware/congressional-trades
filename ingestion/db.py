"""
Shared database helpers used by every ingestion stage.

What's a "connection pool"?
  Opening a connection to a database is slow — it has to go over the internet,
  log in, and set things up. A pool opens a few connections once and then lends
  them out. When a stage is done it hands the connection back instead of closing
  it, so the next stage can reuse it straight away.

Every stage in the pipeline imports from here, so they all share one pool.
"""

import os
import logging
import psycopg2
from psycopg2 import pool

logger = logging.getLogger(__name__)

# The pool starts as None and is only created the first time someone asks for
# a connection. This is called "lazy" creation — if nothing ever needs the
# database, we never connect.
_pool: pool.SimpleConnectionPool | None = None


def _get_pool() -> pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        # DATABASE_URL is a secret connection string, e.g.
        # postgresql://user:password@host:5432/dbname — it lives in .env locally
        # and in GitHub Secrets when running in the cloud.
        dsn = os.environ["DATABASE_URL"]
        # Keep at least 1 connection open, never more than 5.
        # The pipeline runs one stage at a time, so this is plenty.
        _pool = pool.SimpleConnectionPool(1, 5, dsn)
        logger.debug("Connection pool created")
    return _pool


def get_conn():
    """Borrow a connection. Call put_conn() when done."""
    return _get_pool().getconn()


def put_conn(conn):
    """Give a borrowed connection back to the pool so it can be reused."""
    _get_pool().putconn(conn)


def execute_values(sql: str, rows: list[tuple], conn=None) -> int:
    """
    Insert many rows in one go and return how many were actually inserted.

    Sending 2,000 separate INSERT statements would mean 2,000 round trips to
    the database. psycopg2's execute_values packs them all into one statement,
    which is much faster.

    If you don't pass a connection in, this borrows one and returns it itself.
    """
    from psycopg2.extras import execute_values as _ev

    # Remember whether we borrowed the connection ourselves, so we only give
    # back connections we took (not ones the caller is still using).
    owned = conn is None
    if owned:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            _ev(cur, sql, rows)
            # rowcount = rows actually written. With "ON CONFLICT DO NOTHING",
            # duplicates are skipped, so this can be less than len(rows).
            count = cur.rowcount
        # commit() makes the changes permanent. Until then, nothing is saved.
        conn.commit()
        return count
    except Exception:
        # Something went wrong halfway — undo everything from this insert so
        # the database isn't left with half the rows.
        conn.rollback()
        raise
    finally:
        # "finally" always runs, error or not, so we never leak a connection.
        if owned:
            put_conn(conn)
