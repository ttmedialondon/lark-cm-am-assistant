from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app import worker
from app.worker import (
    _body_content,
    _is_author_message,
    _lark_time,
    _member_count_refresh_due,
    _read_member_snapshot,
    _sender_metadata,
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

    assert calls[0][1][1:6] == (False, False, None, False, None)
    assert "attempts = attempts + CASE WHEN %s THEN 1 ELSE 0 END" in calls[0][0]
