from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app import worker
from app.worker import (
    _body_content,
    _is_author_message,
    _lark_time,
    _member_count_refresh_due,
    _message_relation_metadata,
    _p2p_counterpart_open_id,
    _read_member_snapshot,
    _record_social_account_observations,
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


def test_social_account_observation_does_not_assign_an_owner() -> None:
    account_id = uuid4()
    calls = []

    class Result:
        def fetchone(self):
            return {"id": account_id}

    class Connection:
        def execute(self, query, params):
            calls.append((query, params))
            return Result()

    authorization_id = uuid4()
    _record_social_account_observations(
        Connection(),
        source_open_id="ou_sender",
        chat_id="oc_group",
        message_id="om_message",
        observed_by_authorization_id=authorization_id,
        body_content='{"text":"https://instagram.com/example"}',
    )

    assert len(calls) == 2
    assert "social_accounts" in calls[0][0]
    assert "social_account_observations" in calls[1][0]
    assert calls[1][1][1:6] == (account_id, "ou_sender", "oc_group", "om_message", authorization_id)


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


def test_contact_assessment_runner_isolated_from_sync_loop(monkeypatch) -> None:
    settings = object()
    calls = []

    monkeypatch.setattr(
        worker.contact_assessments,
        "run_next_contact_assessment",
        lambda received_settings: calls.append(received_settings) or True,
    )

    assert worker._run_contact_assessment(settings) is True
    assert calls == [settings]


def test_contact_assessment_runner_keeps_worker_alive_on_unexpected_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        worker.contact_assessments,
        "run_next_contact_assessment",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("untrusted details")),
    )

    assert worker._run_contact_assessment(object()) is False


def test_worker_gives_the_durable_ai_queue_a_turn_during_continuous_sync(monkeypatch) -> None:
    calls: list[str] = []
    jobs = iter((object(), object(), object()))

    def recover() -> None:
        if "assessment" in calls:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(app_secret_configured=True, worker_idle_seconds=1),
    )
    monkeypatch.setattr(worker, "_recover_expired_leases", recover)
    monkeypatch.setattr(worker, "_claim_job", lambda: next(jobs))
    monkeypatch.setattr(worker, "_execute_job", lambda _job, _settings: calls.append("sync"))
    monkeypatch.setattr(worker, "_run_contact_assessment", lambda _settings: calls.append("assessment") or True)

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass

    assert calls == ["sync", "sync", "sync", "assessment"]
