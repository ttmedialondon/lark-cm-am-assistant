"""Saved-candidate rechecking never becomes automatic identity confirmation."""

from uuid import uuid4

import pytest
import test_creator_workflow as fixtures
import test_creator_workflow_http_import as http_fixtures

from app import creator_workflow as workflow

db = fixtures.db
people = fixtures.people
source = fixtures.source
client = http_fixtures.client


def test_recheck_returns_only_visible_users_and_does_not_select_or_write(db, people):
    row = source(people, contact_id=None)
    account = uuid4()
    db.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok','abc','@abc')",
        (account,),
    )
    for person, relationship in (("person", "owner"), ("other", "representative")):
        db.execute(
            "UPDATE lark_identities SET display_name='abc name' WHERE contact_id=%s", (people[person],)
        )
        db.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
            VALUES(%s,%s,%s,'confirmed','test')""", (people[person], account, relationship),
        )
    before = db.execute("SELECT count(*) n FROM audit_events").fetchone()["n"]
    scope = workflow.get_scope(people["cm"])
    result = workflow.recheck_candidate(scope, row["id"], row["version"])
    assert [u["contact_id"] for u in result["items"]] == [str(people["person"])]
    assert result["items"][0]["match_type"] == "existing_account"
    assert any(r["code"] == "missing_user" for r in result["reasons"])
    assert workflow.get_record(scope, row["id"])["contact_id"] is None
    assert db.execute("SELECT count(*) n FROM audit_events").fetchone()["n"] == before
    assert db.execute("SELECT count(*) n FROM creator_account_assignments").fetchone()["n"] == 0


def test_recheck_does_not_infer_identity_from_a_private_chat_and_names_are_only_suggestions(db, people):
    scope = workflow.get_scope(people["cm"])
    row = source(people, contact_id=None)
    assert workflow.recheck_candidate(scope, row["id"], row["version"])["items"] == []
    db.execute("UPDATE lark_identities SET display_name='abc possible match'")
    result = workflow.recheck_candidate(scope, row["id"], row["version"])
    assert len(result["items"]) == 1
    assert result["items"][0]["match_type"] == "name_match"
    assert "仅供参考" in result["items"][0]["match_label"]
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    assert workflow.recheck_candidate(scope, row["id"], row["version"])["items"] == []


def test_recheck_requires_current_cm_pending_record_and_version(db, people):
    row = source(people, handle=None)
    scope = workflow.get_scope(people["cm"])
    assert "请先补充 handle" in workflow.recheck_candidate(scope, row["id"], row["version"])["notice"]
    for actor in ("owner", "reviewer"):
        with pytest.raises(PermissionError):
            workflow.recheck_candidate(workflow.get_scope(people[actor], people["cm"]), row["id"], row["version"])
    with pytest.raises(PermissionError):
        workflow.recheck_candidate(workflow.get_scope(people["cm2"]), row["id"], row["version"])
    with pytest.raises(workflow.WorkflowInputError, match="刚刚发生"):
        workflow.recheck_candidate(scope, row["id"], row["version"] + 1)
    formal = source(people, source_key="formal", trusted_identity=True, source_data={"stable_open_id": "ou_person"})
    with pytest.raises(workflow.WorkflowInputError, match="只有待确认"):
        workflow.recheck_candidate(scope, formal["id"], formal["version"])


def test_recheck_reports_conflict_without_exposing_out_of_scope_user(db, people):
    row = source(people)
    account = uuid4()
    db.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok','abc','@abc')",
        (account,),
    )
    db.execute(
        """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
        VALUES(%s,%s,'owner','confirmed','test')""", (people["other"], account),
    )
    db.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) VALUES(%s,%s,%s,'manual')",
        (uuid4(), people["other"], people["cm2"]),
    )
    result = workflow.recheck_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"])
    assert {r["code"] for r in result["reasons"]} == {"ownership_conflict", "identity_conflict"}
    assert result["items"] == [] and "不能直接入库" in result["notice"]
    assert str(people["other"]) not in str(result) and "ou_other" not in str(result)


def test_identity_edits_are_ready_for_one_manual_confirmation_without_separate_review(db, people):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    changed = workflow.save_draft(
        scope, row["id"], row["version"], handle="corrected", contact_id=people["person"], relationship="owner",
    )
    assert changed["can_confirm"] and changed["blocking_reasons"] == []
    assert [r["code"] for r in changed["reasons"]] == ["details_changed"]
    changed = workflow.save_draft(
        scope, changed["id"], changed["version"], handle="corrected_again", contact_id=people["person"],
        relationship="owner", resolved_reasons=["details_changed"],
    )
    assert changed["can_confirm"]
    assert [r["code"] for r in changed["reasons"]] == ["details_changed"]
    result = workflow.recheck_candidate(scope, changed["id"], changed["version"])
    assert result["reasons"] == changed["reasons"]
    reviewed = workflow.save_draft(
        scope, changed["id"], changed["version"], handle="corrected_again", contact_id=people["person"],
        relationship="owner", resolved_reasons=["details_changed"],
    )
    assert reviewed["can_confirm"] and reviewed["status"] == "pending"
    relation_changed = workflow.save_draft(
        scope, reviewed["id"], reviewed["version"], handle="corrected_again", contact_id=people["person"],
        relationship="representative",
    )
    assert relation_changed["can_confirm"]
    assert db.execute("SELECT count(*) n FROM creator_account_assignments").fetchone()["n"] == 0


def test_recheck_http_requires_saved_version_but_no_csrf_for_read_only_request(client, monkeypatch):
    api, _, scope = client
    candidate_id = uuid4()
    calls = []
    monkeypatch.setattr(
        workflow, "recheck_candidate", lambda *args: calls.append(args) or {"items": [], "notice": "checked"},
    )
    url = f"/dashboard/creators/{candidate_id}/recheck"
    for value in ("", "?version=bad", "?version=1.1"):
        assert api.get(url + value).status_code == 409
    assert calls == []
    result = api.get(url + "?version=3")
    assert result.status_code == 200
    assert result.headers["cache-control"].startswith("no-store")
    assert calls == [(scope, candidate_id, 3)]
