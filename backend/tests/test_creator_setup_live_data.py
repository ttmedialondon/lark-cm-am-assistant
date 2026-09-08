"""Remi's preview is an account projection, not a new import or chat reader."""

import json
from contextlib import nullcontext
from copy import deepcopy
from uuid import UUID

import pytest
from test_creator_preparation import preparation_item, preparation_payload
from test_creator_review import Connection, documented_row

from app import creator_preparation, creator_review, creator_setup_preview


def remi_row(*handles):
    row = documented_row()
    row["responsible_id"] = creator_setup_preview.REMI_ID
    row["responsible_name"] = "Quanzhen Liang"
    row["role_audits"][0]["creator_handles"] = list(handles or ["example.handle"])
    row["assignment_audit"] = deepcopy(row["role_audits"][0])
    return row


def bind_roster(monkeypatch, items, *rows, owner=creator_setup_preview.REMI_ID, digest=None):
    payload = preparation_payload(*items)
    payload.update(responsible_id=str(owner), responsible_name="Quanzhen Liang")
    conn = Connection({"metadata": {
        "payload": payload, "payload_sha256": digest or creator_preparation.payload_digest(payload),
    }}, list(rows))
    monkeypatch.setattr(creator_preparation, "connection", lambda: nullcontext(conn))
    return conn


def test_roster_returns_every_handle_once_and_directory_keeps_existing_pagination(monkeypatch):
    handles = [f"account.{index:02d}" for index in range(27)]
    row = remi_row(*handles)
    items = [preparation_item(handle=handle, row=row) for handle in handles]
    conn = bind_roster(monkeypatch, items, row)
    data = creator_setup_preview.data()
    assert [item["handle"] for item in data["accounts"]] == handles
    assert data["summary"] == {
        "total": 27, "documented": 27, "archive_needed": 0, "decision_needed": 0, "stale": 0,
        "formal_people": 1, "formal_handles": 27, "unlisted_formal_handles": 0,
        "unresolved": 0, "alias_missing": 0, "identity_risk": 0,
    }
    assert len(conn.calls) == 2
    bind_roster(monkeypatch, items, row)
    page = creator_preparation.directory(page=2)
    assert len(page["items"]) == 7
    assert page["total_count"] == 27
    assert set(page["summary"]) == {"total", "documented", "archive_needed", "decision_needed", "stale"}


@pytest.mark.parametrize("failure", ["missing", "digest", "other_owner"])
def test_missing_invalid_or_other_owner_snapshot_fails_closed_before_formal_read(monkeypatch, failure):
    row = remi_row()
    if failure == "missing":
        conn = Connection(None)
        monkeypatch.setattr(creator_preparation, "connection", lambda: nullcontext(conn))
    else:
        conn = bind_roster(
            monkeypatch, [preparation_item(row=row)], row,
            owner=UUID(int=99) if failure == "other_owner" else creator_setup_preview.REMI_ID,
            digest="0" * 64 if failure == "digest" else None,
        )
    data = creator_setup_preview.data()
    assert data["available"] is False
    assert data["accounts"] == []
    assert data["source_filename"] == data["prepared_at_label"] == ""
    assert all(value == 0 for value in data["summary"].values())
    assert len(conn.calls) == 1
    assert "demo_" not in json.dumps(data)


def test_candidate_alias_missing_contact_and_historical_risk_are_distinct(monkeypatch):
    row = remi_row()
    items = [
        preparation_item(row=row, issue="identity_risk"),
        preparation_item(row=row, handle="possible.alias", issue="alias_missing"),
        preparation_item(row=row, handle="unknown.handle", issue="unresolved"),
    ]
    bind_roster(monkeypatch, items, row)
    data = creator_setup_preview.data()
    historical, candidate, missing = data["accounts"]
    assert historical["can_view_conversations"] is True
    assert "历史映射" in historical["mapping_label"]
    assert "待确认" in historical["mapping_label"]
    assert candidate["can_view_conversations"] is False
    assert candidate["contact_id"] is None
    assert candidate["suggested_contact_id"] == str(row["id"])
    assert "候选" in candidate["mapping_label"]
    assert missing["can_view_conversations"] is False
    assert missing["contact_id"] is missing["suggested_contact_id"] is None
    assert data["summary"]["identity_risk"] == data["summary"]["alias_missing"] == data["summary"]["unresolved"] == 1


@pytest.mark.parametrize("change", [
    {"assignment_id": UUID(int=99)}, {"responsible_id": UUID(int=99)},
    {"role_audits": [], "assignment_audit": {}},
])
def test_changed_mapping_never_exposes_conversation_entry_or_new_other_owner_name(monkeypatch, change):
    row = remi_row()
    item = preparation_item(row=row)
    current = {**row, **change, "display_name": "Changed private display name"}
    bind_roster(monkeypatch, [item], current)
    data = creator_setup_preview.data()
    account = data["accounts"][0]
    assert account["is_stale"] is True
    assert account["can_view_conversations"] is False
    assert account["category"] == "decision_needed"
    assert "Changed private" not in json.dumps(data)
    assert data["summary"]["stale"] == 1


def test_current_name_is_used_only_for_valid_mapping_and_summary_is_remi_scoped(monkeypatch):
    row = remi_row()
    item = preparation_item(row=row)
    row["display_name"] = "Current creator name"
    other = {**documented_row(), "id": UUID(int=98), "responsible_id": UUID(int=99)}
    other["role_audits"][0]["creator_handles"] = ["other.owner.handle"]
    bind_roster(monkeypatch, [item], row, other)
    data = creator_setup_preview.data()
    assert data["accounts"][0]["display_name"] == "Current creator name"
    assert data["summary"]["formal_people"] == data["summary"]["formal_handles"] == 1
    assert "other.owner.handle" not in json.dumps(data)


def test_formal_handle_missing_from_snapshot_is_a_gap_not_silently_added(monkeypatch):
    row = remi_row("example.handle", "newly.recorded.handle")
    bind_roster(monkeypatch, [preparation_item(row=row)], row)
    data = creator_setup_preview.data()
    assert data["summary"]["formal_handles"] == 2
    assert data["summary"]["unlisted_formal_handles"] == 1
    assert [item["handle"] for item in data["accounts"]] == ["example.handle"]


def test_live_projection_exposes_only_allowed_fields_and_performs_no_chat_reads_or_writes(monkeypatch):
    row = remi_row()
    conn = bind_roster(monkeypatch, [preparation_item(row=row)], row)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Roster projection reached a conversation/body query or write")

    monkeypatch.setattr(creator_review, "connection", forbidden)
    monkeypatch.setattr(creator_review, "messages", forbidden)
    monkeypatch.setattr(creator_review, "detail", forbidden)
    monkeypatch.setattr(creator_review, "record_view", forbidden)
    data = creator_setup_preview.data()
    assert set(data) == {
        "available", "responsible_name", "responsible_id", "source_filename", "prepared_at_label",
        "summary", "accounts",
    }
    assert set(data["accounts"][0]) == set(creator_setup_preview.ACCOUNT_FIELDS) | {
        "can_view_conversations", "mapping_label",
    }
    serialized = json.dumps(data)
    for forbidden_field in ("expected_assignment_id", "expected_role_updated_at", "existing_role_source",
                            "role_audits", "assignment_audit", "body_ciphertext", "access_token"):
        assert forbidden_field not in serialized
    assert len(conn.calls) == 2
    assert all(sql.strip().startswith("SELECT") for sql, _ in conn.calls)
    assert all("chat_messages" not in sql and "body_ciphertext" not in sql for sql, _ in conn.calls)


def test_candidate_that_is_newly_formal_cannot_keep_old_advice(monkeypatch):
    row = remi_row()
    bind_roster(monkeypatch, [preparation_item(row=row, issue="alias_missing")], row)
    data = creator_setup_preview.data()
    assert data["accounts"][0]["is_stale"] is True
    assert data["accounts"][0]["can_view_conversations"] is False
