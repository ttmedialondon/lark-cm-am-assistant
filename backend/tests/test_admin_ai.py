from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_reporting, ai_feature_config


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
        "feature": "contact_assessment",
        "provider": "openai",
        "provider_label": "OpenAI",
        "project_id": "proj_contact_assessment",
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

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get("/admin/ai")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert contexts[-1][0] == "admin/ai.html"
    assert contexts[-1][1]["config"]["api_key_label"] == "已配置 · …abcd"
    assert "api_key_ciphertext" not in contexts[-1][1]["config"]


def test_owner_post_passes_key_to_encrypted_store_without_rendering_it(monkeypatch):
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

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        "/admin/ai",
        data={
            "csrf_token": "csrf",
            "project_id": "proj_contact_assessment",
            "model": "gpt-5.6-luna",
            "enabled": "1",
            "auto_confirm_threshold": "0.90",
            "api_key": "sk-secret-never-rendered",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ai"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert len(saved) == 1
    args, kwargs = saved[0]
    assert args[1:] == (("cipher", b"k" * 32), "contact_assessment")
    assert kwargs == {
        "provider": "openai",
        "project_id": "proj_contact_assessment",
        "model": "gpt-5.6-luna",
        "enabled": True,
        "api_key": "sk-secret-never-rendered",
        "updated_by_authorization_id": owner_id,
        "auto_confirm_threshold": "0.90",
    }
    audit = connections[0].calls[-1][1]
    assert audit[2] == "ai_feature_config_updated"
    assert "sk-secret-never-rendered" not in audit[3]
    assert '"key_rotated": true' in audit[3]
