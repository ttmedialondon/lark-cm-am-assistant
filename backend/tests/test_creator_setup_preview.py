"""Named-user, Remi-scoped live review; no business writes or eager message reads."""

from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_permissions, contact_assignments, creator_review, main

PATH = "/dashboard/creator-setup-preview"
OWNER = UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0")
SHANG = UUID("1225b4a0-fbd8-431e-962e-25e66943a8a0")
OTHER = UUID("00000000-0000-4000-8000-000000000099")
CONTACT = UUID("00000000-0000-4000-8000-000000000011")
CHAT = "oc_test12345678"


def forbidden(*_args, **_kwargs):
    pytest.fail("Unexpected business query/write or eager conversation/message read")


def roster():
    account = {
        "handle": "verified.handle", "display_name": "Test contact", "source_rows": [8],
        "cm_values": ["Remi"], "tap_cm_values": [], "source_group_names": ["Source group"],
        "category": "documented", "category_label": "依据已归档", "issue": "none",
        "issue_label": "来源可追溯", "reason": "Test source", "next_step": "核对会话",
        "is_stale": False, "contact_id": str(CONTACT), "suggested_contact_id": None,
        "can_view_conversations": True, "mapping_label": "现有正式映射",
    }
    pending = {**account, "handle": "pending.handle", "contact_id": None,
               "display_name": "尚未定位", "mapping_label": "联系人尚未确定",
               "category": "decision_needed", "category_label": "待你判断",
               "issue": "unresolved", "issue_label": "联系人尚未确定", "can_view_conversations": False}
    return {
        "available": True, "responsible_name": "Remi · Quanzhen Liang",
        "responsible_id": str(main.creator_setup_preview.REMI_ID), "source_filename": "source.xlsx",
        "prepared_at_label": "2026-09-07", "summary": {
            "total": 2, "documented": 1, "archive_needed": 0, "decision_needed": 1,
            "stale": 0, "formal_people": 1, "formal_handles": 1, "unresolved": 1,
            "alias_missing": 0, "identity_risk": 0, "unlisted_formal_handles": 0,
        }, "accounts": [account, pending],
    }


def client_for(monkeypatch, actor=OWNER, *, status="active", owner=True, review=True,
               name="Viewer", authenticated=True, raise_errors=True):
    auth = {"id": actor, "status": status, "display_name": name,
            "body_consent_at": None, "p2p_discovery_status": "succeeded"}
    state = {"auth": auth if authenticated else None, "owner": owner, "review": review}
    calls = []
    monkeypatch.setattr(main, "_current_authorization", lambda _: state["auth"])

    def access_for(actor_id):
        calls.append(actor_id)
        assert actor_id == actor
        return {"is_owner": state["owner"], "has_admin_access": state["owner"] or state["review"],
                "permissions": {"creator-review"} if state["review"] else set(),
                "landing_path": "/admin" if state["owner"] else "/admin/creator-review"}

    monkeypatch.setattr(admin_permissions, "access_for", access_for)
    monkeypatch.setattr(main.creator_setup_preview, "data", roster)
    for module in (main, admin_permissions, contact_assignments, creator_review):
        monkeypatch.setattr(module, "connection", forbidden)
    for field in ("detail", "messages", "record_view"):
        monkeypatch.setattr(creator_review, field, forbidden)
    monkeypatch.setattr(contact_assignments, "manually_assign", forbidden)
    monkeypatch.setattr(contact_assignments, "confirm_suggested_assignment", forbidden)
    return TestClient(main.app, raise_server_exceptions=raise_errors), state, calls


@pytest.mark.parametrize("options", [
    {"actor": OTHER}, {"actor": OTHER, "name": "Shang Ma"}, {"actor": OTHER, "name": "Shuxuan Li"},
    {"owner": False}, {"status": "revoked"}, {"status": "attention_required"},
    {"actor": SHANG, "owner": False, "review": False}, {"actor": SHANG, "status": "revoked"},
])
def test_denied_before_any_roster_read(monkeypatch, options):
    client, _, _ = client_for(monkeypatch, **options)
    monkeypatch.setattr(main.creator_setup_preview, "data", forbidden)
    response = client.get(f"{PATH}?owner=1&authorization_id={OWNER}")
    assert response.status_code == 404
    assert "no-store" in response.headers["cache-control"]


def test_signed_out_redirect_has_no_acl_or_data_read(monkeypatch):
    client, _, calls = client_for(monkeypatch, authenticated=False)
    monkeypatch.setattr(main.creator_setup_preview, "data", forbidden)
    response = client.get(PATH, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert calls == []


def test_missing_real_snapshot_shows_gap_without_demo_fallback(monkeypatch):
    client, _, _ = client_for(monkeypatch)
    data = roster()
    data.update(available=False, accounts=[])
    monkeypatch.setattr(main.creator_setup_preview, "data", lambda: data)
    response = client.get(PATH)
    assert response.status_code == 200 and "真实来源数据暂不可用" in response.text
    assert "verified.handle" not in response.text and "demo_" not in response.text


def test_source_labels_are_escaped_and_never_become_executable_content(monkeypatch):
    client, _, _ = client_for(monkeypatch)
    data = roster()
    data["accounts"][0]["source_group_names"] = ["<script>untrusted-source</script>"]
    monkeypatch.setattr(main.creator_setup_preview, "data", lambda: data)
    response = client.get(PATH)
    assert "<script>untrusted-source</script>" not in response.text
    assert "&lt;script&gt;untrusted-source&lt;/script&gt;" in response.text


@pytest.mark.parametrize(("actor", "owner"), [(OWNER, True), (SHANG, False)])
def test_live_roster_is_named_user_scoped_read_only_and_uncached(monkeypatch, actor, owner):
    client, _, calls = client_for(monkeypatch, actor, owner=owner, name="Renamed viewer")
    response = client.get(f"{PATH}?responsible_id={OTHER}")
    assert response.status_code == 200
    assert "verified.handle" in response.text and "pending.handle" in response.text
    assert "Remi" in response.text and "Renamed viewer" in response.text
    assert "纯假数据" not in response.text and "creator-setup-data" not in response.text
    assert "sessionStorage" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    for rule in ("connect-src 'none'", "form-action 'none'", "frame-ancestors 'none'"):
        assert rule in response.headers["content-security-policy"]
    assert calls == [actor]
    assert client.post(PATH, json={"confirm": True}).status_code == 405


@pytest.mark.parametrize(("actor", "owner"), [(OWNER, True), (SHANG, False)])
def test_live_viewer_revocation_is_checked_on_next_request(monkeypatch, actor, owner):
    client, state, _ = client_for(monkeypatch, actor, owner=owner)
    assert client.get(PATH).status_code == 200
    state["owner"] = False
    state["review"] = False
    assert client.get(PATH).status_code == 404
    state["owner"] = owner
    state["review"] = True
    state["auth"]["status"] = "revoked"
    assert client.get(PATH).status_code == 404


@pytest.mark.parametrize("query", ["handle=unknown", "handle=pending.handle", f"chat_id={CHAT}"])
def test_unresolved_unknown_or_unbound_chat_never_reaches_readers(monkeypatch, query):
    client, _, _ = client_for(monkeypatch)
    assert client.get(f"{PATH}?{query}").status_code == 404


@pytest.mark.parametrize("issue", ["alias_missing", "stale"])
def test_candidate_or_stale_handle_does_not_authorize_chat_access(monkeypatch, issue):
    client, _, _ = client_for(monkeypatch)
    data = roster()
    data["accounts"][0].update(can_view_conversations=False, issue=issue)
    monkeypatch.setattr(main.creator_setup_preview, "data", lambda: deepcopy(data))
    assert client.get(f"{PATH}?handle=verified.handle&chat_id={CHAT}").status_code == 404


def scoped_detail(monkeypatch):
    def detail(contact_id, *, responsible_id):
        assert contact_id == CONTACT and responsible_id == main.creator_setup_preview.REMI_ID
        return {"creator": {"display_name": "Test contact", "responsible_name": "Quanzhen Liang"},
                "conversation_notice": "Current authorized sources only", "conversations": [{
                    "chat_id": CHAT, "name": "Test conversation", "chat_mode_label": "群聊",
                    "context_label": "联系人相关候选", "message_count": 31,
                    "last_message_at_label": "2026-09-08", "source_label": "Remi",
                }]}
    monkeypatch.setattr(creator_review, "detail", detail)


def test_contact_detail_is_lazy_and_unlisted_chat_denied(monkeypatch):
    client, _, _ = client_for(monkeypatch)
    scoped_detail(monkeypatch)
    assert client.get(f"{PATH}?handle=verified.handle").status_code == 200
    assert client.get(f"{PATH}?handle=verified.handle&chat_id=oc_other123456").status_code == 404


def test_changed_assignment_denies_detail(monkeypatch):
    client, _, _ = client_for(monkeypatch)
    monkeypatch.setattr(creator_review, "detail", lambda *_a, **_kw: None)
    assert client.get(f"{PATH}?handle=verified.handle&chat_id={CHAT}").status_code == 404


@pytest.mark.parametrize("audit_fails", [False, True])
def test_explicit_message_read_is_scoped_audited_and_escaped(monkeypatch, audit_fails):
    client, _, _ = client_for(monkeypatch, SHANG, owner=False, raise_errors=False)
    scoped_detail(monkeypatch)
    events = []

    def messages(contact_id, chat_id, page, *, responsible_id):
        assert (contact_id, chat_id, page, responsible_id) == (CONTACT, CHAT, 2, main.creator_setup_preview.REMI_ID)
        events.append("read")
        return {"page": 2, "has_next": True, "total_count": 65, "total_pages": 3, "items": [{
            "sender_label": "Sender", "sender_role_label": "达人", "message_type_label": "文本",
            "created_at_label": "2026-09-08", "sent_by_author": False,
            "content": "<script>private-test-body</script>",
        }]}

    def audit(viewer, contact_id, chat_id):
        assert (viewer, contact_id, chat_id) == (SHANG, CONTACT, CHAT)
        events.append("audit")
        if audit_fails:
            raise RuntimeError("Audit unavailable")

    monkeypatch.setattr(creator_review, "messages", messages)
    monkeypatch.setattr(creator_review, "record_view", audit)
    response = client.get(f"{PATH}?handle=verified.handle&chat_id={CHAT}&message_page=2&contact_id={OTHER}")
    assert events == ["read", "audit"]
    if audit_fails:
        assert response.status_code == 500 and "private-test-body" not in response.text
    else:
        assert response.status_code == 200 and "&lt;script&gt;private-test-body&lt;/script&gt;" in response.text
        assert "message_page=1" in response.text and "message_page=3" in response.text


def test_revoked_chat_scope_does_not_generate_audit(monkeypatch):
    client, _, _ = client_for(monkeypatch)
    scoped_detail(monkeypatch)
    monkeypatch.setattr(creator_review, "messages", lambda *_a, **_kw: None)
    assert client.get(f"{PATH}?handle=verified.handle&chat_id={CHAT}").status_code == 404


@pytest.mark.parametrize(("actor", "owner", "review", "visible"), [
    (OWNER, True, True, True), (SHANG, False, True, True), (SHANG, False, False, False),
    (OTHER, True, True, False), (OWNER, False, True, False),
])
def test_dashboard_entry_matches_live_access_without_loading_roster(monkeypatch, actor, owner, review, visible):
    client, _, _ = client_for(monkeypatch, actor, owner=owner, review=review)
    monkeypatch.setattr(main.creator_setup_preview, "data", forbidden)
    monkeypatch.setattr(main, "_dashboard_stats", lambda _: {
        "badge": "success", "status": "已同步", "detail": "Summary", "last_synced_at": "—",
        "job": {"status": "succeeded"},
    })
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _: "csrf")
    monkeypatch.setattr(contact_assignments, "assignment_available", lambda: False)
    monkeypatch.setattr(contact_assignments, "creator_workbench", lambda _: {
        "pending_candidates": [], "confirmed_creators": [],
    })
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert (f'href="{PATH}"' in response.text) is visible
    assert "我的达人（正式）" in response.text
