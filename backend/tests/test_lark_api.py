from contextlib import contextmanager

import httpx
import pytest

from app.config import Settings
from app.lark_api import LarkApiError, LarkClient


def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_redirect_uri="https://larkbot.ttapp.help/auth/lark/callback",
        token_encryption_key=b"a" * 32,
        oauth_authorize_url="https://accounts.larksuite.com/open-apis/authen/v1/authorize",
        oauth_token_url="https://open.larksuite.com/open-apis/authen/v2/oauth/token",
        user_info_url="https://open.larksuite.com/open-apis/authen/v1/user_info",
        lark_api_base_url="https://open.larksuite.com",
        oauth_scopes=("offline_access", "im:chat:read"),
        sync_lookback_days=0,
        sync_interval_seconds=180,
        sync_overlap_seconds=600,
        member_count_refresh_seconds=86400,
        worker_idle_seconds=3,
    )


def test_authorization_url_uses_oauth_client_id_and_pkce() -> None:
    url = LarkClient(settings()).authorization_url("state value", "challenge-value")

    assert "client_id=cli_test" in url
    assert "app_id=" not in url
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "scope=offline_access+im%3Achat%3Aread" in url


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"name": "Quanzhen Liang", "en_name": "Remi"}, "Remi（Quanzhen Liang）"),
        ({"name": "Shang Ma", "en_name": "Shang Ma"}, "Shang Ma"),
        ({"name": " Shuxuan Li ", "en_name": "shuxuan li"}, "shuxuan li"),
        ({"en_name": "Remi"}, "Remi"),
        ({"name": "Quanzhen Liang"}, "Quanzhen Liang"),
    ],
)
def test_user_profile_preserves_both_names(monkeypatch, fields, expected) -> None:
    client = LarkClient(settings())
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"open_id": "ou_person", **fields})
    profile = client.user_info("user-token")
    assert profile.display_name == expected
    assert profile.en_name_present == ("en_name" in fields)
    assert profile.name == ((fields.get("name") or "").strip() or None)
    assert profile.en_name == fields.get("en_name")


def test_chat_member_list_requests_a_paginated_user_roster(monkeypatch) -> None:
    client = LarkClient(settings())
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {"user_total": "17", "users": [{"member_id": "ou_saved"}]}

    monkeypatch.setattr(client, "_request", fake_request)

    page = client.list_chat_members("user-token", "oc_test", "page-2")

    assert page["user_total"] == "17"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/open-apis/im/v1/chats/oc_test/members/list")
    assert captured["access_token"] == "user-token"
    assert captured["params"] == {
        "member_id_type": "open_id",
        "member_types": "user",
        "page_size": 100,
        "page_token": "page-2",
    }


def test_message_list_requests_sender_names(monkeypatch) -> None:
    client = LarkClient(settings())
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {"items": []}

    monkeypatch.setattr(client, "_request", fake_request)

    client.list_chat_messages("user-token", "oc_test", page_token="page-2", ascending=False)

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/open-apis/im/v1/messages")
    assert captured["access_token"] == "user-token"
    assert captured["params"] == {
        "container_id_type": "chat",
        "container_id": "oc_test",
        "with_sender_name": "true",
        "page_size": 50,
        "sort_type": "ByCreateTimeDesc",
        "page_token": "page-2",
    }


def test_p2p_discovery_uses_the_user_chat_type_filter(monkeypatch) -> None:
    client = LarkClient(settings())
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {"items": []}

    monkeypatch.setattr(client, "_request", fake_request)

    client.list_chats("user-token", types="p2p")

    assert captured["params"]["types"] == "p2p"
    assert captured["params"]["user_id_type"] == "open_id"


def test_trust_party_mapping_uses_a_short_lived_tenant_token(monkeypatch) -> None:
    client = LarkClient(settings())
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return {"tenant_access_token": "tenant-token"}
        return {"items": []}

    monkeypatch.setattr(client, "_request", fake_request)

    token = client.tenant_access_token()
    client.list_collaboration_tenants(token, "next-page")

    assert token == "tenant-token"
    assert calls[0] == (
        "POST",
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        {"json": {"app_id": "cli_test", "app_secret": "secret"}},
    )
    assert calls[1][0] == "GET"
    assert calls[1][1].endswith("/open-apis/directory/v1/collaboration_tenants")
    assert calls[1][2]["access_token"] == "tenant-token"
    assert calls[1][2]["params"] == {"page_size": 100, "page_token": "next-page"}


def test_missing_app_scope_is_treated_as_an_authorization_problem() -> None:
    assert LarkApiError(400, 99991672, "scope missing").likely_auth_problem


def test_rate_limit_pauses_shared_admission_gate() -> None:
    events = []

    class Admission:
        @contextmanager
        def request(self):
            events.append("request")
            try:
                yield
            finally:
                events.append("release")

        def pause(self, seconds):
            events.append(("pause", seconds))

    class HttpClient:
        @staticmethod
        def request(*_args, **_kwargs):
            return httpx.Response(
                429,
                headers={"x-ogw-ratelimit-reset": "7"},
                json={"code": 99991400, "msg": "rate limited"},
            )

    client = LarkClient(settings(), http_client=HttpClient(), admission=Admission())

    with pytest.raises(LarkApiError) as raised:
        client.list_chats("user-token")

    assert raised.value.rate_limited
    assert raised.value.retry_after == 7
    assert events == ["request", ("pause", 7), "release"]


def test_legacy_rate_limit_code_is_retryable_without_http_429() -> None:
    error = LarkApiError(400, 99991400, "legacy rate limit")

    assert error.rate_limited
    assert error.retryable
