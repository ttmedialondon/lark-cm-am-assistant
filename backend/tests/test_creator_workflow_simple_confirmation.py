"""A manual click confirms the handle/user pair, not an ownership classification."""

from uuid import uuid4

import pytest
import test_creator_workflow as fixtures

from app import creator_workflow as workflow

db = fixtures.db
people = fixtures.people
source = fixtures.source


def test_one_click_resolves_historical_flags_but_preserves_source_and_before_audit(db, people):
    relationship = None
    row = source(
        people, relationship=relationship,
        uncertainties=["identity_uncertain", "ownership_uncertain", "handle_uncertain", "identity_risk"],
    )
    scope = workflow.get_scope(people["cm"])
    assert row["can_confirm"] and row["blocking_reasons"] == [] and row["status"] == "pending"
    source_before = db.execute("SELECT * FROM creator_workflow_sources").fetchall()
    confirmed = workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    assert confirmed["status"] == "confirmed" and confirmed["relationship"] == "associated"
    assert db.execute("SELECT * FROM creator_workflow_sources").fetchall() == source_before
    audit = db.execute("SELECT metadata FROM audit_events WHERE action='creator_workflow_confirmed'").fetchone()[
        "metadata"
    ]
    assert audit["before"]["relationship"] == relationship
    assert audit["before"]["uncertainties"] and audit["after"]["uncertainties"] == []
    assert db.execute("SELECT count(*) n FROM contact_roles").fetchone()["n"] == 0


@pytest.mark.parametrize("edit", ["handle", "contact"])
def test_identity_edit_does_not_carry_old_relationship_and_needs_no_extra_acknowledgement(db, people, edit):
    row = source(people, relationship="representative")
    scope = workflow.get_scope(people["cm"])
    # Make the second user genuinely visible to this CM, never bypass the scope check.
    db.execute(
        "INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_type,p2p_counterpart_open_id) "
        "VALUES(%s,'chat_cm2','user','ou_other')", (people["cm"],),
    )
    saved = workflow.save_draft(
        scope, row["id"], row["version"], handle="corrected" if edit == "handle" else "abc",
        contact_id=people["other"] if edit == "contact" else people["person"],
    )
    assert saved["relationship"] is None and saved["can_confirm"] and saved["status"] == "pending"
    assert any(r["code"] == "details_changed" for r in saved["reasons"])
    formal = workflow.confirm_candidate(scope, saved["id"], saved["version"], request_id=uuid4())
    assert formal["status"] == "confirmed" and formal["relationship"] == "associated"


def test_same_user_reuses_existing_formal_relationship_without_overwriting_it(db, people):
    relationship = "representative"
    account_id = uuid4()
    db.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok','abc','@abc')",
        (account_id,),
    )
    db.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,%s,'confirmed','existing')", (people["person"], account_id, relationship),
    )
    before = db.execute("SELECT * FROM contact_social_accounts").fetchall()
    row = source(people, relationship=None)
    assert row["can_confirm"]
    formal = workflow.confirm_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"], request_id=uuid4())
    assert formal["relationship"] == relationship
    assert db.execute("SELECT * FROM contact_social_accounts").fetchall() == before


def test_missing_relationship_cannot_bypass_existing_formal_user_conflict(db, people):
    existing_relation = "associated"
    account_id = uuid4()
    db.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok','abc','@abc')",
        (account_id,),
    )
    db.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,%s,'confirmed','existing')", (people["other"], account_id, existing_relation),
    )
    row = source(people, relationship=None, uncertainties=["identity_uncertain"])
    assert not row["can_confirm"]
    assert {r["code"] for r in row["blocking_reasons"]} == {"identity_conflict"}
    with pytest.raises(workflow.WorkflowInputError, match="冲突"):
        workflow.confirm_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"], request_id=uuid4())
    saved = db.execute("SELECT relationship,uncertainties FROM creator_workflow_candidates").fetchone()
    assert saved["relationship"] is None and saved["uncertainties"] == ["identity_uncertain"]
    assert db.execute("SELECT count(*) n FROM creator_account_assignments").fetchone()["n"] == 0


def test_later_sources_do_not_replace_manual_association_with_a_guessed_owner_role(db, people):
    row = source(people)
    formal = workflow.confirm_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"], request_id=uuid4())
    extra = source(people, source_key="new-excel-source", trusted_identity=True,
                   source_data={"stable_open_id": "ou_person"})
    assert extra["id"] == formal["id"] and extra["relationship"] == "associated"
    assert db.execute("SELECT count(*) n FROM creator_workflow_candidates").fetchone()["n"] == 1
    assert db.execute("SELECT relationship FROM contact_social_accounts").fetchone()["relationship"] == "associated"


def test_handle_arriving_later_reuses_the_same_person_candidate(db, people):
    missing_handle = source(people, handle=None)
    known = source(people, source_key="known-handle")
    scope = workflow.get_scope(people["cm"])
    assert missing_handle["id"] == known["id"] and known["can_confirm"]
    formal = workflow.confirm_candidate(scope, known["id"], known["version"], request_id=uuid4())
    assert formal["status"] == "confirmed" and formal["relationship"] == "associated"
    assert db.execute("SELECT count(*) n FROM creator_account_assignments").fetchone()["n"] == 1
