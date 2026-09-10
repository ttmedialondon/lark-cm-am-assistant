"""The initial-scan retry URL must retain session, CSRF and CM-scope boundaries."""

from uuid import uuid4

import pytest
import test_creator_onboarding as onboarding_fixtures
import test_creator_workflow_http_import as http_fixtures

from app import creator_onboarding as onboarding
from app import creator_workflow as workflow
from app import creator_workflow_routes as routes

client = http_fixtures.client
pg = onboarding_fixtures.pg
real_scan = onboarding_fixtures.real_scan
initial = onboarding_fixtures.initial
URL = "/dashboard/creators/ai/initial-retry"


def test_initial_retry_http_route_uses_authorized_query_scope_not_body_or_candidate_route(client, monkeypatch):
    api, actor, scope = client
    target, claimed = uuid4(), uuid4()
    calls, scope_calls = [], []

    def get_scope(actor_id, responsible_id):
        scope_calls.append((actor_id, responsible_id))
        return {**scope, "responsible_id": str(responsible_id)}

    monkeypatch.setattr(workflow, "get_scope", get_scope)
    monkeypatch.setattr(
        routes, "retry_initial_scan", lambda actor_id, cm_id: calls.append((actor_id, cm_id)) or {"status": "scanning"}
    )
    # No candidate UUID or version is sent: a shadowing /{candidate_id}/{action}
    # route would fail instead of dispatching this dedicated service.
    response = api.post(
        f"{URL}?responsible_id={target}",
        headers={"X-CSRF-Token": "csrf", "Origin": "http://testserver"},
        json={"request_id": str(uuid4()), "responsible_id": str(claimed)},
    )
    assert response.status_code == 200 and response.json() == {"onboarding": {"status": "scanning"}}
    assert scope_calls == [(actor["id"], target)] and calls == [(actor["id"], str(target))]
    assert response.headers["cache-control"].startswith("no-store")


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"},
                                     {"X-CSRF-Token": "csrf", "Origin": "https://evil.invalid"}])
def test_initial_retry_http_rejects_csrf_or_cross_origin_before_service(client, monkeypatch, headers):
    api, _, _ = client
    monkeypatch.setattr(routes, "retry_initial_scan", lambda *_: pytest.fail("must not retry"))
    assert api.post(URL, headers=headers, json={"request_id": str(uuid4())}).status_code == 403


@pytest.mark.parametrize("request_id", [None, "not-a-uuid", 123])
def test_initial_retry_http_requires_valid_request_id(client, monkeypatch, request_id):
    api, _, _ = client
    monkeypatch.setattr(routes, "retry_initial_scan", lambda *_: pytest.fail("must not retry"))
    assert api.post(URL, headers={"X-CSRF-Token": "csrf"}, json={"request_id": request_id}).status_code == 409


def test_initial_retry_http_requires_selected_cm_and_returns_service_permission_failure(client, monkeypatch):
    api, _, scope = client
    payload, headers = {"request_id": str(uuid4())}, {"X-CSRF-Token": "csrf"}
    monkeypatch.setattr(routes, "retry_initial_scan", lambda *_: pytest.fail("must not retry without CM"))
    scope["responsible_id"] = None
    assert api.post(URL, headers=headers, json=payload).status_code == 409
    scope["responsible_id"] = str(uuid4())

    def forbidden(*_):
        raise PermissionError("只能扫描本人范围")

    monkeypatch.setattr(routes, "retry_initial_scan", forbidden)
    assert api.post(URL, headers=headers, json=payload).status_code == 403


def test_retry_service_rejects_non_owner_cross_cm_without_changing_onboarding(pg, initial):
    ids, _ = initial  # Includes the required onboarding.connection patch to this isolated database.
    outsider = uuid4()
    pg.execute(
        """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
        assignment_eligible,body_consent_at) VALUES(%s,'ou_outsider','Other CM','test',TRUE,NOW())""",
        (outsider,),
    )
    pg.execute("UPDATE creator_workflow_onboarding SET status='failed' WHERE authorization_id=%s", (ids["cm"],))
    before = onboarding.get_status(pg, ids["cm"])
    with pytest.raises(PermissionError, match="跨 CM 操作仅限系统主人"):
        onboarding.retry_initial_scan(outsider, ids["cm"])
    assert onboarding.get_status(pg, ids["cm"]) == before
    assert not pg.execute("SELECT 1 FROM creator_workflow_ai_tasks").fetchone()
