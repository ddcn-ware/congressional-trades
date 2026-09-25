"""
Shared database connection helper.
Uses a module-level connection pool so every ingestion stage in the same
process reuses connections rather than opening a new one per function call.
"""

import os
import logging
import psycopg2
from psycopg2 import pool

logger = logging.getLogger(__name__)

_pool: pool.SimpleConnectionPool | None = None


def _get_pool() -> pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        # minconn=1 is fine for GitHub Actions (single-process runs);
        # maxconn=5 gives headroom if we ever parallelise stages.
        _pool = pool.SimpleConnectionPool(1, 5, dsn)
        logger.debug("Connection pool created")
    return _pool


def get_conn():
    """Borrow a connection from the pool. Caller must call put_conn() after."""
    return _get_pool().getconn()


def put_conn(conn):
    _get_pool().putconn(conn)


def execute_values(sql: str, rows: list[tuple], conn=None) -> int:
    """
    Bulk-insert rows using psycopg2's execute_values for efficiency.
    Returns the number of rows inserted/affected.
    Borrows its own connection if none is passed in.
    """
    from psycopg2.extras import execute_values as _ev

    owned = conn is None
    if owned:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            _ev(cur, sql, rows)
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        if owned:
            put_conn(conn)
