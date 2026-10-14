"""Frontend CM review stays separate from owner-only scan management."""

import asyncio
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import (
    admin_permissions,
    broadcast_images,
    contact_handles,
    creator_broadcast,
    creator_broadcast_delivery,
    creator_handles_routes,
    creator_importance,
    creator_imports,
    creator_roster,
)
from app.crypto import TokenCipher
from app.lark_api import LarkApiError, LarkProfile, TokenBundle


def browser(monkeypatch, *, owner=False, active=True):
    actor = {"id": uuid4(), "status": "active" if active else "revoked", "display_name": "CM"}
    monkeypatch.setattr(admin_permissions, "access_for", lambda _id: {"is_owner": owner})
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                            autoescape=select_autoescape())
    app = FastAPI()
    def authorization(_request):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        actor["session_reads"] = actor.get("session_reads", 0) + 1
        return actor

    app.include_router(creator_handles_routes.create_router(
        current_authorization=authorization,
        csrf_token=lambda _auth: "csrf",
        render_template=lambda _request, name, context, status_code=200: HTMLResponse(
            templates.get_template(name).render(**context), status_code=status_code),
    ))
    return TestClient(app), actor


def test_image_endpoints_require_session_csrf_bounds_and_private_reads(monkeypatch):
    client, actor = browser(monkeypatch)
    asset, other = uuid4(), uuid4()
    calls = []
    def upload(who, content, filename):
        calls.append((who, content, filename))
        return {"id": str(asset), "url": f"/creators/broadcast/images/{asset}"}
    monkeypatch.setattr(broadcast_images, "upload", upload)
    monkeypatch.setattr(broadcast_images, "MAX_BYTES", 10)
    path = "/creators/broadcast/images"
    headers = {"Content-Type": "image/png", "X-CSRF-Token": "csrf", "X-Filename": "a%20b.png"}
    assert client.post(path, content=b"png", headers={"Content-Type": "image/png"}).status_code == 400
    assert client.post(path, content=b"svg", headers={**headers, "Content-Type": "image/svg+xml"}).status_code == 400
    assert client.post(path, content=b"x" * 11, headers=headers).status_code == 400
    assert not calls
    assert client.post(path, content=b"png", headers=headers).json()["image"]["id"] == str(asset)
    assert calls == [(actor["id"], b"png", "a b.png")]
    def missing(*args):
        raise creator_broadcast.BroadcastAuthorizationRequired(actor["id"], media=True)
    monkeypatch.setattr(broadcast_images, "upload", missing)
    blocked = client.post(path, content=b"png", headers=headers)
    assert blocked.status_code == 403 and "media=1" in blocked.json()["authorization_url"]
    def failed(*args):
        raise LarkApiError(503, None, "provider-private-error")
    monkeypatch.setattr(broadcast_images, "upload", failed)
    failure = client.post(path, content=b"png", headers=headers)
    assert failure.status_code == 502 and "provider-private-error" not in failure.text
    def read(who, image_id):
        assert who == actor["id"]
        if image_id != asset:
            raise PermissionError("图片不属于本人。")
        return b"private-original", "image/png"
    monkeypatch.setattr(broadcast_images, "read", read)
    result = client.get(f"{path}/{asset}")
    assert result.content == b"private-original" and "no-store" in result.headers["cache-control"]
    assert result.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"{path}/{other}").status_code == 403
    actor["status"] = "revoked"
    assert client.get(f"{path}/{asset}").status_code == 403
    assert client.post(path, content=b"png", headers=headers).status_code == 403


def test_manual_handle_workflow_preserves_siblings_history_and_all_cm_responsibilities(pg, monkeypatch):
    client, actor = browser(monkeypatch, owner=True)
    owner = actor["id"]
    cm, second_cm, manager = uuid4(), uuid4(), uuid4()
    for authorization in (owner, cm, second_cm, manager):
        pg.execute("""INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at)
            VALUES(%s,%s,'unused',NOW())""", (authorization, "ou_" + authorization.hex))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (owner,))
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (manager,))
    person, empty_person, legacy_person, foreign_person, unassigned_person = (uuid4() for _ in range(5))
    for contact in (person, empty_person, legacy_person, foreign_person, unassigned_person):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Creator')", (contact,))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                   "VALUES(%s,'creator','confirmed','manual')", (contact,))
    for contact, responsible in ((person, cm), (person, second_cm), (empty_person, cm),
                                 (legacy_person, cm), (legacy_person, second_cm)):
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), contact, responsible))
    accounts = {handle: uuid4() for handle in ("old_handle", "sibling", "occupied", "represented", "legacy_old")}
    for handle, account in accounts.items():
        pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
                   "VALUES(%s,'tiktok',%s,%s)", (account, handle, handle))
    for contact, handle, relationship in ((person, "old_handle", "owner"), (person, "sibling", "owner"),
                                          (foreign_person, "occupied", "owner"),
                                          (person, "represented", "representative"),
                                          (legacy_person, "legacy_old", "owner")):
        pg.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
            VALUES(%s,%s,%s,'confirmed','excel_ai_verified')""", (contact, accounts[handle], relationship))
    batch, import_row = uuid4(), uuid4()
    pg.execute("""INSERT INTO creator_import_batches(id,cm_authorization_id,uploaded_by,filename,
        file_fingerprint,policy_version,status) VALUES(%s,%s,%s,'history.xlsx','history',%s,'completed')""",
        (batch, cm, owner, creator_imports.POLICY_VERSION))
    pg.execute("""INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,row_number,original_handle,
        handle,contact_id,status,review_status,pack_ciphertext,result_ciphertext)
        VALUES(%s,%s,%s,1,'old_handle','old_handle',%s,'succeeded','confirmed','history','history')""",
        (import_row, batch, cm, person))
    pg.execute("UPDATE contact_social_accounts SET import_row_id=%s WHERE contact_id=%s AND social_account_id=%s",
               (import_row, person, accounts["old_handle"]))
    monkeypatch.setattr(creator_handles_routes, "connection", lambda: nullcontext(pg))
    url = f"/creators/people/{person}/handle"
    fields = {"csrf_token": "csrf", "cm_id": str(cm), "old_handle": "@OLD_HANDLE", "handle": "new_handle"}

    def facts():
        return {table: pg.execute(f"SELECT to_jsonb(t) data FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
                for table in ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments",
                              "social_accounts", "contact_social_accounts", "creator_import_rows", "audit_events")}

    original = facts()
    assert client.post(url, data={**fields, "csrf_token": "bad"}).status_code == 400
    for current_actor in (cm, manager):
        actor["id"] = current_actor
        assert client.post(url, data={**fields, "cm_id": str(second_cm)}).status_code == 403
    actor.update(id=owner, status="revoked")
    assert client.post(url, data=fields).status_code == 403
    actor["status"] = "active"
    for changes, code in (({"cm_id": str(manager)}, 403), ({"handle": "invalid handle"}, 400),
                          ({"old_handle": "stale"}, 400), ({"old_handle": ""}, 400),
                          ({"handle": "occupied"}, 400), ({"handle": "represented"}, 400)):
        assert client.post(url, data={**fields, **changes}).status_code == code
        assert facts() == original
    saved = client.post(url, data=fields)
    assert saved.status_code == 200 and saved.json()["handles"] == ["new_handle", "sibling"]
    changed = facts()
    for table in ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments",
                  "creator_import_rows"):
        assert changed[table] == original[table]
    assert pg.execute("SELECT normalized_handle FROM social_accounts WHERE id=%s", (accounts["old_handle"],)
                      ).fetchone()["normalized_handle"] == "old_handle"
    rejected = pg.execute("SELECT * FROM contact_social_accounts WHERE contact_id=%s AND social_account_id=%s",
                          (person, accounts["old_handle"])).fetchone()
    assert rejected["status"] == "rejected" and rejected["import_row_id"] == import_row
    assert rejected["reviewed_by_authorization_id"] == owner and rejected["reviewed_at"]
    assert creator_imports._conflicts(pg, [(None, person, "old_handle")]) == [True]
    assert next(row for row in contact_handles.confirmed_pairs(pg, [person])
                if row["handle"] == "new_handle")["human_confirmed"]
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_manual'").fetchone()["metadata"]
    assert audit["before"] == ["old_handle", "sibling"]
    assert audit["after"] == ["new_handle", "sibling"]
    assert client.post(url, data=fields).status_code == 400  # A second stale save cannot remove the new account.
    assert facts() == changed

    # A sibling added while the editor was open survives the single-account correction.
    extra_account = uuid4()
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','concurrent_extra','concurrent_extra')", (extra_account,))
    pg.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
        VALUES(%s,%s,'owner','confirmed','creator_handle_auto_verified')""", (person, extra_account))
    restore = {**fields, "old_handle": "new_handle", "handle": "https://www.tiktok.com/@old_handle"}
    assert client.post(url, data=restore).status_code == 200
    assert contact_handles.for_contacts(pg, [person])[person] == [
        "concurrent_extra", "old_handle", "sibling"]
    assert creator_imports._conflicts(pg, [(None, person, "new_handle")]) == [True]
    assert client.post(url, data={**fields, "old_handle": "old_handle", "handle": "sibling"}).status_code == 200
    assert contact_handles.for_contacts(pg, [person])[person] == ["concurrent_extra", "sibling"]

    add_url = f"/creators/people/{empty_person}/handle"
    add_fields = {**fields, "old_handle": "", "handle": "first_account"}
    assert client.post(add_url, data=add_fields).status_code == 200
    assert client.post(add_url, data={**add_fields, "handle": "second_account"}).status_code == 400
    assert contact_handles.for_contacts(pg, [empty_person])[empty_person] == ["first_account"]

    legacy_url = f"/creators/people/{legacy_person}/handle"
    legacy_fields = {**fields, "old_handle": "legacy_old", "handle": "legacy_new"}
    assert client.post(legacy_url, data=legacy_fields).status_code == 200
    assert contact_handles.for_contacts(pg, [legacy_person])[legacy_person] == ["legacy_new"]
    for responsible in (cm, second_cm):
        assert pg.execute("SELECT creator_importance_is_eligible(%s,%s) ok", (legacy_person, responsible)
                          ).fetchone()["ok"]
    assert creator_imports._conflicts(pg, [(None, legacy_person, "legacy_old")]) == [True]
    assert pg.execute("SELECT reviewed_by_authorization_id FROM contact_social_accounts "
                      "WHERE contact_id=%s AND social_account_id=%s", (legacy_person, accounts["legacy_old"])
                      ).fetchone()["reviewed_by_authorization_id"] == owner

    # A CM can maintain their own person without receiving global creator management.
    actor["id"] = cm
    responsibilities = pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall()
    added = {**fields, "operation": "add", "old_handle": "", "handle": "cm_extra"}
    assert client.post(url, data=added).status_code == 200
    assert client.post(url, data=added).status_code == 200
    replaced = {**fields, "operation": "replace", "old_handle": "sibling", "handle": "cm_current"}
    assert client.post(url, data=replaced).status_code == 200
    assert client.post(url, data={**added, "operation": "remove", "old_handle": "cm_extra", "handle": ""}
                       ).status_code == 200
    assert pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall() == responsibilities
    assert "cm_current" in contact_handles.for_contacts(pg, [person])[person]
    assert "cm_extra" not in contact_handles.for_contacts(pg, [person])[person]
    assert creator_imports._conflicts(pg, [(None, person, "cm_extra")]) == [True]
    assert client.post(url, data={**added, "handle": "old_handle"}).status_code == 403
    assert client.post(f"/creators/people/{unassigned_person}/handle", data=added).status_code == 403

    # Direct association uses visible Lark people and never silently steals an occupied account.
    new_person = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Same Name')", (new_person,))
    for contact in (person, empty_person, legacy_person, foreign_person, new_person):
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,identity_type,"
                   "tenant_classification,avatar_url) "
                   "VALUES(%s,%s,'Same Name','user','known_external','https://img.feishucdn.com/fixture-avatar')",
                   ("ou_person_" + contact.hex, contact))
    pg.execute("""INSERT INTO lark_identities(open_id,contact_id,display_name,identity_type,tenant_classification,
        avatar_url,last_seen_at) VALUES('ou_private_alias',%s,'Same Name','user','known_external',
        'https://img.feishucdn.com/private-avatar',NOW()+INTERVAL '1 hour')""", (person,))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('manual-visible','Visible Creator Group','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'manual-visible')", (cm,))
    for contact in (person, empty_person, legacy_person, new_person):
        pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('manual-visible',%s,'user')",
                   ("ou_person_" + contact.hex,))
    found = client.get(f"/creators/people/search?cm_id={cm}&q=Same").json()
    assert {row["contact_id"] for row in found["items"]} == {
        str(person), str(empty_person), str(legacy_person), str(new_person)}
    assert all(row["avatar_url"] and row["context_label"] == "Visible Creator Group" for row in found["items"])
    assert next(row for row in found["items"] if row["contact_id"] == str(person)
                )["open_id"] == "ou_person_" + person.hex
    assert client.get(f"/creators/people/search?cm_id={second_cm}&q=Same").status_code == 403
    link = {"csrf_token": "csrf", "cm_id": str(cm), "handle": "newly_linked", "contact_id": str(new_person)}
    assert client.post("/creators/people/link", data={**link, "csrf_token": "bad"}).status_code == 400
    assert client.post("/creators/people/link", data={**link, "contact_id": str(foreign_person)}).status_code == 403
    assert client.post("/creators/people/link", data={**link, "cm_id": str(second_cm)}).status_code == 403
    assert client.post("/creators/people/link", data=link).status_code == 200
    assert client.post("/creators/people/link", data=link).status_code == 200
    assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s "
                      "AND responsible_authorization_id=%s "
                      "AND ended_at IS NULL", (new_person, cm)).fetchone()["n"] == 1
    assert pg.execute("SELECT status,reviewed_by_authorization_id FROM contact_roles WHERE contact_id=%s "
                      "AND role='creator'", (new_person,)).fetchone() == {
        "status": "confirmed", "reviewed_by_authorization_id": cm}
    assert next(row for row in contact_handles.confirmed_pairs(pg, [new_person])
                if row["handle"] == "newly_linked")["human_confirmed"]
    correction = {**link, "handle": "cm_current", "contact_id": str(empty_person), "old_contact_id": str(person)}
    assert client.post("/creators/people/link", data=correction).status_code == 403
    assert client.post("/creators/people/link", data={**correction, "old_contact_id": ""}).status_code == 400
    actor["id"] = owner
    before_correction = pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall()
    assert client.post("/creators/people/link", data=correction).status_code == 200
    assert pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall() == before_correction
    assert "cm_current" not in contact_handles.for_contacts(pg, [person])[person]
    assert "cm_current" in contact_handles.for_contacts(pg, [empty_person])[empty_person]
    assert creator_imports._conflicts(pg, [(None, person, "cm_current")]) == [True]
    corrected = facts()
    assert client.post("/creators/people/link", data=correction).status_code == 400
    assert facts() == corrected
    actor["id"] = cm
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s "
               "AND chat_id='manual-visible'", (cm,))
    assert not client.get(f"/creators/people/search?cm_id={cm}&q=Same").json()["items"]
    assert client.post("/creators/people/link", data={**link, "handle": "hidden_link"}).status_code == 403


def test_importance_roster_filters_and_owner_adjustment_are_scoped(pg, monkeypatch):
    client, actor = browser(monkeypatch, owner=True)
    settings = SimpleNamespace(token_encryption_key=b"i" * 32)
    cm, assigned, unassigned = uuid4(), uuid4(), uuid4()
    for account, name in ((actor["id"], "Owner"), (cm, "CM")):
        pg.execute("""INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
            refresh_token_ciphertext,access_token_expires_at,refresh_token_expires_at,body_consent_at)
            VALUES(%s,%s,%s,'unused','unused',NOW()+INTERVAL '1 hour',NOW()+INTERVAL '1 day',NOW())""",
                   (account, "ou_" + account.hex, name))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (actor["id"],))
    for person, name in ((assigned, "Assigned"), (unassigned, "Unassigned")):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (person, name))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
                   (person,))
    pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        VALUES(%s,%s,%s,'manual')""", (uuid4(), assigned, cm))
    pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,ended_at)
        VALUES(%s,%s,%s,'manual',NOW())""", (uuid4(), unassigned, cm))
    candidate = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Unconfirmed candidate')", (candidate,))
    pg.execute("INSERT INTO creator_workflow_candidates(id,contact_id) VALUES(%s,%s)", (uuid4(), candidate))
    for module in (creator_handles_routes, creator_imports, creator_roster):
        monkeypatch.setattr(module, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_roster, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_importance, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_permissions, "permission_keys_for", lambda _: ())

    monkeypatch.setattr(creator_imports, "_page_data", lambda *args, **kwargs: pytest.fail(
        "Confirmed rosters queried retired import candidates"))
    for suffix in ("", "?tab=candidates", "?tab=invalid"):
        default = client.get("/creators/data" + suffix)
        assert default.status_code == 200 and default.json()["tab"] == "mine"
        assert default.json()["items"] == []
        assert not default.json()["read_only"] and default.json()["can_edit_handles"]
    pending = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&tier=pending")
    assert pending.status_code == 200 and pending.json()["total_count"] == 1
    assert pending.json()["items"][0]["importance"]["tier"] == "pending"
    assert set(pending.json()["items"][0]["importance"]) == {"tier", "tier_label", "manual_tier", "manual_reason"}
    assert "evidence_rows" not in pending.json()["items"][0]
    assert pending.json()["can_edit_handles"]
    assert not pending.json()["importance_running"]
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE,api_key_ciphertext='fixture-only',key_suffix='only' "
               "WHERE feature='creator_importance'")
    pg.execute("SELECT enqueue_creator_importance(%s,%s)", (assigned, cm))
    assert client.get(f"/creators/data?tab=mine&cm_id={cm}").json()["importance_running"]
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature='creator_importance'")
    url = f"/creators/people/{assigned}/importance"
    fields = {"csrf_token": "csrf", "cm_id": str(cm), "tier": "focus", "reason": "Owner reviewed cooperation"}
    assert client.post(url, data={**fields, "csrf_token": "bad"}).status_code == 400
    saved = client.post(url, data=fields)
    assert saved.status_code == 200 and saved.json()["importance"]["tier"] == "focus"
    focus = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&tier=focus")
    assert focus.status_code == 200 and focus.json()["total_count"] == 1
    assert focus.json()["items"][0]["importance"]["manual_tier"] == "focus"
    assert client.get(f"/creators/data?tab=mine&cm_id={cm}&tier=normal").json()["total_count"] == 0
    for added_tier in ("priority", "dormant"):
        assert client.post(url, data={**fields, "tier": added_tier}).status_code == 200
        selected = client.get(f"/creators/data?tab=mine&cm_id={cm}&tier={added_tier}").json()
        assert selected["total_count"] == 1 and selected["items"][0]["importance"]["tier"] == added_tier
    assert client.post(url, data=fields).status_code == 200
    safe = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=bad%27SQL&tier=bad%27SQL").json()
    assert safe["sort"] == "name" and safe["tier"] == "" and safe["total_count"] == 1
    assert safe["items"][0]["importance"]["manual_reason"] == fields["reason"]
    monkeypatch.setattr(creator_importance, "view_for", lambda *args, **kwargs: pytest.fail(
        "Roster queried/decrypted a profile separately for each person"))
    queries = []

    class CountedConnection:
        def execute(self, sql, params=None):
            queries.append(sql)
            return pg.execute(sql, params)

        def transaction(self):
            return pg.transaction()

    monkeypatch.setattr(creator_roster, "connection", lambda: nullcontext(CountedConnection()))
    one = creator_roster.page_data(actor["id"], cm)
    assert len(one["items"]) == 1
    one_count = len(queries)
    assert sum("LEFT JOIN LATERAL" in sql for sql in queries) == 1
    pg.execute("SAVEPOINT roster_validity")
    pg.execute("""UPDATE creator_importance_profiles SET manual_tier=NULL,auto_valid=TRUE,
        facts_ciphertext='not-a-ciphertext',prompt_version=%s,sources='[]',
        coverage=jsonb_build_object('has_readable',TRUE,'priority',jsonb_build_object('primary_at',NOW()))
        WHERE contact_id=%s AND responsible_authorization_id=%s""",
               (creator_importance.facts.PROMPT_VERSION, assigned, cm))
    automatic = creator_roster.page_data(actor["id"], cm, sort="name")["items"][0]["importance"]
    assert automatic["tier"] == "focus" and automatic["manual_tier"] == ""
    pg.execute("UPDATE creator_importance_profiles SET sources=%s::jsonb "
               "WHERE contact_id=%s AND responsible_authorization_id=%s",
               (json.dumps([{"chat_id": "missing_chat", "message_id": "missing_message"}]), assigned, cm))
    assert creator_roster.page_data(actor["id"], cm)["items"][0]["importance"]["tier"] == "pending"
    pg.execute("UPDATE creator_importance_profiles SET manual_tier='focus' "
               "WHERE contact_id=%s AND responsible_authorization_id=%s", (assigned, cm))
    reviewed = creator_roster.page_data(actor["id"], cm)["items"][0]["importance"]
    assert reviewed["tier"] == "focus" and reviewed["manual_reason"] == fields["reason"]
    pg.execute("ROLLBACK TO SAVEPOINT roster_validity")
    pg.execute("SAVEPOINT roster_pagination")
    expected_ids = [str(assigned)]
    for manual_tier in creator_importance.TIERS:
        people = [uuid4() for _ in range(6)]
        expected_ids.extend(sorted(str(person) for person in people))
        for person in people:
            pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Pagination creator')", (person,))
            pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                       "VALUES(%s,'creator','confirmed','manual')", (person,))
            pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                       "VALUES(%s,%s,%s,'manual')", (uuid4(), person, cm))
            pg.execute("INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,manual_tier) "
                       "VALUES(%s,%s,%s)", (person, cm, manual_tier))
    queries.clear()
    first_page = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&page=1").json()
    assert len(queries) == one_count
    assert sum("LEFT JOIN LATERAL" in sql for sql in queries) == 1
    second_page = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&page=2").json()
    assert first_page["total_count"] == second_page["total_count"] == 31
    assert first_page["total_pages"] == second_page["total_pages"] == 2
    assert len(first_page["items"]) == 25 and len(second_page["items"]) == 6
    items = first_page["items"] + second_page["items"]
    assert [item["id"] for item in items] == expected_ids
    assert len({item["id"] for item in items}) == 31
    assert [item["importance"]["tier"] for item in items] == ["focus"] + [
        t for t in creator_importance.TIERS for _ in range(6)]
    repeated = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&page=2").json()
    assert [item["id"] for item in repeated["items"]] == expected_ids[25:]
    queries.clear()
    assert len(creator_roster.page_data(actor["id"], cm, sort="name")["items"]) == 25
    assert len(queries) == one_count
    pg.execute("ROLLBACK TO SAVEPOINT roster_pagination")
    actor["id"] = cm
    assert client.post(url, data=fields).status_code == 403
    with monkeypatch.context() as scoped:
        scoped.setattr(creator_importance, "_cipher", lambda *_args: pytest.fail(
            "A non-Owner roster decrypted the manual reason"))
        personal = client.get("/creators/data").json()
    assert personal["tab"] == "mine" and personal["total_count"] == 1
    assert not personal["can_adjust_importance"] and personal["can_edit_handles"] and personal["can_link_people"]
    assert personal["items"][0]["importance"]["manual_reason"] == ""
    assert client.get("/creators/data?tab=candidates").json()["tab"] == "mine"
    assert client.get("/creators/data?tab=unassigned").status_code == 403
    pg.execute("SAVEPOINT roster_revoked")
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
    assert client.get("/creators/data").status_code == 403
    pg.execute("ROLLBACK TO SAVEPOINT roster_revoked")
    owner = pg.execute("SELECT authorization_id FROM admin_roles WHERE role='owner'").fetchone()["authorization_id"]
    actor["id"] = owner
    monkeypatch.setattr(creator_importance, "view_for", lambda *args, **kwargs: pytest.fail(
        "Roster-only or unassigned request read a CM profile"))
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (owner,))
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-roster')", (owner,))
    readonly = client.get(f"/creators/data?tab=mine&cm_id={cm}&sort=importance&tier=focus")
    assert readonly.status_code == 200
    assert readonly.json()["total_count"] == 1 and readonly.json()["items"][0]["importance"] is None
    assert readonly.json()["sort"] == "name" and readonly.json()["tier"] == ""
    assert not readonly.json()["can_view_importance"] and not readonly.json()["importance_running"]
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (owner,))
    separate = client.get("/creators/data?tab=unassigned&sort=importance&tier=focus")
    assert separate.status_code == 200
    assert separate.json()["items"][0]["id"] == str(unassigned)
    assert separate.json()["items"][0]["importance"] is None
    assert separate.json()["sort"] == "name" and separate.json()["tier"] == ""


def test_broadcast_permission_workflow_checks_real_account_without_sending(pg, monkeypatch):
    from app import worker

    client, actor = browser(monkeypatch, owner=True)
    other = uuid4()
    baseline = ("offline_access", "im:chat:read", "im:message:readonly")
    required = (*baseline, "im:message", "im:message.send_as_user")
    settings = SimpleNamespace(oauth_scopes=baseline, token_encryption_key=b"p" * 32)
    cipher = TokenCipher(settings.token_encryption_key)
    for account, open_id, token, scopes in ((actor["id"], "ou_owner", "fixture-owner", required),
                                           (other, "ou_other", "fixture-other", ())):
        pg.execute("""INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
            refresh_token_ciphertext,access_token_expires_at,refresh_token_expires_at,scopes,body_consent_at)
            VALUES(%s,%s,%s,%s,%s,NOW()+INTERVAL '1 hour',NOW()+INTERVAL '1 day',%s,NOW())""",
            (account, open_id, open_id, cipher.encrypt(token), cipher.encrypt("fixture-refresh"), list(scopes)))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (actor["id"],))
    state = {"connections": 0, "calls": [], "refreshes": 0, "error": None, "change": None}

    @contextmanager
    def connection():
        state["connections"] += 1
        try:
            yield pg
        finally:
            state["connections"] -= 1

    class FakeLark:
        def __init__(self, received, *, http_client):
            assert received is settings and http_client.timeout.read == 8

        def user_info(self, token):
            assert state["connections"] == 0
            state["calls"].append(token)
            if state["error"]:
                raise state["error"]
            if state["change"] == "token":
                pg.execute("UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s",
                           (cipher.encrypt("fixture-replacement"), actor["id"]))
            elif state["change"] == "scope":
                pg.execute("UPDATE authorizations SET scopes='{}' WHERE id=%s", (actor["id"],))
            elif state["change"] == "revoked":
                pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor["id"],))
            elif state["change"] == "owner":
                pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (actor["id"],))
            elif state["change"] == "broadcast":
                pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor["id"],))
            assert token != "fixture-other"  # The recipient filter never selects another person's sending token.
            open_id = "ou_owner"
            return LarkProfile("ou_wrong" if state["change"] == "identity" else open_id, None, None, None)

        def refresh_token(self, _token):
            state["refreshes"] += 1
            return TokenBundle("fixture-refreshed", "fixture-refresh-new", 3600, 86400, "")

    monkeypatch.setattr(creator_broadcast, "connection", connection)
    monkeypatch.setattr(creator_broadcast_delivery, "connection", connection)
    monkeypatch.setattr(worker, "connection", connection)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_broadcast_delivery, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_broadcast, "LarkClient", FakeLark)
    url, headers = "/creators/broadcast/permissions", {"X-CSRF-Token": "csrf"}

    def check(cm_id="all"):
        response = client.post(url, json={"cm_id": cm_id, "actor_id": str(other)}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store, max-age=0"
        return response.json()

    assert client.post(url, json={"cm_id": "all"}).status_code == 400
    assert not state["calls"]
    own = check()
    assert own["status"] == "ready" and own["is_self"] and own["is_sender"]
    assert not own["missing_scopes"] and own["authorization_url"] is None and own["checked_at"]
    assert check(str(actor["id"]))["is_sender"] and state["refreshes"] == 0
    filtered = check(str(other))
    assert filtered["status"] == "ready" and filtered["account_id"] == str(actor["id"])
    assert filtered["is_sender"] and filtered["is_self"] and state["calls"][-1] == "fixture-owner"
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(baseline), actor["id"]))
    missing = check(str(other))
    assert missing["status"] == "needs_authorization"
    assert set(missing["missing_scopes"]) == set(creator_broadcast.SEND_SCOPES)
    assert missing["is_sender"] and missing["is_self"] and missing["account_id"] == str(actor["id"])
    assert missing["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    assert "发送消息" in missing["message"] and "以本人身份发送" in missing["message"]
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(required), actor["id"]))
    assert client.post(url, json={"cm_id": ""}, headers=headers).status_code == 400
    assert client.post(url, json={"cm_id": "bad"}, headers=headers).status_code == 400
    for error in (LarkApiError(403, 99991679, "denied"), LarkApiError(429, None, "limited"),
                  LarkApiError(500, None, "unavailable"), httpx.ReadTimeout("fixture timeout")):
        state["error"] = error
        result = check(str(other))
        assert result["status"] == "unavailable" and not result["missing_scopes"]
        assert result["authorization_url"] is None
    state["error"] = LarkApiError(401, None, "invalid user token")
    assert check(str(other))["status"] == "needs_authorization"
    state["error"] = None
    for change in ("identity", "token", "scope"):
        pg.execute("UPDATE authorizations SET status='active',scopes=%s,access_token_ciphertext=%s WHERE id=%s",
                   (list(required), cipher.encrypt("fixture-owner"), actor["id"]))
        state["change"] = change
        result = check(str(other))
        assert result["status"] == "unavailable"
        assert result["authorization_url"] is None and not result["missing_scopes"]
    state["change"] = "revoked"
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(required), actor["id"]))
    assert client.post(url, json={"cm_id": str(other)}, headers=headers).status_code == 403
    state["change"] = None
    before = len(state["calls"])
    assert client.post(url, json={"cm_id": str(other)}, headers=headers).status_code == 403
    assert len(state["calls"]) == before
    pg.execute("UPDATE authorizations SET status='active',body_consent_at=NULL WHERE id=%s", (actor["id"],))
    assert check(str(other))["authorization_url"] is None and len(state["calls"]) == before
    pg.execute("UPDATE authorizations SET status='attention_required',body_consent_at=NOW(),scopes=%s WHERE id=%s",
               (list(required), actor["id"]))
    attention = check(str(other))
    assert attention["status"] == "needs_authorization" and not attention["missing_scopes"]
    assert pg.execute("SELECT status FROM authorizations WHERE id=%s",
                      (actor["id"],)).fetchone()["status"] == "attention_required"
    before_refresh = len(state["calls"])
    pg.execute("""UPDATE authorizations SET body_consent_at=NOW(),scopes=%s,access_token_ciphertext=%s,
        access_token_expires_at=NOW()-INTERVAL '1 minute',status='active' WHERE id=%s""",
        (list(required), cipher.encrypt("fixture-owner"), actor["id"]))
    refreshed = check(str(other))
    assert state["refreshes"] == 1 and len(state["calls"]) == before_refresh
    assert refreshed["status"] == "needs_authorization" and set(refreshed["missing_scopes"]) == set(required)
    assert pg.execute("SELECT scopes,status FROM authorizations WHERE id=%s", (actor["id"],)).fetchone() == {
        "scopes": [], "status": "attention_required"}
    assert settings.oauth_scopes == baseline
    assert pg.execute("SELECT COUNT(*) AS n FROM sync_jobs").fetchone()["n"] == 0
    actor.update(status="attention_required", body_consent_at=True)
    reconnect = check(str(other))
    assert reconnect["status"] == "needs_authorization" and reconnect["is_self"]
    assert reconnect["account_id"] == str(actor["id"]) and not reconnect["is_sender"]
    assert reconnect["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    assert client.post(url, json={"cm_id": "all"}).status_code == 400
    attention_page = client.get("/creators/broadcast", follow_redirects=False)
    assert attention_page.status_code == 403 and "location" not in attention_page.headers
    blocked = client.post("/creators/broadcast/templates/save", json={"name": "Example", "body": "Example"},
                          headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    actor["status"] = "active"
    pg.execute("""UPDATE authorizations SET status='active',scopes=%s,access_token_ciphertext=%s,
        access_token_expires_at=NOW()+INTERVAL '1 hour' WHERE id=%s""",
        (list(required), cipher.encrypt("fixture-owner"), actor["id"]))
    state["change"] = "owner"
    pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor["id"],))
    assert client.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403
    state["change"] = None
    before = len(state["calls"])
    assert client.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403
    assert len(state["calls"]) == before

    # A frontend grant allows only this account, not management or another CM's resources.
    pg.execute("UPDATE authorizations SET broadcast_enabled=TRUE WHERE id=%s", (actor["id"],))
    data = creator_broadcast.page_data(actor["id"])
    assert data["cms"] == [{"id": str(actor["id"]), "name": "ou_owner"}]
    assert data["selected_cm_id"] == str(actor["id"]) and not data["can_select_cm"]
    opened = client.get("/creators/broadcast")
    assert opened.status_code == 200 and "发送记录" in opened.text and "发送（未开通）" not in opened.text
    assert check()["is_self"] and check()["is_sender"]
    pg.execute("UPDATE authorizations SET status='active',scopes=%s WHERE id=%s", (list(required), other))
    for account, chat, name in ((actor["id"], "oc_own", "Own creator"), (other, "oc_foreign", "Foreign creator")):
        person, identity = uuid4(), "ou_" + chat
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (person, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   (identity, person, name))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
                   (person,))
        pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
            VALUES(%s,%s,%s,'manual')""", (uuid4(), person, account))
        pg.execute("INSERT INTO chats(chat_id,name) VALUES(%s,%s)", (chat, name + " group"))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (account, chat))
        pg.execute("""INSERT INTO chat_group_classifications(chat_id,purpose,confirmed_by_authorization_id)
            VALUES(%s,'category',%s)""", (chat, actor["id"]))
    own_fields = {"cm_id": "all", "purpose": "category", "mention": True,
                  "message": "Example", "selections": [{"chat_id": "oc_own"}]}
    matched = client.post("/creators/broadcast/match", json={"cm_id": "all", "purpose": "support",
                          "raw": "Own creator\nForeign creator"}, headers=headers).json()
    assert matched["selected_cm_id"] == str(actor["id"])
    assert matched["entries"][0]["person"]["name"] == "Own creator"
    assert matched["entries"][1]["person"] is None
    joined = client.post("/creators/broadcast/groups", json=own_fields, headers=headers).json()
    assert [group["id"] for group in joined["groups"]] == ["oc_own"]
    checked = client.post("/creators/broadcast/preview", json=own_fields, headers=headers).json()
    assert checked["groups"][0]["mention_all"] and not checked["can_send"]
    # Batched routes must keep membership, classification and visibility checks.
    own_person = pg.execute("SELECT contact_id FROM lark_identities WHERE open_id='ou_oc_own'").fetchone()["contact_id"]
    pg.execute("SAVEPOINT support_route")
    pg.execute("UPDATE chat_group_classifications SET purpose='support' WHERE chat_id='oc_own'")
    pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('oc_own','ou_oc_own','user')")
    support = {"id": "oc_own", "people": [{"id": str(own_person)}],
               "mentions": [{"id": "ou_oc_own", "contact_id": str(own_person)}]}
    creator_broadcast_delivery._routes_current(pg, actor["id"], "support", [support])
    with pytest.raises(PermissionError, match="身份关联"):
        creator_broadcast_delivery._routes_current(pg, actor["id"], "support", [
            {**support, "mentions": [{"id": "ou_oc_foreign", "contact_id": str(own_person)}]}])
    pg.execute("DELETE FROM chat_members WHERE chat_id='oc_own'")
    with pytest.raises(PermissionError):
        creator_broadcast_delivery._routes_current(pg, actor["id"], "support", [support])
    pg.execute("ROLLBACK TO SAVEPOINT support_route")
    for sql in ("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='oc_own'",
                "UPDATE chat_group_classifications SET purpose='support' WHERE chat_id='oc_own'"):
        pg.execute("SAVEPOINT changed_route")
        pg.execute(sql)
        with pytest.raises(PermissionError):
            creator_broadcast_delivery._routes_current(pg, actor["id"], "category", checked["groups"])
        pg.execute("ROLLBACK TO SAVEPOINT changed_route")
    assert client.post("/creators/broadcast/send", json=own_fields, headers=headers).status_code == 400
    list_fields = dict(own_fields, name="Own list")
    list_item = client.post("/creators/broadcast/lists/save", json=list_fields, headers=headers).json()["list"]
    assert list_item["cm_id"] == str(actor["id"])
    assert client.post("/creators/broadcast/lists/load", json={"id": list_item["id"]},
                       headers=headers).json()["selected_chat_ids"] == ["oc_own"]
    draft = {"name": "Own template", "body": "Example"}
    template = client.post("/creators/broadcast/templates/save", json=draft, headers=headers).json()["template"]
    foreign_list = pg.execute("""INSERT INTO creator_broadcast_lists(owner_authorization_id,name,cm_id,purpose,
        targets,snapshot) VALUES(%s,'Foreign list',%s,'category','[{"chat_id":"oc_foreign"}]','{"mode":"groups"}')
        RETURNING id""", (other, other)).fetchone()["id"]
    foreign_template = pg.execute("""INSERT INTO creator_broadcast_templates(owner_authorization_id,name,body)
        VALUES(%s,'Foreign template','Example') RETURNING id""", (other,)).fetchone()["id"]
    personal = creator_broadcast.page_data(actor["id"])
    assert [item["id"] for item in personal["lists"]] == [list_item["id"]]
    assert personal["templates"] == [template]
    for action in ("load", "delete", "save"):
        response = client.post("/creators/broadcast/lists/" + action,
                               json=dict(list_fields, id=str(foreign_list), revision=1), headers=headers)
        assert response.status_code == 404
    for action in ("delete", "save"):
        response = client.post("/creators/broadcast/templates/" + action,
                               json=dict(draft, id=str(foreign_template), revision=1), headers=headers)
        assert response.status_code == 404
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (actor["id"],))
    for action in ("permissions", "match", "groups", "resolve", "preview", "lists/save"):
        fields = dict(list_fields, cm_id=str(other), raw="Foreign creator", contact_ids=[str(uuid4())])
        assert client.post("/creators/broadcast/" + action, json=fields, headers=headers).status_code == 403
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(baseline), actor["id"]))
    missing = client.get("/creators/broadcast", follow_redirects=False)
    assert missing.status_code == 200 and "location" not in missing.headers
    assert client.post("/creators/broadcast/preview", json=own_fields, headers=headers).status_code == 200
    assert client.post("/creators/broadcast/templates/save", json=draft, headers=headers).status_code == 200
    send_fields = dict(own_fields, cm_id=str(actor["id"]))
    blocked_send = client.post("/creators/broadcast/prepare", json=send_fields, headers=headers)
    assert blocked_send.status_code == 403
    assert blocked_send.json()["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    assert pg.execute("SELECT COUNT(*) n FROM creator_broadcast_batches").fetchone()["n"] == 0
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(required), actor["id"]))
    state["change"] = "broadcast"
    assert client.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403
    state["change"] = None
    assert client.get("/creators/broadcast").status_code == 403
    assert client.post("/creators/broadcast/templates/save", json=draft, headers=headers).status_code == 403
    pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (other,))
    ordinary, ordinary_actor = browser(monkeypatch)
    ordinary_actor["id"] = other
    assert ordinary.get("/creators/broadcast").status_code == 403
    ordinary_actor.update(status="attention_required", body_consent_at=True)
    assert ordinary.get("/creators/broadcast", follow_redirects=False).status_code == 403
    assert ordinary.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403

    assert pg.execute("SELECT COUNT(*) n FROM creator_broadcast_batches").fetchone()["n"] == 0


def test_broadcast_real_data_workflow_rechecks_scope_routes_and_never_sends(monkeypatch):
    client, actor = browser(monkeypatch, owner=True)
    baseline = ("offline_access", "im:chat:read", "im:message:readonly", "im:chat.members:read",
                "contact:contact.base:readonly", "contact:user.base:readonly")
    required = (*baseline, *creator_broadcast.SEND_SCOPES)
    settings = SimpleNamespace(oauth_scopes=baseline)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
    p1, p2, other_cm = map(str, (uuid4(), uuid4(), uuid4()))
    people = [{"id": p1, "name": "Example One", "handles": ["example_one", "example_backup"]},
              {"id": p2, "name": "Example Two", "handles": ["example_two"]}]
    group = {"id": "oc_example", "name": "Example group", "purpose": "support", "members": [p1, p2]}
    changed = {"routes": False, "role": True, "cm_active": True, "scopes": list(required), "consent": True}
    reads = []
    saved_lists = {}
    saved_templates = {}

    class Rows:
        def __init__(self, values):
            self.values = values
        def fetchone(self):
            return self.values[0] if self.values else None
        def fetchall(self):
            return self.values

    class Conn:
        def transaction(self):
            return nullcontext()
        def execute(self, sql, params=()):
            reads.append(sql)
            if "creator_broadcast_templates" in sql:
                if sql.startswith("INSERT"):
                    name, body, document, owner = params
                    item = {"id": uuid4(), "name": name, "body": body,
                            "body_document": document.obj if document else None, "owner_authorization_id": owner,
                            "revision": 1, "deleted_at": None}
                    saved_templates[str(item["id"])] = item
                    return Rows([dict(item)])
                if sql.startswith("UPDATE"):
                    item = saved_templates.get(str(params[-3]))
                    if (not item or str(item["owner_authorization_id"]) != str(params[-2])
                            or item["revision"] != params[-1] or item["deleted_at"]):
                        return Rows([])
                    if "SET deleted_at" in sql:
                        item["deleted_at"] = True
                    else:
                        item.update(name=params[0], body=params[1],
                                    body_document=params[2].obj if params[2] else None)
                    item["revision"] += 1
                    return Rows([dict(item)])
                if "WHERE id=%s" in sql:
                    item = saved_templates.get(str(params[0]))
                    owned = item and str(item["owner_authorization_id"]) == str(params[1]) and not item["deleted_at"]
                    return Rows([dict(item)] if owned else [])
                return Rows([dict(item) for item in saved_templates.values()
                             if str(item["owner_authorization_id"]) == str(params[0]) and not item["deleted_at"]])
            if "creator_broadcast_lists" in sql:
                if sql.startswith("INSERT"):
                    name, notes, cm, purpose, mention, targets, snapshot, owner = params
                    item = {"id": uuid4(), "name": name, "notes": notes, "cm_id": cm,
                            "purpose": purpose, "mention": mention, "targets": targets.obj,
                            "snapshot": snapshot.obj, "owner_authorization_id": owner, "revision": 1,
                            "deleted_at": None}
                    saved_lists[str(item["id"])] = item
                    return Rows([dict(item)])
                if sql.startswith("UPDATE"):
                    item = saved_lists.get(str(params[-3]))
                    if (not item or str(item["owner_authorization_id"]) != str(params[-2])
                            or item["revision"] != params[-1] or item["deleted_at"]):
                        return Rows([])
                    if "SET deleted_at" in sql:
                        item["deleted_at"] = True
                    elif "cm_id=%s" in sql:
                        name, notes, cm, purpose, mention, targets, snapshot = params[:7]
                        item.update(name=name, notes=notes, cm_id=cm, purpose=purpose, mention=mention,
                                    targets=targets.obj, snapshot=snapshot.obj)
                    else:
                        item.update(name=params[0], notes=params[1])
                    item["revision"] += 1
                    return Rows([dict(item)])
                if "WHERE id=%s" in sql:
                    item = saved_lists.get(str(params[0]))
                    owned = item and str(item["owner_authorization_id"]) == str(params[1]) and not item["deleted_at"]
                    return Rows([dict(item)] if owned else [])
                return Rows([dict(item) for item in saved_lists.values()
                             if str(item["owner_authorization_id"]) == str(params[0]) and not item["deleted_at"]])
            if sql == "SET TRANSACTION READ ONLY":
                return Rows([])
            if "JOIN admin_roles" in sql:
                owner = changed["role"] and str(params[0]) == str(actor["id"])
                if "broadcast_enabled" in sql:
                    return Rows([{"status": "active", "broadcast_enabled": False,
                                  "role": "owner" if owner else None, "is_active": owner}])
                return Rows([{"owner": True}] if owner else [])
            if "SELECT id,status,body_consent_at,scopes" in sql:
                return Rows([{"id": params[0], "status": "active", "body_consent_at": changed["consent"],
                              "scopes": changed["scopes"] if str(params[0]) == str(actor["id"]) else list(required)}])
            if "SELECT id,display_name" in sql:
                return Rows([{"id": actor["id"], "display_name": "CM"}])
            if "SELECT 1 FROM authorizations" in sql:
                return Rows([{"active": True}])
            if "SELECT id,open_id,display_name" in sql:
                return Rows([{"id": params[0], "open_id": "ou_example", "display_name": "CM"}]
                            if changed["cm_active"] else [])
            if "FROM authorization_chats ac JOIN authorizations" in sql:
                return Rows([{"chat_id": group["id"], "name": group["name"]}] if not changed["routes"] else [])
            raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(creator_broadcast, "connection", lambda: nullcontext(Conn()))
    monkeypatch.setattr(creator_broadcast, "_people", lambda _conn, cm, ids=None: [
        person for person in people if (ids is None or person["id"] in ids) and (cm is None or person["id"] == p1)
    ])
    monkeypatch.setattr(contact_handles, "for_contacts", lambda _conn, ids: {
        key: next(person["handles"] for person in people if person["id"] == str(key)) for key in ids})
    monkeypatch.setattr(creator_broadcast, "_groups", lambda _conn, ids, _cm, purpose: [dict(group, purpose=purpose,
                        members=[contact for contact in group["members"] if contact in ids])]
                        if ids and not changed["routes"] else [])
    response = client.get("/creators/broadcast")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "发送记录" in response.text and "准备发送" in response.text
    assert "发送（未开通）" not in response.text and "creator-broadcast.mjs" in response.text
    assert creator_broadcast.page_data(actor["id"])["selected_cm_id"] == str(actor["id"])
    for label in creator_broadcast.PURPOSES.values():
        assert label in response.text
    assert client.post("/creators/broadcast").status_code == 405
    headers = {"X-CSRF-Token": "csrf"}
    request = {"cm_id": "all", "purpose": "support", "raw": "@example_one\n@example_backup\n@example_two"}
    url = "/creators/broadcast/match"
    assert client.post(url, json=request).status_code == 400
    result = client.post(url, json=request, headers=headers)
    assert result.status_code == 200 and len(result.json()["entries"]) == 3
    assert result.headers["cache-control"] == "no-store, max-age=0"
    assert result.json()["can_send"] is False
    preview = {"cm_id": "all", "purpose": "support", "message": "Example\nhttps://example.com", "mention": True,
               "selections": [{"contact_id": p1, "chat_id": group["id"]},
                              {"contact_id": p1, "chat_id": group["id"]},
                              {"contact_id": p2, "chat_id": group["id"]}]}
    checked = client.post("/creators/broadcast/preview", json=preview, headers=headers).json()
    assert checked["issues"] == [] and len(checked["groups"]) == 1
    assert len(checked["groups"][0]["mentions"]) == 2 and checked["can_send"] is False
    for purpose in creator_broadcast.PURPOSES.keys() - {"support"}:
        group_fields = {"cm_id": other_cm, "purpose": purpose, "mention": True,
                        "message": "Example", "selections": [{"chat_id": group["id"]}, {"chat_id": group["id"]}]}
        available = client.post("/creators/broadcast/groups", json=group_fields, headers=headers)
        assert available.status_code == 200 and available.json()["selected_chat_ids"] == []
        assert len(available.json()["groups"]) == 1 and available.json()["groups"][0]["members"] == []
        joined_sql = reads[-1]
        assert "ac.is_visible" in joined_sql and "sync_enabled" not in joined_sql
        assert "contact_roles" not in joined_sql and "chat_members" not in joined_sql
        assert "a.status='active'" in joined_sql and "a.body_consent_at IS NOT NULL" in joined_sql
        checked = client.post("/creators/broadcast/preview", json=group_fields, headers=headers).json()
        assert checked["mode"] == "groups" and checked["issues"] == [] and len(checked["groups"]) == 1
        assert checked["groups"][0]["mention_all"] and checked["groups"][0]["mentions"] == []
        assert not checked["can_send"] and checked["people"] == []
        assert client.post("/creators/broadcast/groups", json=dict(group_fields, cm_id="all"),
                           headers=headers).status_code == 400
        assert client.post(url, json=dict(request, purpose=purpose), headers=headers).status_code == 400
    preview.update(cm_id=other_cm, actor_id=other_cm)
    scoped = client.post("/creators/broadcast/preview", json=preview, headers=headers).json()
    assert len(scoped["issues"]) == 1 and len(scoped["groups"][0]["mentions"]) == 1
    changed["routes"] = True
    assert client.post("/creators/broadcast/preview", json=preview, headers=headers).json()["groups"] == []
    for invalid in (dict(request, purpose="unknown"), dict(request, raw=123), dict(request, raw="name\n" * 501)):
        assert client.post(url, json=invalid, headers=headers).status_code == 400
    json_headers = dict(headers, **{"Content-Type": "application/json"})
    assert client.post(url, content=b"x" * 300001, headers=json_headers).status_code == 400
    assert client.post(url, content=iter([b"x" * 300001]), headers=json_headers).status_code == 400
    assert client.post(url, content=b"{", headers=json_headers).status_code == 400
    assert client.post("/creators/broadcast/send", json=preview, headers=headers).status_code == 400
    assert any(sql == "SET TRANSACTION READ ONLY" for sql in reads)
    changed["routes"] = False
    preview.update(cm_id="all", purpose="support")
    resolve_url = "/creators/broadcast/resolve"
    resolved = client.post(resolve_url, json={"cm_id": "all", "purpose": "support",
                                             "contact_ids": [p1, p2, p1]}, headers=headers).json()
    assert len(resolved["entries"]) == 2 and resolved["duplicate_inputs"] == 1
    for scope in (None, "", "bad-uuid"):
        denied_scope = client.post(resolve_url, json={"cm_id": scope, "purpose": "support", "contact_ids": [p1]},
                                   headers=headers)
        assert denied_scope.status_code == 400
    lists_url = "/creators/broadcast/lists/"
    fields = {"name": "Example static list", "notes": "Example", "cm_id": "all", "purpose": "support",
              "mention": True, "selections": preview["selections"], "message": "must not be saved"}
    assert client.post(lists_url + "save", json=fields).status_code == 400
    for invalid in (dict(fields, name="bad\x00name"), dict(fields, notes="bad\x00notes")):
        assert client.post(lists_url + "save", json=invalid, headers=headers).status_code == 400
    invalid_unicode = json.dumps(dict(fields, name="bad\ud800name"))
    assert client.post(lists_url + "save", content=invalid_unicode,
                       headers=dict(headers, **{"Content-Type": "application/json"})).status_code == 400
    created = client.post(lists_url + "save", json=fields, headers=headers)
    assert created.status_code == 200
    item = created.json()["list"]
    stored = saved_lists[item["id"]]
    assert len(stored["targets"]) == 2 and "message" not in stored["snapshot"]
    assert item["target_count"] == 2
    loaded = client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).json()
    assert loaded["issues"] == [] and len(loaded["routes"]) == 2
    changed["routes"] = True
    stale = client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).json()
    assert len(stale["issues"]) == 2 and all(not route["chat_id"] for route in stale["routes"])
    assert all(not entry["selected"] for entry in stale["entries"])
    metadata = dict(fields, id=item["id"], revision=1, metadata_only=True, name="Renamed", cm_id="invalid",
                    selections=[], mention="invalid", purpose="invalid")
    renamed = client.post(lists_url + "save", json=metadata, headers=headers)
    assert renamed.status_code == 200 and renamed.json()["list"]["revision"] == 2
    assert stored["purpose"] == "support" and len(stored["targets"]) == 2
    assert client.post(lists_url + "save", json=metadata, headers=headers).status_code == 400
    invalid_update = client.post(lists_url + "save", json=dict(fields, id=item["id"], revision=2), headers=headers)
    assert invalid_update.status_code == 400
    changed["routes"] = False
    updated = client.post(lists_url + "save", json=dict(fields, id=item["id"], revision=2,
                           selections=[{"contact_id": p1, "chat_id": group["id"]}]), headers=headers)
    assert updated.status_code == 200 and updated.json()["list"]["target_count"] == 1, updated.text
    stored["owner_authorization_id"] = other_cm
    for action in ("load", "delete", "save"):
        foreign = client.post(lists_url + action, json=dict(fields, id=item["id"], revision=3), headers=headers)
        assert foreign.status_code == 404
    stored["owner_authorization_id"] = actor["id"]
    assert client.post(lists_url + "delete", json={"id": item["id"], "revision": 2}, headers=headers).status_code == 400
    deleted = client.post(lists_url + "delete", json={"id": item["id"], "revision": 3}, headers=headers)
    assert deleted.status_code == 200 and deleted.json()["lists"] == [] and stored["deleted_at"]
    assert client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).status_code == 404

    # Group lists do not require a creator contact or an assigned creator. Older list data is preserved.
    group_list = dict(group_fields, name="Group list", notes="Example")
    created_group = client.post(lists_url + "save", json=group_list, headers=headers)
    assert created_group.status_code == 200, created_group.text
    group_item = created_group.json()["list"]
    group_stored = saved_lists[group_item["id"]]
    assert group_item["target_count"] == 1 and group_stored["targets"] == [{"chat_id": group["id"]}]
    loaded_group = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert loaded_group["selected_chat_ids"] == [group["id"]] and loaded_group["issues"] == []
    assert loaded_group["mention"] and loaded_group["list"]["mention"]
    group_stored["targets"] = [{"contact_id": p1, "chat_id": group["id"]},
                               {"contact_id": p2, "chat_id": group["id"]}]
    legacy = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert legacy["list"]["targets"] == [{"chat_id": group["id"]}] and legacy["list"]["target_count"] == 1
    assert legacy["selected_chat_ids"] == [group["id"]] and legacy["issues"] == []
    assert not legacy["mention"] and not legacy["list"]["mention"] and "@all" in legacy["notice"]
    assert group_stored["mention"]  # Old @selected-people is not silently upgraded to @all or changed in DB.
    group_stored["targets"] = [{"chat_id": group["id"]}]
    group_stored["snapshot"].pop("mode")
    snapshot_legacy = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert not snapshot_legacy["list"]["mention"] and snapshot_legacy["selected_chat_ids"] == [group["id"]]
    group_stored["targets"] = [{"contact_id": p1, "chat_id": group["id"]},
                               {"contact_id": p2, "chat_id": group["id"]}]
    assert len(group_stored["targets"]) == 2  # Reading an old list never rewrites its stored targets.
    group_stored["cm_id"] = None
    legacy_all = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert legacy_all["selected_chat_ids"] == [] and legacy_all["issues"] and legacy_all["list"]["id"]
    group_stored["cm_id"] = other_cm
    changed["routes"] = True
    invalid_group = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert invalid_group["selected_chat_ids"] == [] and len(invalid_group["issues"]) == 1
    assert client.post(lists_url + "save", json=group_list, headers=headers).status_code == 400
    changed["routes"] = False
    changed["cm_active"] = False
    revoked_group = client.post(lists_url + "load", json={"id": group_item["id"]}, headers=headers).json()
    assert revoked_group["selected_chat_ids"] == [] and len(revoked_group["issues"]) == 1
    assert client.post("/creators/broadcast/groups", json=group_fields, headers=headers).status_code == 403
    assert client.post("/creators/broadcast/preview", json=group_fields, headers=headers).status_code == 403
    changed["cm_active"] = True

    templates_url = "/creators/broadcast/templates/"
    draft = {"name": "Example template", "body": "Example\nhttps://example.com"}
    assert client.post(templates_url + "save", json=draft).status_code == 400
    for invalid in (dict(draft, body=" "), dict(draft, body="x" * 4001),
                    dict(draft, body="bad\x00body"), dict(draft, name="bad\x00name")):
        assert client.post(templates_url + "save", json=invalid, headers=headers).status_code == 400
    template = client.post(templates_url + "save", json=draft, headers=headers)
    assert template.status_code == 200, template.text
    template_item = template.json()["template"]
    assert template_item["body"] == draft["body"]
    template_stored = saved_templates[template_item["id"]]
    assert creator_broadcast.page_data(actor["id"])["templates"] == [template_item]
    assert draft["name"] in client.get("/creators/broadcast").text
    update_template = dict(draft, id=template_item["id"], revision=1, body="Updated")
    assert client.post(templates_url + "save", json=update_template,
                       headers=headers).json()["template"]["revision"] == 2
    assert client.post(templates_url + "save", json=update_template, headers=headers).status_code == 400
    template_stored["owner_authorization_id"] = other_cm
    for action in ("save", "delete"):
        assert client.post(templates_url + action, json=dict(update_template, revision=2),
                           headers=headers).status_code == 404
    assert creator_broadcast.page_data(actor["id"])["templates"] == []
    template_stored["owner_authorization_id"] = actor["id"]
    assert client.post(templates_url + "delete", json={"id": template_item["id"], "revision": 1},
                       headers=headers).status_code == 400
    deleted_template = client.post(templates_url + "delete", json={"id": template_item["id"], "revision": 2},
                                   headers=headers)
    assert deleted_template.json()["templates"] == [] and template_stored["deleted_at"]
    # Preview/editor entry points retain baseline authorization, not optional send scopes.
    business_calls = (
        lambda: creator_broadcast.page_data(actor["id"]),
        lambda: creator_broadcast.match(actor["id"], other_cm, request["raw"], "support"),
        lambda: creator_broadcast.groups(actor["id"], other_cm, "category"),
        lambda: creator_broadcast.resolve(actor["id"], other_cm, "support", [p1]),
        lambda: creator_broadcast.preview(actor["id"], other_cm, "support", preview["selections"], "Example", True),
        lambda: creator_broadcast.list_save(actor["id"], fields),
        lambda: creator_broadcast.list_load(actor["id"], group_item["id"]),
        lambda: creator_broadcast.list_delete(actor["id"], group_item["id"], 1),
        lambda: creator_broadcast.template_save(actor["id"], draft),
        lambda: creator_broadcast.template_delete(actor["id"], template_item["id"], 1),
    )
    for removed_scope in creator_broadcast.SEND_SCOPES:
        changed["scopes"] = [scope for scope in required if scope != removed_scope]
        opened = client.get("/creators/broadcast", follow_redirects=False)
        assert opened.status_code == 200 and "location" not in opened.headers
        assert client.post(url, json=request, headers=headers).status_code == 200
        assert client.post("/creators/broadcast/preview", json=preview, headers=headers).status_code == 200
        assert client.post(templates_url + "save", json=draft, headers=headers).status_code == 200
        with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired) as denied:
            creator_broadcast._require_broadcast_grant(Conn(), actor["id"], sending=True)
        assert denied.value.account_id == str(actor["id"])
    changed["scopes"] = [scope for scope in required if scope != baseline[0]]
    before = len(reads)
    denied_page = client.get("/creators/broadcast", follow_redirects=False)
    assert denied_page.status_code == 403 and "location" not in denied_page.headers
    assert not any("creator_broadcast_lists" in sql or "creator_broadcast_templates" in sql
                   for sql in reads[before:])
    for call in business_calls:
        before = len(reads)
        with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired) as denied:
            call()
        assert denied.value.account_id == str(actor["id"])
        assert not any("creator_broadcast_lists" in sql or "creator_broadcast_templates" in sql
                       for sql in reads[before:])
    blocked = client.post(url, json=dict(request, cm_id=other_cm), headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    assert client.post(resolve_url, json={"cm_id": other_cm, "purpose": "support", "contact_ids": [p1]},
                       headers=headers).status_code == 403
    changed["scopes"] = list(required)
    changed["consent"] = False
    with pytest.raises(PermissionError, match="本人聊天授权已失效"):
        creator_broadcast.page_data(actor["id"])
    changed["consent"] = True
    assert client.post(url, json=request, headers=headers).status_code == 200
    changed["role"] = False
    assert client.get("/creators/broadcast").status_code == 403
    assert client.post(url, json=request, headers=headers).status_code == 403
    assert client.post(lists_url + "save", json=fields, headers=headers).status_code == 403
    assert client.post(templates_url + "save", json=draft, headers=headers).status_code == 403
    ordinary, _ = browser(monkeypatch)
    assert ordinary.get("/creators/broadcast").status_code == 403
    revoked, _ = browser(monkeypatch, owner=True, active=False)
    assert revoked.get("/creators/broadcast").status_code == 403
    app = FastAPI()
    app.include_router(creator_handles_routes.create_router(
        current_authorization=lambda _: None, csrf_token=lambda _: "",
        render_template=lambda *_: pytest.fail("Unauthenticated prototype was rendered"),
    ))
    denied = TestClient(app).get("/creators/broadcast", follow_redirects=False)
    assert (denied.status_code, denied.headers["location"]) == (303, "/")




def test_revoked_creator_session_cannot_read_or_modify_roster(monkeypatch):
    client, actor = browser(monkeypatch, active=False)
    assert client.get("/creators").status_code == 403
    assert client.get("/creators/data").status_code == 403
    assert client.post("/creators/people/link", data={
        "csrf_token": "csrf", "cm_id": str(actor["id"]), "contact_id": str(uuid4()), "handle": "fixture",
    }).status_code == 403


def test_excel_upload_is_bounded_scoped_and_requires_explicit_start_and_confirmation(monkeypatch):
    client, actor = browser(monkeypatch)
    batch, row, person, foreign = uuid4(), uuid4(), uuid4(), uuid4()
    calls = []

    def preview(actor_id, cm_id, filename, content):
        if cm_id != actor_id:
            raise PermissionError("只能导入自己的名单。")
        calls.append(("preview", actor_id, filename, content))
        return {"id": str(batch), "status": "preview", "counts": {"valid": 1, "duplicates": 0, "invalid": 0},
                "rows": [{"row_number": 2, "handle": "example", "error": ""}]}

    monkeypatch.setattr(creator_imports, "preview_import", preview)
    monkeypatch.setattr(creator_imports, "start_import", lambda *args: calls.append(("start", *args)))
    monkeypatch.setattr(creator_imports, "confirm_row", lambda *args: calls.append(("confirm", *args)))
    monkeypatch.setattr(creator_imports, "defer_row", lambda *args: calls.append(("defer", *args)))
    def candidates(actor_id, cm_id, **fields):
        assert actor_id == actor["id"] and cm_id == actor["id"] and fields["batch_id"] == batch
        return {"candidates": [{"id": str(row), "handle": "example"}], "total": 1}
    monkeypatch.setattr(creator_imports, "page_data", candidates)
    monkeypatch.setattr(creator_imports, "search_users", lambda *args: [{"id": str(person), "display_name": "Creator"}])
    url = f"/creators/imports/preview?cm_id={actor['id']}"
    headers = {"Content-Type": "application/octet-stream", "X-Filename": "CAP%20handles.xlsx"}
    assert client.post(url, content=b"xlsx", headers=headers).status_code == 400
    headers["X-CSRF-Token"] = "csrf"
    assert client.post(url, content=b"x" * (creator_imports.MAX_FILE_BYTES + 1), headers=headers).status_code == 400
    assert client.post(url, content=iter([b"x" * (creator_imports.MAX_FILE_BYTES + 1)]),
                       headers=headers).status_code == 400
    assert not calls
    assert client.post(f"/creators/imports/preview?cm_id={foreign}", content=b"xlsx",
                       headers=headers).status_code == 403
    result = client.post(url, content=b"xlsx", headers=headers)
    assert result.status_code == 200 and result.json()["counts"]["valid"] == 1
    assert result.headers["cache-control"] == "no-store, max-age=0"
    assert calls == [("preview", actor["id"], "CAP handles.xlsx", b"xlsx")]
    for creator_type in ("cap", "non_cap", "pending"):
        assert client.post(url + "&creator_type=" + creator_type, content=b"xlsx", headers=headers).status_code == 200
    assert len(calls) == 4
    assert client.post(f"/creators/imports/{batch}/start").status_code == 400
    assert client.post(f"/creators/imports/{batch}/start", data={"csrf_token": "csrf"}).status_code == 200
    assert calls[-1] == ("start", actor["id"], batch)
    confirm = f"/creators/imports/rows/{row}/confirm"
    assert client.post(confirm, data={"handle": "example", "contact_id": str(person)}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example"}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example", "contact_id": str(person),
                                      "actor_id": str(foreign)}).status_code == 200
    assert calls[-1] == ("confirm", actor["id"], row, "example", person)
    listed = client.get(f"/creators/imports/data?cm_id={actor['id']}&batch_id={batch}")
    assert listed.status_code == 200 and listed.json()["candidates"][0]["id"] == str(row)
    users = client.get(f"/creators/imports/users?cm_id={actor['id']}&q=Creator")
    assert users.status_code == 200 and users.json()[0]["id"] == str(person)
    defer = f"/creators/imports/rows/{row}/defer"
    assert client.post(defer).status_code == 400
    assert client.post(defer, data={"csrf_token": "csrf"}).status_code == 200
    assert calls[-1] == ("defer", actor["id"], row)
    for action, name in (("pause", "pause_import"), ("resume", "resume_import"), ("recheck", "recheck_batch")):
        def scoped(actor_id, batch_id, *, action=action):
            if batch_id != batch:
                raise PermissionError("只能操作自己的导入批次。")
            calls.append((action, actor_id, batch_id))

        monkeypatch.setattr(creator_imports, name, scoped)
        action_url = f"/creators/imports/{batch}/{action}"
        assert client.post(action_url).status_code == 400
        assert client.post(action_url, data={"csrf_token": "csrf", "actor_id": str(foreign)}).status_code == 200
        assert calls[-1] == (action, actor["id"], batch)
        assert client.post(f"/creators/imports/{foreign}/{action}", data={"csrf_token": "csrf"}).status_code == 403

    def evidence(actor_id, row_id):
        assert actor_id == actor["id"]
        if row_id != row:
            raise PermissionError("不能读取其他 CM 的材料。")
        return {"handle": "example", "stages": [{"stage": "group", "messages": []}]}

    monkeypatch.setattr(creator_imports, "row_evidence", evidence)
    material = client.get(f"/creators/imports/rows/{row}/evidence")
    assert material.status_code == 200 and material.json()["stages"][0]["stage"] == "group"
    assert material.headers["cache-control"] == "no-store, max-age=0"
    assert client.get(f"/creators/imports/rows/{foreign}/evidence").status_code == 403
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get(f"/creators/imports/rows/{row}/evidence").status_code == 403
    assert revoked.post(f"/creators/imports/{batch}/recheck", data={"csrf_token": "csrf"}).status_code == 403


def test_broadcast_recipient_matching_deduplicates_without_guessing_ambiguous_or_revoked_targets():
    sample = [{"id": "p1", "name": "Ava Collins", "handles": ["demo_ava", "demo_ava_backup"]},
              {"id": "p2", "name": "Ben Carter", "handles": ["demo_ben"]},
              {"id": "p3", "name": "Ben Carter", "handles": ["demo_ben_live"]},
              {"id": "p4", "name": "Demo Ava", "handles": ["shared_demo"], "aliases": ["Ava Collins", "Demo"]},
              {"id": "p5", "name": "Other Ava", "handles": ["shared_demo", "demo"]}]
    entries, duplicates = creator_broadcast._match_entries(
        "@DEMO_AVA\nhttps://tiktok.com/@demo_ava\nBen Carter\nAva", sample)
    assert duplicates == 1 and len(entries) == 3
    assert entries[0]["person"]["id"] == "p1"
    assert entries[1]["person"] is None and len(entries[1]["candidates"]) == 2
    assert entries[2]["person"] is None and entries[2]["approximate"]
    for query, ids in (("Ａｖａ　Ｃｏｌｌｉｎｓ", {"p1", "p4"}), ("@shared_demo", {"p4", "p5"})):
        result = creator_broadcast._match_entries(query, sample)[0][0]
        assert result["person"] is None and {p["id"] for p in result["candidates"]} == ids
    approximate = creator_broadcast._match_entries("dem", sample)[0][0]
    assert approximate["person"] is None and {p["id"] for p in approximate["candidates"]} >= {"p4", "p5"}
    assert creator_broadcast._match_entries("https://tiktok.com.evil.test/@demo_ava", sample)[0][0]["candidates"] == []
    with pytest.raises(ValueError):
        creator_broadcast._match_entries("name\n" * 501, sample)
    contact = "00000000-0000-0000-0000-000000000001"
    target = {"contact_id": contact, "chat_id": "demo-fixed"}
    assert creator_broadcast._targets([target, target], one_per_contact=True) == [(contact, "demo-fixed")]
    with pytest.raises(ValueError):
        creator_broadcast._targets([target, {**target, "chat_id": "demo-other"}], one_per_contact=True)
    revoked = creator_broadcast._resolve(None, contact, "support", [contact], [target], "CM 授权已失效")
    assert revoked["entries"][0]["person"] is None and not revoked["entries"][0]["selected"]
    assert revoked["routes"] == [{"contact_id": contact, "chat_id": ""}]
    assert len(revoked["issues"]) == 1 and not revoked["can_send"]
