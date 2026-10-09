"""Regression for summary roster -> chat reads racing sync chat -> identity writes."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app import summary_dialogues
from app.ai_feature_config import api_key_cipher
from app.db import lock_sync_material


def _dsn():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL for isolated PostgreSQL checks")
    return value


@pytest.mark.parametrize("reader_first", [True, False])
def test_summary_and_sync_lock_before_any_source_row(reader_first):
    dsn = _dsn()
    schema = "test_sync_lock_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            for table in ("chats", "identities"):
                setup.execute(sql.SQL("CREATE TABLE {}.{}(id INT PRIMARY KEY,version INT)")
                              .format(sql.Identifier(schema), sql.Identifier(table)))
                setup.execute(sql.SQL("INSERT INTO {}.{} VALUES(1,0)")
                              .format(sql.Identifier(schema), sql.Identifier(table)))
            with psycopg.connect(dsn) as first, ThreadPoolExecutor(max_workers=1) as executor:
                first.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                first.execute("SET statement_timeout='3s'")
                lock_sync_material(first, read=reader_first)
                first.execute("SELECT * FROM identities FOR SHARE" if reader_first
                              else "UPDATE chats SET version=1")

                def second_transaction():
                    with psycopg.connect(dsn) as second:
                        second.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                        second.execute("SET statement_timeout='3s'")
                        lock_sync_material(second, read=not reader_first)
                        if reader_first:
                            second.execute("UPDATE chats SET version=1")
                            second.execute("UPDATE identities SET version=1")
                        else:
                            assert second.execute("SELECT version FROM identities FOR SHARE").fetchone() == (1,)
                            assert second.execute("SELECT version FROM chats FOR SHARE").fetchone() == (1,)

                future = executor.submit(second_transaction)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if setup.execute("SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                                     "AND wait_event='advisory'").fetchone():
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("second transaction did not wait before source row locks")
                assert not future.done()
                # The first transaction can finish the reverse row order without a deadlock.
                if reader_first:
                    assert first.execute("SELECT version FROM chats FOR SHARE").fetchone() == (0,)
                else:
                    first.execute("UPDATE identities SET version=1")
                first.commit()
                future.result(timeout=4)
        finally:
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_summary_provider_request_runs_after_material_lock_released(monkeypatch):
    dsn = _dsn()
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    cfg = {"api_key_ciphertext": api_key_cipher(settings.token_encryption_key).encrypt("fixture-only")}
    job = {"chat_id": "fixture-chat", "responsible_authorization_id": uuid4()}
    monkeypatch.setattr(summary_dialogues.core, "connection", lambda: psycopg.connect(dsn))
    monkeypatch.setattr(summary_dialogues, "checked_chat",
                        lambda conn, *_: lock_sync_material(conn, read=True))

    def provider_probe(*args, **kwargs):
        with psycopg.connect(dsn) as probe:
            assert probe.execute("SELECT pg_try_advisory_xact_lock("
                                 "hashtextextended('lark-sync-material',0))").fetchone() == (True,)
        return {"text": "fixture response"}

    monkeypatch.setattr(summary_dialogues.openai_client, "call_openai", provider_probe)
    assert summary_dialogues.request(job, settings, cfg, {}, {}, "fixture") == {"text": "fixture response"}
