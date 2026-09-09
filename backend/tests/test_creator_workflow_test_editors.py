"""Real SQL regression for the two named testers, without granting general delegation."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
import test_creator_workflow as workflow_fixtures

from app import creator_workflow as workflow
from app import creator_workflow_ai as ai
from app import creator_workflow_import as imports

db = workflow_fixtures.db
people = workflow_fixtures.people
source = workflow_fixtures.source


@pytest.fixture
def test_editors(people, monkeypatch):
    # Reuse isolated fixture identities, never seed real employee IDs into tests.
    monkeypatch.setattr(workflow, "TEST_CM_ID", people["cm"])
    monkeypatch.setattr(workflow, "TEST_EDITOR_IDS", frozenset({people["owner"], people["reviewer"]}))


@pytest.mark.parametrize("actor", ["owner", "reviewer"])
def test_named_testers_can_review_and_confirm_for_the_cm(db, people, test_editors, actor):
    scope = workflow.get_scope(people[actor], people["cm"])
    assert scope["test_editor"] and scope["can_write"] and not scope["readonly"]
    assert scope["actor_id"] == str(people[actor])
    assert scope["responsible_id"] == str(people["cm"])
    assert [user["contact_id"] for user in workflow.search_users(scope, "Same")] == [str(people["person"])]

    row = source(people, handle=None, contact_id=None)
    with pytest.raises(PermissionError):
        workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=people["other"])
    draft = workflow.save_draft(
        scope, row["id"], row["version"], handle="abc", contact_id=people["person"], relationship="owner"
    )
    recheck = workflow.recheck_candidate(scope, draft["id"], draft["version"])
    assert recheck["candidate_id"] == draft["id"]
    assert workflow.get_record(scope, draft["id"])["status"] == "pending"
    checked = workflow.save_draft(
        scope, draft["id"], draft["version"], handle="abc", contact_id=people["person"],
        relationship="owner", resolved_reasons=["details_changed"],
    )
    formal = workflow.confirm_candidate(scope, checked["id"], checked["version"], request_id=uuid4())
    assert formal["status"] == "confirmed" and formal["responsible_id"] == str(people["cm"])
    assignment = db.execute("SELECT * FROM creator_account_assignments").fetchone()
    assert assignment["responsible_authorization_id"] == people["cm"]
    assert assignment["created_by_authorization_id"] == people[actor]
    mapping = db.execute("SELECT * FROM contact_social_accounts").fetchone()
    assert mapping["reviewed_by_authorization_id"] == people[actor]
    audit = db.execute(
        "SELECT authorization_id, metadata FROM audit_events WHERE action='creator_workflow_confirmed'"
    ).fetchone()
    assert audit["authorization_id"] == people[actor]
    assert audit["metadata"]["after"]["responsible_authorization_id"] == str(people["cm"])
    assert db.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 0


@pytest.mark.parametrize("actor", ["owner", "reviewer"])
@pytest.mark.parametrize("decision", ["defer", "reject"])
def test_named_testers_can_defer_or_reject_with_actual_actor_audit(db, people, test_editors, actor, decision):
    scope = workflow.get_scope(people[actor], people["cm"])
    row = source(people)
    result = workflow.decide_candidate(scope, row["id"], row["version"], decision, request_id=uuid4())
    assert result["status"] == {"defer": "deferred", "reject": "rejected"}[decision]
    assert result["responsible_id"] == (str(people["cm"]) if decision == "defer" else None)
    audit = db.execute(
        "SELECT authorization_id FROM audit_events WHERE action=%s", ("creator_workflow_" + decision,)
    ).fetchone()
    assert audit["authorization_id"] == people[actor]


@pytest.mark.parametrize("actor", ["owner", "reviewer"])
def test_testing_exception_does_not_cover_other_cms(db, people, test_editors, actor):
    scope = workflow.get_scope(people[actor], people["cm2"])
    assert not scope["test_editor"] and not scope["can_write"] and scope["readonly"]
    with pytest.raises(PermissionError):
        workflow.search_users(scope, "Same")
    assert workflow.get_scope(people["cm"])["can_write"]
    assert workflow.get_scope(people["cm2"])["can_write"]


def test_other_reviewers_or_allowlisted_users_without_grants_cannot_write(db, people, test_editors, monkeypatch):
    db.execute(
        "INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-review')",
        (people["cm2"],),
    )
    scope = workflow.get_scope(people["cm2"], people["cm"])
    assert scope["readonly"] and not scope["test_editor"]
    with pytest.raises(PermissionError):
        workflow.search_users(scope, "Same")
    monkeypatch.setattr(workflow, "TEST_EDITOR_IDS", frozenset({people["cm2"]}))
    db.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (people["cm2"],))
    with pytest.raises(PermissionError):
        workflow.get_scope(people["cm2"], people["cm"])


@pytest.mark.parametrize("revocation", ["review_grant", "owner_role", "actor_inactive", "cm_inactive", "cm_ineligible"])
def test_old_test_editor_scope_cannot_write_after_revocation(db, people, test_editors, revocation):
    actor = "owner" if revocation == "owner_role" else "reviewer"
    scope = workflow.get_scope(people[actor], people["cm"])
    row = source(people)
    if revocation == "review_grant":
        db.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (people[actor],))
    elif revocation == "owner_role":
        db.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (people[actor],))
    elif revocation == "actor_inactive":
        db.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (people[actor],))
    elif revocation == "cm_inactive":
        db.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (people["cm"],))
    else:
        db.execute("UPDATE authorizations SET assignment_eligible=FALSE WHERE id=%s", (people["cm"],))
    for operation in (
        lambda: workflow.search_users(scope, "Same"),
        lambda: workflow.recheck_candidate(scope, row["id"], row["version"]),
        lambda: workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=None),
        lambda: workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4()),
        lambda: workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4()),
    ):
        with pytest.raises(PermissionError):
            operation()
    assert db.execute("SELECT count(*) n FROM creator_account_assignments").fetchone()["n"] == 0
    assert db.execute("SELECT status FROM creator_workflow_candidates WHERE id=%s", (row["id"],)).fetchone()[
        "status"
    ] == "pending"


def test_review_tester_gains_no_cross_cm_import_or_ai_scan(db, people, test_editors, monkeypatch):
    scope = workflow.get_scope(people["reviewer"], people["cm"])
    assert scope["can_write"] and not scope["can_import"] and not scope["can_scan"]
    assert not scope["can_admin"]
    owner = workflow.get_scope(people["owner"], people["cm"])
    assert owner["can_import"] and owner["can_scan"]
    own = workflow.get_scope(people["cm"])
    assert own["can_import"] and own["can_scan"]
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(db))
    with pytest.raises(PermissionError):
        imports.preview(scope, b"not-read-before-permission-check", "test.xlsx")
    with pytest.raises(PermissionError):
        imports.commit(scope, uuid4(), uuid4())
    with pytest.raises(PermissionError):
        ai._authorize(db, people["reviewer"], people["cm"])
    with pytest.raises(PermissionError):
        workflow.ingest_source(
            people["reviewer"], responsible_id=people["cm"], source_type="excel", source_key="forbidden",
            source_data={}, handle="abc", contact_id=people["person"], relationship="owner",
        )
