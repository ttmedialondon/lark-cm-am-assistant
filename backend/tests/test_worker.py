from collections import Counter
from concurrent.futures import Future as TaskFuture
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import worker
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
    for _ in range(2):
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

    client.target_id, client.target_type = "cli_bot", "bot"
    worker._discover_chats(
        job, {"id": job.authorization_id}, "test", client, SimpleNamespace(), chat_type="p2p",
    )
    assert pg.execute(
        "SELECT p2p_counterpart_open_id,p2p_counterpart_seen_at FROM authorization_chats WHERE chat_id='oc_person'",
    ).fetchone() == {"p2p_counterpart_open_id": None, "p2p_counterpart_seen_at": None}
    assert pg.execute("SELECT contact_id FROM lark_identities WHERE open_id='ou_person'").fetchone()[
        "contact_id"] == contact_id


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

    assert worker._fill_ai_slots(object(), object(), {Future(), Future(), Future()}, 2) == 2


def test_dispatch_uses_at_most_three_slots_and_fairly_rotates_queues(monkeypatch) -> None:
    submitted: list[tuple[object, object]] = []

    class Future:
        def done(self):
            return False

    class Executor:
        def submit(self, runner, job, settings):
            submitted.append((runner, job))
            return Future()

    assessment_jobs = iter(({"id": "identity-1"}, {"id": "identity-2"}))
    summary_jobs = iter(({"id": "summary-1"}, {"id": "summary-2"}))
    ownership_jobs = iter(({"id": "ownership-1"}, {"id": "ownership-2"}))
    import_jobs = iter(({"id": "import-1"},))
    reply_jobs = iter(({"id": "reply-1"},))
    monkeypatch.setattr(worker.summary_replies, "claim", lambda: next(reply_jobs, None))
    monkeypatch.setattr(worker.offhours_summary, "claim_next_summary", lambda: next(summary_jobs, None))
    monkeypatch.setattr(worker.contact_assessments, "claim_next_contact_assessment",
                        lambda: next(assessment_jobs, None))
    monkeypatch.setattr(worker.creator_ownership, "claim_next_ownership", lambda: next(ownership_jobs, None))
    monkeypatch.setattr(worker.creator_imports, "claim_next_import", lambda: next(import_jobs, None))

    futures = set()
    submitted_one, preference = worker._dispatch_ai_turn(object(), Executor(), futures, 0)
    submitted_two, preference = worker._dispatch_ai_turn(object(), Executor(), futures, preference)
    submitted_three, preference = worker._dispatch_ai_turn(object(), Executor(), futures, preference)
    submitted_four, _ = worker._dispatch_ai_turn(object(), Executor(), futures, preference)

    assert (submitted_one, submitted_two, submitted_three, submitted_four) == (True, True, True, False)
    assert [job["id"] for _runner, job in submitted] == ["identity-1", "summary-1", "ownership-1"]
    futures.clear()
    preference = worker._fill_ai_slots(object(), Executor(), futures, preference)
    assert preference == 1
    assert [job["id"] for _runner, job in submitted[3:]] == ["import-1", "reply-1", "identity-2"]
    futures.clear()
    assert worker._fill_ai_slots(object(), Executor(), futures, preference) == 3
    assert [job["id"] for _runner, job in submitted[6:]] == ["summary-2", "ownership-2"]

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


def test_offhours_tasks_fill_three_parallel_slots_without_waiting(monkeypatch) -> None:
    jobs = iter({"id": index} for index in range(3))
    submitted = []

    class Executor:
        def submit(self, runner, job, settings):
            assert runner is worker.offhours_summary.run_claimed_summary
            submitted.append(job["id"])
            return object()

    monkeypatch.setattr(worker.offhours_summary, "claim_next_summary", lambda: next(jobs))
    monkeypatch.setattr(worker.contact_assessments, "claim_next_contact_assessment", lambda: None)
    monkeypatch.setattr(worker.creator_ownership, "claim_next_ownership", lambda: None)
    monkeypatch.setattr(worker.creator_imports, "claim_next_import", lambda: None)
    monkeypatch.setattr(worker.summary_replies, "claim", lambda: None)
    futures = set()
    worker._fill_ai_slots(object(), Executor(), futures, 0)
    assert submitted == [0, 1, 2]
    assert len(futures) == 3


def test_creator_previews_are_fair_single_flight_and_back_off_without_work(monkeypatch) -> None:
    now = [10.0]
    submitted = []
    prepared = []
    settings = SimpleNamespace(worker_idle_seconds=3)
    ownership_jobs, import_jobs = iter((True, False)), iter((True, False))

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
    monkeypatch.setattr(worker.creator_ownership, "prepare_next_preview",
                        lambda _: prepare("ownership", ownership_jobs))
    monkeypatch.setattr(worker.creator_imports, "prepare_next_import",
                        lambda _: prepare("import", import_jobs))
    monkeypatch.setattr(worker.offhours_summary, "prepare_next_summary", lambda _: False)
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
    assert prepared == ["ownership", "import"]
    task, runner, preference = submitted[2]
    task.set_result(runner(settings, preference))
    future, next_check, queue = worker._schedule_creator_preview(settings, executor, future, next_check, queue)
    assert future is None and next_check == 13.0
    assert worker._schedule_creator_preview(settings, executor, future, next_check, queue) == (None, 13.0, queue)
    assert len(submitted) == 3 and prepared == ["ownership", "import", "ownership", "import"]

    now[0] = 13.0
    future, next_check, queue = worker._schedule_creator_preview(settings, executor, future, next_check, queue)
    assert future is submitted[3][0]
    future.set_exception(RuntimeError("preview failed"))
    assert worker._schedule_creator_preview(settings, executor, future, next_check, queue) == (None, 16.0, queue)

    def failed(_):
        raise RuntimeError("one preparation queue failed")

    monkeypatch.setattr(worker.creator_ownership, "prepare_next_preview", failed)
    monkeypatch.setattr(worker.creator_imports, "prepare_next_import", lambda _: True)
    assert worker._prepare_creator_preview(settings, 0) == (True, 2)


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
