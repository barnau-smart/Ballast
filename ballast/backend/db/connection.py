"""Postgres connection helpers built on psycopg v3.

Exposes a thin ``get_connection`` context helper and a ``check_db`` liveness
probe used by the health endpoint. ``check_db`` never raises — it logs and
returns False on any failure so callers can degrade gracefully.
"""

from __future__ import annotations

import logging

import psycopg

from api.config import get_settings

logger = logging.getLogger("ballast.db")


def get_connection() -> psycopg.Connection:
    """Open and return a new psycopg connection using the configured URL.

    Caller is responsible for closing (use as a context manager).
    """
    settings = get_settings()
    return psycopg.connect(settings.DATABASE_URL)


def check_db() -> bool:
    """Run ``SELECT 1`` against Postgres.

    Returns True on success, False on any failure. Never raises. Connection
    errors are logged without leaking the connection string / credentials.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                return row is not None and row[0] == 1
    except Exception as exc:  # noqa: BLE001 - liveness probe must not raise
        logger.warning("db_check_failed error_type=%s", type(exc).__name__)
        return False
