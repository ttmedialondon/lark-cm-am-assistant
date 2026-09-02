from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import worker
from app.worker import (
    _body_content,
    _is_author_message,
    _lark_time,
    _member_count_refresh_due,
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


def test_member_count_refresh_is_due_only_when_missing_or_stale() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)

    assert _member_count_refresh_due(None, now, 86400)
    assert not _member_count_refresh_due(now - timedelta(hours=23), now, 86400)
    assert _member_count_refresh_due(now - timedelta(hours=24), now, 86400)


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


def test_normal_pagination_requeue_does_not_increment_retry_count(monkeypatch) -> None:
    calls = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params):
            calls.append((query, params))

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(Connection()))

    worker._requeue(uuid4(), delay_seconds=0, retry=False)

    assert calls[0][1][1:4] == (False, None, None)
    assert "attempts = attempts + CASE WHEN %s THEN 1 ELSE 0 END" in calls[0][0]
    assert "last_error_code = %s" in calls[0][0]


def test_worker_fills_ai_slots_before_a_lark_sync_job(monkeypatch) -> None:
    calls: list[str] = []
    jobs = iter((object(),))

    def recover() -> None:
        if calls == ["assessment", "sync"]:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(app_secret_configured=True, worker_idle_seconds=1),
    )
    monkeypatch.setattr(worker, "_recover_expired_leases", recover)
    monkeypatch.setattr(worker, "_claim_job", lambda: next(jobs))
    monkeypatch.setattr(worker, "_execute_job", lambda _job, _settings: calls.append("sync"))
    monkeypatch.setattr(worker, "_fill_ai_slots", lambda *_args: calls.append("assessment") or True)

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass

    assert calls == ["assessment", "sync"]


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
