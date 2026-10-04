from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_permissions, admin_reporting, ai_feature_config
from app import creator_handle_matching_routes as matching_routes


@pytest.fixture(autouse=True)
def legacy_role_scenarios_use_explicit_acl(monkeypatch):
    monkeypatch.setattr(admin_permissions, "access_for", lambda authorization_id: admin_permissions._access({
        "status": "active", "role": admin_reporting.admin_role_for(authorization_id),
        "role_active": True, "permissions": [],
    }))


class _Connection:
    def __init__(self):
        self.calls = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params):
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


def test_ai_config_is_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/ai")

    assert response.status_code == 403


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
    assert contexts[-1][1]["config"]["api_key_label"] == "已配置 · …abcd"
    assert "api_key_ciphertext" not in contexts[-1][1]["config"]
    assert contexts[-1][1]["ai_dispatch"]["state"] == "paused"
    assert contexts[-1][1]["summary_settings"]["status_label"] == "未开启"
    assert [item["feature"] for item in contexts[-1][1]["configs"]] == [
        "creator_import", "creator_handle_matching", "creator_responsibility_matching",
        "creator_group_classification",
        "offhours_summary", "desktop_reply",
    ]
    for feature in ("contact_assessment", "contact_assignment", "creator_workflow"):
        response = browser.post("/admin/ai", data={"csrf_token": "csrf", "feature": feature, "enabled": "1"})
        assert response.status_code == 400
        assert "未知的 AI 功能配置" in contexts[-1][1]["form_error"]


@pytest.mark.parametrize("enabled", ["1", ""])
def test_owner_post_passes_key_to_encrypted_store_without_rendering_it(monkeypatch, enabled):
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
    monkeypatch.setattr(ai_feature_config, "save_feature_config", lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(admin.offhours_summary, "save_settings_and_enqueue", lambda *args: None)

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        "/admin/ai",
        data={
            "csrf_token": "csrf",
            "feature": "offhours_summary",
            "project_id": "proj_summary",
            "model": "gpt-5.6-luna",
            "enabled": enabled,
            "auto_confirm_threshold": "0.900",
            "api_key": "sk-secret-never-rendered",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ai?saved=offhours-summary#offhours-summary"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert len(saved) == 1
    args, kwargs = saved[0]
    assert args[1:] == (("cipher", b"k" * 32), "offhours_summary")
    assert kwargs == {
        "provider": "openai",
        "project_id": "proj_summary",
        "model": "gpt-5.6-luna",
        "enabled": enabled == "1",
        "api_key": "sk-secret-never-rendered",
        "updated_by_authorization_id": owner_id,
        "auto_confirm_threshold": "0.900",
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


@pytest.mark.parametrize("feature", ["creator_handle_matching", "creator_responsibility_matching"])
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
    app = FastAPI()
    app.include_router(matching_routes.create_router(
        current_authorization=lambda _: session["auth"], csrf_token=lambda _: "csrf",
        render_template=lambda request, name, context: contexts.append(context) or PlainTextResponse("rendered"),
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
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (owner,))
    assert client.get(path).status_code == 403  # Session does not retain an obsolete Owner grant.
    assert pg.execute("SELECT count(*) n FROM creator_handle_jobs").fetchone()["n"] == 0
