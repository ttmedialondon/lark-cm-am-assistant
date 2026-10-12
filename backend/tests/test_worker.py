from collections import Counter
from concurrent.futures import Future as TaskFuture
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import worker
from app.crypto import TokenCipher
from app.lark_api import TokenBundle
from app.worker import (
    _body_content,
    _is_author_message,
    _lark_time,
    _message_relation_metadata,
    _p2p_counterpart_open_id,
    _read_member_snapshot,
    _sender_metadata,
    _tenant_classification,
)


def test_author_message_requires_open_id_semantics() -> None:
    message = {"sender": {"sender_type": "user", "id_type": "open_id", "id": "ou_me"}}
    assert _is_author_message(message, "ou_me")
    assert not _is_author_message({"sender": {"sender_type": "user", "id": "ou_me"}}, "ou_me")


def test_lark_time_interprets_milliseconds() -> None:
    timestamp = _lark_time("1615380573411")
    assert timestamp is not None
    assert timestamp.year == 2021


def test_message_body_content_is_kept_as_the_lark_payload() -> None:
    assert _body_content({"body": {"content": '{"text":"hello"}'}}) == '{"text":"hello"}'
    assert _body_content({"body": {}}) is None


def test_sender_metadata_keeps_open_id_only_when_lark_marks_it_as_open_id() -> None:
    user = {
        "sender": {
            "id": "ou_sender",
            "id_type": "open_id",
            "tenant_key": "tenant_external",
            "sender_type": "user",
            "sender_name": "Creator",
        }
    }
    app = {"sender": {"id": "cli_app", "id_type": "app_id", "sender_type": "app"}}

    assert _sender_metadata(user) == (
        "ou_sender",
        "open_id",
        "ou_sender",
        "tenant_external",
        "user",
        "Creator",
    )
    assert _sender_metadata(app)[2] is None


def test_same_sender_name_keeps_distinct_lark_identity_keys() -> None:
    first = _sender_metadata(
        {"sender": {"id": "ou_first", "id_type": "open_id", "tenant_key": "tenant_a", "sender_name": "Alex"}}
    )
    second = _sender_metadata(
        {"sender": {"id": "ou_second", "id_type": "open_id", "tenant_key": "tenant_b", "sender_name": "Alex"}}
    )

    assert first[2:] == ("ou_first", "tenant_a", None, "Alex")
    assert second[2:] == ("ou_second", "tenant_b", None, "Alex")


def test_message_relationship_metadata_keeps_only_context_fields() -> None:
    relations = _message_relation_metadata(
        {
            "root_id": "om_root",
            "parent_id": "om_parent",
            "thread_id": "omt_thread",
            "message_app_link": "https://applink.larksuite.com/client/chat/open?openChatId=oc_test",
            "message_position": "12",
            "thread_message_position": 3,
            "mentions": [
                {
                    "key": "@_user_1",
                    "id": "ou_mentioned",
                    "id_type": "open_id",
                    "name": "Mentioned person",
                    "tenant_key": "tenant_external",
                    "ignored": "not stored",
                },
                "not-a-mention",
            ],
        }
    )

    assert relations == {
        "root_id": "om_root",
        "parent_id": "om_parent",
        "thread_id": "omt_thread",
        "mentions": [
            {
                "key": "@_user_1",
                "id": "ou_mentioned",
                "id_type": "open_id",
                "name": "Mentioned person",
                "tenant_key": "tenant_external",
            }
        ],
        "message_app_link": "https://applink.larksuite.com/client/chat/open?openChatId=oc_test",
        "message_position": 12,
        "thread_message_position": 3,
    }


def test_tenant_classification_keeps_unknown_people_neutral() -> None:
    internal_tenants = {"tenant_tt_media"}

    assert _tenant_classification(
        tenant_key="tenant_tt_media",
        identity_type="user",
        internal_tenant_keys=internal_tenants,
    ) == "internal"
    assert _tenant_classification(
        tenant_key=None,
        identity_type="user",
        internal_tenant_keys=internal_tenants,
    ) == "unresolved"
    assert _tenant_classification(
        tenant_key="tenant_external",
        identity_type="user",
        internal_tenant_keys=internal_tenants,
    ) == "known_external"


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


def test_full_ai_slots_do_not_attempt_another_claim(monkeypatch) -> None:
    class Future:
        pass

    monkeypatch.setattr(
        worker,
        "_dispatch_ai_turn",
        lambda *_args: pytest.fail("a full AI pool must not claim another task"),
    )

    assert worker._fill_ai_slots(object(), object(), {Future() for _ in range(5)}, 2) == 2


def test_dispatch_uses_at_most_five_slots_and_fairly_rotates_all_eight_queues(monkeypatch) -> None:
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
    monkeypatch.setattr(worker.assistant, "claim", lambda: next(assistant_jobs, None))
    monkeypatch.setattr(worker.creator_importance, "claim", lambda: next(importance_jobs, None))
    monkeypatch.setattr(worker.summary_replies, "claim", lambda: next(reply_jobs, None))
    monkeypatch.setattr(worker.offhours_summary, "claim_next_summary", lambda: next(summary_jobs, None))
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
        "assistant-1", "summary-1", "import-1", "reply-1", "matching-1"
    ]
    assert submitted[0][0] is worker.assistant.run
    assert submitted[4][0] is worker.creator_handle_matching.run
    futures.clear()
    preference = worker._fill_ai_slots(object(), Executor(), futures, preference)
    assert preference == 2
    assert [job["id"] for _runner, job in submitted[5:]] == [
        "responsibility-1", "group-1", "importance-1", "summary-2", "summary-3"
    ]
    assert submitted[6][0] is worker.creator_group_matching.run
    futures.clear()
    assert worker._fill_ai_slots(object(), Executor(), futures, preference) == 2
    assert [job["id"] for _runner, job in submitted[10:]] == []

    # Exhausted queues must not query on every busy Lark tick. Completion wakes
    # the dispatcher immediately so a chained summary/reply is not delayed.
    checks = []
    now = [10.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "_fill_ai_slots", lambda *args: checks.append(now[0]) or args[-1])
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


def test_offhours_tasks_fill_five_parallel_slots_without_waiting(monkeypatch) -> None:
    jobs = iter({"id": index} for index in range(5))
    submitted = []

    class Executor:
        def submit(self, runner, job, settings):
            assert runner is worker.offhours_summary.run_claimed_summary
            submitted.append(job["id"])
            return object()

    monkeypatch.setattr(worker.offhours_summary, "claim_next_summary", lambda: next(jobs, None))
    monkeypatch.setattr(worker.creator_imports, "claim_next_import", lambda: None)
    monkeypatch.setattr(worker.summary_replies, "claim", lambda: None)
    monkeypatch.setattr(worker.creator_handle_matching, "claim", lambda: None)
    monkeypatch.setattr(worker.creator_responsibility_matching, "claim", lambda: None)
    monkeypatch.setattr(worker.creator_group_matching, "claim", lambda: None)
    futures = set()
    worker._fill_ai_slots(object(), Executor(), futures, 0)
    assert submitted == [0, 1, 2, 3, 4]
    assert len(futures) == 5


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


def test_page_claim_uses_locked_conversation_cursor(monkeypatch) -> None:
    calls = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT mode" in query:
                return Result({"mode": "backfill"})
            if "SELECT COUNT(*) AS count" in query:
                return Result({"count": 0})
            if "manual_backfill_started_at IS NULL" in query:
                return Result()
            if "SELECT access.*" in query:
                return Result(
                    {
                        "chat_id": "oc_claim",
                        "sync_generation": 4,
                        "backfill_window_start_at": None,
                        "backfill_window_end_at": None,
                        "manual_backfill_requested_at": None,
                        "backfill_page_token": "page-2",
                    }
                )
            return Result()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "backfill", 0, "succeeded", 3)

    claim = worker._claim_next_page(
        job,
        {"open_id": "ou_cm", "body_consent_at": None},
        SimpleNamespace(
            sync_lookback_days=0,
            sync_overlap_seconds=600,
            lark_sync_per_authorization_max_inflight=2,
        ),
    )

    assert claim is not None
    assert claim.chat_id == "oc_claim"
    assert claim.expected_page_token == "page-2"
    sql = "\n".join(query for query, _params in calls)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "page_claim_id = %s, page_claim_job_id = %s, page_claim_mode = %s" in sql


def test_page_claim_respects_the_durable_per_authorization_limit(monkeypatch) -> None:
    calls = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT mode" in query:
                return Result({"mode": "backfill"})
            if "SELECT COUNT(*) AS count" in query:
                return Result({"count": 2})
            return Result()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "backfill", 0, "succeeded", 3)

    claim = worker._claim_next_page(
        job,
        {"open_id": "ou_cm", "body_consent_at": None},
        SimpleNamespace(sync_lookback_days=0, sync_overlap_seconds=600),
        2,
    )

    assert claim is None
    assert not any("SELECT access.*" in query for query, _params in calls)


def test_stale_page_claim_does_not_write_messages_after_reauthorization(monkeypatch) -> None:
    calls = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "FROM sync_jobs" in query:
                return Result({"present": 1})
            if "FROM authorization_chats" in query:
                return Result()
            return Result()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "backfill", 0, "succeeded", 3)
    claim = worker.PageClaim(
        job=job,
        chat_id="oc_stale",
        mode="backfill",
        generation=9,
        claim_id=UUID(int=3),
        expected_page_token="old-page",
        start_at=None,
        end_at=worker.datetime.now(worker.UTC),
        author_open_id="ou_cm",
        body_consent_at=None,
    )

    result = worker._record_claimed_page(claim, [{"message_id": "om_stale"}], None, None)

    assert result.committed is False
    assert not any("INSERT INTO chat_messages" in query for query, _params in calls)
    assert any("page_lease_until > NOW()" in query for query, _params in calls)


def test_expired_lease_releases_claims_without_resetting_cursors(monkeypatch) -> None:
    calls = []

    class Result:
        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchall(self):
            return self.rows

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "UPDATE sync_jobs" in query:
                return Result([{"id": UUID(int=1)}])
            return Result()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))

    worker._recover_expired_leases()

    claim_updates = [query for query, _params in calls if "page_claim_id = NULL" in query]
    assert claim_updates
    assert all("backfill_page_token" not in query for query in claim_updates)
    assert any("page_lease_until < NOW()" in query for query in claim_updates)


def test_lark_dispatch_favors_users_then_claims_distinct_chats(monkeypatch) -> None:
    submitted = []
    authorization_a = UUID(int=10)
    authorization_b = UUID(int=20)
    job_a = worker.SyncJob(UUID(int=1), authorization_a, "backfill", 0, "succeeded", 0)
    job_b = worker.SyncJob(UUID(int=2), authorization_b, "backfill", 0, "succeeded", 0)
    remaining = {
        authorization_a: iter(("oc_a_1", "oc_a_2")),
        authorization_b: iter(("oc_b_1",)),
    }

    class Future:
        pass

    class Executor:
        def submit(self, _runner, _client, claim, _token):
            submitted.append(claim)
            return Future()

    def claim_next(job, *_args):
        try:
            chat_id = next(remaining[job.authorization_id])
        except StopIteration:
            return None
        return SimpleNamespace(job=job, chat_id=chat_id)

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

    assert [claim.job.authorization_id for claim in submitted] == [authorization_a, authorization_b, authorization_a]
    chats_for_a = [claim.chat_id for claim in submitted if claim.job.authorization_id == authorization_a]
    assert chats_for_a == ["oc_a_1", "oc_a_2"]


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
                               sync_overlap_seconds=600, sync_lookback_days=0)
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
    pg.execute("UPDATE sync_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (first_job,))
    worker._recover_expired_leases()
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


def test_completed_backfill_enqueues_a_separate_incremental_job(monkeypatch) -> None:
    calls = []
    job = worker.SyncJob(UUID(int=1), UUID(int=2), "backfill", 0, "succeeded", 3)

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT mode" in query:
                return Result({"mode": "backfill"})
            if "AS claimed" in query:
                return Result({"claimed": False})
            if "AS pending" in query:
                return Result({"pending": False})
            if "SET status = 'succeeded'" in query:
                return Result({"id": job.id})
            return Result()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(sync_interval_seconds=300))

    worker._finish_and_schedule_incremental(job)

    sql = "\n".join(query for query, _params in calls)
    assert "SET status = 'succeeded'" in sql
    assert "INSERT INTO sync_jobs" in sql
    assert "incremental_job_id IS DISTINCT" not in sql


def test_delayed_only_job_yields_its_coordinator_slot() -> None:
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
