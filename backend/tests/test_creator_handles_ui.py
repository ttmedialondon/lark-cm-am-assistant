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

from app import admin_permissions, creator_broadcast, creator_handles_routes, creator_imports
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
        render_template=lambda _request, name, context: HTMLResponse(templates.get_template(name).render(**context)),
    ))
    return TestClient(app), actor


def test_broadcast_permission_workflow_checks_real_account_without_sending(pg, monkeypatch):
    from app import worker

    client, actor = browser(monkeypatch, owner=True)
    other = uuid4()
    baseline = ("offline_access", "im:chat:read")
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
                           (cipher.encrypt("fixture-replacement"), other))
            elif state["change"] == "scope":
                pg.execute("UPDATE authorizations SET scopes='{}' WHERE id=%s", (other,))
            elif state["change"] == "revoked":
                pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (other,))
            elif state["change"] == "owner":
                pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (actor["id"],))
            elif state["change"] == "broadcast":
                pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor["id"],))
            open_id = "ou_owner" if token == "fixture-owner" else "ou_other"
            return LarkProfile("ou_wrong" if state["change"] == "identity" else open_id, None, None, None)

        def refresh_token(self, _token):
            state["refreshes"] += 1
            return TokenBundle("fixture-refreshed", "fixture-refresh-new", 3600, 86400, "")

    monkeypatch.setattr(creator_broadcast, "connection", connection)
    monkeypatch.setattr(worker, "connection", connection)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
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
    assert own["status"] == "ready" and own["is_self"] and not own["is_sender"]
    assert not own["missing_scopes"] and own["authorization_url"] is None and own["checked_at"]
    assert check(str(actor["id"]))["is_sender"] and state["refreshes"] == 0
    missing = check(str(other))
    assert missing["status"] == "needs_authorization" and set(missing["missing_scopes"]) == set(required)
    assert missing["is_sender"] and not missing["is_self"] and missing["account_id"] == str(other)
    assert missing["authorization_url"] == f"/auth/lark/start?send=1&account_id={other}"
    assert "发送消息" in missing["message"] and "以本人身份发送" in missing["message"]
    assert state["calls"][-1] == "fixture-other"
    assert check("")["status"] == "unavailable"
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
    for change in ("identity", "token", "scope", "revoked"):
        pg.execute("UPDATE authorizations SET status='active',scopes=%s,access_token_ciphertext=%s WHERE id=%s",
                   (list(required), cipher.encrypt("fixture-other"), other))
        state["change"] = change
        result = check(str(other))
        assert result["status"] == ("needs_authorization" if change == "revoked" else "unavailable")
        assert result["authorization_url"] is None and not result["missing_scopes"]
    state["change"] = None
    before = len(state["calls"])
    assert check(str(other))["status"] == "needs_authorization" and len(state["calls"]) == before
    pg.execute("UPDATE authorizations SET status='active',body_consent_at=NULL WHERE id=%s", (other,))
    assert check(str(other))["authorization_url"] is None and len(state["calls"]) == before
    pg.execute("UPDATE authorizations SET status='attention_required',body_consent_at=NOW(),scopes=%s WHERE id=%s",
               (list(required), other))
    attention = check(str(other))
    assert attention["status"] == "needs_authorization" and not attention["missing_scopes"]
    assert pg.execute("SELECT status FROM authorizations WHERE id=%s",
                      (other,)).fetchone()["status"] == "attention_required"
    pg.execute("""UPDATE authorizations SET body_consent_at=NOW(),scopes=%s,access_token_ciphertext=%s,
        access_token_expires_at=NOW()-INTERVAL '1 minute',status='active' WHERE id=%s""",
        (list(required), cipher.encrypt("fixture-other"), other))
    refreshed = check(str(other))
    assert state["refreshes"] == 1 and state["calls"][-1] == "fixture-refreshed"
    assert refreshed["status"] == "needs_authorization" and set(refreshed["missing_scopes"]) == set(required)
    assert pg.execute("SELECT scopes,status FROM authorizations WHERE id=%s", (other,)).fetchone() == {
        "scopes": [], "status": "active"}
    assert settings.oauth_scopes == baseline
    assert pg.execute("SELECT COUNT(*) AS n FROM sync_jobs").fetchone()["n"] == 0
    actor.update(status="attention_required", body_consent_at=True)
    reconnect = check(str(other))
    assert reconnect["status"] == "needs_authorization" and reconnect["is_self"]
    assert reconnect["account_id"] == str(actor["id"]) and not reconnect["is_sender"]
    assert reconnect["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    assert client.post(url, json={"cm_id": "all"}).status_code == 400
    attention_page = client.get("/creators/broadcast", follow_redirects=False)
    assert attention_page.status_code == 303
    assert attention_page.headers["location"] == f"/auth/lark/start?send=1&account_id={actor['id']}&auto=1"
    blocked = client.post("/creators/broadcast/templates/save", json={"name": "Example", "body": "Example"},
                          headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["authorization_url"] == f"/auth/lark/start?send=1&account_id={actor['id']}"
    actor["status"] = "active"
    state["change"] = "owner"
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
    assert opened.status_code == 200 and "发送（未开通）" in opened.text
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
    assert client.post("/creators/broadcast/send", json=own_fields, headers=headers).status_code == 404
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
    assert missing.status_code == 303
    assert missing.headers["location"] == f"/auth/lark/start?send=1&account_id={actor['id']}&auto=1"
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (list(required), actor["id"]))
    state["change"] = "broadcast"
    assert client.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403
    state["change"] = None
    assert client.get("/creators/broadcast").status_code == 403
    assert client.post("/creators/broadcast/templates/save", json=draft, headers=headers).status_code == 403
    ordinary, ordinary_actor = browser(monkeypatch)
    ordinary_actor["id"] = other
    assert ordinary.get("/creators/broadcast").status_code == 403
    ordinary_actor.update(status="attention_required", body_consent_at=True)
    assert ordinary.get("/creators/broadcast", follow_redirects=False).status_code == 403
    assert ordinary.post(url, json={"cm_id": "all"}, headers=headers).status_code == 403


def test_broadcast_real_data_workflow_rechecks_scope_routes_and_never_sends(monkeypatch):
    client, actor = browser(monkeypatch, owner=True)
    baseline = ("offline_access", "im:chat:read", "im:message:readonly", "im:chat.members:read",
                "contact:contact.base:readonly", "contact:user.base:readonly")
    required = (*baseline, *creator_broadcast.SEND_SCOPES)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: SimpleNamespace(oauth_scopes=baseline))
    p1, p2, other_cm = map(str, (uuid4(), uuid4(), uuid4()))
    people = [{"id": p1, "name": "Example One", "handles": ["example_one", "example_backup"]},
              {"id": p2, "name": "Example Two", "handles": ["example_two"]}]
    group = {"id": "oc_example", "name": "Example group", "purpose": "support", "members": [p1, p2],
             "member_count": None, "membership_note": creator_broadcast.MEMBERSHIP_NOTE}
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
                    name, body, owner = params
                    item = {"id": uuid4(), "name": name, "body": body, "owner_authorization_id": owner,
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
                        item.update(name=params[0], body=params[1])
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
                return Rows([{"chat_id": group["id"], "name": group["name"], "purpose": params[0],
                              "member_count": None, "member_count_status": "pending",
                              "member_snapshot_checked_at": None, "member_snapshot_status": "pending",
                              "last_seen_at": None}] if not changed["routes"] else [])
            raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(creator_broadcast, "connection", lambda: nullcontext(Conn()))
    monkeypatch.setattr(creator_broadcast, "_people", lambda _conn, cm, ids=None: [
        person for person in people if (ids is None or person["id"] in ids) and (cm is None or person["id"] == p1)
    ])
    monkeypatch.setattr(creator_broadcast, "_groups", lambda _conn, ids, _cm, purpose: [dict(group, purpose=purpose,
                        members=[contact for contact in group["members"] if contact in ids])]
                        if ids and not changed["routes"] else [])
    response = client.get("/creators/broadcast")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "仅预览 · 不会发送" in response.text
    assert "发送（未开通）" in response.text and "creator-broadcast.mjs" in response.text
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
    assert client.post("/creators/broadcast/send", json=preview, headers=headers).status_code == 404
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
    # Every business entry is gated by the live actor grant, even when another CM has all scopes.
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
    for removed_scope in (baseline[0], *creator_broadcast.SEND_SCOPES):
        changed["scopes"] = [scope for scope in required if scope != removed_scope]
        before = len(reads)
        redirect = client.get("/creators/broadcast", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == f"/auth/lark/start?send=1&account_id={actor['id']}&auto=1"
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




def test_retired_handle_ai_and_type_actions_still_require_active_auth_and_csrf(monkeypatch):
    client, actor = browser(monkeypatch)
    run, task = uuid4(), uuid4()
    actions = [
        "/creators/preview",
        *(
            f"/creators/runs/{run}/{name}"
            for name in (
                "start",
                "pause",
                "resume",
                "auto-admit",
            )
        ),
        *(f"/creators/tasks/{task}/{name}" for name in ("recheck", "confirm", "defer")),
        f"/creators/people/{task}/type",
    ]
    for path in actions:
        assert client.post(path, data={
            "csrf_token": "csrf", "cm_id": str(actor["id"]), "enabled": "1",
        }).status_code == 410
    for invalid in ({}, {"csrf_token": "wrong"}):
        assert client.post(f"/creators/tasks/{task}/confirm", data=invalid).status_code == 400
    owner, _admin = browser(monkeypatch, owner=True)
    for path in actions:
        assert owner.post(path, data={"cm_id": str(actor["id"]), "enabled": "1"}).status_code == 400
        assert owner.post(path, data={"csrf_token": "csrf", "enabled": "1"}).status_code == 410
    evidence = client.get(f"/creators/tasks/{task}/evidence")
    assert evidence.status_code == 410
    assert evidence.headers["cache-control"] == "no-store, max-age=0"
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get("/creators").status_code == 403
    assert revoked.get(f"/creators/tasks/{task}/evidence").status_code == 403
    for path in actions:
        assert revoked.post(path, data={"csrf_token": "csrf"}).status_code == 403
    assert revoked.post(f"/creators/tasks/{task}/defer", data={"csrf_token": "csrf"}).status_code == 403


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
