"""Real SQL import/evidence checks plus the session/CSRF boundary."""

import io
import json
import zipfile
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
import test_creator_workflow as workflow_fixtures
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import creator_workflow as workflow
from app import creator_workflow_evidence as evidence
from app import creator_workflow_import as imports
from app import creator_workflow_routes as routes

db = workflow_fixtures.db
people = workflow_fixtures.people


def workbook(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_parse_missing_handle_and_literal_dash_are_preserved_as_null():
    rows = imports.parse_workbook(workbook([["creator handle", "CM"], ["—", "Remi"], [None, "Remi"]]), "file.xlsx")
    assert len(rows) == 2 and all(row["handle"] is None for row in rows)
    assert [row["row"] for row in rows] == [2, 3]


def test_combined_cm_header_is_checked_instead_of_ignored():
    row = imports.parse_workbook(workbook([["handle", "CM / TAP CM"], ["abc", "another_cm"]]), "cap.xlsx")[0]
    assert row["cm"] == "another_cm"


def test_lark_a1_dimension_does_not_silently_drop_rows():
    raw = workbook([["handle", "CM"], ["abc", "Remi"]])
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(stream, "w") as changed:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b'<dimension ref="A1:B2"', b'<dimension ref="A1"')
            changed.writestr(item.filename, data)
    assert imports.parse_workbook(stream.getvalue(), "file.xlsx")[0]["handle"] == "abc"


@pytest.mark.parametrize("raw,name", [(b"not a zip", "file.xlsx"), (b"x", "file.xls")])
def test_bad_uploads_fail_without_rows(raw, name):
    with pytest.raises(workflow.WorkflowInputError):
        imports.parse_workbook(raw, name)


def test_formula_is_not_executed_or_used_as_identity():
    rows = imports.parse_workbook(
        workbook([["handle", "CM"], ['=HYPERLINK("https://example.invalid")', "Remi"]]), "a.xlsx"
    )
    assert rows[0]["formula"] is True


@pytest.fixture
def client(monkeypatch):
    actor = {"id": uuid4(), "status": "active"}
    scope = {
        "actor_id": str(actor["id"]),
        "responsible_id": str(actor["id"]),
        "can_write": True,
        "readonly": False,
        "is_owner": False,
        "can_import": True,
    }
    monkeypatch.setattr(workflow, "get_scope", lambda *_: scope)
    monkeypatch.setattr(workflow, "list_responsibles", lambda *_: [])
    app = FastAPI()
    app.include_router(
        routes.create_router(
            current_authorization=lambda _: actor,
            render_template=lambda *_: HTMLResponse("page"),
            csrf_token=lambda _: "csrf",
        )
    )
    return TestClient(app), actor, scope


def test_csrf_and_foreign_origin_reject_before_mutation(client, monkeypatch):
    api, _, _ = client
    monkeypatch.setattr(workflow, "confirm_candidate", lambda *_a, **_kw: pytest.fail("must not write"))
    url = f"/dashboard/creators/{uuid4()}/confirm"
    payload = {"version": 1, "request_id": str(uuid4())}
    assert api.post(url, json=payload).status_code == 403
    assert (
        api.post(url, json=payload, headers={"X-CSRF-Token": "csrf", "Origin": "https://evil.invalid"}).status_code
        == 403
    )


def test_import_rejects_csrf_and_foreign_origin_before_preflight_or_commit(client, monkeypatch):
    api, _, _ = client
    monkeypatch.setattr(imports, "preview", lambda *_a, **_kw: pytest.fail("must not create batch"))
    monkeypatch.setattr(imports, "commit", lambda *_a, **_kw: pytest.fail("must not import"))
    for url in ("/dashboard/creators/import/preview", f"/dashboard/creators/import/{uuid4()}/commit"):
        assert api.post(url, content=b"{}").status_code == 403
        assert api.post(
            url, content=b"{}", headers={"X-CSRF-Token": "csrf", "Origin": "https://evil.invalid"}
        ).status_code == 403


def test_confirm_rechecks_version_and_requires_idempotency_key(client, monkeypatch):
    api, _, _ = client
    called = []
    monkeypatch.setattr(
        workflow, "confirm_candidate", lambda *a, **kw: called.append((a, kw)) or {"status": "confirmed"}
    )
    url = f"/dashboard/creators/{uuid4()}/confirm"
    assert api.post(url, headers={"X-CSRF-Token": "csrf"}, json={"version": 1}).status_code == 409
    result = api.post(url, headers={"X-CSRF-Token": "csrf"}, json={"version": 1, "request_id": str(uuid4())})
    assert result.status_code == 200 and len(called) == 1
    assert result.headers["cache-control"].startswith("no-store")


def test_search_never_calls_global_directory(client, monkeypatch):
    api, _, scope = client
    calls = []
    monkeypatch.setattr(workflow, "search_users", lambda s, q: calls.append((s, q)) or [])
    assert api.get("/dashboard/creators/users?q=name").json() == {"items": []}
    assert calls == [(scope, "name")]


@pytest.fixture
def real_import(db, people, monkeypatch):
    migrations = Path(__file__).parents[1] / "migrations"
    db.execute((migrations / "030_creator_workflow_imports.sql").read_text())
    db.execute((migrations / "033_identity_names.sql").read_text())
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(db))
    monkeypatch.setattr(evidence, "connection", lambda: nullcontext(db))
    return workflow.get_scope(people["cm"])


def test_bilingual_cm_names_remain_excel_aliases_and_collisions_are_invalid(db, people, real_import):
    db.execute(
        "UPDATE authorizations SET display_name='Remi（Quanzhen Liang）' WHERE id=%s", (people["cm"],)
    )
    db.execute(
        """INSERT INTO lark_identities(open_id,name,en_name,display_name)
        VALUES('ou_cm','Quanzhen Liang','Remi','Remi（Quanzhen Liang）')"""
    )
    aliases = imports._aliases(db)
    for name in ("remi", "quanzhen liang", "remi（quanzhen liang）", str(people["cm"])):
        assert aliases[name] == {str(people["cm"])}
    rows = imports.resolve_rows(db, real_import, [
        {"handle": "creator_" + str(index), "cm": name}
        for index, name in enumerate(("Remi", "Quanzhen Liang", "Remi（Quanzhen Liang）"))
    ])
    assert all(row["responsible_id"] == str(people["cm"]) for row in rows)

    db.execute(
        "INSERT INTO lark_identities(open_id,name,en_name) VALUES('ou_cm2','Another Person','Remi')"
    )
    assert imports._aliases(db)["remi"] == {str(people["cm"]), str(people["cm2"])}
    owner_scope = workflow.get_scope(people["owner"], people["cm"])
    row = imports.resolve_rows(db, owner_scope, [{"handle": "uncertain", "cm": "Remi"}])[0]
    assert row["responsible_id"] == str(people["cm"])
    assert row["status"] == "invalid"
    assert "无法唯一确定" in row["reasons"][0]


def test_real_import_excel_only_complete_and_partial_both_stay_pending(db, people, real_import):
    raw = workbook(
        [
            ["handle", "CM", "lark open id", "relationship"],
            ["complete", "cm", "ou_person", "owner"],
            [None, "cm", None, None],
        ]
    )
    check = imports.preview(real_import, raw, "scope.xlsx")
    assert check["summary"] == {"total": 2, "ready": 0, "pending": 2, "invalid": 0}
    assert check["rows"][0]["contact_id"] == str(people["person"])
    assert all(row["trusted_identity"] is False and row["relationship"] is None for row in check["rows"])
    assert all("missing_relationship" not in row["reasons"] for row in check["rows"])
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == 0
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert (result["formal"], result["pending"]) == (0, 2)
    assert imports.commit(real_import, check["batch_id"], uuid4()) == result
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 0
    for row in db.execute("SELECT source_data FROM creator_workflow_sources").fetchall():
        assert row["source_data"]["creator_type"] == "cap"
        assert row["source_data"]["type_evidence"] == "cm_excel_upload"
        assert row["source_data"]["cap_policy_version"] == "cap_v1"


def test_real_import_other_cm_forbidden_and_missing_cm_uses_upload_scope(db, people, real_import):
    check = imports.preview(real_import, workbook([["handle", "CM"], ["other", "cm2"], ["unknown", None]]), "a.xlsx")
    assert check["summary"]["invalid"] == 1
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert result["skipped"] == 1 and result["pending"] == 1 and result["formal"] == 0
    row = db.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["responsible_authorization_id"] == people["cm"]


def test_handle_only_upload_normalizes_profiles_and_deduplicates_without_formal_entries(db, people, real_import):
    check = imports.preview(
        real_import,
        workbook([["handle"], [" @Creator.Name "], ["creator.name"], ["https://www.tiktok.com/@Creator.Name?lang=en"]]),
        "cap.xlsx",
    )
    assert {row["handle"] for row in check["rows"]} == {"creator.name"}
    assert all(row["responsible_id"] == str(people["cm"]) for row in check["rows"])
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert result["imported"] == result["pending"] == 3 and result["formal"] == 0
    assert len({item["id"] for item in result["items"]}) == 1
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_sources").fetchone()["n"] == 3
    repeated = imports.preview(real_import, workbook([["handle"], ["creator.name"]]), "repeat.xlsx")
    imports.commit(real_import, repeated["batch_id"], uuid4())
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == 1


def test_malformed_profile_url_keeps_other_rows_and_stays_pending(db, people, real_import):
    check = imports.preview(
        real_import, workbook([["handle"], ["valid_handle"], ["https://[broken/@abc"]]), "cap.xlsx"
    )
    assert check["summary"] == {"total": 2, "ready": 0, "pending": 2, "invalid": 0}
    assert check["rows"][0]["handle"] == "valid_handle"
    assert check["rows"][1]["handle"] is None
    assert "handle_uncertain" in check["rows"][1]["reasons"]
    assert imports.commit(real_import, check["batch_id"], uuid4())["pending"] == 2


@pytest.mark.parametrize("cm,tap_cm", [("cm2", None), ("cm", "cm2"), ("cm/cm2", None), ("unknown", None)])
def test_owner_upload_scope_cannot_be_redirected_by_extra_cm_columns(db, people, real_import, cm, tap_cm):
    scope = workflow.get_scope(people["owner"], people["cm"])
    check = imports.preview(scope, workbook([["handle", "CM", "TAP CM"], ["abc", cm, tap_cm]]), "cap.xlsx")
    assert check["rows"][0]["status"] == "invalid"
    assert check["rows"][0]["responsible_id"] == str(people["cm"])
    result = imports.commit(scope, check["batch_id"], uuid4())
    assert result["skipped"] == 1 and result["imported"] == 0
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == 0


def test_owner_upload_requires_explicit_active_cm_and_preserves_actor_audit(db, people, real_import):
    raw = workbook([["handle"], ["abc"]])
    with pytest.raises(workflow.WorkflowInputError, match="选择当前有效的负责人"):
        imports.preview(workflow.get_scope(people["owner"], None), raw, "cap.xlsx")
    scope = workflow.get_scope(people["owner"], people["cm"])
    check = imports.preview(scope, raw, "cap.xlsx")
    assert imports.commit(scope, check["batch_id"], uuid4())["pending"] == 1
    assert db.execute("SELECT responsible_authorization_id FROM creator_workflow_candidates").fetchone()[
        "responsible_authorization_id"
    ] == people["cm"]
    assert db.execute("SELECT submitted_by_authorization_id FROM creator_workflow_sources").fetchone()[
        "submitted_by_authorization_id"
    ] == people["owner"]
    check = imports.preview(scope, workbook([["handle"], ["next"]]), "cap2.xlsx")
    db.execute("UPDATE authorizations SET assignment_eligible=FALSE WHERE id=%s", (people["cm"],))
    with pytest.raises(workflow.WorkflowInputError, match="选择当前有效的负责人"):
        imports.commit(scope, check["batch_id"], uuid4())


def test_upload_does_not_store_unrelated_sensitive_columns_or_relationship_claims(db, people, real_import):
    raw = workbook([
        ["handle", "relationship", "bank account", "private notes", "unrelated formula"],
        ["abc", "owner", "sensitive-bank-value", "sensitive-note-value", "=1+1"],
    ])
    check = imports.preview(real_import, raw, "cap.xlsx")
    stored = db.execute("SELECT rows FROM creator_workflow_import_batches").fetchone()["rows"]
    assert set(stored[0]) == {"handle", "sheet", "row", "formula", "cap_policy_version"}
    assert stored[0]["formula"] is False
    imports.commit(real_import, check["batch_id"], uuid4())
    source = db.execute("SELECT source_data FROM creator_workflow_sources").fetchone()["source_data"]
    persisted = json.dumps([stored, source])
    assert "sensitive-bank-value" not in persisted and "sensitive-note-value" not in persisted
    assert "relationship" not in stored[0]


@pytest.mark.parametrize("completed", [False, True])
def test_legacy_batch_cannot_be_reinterpreted_as_cap_but_completed_result_is_idempotent(
    db, people, real_import, completed
):
    check = imports.preview(real_import, workbook([["handle"], ["abc"]]), "cap.xlsx")
    original = imports.commit(real_import, check["batch_id"], uuid4()) if completed else None
    stored = db.execute("SELECT rows FROM creator_workflow_import_batches").fetchone()["rows"]
    for row in stored:
        row.pop("cap_policy_version")
    db.execute(
        "UPDATE creator_workflow_import_batches SET rows=%s::jsonb WHERE id=%s",
        (json.dumps(stored), check["batch_id"]),
    )
    if completed:
        assert imports.commit(real_import, check["batch_id"], uuid4()) == original
    else:
        with pytest.raises(workflow.WorkflowInputError, match="导入规则已更新"):
            imports.commit(real_import, check["batch_id"], uuid4())
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == int(completed)


def test_real_import_revalidates_visibility_after_preflight(db, people, real_import):
    raw = workbook([["handle", "CM", "open id", "relationship"], ["abc", "cm", "ou_person", "owner"]])
    check = imports.preview(real_import, raw, "a.xlsx")
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert result["formal"] == 0 and result["pending"] == 1


def test_real_readonly_cannot_import_and_batch_cannot_be_stolen(db, people, real_import):
    check = imports.preview(real_import, workbook([["handle", "CM"], ["abc", "cm"]]), "a.xlsx")
    with pytest.raises(PermissionError):
        imports.preview(workflow.get_scope(people["reviewer"], people["cm"]), b"", "a.xlsx")
    with pytest.raises(LookupError):
        imports.commit(workflow.get_scope(people["cm2"]), check["batch_id"], uuid4())


def test_real_evidence_does_not_grant_another_cm_chat(db, people, real_import, monkeypatch):
    row = workflow.ingest_source(
        people["cm"],
        source_type="excel",
        source_key="evidence-row",
        source_data={},
        responsible_id=people["cm"],
        handle="abc",
        contact_id=people["person"],
    )
    result = evidence.evidence(real_import, row["id"])
    assert [c["chat_id"] for c in result["conversations"]] == ["chat_cm"]
    with pytest.raises(PermissionError):
        evidence.evidence(real_import, row["id"], "chat_cm2")
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    assert evidence.evidence(real_import, row["id"])["conversations"] == []
