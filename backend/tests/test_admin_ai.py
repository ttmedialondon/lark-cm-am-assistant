import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin, admin_permissions, admin_reporting, ai_feature_config, assistant_rules
from app import creator_matching_routes as matching_routes


@pytest.fixture(autouse=True)
def legacy_role_scenarios_use_explicit_acl(monkeypatch):
    monkeypatch.setattr(admin_permissions, "access_for", lambda authorization_id: admin_permissions._access({
        "status": "active", "role": admin_reporting.admin_role_for(authorization_id),
        "role_active": True, "permissions": [],
    }))
    monkeypatch.setattr(admin.creator_importance, "metrics", lambda _: {
        "counts": {"pending": 0, "running": 0, "succeeded": 0, "failed": 0},
        "usage": {"api_requests": 0, "input_tokens": 0, "output_tokens": 0},
    })


class _Connection:
    def __init__(self):
        self.calls = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params=None):
        self.calls.append((query, params))


@contextmanager
def _connection():
    yield _Connection()


def _client(current_authorization, contexts=None):
    app = FastAPI()

    def render(_request, name, context, status_code=200):
        if contexts is not None:
            contexts.append((name, context))
        return PlainTextResponse("rendered", status_code=status_code)

    app.include_router(
        admin.create_admin_router(
            current_authorization=current_authorization,
            render_template=render,
            csrf_token=lambda _authorization: "csrf",
        )
    )
    return TestClient(app)


def _config():
    return {
        "feature": "offhours_summary",
        "provider": "openai",
        "provider_label": "OpenAI",
        "project_id": "proj_summary",
        "model": "gpt-5.6-luna",
        "auto_confirm_threshold": 0.9,
        "auto_confirm_threshold_label": "90%",
        "enabled": False,
        "enabled_label": "已停用",
        "enabled_badge": "secondary",
        "key_configured": True,
        "key_suffix": "abcd",
        "api_key_label": "已配置 · …abcd",
        "last_tested_at_label": "尚未测试",
        "last_test_status_label": "尚未测试",
        "last_test_badge": "secondary",
        "key_updated_at": None,
        "key_updated_at_label": "尚未配置",
    }


def test_owner_can_view_only_safe_ai_config_fields(monkeypatch):
    owner_id = uuid4()
    contexts = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin, "connection", _connection)
    monkeypatch.setattr(ai_feature_config, "get_feature_config", lambda *_args: {"api_key_ciphertext": "secret"})
    monkeypatch.setattr(ai_feature_config, "display_config", lambda _row: _config())
    monkeypatch.setattr(ai_feature_config, "save_feature_config", lambda *a, **kw: pytest.fail("must not save"))
    monkeypatch.setattr(admin.offhours_summary, "get_settings_view", lambda _: {"status_label": "未开启"})
    monkeypatch.setattr(
        admin.ai_dispatch,
        "status",
        lambda _conn: {"state": "paused", "label": "AI 队列已暂停", "badge": "danger", "message": "safe"},
    )

    browser = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts)
    response = browser.get("/admin/ai")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert contexts[-1][0] == "admin/ai.html"
    for item in contexts[-1][1]["configs"]:
        assert item["config"]["api_key_label"] == "已配置 · …abcd"
        assert "api_key_ciphertext" not in item["config"]
    assert contexts[-1][1]["ai_dispatch"]["state"] == "paused"
    assert contexts[-1][1]["summary_settings"]["status_label"] == "未开启"
    assert [item["feature"] for item in contexts[-1][1]["configs"]] == [
        "operations_assistant",
        "creator_import", "creator_handle_matching", "creator_responsibility_matching",
        "creator_group_classification",
        "creator_importance",
        "offhours_summary", "desktop_reply",
    ]
    for feature in ("contact_assessment", "contact_assignment", "creator_workflow"):
        response = browser.post("/admin/ai", data={"csrf_token": "csrf", "feature": feature, "enabled": "1"})
        assert response.status_code == 400
        assert "未知的 AI 功能配置" in contexts[-1][1]["form_error"]


@pytest.mark.parametrize("enabled", ["1", ""])
@pytest.mark.parametrize("feature", ["offhours_summary", "creator_import"])
def test_owner_post_passes_key_to_encrypted_store_without_rendering_it(monkeypatch, enabled, feature):
    owner_id = uuid4()
    saved = []
    connections = []

    @contextmanager
    def connection():
        conn = _Connection()
        connections.append(conn)
        yield conn

    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin, "connection", connection)
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"k" * 32))
    monkeypatch.setattr(ai_feature_config, "api_key_cipher", lambda master_key: ("cipher", master_key))
    monkeypatch.setattr(ai_feature_config, "get_feature_config", lambda *_: {"auto_confirm_threshold": "0.980"})

    def save_config(*args, **kwargs):
        assert args[0].calls == ([("SELECT pg_advisory_xact_lock_shared("
                                  "hashtextextended('lark-sync-material',0))", None)]
                                if feature == "offhours_summary" else [])
        saved.append((args, kwargs))

    monkeypatch.setattr(ai_feature_config, "save_feature_config", save_config)
    monkeypatch.setattr(admin.offhours_summary, "save_settings_and_enqueue", lambda *args: None)

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        "/admin/ai",
        data={
            "csrf_token": "csrf",
            "feature": feature,
            "project_id": "proj_summary",
            "model": "gpt-5.6-luna",
            "enabled": enabled,
            **({"auto_confirm_threshold": "0.970"} if feature == "creator_import" and enabled
               else {"auto_confirm_threshold": "0.900"} if feature == "offhours_summary" else {}),
            "api_key": "sk-secret-never-rendered",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    anchor = feature.replace("_", "-")
    assert response.headers["location"] == f"/admin/ai?saved={anchor}#{anchor}"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert len(saved) == 1
    args, kwargs = saved[0]
    assert args[1:] == (("cipher", b"k" * 32), feature)
    assert kwargs == {
        "provider": "openai",
        "project_id": "proj_summary",
        "model": "gpt-5.6-luna",
        "enabled": feature != "creator_import" and enabled == "1",
        "api_key": "sk-secret-never-rendered",
        "updated_by_authorization_id": owner_id,
        "auto_confirm_threshold": ("0.970" if enabled else "0.980") if feature == "creator_import" else "0.900",
    }
    audit = connections[0].calls[-1][1]
    assert audit[2] == "ai_feature_config_updated"
    assert "sk-secret-never-rendered" not in audit[3]
    assert '"key_rotated": true' in audit[3]


def test_owner_can_resume_the_shared_ai_queue(monkeypatch):
    owner_id = uuid4()
    connections = []
    resumed = []

    @contextmanager
    def connection():
        conn = _Connection()
        connections.append(conn)
        yield conn

    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin, "connection", connection)
    monkeypatch.setattr(admin.ai_dispatch, "resume", lambda conn: resumed.append(conn))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        "/admin/ai/resume", data={"csrf_token": "csrf"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ai"
    assert resumed == [connections[0]]
    audit = connections[0].calls[-1][1]
    assert audit[2] == "ai_queue_resumed"
    assert audit[3] == '{"scope": "shared"}'


@pytest.mark.parametrize("enabled", ["1", ""])
def test_summary_save_uses_same_transaction_and_immediately_schedules(monkeypatch, enabled):
    owner_id = uuid4()
    called = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: "owner")
    monkeypatch.setattr(admin, "connection", _connection)
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"k" * 32))
    monkeypatch.setattr(ai_feature_config, "save_feature_config", lambda conn, *a, **kw: called.append(
        ("config", conn, kw["enabled"])))
    monkeypatch.setattr(admin.offhours_summary, "save_settings_and_enqueue", lambda conn, actor, fields: called.append(
        ("summary", conn, actor, fields)))
    response = _client(lambda _: {"id": owner_id, "status": "active"}).post("/admin/ai", data={
        "csrf_token": "csrf", "feature": "offhours_summary", "project_id": "project",
        "model": "configured-model", "enabled": enabled,
        "window_start": "2026-09-09T18:00", "window_end": "2026-09-10T12:00",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ai?saved=offhours-summary#offhours-summary"
    assert [item[0] for item in called] == ["config", "summary"]
    assert called[0][1] is called[1][1]
    assert called[0][2] == (enabled == "1")
    assert called[1][2] == owner_id
    assert called[1][3]["window_end"] == "2026-09-10T12:00"


def test_summary_csrf_failure_does_not_save_or_schedule(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: "owner")
    monkeypatch.setattr(admin, "connection", lambda: pytest.fail("must reject before DB"))
    response = _client(lambda _: {"id": uuid4(), "status": "active"}).post("/admin/ai", data={
        "csrf_token": "wrong", "feature": "offhours_summary", "enabled": "1",
    })
    assert response.status_code == 400


def test_rule_form_rejects_bad_csrf_duplicate_and_oversized_input_before_storage(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: "owner")
    monkeypatch.setattr(admin, "connection", lambda: pytest.fail("invalid form must not reach storage"))
    client = _client(lambda _: {"id": uuid4(), "status": "active"})
    form = {"csrf_token": "csrf", "document": "core", "revision": "0", "content": "test",
            "title": "核心规则", "description": "权限", "kind": "rule", "status": "published", "visibility": "all"}
    for body in (urlencode({**form, "csrf_token": "wrong"}), urlencode(form) + "&revision=0",
                 urlencode({**form, "content": "四" * (assistant_rules.MAX_DOCUMENT_CHARS * 2)})):
        assert client.post("/admin/assistant-rules", content=body,
                           headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 400


def test_owner_rule_editor_encrypts_overrides_rejects_conflicts_and_live_revocation(pg, monkeypatch):
    owner, member = uuid4(), uuid4()
    for actor in (owner, member):
        pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
                   (actor, "ou_" + actor.hex))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (owner,))
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) VALUES(%s,'ai',%s)",
               (member, owner))
    settings = SimpleNamespace(token_encryption_key=b"k" * 32)
    monkeypatch.setattr(admin, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(admin_permissions, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(admin_permissions, "access_for",
                        lambda actor: admin_permissions.users_access_data([actor])[actor])
    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    session = {"auth": {"id": owner, "status": "active"}}
    templates = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"),
                            autoescape=select_autoescape(["html"]))
    app = FastAPI()
    app.include_router(admin.create_admin_router(
        current_authorization=lambda _: session["auth"], csrf_token=lambda _: "csrf",
        render_template=lambda request, name, context, status_code=200: HTMLResponse(
            templates.get_template(name).render(**context), status_code=status_code),
    ))
    client, path = TestClient(app), "/admin/assistant-rules"
    pinned = assistant_rules.snapshot(pg, settings)
    defaults = assistant_rules.builtin_snapshot()
    assert pinned["protocol"] == 2
    backend_keys = assistant_rules.BACKEND_KEYS
    assert backend_keys <= assistant_rules.library(pg, settings).keys()
    assert {item["key"] for item in pinned["catalog"]} <= pinned["documents"].keys()
    for owner_scope in (False, True):
        navigation = assistant_rules.snapshot(pg, settings, owner=owner_scope)
        assert not backend_keys.intersection(navigation["documents"])
        assert not backend_keys.intersection(item["key"] for item in navigation["catalog"])
        for backend_key in backend_keys:
            with pytest.raises(PermissionError):
                assistant_rules.read_documents(navigation, [backend_key])
        from app import reply_rules
        assert not backend_keys.intersection(reply_rules.from_snapshot(navigation, 'desktop')['documents'])
    assert {key: pinned["documents"][key]["text"] for key in assistant_rules.KEYS} == {
        key: document["text"] for key, document in defaults["documents"].items()}
    pinned_before = json.dumps(pinned, sort_keys=True)
    for key, document in pinned["documents"].items():
        assistant_rules.validate(key, document["text"])
    page = client.get(path)
    assert page.status_code == 200
    content = pinned["documents"]["core"]["text"].replace(
        "## 主对话\n", "## 主对话\n测试 <script>alert(1)</script>。\n")
    core = assistant_rules.library(pg, settings)["core"]
    form = {"csrf_token": "csrf", "document": "core", "revision": "0", "content": content.replace("\n", "\r\n"),
            **{key: core[key] for key in ("title", "description", "kind", "status", "visibility")}}
    assert client.post(path, data={**form, "csrf_token": "wrong"}).status_code == 400
    assert pg.execute("SELECT count(*) n FROM assistant_rule_overrides").fetchone()["n"] == 0
    saved = client.post(path, data=form, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == path + "?document=core&saved=core"
    row = pg.execute("SELECT * FROM assistant_rule_overrides").fetchone()
    assert row["revision"] == 1 and row["updated_by_authorization_id"] == owner
    assert content not in row["content_ciphertext"]
    current = assistant_rules.snapshot(pg, settings)
    assert current["documents"]["core"]["text"] == content and current["digest"] != pinned["digest"]
    assert json.dumps(pinned, sort_keys=True) == pinned_before  # An existing turn keeps its pinned snapshot.
    conflict = client.post(path, data={**form, "content": content + "\n未保存修改。"})
    assert conflict.status_code == 409 and "未保存修改。" in conflict.text and "当前已保存版本" in conflict.text
    assert "<script>alert(1)</script>" not in conflict.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in conflict.text
    assert pg.execute("SELECT revision FROM assistant_rule_overrides").fetchone()["revision"] == 1
    for invalid in ("", content.replace("## 分段归纳\n", ""), "x" * (assistant_rules.MAX_DOCUMENT_CHARS + 1)):
        assert client.post(path, data={**form, "revision": "1", "content": invalid}).status_code == 400
    for field, value in (("kind", "knowledge"), ("status", "draft"), ("visibility", "owner")):
        assert client.post(path, data={**form, "revision": "1", field: value}).status_code == 400
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action='assistant_rule_updated'").fetchone()["metadata"]
    assert audit["revision"] == 1 and "content" not in audit and "测试" not in json.dumps(audit, ensure_ascii=False)
    draft_form = {"csrf_token": "csrf", "document": "", "revision": "0", "title": "样品进度手册",
                  "description": "回答寄样安排及后续进展。", "kind": "manual", "status": "draft",
                  "visibility": "all", "content": "先读取实际样品询问，再核对员工回复及后续消息。"}
    assert client.post(path, data={**draft_form, "kind": "invalid"}).status_code == 400
    created = client.post(path, data=draft_form, follow_redirects=False)
    assert created.status_code == 303
    key = created.headers["location"].split("document=", 1)[1].split("&", 1)[0]
    assert key.startswith("doc-")
    document = assistant_rules.library(pg, settings)[key]
    assert document["status"] == "draft" and document["revision"] == 1
    publish_form = {**draft_form, "document": key, "revision": "1", "status": "published"}
    assert client.post(path, data=publish_form, follow_redirects=False).status_code == 303
    document = assistant_rules.library(pg, settings)[key]
    assert document["status"] == "published" and document["revision"] == 2
    stale = client.post(path, data={**publish_form, "title": "未保存标题", "description": "未保存说明",
                                   "content": "未保存的手册正文。"})
    assert stale.status_code == 409
    assert assistant_rules.library(pg, settings)[key] == document
    assert client.post(path, data={**publish_form, "revision": "2", "status": "archived"},
                       follow_redirects=False).status_code == 303
    assert assistant_rules.library(pg, settings)[key]["status"] == "archived"
    event_document = assistant_rules.library(pg, settings)["event-extraction"]
    event_content = event_document["text"] + "\n测试事件手册 <script>alert(2)</script>。"
    event_form = {"csrf_token": "csrf", "document": "event-extraction", "revision": "0", "content": event_content,
                  **{name: event_document[name] for name in ("title", "description", "kind", "status", "visibility")}}
    navigation_before = assistant_rules.snapshot(pg, settings, owner=True)
    assert client.post(path, data=event_form, follow_redirects=False).status_code == 303
    event_row = pg.execute("SELECT * FROM assistant_rule_overrides WHERE rule_key='event-extraction'").fetchone()
    assert event_row["revision"] == 1 and event_content not in event_row["content_ciphertext"]
    assert assistant_rules.library(pg, settings)["event-extraction"]["text"] == event_content
    assert assistant_rules.snapshot(pg, settings, owner=True) == navigation_before
    event_conflict = client.post(path, data={**event_form, "content": event_content + "\n未保存的事件修改。"})
    assert event_conflict.status_code == 409 and "未保存的事件修改。" in event_conflict.text
    for field, value in (("kind", "knowledge"), ("status", "draft"), ("status", "archived"), ("visibility", "all")):
        assert client.post(path, data={**event_form, "revision": "1", field: value}).status_code == 400
    assert assistant_rules.library(pg, settings)["event-extraction"]["revision"] == 1
    responsibility_key = "responsibility-judgment"
    responsibility_document = assistant_rules.library(pg, settings)[responsibility_key]
    responsibility_pinned = assistant_rules.responsibility_snapshot(pg, settings)
    assert set(responsibility_pinned["documents"]) == assistant_rules.RESPONSIBILITY_KEYS
    assert all(doc["text"] in assistant_rules.responsibility_instructions(responsibility_pinned)
               for doc in responsibility_pinned["documents"].values())
    responsibility_page = client.get(path + '?document=' + responsibility_key)
    assert responsibility_page.status_code == 200 and '固定用途：负责人匹配' in responsibility_page.text
    assert 'id="reply-usable"' not in responsibility_page.text
    responsibility_form = {"csrf_token": "csrf", "document": responsibility_key, "revision": "0",
        "content": responsibility_document["text"] + "\n测试负责人规则。", "title": responsibility_document["title"],
        "description": responsibility_document["description"], "kind": "manual", "status": "published",
        "visibility": "owner"}
    for field, value in (("kind", "knowledge"), ("visibility", "all"), ("reply_usable", "1")):
        assert client.post(path, data={**responsibility_form, field: value}).status_code == 400
    assert client.post(path, data=responsibility_form, follow_redirects=False).status_code == 303
    assert assistant_rules.responsibility_snapshot(pg, settings)["digest"] != responsibility_pinned["digest"]
    assistant_rules.responsibility_validate_access(pg, responsibility_pinned)
    assert assistant_rules.snapshot(pg, settings, owner=True) == navigation_before
    creator_key = "creator-identity"
    creator_document = assistant_rules.library(pg, settings)[creator_key]
    creator_pinned = assistant_rules.creator_snapshot(pg, settings)
    creator_pinned_before = json.dumps(creator_pinned, sort_keys=True)
    assert set(creator_pinned["documents"]) == assistant_rules.CREATOR_KEYS
    identity_rules = assistant_rules.creator_instructions(creator_pinned, "identity")
    handle_rules = assistant_rules.creator_instructions(creator_pinned, "handle")
    assert creator_document["text"] in identity_rules and creator_document["text"] not in handle_rules
    assert creator_pinned["documents"]["creator-handle-link"]["text"] in handle_rules
    assert creator_pinned["documents"]["creator-handle-link"]["text"] not in identity_rules
    assert creator_pinned["documents"]["creator-regression"]["text"] not in identity_rules + handle_rules
    for changed_field, value in (("text", "未经版本校验的规则"), ("title", "篡改标题"), ("revision", 10)):
        damaged = json.loads(creator_pinned_before)
        damaged["documents"][creator_key][changed_field] = value
        with pytest.raises(ValueError):
            assistant_rules.creator_instructions(damaged, "identity")
    damaged = json.loads(creator_pinned_before)
    del damaged["documents"][creator_key]
    with pytest.raises(ValueError):
        assistant_rules.creator_instructions(damaged, "identity")
    creator_content = creator_document["text"] + "\n测试识别规范 <script>alert(3)</script>。"
    creator_form = {"csrf_token": "csrf", "document": creator_key, "revision": "0", "content": creator_content,
                    **{name: creator_document[name]
                       for name in ("title", "description", "kind", "status", "visibility")}}
    assert client.post(path, data={**creator_form, "csrf_token": "wrong"}).status_code == 400
    assert client.post(path, data=creator_form, follow_redirects=False).status_code == 303
    creator_row = pg.execute("SELECT * FROM assistant_rule_overrides WHERE rule_key=%s", (creator_key,)).fetchone()
    assert creator_row["revision"] == 1 and creator_content not in creator_row["content_ciphertext"]
    assert assistant_rules.library(pg, settings)[creator_key]["text"] == creator_content
    assert assistant_rules.creator_snapshot(pg, settings)["digest"] != creator_pinned["digest"]
    assert json.dumps(creator_pinned, sort_keys=True) == creator_pinned_before
    assistant_rules.creator_validate_access(pg, creator_pinned, "identity")  # Published edits keep the pinned task.
    assert assistant_rules.snapshot(pg, settings, owner=True) == navigation_before
    creator_conflict = client.post(path, data={**creator_form, "content": creator_content + "\n未保存的识别修改。"})
    assert creator_conflict.status_code == 409 and "未保存的识别修改。" in creator_conflict.text
    for field, value in (("kind", "knowledge"), ("visibility", "all")):
        assert client.post(path, data={**creator_form, "revision": "1", field: value}).status_code == 400
    regression = assistant_rules.library(pg, settings)["creator-regression"]
    assistant_rules.save_document(pg, settings, owner, "creator-regression", regression["title"],
        regression["description"], "manual", "draft", "owner", regression["text"], 0)
    development_only = assistant_rules.creator_snapshot(pg, settings)
    assistant_rules.creator_validate_access(pg, development_only, "identity")
    with pytest.raises(PermissionError):
        assistant_rules.creator_instructions(development_only, "regression")
    paused_jobs = {}
    for name, purpose, state, reason in (("identity", "identity", "waiting_evidence", "rules_unavailable"),
            ("handles", "handles", "waiting_evidence", "rules_unavailable"),
            ("no_text", "identity", "waiting_evidence", "no_text"),
            ("running", "identity", "running", "rules_unavailable"),
            ("closed", "identity", "completed", "rules_unavailable")):
        contact, job = uuid4(), uuid4()
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact, name))
        pg.execute("INSERT INTO creator_handle_jobs(id,contact_id,purpose,status,reason_code) VALUES(%s,%s,%s,%s,%s)",
                   (job, contact, purpose, state, reason))
        paused_jobs[name] = job
    paused_imports = {}
    old_pack = assistant_rules.codec.encode({"group": {"pack": {"_rules": creator_pinned}}}, settings)
    for state in ("running", "paused", "completed"):
        batch, row_id = uuid4(), uuid4()
        pg.execute("INSERT INTO creator_import_batches(id,cm_authorization_id,uploaded_by,filename,file_fingerprint,"
            "policy_version,status) VALUES(%s,%s,%s,'roster.xlsx',%s,'test',%s)",
            (batch, owner, owner, str(batch), state))
        pg.execute("INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,row_number,original_handle,handle,"
            "status,reason_code,pack_ciphertext,stats) VALUES(%s,%s,%s,1,'test','test','invalid',"
            "'rules_unavailable',%s,"
            "'{\"analysis_stage\":\"person\",\"analysis_generation\":2}')", (row_id, batch, owner, old_pack))
        paused_imports[state] = row_id
    for revision, status in enumerate(("draft", "published", "archived"), 1):
        assert client.post(path, data={**creator_form, "revision": str(revision), "status": status},
                           follow_redirects=False).status_code == 303
        assert assistant_rules.library(pg, settings)[creator_key]["status"] == status
        assert assistant_rules.snapshot(pg, settings, owner=True) == navigation_before
        current_creator = assistant_rules.creator_snapshot(pg, settings)
        assistant_rules.creator_validate_access(pg, current_creator, "handle")
        if status == "published":
            assistant_rules.creator_validate_access(pg, creator_pinned, "identity")
            resumed = pg.execute("SELECT status,desired_version FROM creator_handle_jobs WHERE id=%s",
                                 (paused_jobs["identity"],)).fetchone()
            assert resumed == {"status": "queued", "desired_version": 2}
            for name, state in (("handles", "waiting_evidence"), ("no_text", "waiting_evidence"),
                                ("running", "running"), ("closed", "completed")):
                unchanged = pg.execute("SELECT status,desired_version FROM creator_handle_jobs WHERE id=%s",
                                       (paused_jobs[name],)).fetchone()
                assert unchanged == {"status": state, "desired_version": 1}
            resumed_import = pg.execute("SELECT * FROM creator_import_rows WHERE id=%s",
                                        (paused_imports["running"],)).fetchone()
            assert resumed_import["status"] == "pending" and resumed_import["reason_code"] == ""
            assert resumed_import["pack_ciphertext"] is None and resumed_import["result_ciphertext"] is None
            assert resumed_import["stats"]["analysis_generation"] == 3
            assert resumed_import["stats"]["analysis_stage"] == "group"
            for state in ("paused", "completed"):
                untouched = pg.execute("SELECT status,pack_ciphertext FROM creator_import_rows WHERE id=%s",
                                       (paused_imports[state],)).fetchone()
                assert untouched == {"status": "invalid", "pack_ciphertext": old_pack}
            archived = pg.execute("SELECT metadata FROM audit_events "
                "WHERE action='creator_import_evidence_invalidated' "
                "AND metadata->>'row_id'=%s", (str(paused_imports["running"]),)).fetchone()["metadata"]
            assert archived["previous"]["pack_ciphertext"] == old_pack
        else:
            with pytest.raises(PermissionError):
                assistant_rules.creator_instructions(current_creator, "identity")
            with pytest.raises(PermissionError):
                assistant_rules.creator_validate_access(pg, creator_pinned, "identity")
    creator_audit = pg.execute("SELECT metadata FROM audit_events WHERE action='assistant_rule_updated' "
        "AND metadata->>'document'=%s ORDER BY (metadata->>'revision')::int DESC LIMIT 1",
        (creator_key,)).fetchone()["metadata"]
    assert creator_audit["revision"] == 4 and "content" not in creator_audit
    assert "测试识别" not in json.dumps(creator_audit, ensure_ascii=False)
    session["auth"] = {"id": member, "status": "active"}
    assert client.get(path).status_code == 403 and client.post(path, data=form).status_code == 403
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) "
               "VALUES(%s,'assistant-rules',%s)", (member, owner))
    assert client.get(path + '?new=1').status_code == 200
    assert client.get(path + '?document=' + creator_key).status_code == 200
    assert client.get(path + '?document=' + responsibility_key).status_code == 200
    assert client.post(path, data={**responsibility_form, "revision": "1",
        "content": responsibility_form["content"] + "\n文档管理员维护。"}, follow_redirects=False).status_code == 303
    saved_responsibility = assistant_rules.library(pg, settings)[responsibility_key]
    assert saved_responsibility["revision"] == 2 and saved_responsibility["updated_by"] == str(member)
    delegated = client.post(path, data={**draft_form, 'kind': 'knowledge', 'status': 'published',
        'visibility': 'owner', 'content': 'PRIVATE_DELEGATED_FACT'}, follow_redirects=False)
    assert delegated.status_code == 303
    delegated_key = delegated.headers['location'].split('document=', 1)[1].split('&', 1)[0]
    stored = assistant_rules.library(pg, settings)[delegated_key]
    assert stored['updated_by'] == str(member) and stored['revision'] == 1
    assert delegated_key not in assistant_rules.snapshot(pg, settings)['documents']
    from app import assistant_tools, reply_rules
    assert not reply_rules._owner(pg, member)
    assert not assistant_tools._owner(pg, member)
    with pytest.raises(PermissionError):
        assistant_tools.effective_context(pg, member, {}, {'cm_id': str(owner)})
    assert client.post('/admin/ai', data={'csrf_token': 'csrf'}).status_code == 403
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s AND permission='assistant-rules'",
               (member,))
    assert client.get(path + '?document=' + delegated_key).status_code == 403
    assert client.get(path + '?document=' + responsibility_key).status_code == 403
    assert client.post(path, data={**draft_form, 'document': delegated_key, 'revision': '1'}).status_code == 403
    with pytest.raises(PermissionError):
        assistant_rules.save_document(pg, settings, member, delegated_key, stored['title'], stored['description'],
            stored['kind'], stored['status'], stored['visibility'], stored['text'], stored['revision'])
    assert assistant_rules.library(pg, settings)[delegated_key] == stored
    assert assistant_rules.library(pg, settings)[responsibility_key] == saved_responsibility
    session["auth"] = {"id": owner, "status": "active"}
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (owner,))
    assert client.get(path).status_code == 403 and client.post(path, data=form).status_code == 403
    with pg.transaction(), pytest.raises(PermissionError):
        assistant_rules.save_document(pg, settings, owner, "core", core["title"], core["description"],
            "rule", "published", "all", content, 1)
    with pg.transaction(), pytest.raises(PermissionError):
        assistant_rules.save_document(pg, settings, owner, creator_key, creator_document["title"],
            creator_document["description"], "manual", "published", "owner", creator_content, 4)
    assert pg.execute("SELECT revision FROM assistant_rule_overrides WHERE rule_key='core'").fetchone()["revision"] == 1
    saved_creator = pg.execute("SELECT revision FROM assistant_rule_overrides WHERE rule_key=%s",
                              (creator_key,)).fetchone()
    assert saved_creator["revision"] == 4


@pytest.mark.parametrize("feature", [
    "creator_handle_matching", "creator_responsibility_matching", "creator_importance",
])
def test_matching_save_keeps_configuration_and_queue_invalidation_in_one_transaction(monkeypatch, feature):
    owner_id = uuid4()
    called = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: "owner")
    monkeypatch.setattr(admin, "connection", _connection)
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"k" * 32))
    monkeypatch.setattr(ai_feature_config, "save_feature_config", lambda conn, *a, **kw: called.append(
        ("config", conn, kw)))
    matching = getattr(admin, feature)
    monkeypatch.setattr(matching, "configuration_changed", lambda conn: called.append(
        ("queue", conn)))
    response = _client(lambda _: {"id": owner_id, "status": "active"}).post("/admin/ai", data={
        "csrf_token": "csrf", "feature": feature, "project_id": "proj_matching",
        "model": "gpt-5.6-luna", "enabled": "1", "rpm_limit": "20", "tpm_limit": "40000",
        "daily_token_budget": "500000", "api_key": "sk-test-only-1234",
        "auto_confirm_threshold": "0.970",
    }, follow_redirects=False)
    assert response.status_code == 303
    anchor = feature.replace("_", "-")
    assert response.headers["location"] == f"/admin/ai?saved={anchor}#{anchor}"
    assert [item[0] for item in called] == ["config", "queue"]
    assert called[0][1] is called[1][1]
    assert called[0][2]["enabled"] is True
    assert called[0][2]["auto_confirm_threshold"] == (
        "0.970" if feature == "creator_responsibility_matching" else "0.900"
    )
    assert not {"rpm_limit", "tpm_limit", "daily_token_budget"}.intersection(called[0][2])


def test_matching_routes_recheck_live_owner_and_csrf_before_queue_writes(pg, monkeypatch):
    owner, member = uuid4(), uuid4()
    for actor in (owner, member):
        pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
                   (actor, "ou_" + actor.hex))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (owner,))
    session = {"auth": {"id": owner, "status": "active"}}
    connections, contexts = [], []

    def connection():
        connections.append(pg)
        return nullcontext(pg)

    monkeypatch.setattr(matching_routes, "connection", connection)
    monkeypatch.setattr(matching_routes, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"k" * 32))
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                            autoescape=select_autoescape(["html"]))
    app = FastAPI()
    app.include_router(matching_routes.create_router(
        "handle",
        current_authorization=lambda _: session["auth"], csrf_token=lambda _: "csrf",
        render_template=lambda request, name, context, status_code=200: contexts.append(context) or HTMLResponse(
            templates.get_template(name).render(**context), status_code=status_code),
    ))
    client = TestClient(app)
    path = "/admin/ai/creator-handle-matching"
    response = client.get(path)
    assert response.status_code == 200 and contexts[-1]["matching"]["enabled"] is False
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert contexts[-1]["admin_permissions"] == admin_permissions.PAGE_KEYS
    before = len(connections)
    assert client.post(path + "/enqueue", data={"csrf_token": "wrong"}).status_code == 400
    session["auth"] = None
    assert client.get(path).status_code == 403
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}).status_code == 403
    assert len(connections) == before
    session["auth"] = {"id": member, "status": "active"}
    assert client.get(path).status_code == 403
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}).status_code == 403
    assert client.post(path + f"/jobs/{uuid4()}/cancel", data={"csrf_token": "csrf"}).status_code == 403
    session["auth"] = {"id": owner, "status": "active"}
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}).status_code == 400  # Disabled.
    assert pg.execute("SELECT count(*) n FROM creator_handle_jobs").fetchone()["n"] == 0
    contact, job = uuid4(), uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Synthetic creator')", (contact,))
    pg.execute("INSERT INTO creator_handle_jobs(id,contact_id) VALUES(%s,%s)", (job, contact))
    before = len(connections)
    assert client.post(path + f"/jobs/{job}/cancel", data={"csrf_token": "csrf", "page": "bad"}).status_code == 400
    assert client.post(path + f"/jobs/{job}/unknown", data={"csrf_token": "csrf", "page": "8"}).status_code == 400
    assert len(connections) == before
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job,)).fetchone()["status"] == "queued"
    response = client.post(path + f"/jobs/{job}/cancel", data={"csrf_token": "csrf", "page": "8"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == path + "?page=8"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job,)).fetchone()["status"] == "cancelled"
    pg.execute("UPDATE creator_handle_jobs SET status='failed',reason_code='request_unknown' WHERE id=%s", (job,))
    pg.execute("INSERT INTO creator_handle_requests(id,job_id,error_code) VALUES(%s,%s,'request_unknown')",
               (uuid4(), job))
    unknown = client.get(path)
    assert unknown.status_code == 200 and "失败：1" in unknown.text
    assert unknown.text.count("请求结果未知，需核实后重新检查") == 1
    assert "request_unknown" not in unknown.text
    assert contexts[-1]["matching"]["jobs"][0]["status"] == "failed"
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) VALUES(%s,'ai',%s)",
               (member, owner))
    session["auth"] = {"id": member, "status": "active"}
    readonly = client.get(path)
    assert readonly.status_code == 200 and "请求结果未知，需核实后重新检查" in readonly.text
    assert '<form method="post"' not in readonly.text
    assert contexts[-1]["admin_role"] == "viewer" and contexts[-1]["admin_permissions"] == frozenset({"ai"})
    assert contexts[-1]["admin_landing_path"] == "/admin/ai"
    assert client.post(path + f"/jobs/{job}/cancel", data={"csrf_token": "csrf"}).status_code == 403
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (member,))
    assert client.get(path).status_code == 403
    session["auth"] = {"id": owner, "status": "active"}
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) VALUES(%s,'ai',%s)",
               (owner, owner))
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (owner,))
    assert client.get(path).status_code == 403  # Session does not retain an obsolete Owner grant.
    assert client.post(path + f"/jobs/{job}/cancel", data={"csrf_token": "csrf", "page": "8"}).status_code == 403
    assert pg.execute("SELECT count(*) n FROM creator_handle_jobs").fetchone()["n"] == 1
