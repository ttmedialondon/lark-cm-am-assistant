"""Isolated database coverage for the once-only OAuth registration and cursor."""

import json
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4

import pytest
from test_creator_workflow_ai import pg as pg_fixture
from test_creator_workflow_ai import real_scan as real_scan_fixture

from app import creator_onboarding as onboarding
from app import creator_workflow_ai as ai
from app.crypto import message_body_cipher

pg, real_scan = pg_fixture, real_scan_fixture


@pytest.fixture
def initial(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    monkeypatch.setattr(onboarding, "connection", lambda: nullcontext(pg))
    onboarding.register_initial_scan(pg, ids["cm"])
    return ids, settings


def tick(pg):
    pg.execute("UPDATE creator_workflow_onboarding SET next_check_at=NOW() WHERE status IN ('waiting_sync','scanning')")
    onboarding.advance_initial_scans()


def synced(pg, cm):
    pg.execute(
        """INSERT INTO sync_jobs(id,authorization_id,mode,status,finished_at)
           VALUES(%s,%s,'backfill','succeeded',NOW())""",
        (uuid4(), cm),
    )
    pg.execute("UPDATE authorization_chats SET backfill_complete=TRUE WHERE authorization_id=%s", (cm,))


def test_old_authorizations_are_not_seeded_and_registration_is_idempotent(pg, real_scan):
    ids, _ = real_scan
    assert not pg.execute("SELECT 1 FROM creator_workflow_onboarding").fetchone()
    onboarding.register_initial_scan(pg, ids["cm"])
    before = onboarding.get_status(pg, ids["cm"])
    onboarding.register_initial_scan(pg, ids["cm"])
    assert onboarding.get_status(pg, ids["cm"]) == before
    assert onboarding.get_status(pg, ids["owner"]) is None


def test_waits_for_cm_consent_configuration_and_first_sync_without_losing_request(pg, initial):
    ids, _ = initial
    pg.execute("UPDATE authorizations SET assignment_eligible=FALSE WHERE id=%s", (ids["cm"],))
    tick(pg)
    assert "CM 资格" in onboarding.get_status(pg, ids["cm"])["error_message"]
    pg.execute("UPDATE authorizations SET assignment_eligible=TRUE WHERE id=%s", (ids["cm"],))
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature=%s", (ai.FEATURE,))
    tick(pg)
    assert "配置" in onboarding.get_status(pg, ids["cm"])["error_message"]
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE WHERE feature=%s", (ai.FEATURE,))
    tick(pg)
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["onboarding"]["status"] == "waiting_sync"
    assert "同步完成" in status["onboarding"]["error_message"]
    assert not pg.execute("SELECT 1 FROM creator_workflow_ai_tasks").fetchone()
    synced(pg, ids["cm"])
    tick(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "scanning"
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_ai_tasks").fetchone()["n"] == 1


def test_keyset_pages_cover_over_100_chats_once_and_share_normal_task_queue(pg, initial, monkeypatch):
    ids, settings = initial
    for number in range(108):
        chat = f"b{number:03d}"
        pg.execute("INSERT INTO chats(chat_id,chat_mode) VALUES(%s,'group')", (chat,))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (ids["cm"], chat))
    synced(pg, ids["cm"])
    tick(pg)
    first = onboarding.get_status(pg, ids["cm"])
    assert first["total_chats"] == 109 and first["checked_chats"] == 100 and first["queued_chats"] == 0
    assert first["window_end"] - first["window_start"] == timedelta(days=90)
    tick(pg)
    second = onboarding.get_status(pg, ids["cm"])
    assert second["checked_chats"] == 109 and second["queued_chats"] == 1
    calls = []
    monkeypatch.setattr(
        ai.contact_assessments, "call_openai", lambda *_a, **_k: calls.append(True) or {"text": '{"candidates":[]}'}
    )
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    tick(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "succeeded"
    onboarding.register_initial_scan(pg, ids["cm"])
    tick(pg)
    assert ai.claim_next_creator_scan() is None and calls == [True]
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_ai_runs").fetchone()["n"] == 2


def test_revocation_cancels_first_scan_without_late_model_call_or_old_authorization_rescan(pg, initial, monkeypatch):
    ids, settings = initial
    synced(pg, ids["cm"])
    tick(pg)
    job = ai.claim_next_creator_scan()
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (ids["cm"],))
    tick(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "cancelled"
    calls = []
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: calls.append(True))
    ai.run_claimed_creator_scan(job, settings)
    assert not calls
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (ids["cm"],))
    onboarding.register_initial_scan(pg, ids["cm"])
    tick(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "cancelled"
    assert ai.claim_next_creator_scan() is None


def test_first_scan_failure_stops_until_explicit_retry_without_resetting_attempt_fence(pg, initial, monkeypatch):
    ids, settings = initial
    synced(pg, ids["cm"])
    tick(pg)
    job = ai.claim_next_creator_scan()
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "invalid JSON"})
    ai.run_claimed_creator_scan(job, settings)
    tick(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "failed"
    tick(pg)
    assert ai.claim_next_creator_scan() is None
    result = onboarding.retry_initial_scan(ids["owner"], ids["cm"])
    assert result["status"] == "scanning"
    retry = ai.claim_next_creator_scan()
    assert retry["id"] == job["id"] and retry["attempts"] == job["attempts"] + 1


def test_frozen_window_excludes_new_messages_and_non_normal_chats(pg, initial):
    ids, _ = initial
    synced(pg, ids["cm"])
    tick(pg)
    row = pg.execute("SELECT * FROM creator_workflow_onboarding").fetchone()
    pg.execute("UPDATE chat_messages SET message_create_time=%s", (row["window_end"],))
    assert not pg.execute(ai._MESSAGES_SQL, (ids["cm"], "chat", row["window_start"], row["window_end"], 30)).fetchall()
    pg.execute("UPDATE chat_messages SET message_create_time=%s", (row["window_start"],))
    assert (
        len(pg.execute(ai._MESSAGES_SQL, (ids["cm"], "chat", row["window_start"], row["window_end"], 30)).fetchall())
        == 2
    )
    pg.execute("UPDATE chats SET chat_status='dissolved' WHERE chat_id='chat'")
    assert not pg.execute(ai._MESSAGES_SQL, (ids["cm"], "chat", row["window_start"], row["window_end"], 30)).fetchall()


def test_first_scan_reports_per_chat_limits_and_locks_evidence_before_decryption(pg, initial, monkeypatch):
    ids, settings = initial
    cipher = message_body_cipher(settings.token_encryption_key)
    for number in range(35):
        message_id = f"long-{number:02d}"
        pg.execute(
            """INSERT INTO chat_messages
               (chat_id,message_id,sender_open_id,message_type,body_ciphertext,message_create_time)
               VALUES('chat',%s,'ou_person','text',%s,NOW()-INTERVAL '10 seconds')""",
            (message_id, cipher.encrypt(json.dumps({"text": "x" * 2000}))),
        )
        pg.execute(
            """INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
               VALUES(%s,'chat',%s)""",
            (ids["cm"], message_id),
        )
    synced(pg, ids["cm"])
    tick(pg)
    status = onboarding.get_status(pg, ids["cm"])
    assert status["truncated_chats"] == 1
    job = ai.claim_next_creator_scan()
    queries = []

    class LockedReader:
        def execute(self, query, params=None):
            queries.append(query)
            return pg.execute(query, params)

    class Cipher:
        def decrypt(self, body):
            assert any("FOR SHARE OF ac,c" in q for q in queries)
            assert any("FOR SHARE OF m,o" in q for q in queries)
            assert any("ORDER BY open_id FOR SHARE" in q for q in queries)
            return cipher.decrypt(body)

    monkeypatch.setattr(ai, "message_body_cipher", lambda _: Cipher())
    evidence = ai._load_evidence(LockedReader(), job, settings)
    assert evidence["pack"]["input_truncated"] and evidence["pack"]["history_synced"]
    assert len(evidence["pack"]["messages"]) == 30
    assert all(len(message["text"]) == 1500 for message in evidence["pack"]["messages"])
