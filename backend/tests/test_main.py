from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main


def _request_with_session(session: str | None = None, csrf: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if session:
        headers.append((b"cookie", f"{main.SESSION_COOKIE}={session}".encode()))
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_csrf_token_is_bound_to_the_http_only_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"k" * 32))
    request = _request_with_session("session-a")
    token = main._csrf_token(request)

    assert token
    assert token != main._csrf_token(_request_with_session("session-b"))
    main._require_csrf(_request_with_session("session-a", token))

    with pytest.raises(HTTPException) as error:
        main._require_csrf(_request_with_session("session-a", "wrong"))
    assert error.value.status_code == 403


def test_manual_backfill_state_and_pending_copy_are_clear() -> None:
    assert main._manual_backfill_state(None, None) is None
    assert main._manual_backfill_state(object(), None) == "queued"
    assert main._manual_backfill_state(object(), object()) == "running"
    assert main._chat_sync_status("pending") == ("等待首次回填", "secondary")
