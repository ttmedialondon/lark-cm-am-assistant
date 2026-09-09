"""Real SQL import/evidence checks plus the session/CSRF boundary."""

import io
import zipfile
from contextlib import nullcontext
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
    db.execute(
        (__import__("pathlib").Path(__file__).parents[1] / "migrations/030_creator_workflow_imports.sql").read_text()
    )
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(db))
    monkeypatch.setattr(evidence, "connection", lambda: nullcontext(db))
    return workflow.get_scope(people["cm"])


def test_real_import_preflight_complete_direct_and_partial_pending(db, people, real_import):
    raw = workbook(
        [
            ["handle", "CM", "lark open id", "relationship"],
            ["complete", "cm", "ou_person", "owner"],
            [None, "cm", None, None],
        ]
    )
    check = imports.preview(real_import, raw, "scope.xlsx")
    assert check["summary"] == {"total": 2, "ready": 1, "pending": 1, "invalid": 0}
    assert db.execute("SELECT COUNT(*) n FROM creator_workflow_candidates").fetchone()["n"] == 0
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert (result["formal"], result["pending"]) == (1, 1)
    assert imports.commit(real_import, check["batch_id"], uuid4()) == result
    assert db.execute("SELECT COUNT(*) n FROM creator_account_assignments").fetchone()["n"] == 1


def test_real_import_other_cm_forbidden_and_missing_cm_not_assumed(db, people, real_import):
    check = imports.preview(real_import, workbook([["handle", "CM"], ["other", "cm2"], ["unknown", None]]), "a.xlsx")
    assert check["summary"]["invalid"] == 1
    result = imports.commit(real_import, check["batch_id"], uuid4())
    assert result["skipped"] == 1 and result["pending"] == 1 and result["formal"] == 0
    row = db.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["responsible_authorization_id"] is None


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
