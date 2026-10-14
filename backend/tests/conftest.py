"""Shared isolated PostgreSQL fixture for the current directory and AI checks."""

import os
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import ai_dispatch


@pytest.fixture
def pg(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for isolated PostgreSQL checks")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        schema = "test_people_" + uuid4().hex
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            conn.execute(path.read_text())
        # Test accounts are valid readers unless a case explicitly removes their grants.
        conn.execute("ALTER TABLE authorizations ALTER scopes SET DEFAULT "
                     "ARRAY['im:chat:read','im:message:readonly']::text[]")
        monkeypatch.setattr(ai_dispatch, "connection", lambda: nullcontext(conn))
        try:
            yield conn
        finally:
            conn.rollback()
