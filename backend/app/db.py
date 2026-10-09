"""Small synchronous PostgreSQL data-access helpers shared by the Web app and worker."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .config import get_settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def lock_sync_material(conn: psycopg.Connection, *, read: bool = False) -> None:
    """Take this before source row locks; release with the existing transaction."""
    function = "pg_advisory_xact_lock_shared" if read else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {function}(hashtextextended('lark-sync-material',0))")


def database_is_ready() -> bool:
    try:
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS healthy")
            return cursor.fetchone() == {"healthy": 1}
    except psycopg.Error:
        return False
