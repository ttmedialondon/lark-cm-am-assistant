"""Independent regression checks for the import and evidence trust boundaries."""

import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_creator_workflow_http_import as fixtures
from openpyxl import Workbook

from app import creator_workflow as workflow
from app import creator_workflow_evidence as evidence
from app import creator_workflow_import as imports
from app.crypto import message_body_cipher

client = fixtures.client
db = fixtures.db
people = fixtures.people
real_import = fixtures.real_import
workbook = fixtures.workbook


def test_draft_without_relationship_never_defaults_to_creator_owner(client, monkeypatch):
    api, _, _ = client
    selected = []
    monkeypatch.setattr(
        workflow, "save_draft", lambda *_args, **kwargs: selected.append(kwargs) or {"status": "pending"}
    )
    result = api.post(
        f"/dashboard/creators/{uuid4()}/draft",
        headers={"X-CSRF-Token": "csrf"},
        json={"request_id": str(uuid4()), "version": 1, "handle": "alice", "contact_id": str(uuid4())},
    )
    assert result.status_code == 200
    assert selected[0]["relationship"] is None


def test_ordinary_cm_handle_import_uses_current_upload_scope(db, people, real_import):
    preview = imports.preview(real_import, workbook([["handle", "CM"], ["owner_unknown", None]]), "missing.xlsx")
    assert preview["rows"][0]["responsible_id"] == str(people["cm"])
    result = imports.commit(real_import, preview["batch_id"], uuid4())
    assert result["pending"] == 1
    assert (
        db.execute("SELECT responsible_authorization_id FROM creator_workflow_candidates").fetchone()[
            "responsible_authorization_id"
        ]
        == people["cm"]
    )


def test_excel_existing_owner_only_prefills_candidate_and_ignores_relationship_column(db, people, real_import):
    account = uuid4()
    db.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok','alice','alice')",
        (account,),
    )
    db.execute(
        """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
           VALUES(%s,%s,'owner','confirmed','verified_existing')""",
        (people["person"], account),
    )
    preview = imports.preview(
        real_import, workbook([["handle", "CM", "relationship"], ["alice", "cm", "representative"]]), "a.xlsx"
    )
    assert preview["rows"][0]["relationship"] is None
    assert preview["rows"][0]["contact_id"] == str(people["person"])
    assert preview["rows"][0]["trusted_identity"] is False
    assert "missing_relationship" not in preview["rows"][0]["reasons"]
    assert preview["rows"][0]["status"] == "pending"
    assert imports.commit(real_import, preview["batch_id"], uuid4())["formal"] == 0
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 0
    assert db.execute("SELECT relationship FROM contact_social_accounts").fetchone()["relationship"] == "owner"


def test_parse_all_populated_sheets_not_just_first():
    book = Workbook()
    book.active.title = "CM One"
    book.active.append(["handle", "CM"])
    book.active.append(["alice", "Remi"])
    other = book.create_sheet("CM Two")
    other.append(["handle", "CM"])
    other.append(["bob", "Remi"])
    stream = io.BytesIO()
    book.save(stream)
    rows = imports.parse_workbook(stream.getvalue(), "two-sheets.xlsx")
    assert {(r["sheet"], r["handle"]) for r in rows} == {("CM One", "alice"), ("CM Two", "bob")}


def test_unknown_cm_evidence_allows_only_current_valid_source_chats_for_owner(db, people, real_import):
    row = workflow.ingest_source(
        people["owner"],
        source_type="ai",
        source_key="unknown-cm-evidence",
        source_data={
            "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}]
        },
        handle="unknown_owner",
        responsible_id=None,
        uncertainties=["ownership_uncertain"],
    )
    owner = workflow.get_scope(people["owner"], None)
    current = evidence.evidence(owner, row["id"])
    assert [chat["chat_id"] for chat in current["conversations"]] == ["chat_cm"]
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    assert evidence.evidence(owner, row["id"])["conversations"] == []


def test_excel_source_preserves_original_spelling(db, people, real_import):
    preview = imports.preview(real_import, workbook([["handle", "CM"], ["@Original.Name", "cm"]]), "original.xlsx")
    imports.commit(real_import, preview["batch_id"], uuid4())
    row = db.execute("SELECT source_data FROM creator_workflow_sources").fetchone()
    assert row["source_data"]["original_handle"] == "@Original.Name"


def test_unassigned_source_messages_are_deduplicated_and_scope_does_not_expand(db, people, real_import, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    monkeypatch.setattr(evidence, "get_settings", lambda: settings)
    cipher = message_body_cipher(settings.token_encryption_key)
    db.execute(
        "UPDATE chat_messages SET body_ciphertext=%s,message_type='text' WHERE chat_id='chat_cm'",
        (cipher.encrypt(json.dumps({"text": "Exact source evidence a@example.com +44 1234 567890 ou_PrivateId"})),),
    )
    # Two authorized sources observed one canonical message. The other CM also
    # has a different chat, but that unrelated chat must never be exposed here.
    db.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat_cm')", (people["cm2"],))
    db.execute(
        """INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
           VALUES(%s,'chat_cm','message1')""",
        (people["cm2"],),
    )
    refs = [
        {"authorization_id": str(people[key]), "chat_id": "chat_cm", "message_id": "message1"} for key in ("cm", "cm2")
    ]
    row = workflow.ingest_source(
        people["owner"],
        source_type="ai",
        source_key="two-grants-one-message",
        source_data={"evidence": refs},
        handle="unclear_cm",
        responsible_id=None,
        uncertainties=["ownership_uncertain"],
    )
    scope = workflow.get_scope(people["owner"], None)
    result = evidence.evidence(scope, row["id"], "chat_cm")
    assert [c["chat_id"] for c in result["conversations"]] == ["chat_cm"]
    assert len(result["messages"]) == 1
    text = result["messages"][0]["text"]
    assert text.startswith("Exact source evidence")
    assert "a@example.com" not in text and "1234" not in text and "ou_PrivateId" not in text
    assert "邮箱已隐藏" in text and "电话号码已隐藏" in text and "标识已隐藏" in text
    assert result["pages"] == 1
    with pytest.raises(PermissionError):
        evidence.evidence(scope, row["id"], "chat_cm2")
    db.execute(
        "UPDATE authorizations SET body_consent_at=NOW()+INTERVAL '1 hour' WHERE id=ANY(%s)",
        ([people["cm"], people["cm2"]],),
    )
    assert evidence.evidence(scope, row["id"])["conversations"] == []


def test_revoked_viewer_cannot_use_previous_owner_evidence_scope(db, people, real_import):
    row = workflow.ingest_source(
        people["owner"],
        source_type="ai",
        source_key="revoked-owner",
        handle="alice",
        responsible_id=None,
        source_data={
            "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}]
        },
    )
    scope = workflow.get_scope(people["owner"], None)
    db.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (people["owner"],))
    with pytest.raises(PermissionError):
        evidence.evidence(scope, row["id"])
