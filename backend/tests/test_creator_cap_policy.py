"""Three-condition creator admission; real PostgreSQL, no external model calls."""

from uuid import uuid4

import psycopg
import pytest
import test_creator_workflow as fixtures

from app import creator_workflow as workflow
from app.crypto import message_body_cipher

db = fixtures.db
people = fixtures.people


def ai_data(people, **changes):
    return {"staged_policy_version": workflow.STAGED_POLICY_VERSION, "preview_only": False,
            "stages": {key: {"verdict": "pass", "confidence": .98}
                       for key in ("identity", "ownership", "handle")},
            "creator_confidence": .98, "cm_confidence": .98, "identity_confidence": .98,
            "direct_creator_evidence": True, "direct_cm_evidence": True,
            "verified_business_dialogue": True, "exact_handle_evidence": True,
            "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}],
            **changes}


def ai(people, *, data=None, **changes):
    return fixtures.source(people, **{
        "source_type": "ai", "source_key": "ai-result", "source_data": ai_data(people) if data is None else data,
        "creator_type": None, "relationship": None, **changes})


def test_three_verified_conditions_auto_confirm_without_cap_and_deduplicate(db, people):
    row = ai(people)
    assert row["status"] == "confirmed" and row["handle"] == "abc" and row["creator_type"] is None
    assert row["admission_ready"] and row["handle_verified"]
    assert ai(people)["id"] == row["id"]
    formal = db.execute("SELECT * FROM creator_account_assignments").fetchall()
    assert len(formal) == 1 and formal[0]["social_account_id"] and formal[0]["contact_id"] == people["person"]
    assert formal[0]["source"] == "ai_business_verified" and formal[0]["creator_type"] is None


def test_no_legacy_score_excel_or_manual_click_can_bypass_ai_conditions(db, people):
    scope = workflow.get_scope(people["cm"])
    row = fixtures.source(people, uncertainties=["identity_uncertain", "ownership_uncertain"])
    assert not row["can_confirm"]
    saved = workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=people["person"],
                                handle_verified=True, resolved_reasons=["identity_uncertain", "ownership_uncertain"])
    assert saved["handle_verified"] and not saved["can_confirm"]
    for changes in (
        {"staged_policy_version": None}, {"preview_only": True}, {"creator_confidence": .94},
        {"cm_confidence": True}, {"direct_creator_evidence": False}, {"direct_cm_evidence": False},
        {"verified_business_dialogue": False},
        {"stages": {"identity": {"verdict": "fail", "confidence": .99}}},
    ):
        row = ai(people, data=ai_data(people, **changes))
        assert row["status"] == "pending" and not row["can_confirm"]
        with pytest.raises(workflow.WorkflowInputError):
            workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    assert not db.execute("SELECT 1 FROM creator_account_assignments").fetchone()


def test_missing_handle_or_user_stays_pending_and_manual_handle_only_satisfies_third_gate(db, people):
    row = ai(people, handle=None, data=ai_data(people, stages={
        "identity": {"verdict": "pass", "confidence": .98},
        "ownership": {"verdict": "pass", "confidence": .98},
        "handle": {"verdict": "unknown", "confidence": 0},
    }, exact_handle_evidence=False, identity_confidence=0))
    assert row["status"] == "pending" and row["processing_stage"] == "handle"
    scope = workflow.get_scope(people["cm"])
    with pytest.raises(workflow.WorkflowInputError, match="handle"):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    filled = workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=people["person"])
    assert not filled["can_confirm"] and not filled["handle_verified"]
    request = uuid4()
    confirmed = workflow.save_draft(scope, filled["id"], filled["version"], handle="abc", contact_id=people["person"],
                                    handle_verified=True, request_id=request)
    assert confirmed["status"] == "confirmed" and confirmed["stages"]["handle"]["source"] == "cm"
    assert workflow.save_draft(scope, filled["id"], filled["version"], handle="abc", contact_id=people["person"],
                               handle_verified=True, request_id=request) == confirmed
    missing = fixtures.source(people, source_key="no-user", handle="second", contact_id=None)
    assert not missing["can_confirm"] and missing["processing_stage"] == "identity"


def test_confirmed_handle_correction_ends_old_relationship_until_new_handle_verified(db, people):
    row = ai(people)
    scope = workflow.get_scope(people["cm"])
    corrected = workflow.save_draft(scope, row["id"], row["version"], handle="corrected", contact_id=people["person"])
    assert corrected["status"] == "pending" and corrected["processing_stage"] == "handle"
    assert not db.execute("SELECT 1 FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()
    assert db.execute("SELECT status FROM contact_social_accounts").fetchone()["status"] == "rejected"
    ready = workflow.save_draft(scope, corrected["id"], corrected["version"], handle="corrected",
                               contact_id=people["person"], handle_verified=True)
    assert ready["status"] == "confirmed" and ready["stages"]["identity"]["verdict"] == "pass"
    assert db.execute("SELECT count(*) n FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()["n"] == 1


def test_user_correction_and_switching_back_do_not_reuse_invalidated_ai(db, people):
    row = ai(people)
    scope = workflow.get_scope(people["cm"])
    db.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_type,p2p_counterpart_open_id) "
               "VALUES(%s,'chat_cm2','user','ou_other')", (people["cm"],))
    changed = workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=people["other"],
                                 handle_verified=True)
    assert changed["status"] == "pending" and changed["stages"]["identity"]["verdict"] == "unknown"
    restored = workflow.save_draft(scope, changed["id"], changed["version"], handle="abc", contact_id=people["person"],
                                  handle_verified=True)
    assert not restored["can_confirm"] and restored["processing_stage"] == "identity"
    assert not db.execute("SELECT 1 FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()


def test_latest_negative_result_withdraws_old_formal_and_is_not_pending(db, people):
    row = ai(people)
    failed = ai(people, data=ai_data(people, stages={"identity": {"verdict": "fail", "confidence": .98}}))
    assert failed["id"] == row["id"] and failed["status"] == "pending" and not failed["can_confirm"]
    summary = workflow.list_records(workflow.get_scope(people["cm"]))["summary"]
    assert summary["confirmed"] == summary["pending"] == 0 and summary["excluded"] == 1
    assert workflow.list_records(workflow.get_scope(people["cm"]), status="pending")["total"] == 0
    assert not db.execute("SELECT 1 FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()


def test_evidence_revision_and_live_scope_are_rechecked_at_manual_confirmation(db, people):
    row = fixtures.approve(people, fixtures.source(people))
    scope = workflow.get_scope(people["cm"])
    cipher = message_body_cipher(b"x" * 32)
    db.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat_cm'",
               (cipher.encrypt("Creator business evidence"),))
    assert workflow.get_record(scope, row["id"])["can_confirm"]
    assert workflow.list_records(scope)["items"][0]["can_confirm"]
    db.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat_cm'",
               (cipher.encrypt("Changed body without a provider update timestamp"),))
    assert not workflow.get_record(scope, row["id"])["can_confirm"]
    assert not workflow.list_records(scope)["items"][0]["can_confirm"]
    with pytest.raises(workflow.WorkflowInputError):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    listed = workflow.list_records(scope)["items"][0]
    assert not listed["can_confirm"] and listed["contact"] is None
    assert any(item["source_data"].get("unavailable") for item in listed["evidence"])
    with pytest.raises(PermissionError):
        ai(people)


def test_excel_adds_optional_cap_but_cannot_replace_formal_user_or_owner(db, people):
    row = ai(people)
    excel = fixtures.source(people, source_key="cap-sheet")
    assert excel["id"] == row["id"] and excel["creator_type"] == "cap" and excel["status"] == "confirmed"
    foreign = workflow.ingest_source(people["cm2"], source_type="excel", source_key="other-cm", source_data={},
                                     responsible_id=people["cm2"], contact_id=people["other"], handle="abc")
    assert foreign["responsible_id"] is None and not foreign["can_confirm"]
    assignment = db.execute(
        "SELECT responsible_authorization_id FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()
    assert assignment["responsible_authorization_id"] == people["cm"]


def test_formal_database_constraint_rejects_missing_handle_and_deferred_does_not_auto_admit(db, people):
    row = fixtures.source(people)
    scope = workflow.get_scope(people["cm"])
    workflow.decide_candidate(scope, row["id"], row["version"], "defer", request_id=uuid4())
    ready = ai(people)
    assert ready["status"] == "deferred" and ready["can_confirm"]
    formal = workflow.confirm_candidate(scope, ready["id"], ready["version"], request_id=uuid4())
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("UPDATE creator_workflow_candidates SET normalized_handle=NULL WHERE id=%s", (formal["id"],))


def test_owner_reassignment_ends_formal_and_invalidates_old_ai(db, people):
    row = ai(people)
    owner_scope = workflow.get_scope(people["owner"])
    changed = workflow.assign_candidate(owner_scope, row["id"], row["version"], people["cm2"], request_id=uuid4())
    assert changed["status"] == "pending" and not changed["admission_ready"]
    assert not db.execute("SELECT 1 FROM creator_account_assignments WHERE ended_at IS NULL").fetchone()


def test_manual_identity_corrections_cancel_only_the_old_person_scan_without_resetting_budget(db, people):
    row = fixtures.source(people)
    scope = workflow.get_scope(people["cm"])
    other = fixtures.source(people, source_key="unmatched", handle="unmatched", contact_id=None)
    run_id, task_id = uuid4(), uuid4()
    db.execute(
        "INSERT INTO creator_workflow_ai_runs(id,responsible_authorization_id,requested_by_authorization_id,"
        "status,history_days,chat_limit,window_start,window_end,scan_mode) "
        "VALUES(%s,%s,%s,'running',0,3,NOW()-INTERVAL '1 day',NOW(),'person_staged_v1')",
        (run_id, people["cm"], people["cm"]),
    )
    db.execute(
        "INSERT INTO creator_workflow_ai_tasks(id,run_id,chat_id,contact_id,input_fingerprint,initial_fingerprint,"
        "status,attempts,stage,stage_attempts,retry_count,lease_until) "
        "VALUES(%s,%s,'chat_cm',%s,'frozen','initial','running',3,'handle',1,1,NOW()+INTERVAL '10 minutes')",
        (task_id, run_id, people["person"]),
    )
    # An unrelated draft does not stop this person's work.
    workflow.save_draft(scope, other["id"], other["version"], handle="changed", contact_id=None)
    untouched = db.execute("SELECT status FROM creator_workflow_ai_tasks WHERE id=%s", (task_id,)).fetchone()
    assert untouched["status"] == "running"
    denied = workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4())
    assert denied["status"] == "rejected" and denied["responsible_id"] is None
    task = db.execute("SELECT * FROM creator_workflow_ai_tasks WHERE id=%s", (task_id,)).fetchone()
    assert task["status"] == "cancelled" and task["lease_until"] is None
    assert (task["attempts"], task["stage_attempts"], task["retry_count"]) == (3, 1, 1)
    run = db.execute("SELECT status FROM creator_workflow_ai_runs WHERE id=%s", (run_id,)).fetchone()
    assert run["status"] == "cancelled"
