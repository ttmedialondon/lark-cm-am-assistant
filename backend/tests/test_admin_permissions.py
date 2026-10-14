"""Real permission changes and reads share the database; routes have a separate ACL matrix."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_permissions, admin_reporting

OWNER = UUID("00000000-0000-0000-0000-000000000001")
PERSON = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def permissions(pg, monkeypatch):
    for person, role in ((OWNER, "owner"), (PERSON, "operator")):
        pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
                   (person, str(person)))
        pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,%s)", (person, role))
    for source in (OWNER, PERSON):
        pg.execute("INSERT INTO admin_message_source_limits VALUES(%s,%s)", (PERSON, source))
    for module in (admin, admin_permissions, admin_reporting):
        monkeypatch.setattr(module, "connection", lambda: nullcontext(pg))
    session = {"id": OWNER, "status": "active"}
    app = FastAPI()
    app.include_router(admin.create_admin_router(
        current_authorization=lambda _: session,
        csrf_token=lambda _: "csrf",
        render_template=lambda request, name, context, status_code=200:
            PlainTextResponse("rendered", status_code=status_code),
    ))
    return SimpleNamespace(browser=TestClient(app), session=session)


def test_owner_grant_read_replace_revoke_preserves_source_limits_and_audit(pg, permissions):
    api = permissions.browser
    missing = uuid4()
    initial = admin_permissions.users_access_data([OWNER, PERSON, PERSON, missing])
    assert set(initial) == {OWNER, PERSON, missing}
    assert initial[OWNER]["is_owner"] and initial[OWNER]["permissions"] == admin_permissions.PAGE_KEYS
    assert not initial[PERSON]["has_admin_access"] and not initial[missing]["has_admin_access"]
    assert admin_permissions.users_access_data([]) == {}

    path = f"/admin/users/{PERSON}/permissions"
    for grants in (["messages"], ["messages", "users"], []):
        permissions.session["id"] = OWNER
        response = api.post(path, data={"csrf_token": "csrf", "permissions": grants}, follow_redirects=False)
        assert response.status_code == 303
        permissions.session["id"] = PERSON
        access = admin_permissions.access_for(PERSON)
        assert access["permissions"] == set(grants) and access["message_source_ids"] == [OWNER, PERSON]
        assert api.get("/admin/messages").status_code == (200 if "messages" in grants else 403)
        assert api.get("/admin/users").status_code == (200 if "users" in grants else 403)

    audits = pg.execute("SELECT authorization_id,metadata FROM audit_events "
                        "WHERE action='admin_page_permissions_updated'").fetchall()
    assert len(audits) == 3 and all(row["authorization_id"] == OWNER for row in audits)
    assert all(row["metadata"]["target_authorization_id"] == str(PERSON) for row in audits)
    assert {(tuple(row["metadata"]["before"]), tuple(row["metadata"]["after"])) for row in audits} == {
        ((), ("messages",)), (("messages",), ("messages", "users")), (("messages", "users"), ()),
    }
    admin_permissions.set_permissions(OWNER, PERSON, [])
    assert pg.execute("SELECT count(*) n FROM audit_events WHERE action='admin_page_permissions_updated'"
                      ).fetchone()["n"] == 3
    assert admin_permissions.access_for(OWNER)["message_source_ids"] is None


def test_permission_changes_reject_non_owner_self_and_invalid_targets(pg, permissions):
    with pytest.raises(PermissionError):
        admin_permissions.set_permissions(PERSON, PERSON, ["users"])
    with pytest.raises(ValueError):
        admin_permissions.set_permissions(OWNER, OWNER, [])
    with pytest.raises(ValueError):
        admin_permissions.set_permissions(OWNER, PERSON, ["unknown-page"])
    with pytest.raises(ValueError):
        admin_permissions.set_permissions(OWNER, PERSON, ["jobs", "jobs"])
    with pytest.raises(LookupError):
        admin_permissions.set_permissions(OWNER, uuid4(), ["users"])
    assert pg.execute("SELECT count(*) n FROM admin_page_permissions").fetchone()["n"] == 0
    assert pg.execute("SELECT count(*) n FROM audit_events WHERE action='admin_page_permissions_updated'"
                      ).fetchone()["n"] == 0


def test_saved_permissions_do_not_bypass_revoked_accounts_or_owner_roles(pg, permissions):
    admin_permissions.set_permissions(OWNER, PERSON, ["messages"])
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (PERSON,))
    access = admin_permissions.access_for(PERSON)
    assert not access["has_admin_access"] and access["configured_permissions"] == {"messages"}

    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'users')", (OWNER,))
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (OWNER,))
    access = admin_permissions.access_for(OWNER)
    assert access["is_owner_account"] and not access["is_owner"] and not access["permissions"]
    with pytest.raises(PermissionError):
        admin_permissions.set_permissions(OWNER, PERSON, [])
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (OWNER,))
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (OWNER,))
    assert not admin_permissions.access_for(OWNER)["has_admin_access"]
    with pytest.raises(PermissionError):
        admin_permissions.set_permissions(OWNER, PERSON, [])
    assert pg.execute("SELECT permission FROM admin_page_permissions WHERE authorization_id=%s",
                      (PERSON,)).fetchone()["permission"] == "messages"
