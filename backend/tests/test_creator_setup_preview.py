"""A named-account, synthetic-only preview must not become a new data capability."""

import json
import re
from collections import Counter
from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_permissions, admin_reporting, contact_assignments, creator_review, main

PATH = "/dashboard/creator-setup-preview"
PREVIEW_ACTOR = UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0")
SHANG_MA_ACTOR = UUID("1225b4a0-fbd8-431e-962e-25e66943a8a0")
OTHER_ACTOR = UUID("00000000-0000-4000-8000-000000000099")


def forbidden_data(*_args, **_kwargs):
    pytest.fail("The synthetic preview reached a real business query, write, or message reader")


def preview_client(monkeypatch, *, actor_id=PREVIEW_ACTOR, status="active", owner=True,
                   name="Shuxuan Li", authenticated=True):
    authorization = {"id": actor_id, "status": status, "display_name": name,
                     "body_consent_at": None, "p2p_discovery_status": "succeeded"}
    state = {"authorization": authorization if authenticated else None, "owner": owner}
    reads = []
    monkeypatch.setattr(main, "_current_authorization", lambda _request: state["authorization"])

    def access_for(actual_id):
        reads.append(actual_id)
        assert actual_id == actor_id
        return {"is_owner": state["owner"], "has_admin_access": state["owner"],
                "permissions": frozenset(), "landing_path": "/admin" if state["owner"] else None}

    monkeypatch.setattr(admin_permissions, "access_for", access_for)
    for module in (main, admin_permissions, admin_reporting, contact_assignments, creator_review):
        monkeypatch.setattr(module, "connection", forbidden_data)
    monkeypatch.setattr(main, "_dashboard_stats", forbidden_data)
    monkeypatch.setattr(contact_assignments, "creator_workbench", forbidden_data)
    monkeypatch.setattr(contact_assignments, "manually_assign", forbidden_data)
    monkeypatch.setattr(contact_assignments, "confirm_suggested_assignment", forbidden_data)
    monkeypatch.setattr(admin_reporting, "message_page", forbidden_data)
    monkeypatch.setattr(creator_review, "messages", forbidden_data)
    return TestClient(main.app), state, reads


@pytest.mark.parametrize(("actor_id", "status", "owner", "expected"), [
    (PREVIEW_ACTOR, "active", True, True),
    (SHANG_MA_ACTOR, "active", False, True),
    (SHANG_MA_ACTOR, "active", True, True),
    (str(SHANG_MA_ACTOR), "active", False, True),
    (SHANG_MA_ACTOR, "revoked", False, False),
    (SHANG_MA_ACTOR, "attention_required", False, False),
    (OTHER_ACTOR, "active", True, False),
    (PREVIEW_ACTOR, "active", False, False),
    (PREVIEW_ACTOR, "revoked", True, False),
    (PREVIEW_ACTOR, "attention_required", True, False),
    (None, "active", True, False),
])
def test_access_helper_requires_exact_active_account_and_owner_only_for_shuxuan(actor_id, status, owner, expected):
    assert main.CREATOR_SETUP_PREVIEW_AUTHORIZATION_ID == PREVIEW_ACTOR
    assert main.CREATOR_SETUP_PREVIEW_SHANG_MA_ID == SHANG_MA_ACTOR
    authorization = {"id": actor_id, "status": status, "display_name": "Shuxuan Li"}
    assert main._can_preview_creator_setup(authorization, {"is_owner": owner}) is expected


@pytest.mark.parametrize("options", [
    {"actor_id": OTHER_ACTOR, "owner": True},
    {"actor_id": OTHER_ACTOR, "owner": False},
    {"owner": False}, {"status": "revoked"}, {"status": "attention_required"},
    {"actor_id": SHANG_MA_ACTOR, "owner": False, "status": "revoked"},
    {"actor_id": SHANG_MA_ACTOR, "owner": False, "status": "attention_required"},
])
def test_denied_accounts_cannot_discover_preview_or_load_business_data(monkeypatch, options):
    client, _, reads = preview_client(monkeypatch, **options)
    monkeypatch.setattr(main.creator_setup_preview, "data", forbidden_data)
    response = client.get(f"{PATH}?authorization_id={PREVIEW_ACTOR}&owner=1", follow_redirects=False)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "creator-setup-data" not in response.text
    if options.get("actor_id", PREVIEW_ACTOR) != PREVIEW_ACTOR or options.get("status", "active") != "active":
        assert reads == []


def test_signed_out_preview_redirects_without_even_loading_acl(monkeypatch):
    client, _, reads = preview_client(monkeypatch, authenticated=False)
    monkeypatch.setattr(main.creator_setup_preview, "data", forbidden_data)
    response = client.get(PATH, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert reads == []


@pytest.mark.parametrize(("actor_id", "owner", "name"), [
    (PREVIEW_ACTOR, True, "Shuxuan Li"),
    (SHANG_MA_ACTOR, False, "Shang Ma"),
])
def test_preview_access_is_not_a_display_name_check(monkeypatch, actor_id, owner, name):
    client, _, _ = preview_client(monkeypatch, actor_id=actor_id, owner=owner, name="Renamed account")
    assert client.get(PATH).status_code == 200
    client, _, _ = preview_client(monkeypatch, actor_id=OTHER_ACTOR, name=name)
    assert client.get(PATH).status_code == 404


@pytest.mark.parametrize(("actor_id", "owner", "expected_acl_reads"), [
    (PREVIEW_ACTOR, True, [PREVIEW_ACTOR]),
    (SHANG_MA_ACTOR, False, []),
])
def test_allowed_get_uses_only_synthetic_data_and_is_not_cached(monkeypatch, actor_id, owner, expected_acl_reads):
    client, _, reads = preview_client(monkeypatch, actor_id=actor_id, owner=owner)
    response = client.get(PATH)
    assert response.status_code == 200
    assert 'id="creator-setup-data"' in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    csp = response.headers["content-security-policy"]
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert reads == expected_acl_reads
    assert re.search(r'method\s*=\s*["\']post["\']', response.text, re.IGNORECASE) is None
    assert not re.search(r"\b(?:oc|ou|on|cli)_[A-Za-z0-9]+\b", response.text)


@pytest.mark.parametrize(("actor_id", "owner"), [(PREVIEW_ACTOR, True), (SHANG_MA_ACTOR, False)])
def test_revocation_applies_on_next_request_not_only_initial_page_load(monkeypatch, actor_id, owner):
    client, state, _ = preview_client(monkeypatch, actor_id=actor_id, owner=owner)
    assert client.get(PATH).status_code == 200
    if actor_id == PREVIEW_ACTOR:
        state["owner"] = False
        assert client.get(PATH).status_code == 404
        state["owner"] = True
    state["authorization"]["status"] = "revoked"
    assert client.get(PATH).status_code == 404


@pytest.mark.parametrize(("actor_id", "owner"), [(PREVIEW_ACTOR, True), (SHANG_MA_ACTOR, False)])
def test_preview_has_no_mutating_endpoint_even_for_the_allowed_account(monkeypatch, actor_id, owner):
    client, _, reads = preview_client(monkeypatch, actor_id=actor_id, owner=owner)
    response = client.post(PATH, json={"handle": "live.handle", "confirm": True, "cm": str(actor_id)})
    assert response.status_code == 405
    assert reads == []
    assert all(route.methods == {"GET"} for route in main.app.routes if route.path.startswith(PATH))


@pytest.mark.parametrize(("options", "visible"), [
    ({}, True), ({"actor_id": OTHER_ACTOR}, False), ({"owner": False}, False),
    ({"status": "revoked"}, False), ({"name": "Renamed account"}, True),
    ({"actor_id": SHANG_MA_ACTOR, "owner": False, "name": "Shang Ma"}, True),
    ({"actor_id": SHANG_MA_ACTOR, "owner": False, "name": "Renamed account"}, True),
    ({"actor_id": SHANG_MA_ACTOR, "owner": False, "status": "revoked"}, False),
    ({"actor_id": SHANG_MA_ACTOR, "owner": False, "status": "attention_required"}, False),
    ({"actor_id": OTHER_ACTOR, "name": "Shang Ma"}, False),
])
def test_dashboard_preview_entry_uses_the_same_server_side_boundary(monkeypatch, options, visible):
    client, _, _ = preview_client(monkeypatch, **options)
    monkeypatch.setattr(main, "_dashboard_stats", lambda _actor_id: {
        "badge": "success", "status": "已同步", "detail": "Synthetic summary", "last_synced_at": "—",
        "job": {"status": "succeeded"},
    })
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _authorization: "csrf")
    monkeypatch.setattr(contact_assignments, "assignment_available", lambda: False)
    monkeypatch.setattr(contact_assignments, "creator_workbench", lambda _actor_id: {
        "pending_candidates": [], "confirmed_creators": [],
    })
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert (f'href="{PATH}"' in response.text) is visible
    assert "我的达人（正式）" in response.text
    if options.get("actor_id") == SHANG_MA_ACTOR:
        assert 'href="/admin"' not in response.text


def test_preview_dataset_contains_no_real_provider_or_database_identifiers():
    data = main.creator_setup_preview.data()
    serialized = json.dumps(data, ensure_ascii=False)
    assert data
    assert not re.search(r"\b(?:oc|ou|on|cli)_[A-Za-z0-9]+\b", serialized)
    assert not re.search(r"\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b", serialized, re.IGNORECASE)
    assert "body_ciphertext" not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized


def test_inline_demo_data_cannot_break_out_of_its_json_script(monkeypatch):
    client, _, _ = preview_client(monkeypatch)
    data = deepcopy(main.creator_setup_preview.data())
    injection = "</script><script>alert('proto-xss')</script>"
    data["accounts"][0]["note"] = injection
    monkeypatch.setattr(main.creator_setup_preview, "data", lambda: data)
    response = client.get(PATH)
    assert response.status_code == 200
    assert injection not in response.text
    assert "<script>alert('proto-xss')</script>" not in response.text
    assert "\\u003c/script\\u003e" in response.text or "&lt;/script&gt;" in response.text


def test_demo_accounts_have_consistent_conversation_references_and_safety_examples():
    data = main.creator_setup_preview.data()
    accounts, conversations = data["accounts"], data["conversations"]
    assert data["version"] == 1
    assert data["storage_key"] == "ttmedia.creator-setup-preview.v1"
    assert len(accounts) == 12
    account_ids = [account["id"] for account in accounts]
    handles = [account["handle"] for account in accounts]
    conversation_ids = {conversation["id"] for conversation in conversations}
    assert len(set(account_ids)) == len(account_ids)
    assert len(set(handles)) == len(handles)
    assert len(conversation_ids) == len(conversations)
    assert all(value.startswith("demo_") for value in [*account_ids, *handles, *conversation_ids])
    for account in accounts:
        assert account["decision"] in {"pending", "mine", "defer", "not_mine"}
        assert set(account["conversation_ids"]) <= conversation_ids
        assert set(account["selected_conversations"]) <= set(account["conversation_ids"])
        if account["owner_conflict"]:
            assert account["decision"] != "mine"
    assert any(account["owner_conflict"] for account in accounts)
    assert any(not account["conversation_ids"] for account in accounts)
    use_counts = Counter(cid for account in accounts for cid in account["conversation_ids"])
    assert any(count > 1 for count in use_counts.values())
    assert all(conversation["messages"] for conversation in conversations)
