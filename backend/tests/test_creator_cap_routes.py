"""Bounded AI preparation, start and preview retain session/CSRF/CM boundaries."""

from uuid import uuid4

import pytest
import test_creator_workflow_http_import as http_fixtures

from app import creator_workflow as workflow
from app import creator_workflow_routes as routes

client = http_fixtures.client
BASE = "/dashboard/creators/ai"


def test_targeted_routes_use_query_scope_and_frozen_preflight_id(client, monkeypatch):
    api, actor, scope = client
    target, claimed, preflight_id = uuid4(), uuid4(), uuid4()
    calls = []
    monkeypatch.setattr(workflow, "get_scope", lambda _, cm: {**scope, "responsible_id": str(cm)})
    for name in ("preflight_scan", "queue_scan", "pause_scan", "preflight_details"):
        monkeypatch.setattr(routes.ai, name, lambda *args, _name=name, **kwargs:
                            calls.append((_name, args, kwargs)) or {"targeted": True}, raising=False)
    payload = {"request_id": str(uuid4()), "responsible_id": str(claimed),
               "preflight_id": str(preflight_id), "limit": 999}
    for action in ("preflight", "scan", "pause"):
        response = api.post(f"{BASE}/{action}?responsible_id={target}",
                            headers={"X-CSRF-Token": "csrf", "Origin": "http://testserver"}, json=payload)
        assert response.status_code == 200 and response.json() == {"targeted": True}
        assert response.headers["cache-control"].startswith("no-store")
    response = api.get(f"{BASE}/preflight/{preflight_id}?responsible_id={target}")
    assert response.status_code == 200 and response.headers["cache-control"].startswith("no-store")
    assert calls == [
        ("preflight_scan", (actor["id"], str(target)), {"limit": 30}),
        ("queue_scan", (actor["id"], str(target)), {"preflight_id": preflight_id, "retry_failed": False}),
        ("pause_scan", (actor["id"], str(target)), {}),
        ("preflight_details", (actor["id"], str(target), preflight_id), {}),
    ]


def test_targeted_writes_reject_csrf_origin_and_invalid_ids_before_service(client, monkeypatch):
    api, _, _ = client
    for name in ("preflight_scan", "queue_scan", "pause_scan"):
        monkeypatch.setattr(routes.ai, name, lambda *_args, **_kwargs: pytest.fail("must not execute"), raising=False)
    for action in ("preflight", "scan", "pause"):
        for headers in ({}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "csrf", "Origin": "https://evil.invalid"}):
            assert api.post(f"{BASE}/{action}", headers=headers, json={"request_id": str(uuid4())}).status_code == 403
        for request_id in (None, "not-a-uuid", 123):
            assert api.post(f"{BASE}/{action}", headers={"X-CSRF-Token": "csrf"},
                            json={"request_id": request_id}).status_code == 409
    assert api.post(f"{BASE}/scan", headers={"X-CSRF-Token": "csrf"},
                    json={"request_id": str(uuid4())}).status_code == 409


def test_targeted_routes_require_selected_cm_and_preserve_service_permission_failure(client, monkeypatch):
    api, _, scope = client
    def forbidden(*_args, **_kwargs):
        raise PermissionError("只能核对本人范围")

    for name in ("preflight_scan", "queue_scan", "pause_scan", "preflight_details"):
        monkeypatch.setattr(routes.ai, name, forbidden, raising=False)
    payload = {"request_id": str(uuid4()), "preflight_id": str(uuid4())}
    for cm, code in ((None, 409), (str(uuid4()), 403)):
        scope["responsible_id"] = cm
        for action in ("preflight", "scan", "pause"):
            assert api.post(f"{BASE}/{action}", headers={"X-CSRF-Token": "csrf"}, json=payload).status_code == code
        assert api.get(f"{BASE}/preflight/{uuid4()}").status_code == code


def test_old_history_retry_endpoint_cannot_restart_work(client, monkeypatch):
    api, _, _ = client
    monkeypatch.setattr(routes.ai, "queue_scan", lambda *_args, **_kwargs: pytest.fail("legacy must not start"))
    assert api.post(f"{BASE}/initial-retry", headers={"X-CSRF-Token": "csrf"},
                    json={"request_id": str(uuid4())}).status_code in (404, 422)
