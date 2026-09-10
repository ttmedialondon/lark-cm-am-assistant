"""CAP dual-source policy regressions; isolated PostgreSQL only, no AI/API calls."""

import json
from uuid import uuid4

import pytest
import test_creator_workflow as fixtures

from app import creator_workflow as workflow

db = fixtures.db
people = fixtures.people


def ai_data(people, **changes):
    return {
        "cap_policy_version": "cap_v1",
        "creator_confidence": 0.95,
        "identity_confidence": 0.95,
        "cm_confidence": 0.95,
        "exact_handle_evidence": True,
        "direct_creator_evidence": True,
        "direct_cm_evidence": True,
        "unknowns": [],
        "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}],
        **changes,
    }


def ai(people, *, data=None, **changes):
    return fixtures.source(people, **{
        "source_type": "ai", "source_key": "ai-result", "source_data": ai_data(people) if data is None else data,
        "creator_type": None, "relationship": None, **changes,
    })


def excel(people, **changes):
    return fixtures.source(people, **{
        "source_key": "cap-sheet-row", "creator_type": "cap", "contact_id": None, "relationship": None,
        "source_data": {"cap_policy_version": "cap_v1", "creator_type": "cap", "type_evidence": "cm_excel_upload"},
        **changes,
    })


def assert_no_formal(db):
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 0


@pytest.mark.parametrize("ai_first", [False, True])
def test_two_sources_confirm_in_either_order_and_repeated_sources_are_idempotent(db, people, ai_first):
    first = ai(people) if ai_first else excel(people)
    assert first["status"] == "pending"
    assert first["creator_type"] == (None if ai_first else "cap")
    assert_no_formal(db)
    second = excel(people) if ai_first else ai(people)
    assert second["id"] == first["id"] and second["status"] == "confirmed"
    assert second["creator_type"] == "cap" and second["sources"] == ["ai", "excel"]
    assignment = db.execute("SELECT * FROM creator_account_assignments").fetchone()
    assert assignment["source"] == "excel_ai_verified" and assignment["creator_type"] == "cap"
    assert assignment["responsible_authorization_id"] == people["cm"]
    assert assignment["contact_id"] == people["person"] and assignment["relationship"] == "associated"
    assert ai(people)["id"] == excel(people)["id"] == first["id"]
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_sources").fetchone()["n"] == 2
    assert db.execute("SELECT COUNT(*) n FROM contact_roles").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM contact_assignments").fetchone()["n"] == 0


@pytest.mark.parametrize("field,value", [
    ("creator_confidence", 0.949), ("identity_confidence", 0.949), ("cm_confidence", 0.949),
    ("identity_confidence", None), ("identity_confidence", True), ("identity_confidence", "0.99"),
    ("identity_confidence", 1.01), ("identity_confidence", -1),
    ("exact_handle_evidence", False), ("direct_creator_evidence", False), ("direct_cm_evidence", False),
    ("exact_handle_evidence", 1), ("direct_cm_evidence", None),
    ("unknowns", ["identity is uncertain"]), ("cap_policy_version", None),
])
def test_each_ai_gate_must_pass_even_after_cap_upload(db, people, field, value):
    excel(people)
    assert ai(people, data=ai_data(people, **{field: value}))["status"] == "pending"
    assert_no_formal(db)


@pytest.mark.parametrize("field", ["normalized_handle", "responsible_authorization_id", "contact_id"])
def test_incomplete_candidate_never_auto_confirms(db, people, field):
    changes = {
        "normalized_handle": "handle", "responsible_authorization_id": "responsible_id", "contact_id": "contact_id"
    }
    excel(people, **{changes[field]: None})
    ai(people, **{changes[field]: None})
    assert_no_formal(db)


@pytest.mark.parametrize("legacy_source", ["excel", "ai"])
def test_legacy_source_without_cap_policy_cannot_complete_new_gate(db, people, legacy_source):
    excel(people, **({"source_data": {"stable_open_id": "ou_person"}, "trusted_identity": True}
                     if legacy_source == "excel" else {}))
    data = ai_data(people)
    if legacy_source == "ai":
        data.pop("cap_policy_version")
    assert ai(people, data=data)["status"] == "pending"
    assert_no_formal(db)


@pytest.mark.parametrize("same_key", [False, True])
@pytest.mark.parametrize("field,value", [
    ("identity_confidence", 0.30), ("direct_cm_evidence", False), ("unknowns", ["not certain"]),
])
def test_new_uncertain_ai_result_supersedes_old_high_score(db, people, same_key, field, value):
    ai(people)
    ai(people, source_key="ai-result" if same_key else "new-run", data=ai_data(people, **{field: value}))
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_sources WHERE source_type='ai'").fetchone()["n"] == 2
    assert excel(people)["status"] == "pending"
    assert_no_formal(db)


@pytest.mark.parametrize("change", [
    "edit", "rename", "rebind", "deleted", "hidden", "sync_disabled", "consent",
    "mentions", "p2p_counterpart", "tenant", "chat_mode",
])
def test_current_evidence_and_permissions_are_rechecked_when_excel_arrives(db, people, change):
    ai(people)
    if change == "edit":
        db.execute("UPDATE chat_messages SET message_update_time=clock_timestamp() WHERE chat_id='chat_cm'")
    elif change == "rename":
        db.execute("UPDATE chats SET name='A different account group' WHERE chat_id='chat_cm'")
    elif change == "rebind":
        db.execute("UPDATE lark_identities SET contact_id=%s WHERE open_id='ou_person'", (people["other"],))
    elif change == "deleted":
        db.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE chat_id='chat_cm'")
    elif change in {"hidden", "sync_disabled"}:
        column = "is_visible" if change == "hidden" else "sync_enabled"
        db.execute(f"UPDATE authorization_chats SET {column}=FALSE WHERE authorization_id=%s", (people["cm"],))
    elif change == "consent":
        db.execute("UPDATE authorizations SET body_consent_at=NOW()+INTERVAL '1 hour' WHERE id=%s", (people["cm"],))
    elif change == "mentions":
        db.execute(
            "UPDATE chat_messages SET mentions_json=%s::jsonb WHERE chat_id='chat_cm'",
            (json.dumps([{"key": "@_user_1", "id_type": "open_id", "id": "ou_other"}]),),
        )
    elif change == "p2p_counterpart":
        db.execute(
            "UPDATE authorization_chats SET p2p_counterpart_open_id='ou_other' WHERE authorization_id=%s",
            (people["cm"],),
        )
    elif change == "tenant":
        db.execute("UPDATE lark_identities SET tenant_classification='internal' WHERE open_id='ou_person'")
    elif change == "chat_mode":
        db.execute("UPDATE chats SET chat_mode='group' WHERE chat_id='chat_cm'")
    assert excel(people)["status"] == "pending"
    assert_no_formal(db)


def test_reanalysis_of_edited_evidence_is_new_source_even_when_scores_are_unchanged(db, people):
    ai(people)
    db.execute("UPDATE chat_messages SET message_update_time=clock_timestamp() WHERE chat_id='chat_cm'")
    ai(people)
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_sources WHERE source_type='ai'").fetchone()["n"] == 2
    assert excel(people)["status"] == "confirmed"


@pytest.mark.parametrize("state", ["deferred", "details_changed"])
def test_human_pause_or_edits_are_not_bypassed_by_later_excel(db, people, state):
    row = ai(people)
    scope = workflow.get_scope(people["cm"])
    if state == "deferred":
        workflow.decide_candidate(scope, row["id"], row["version"], "defer", request_id=uuid4())
    else:
        workflow.save_draft(
            scope, row["id"], row["version"], handle="abc", contact_id=people["person"], creator_type="cap"
        )
    result = excel(people)
    assert result["status"] == ("deferred" if state == "deferred" else "pending")
    if state == "details_changed":
        assert "details_changed" in {reason["code"] for reason in result["reasons"]}
    assert_no_formal(db)


def test_existing_identity_risk_prevents_auto_confirmation(db, people):
    ai(people)
    db.execute(
        "INSERT INTO audit_events(id,action,metadata) VALUES(%s,'creator_handle_identity_review_flagged',%s::jsonb)",
        (uuid4(), json.dumps({"contact_id": str(people["person"]), "handle": "abc"})),
    )
    assert excel(people)["status"] == "pending"
    assert_no_formal(db)


def test_conflicting_type_requires_manual_review_instead_of_automatic_relabel(db, people):
    excel(people)
    row = ai(people, creator_type="non_cap")
    assert row["creator_type"] == "cap" and row["status"] == "pending"
    assert "source_conflict" in {reason["code"] for reason in row["reasons"]}
    assert_no_formal(db)


def test_missing_type_blocks_manual_confirmation_and_non_cap_can_be_explicitly_confirmed(db, people):
    row = ai(people)
    scope = workflow.get_scope(people["cm"])
    assert row["creator_type"] is None and not row["can_confirm"]
    with pytest.raises(workflow.WorkflowInputError, match="选择 CAP 或非 CAP"):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    assert_no_formal(db)
    saved = workflow.save_draft(
        scope, row["id"], row["version"], handle="abc", contact_id=people["person"], creator_type="non_cap"
    )
    confirmed = workflow.confirm_candidate(scope, row["id"], saved["version"], request_id=uuid4())
    assert confirmed["creator_type"] == "non_cap" and confirmed["status"] == "confirmed"
    assert db.execute("SELECT creator_type,source FROM creator_account_assignments").fetchone() == {
        "creator_type": "non_cap", "source": "cm_confirmation"
    }


def test_type_filter_keeps_unknown_separate_and_combines_with_source(db, people):
    scope = workflow.get_scope(people["cm"])
    excel(people, handle="cap_account")
    fixtures.source(people, source_key="non-cap", handle="non_cap_account", creator_type="non_cap")
    ai(people, handle="unknown_account")
    for kind, handle in (("cap", "cap_account"), ("non_cap", "non_cap_account"), ("unknown", "unknown_account")):
        result = workflow.list_records(scope, type_filter=kind)
        assert result["total"] == 1 and [row["handle"] for row in result["items"]] == [handle]
    assert workflow.list_records(scope, type_filter="all")["total"] == 3
    assert workflow.list_records(scope, type_filter="unknown", source="excel")["total"] == 0
    with pytest.raises(workflow.WorkflowInputError, match="类型筛选"):
        workflow.list_records(scope, type_filter="everything")
