"""Business-evidence admission and optional account details; isolated PostgreSQL only."""

from uuid import uuid4

import pytest
import test_creator_workflow as fixtures

from app import creator_workflow as workflow

db = fixtures.db
people = fixtures.people


def ai_data(people, **changes):
    return {
        "business_policy_version": "business_v2",
        "creator_confidence": 0.95,
        "cm_confidence": 0.95,
        "exact_handle_evidence": True,
        "verified_business_dialogue": True,
        "direct_creator_evidence": True,
        "direct_cm_evidence": True,
        "unknowns": [],
        "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}],
        **changes,
    }


def ai(people, *, data=None, **changes):
    return fixtures.source(people, **{
        "source_type": "ai", "source_key": "ai-result", "source_data": ai_data(people) if data is None else data,
        "creator_type": None, "relationship": None, "handle": None, **changes,
    })


def excel(people, **changes):
    return fixtures.source(people, **{
        "source_key": "cap-sheet-row", "creator_type": "cap", "contact_id": None, "relationship": None,
        "source_data": {"cap_policy_version": "cap_v1", "creator_type": "cap", "type_evidence": "cm_excel_upload"},
        **changes,
    })


def test_verified_dialogue_confirms_a_person_without_handle_or_cap_and_deduplicates(db, people):
    row = ai(people)
    assert row["status"] == "confirmed" and row["handle"] is None and row["creator_type"] is None
    assert ai(people)["id"] == ai(people, source_key="other-conversation")["id"] == row["id"]
    formal = db.execute("SELECT * FROM creator_account_assignments").fetchall()
    assert len(formal) == 1 and formal[0]["social_account_id"] is None
    assert formal[0]["source"] == "ai_business_verified" and formal[0]["creator_type"] is None
    assert formal[0]["contact_id"] == people["person"] and formal[0]["relationship"] == "associated"
    for table in ("social_accounts", "contact_social_accounts", "contact_roles", "contact_assignments"):
        assert db.execute("SELECT COUNT(*) n FROM " + table).fetchone()["n"] == 0


def test_scores_direct_dialogue_and_new_policy_are_required_not_excel_or_identity_score(db, people):
    excel(people, contact_id=people["person"])
    for changes in (
        {"creator_confidence": 0.94}, {"cm_confidence": True}, {"cm_confidence": 1.01},
        {"direct_creator_evidence": False}, {"direct_cm_evidence": False},
        {"verified_business_dialogue": False}, {"business_policy_version": None},
        {"unknowns": ["responsibility is unclear"]},
    ):
        assert ai(people, data=ai_data(people, **changes))["status"] == "pending"
    row = ai(people, data=ai_data(people, identity_confidence=0, exact_handle_evidence=False))
    assert row["status"] == "confirmed" and row["handle"] is None and row["creator_type"] == "cap"


def test_excel_does_not_confirm_alone_but_adds_cap_to_a_verified_account(db, people):
    pending = excel(people)
    assert pending["status"] == "pending" and not pending["can_confirm"]
    row = ai(people, handle="abc")
    assert row["status"] == "confirmed" and row["creator_type"] == "cap"
    assert db.execute("SELECT COUNT(*) n FROM social_accounts").fetchone()["n"] == 1
    assert excel(people)["id"] == row["id"]


def test_manual_confirmation_allows_unknown_details_then_first_handle_is_audited_and_idempotent(db, people):
    row = fixtures.source(people, handle=None, creator_type=None, relationship=None)
    scope = workflow.get_scope(people["cm"])
    assert row["can_confirm"]
    row = workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    before = db.execute("SELECT id FROM creator_account_assignments").fetchone()["id"]
    request = uuid4()
    added = workflow.save_draft(scope, row["id"], row["version"], handle="abc",
                                contact_id=people["person"], creator_type="cap", request_id=request)
    assert workflow.save_draft(scope, row["id"], row["version"], handle="abc",
                               contact_id=people["person"], creator_type="cap", request_id=request) == added
    formal = db.execute("SELECT id,social_account_id,creator_type FROM creator_account_assignments").fetchone()
    assert formal["id"] == before and formal["social_account_id"] and formal["creator_type"] == "cap"
    assert added["status"] == "confirmed" and added["handle"] == "abc"
    for changes in ({"handle": "wrong"}, {"contact_id": people["other"]}, {"creator_type": "non_cap"}):
        with pytest.raises(workflow.WorkflowInputError):
            workflow.save_draft(scope, added["id"], added["version"],
                                **{"handle": "abc", "contact_id": people["person"], "creator_type": "cap", **changes})
    assert db.execute("SELECT COUNT(*) n FROM audit_events "
                      "WHERE action='creator_workflow_confirmed_details_added'").fetchone()["n"] == 1


def test_missing_user_stays_pending_and_same_person_cannot_be_assigned_to_another_cm(db, people):
    missing = fixtures.source(people, handle=None, contact_id=None)
    assert not missing["can_confirm"]
    with pytest.raises(workflow.WorkflowInputError):
        workflow.confirm_candidate(workflow.get_scope(people["cm"]), missing["id"], missing["version"],
                                   request_id=uuid4())
    row = ai(people)
    db.execute("UPDATE authorization_chats SET p2p_counterpart_open_id='ou_person' WHERE authorization_id=%s",
               (people["cm2"],))
    other = workflow.ingest_source(people["cm2"], source_type="excel", source_key="second-cm",
                                   source_data={}, responsible_id=people["cm2"], contact_id=people["person"])
    assert other["status"] == "pending" and not other["can_confirm"]
    with pytest.raises(workflow.WorkflowInputError, match="冲突"):
        workflow.confirm_candidate(workflow.get_scope(people["cm2"]), other["id"], other["version"],
                                   request_id=uuid4())
    formal = db.execute("SELECT candidate_id,responsible_authorization_id FROM creator_account_assignments").fetchall()
    assert formal == [{"candidate_id": workflow._uuid(row["id"]), "responsible_authorization_id": people["cm"]}]


def test_conflicting_handle_does_not_bind_or_block_a_verified_person(db, people):
    other = workflow.ingest_source(people["cm2"], source_type="excel", source_key="other-handle", source_data={},
                                   responsible_id=people["cm2"], contact_id=people["other"], handle="taken")
    workflow.confirm_candidate(workflow.get_scope(people["cm2"]), other["id"], other["version"], request_id=uuid4())
    row = ai(people, handle="taken")
    # The existing account is routed to admin instead of being silently relabelled.
    assert row["status"] == "pending" and row["responsible_id"] is None
    row = ai(people, source_key="person-only-evidence")
    assert row["status"] == "confirmed" and row["handle"] is None
    with pytest.raises(workflow.WorkflowInputError, match="冲突"):
        workflow.save_draft(workflow.get_scope(people["cm"]), row["id"], row["version"],
                            handle="taken", contact_id=people["person"])
    assert db.execute("SELECT COUNT(*) n FROM social_accounts").fetchone()["n"] == 1


def test_edited_evidence_and_new_uncertain_results_cannot_reuse_an_old_high_score(db, people):
    row = ai(people, uncertainties=["identity_uncertain"])
    db.execute("UPDATE chat_messages SET message_update_time=clock_timestamp() WHERE chat_id='chat_cm'")
    record = db.execute("UPDATE creator_workflow_candidates SET uncertainties='{}' WHERE id=%s RETURNING *",
                        (row["id"],)).fetchone()
    scope = workflow.get_scope(people["cm"])
    assert workflow._maybe_auto_confirm(db, record, scope)["status"] == "pending"
    # A new low-confidence result supersedes the old evidence, even under the same source key.
    row = ai(people, data=ai_data(people, cm_confidence=0.2))
    record = db.execute("SELECT * FROM creator_workflow_candidates WHERE id=%s", (row["id"],)).fetchone()
    assert workflow._maybe_auto_confirm(db, record, scope)["status"] == "pending"
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 0
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    with pytest.raises(PermissionError):
        ai(people)


def test_deferred_candidate_stays_paused_and_confirmed_cap_is_never_silently_relabelled(db, people):
    row = excel(people, contact_id=people["person"])
    scope = workflow.get_scope(people["cm"])
    workflow.decide_candidate(scope, row["id"], row["version"], "defer", request_id=uuid4())
    paused = ai(people, handle="abc")
    assert paused["status"] == "deferred"
    confirmed = workflow.confirm_candidate(scope, paused["id"], paused["version"], request_id=uuid4())
    changed = ai(people, handle="abc", creator_type="non_cap", source_key="type-conflict")
    assert changed["status"] == "pending" and changed["responsible_id"] is None
    assert workflow.get_record(scope, confirmed["id"])["creator_type"] == "cap"
