from datetime import UTC, datetime, timedelta

from app.worker import _is_author_message, _lark_time, _member_count_refresh_due


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
