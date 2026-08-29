from app.worker import _is_author_message, _lark_time


def test_author_message_requires_open_id_semantics() -> None:
    message = {"sender": {"sender_type": "user", "id_type": "open_id", "id": "ou_me"}}
    assert _is_author_message(message, "ou_me")
    assert not _is_author_message({"sender": {"sender_type": "user", "id": "ou_me"}}, "ou_me")


def test_lark_time_interprets_milliseconds() -> None:
    timestamp = _lark_time("1615380573411")
    assert timestamp is not None
    assert timestamp.year == 2021
