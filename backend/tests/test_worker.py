from collections import Counter
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


def test_p2p_contact_discovery_skips_bots() -> None:
    assert _p2p_counterpart_open_id("ou_person", "user") == "ou_person"
    assert _p2p_counterpart_open_id("cli_bot", "bot") is None


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

    assert worker._fill_ai_slots(object(), object(), {Future(), Future(), Future()}, False) is False


def test_dispatch_uses_at_most_three_slots_and_alternates_queues(monkeypatch) -> None:
    submitted: list[tuple[object, object]] = []

    class Future:
        def done(self):
            return False

    class Executor:
        def submit(self, runner, job, settings):
            submitted.append((runner, job))
            return Future()

    assessment_jobs = iter(({"id": "identity-1"}, {"id": "identity-2"}))
    assignment_jobs = iter(({"id": "assignment-1"},))
    monkeypatch.setattr(worker.contact_assessments, "claim_next_contact_assessment", lambda: next(assessment_jobs))
    monkeypatch.setattr(worker.contact_assignments, "claim_next_contact_assignment", lambda: next(assignment_jobs))

    futures = set()
    submitted_one, preference = worker._dispatch_ai_turn(object(), Executor(), futures, False)
    submitted_two, preference = worker._dispatch_ai_turn(object(), Executor(), futures, preference)
    submitted_three, preference = worker._dispatch_ai_turn(object(), Executor(), futures, preference)
    submitted_four, _ = worker._dispatch_ai_turn(object(), Executor(), futures, preference)

    assert (submitted_one, submitted_two, submitted_three, submitted_four) == (True, True, True, False)
    assert [job["id"] for _runner, job in submitted] == ["identity-1", "assignment-1", "identity-2"]


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
