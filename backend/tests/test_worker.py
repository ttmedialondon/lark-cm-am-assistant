import time
from collections import Counter
from concurrent.futures import Future as TaskFuture
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import creator_authorization_rescans, worker
from app.crypto import TokenCipher
from app.lark_api import TokenBundle
from app.worker import (
    _p2p_counterpart_open_id,
    _read_member_snapshot,
)


def test_claimed_ingestion_keeps_identity_evidence_encrypted_and_deduplicates(pg, monkeypatch):
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    signals = []
    monkeypatch.setattr(worker.creator_responsibility_matching, "touch_chats",
                        lambda _conn, _chats, **signal: signals.append(signal))
    actor, job_id = UUID(int=1), UUID(int=2)
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'ou_cm','fixture')", (actor,))
    pg.execute("""INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status,discovery_generation)
               VALUES(%s,%s,'backfill','running','succeeded',3)""", (job_id, actor))
    for chat in ("oc_ingest_1", "oc_ingest_2", "oc_ingest_3"):
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_page_token)
                   VALUES(%s,%s,'page-2')""", (actor, chat))
    job = worker.SyncJob(job_id, actor, "backfill", 0, "succeeded", 3)
    config = SimpleNamespace(sync_lookback_days=0, sync_overlap_seconds=600, sync_interval_seconds=300,
                             lark_sync_per_authorization_max_inflight=2)
    authorization = {"open_id": "ou_cm", "body_consent_at": None}
    claim = worker._claim_next_page(job, authorization, config)
    other = worker._claim_next_page(job, authorization, config)
    assert claim.mode == "incremental" and claim.end_at - claim.start_at == worker.timedelta(days=7)
    assert other.mode == "backfill" and other.expected_page_token == "page-2" and other.start_at is None
    assert claim.chat_id != other.chat_id
    assert worker._claim_next_page(job, authorization, config) is None
    body = '{"text":"hello"}'
    mention = {"key": "@_user_1", "id": "ou_mentioned", "id_type": "open_id", "name": "Mentioned"}
    messages = [
        {"message_id": "om_cm", "create_time": "1615380573411", "body": {"content": body},
         "msg_type": "text", "parent_id": "om_parent", "root_id": "om_root", "thread_id": "omt_thread",
         "message_position": "12", "mentions": [{**mention, "ignored": "not stored"}],
         "sender": {"id": "ou_cm", "id_type": "open_id", "sender_type": "user", "sender_name": "Alex"}},
        {"message_id": "om_external", "sender": {"id": "ou_external", "id_type": "open_id",
         "tenant_key": "external", "sender_type": "user", "sender_name": "Alex"}},
        {"message_id": "om_app", "sender": {"id": "cli_app", "id_type": "app_id", "sender_type": "app"}},
        {"message_id": "om_untyped", "sender": {"id": "ou_cm", "sender_type": "user"}},
    ]
    cipher = TokenCipher(b"i" * 32)
    result = worker._record_claimed_page(claim, messages, "page-3", cipher, fingerprint_key=b"i" * 32)
    assert result.committed and (result.messages, result.own_messages) == (4, 1)
    rows = {row["message_id"]: row for row in pg.execute("SELECT * FROM chat_messages")}
    saved = rows["om_cm"]
    assert cipher.decrypt(saved["body_ciphertext"]) == body and body not in repr(saved)
    assert saved["message_create_time"].year == 2021
    assert (saved["parent_id"], saved["root_id"], saved["thread_id"], saved["message_position"]) == (
        "om_parent", "om_root", "omt_thread", 12)
    assert saved["mentions_json"] == [mention]
    assert rows["om_app"]["sender_open_id"] is None and rows["om_untyped"]["sender_open_id"] is None
    identities = {row["open_id"]: row for row in pg.execute("SELECT * FROM lark_identities")}
    assert identities["ou_cm"]["contact_id"] != identities["ou_external"]["contact_id"]
    assert identities["ou_external"]["tenant_classification"] == "known_external"
    assert identities["ou_mentioned"]["tenant_classification"] == "unresolved"
    replay = worker._claim_next_page(job, authorization, config)
    assert replay.chat_id == claim.chat_id and replay.expected_page_token == "page-3"
    duplicate = worker._record_claimed_page(replay, messages, None, cipher, fingerprint_key=b"i" * 32)
    assert duplicate.committed and duplicate.messages == duplicate.own_messages == 0
    synced = pg.execute("SELECT * FROM authorization_chats WHERE chat_id=%s", (claim.chat_id,)).fetchone()
    assert synced["last_incremental_at"] == replay.end_at and not synced["backfill_complete"]
    assert synced["backfill_page_token"] == "page-2" and synced["sync_status"] != "succeeded"
    assert len(signals) == 1 and set(signals[0]["message_ids"]) == {m["message_id"] for m in messages}
    assert signals[0]["authorization_id"] == actor and signals[0]["new_scope"]
    assert set(signals[0]["observed_message_ids"]) == {m["message_id"] for m in messages}
    assert pg.execute("SELECT count(*) n FROM authorization_message_observations").fetchone()["n"] == 4
    assert not worker._record_claimed_page(claim, [{"message_id": "om_stale"}], None, cipher).committed
    assert not pg.execute("SELECT 1 FROM chat_messages WHERE message_id='om_stale'").fetchone()
    pg.execute("UPDATE authorization_message_observations SET observed_at=NOW()-INTERVAL '1 day'")
    pg.execute("UPDATE authorizations SET body_consent_at=NOW() WHERE id=%s", (actor,))
    authorization["body_consent_at"] = pg.execute("SELECT NOW() consent").fetchone()["consent"]
    granted = worker._claim_next_page(job, authorization, config)
    assert granted.chat_id == claim.chat_id and granted.mode == "backfill"
    restored = worker._record_claimed_page(granted, messages, "history-3", cipher, fingerprint_key=b"i" * 32)
    assert restored.committed and restored.messages == 0
    assert len(signals) == 2 and not signals[-1]["message_ids"] and signals[-1]["new_scope"]
    assert set(signals[-1]["observed_message_ids"]) == {m["message_id"] for m in messages}
    pg.execute("UPDATE authorization_chats SET sync_enabled=FALSE WHERE chat_id='oc_ingest_3'")
    edited = {**messages[0], "body": {"content": '{"text":"updated"}'}}
    history = worker._claim_next_page(job, authorization, config)
    assert history.chat_id == claim.chat_id and history.mode == "backfill"
    assert worker._record_claimed_page(history, [edited], None, cipher, fingerprint_key=b"i" * 32).committed
    assert len(signals) == 3 and signals[-1]["message_ids"] == ["om_cm"] and not signals[-1]["new_scope"]
    assert signals[-1]["observed_message_ids"] == []
    pg.execute("UPDATE authorization_chats SET sync_enabled=TRUE WHERE chat_id='oc_ingest_3'")
    current = worker._claim_next_page(job, authorization, config)
    pg.execute("UPDATE authorization_chats SET sync_generation=sync_generation+1 WHERE chat_id=%s", (current.chat_id,))
    assert not worker._record_claimed_page(current, [{"message_id": "om_revoked"}], None, cipher).committed
    assert not pg.execute("SELECT 1 FROM chat_messages WHERE message_id='om_revoked'").fetchone()
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(sync_interval_seconds=300))
    pg.execute("UPDATE authorization_chats SET backfill_complete=TRUE,page_claim_id=NULL,"
               "page_claim_job_id=NULL,page_lease_until=NULL,incremental_job_id=%s,"
               "incremental_window_start_at=NULL,incremental_window_end_at=NULL", (job_id,))
    pg.execute("UPDATE sync_jobs SET mode='backfill' WHERE id=%s", (job_id,))
    worker._finish_and_schedule_incremental(job)
    following = pg.execute("SELECT * FROM sync_jobs WHERE status='queued'").fetchone()
    assert following["id"] != job_id and following["mode"] == "incremental"
    assert following["scheduled_at"] == pg.execute("SELECT NOW() due").fetchone()["due"]


def test_new_cm_interleaves_recent_and_history_without_losing_either_window(pg, monkeypatch):
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    settings = SimpleNamespace(sync_lookback_days=0, sync_overlap_seconds=600, sync_interval_seconds=300,
                               lark_sync_per_authorization_max_inflight=2)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    actor, job_id = uuid4(), uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'ou_new','fixture')", (actor,))
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_long_history')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'oc_long_history')", (actor,))
    pg.execute("""INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status)
        VALUES(%s,%s,'backfill','running','succeeded')""", (job_id, actor))
    job, auth = worker.SyncJob(job_id, actor, "backfill", 0, "succeeded", 0), {"open_id": "ou_new"}
    recent = worker._claim_next_page(job, auth, settings)
    assert recent.mode == "incremental" and recent.end_at - recent.start_at == worker.timedelta(days=7)
    assert worker._record_claimed_page(recent, [], "recent-2", None).committed
    assert pg.execute("SELECT recent_coverage_start_at FROM authorization_chats").fetchone()[
        "recent_coverage_start_at"] is None
    history = worker._claim_next_page(job, auth, settings)
    assert history.mode == "backfill" and history.start_at is None
    assert worker._record_claimed_page(history, [], "history-2", None).committed
    resumed = worker._claim_next_page(job, auth, settings)
    assert (resumed.mode, resumed.expected_page_token, resumed.start_at, resumed.end_at) == (
        "incremental", "recent-2", recent.start_at, recent.end_at)
    assert worker._record_claimed_page(resumed, [], None, None).committed
    row = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_long_history'").fetchone()
    assert row["last_incremental_at"] == recent.end_at and not row["backfill_complete"]
    assert row["recent_coverage_start_at"] == recent.start_at
    assert row["backfill_page_token"] == "history-2"
    pg.execute("UPDATE authorization_chats SET last_incremental_at=NOW()-INTERVAL '6 minutes'")
    waterline = pg.execute("SELECT last_incremental_at FROM authorization_chats").fetchone()["last_incremental_at"]
    resumed_history = worker._claim_next_page(job, auth, settings)
    assert (resumed_history.mode, resumed_history.expected_page_token, resumed_history.end_at) == (
        "backfill", "history-2", history.end_at)
    assert worker._record_claimed_page(resumed_history, [], "history-3", None).committed
    updated = worker._claim_next_page(job, auth, settings)
    assert updated.mode == "incremental" and updated.start_at == waterline - worker.timedelta(seconds=600)
    assert worker._record_claimed_page(updated, [], None, None).committed
    last_history = worker._claim_next_page(job, auth, settings)
    assert last_history.mode == "backfill" and last_history.expected_page_token == "history-3"
    assert worker._record_claimed_page(last_history, [], None, None).committed
    row = pg.execute("SELECT * FROM authorization_chats").fetchone()
    assert row["backfill_complete"] and row["last_backfill_at"] == history.end_at
    assert row["last_incremental_at"] == updated.end_at
    pg.execute("""UPDATE authorization_chats SET last_incremental_at=NOW()-INTERVAL '10 days',
        incremental_window_start_at=NOW()-INTERVAL '3 days',incremental_window_end_at=NOW(),
        incremental_page_token='gap-1'""")
    gap = worker._claim_next_page(job, auth, settings)
    assert worker._record_claimed_page(gap, [], "gap-2", None).committed
    assert pg.execute("SELECT recent_coverage_start_at FROM authorization_chats").fetchone()[
        "recent_coverage_start_at"] == recent.start_at
    late = worker._claim_next_page(job, auth, settings)
    pg.execute("UPDATE authorization_chats SET sync_generation=sync_generation+1")
    assert not worker._record_claimed_page(late, [], None, None).committed
    assert pg.execute("SELECT recent_coverage_start_at FROM authorization_chats").fetchone()[
        "recent_coverage_start_at"] == recent.start_at
    pg.execute("UPDATE authorization_chats SET page_claim_id=NULL,page_claim_job_id=NULL,page_lease_until=NULL")
    renewed = worker._claim_next_page(job, auth, settings)
    assert renewed.expected_page_token == "gap-2" and worker._record_claimed_page(renewed, [], None, None).committed
    assert pg.execute("SELECT recent_coverage_start_at FROM authorization_chats").fetchone()[
        "recent_coverage_start_at"] == gap.start_at
    worker._finish_and_schedule_incremental(job)
    following = pg.execute("SELECT * FROM sync_jobs WHERE status='queued'").fetchone()
    assert following["mode"] == "incremental" and following["scheduled_at"] == pg.execute(
        "SELECT NOW()+INTERVAL '300 seconds' due").fetchone()["due"]


def test_p2p_contact_discovery_skips_bots(pg, monkeypatch) -> None:
    assert _p2p_counterpart_open_id("ou_person", "user") == "ou_person"
    assert _p2p_counterpart_open_id("cli_bot", "bot") is None
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "incremental", 1, "running", 1)
    pg.execute(
        "INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'ou_cm','test')",
        (job.authorization_id,),
    )
    pg.execute(
        """INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status,discovery_generation)
               VALUES(%s,%s,'incremental','running','running',1)""",
        (job.id, job.authorization_id),
    )
    pg.execute("CREATE TEMP TABLE p2p_open_id_changes(old_id TEXT,new_id TEXT)")
    pg.execute("""CREATE FUNCTION pg_temp.capture_p2p_open_id_change() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO p2p_open_id_changes VALUES(OLD.p2p_counterpart_open_id,NEW.p2p_counterpart_open_id);
        RETURN NEW; END $$""")
    pg.execute("""CREATE TRIGGER capture_p2p_open_id_change AFTER UPDATE OF p2p_counterpart_open_id
        ON authorization_chats FOR EACH ROW
        WHEN(OLD.p2p_counterpart_open_id IS DISTINCT FROM NEW.p2p_counterpart_open_id)
        EXECUTE FUNCTION pg_temp.capture_p2p_open_id_change()""")

    class Client:
        target_id, target_type = "ou_person", "user"

        def list_chats(self, _token, page_token, *, types):
            assert page_token is None and types == "p2p"
            return {"items": [
                {"chat_id": "oc_person", "p2p_target_id": self.target_id,
                 "p2p_target_type": self.target_type},
                {"chat_id": "oc_bot", "p2p_target_id": "cli_bot", "p2p_target_type": "bot"},
            ]}

    client = Client()
    assert pg.execute("SELECT 1 FROM lark_identities WHERE open_id='ou_person'").fetchone() is None
    contact_id = None
    for attempt in range(2):
        assert worker._discover_chats(
            job, {"id": job.authorization_id}, "test", client, SimpleNamespace(), chat_type="p2p",
        ) == 2
        rows = pg.execute(
            """SELECT access.chat_id,access.p2p_counterpart_open_id,access.p2p_counterpart_seen_at,
                      identity.contact_id
               FROM authorization_chats access
               LEFT JOIN lark_identities identity ON identity.open_id=access.p2p_counterpart_open_id
               ORDER BY access.chat_id""",
        ).fetchall()
        assert rows[0] == {"chat_id": "oc_bot", "p2p_counterpart_open_id": None,
                           "p2p_counterpart_seen_at": None, "contact_id": None}
        assert rows[1]["p2p_counterpart_open_id"] == "ou_person"
        assert rows[1]["p2p_counterpart_seen_at"] is not None
        assert rows[1]["contact_id"] is not None
        if contact_id is not None:
            assert rows[1]["contact_id"] == contact_id
        contact_id = rows[1]["contact_id"]
        assert pg.execute("SELECT COUNT(*) AS count FROM contacts").fetchone()["count"] == 1
        assert pg.execute("SELECT 1 FROM lark_identities WHERE open_id='cli_bot'").fetchone() is None
        if attempt == 0:
            pg.execute("DELETE FROM p2p_open_id_changes")
        else:
            assert pg.execute("SELECT old_id,new_id FROM p2p_open_id_changes").fetchall() == []

    client.target_id = "ou_other_person"
    worker._discover_chats(
        job, {"id": job.authorization_id}, "test", client, SimpleNamespace(), chat_type="p2p",
    )
    assert pg.execute("SELECT old_id,new_id FROM p2p_open_id_changes").fetchall() == [
        {"old_id": "ou_person", "new_id": "ou_other_person"}
    ]
    pg.execute("DELETE FROM p2p_open_id_changes")

    client.target_id, client.target_type = "cli_bot", "bot"
    worker._discover_chats(
        job, {"id": job.authorization_id}, "test", client, SimpleNamespace(), chat_type="p2p",
    )
    assert pg.execute("SELECT old_id,new_id FROM p2p_open_id_changes").fetchall() == [
        {"old_id": "ou_other_person", "new_id": None}
    ]
    assert pg.execute(
        "SELECT p2p_counterpart_open_id,p2p_counterpart_seen_at FROM authorization_chats WHERE chat_id='oc_person'",
    ).fetchone() == {"p2p_counterpart_open_id": None, "p2p_counterpart_seen_at": None}
    assert pg.execute("SELECT contact_id FROM lark_identities WHERE open_id='ou_person'").fetchone()[
        "contact_id"] == contact_id


def test_token_refresh_records_actual_scope_without_inventing_missing_grants(pg, monkeypatch):
    actor, key = UUID(int=991), b"r" * 32
    cipher = TokenCipher(key)
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    pg.execute("""INSERT INTO authorizations(id,open_id,access_token_ciphertext,refresh_token_ciphertext,
        access_token_expires_at,refresh_token_expires_at,scopes,body_consent_at)
        VALUES(%s,'ou_refresh_scope',%s,%s,NOW()-INTERVAL '1 hour',NOW()+INTERVAL '1 day',%s,
        NOW()-INTERVAL '30 days')""",
        (actor, cipher.encrypt("old-access"), cipher.encrypt("old-refresh"),
         ["im:chat:read", "im:message:readonly", "im:message.send_as_user"]))
    consent = worker._authorization(actor)["body_consent_at"]

    class Client:
        calls = 0
        scope = None
        def refresh_token(self, refresh):
            assert refresh == ("old-refresh" if self.calls == 0 else f"refresh-{self.calls}")
            self.calls += 1
            return TokenBundle(f"access-{self.calls}", f"refresh-{self.calls}", 3600, 86400, self.scope)

    client = Client()
    for scope, expected in (("im:chat:read im:message:readonly im:message:readonly",
                            ["im:chat:read", "im:message:readonly"]),
                            (None, ["im:chat:read", "im:message:readonly"]),
                            ("im:message:readonly", ["im:message:readonly"]),
                            ("im:message.send_as_user im:message.send_as_user", ["im:message.send_as_user"]),
                            ("", [])):
        pg.execute("UPDATE authorizations SET status='active',access_token_expires_at=NOW()-INTERVAL '1 hour' "
                   "WHERE id=%s", (actor,))
        client.scope = scope
        if scope is not None and not worker.BODY_READ_SCOPES.issubset(expected):
            with pytest.raises(worker.ReauthorizationRequired):
                worker._usable_access_token(actor, worker._authorization(actor), client,
                                            SimpleNamespace(token_encryption_key=key))
        else:
            assert worker._usable_access_token(actor, worker._authorization(actor), client,
                                               SimpleNamespace(token_encryption_key=key)) == f"access-{client.calls}"
        row = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
        assert row["scopes"] == expected and row["body_consent_at"] == consent
        assert row["status"] == ("active" if scope is None or worker.BODY_READ_SCOPES.issubset(expected)
                                 else "attention_required")
        assert cipher.decrypt(row["refresh_token_ciphertext"]) == f"refresh-{client.calls}"

    before = row
    pg.execute("UPDATE authorizations SET status='active',access_token_expires_at=NOW()-INTERVAL '1 hour' "
               "WHERE id=%s", (actor,))
    def unavailable(_refresh):
        raise worker.LarkApiError(503, None, "Temporary service issue")
    client.refresh_token = unavailable
    with pytest.raises(worker.LarkApiError):
        worker._usable_access_token(actor, worker._authorization(actor), client,
                                    SimpleNamespace(token_encryption_key=key))
    row = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
    assert row["status"] == "active" and row["body_consent_at"] == consent
    assert row["scopes"] == before["scopes"] and row["refresh_token_ciphertext"] == before["refresh_token_ciphertext"]


def test_member_snapshot_follows_lark_pagination_and_keeps_member_facts() -> None:
    class Client:
        def __init__(self):
            self.tokens = []

        def list_chat_members(self, _token, _chat_id, page_token=None):
            self.tokens.append(page_token)
            if page_token is None:
                return {
                    "user_total": 2,
                    "has_more": True,
                    "page_token": "next",
                    "users": [{"member_id": "ou_one", "name": "One", "tenant_key": "tenant_a"}],
                }
            return {"user_total": 2, "items": [{"member_id": "ou_two", "name": "Two"}]}

    client = Client()
    snapshot = _read_member_snapshot(client, "token", "oc_test")

    assert client.tokens == [None, "next"]
    assert snapshot.count == 2
    assert snapshot.snapshot_status == "succeeded"
    assert {member["open_id"] for member in snapshot.members} == {"ou_one", "ou_two"}


def test_background_rotation_does_not_block_interactive_requests(monkeypatch) -> None:
    submitted: list[tuple[object, object]] = []

    class Future:
        def done(self):
            return False

    class Executor:
        def submit(self, runner, job, settings):
            submitted.append((runner, job))
            return Future()

    summary_jobs = iter(({"id": "summary-1"}, {"id": "summary-2"}, {"id": "summary-3"}))
    import_jobs = iter(({"id": "import-1"},))
    reply_jobs = iter(({"id": "reply-1"},))
    matching_jobs = iter(({"id": "matching-1"},))
    responsibility_jobs = iter(({"id": "responsibility-1"},))
    group_jobs = iter(({"id": "group-1"},))
    assistant_jobs = iter(({"id": "assistant-1"},))
    importance_jobs = iter(({"id": "importance-1"},))
    material_jobs = iter(({"id": "material-1"},))
    monkeypatch.setattr(worker.message_materials, "claim", lambda: next(material_jobs, None))
    monkeypatch.setattr(worker.assistant, "claim", lambda: next(assistant_jobs, None))
    monkeypatch.setattr(worker.creator_importance, "claim", lambda: next(importance_jobs, None))
    monkeypatch.setattr(worker.summary_replies, "claim", lambda: next(reply_jobs, None))
    monkeypatch.setattr(worker.offhours_summary, "claim_next_summary",
                        lambda **kw: None if kw.get("interactive") else next(summary_jobs, None))
    monkeypatch.setattr(worker.creator_imports, "claim_next_import", lambda: next(import_jobs, None))
    monkeypatch.setattr(worker.creator_handle_matching, "claim", lambda: next(matching_jobs, None))
    monkeypatch.setattr(worker.creator_responsibility_matching, "claim", lambda: next(responsibility_jobs, None))
    monkeypatch.setattr(worker.creator_group_matching, "claim", lambda: next(group_jobs, None))

    futures = set()
    turns, preference = [], 0
    for _ in range(6):
        dispatched, preference = worker._dispatch_ai_turn(object(), Executor(), futures, preference)
        turns.append(dispatched)

    assert turns == [True, True, True, True, True, False] and len(futures) == 5
    assert [job["id"] for _runner, job in submitted] == [
        "summary-1", "import-1", "matching-1", "responsibility-1", "group-1"
    ]
    interactive = set()
    worker._fill_ai_slots(object(), Executor(), interactive, 0, interactive=True)
    assert len(interactive) == 2 and len(futures) == 5
    assert [job["id"] for _runner, job in submitted[5:]] == ["assistant-1", "reply-1"]
    assert worker._fill_ai_slots(object(), Executor(), interactive, 0, interactive=True) == 0
    futures.clear()
    preference = worker._fill_ai_slots(object(), Executor(), futures, preference)
    assert [job["id"] for _runner, job in submitted[7:]] == [
        "importance-1", "material-1", "summary-2", "summary-3"
    ]
    futures.clear()
    assert worker._fill_ai_slots(object(), Executor(), futures, preference) == preference
    assert len(submitted) == 11

    # Exhausted queues must not query on every busy Lark tick. Completion wakes
    # the dispatcher immediately so a chained summary/reply is not delayed.
    checks = []
    now = [10.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "_fill_ai_slots", lambda *args, **kw: checks.append(now[0]) or args[-1])
    settings = SimpleNamespace(worker_idle_seconds=3)
    futures.clear()
    preference, deadline = worker._schedule_ai(settings, Executor(), futures, preference, 0)
    assert deadline == 13.0 and checks == [10.0]
    now[0] = 10.05
    assert worker._schedule_ai(settings, Executor(), futures, preference, deadline) == (preference, deadline)
    assert checks == [10.0]
    finished = TaskFuture()
    finished.set_result(None)
    futures.add(finished)
    preference, deadline = worker._schedule_ai(settings, Executor(), futures, preference, deadline)
    assert checks == [10.0, 10.05] and not futures
    now[0] = deadline
    worker._schedule_ai(settings, Executor(), futures, preference, deadline)
    assert len(checks) == 3


def test_sync_recovery_runs_once_per_minute_during_busy_ticks(monkeypatch):
    now, calls = [10.0], []
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "_recover_expired_leases", lambda: calls.append(now[0]))
    deadline = worker._schedule_sync_recovery(0)
    for tick in (10.05, 11.0, 69.95):
        now[0] = tick
        assert worker._schedule_sync_recovery(deadline) == deadline
    now[0] = 70.0
    assert worker._schedule_sync_recovery(deadline) == 130.0
    assert calls == [10.0, 70.0]


def test_broadcast_uses_two_independent_slots_and_idle_backoff(monkeypatch):
    now = [10.0]
    jobs = iter(({"id": 1}, {"id": 2}, None, {"id": 3}, None))
    calls, submitted = [], []

    def claim():
        calls.append(now[0])
        return next(jobs)

    class Executor:
        def submit(self, runner, job, settings):
            assert runner is worker.creator_broadcast_delivery.run_claimed
            future = TaskFuture()
            submitted.append((job, future))
            return future

    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker.creator_broadcast_delivery, "claim_next", claim)
    monkeypatch.setattr(worker, "_schedule_ai", lambda *_: pytest.fail("Broadcast must not occupy an AI slot"))
    futures = set()
    config = SimpleNamespace(worker_idle_seconds=3)
    executor = Executor()
    next_check = worker._schedule_broadcast(config, executor, futures, 0)
    assert len(futures) == 2 and next_check == 0 and len(calls) == 2
    assert worker._schedule_broadcast(config, executor, futures, next_check) == 0 and len(calls) == 2
    submitted[0][1].set_result(None)
    next_check = worker._schedule_broadcast(config, executor, futures, next_check)
    assert next_check == 13 and len(futures) == 1
    now[0] = 11
    assert worker._schedule_broadcast(config, executor, futures, next_check) == 13 and len(calls) == 3
    submitted[1][1].set_exception(RuntimeError("safe failure"))
    next_check = worker._schedule_broadcast(config, executor, futures, next_check)
    assert next_check == 14 and [job["id"] for job, _ in submitted] == [1, 2, 3]
    assert len(futures) == 1


def test_creator_previews_are_fair_single_flight_and_back_off_without_work(monkeypatch) -> None:
    now = [10.0]
    submitted = []
    prepared = []
    settings = SimpleNamespace(worker_idle_seconds=3)
    summary_jobs, import_jobs = iter((True, False)), iter((True, False))

    def prepare(name, jobs):
        prepared.append(name)
        return next(jobs, False)

    class Executor:
        def submit(self, runner, argument, next_queue):
            assert runner is worker._prepare_creator_preview
            assert argument is settings
            future = TaskFuture()
            submitted.append((future, runner, next_queue))
            return future

    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker.assistant, "prepare", lambda _: False)
    monkeypatch.setattr(worker.creator_importance, "prepare_next", lambda _: False)
    monkeypatch.setattr(worker.creator_authorization_rescans, "prepare_next", lambda _: False)
    monkeypatch.setattr(worker.offhours_summary, "prepare_next_summary",
                        lambda _: prepare("summary", summary_jobs))
    monkeypatch.setattr(worker.creator_imports, "prepare_next_import",
                        lambda _: prepare("import", import_jobs))
    monkeypatch.setattr(worker.creator_handle_matching, "prepare_next", lambda _: False)
    monkeypatch.setattr(worker.creator_responsibility_matching, "prepare_next", lambda _: False)
    monkeypatch.setattr(worker.creator_group_matching, "prepare_next", lambda _: False)
    executor = Executor()
    future, next_check, queue = worker._schedule_creator_preview(settings, executor, None, 0, 0)
    assert future is submitted[0][0]
    assert worker._schedule_creator_preview(settings, executor, future, next_check, queue) == (
        future, next_check, queue)
    assert len(submitted) == 1 and not prepared

    for index in range(2):
        task, runner, preference = submitted[index]
        task.set_result(runner(settings, preference))
        future, next_check, queue = worker._schedule_creator_preview(settings, executor, future, next_check, queue)
        assert future is submitted[index+1][0]
    assert prepared == ["import", "summary"]
    task, runner, preference = submitted[2]
    task.set_result(runner(settings, preference))
    future, next_check, queue = worker._schedule_creator_preview(settings, executor, future, next_check, queue)
    assert future is None and next_check == 13.0
    assert worker._schedule_creator_preview(settings, executor, future, next_check, queue) == (None, 13.0, queue)
    assert len(submitted) == 3 and prepared == ["import", "summary", "import", "summary"]

    now[0] = 13.0
    future, next_check, queue = worker._schedule_creator_preview(settings, executor, future, next_check, queue)
    assert future is submitted[3][0]
    future.set_exception(RuntimeError("preview failed"))
    assert worker._schedule_creator_preview(settings, executor, future, next_check, queue) == (None, 16.0, queue)

    def failed(_):
        raise RuntimeError("one preparation queue failed")

    monkeypatch.setattr(worker.offhours_summary, "prepare_next_summary", failed)
    monkeypatch.setattr(worker.creator_imports, "prepare_next_import", lambda _: True)
    assert worker._prepare_creator_preview(settings, 2) == (True, 2)

    prepared.clear()
    monkeypatch.setattr(worker.creator_imports, "prepare_next_import", lambda _: prepared.append("import") or True)
    monkeypatch.setattr(worker.offhours_summary, "prepare_next_summary", lambda _: prepared.append("summary") or True)
    monkeypatch.setattr(worker.creator_authorization_rescans, "prepare_next",
                        lambda _: prepared.append("rescan") or True)
    queue = 0
    for _ in range(6):
        progressed, queue = worker._prepare_creator_preview(settings, queue)
        assert progressed
    assert prepared == ["import", "summary", "rescan", "import", "summary", "rescan"]


@pytest.fixture
def creator_rescan(pg, monkeypatch):
    actor = UUID(int=500)
    pg.execute("""INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at)
               VALUES(%s,'ou_rescan_cm','test',NOW()-INTERVAL '30 days')""", (actor,))
    pg.execute("CREATE TABLE rescan_calls(id SERIAL,kind TEXT,target TEXT,reset BOOLEAN)")
    pg.execute("""CREATE OR REPLACE FUNCTION enqueue_creator_importance(target UUID, cm UUID,
               reset BOOLEAN DEFAULT FALSE, message_chat TEXT DEFAULT NULL, message_id TEXT DEFAULT NULL)
               RETURNS UUID LANGUAGE plpgsql AS $$ BEGIN
                 INSERT INTO rescan_calls(kind,target,reset) VALUES('importance',target::text,reset);
                 RETURN NULL;
               END $$""")
    pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
               RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
                 INSERT INTO rescan_calls(kind,target) VALUES('handle',chat);
               END $$""")
    pg.execute("""CREATE OR REPLACE FUNCTION enqueue_creator_group(target TEXT,
               origin TEXT DEFAULT 'incremental', force BOOLEAN DEFAULT FALSE)
               RETURNS UUID LANGUAGE plpgsql AS $$ BEGIN
                 INSERT INTO rescan_calls(kind,target) VALUES('group',target);
                 RETURN NULL;
               END $$""")
    monkeypatch.setattr(creator_authorization_rescans, "connection", lambda: nullcontext(pg))
    return pg, actor


def test_authorization_rescan_resumes_bounded_importance_then_chat_batches(creator_rescan):
    pg, actor = creator_rescan
    people = [UUID(int=1000 + index) for index in range(14)]
    for index, person in enumerate(people):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Creator')", (person,))
        if index < 10 or index == 13:
            pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,
                       started_at,ended_at) VALUES(%s,%s,%s,'manual',NOW()-INTERVAL '1 day',%s)""",
                       (UUID(int=2000 + index), person, actor, None if index < 10 else
                        pg.execute("SELECT NOW() at").fetchone()["at"]))
    # Historical profiles/jobs and current responsibilities share one deduplicated scan.
    for person in (people[0], people[10]):
        pg.execute("INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id) VALUES(%s,%s)",
                   (person, actor))
    pg.execute("INSERT INTO creator_importance_jobs(contact_id,responsible_authorization_id) VALUES(%s,%s)",
               (people[11], actor))
    pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
               VALUES(%s,%s,%s,'manual')""", (UUID(int=3002), people[12], actor))
    for index in range(13):
        chat = f"oc_rescan_{index:02}"
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (actor, chat))
    pg.execute("DELETE FROM rescan_calls")
    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id) VALUES(%s)", (actor,))
    settings = SimpleNamespace()
    assert creator_authorization_rescans.prepare_next(settings)
    pending = pg.execute("SELECT * FROM creator_authorization_rescans").fetchone()
    assert pending["stage"] == "importance" and pending["contact_cursor"] == people[9]
    assert [row["target"] for row in pg.execute("SELECT * FROM rescan_calls ORDER BY id").fetchall()] == [
        str(person) for person in people[:10]]

    # Durable cursors are sufficient for the next worker invocation; no in-memory progress is needed.
    assert creator_authorization_rescans.prepare_next(settings)
    pending = pg.execute("SELECT * FROM creator_authorization_rescans").fetchone()
    assert pending["stage"] == "chats" and pending["contact_cursor"] == people[12]
    calls = pg.execute("SELECT * FROM rescan_calls ORDER BY id").fetchall()
    assert [row["target"] for row in calls] == [str(person) for person in people[:13]]
    assert all(row["reset"] is True for row in calls)
    assert creator_authorization_rescans.prepare_next(settings)
    pending = pg.execute("SELECT chat_cursor FROM creator_authorization_rescans").fetchone()
    assert pending["chat_cursor"] == "oc_rescan_09"
    assert pg.execute("SELECT count(*) n FROM rescan_calls WHERE kind='handle'").fetchone()["n"] == 10
    assert creator_authorization_rescans.prepare_next(settings)
    assert not pg.execute("SELECT 1 FROM creator_authorization_rescans").fetchone()
    chats = pg.execute("SELECT kind,target FROM rescan_calls WHERE kind<>'importance' ORDER BY id").fetchall()
    assert [(row["kind"], row["target"]) for row in chats] == [
        (kind, f"oc_rescan_{index:02}") for index in range(13) for kind in ("handle", "group")]
    assert not creator_authorization_rescans.prepare_next(settings)


def test_authorization_rescan_failure_retries_and_cannot_back_off_a_new_generation(creator_rescan, monkeypatch, caplog):
    pg, actor = creator_rescan
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_retry')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'oc_retry')", (actor,))
    pg.execute("DELETE FROM rescan_calls")
    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id,stage) VALUES(%s,'chats')", (actor,))
    pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
               RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
                 RAISE EXCEPTION 'private fixture body and token';
               END $$""")
    settings = SimpleNamespace()
    assert not creator_authorization_rescans.prepare_next(settings)
    failed = pg.execute("SELECT *,not_before>NOW() deferred FROM creator_authorization_rescans").fetchone()
    assert failed["deferred"] and failed["last_error_code"] == "P0001"
    assert failed["stage"] == "chats" and failed["chat_cursor"] is None
    assert "private fixture body" not in caplog.text and "token" not in caplog.text
    assert not creator_authorization_rescans.prepare_next(settings)
    pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
               RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
                 INSERT INTO rescan_calls(kind,target) VALUES('handle',chat);
               END $$""")
    pg.execute("UPDATE creator_authorization_rescans SET not_before=NOW()")
    assert creator_authorization_rescans.prepare_next(settings)
    assert not pg.execute("SELECT 1 FROM creator_authorization_rescans").fetchone()
    assert [row["kind"] for row in pg.execute("SELECT kind FROM rescan_calls ORDER BY id").fetchall()] == [
        "handle", "group"]

    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id,generation,stage) VALUES(%s,7,'chats')",
               (actor,))
    pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
               RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'safe fixture failure'; END $$""")
    connections = 0

    def connection():
        nonlocal connections
        connections += 1
        if connections == 2:
            # A new authorization arrives after rollback, before recording the old batch failure.
            pg.execute("UPDATE authorizations SET scopes=array_append(scopes,'new-scope') WHERE id=%s", (actor,))
        return nullcontext(pg)

    monkeypatch.setattr(creator_authorization_rescans, "connection", connection)
    assert not creator_authorization_rescans.prepare_next(settings)
    pending = pg.execute("SELECT *,not_before<=NOW() due FROM creator_authorization_rescans").fetchone()
    assert pending["generation"] == 8 and pending["stage"] == "importance" and pending["due"]
    assert pending["last_error_code"] == "" and pending["chat_cursor"] is None

    pg.execute("DELETE FROM creator_authorization_rescans")
    old_request = pg.execute("""INSERT INTO creator_authorization_rescans(authorization_id,stage)
                            VALUES(%s,'chats') RETURNING request_id""", (actor,)).fetchone()["request_id"]
    connections = 0

    def replace_request_connection():
        nonlocal connections
        connections += 1
        if connections == 2:
            # Completion and a new OAuth can reuse generation 1 after the failed transaction releases its lock.
            pg.execute("DELETE FROM creator_authorization_rescans")
            pg.execute("INSERT INTO creator_authorization_rescans(authorization_id) VALUES(%s)", (actor,))
        return nullcontext(pg)

    monkeypatch.setattr(creator_authorization_rescans, "connection", replace_request_connection)
    assert not creator_authorization_rescans.prepare_next(settings)
    pending = pg.execute("SELECT *,not_before<=NOW() due FROM creator_authorization_rescans").fetchone()
    assert pending["generation"] == 1 and pending["request_id"] != old_request
    assert pending["stage"] == "importance" and pending["due"] and pending["last_error_code"] == ""


@pytest.mark.parametrize("stage", ("importance", "chats"))
def test_authorization_rescan_old_generation_cannot_advance_or_delete_a_new_request(creator_rescan, stage):
    pg, actor = creator_rescan
    person = UUID(int=1000)
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Creator')", (person,))
    pg.execute("INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id) VALUES(%s,%s)",
               (person, actor))
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_generation')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'oc_generation')", (actor,))
    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id,generation,stage) VALUES(%s,7,%s)",
               (actor, stage))
    if stage == "importance":
        pg.execute("""CREATE OR REPLACE FUNCTION enqueue_creator_importance(target UUID, cm UUID,
                   reset BOOLEAN DEFAULT FALSE, message_chat TEXT DEFAULT NULL, message_id TEXT DEFAULT NULL)
                   RETURNS UUID LANGUAGE plpgsql AS $$ BEGIN
                     UPDATE authorizations SET status='attention_required' WHERE id=cm;
                     RETURN NULL;
                   END $$""")
    else:
        pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
                   RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
                     UPDATE authorizations SET status='attention_required' WHERE open_id='ou_rescan_cm';
                   END $$""")
    assert creator_authorization_rescans.prepare_next(SimpleNamespace())
    pending = pg.execute("SELECT * FROM creator_authorization_rescans").fetchone()
    assert pending["generation"] == 8 and pending["stage"] == "importance"
    assert pending["contact_cursor"] is pending["chat_cursor"] is None and pending["last_error_code"] == ""


def test_authorization_rescan_serializes_workers_without_blocking_reauthorization(creator_rescan, monkeypatch):
    pg, actor = creator_rescan
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_concurrent')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'oc_concurrent')", (actor,))
    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id,generation,stage) VALUES(%s,7,'chats')",
               (actor,))
    guard = 913374005
    pg.execute("""CREATE OR REPLACE FUNCTION creator_handle_chat_signal(chat TEXT)
               RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
                 PERFORM set_config('lock_timeout','5s',TRUE);
                 PERFORM pg_advisory_xact_lock(913374005);
                 INSERT INTO rescan_calls(kind,target) VALUES('handle',chat);
               END $$""")
    schema = pg.execute("SELECT current_schema() name").fetchone()["name"]
    worker_pid = pg.info.backend_pid
    # Two real connections need committed fixtures; remove this isolated schema ourselves below.
    pg.commit()
    try:
        pg.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        pg.commit()
        with psycopg.connect(pg.info.dsn, row_factory=dict_row) as other:
            other.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
            other.execute("SELECT pg_advisory_lock(%s)", (guard,))
            other.commit()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(creator_authorization_rescans.prepare_next, SimpleNamespace())
                try:
                    deadline = time.monotonic() + 3
                    while not other.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks
                                             WHERE pid=%s AND locktype='advisory' AND NOT granted) waiting""",
                                             (worker_pid,)).fetchone()["waiting"]:
                        assert time.monotonic() < deadline, "rescan did not reach its chat signal"
                        time.sleep(0.01)
                    monkeypatch.setattr(creator_authorization_rescans, "connection", lambda: nullcontext(other))
                    assert not creator_authorization_rescans.prepare_next(SimpleNamespace())
                    other.execute("SET LOCAL lock_timeout='500ms'")
                    other.execute("UPDATE authorizations SET scopes=array_append(scopes,'new-scope') WHERE id=%s",
                                  (actor,))
                    other.commit()
                finally:
                    other.rollback()
                    other.execute("SELECT pg_advisory_unlock(%s)", (guard,))
                    other.commit()
                assert future.result(timeout=10)
        pending = pg.execute("SELECT * FROM creator_authorization_rescans").fetchone()
        assert pending["generation"] == 8 and pending["stage"] == "importance"
        assert pending["contact_cursor"] is pending["chat_cursor"] is None
        assert pg.execute("SELECT count(*) n FROM rescan_calls WHERE kind='handle'").fetchone()["n"] == 1
    finally:
        pg.rollback()
        pg.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        pg.commit()












def test_lark_dispatch_uses_idle_slot_when_other_user_has_no_backlog(monkeypatch) -> None:
    submitted = []
    authorization_a = UUID(int=10)
    authorization_b = UUID(int=20)
    job_a = worker.SyncJob(UUID(int=1), authorization_a, "backfill", 0, "succeeded", 0)
    job_b = worker.SyncJob(UUID(int=2), authorization_b, "backfill", 0, "succeeded", 0)
    remaining = iter(("oc_a_1", "oc_a_2", "oc_a_3"))

    class Future:
        pass

    class Executor:
        def submit(self, _runner, _client, claim, _token):
            submitted.append(claim)
            return Future()

    def claim_next(job, *_args):
        if job.authorization_id == authorization_b:
            return None
        return SimpleNamespace(job=job, chat_id=next(remaining))

    monkeypatch.setattr(worker, "_authorization", lambda _id: {"status": "active", "open_id": "ou_cm"})
    monkeypatch.setattr(worker, "_usable_access_token", lambda *_args: "token")
    monkeypatch.setattr(worker, "_claim_next_page", claim_next)
    monkeypatch.setattr(
        worker,
        "_active_page_claim_counts",
        lambda _ids: Counter(claim.job.authorization_id for claim in submitted),
    )
    settings = SimpleNamespace(lark_sync_max_inflight=3, lark_sync_per_authorization_max_inflight=2)
    futures = {}
    cache = {}

    for _ in range(3):
        assert worker._dispatch_lark_turn(settings, object(), Executor(), futures, [job_a, job_b], cache)

    assert [claim.chat_id for claim in submitted] == ["oc_a_1", "oc_a_2", "oc_a_3"]


def test_all_due_accounts_rotate_through_three_page_slots_and_resume_cursors(pg, monkeypatch):
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(worker, "_usable_access_token", lambda *_args: "test-token")
    settings = SimpleNamespace(lark_sync_max_inflight=3, lark_sync_per_authorization_max_inflight=2,
                               sync_overlap_seconds=600, sync_lookback_days=0, sync_interval_seconds=300)
    jobs = []
    for index in range(1, 8):
        job_id, actor_id = UUID(int=index), UUID(int=100 + index)
        jobs.append((job_id, actor_id))
        pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
                   (actor_id, f"ou_fair_{index}"))
        pg.execute("""INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status,
                   started_at,lease_until,last_progress_at,scheduled_at)
                   VALUES(%s,%s,'incremental',%s,%s,NOW()-INTERVAL '2 hours',
                   NOW()+INTERVAL '10 minutes',NOW()-INTERVAL '1 minute',NOW()-INTERVAL '1 hour')""",
                   (job_id, actor_id, "running" if index <= 3 else "queued",
                    "succeeded" if index <= 3 else "pending"))
        for chat_index in range(10 if index <= 3 else 1):
            chat_id = f"oc_fair_{index}_{chat_index}"
            pg.execute("INSERT INTO chats(chat_id,chat_status) VALUES(%s,'normal')", (chat_id,))
            pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,
                       last_backfill_at,sync_status) VALUES(%s,%s,TRUE,NOW()-INTERVAL '1 day','succeeded')""",
                       (actor_id, chat_id))

    prepared = []

    def prepare(job, *_args):
        prepared.append(job.id)
        current = worker._begin_discovery(job)
        if current:
            worker._complete_discovery(current, 1)

    monkeypatch.setattr(worker, "_prepare_job", prepare)
    for index in range(4):
        assert worker._start_pending_jobs(settings, object())
        assert len(prepared) == index + 1  # Only one discovery per turn.
    assert len(worker._running_jobs()) == 7
    assert not worker._start_pending_jobs(settings, object())

    first_job, first_actor = jobs[0]
    pg.execute("""UPDATE authorization_chats SET incremental_job_id=%s,
               incremental_page_token='resume-page-2',incremental_window_end_at=NOW(),
               incremental_window_start_at=NOW()-INTERVAL '1 day'
               WHERE authorization_id=%s AND chat_id='oc_fair_1_0'""", (first_job, first_actor))
    interrupted_job = next(job for job in worker._running_jobs() if job.id == first_job)
    interrupted = worker._claim_next_page(interrupted_job, {"open_id": "ou_fair_1"}, settings)
    assert interrupted.expected_page_token == "resume-page-2"
    pg.execute("UPDATE sync_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (first_job,))
    worker._recover_expired_leases()
    assert pg.execute("SELECT page_claim_id FROM authorization_chats WHERE chat_id=%s",
                      (interrupted.chat_id,)).fetchone()["page_claim_id"] is None
    assert worker._start_pending_jobs(settings, object())
    assert pg.execute("""SELECT incremental_page_token FROM authorization_chats
                         WHERE authorization_id=%s AND chat_id='oc_fair_1_0'""",
                      (first_actor,)).fetchone()["incremental_page_token"] == "resume-page-2"

    executor = SimpleNamespace(submit=lambda *_args: TaskFuture())
    seen = set()
    resumed = False
    for _ in range(3):
        futures = {}
        worker._fill_lark_slots(settings, object(), executor, futures)
        assert len(futures) == 3
        with monkeypatch.context() as patch:
            patch.setattr(worker, "_running_jobs", lambda: pytest.fail("Full slots must not query for more work"))
            worker._fill_lark_slots(settings, object(), executor, futures)
        assert max(Counter(claim.job.authorization_id for claim in futures.values()).values()) <= 2
        for claim in futures.values():
            seen.add(claim.job.authorization_id)
            resumed |= claim.expected_page_token == "resume-page-2"
            assert worker._record_claimed_page(claim, [], None, None).committed
            worker._add_job_progress(claim.job, pages=1, messages=0, own_messages=0)
            # Fixture transactions keep NOW() fixed; advance fair ordering explicitly.
            pg.execute("UPDATE sync_jobs SET last_progress_at=clock_timestamp() WHERE id=%s", (claim.job.id,))
    assert seen == {actor for _job, actor in jobs}
    assert resumed

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(sync_interval_seconds=300))
    job = next(job for job in worker._running_jobs() if job.authorization_id == jobs[-1][1])
    authorization = {"open_id": "ou_fair_7", "body_consent_at": None}
    pg.execute("INSERT INTO chats(chat_id,chat_status) VALUES('oc_restricted','normal')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,
               last_backfill_at,last_incremental_at,sync_status,incremental_job_id,incremental_page_token,
               incremental_window_start_at,incremental_window_end_at)
               VALUES(%s,'oc_restricted',TRUE,NOW()-INTERVAL '2 days',NOW()-INTERVAL '1 day','succeeded',
                      %s,'keep-page-2',NOW()-INTERVAL '1 day',NOW())""", (job.authorization_id, job.id))
    before = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_restricted'").fetchone()
    claim = worker._claim_next_page(job, authorization, settings)
    assert claim.chat_id == "oc_restricted" and claim.expected_page_token == "keep-page-2"
    error = worker.LarkApiError(403, 231203, "Restricted Mode; raw_private_value must not be persisted")
    worker._handle_claim_api_error(claim, error)
    blocked = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_restricted'").fetchone()
    assert blocked["sync_status"] == "not_readable"
    assert blocked["last_error"] == "群开启受限模式，Lark禁止读取消息（231203）"
    assert blocked["page_claim_id"] is None
    assert blocked["page_retry_at"] == pg.execute("SELECT NOW()+INTERVAL '1 day' AS due").fetchone()["due"]
    for field in ("backfill_complete", "last_backfill_at", "last_incremental_at", "incremental_page_token",
                  "incremental_window_start_at", "incremental_window_end_at"):
        assert blocked[field] == before[field]
    assert pg.execute("SELECT status FROM authorizations WHERE id=%s", (job.authorization_id,)).fetchone()[
        "status"] == "active"
    assert not worker._has_claimable_page_in_connection(pg, job)
    assert not worker._yield_job_to_retry_if_idle(pg, job)
    worker._finish_and_schedule_incremental(job)
    assert pg.execute("SELECT status FROM sync_jobs WHERE id=%s", (job.id,)).fetchone()["status"] == "succeeded"
    following = pg.execute("SELECT * FROM sync_jobs WHERE authorization_id=%s AND status='queued'",
                           (job.authorization_id,)).fetchone()
    assert following["scheduled_at"] == pg.execute("SELECT NOW()+INTERVAL '300 seconds' AS due").fetchone()["due"]
    pg.execute("""UPDATE sync_jobs SET status='running',discovery_status='succeeded',
               lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s""", (following["id"],))
    next_job = next(job for job in worker._running_jobs() if job.id == following["id"])
    # Legacy rows without a retry time are rechecked once, not permanently skipped.
    pg.execute("UPDATE authorization_chats SET page_retry_at=NULL WHERE chat_id='oc_restricted'")
    recovered = worker._claim_next_page(next_job, authorization, settings)
    assert recovered.chat_id == "oc_restricted" and recovered.mode == "incremental"
    assert recovered.start_at == before["last_incremental_at"] - worker.timedelta(seconds=600)
    pending = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_restricted'").fetchone()
    assert pending["sync_status"] == "not_readable" and pending["last_error"] == blocked["last_error"]
    worker._handle_claim_api_error(claim, error)  # A late failure cannot replace the new lease.
    assert pg.execute("SELECT page_claim_id FROM authorization_chats WHERE chat_id='oc_restricted'").fetchone()[
        "page_claim_id"] == recovered.claim_id
    assert worker._record_claimed_page(recovered, [], None, None).committed
    restored = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_restricted'").fetchone()
    assert restored["sync_status"] == "succeeded" and restored["last_error"] is None
    assert restored["page_retry_at"] is None and restored["incremental_page_token"] is None

    # A denied initial backfill keeps its original cursor without falsely completing history.
    pg.execute("INSERT INTO chats(chat_id,chat_status) VALUES('oc_backfill_denied','normal')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_page_token,
               backfill_window_start_at,backfill_window_end_at)
               VALUES(%s,'oc_backfill_denied','keep-history-page',NOW()-INTERVAL '7 days',NOW())""",
               (job.authorization_id,))
    denied = worker._claim_next_page(next_job, authorization, settings)
    assert denied.mode == "backfill" and denied.expected_page_token == "keep-history-page"
    worker._handle_claim_api_error(denied, worker.LarkApiError(400, 231203, "Other refusal; private raw body"))
    denied_row = pg.execute("SELECT * FROM authorization_chats WHERE chat_id='oc_backfill_denied'").fetchone()
    assert not denied_row["backfill_complete"] and denied_row["backfill_page_token"] == "keep-history-page"
    assert denied_row["last_error"] == "Lark暂不允许读取此会话消息（错误码 231203）"
    assert not worker._has_pending_backfill_in_connection(pg, job.authorization_id)
    assert worker._select_backfill_chat_for_claim(pg, job.authorization_id) is None
    worker._finish_and_schedule_incremental(next_job)
    assert pg.execute("SELECT status FROM sync_jobs WHERE id=%s", (next_job.id,)).fetchone()["status"] == "succeeded"
    pg.execute("UPDATE authorization_chats SET page_retry_at=NOW() WHERE chat_id='oc_backfill_denied'")
    assert worker._has_pending_backfill_in_connection(pg, job.authorization_id)
    assert worker._select_backfill_chat_for_claim(pg, job.authorization_id)["chat_id"] == "oc_backfill_denied"


def test_discovery_is_single_flight_and_does_not_block_page_reaping(monkeypatch):
    settings = SimpleNamespace(worker_idle_seconds=3, token_encryption_key=b"r" * 32)
    submitted = []
    executor = SimpleNamespace(submit=lambda *args: submitted.append(args) or TaskFuture())
    future, next_check = worker._schedule_discovery(settings, object(), executor, None, 0)
    assert len(submitted) == 1 and not future.done()
    assert worker._schedule_discovery(settings, object(), executor, future, next_check) == (future, next_check)
    assert len(submitted) == 1

    page = TaskFuture()
    page.set_result({"items": []})
    claim = SimpleNamespace(body_consent_at=None, job="page-job")
    pages, progressed = {page: claim}, []
    monkeypatch.setattr(worker, "_record_claimed_page", lambda *_args, **_kwargs: worker.PageCommit(True))
    monkeypatch.setattr(worker, "_add_job_progress", lambda job, **_kwargs: progressed.append(job))
    worker._reap_page_futures(pages, settings)
    assert pages == {} and progressed == ["page-job"] and not future.done()

    monkeypatch.setattr(worker.time, "monotonic", lambda: 100)
    future.set_exception(RuntimeError("synthetic discovery failure"))
    assert worker._schedule_discovery(settings, object(), executor, future, next_check) == (None, 103)
    assert len(submitted) == 1


def test_new_coordinator_keeps_revocation_and_discovery_retry_fences(pg, monkeypatch):
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(worker, "_usable_access_token", lambda *_args: "test-token")
    monkeypatch.setattr(worker, "_refresh_profile_name", lambda *_args: None)
    actor, job_id = UUID(int=900), UUID(int=901)
    pg.execute("""INSERT INTO authorizations(id,open_id,status,access_token_ciphertext)
               VALUES(%s,'ou_discovery_fence','revoked','test')""", (actor,))
    pg.execute("""INSERT INTO sync_jobs(id,authorization_id,mode,status)
               VALUES(%s,%s,'incremental','queued')""", (job_id, actor))
    settings = SimpleNamespace()
    assert worker._start_pending_jobs(settings, object())
    assert pg.execute("SELECT status FROM sync_jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "cancelled"

    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (actor,))
    pg.execute("UPDATE sync_jobs SET status='queued',discovery_status='pending' WHERE id=%s", (job_id,))
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_discovery_resume')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_page_token)
               VALUES(%s,'oc_discovery_resume','keep-page-3')""", (actor,))

    def temporary_failure(*_args, **_kwargs):
        raise worker.LarkApiError(503, None, "synthetic unavailable", retry_after=0)

    client = SimpleNamespace(list_chats=temporary_failure)
    assert worker._start_pending_jobs(settings, client)
    assert pg.execute("SELECT status,discovery_status,discovery_generation FROM sync_jobs WHERE id=%s",
                      (job_id,)).fetchone() == {"status": "queued", "discovery_status": "pending",
                                              "discovery_generation": 1}
    assert pg.execute("SELECT backfill_page_token FROM authorization_chats WHERE authorization_id=%s",
                      (actor,)).fetchone()["backfill_page_token"] == "keep-page-3"
    pg.execute("UPDATE authorizations SET body_consent_at=NOW(),p2p_discovery_status='unavailable',"
               "p2p_last_checked_at=NOW()-INTERVAL '2 hours' WHERE id=%s", (actor,))
    p2p_calls = []
    available = False

    def conversations(_token, _page=None, *, types=None):
        if types == "p2p":
            p2p_calls.append(types)
            if not available:
                raise worker.LarkApiError(503, None, "temporary P2P outage")
            return {"items": []}
        return {"items": [{"chat_id": "oc_discovery_resume"}]}

    client.list_chats = conversations
    for state, checked, count in (("unavailable", "NOW()-INTERVAL '2 hours'", 1),
                                   ("unavailable", "NOW()", 1),
                                   ("unavailable", "NOW()-INTERVAL '2 hours'", 2),
                                   ("attention_required", "NOW()-INTERVAL '2 hours'", 2)):
        pg.execute("UPDATE sync_jobs SET status='running',discovery_status='pending',"
                   "lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s", (job_id,))
        pg.execute(f"UPDATE authorizations SET p2p_discovery_status=%s,p2p_last_checked_at={checked} WHERE id=%s",
                   (state, actor))
        assert worker._start_pending_jobs(settings, client)
        assert len(p2p_calls) == count
        assert pg.execute("SELECT discovery_status FROM sync_jobs WHERE id=%s", (job_id,)).fetchone()[
            "discovery_status"] == "succeeded"
        available = True
    assert pg.execute("SELECT backfill_page_token FROM authorization_chats WHERE authorization_id=%s", (actor,)
                      ).fetchone()["backfill_page_token"] == "keep-page-3"




def test_delayed_only_job_yields_its_coordinator_slot(monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(sync_interval_seconds=300))
    calls = []
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "backfill", 0, "succeeded", 3)

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, params=None):
            calls.append((query, params))
            if "COUNT(*) AS count" in query:
                return Result({"count": 0})
            if "AS pending" in query:
                return Result({"pending": False})
            if "AS ready" in query:
                return Result({"ready": False})
            if "MIN(page_retry_at)" in query:
                return Result({"retry_at": worker.datetime.now(worker.UTC)})
            return Result()

    assert worker._yield_job_to_retry_if_idle(Connection(), job)

    assert any("SET status = 'queued', scheduled_at = %s" in query for query, _params in calls)


def test_member_snapshot_runs_alongside_message_work_with_one_roster_slot(monkeypatch):
    claim = SimpleNamespace(authorization_id="cm", chat_id="group")
    monkeypatch.setattr(worker, "_claim_member_snapshot", lambda settings: claim)
    monkeypatch.setattr(worker, "_authorization", lambda aid: {"status": "active"})
    monkeypatch.setattr(worker, "_usable_access_token", lambda *args: "token")
    submitted = []
    executor = SimpleNamespace(submit=lambda *args: submitted.append(args) or "future")
    settings = SimpleNamespace(oauth_scopes=["im:chat.members:read"])
    members = {}
    worker._schedule_member_snapshot(settings, "client", executor, {"message": "running"}, members)
    assert members == {"future": claim} and len(submitted) == 1
    worker._schedule_member_snapshot(settings, "client", executor, {"message": "running"}, members)
    assert len(submitted) == 1


def test_member_commit_error_releases_exact_claim_for_bounded_retry(monkeypatch):
    from psycopg.errors import DeadlockDetected

    claim = worker.MemberSnapshotClaim(UUID(int=1), "fixture-group", 7, UUID(int=2))
    future = TaskFuture()
    future.set_result(worker.MemberSnapshotMeasurement(1, "succeeded", "succeeded", None))
    futures = {future: claim}
    released = []

    def fail_store(*_):
        raise DeadlockDetected("fixture-only")

    monkeypatch.setattr(worker, "_store_claimed_member_snapshot", fail_store)
    monkeypatch.setattr(worker, "_release_member_snapshot_claim", lambda *args: released.append(args))
    worker._reap_member_futures(futures)
    assert futures == {}
    assert released == [(claim, "pending")]
