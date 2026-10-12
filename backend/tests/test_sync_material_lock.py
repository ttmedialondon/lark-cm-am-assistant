"""Regression for summary roster -> chat reads racing sync chat -> identity writes."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from starlette.requests import Request
from test_identity_names import pg  # noqa: F401

from app import main, summary_dialogues, worker
from app.ai_feature_config import api_key_cipher
from app.crypto import TokenCipher
from app.db import lock_sync_material
from app.lark_api import LarkProfile


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


@pytest.mark.parametrize("oauth_first", [True, False])
def test_oauth_and_discovery_serialize_before_source_locks_and_fence_old_jobs(pg, monkeypatch, oauth_first):
    dsn = _dsn()
    schema = pg.execute("SELECT current_schema() AS name").fetchone()["name"]
    application_name = "oauth_lock_" + uuid4().hex
    settings = SimpleNamespace(token_encryption_key=b"o" * 32, oauth_scopes=("offline_access",),
                               app_secret_configured=True)
    cipher = TokenCipher(settings.token_encryption_key)
    actor, job_id, state = uuid4(), uuid4(), uuid4().hex
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
               "VALUES(%s,'ou_oauth_race',%s,NOW())", (actor, cipher.encrypt("old-access")))
    pg.execute("INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status,discovery_generation) "
               "VALUES(%s,%s,'backfill','running','running',9)", (job_id, actor))
    pg.execute("INSERT INTO oauth_states(state_hash,code_verifier_ciphertext,expires_at) "
               "VALUES(%s,%s,NOW()+INTERVAL '10 minutes')", (main._sha256(state), cipher.encrypt("verifier")))
    # Commit only this fixture's private schema so both real connections can see it.
    pg.commit()
    ready, release = Event(), Event()
    original_queue, original_assert = main._queue_job, worker._assert_discovery_current

    @contextmanager
    def connection():
        with psycopg.connect(dsn, row_factory=dict_row, application_name=application_name,
                             options=f"-c search_path={schema} -c statement_timeout=8000") as conn:
            yield conn

    class FakeLark:
        def __init__(self, _settings=None):
            self.chat_id = "race_chat"

        def exchange_code(self, code, verifier):
            assert code == "fixture-code" and verifier == "verifier"
            return SimpleNamespace(access_token="new-access", refresh_token="new-refresh", expires_in=3600,
                                   refresh_expires_in=86400, scope="offline_access")

        def user_info(self, token):
            assert token == "new-access"
            return LarkProfile(open_id="ou_oauth_race", union_id=None, name="Race CM", avatar_url=None)

        def list_chats(self, access_token, page_token, *, types=None):
            return {"items": [{"chat_id": self.chat_id, "name": "Fixture group"}], "has_more": False}

    def queue(conn, *args):
        if oauth_first:
            ready.set()
            assert release.wait(5), "first OAuth transaction was not released"
        return original_queue(conn, *args)

    def current(conn, *args):
        original_assert(conn, *args)
        if not oauth_first and not ready.is_set():
            ready.set()
            assert release.wait(5), "first discovery transaction was not released"

    monkeypatch.setattr(main, "connection", connection)
    monkeypatch.setattr(worker, "connection", connection)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LarkClient", FakeLark)
    monkeypatch.setattr(main, "_queue_job", queue)
    monkeypatch.setattr(worker, "_assert_discovery_current", current)
    old_job = worker.SyncJob(job_id, actor, "backfill", 0, "running", 9)
    client = FakeLark()
    authorization = {"id": actor, "open_id": "ou_oauth_race"}
    request = Request({"type": "http", "method": "GET", "path": "/auth/lark/callback",
                       "headers": [(b"cookie", f"{main.OAUTH_STATE_COOKIE}={state}".encode())]})

    def oauth():
        response = main.lark_callback(request, code="fixture-code", state=state)
        assert response.status_code == 303 and response.headers["location"] == "/dashboard"

    def discover():
        try:
            worker._discover_chats(old_job, authorization, "old-access", client, settings, chat_type="group")
            return True
        except worker.StaleSyncJob:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(oauth if oauth_first else discover)
            try:
                assert ready.wait(5), "first transaction did not reach its source-row lock"
                second = executor.submit(discover if oauth_first else oauth)
                with connection() as probe:
                    probe.autocommit = True
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if probe.execute("SELECT 1 FROM pg_stat_activity WHERE application_name=%s "
                                         "AND wait_event='advisory'", (application_name,)).fetchone():
                            break
                        time.sleep(0.01)
                    else:
                        pytest.fail("second transaction did not wait before source-row locks")
                assert not second.done()
            finally:
                release.set()
            first.result(timeout=5)
            second_result = second.result(timeout=5)
        if oauth_first:
            assert second_result is False
        # Worker-first may become stale between its page commit and final visibility update.
        with connection() as check:
            job = check.execute("SELECT status,discovery_generation FROM sync_jobs WHERE id=%s", (job_id,)).fetchone()
            assert job == {"status": "queued", "discovery_generation": 10}
            assert len(check.execute("SELECT * FROM web_sessions WHERE authorization_id=%s", (actor,)).fetchall()) == 1
            auth = check.execute("SELECT access_token_ciphertext FROM authorizations WHERE id=%s", (actor,)).fetchone()
            assert cipher.decrypt(auth["access_token_ciphertext"]) == "new-access"
            chats = check.execute("SELECT chat_id,sync_generation,backfill_complete FROM authorization_chats "
                                  "WHERE authorization_id=%s", (actor,)).fetchall()
            assert chats == ([] if oauth_first else [{"chat_id": "race_chat", "sync_generation": 1,
                                                     "backfill_complete": False}])
        client.chat_id = "late_chat"
        assert discover() is False
        with connection() as check:
            assert not check.execute("SELECT 1 FROM chats WHERE chat_id='late_chat'").fetchone()
    finally:
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
