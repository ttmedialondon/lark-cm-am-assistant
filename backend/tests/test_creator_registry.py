from contextlib import nullcontext
from uuid import uuid4

import pytest

from app import creator_registry
from app.creator_registry import CreatorInputError, _normalize_identifier


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, account_id=None):
        self.account_id = account_id or uuid4()
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))
        if "FROM contact_roles" in query:
            return _Result({"exists": 1})
        if "RETURNING id" in query:
            return _Result({"id": self.account_id})
        return _Result()


def test_handle_normalization_keeps_a_single_stable_platform_key() -> None:
    assert _normalize_identifier("handle", " TikTok ", "@Creator.Name") == ("creator.name", "tiktok")


def test_handle_requires_a_platform() -> None:
    with pytest.raises(CreatorInputError):
        _normalize_identifier("handle", "", "@creator")


def test_manual_creator_is_a_contact_with_a_confirmed_creator_role(monkeypatch) -> None:
    conn = _Connection()
    monkeypatch.setattr(creator_registry, "connection", lambda: nullcontext(conn))

    creator_registry.create_creator(uuid4(), "Creator")

    queries = "\n".join(query for query, _params in conn.calls)
    assert "INSERT INTO contacts" in queries
    assert "INSERT INTO contact_roles" in queries
    assert "'creator', 'confirmed', 'manual'" in queries
    assert "INSERT INTO creators" not in queries


def test_manual_handle_creates_a_confirmed_owner_link(monkeypatch) -> None:
    conn = _Connection()
    monkeypatch.setattr(creator_registry, "connection", lambda: nullcontext(conn))

    account_id = creator_registry.add_identifier(
        uuid4(), uuid4(), kind="handle", platform="TikTok", value="@Creator.Name"
    )

    queries = "\n".join(query for query, _params in conn.calls)
    assert account_id == conn.account_id
    assert "INSERT INTO social_accounts" in queries
    assert "INSERT INTO contact_social_accounts" in queries
    assert "'owner', 'confirmed', 'manual'" in queries
    assert "creator_identifiers" not in queries


def test_manual_email_stays_a_contact_identifier(monkeypatch) -> None:
    conn = _Connection()
    monkeypatch.setattr(creator_registry, "connection", lambda: nullcontext(conn))

    creator_registry.add_identifier(uuid4(), uuid4(), kind="email", platform="", value="Creator@Example.com")

    queries = "\n".join(query for query, _params in conn.calls)
    assert "INSERT INTO contact_identifiers" in queries
    assert "INSERT INTO social_accounts" not in queries
