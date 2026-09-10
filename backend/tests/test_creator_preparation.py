"""The owner preparation roster is read-only and is not a creator registry."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from test_admin_page_access import PAGES, _client
from test_creator_review import ASSIGNMENT, CONTACT, NOW, OWNER, Connection, documented_row
from test_creator_workflow import db as workflow_db

from app import creator_preparation, creator_review
from scripts import prepare_creator_handoff

PATH = "/admin/creator-review/preparation"
db = workflow_db


def preparation_item(*, handle="example.handle", issue="none", row=None):
    row = documented_row() if row is None else row
    bound = issue != "unresolved"
    return {
        "handle": handle, "source_rows": [12], "cm_values": ["Owner Person"],
        "tap_cm_values": ["Owner Person"], "source_group_names": ["Example Handle x TT"],
        "contact_id": str(row["id"]) if issue not in {"alias_missing", "unresolved"} else None,
        "suggested_contact_id": str(row["id"]) if issue == "alias_missing" else None,
        "display_name": row["display_name"] if bound else "未定位联系人",
        "category": creator_preparation.ISSUES[issue], "issue": issue,
        "reason": "Synthetic import evidence", "next_step": "Owner will review",
        "existing_role_source": row["role_source"] if bound else None,
        "expected_assignment_id": str(row["assignment_id"]) if bound else None,
        "expected_role_updated_at": row["role_updated_at"].isoformat() if bound else None,
    }


def preparation_payload(*items):
    return {
        "version": 1, "source_filename": "Synthetic creator roster.xlsx", "source_sha256": "a" * 64,
        "prepared_at": NOW.isoformat(), "responsible_id": str(OWNER), "responsible_name": "Owner Person",
        "items": list(items) or [preparation_item()],
    }


def bind_preparation(monkeypatch, payload, *current_rows):
    stored = {"metadata": {"payload": payload, "payload_sha256": creator_preparation.payload_digest(payload)}}
    conn = Connection(stored, list(current_rows))
    monkeypatch.setattr(creator_preparation, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(creator_review, "connection", no_data)
    monkeypatch.setattr(creator_review, "message_body_cipher", no_data)
    monkeypatch.setattr(creator_review, "record_view", no_data)
    return conn


def no_data(*_args, **_kwargs):
    pytest.fail("A denied/read-only preparation request reached protected data or a write")


@pytest.mark.parametrize("permissions", [(), *[(page,) for page in PAGES], PAGES])
def test_page_grants_never_allow_preparation_or_query_its_snapshot(monkeypatch, permissions):
    browser, _, _ = _client(monkeypatch, permissions=permissions)
    monkeypatch.setattr(creator_preparation, "connection", no_data)
    monkeypatch.setattr(creator_review, "connection", no_data)
    response = browser.get(PATH, follow_redirects=False)
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


@pytest.mark.parametrize("owner", [False, True])
def test_revoked_source_cannot_read_preparation_even_as_owner(monkeypatch, owner):
    browser, _, _ = _client(monkeypatch, permissions=PAGES, owner=owner, status="revoked")
    monkeypatch.setattr(creator_preparation, "connection", no_data)
    monkeypatch.setattr(creator_review, "connection", no_data)
    assert browser.get(PATH, follow_redirects=False).status_code == 403


def test_preparation_exposes_no_post_operation_even_to_owner(monkeypatch):
    browser, _, _ = _client(monkeypatch, owner=True)
    monkeypatch.setattr(creator_preparation, "connection", no_data)
    monkeypatch.setattr(creator_review, "connection", no_data)
    assert browser.post(PATH, data={"action": "confirm_all"}).status_code == 405


def test_preparation_route_is_reachable_by_owner_before_contact_id_route(monkeypatch):
    class ReachedPreparation(Exception):
        pass

    def reached(*_args, **_kwargs):
        raise ReachedPreparation

    browser, _, _ = _client(monkeypatch, owner=True)
    monkeypatch.setattr(creator_preparation, "connection", reached)
    monkeypatch.setattr(creator_review, "connection", no_data)
    with pytest.raises(ReachedPreparation):
        browser.get(PATH)


def test_valid_payload_preserves_source_and_never_confirms_suggested_or_missing_handles():
    payload = preparation_payload(
        preparation_item(), preparation_item(handle="alias.handle", issue="alias_missing"),
        preparation_item(handle="pending.handle", issue="unresolved"),
    )
    original = deepcopy(payload)
    assert creator_preparation.parse_payload(payload) == original
    assert payload["items"][1]["contact_id"] is None
    assert payload["items"][1]["suggested_contact_id"] == str(CONTACT)
    assert payload["items"][2]["contact_id"] is None
    assert payload["items"][2]["suggested_contact_id"] is None


@pytest.mark.parametrize("change", [
    {"version": 2}, {"version": True}, {"message_body": "must not be retained"},
    {"source_filename": "../private.xlsx"}, {"source_sha256": "invalid"},
    {"prepared_at": "2026-09-07T01:00:00"}, {"responsible_id": "not-an-id"},
    {"items": []}, {"items": [{}]},
])
def test_snapshot_rejects_unknown_or_malformed_top_level_fields(change):
    payload = {**preparation_payload(), **change}
    with pytest.raises((ValueError, TypeError, AttributeError)):
        creator_preparation.parse_payload(payload)


@pytest.mark.parametrize("change", [
    {"category": "auto_confirmed"}, {"category": "documented", "issue": "alias_missing"},
    {"body": "must not be retained"}, {"source_rows": [True]}, {"source_rows": [12, 12]},
    {"source_rows": [0]}, {"source_rows": []}, {"handle": "@example.handle"},
    {"handle": "EXAMPLE.HANDLE"}, {"handle": "example handle"}, {"contact_id": "bad-uuid"},
    {"expected_assignment_id": None}, {"expected_role_updated_at": None},
    {"existing_role_source": None}, {"suggested_contact_id": str(CONTACT)},
    {"reason": ""},
])
def test_snapshot_rejects_ambiguous_or_unbound_items(change):
    payload = preparation_payload({**preparation_item(), **change})
    with pytest.raises((ValueError, TypeError, AttributeError)):
        creator_preparation.parse_payload(payload)


def test_snapshot_rejects_duplicate_handles_as_a_whole():
    with pytest.raises(ValueError):
        creator_preparation.parse_payload(preparation_payload(preparation_item(), preparation_item()))


@pytest.mark.parametrize("change", [
    {"id": UUID(int=99)}, {"assignment_id": UUID(int=99)}, {"responsible_id": UUID(int=99)},
    {"role_source": "manual_p2p_confirmed"}, {"role_updated_at": NOW + timedelta(seconds=1)},
    {"assignment_id": None}, {"role_updated_at": None},
])
def test_binding_requires_the_same_live_contact_role_assignment_and_responsible(change):
    item = preparation_item()
    assert creator_preparation.binding_matches(item, documented_row(), str(OWNER)) is True
    assert creator_preparation.binding_matches(item, {**documented_row(), **change}, str(OWNER)) is False
    assert creator_preparation.binding_matches(item, None, str(OWNER)) is False
    assert creator_preparation.binding_matches(item, {}, str(OWNER)) is False


def test_untrusted_snapshot_cannot_fall_back_to_older_or_query_formal_records(monkeypatch):
    payload = preparation_payload()
    bad = {"metadata": {"payload": payload, "payload_sha256": "0" * 64}}
    conn = Connection(bad)
    monkeypatch.setattr(creator_preparation, "connection", lambda: nullcontext(conn))
    result = creator_preparation.directory()
    assert result["available"] is False
    assert result["items"] == []
    assert len(conn.calls) == 1
    assert conn.calls[0][1] == (creator_preparation.ACTION,)
    assert "ORDER BY created_at DESC, id DESC LIMIT 1" in conn.calls[0][0]


@pytest.mark.parametrize("reset_owner,reset_offset,available", [
    (None, None, True),
    (OWNER, timedelta(seconds=1), False),
    (OWNER, timedelta(0), False),
    (UUID(int=99), timedelta(seconds=1), True),
    (OWNER, timedelta(seconds=-1), True),
])
def test_reset_hides_only_older_or_same_time_snapshot_for_that_cm(db, reset_owner, reset_offset, available):
    payload = preparation_payload()
    envelope = {"payload": payload, "payload_sha256": creator_preparation.payload_digest(payload)}
    db.execute(
        "INSERT INTO audit_events(id,action,metadata,created_at) VALUES(%s,%s,%s::jsonb,%s)",
        (uuid4(), creator_preparation.ACTION, json.dumps(envelope), NOW),
    )
    if reset_owner is not None:
        db.execute(
            "INSERT INTO audit_events(id,action,metadata,created_at) VALUES(%s,'creator_roster_reset',%s::jsonb,%s)",
            (uuid4(), json.dumps({"responsible_id": str(reset_owner)}), NOW + reset_offset),
        )
    assert creator_preparation.load_snapshot(db) == (payload if available else None)
    assert db.execute(
        "SELECT metadata FROM audit_events WHERE action=%s", (creator_preparation.ACTION,)
    ).fetchone()["metadata"] == envelope


def test_preparation_get_is_read_only_and_keeps_candidate_handles_out_of_registry(monkeypatch):
    row = documented_row()
    payload = preparation_payload(
        preparation_item(), preparation_item(handle="alias.handle", issue="alias_missing"),
        preparation_item(handle="pending.handle", issue="unresolved"),
    )
    original = deepcopy((payload, row))
    conn = bind_preparation(monkeypatch, payload, row)
    contexts = []
    browser, _, _ = _client(monkeypatch, owner=True, contexts=contexts)
    response = browser.get(f"{PATH}?chat_id=oc_ignored&action=confirm_all")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert contexts[-1][0] == "admin/creator_preparation.html"
    result = contexts[-1][1]["preparation"]
    assert result["summary"] == {
        "total": 3, "documented": 1, "archive_needed": 0, "decision_needed": 2, "stale": 0,
    }
    by_handle = {item["handle"]: item for item in result["items"]}
    assert by_handle["alias.handle"]["detail_url"] is None
    assert by_handle["pending.handle"]["detail_url"] is None
    assert by_handle["example.handle"]["detail_url"] == f"/admin/creator-review/{CONTACT}"
    assert creator_review._evidence(row)["handles"] == ["example.handle"]
    assert (payload, row) == original
    assert len(conn.calls) == 2
    assert all(sql.strip().startswith("SELECT") for sql, _ in conn.calls)
    assert all("body_ciphertext" not in sql for sql, _ in conn.calls)


def test_live_names_overlay_display_without_rewriting_historical_snapshot(monkeypatch):
    payload = preparation_payload()
    original = deepcopy(payload)
    row = {**documented_row(), "responsible_name": "Remi（Quanzhen Liang）", "display_name": "Lami（Lamide Oj）"}
    conn = bind_preparation(monkeypatch, payload, row)
    result = creator_preparation.roster()
    assert result["responsible_name"] == row["responsible_name"]
    assert result["items"][0]["display_name"] == row["display_name"]
    assert payload == original
    assert len(conn.calls) == 2


@pytest.mark.parametrize("change", [
    {"assignment_id": UUID(int=99)}, {"responsible_id": UUID(int=99)},
    {"role_updated_at": NOW + timedelta(seconds=1)}, {"role_source": "manual_p2p_confirmed"},
    {"role_audits": [], "assignment_audit": {}},
])
def test_changed_formal_state_makes_snapshot_stale_without_promoting_it(change, monkeypatch):
    payload = preparation_payload()
    bind_preparation(monkeypatch, payload, {**documented_row(), **change})
    result = creator_preparation.directory()
    assert result["summary"]["stale"] == 1
    assert result["summary"]["documented"] == 0
    item = result["items"][0]
    assert item["is_stale"] is True
    assert item["category"] == "decision_needed"
    assert item["detail_url"] is None


@pytest.mark.parametrize("issue", ["alias_missing", "unresolved"])
def test_newly_recorded_candidate_handle_invalidates_old_candidate_advice(monkeypatch, issue):
    item = preparation_item(issue=issue)
    bind_preparation(monkeypatch, preparation_payload(item), documented_row())
    result = creator_preparation.directory()
    assert result["items"][0]["is_stale"] is True
    assert result["items"][0]["detail_url"] is None


@pytest.mark.parametrize("query", ["example.handle", " @EXAMPLE.HANDLE ", "  Example  Creator  "])
def test_handle_search_accepts_at_case_and_surrounding_whitespace(monkeypatch, query):
    bind_preparation(monkeypatch, preparation_payload(), documented_row())
    result = creator_preparation.directory(99, query)
    assert result["total_count"] == 1
    assert result["page"] == 1


def test_latest_identity_evidence_cannot_be_inherited_by_other_historical_handles():
    row = documented_row()
    row["role_audits"].append({"handle": "older.handle", "excel_rows": [13]})
    facts = creator_review._evidence(row)
    assert facts["handles"] == ["example.handle", "older.handle"]
    assert facts["documented"] is False
    assert facts["group_ids"] == []


def test_identity_warning_is_bound_to_its_original_responsible_and_never_adds_handles():
    row = documented_row()
    note = {**preparation_item(issue="identity_risk"), "responsible_id": str(OWNER)}
    row["review_preparation"] = note
    assert creator_review._evidence(row)["identity_review"] is True
    changed = {**row, "responsible_id": UUID(int=99)}
    assert creator_review._evidence(changed)["identity_review"] is False
    changed = {**row, "review_preparation": {**note, "handle": "unconfirmed.alias"}}
    assert creator_review._evidence(changed)["identity_review"] is False
    assert creator_review._evidence(changed)["handles"] == ["example.handle"]
    changed = {**row, "review_preparation": {**note, "expected_assignment_id": str(ASSIGNMENT),
                                           "responsible_id": None}}
    assert creator_review._evidence(changed)["identity_review"] is False


def publish_connection(*, row=None, actor=True, enabled=False, jobs=0, previous=None, write_count=0):
    return Connection(
        None, {"id": OWNER} if actor else None,
        [{"feature": "contact_assessment", "enabled": enabled},
         {"feature": "contact_assignment", "enabled": False}],
        {"count": jobs}, [row or documented_row()], previous,
        *[None for _ in range(write_count)],
    )


def test_handoff_dry_run_only_locks_and_reads_without_changing_records():
    payload = preparation_payload(preparation_item(issue="identity_risk"))
    original = deepcopy(payload)
    conn = publish_connection()
    result = prepare_creator_handoff.publish(conn, payload, OWNER)
    assert result["state"] == "dry_run"
    assert result["formal_changes"] == 0
    assert len(conn.calls) == 6
    assert all(sql.strip().startswith(("SELECT", "LOCK TABLE")) for sql, _ in conn.calls)
    assert payload == original


def test_apply_only_appends_snapshot_and_state_bound_risk_notes():
    payload = preparation_payload(preparation_item(issue="identity_risk"))
    conn = publish_connection(write_count=2)
    result = prepare_creator_handoff.publish(conn, payload, OWNER, apply=True)
    assert result["state"] == "prepared"
    assert result["formal_changes"] == 0
    writes = [(sql, params) for sql, params in conn.calls if not sql.strip().startswith(("SELECT", "LOCK TABLE"))]
    assert len(writes) == 2
    assert all(sql.startswith("INSERT INTO audit_events ") for sql, _ in writes)
    snapshot_params, risk_params = writes[0][1], writes[1][1]
    assert snapshot_params[1:3] == (OWNER, creator_preparation.ACTION)
    assert json.loads(snapshot_params[3])["payload"] == payload
    assert risk_params[1:3] == (OWNER, creator_preparation.RISK_ACTION)
    note = json.loads(risk_params[3])
    assert creator_preparation.binding_matches(note, documented_row(), str(OWNER))
    assert set(note) == {
        "handle", "contact_id", "source_rows", "reason", "existing_role_source", "expected_assignment_id",
        "expected_role_updated_at", "batch_id", "responsible_id",
    }


def test_republishing_same_snapshot_is_idempotent_without_additional_writes():
    payload = preparation_payload()
    previous = {"metadata": {"payload": payload, "payload_sha256": creator_preparation.payload_digest(payload)}}
    conn = publish_connection(previous=previous)
    result = prepare_creator_handoff.publish(conn, payload, OWNER, apply=True)
    assert result["state"] == "already_prepared"
    assert len(conn.calls) == 6
    assert all(sql.strip().startswith(("SELECT", "LOCK TABLE")) for sql, _ in conn.calls)


@pytest.mark.parametrize("options", [{"actor": False}, {"enabled": True}, {"jobs": 1},
                                   {"row": {**documented_row(), "assignment_id": UUID(int=99)}}])
def test_publish_preconditions_abort_before_any_mutation(options):
    conn = publish_connection(**options)
    with pytest.raises(ValueError):
        prepare_creator_handoff.publish(conn, preparation_payload(), OWNER, apply=True)
    assert all(sql.strip().startswith(("SELECT", "LOCK TABLE")) for sql, _ in conn.calls)


def test_snapshot_cannot_promote_a_legacy_import_to_documented_or_omit_formal_handles():
    row = {**documented_row(), "role_source": "manual_p2p_confirmed"}
    item = preparation_item(row=row)
    conn = publish_connection(row=row)
    with pytest.raises(ValueError, match="cannot promote undocumented"):
        prepare_creator_handoff.publish(conn, preparation_payload(item), OWNER, apply=True)
    row = documented_row()
    row["role_audits"].append({"handle": "unlisted.handle", "excel_rows": [13]})
    conn = publish_connection(row=row)
    with pytest.raises(ValueError, match="cover this owner's current handle registry exactly"):
        prepare_creator_handoff.publish(
            conn, preparation_payload(preparation_item(issue="missing_archive")), OWNER, apply=True,
        )
    assert all(sql.strip().startswith(("SELECT", "LOCK TABLE")) for sql, _ in conn.calls)
