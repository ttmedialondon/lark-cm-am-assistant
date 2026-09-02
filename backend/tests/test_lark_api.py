from app.config import Settings
from app.lark_api import LarkClient


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
        max_pages_per_job=30,
        worker_idle_seconds=3,
    )


def test_authorization_url_uses_oauth_client_id_and_pkce() -> None:
    url = LarkClient(settings()).authorization_url("state value", "challenge-value")

    assert "client_id=cli_test" in url
    assert "app_id=" not in url
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "scope=offline_access+im%3Achat%3Aread" in url


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
