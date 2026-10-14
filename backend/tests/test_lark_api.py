import json
from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest

from app.config import Settings
from app.lark_api import LarkApiError, LarkClient, private_chat_url


def test_private_chat_link_uses_only_stored_user_identity():
    from urllib.parse import parse_qs, urlsplit

    target = "ou_" + "a" * 32
    url = urlsplit(private_chat_url(target))
    assert (url.scheme, url.netloc, url.path) == ("https", "applink.larksuite.com", "/client/chat/open")
    assert parse_qs(url.query) == {"openId": [target]}  # Never a CM chat ID, app ID or token.
    for missing_or_invalid in (None, "", "user536353", "oc_group", "ou_x&openChatId=oc_other", "javascript:alert(1)"):
        assert private_chat_url(missing_or_invalid) is None


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


def test_authorization_url_uses_oauth_client_id_and_pkce(monkeypatch) -> None:
    url = LarkClient(settings()).authorization_url("state value", "challenge-value")

    assert "client_id=cli_test" in url
    assert "app_id=" not in url
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "scope=offline_access+im%3Achat%3Aread" in url
    from dataclasses import replace
    from urllib.parse import parse_qs, urlsplit

    profile_scopes = ("contact:contact.base:readonly", "contact:user.base:readonly")
    config = replace(settings(), oauth_scopes=(*settings().oauth_scopes, *profile_scopes))
    client = LarkClient(config)
    requested = parse_qs(urlsplit(client.authorization_url("state", "challenge")).query)["scope"][0].split()
    assert set(profile_scopes).issubset(requested)
    requests = []
    monkeypatch.setattr(client, "_token_request", lambda payload: requests.append(payload))
    client.exchange_code("code", "verifier")
    client.refresh_token("refresh-token")
    assert requests[0]["scope"].split() == list(config.oauth_scopes)
    assert "scope" not in requests[1]  # Renewal cannot silently request newly configured privileges.


def test_token_response_distinguishes_missing_scope_from_an_explicit_empty_grant(monkeypatch):
    client = LarkClient(settings())
    for fields, expected in (({}, None), ({"scope": " "}, ""), ({"scope": " im:message "}, "im:message")):
        monkeypatch.setattr(client, "_request", lambda *_args, fields=fields, **_kwargs: {
            "access_token": "fixture", **fields,
        })
        assert client.refresh_token("fixture-refresh").scope == expected


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"name": "Quanzhen Liang", "en_name": "Remi"}, "Remi（Quanzhen Liang）"),
        ({"name": "Shang Ma", "en_name": "Shang Ma", "avatar_url": "https://s16-imfile-sg.feishucdn.com/a"},
         "Shang Ma"),
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
    assert profile.avatar_present == ("avatar_url" in fields)
    assert profile.avatar_url == fields.get("avatar_url")


def test_contact_profile_keeps_avatar_bound_to_requested_id_and_distinguishes_missing_fields(monkeypatch):
    client = LarkClient(settings())
    avatar = "https://s16-imfile-sg.feishucdn.com/static-resource/v1/avatar"
    response = {"user": {"open_id": "ou_person", "name": "Same Name", "avatar": {"avatar_240": avatar}}}

    def request(method, url, **kwargs):
        assert method == "GET" and url.endswith("/contact/v3/users/ou_person")
        assert kwargs == {"access_token": "user-token", "params": {"user_id_type": "open_id"}}
        return response

    monkeypatch.setattr(client, "_request", request)
    profile = client.contact_user("user-token", "ou_person")
    assert profile.open_id == "ou_person" and profile.avatar_url == avatar and profile.avatar_present
    response["user"].pop("avatar")
    assert not client.contact_user("user-token", "ou_person").avatar_present
    response["user"]["avatar"] = {}
    assert client.contact_user("user-token", "ou_person").avatar_present
    assert client.contact_user("user-token", "ou_person").avatar_url is None
    response["user"]["open_id"] = "ou_someone_with_same_name"
    with pytest.raises(LarkApiError, match="identity did not match"):
        client.contact_user("user-token", "ou_person")


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
    assert client.get_message("user-token", "om_saved") == {"items": []}
    assert captured == {
        "method": "GET", "url": "https://open.larksuite.com/open-apis/im/v1/messages/om_saved",
        "access_token": "user-token", "params": {"user_id_type": "open_id"},
    }
    with pytest.raises(ValueError, match="Invalid Lark identity"):
        client.get_message("user-token", "om_saved/resources/img_other")

    def rejected(*_args, **_kwargs):
        raise LarkApiError(403, 99991672, "secret body img_private", 7)

    monkeypatch.setattr(client, "_request", rejected)
    with pytest.raises(LarkApiError) as raised:
        client.get_message("user-token", "om_saved")
    assert (raised.value.status_code, raised.value.lark_code, raised.value.retry_after) == (403, 99991672, 7)
    assert raised.value.likely_auth_problem and "secret" not in str(raised.value)


def test_message_resources_are_bounded_in_memory_and_never_follow_redirects_or_expose_errors():
    calls, events, yielded, closed = [], [], [], []

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

    class ResourceStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"1234", b"56789", b"not-read"):
                yielded.append(chunk)
                yield chunk

        def close(self):
            closed.append(True)

    responses = iter((
        httpx.Response(200, content=b"pixels", headers={
            "content-type": "image/png", "content-disposition": "attachment; filename*=UTF-8''test%20image.png"}),
        httpx.Response(200, content=b"%PDF-x", headers={
            "content-type": "application/pdf; charset=binary",
            "content-disposition": 'attachment; filename="../a.pdf"'}),
        httpx.Response(200, stream=ResourceStream(), headers={"content-type": "application/pdf"}),
        httpx.Response(200, stream=ResourceStream(), headers={"content-length": "999"}),
        httpx.Response(429, json={"code": 99991400, "msg": "secret body img_private"},
                       headers={"x-ogw-ratelimit-reset": "7"}),
        httpx.Response(200, json={"code": 230020, "msg": "secret body img_private"}, headers={"Retry-After": "3"}),
        httpx.Response(403, json={"code": 99991672, "msg": "secret body img_private"}),
        httpx.Response(302, headers={"Location": "https://elsewhere.example/private"}),
        httpx.Response(200, content=b""),
        httpx.ReadTimeout("secret-token img_private secret body"),
    ))

    def handle(request):
        calls.append(request)
        assert request.url.host == "open.larksuite.com"
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.url.path.startswith("/open-apis/im/v1/messages/om_saved/resources/")
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as http_client:
        client = LarkClient(settings(), http_client=http_client, admission=Admission())
        assert client.download_message_resource("secret-token", "om_saved", "img_private", "image", 8) == {
            "content": b"pixels", "mime_type": "image/png", "filename": "test image.png"}
        assert dict(calls[-1].url.params) == {"type": "image"}
        assert client.download_message_resource("secret-token", "om_saved", "file_private", "file", 8) == {
            "content": b"%PDF-x", "mime_type": "application/pdf", "filename": "a.pdf"}
        assert dict(calls[-1].url.params) == {"type": "file"}
        for size in (8, 8):
            with pytest.raises(LarkApiError) as raised:
                client.download_message_resource("secret-token", "om_saved", "file_private", "file", size)
            assert raised.value.status_code == 413 and not raised.value.retryable
        assert yielded == [b"1234", b"56789"] and closed == [True, True]
        for status, code, retry in ((429, 99991400, 7), (502, 230020, 3), (403, 99991672, None),
                                    (302, None, None), (502, None, None), (503, None, None)):
            before = len(calls)
            with pytest.raises(LarkApiError) as raised:
                client.download_message_resource("secret-token", "om_saved", "file_private", "file", 128)
            assert (raised.value.status_code, raised.value.lark_code, raised.value.retry_after) == (status, code, retry)
            assert len(calls) == before + 1 and events[-1] == "release"
            assert not any(secret in str(raised.value) for secret in ("secret", "img_private", "file_private"))
            if retry:
                assert raised.value.rate_limited and ("pause", retry) in events
        before = len(calls)
        for message_id, key, resource_type, limit in (
            ("om_saved/other", "img_private", "image", 8), ("om_saved", "img_other/redirect", "image", 8),
            ("om_saved", "file_private", "image", 8), ("om_saved", "img_private", "audio", 8),
            ("om_saved", "img_private", "image", 0), ("om_saved", "img_private", "image", True),
        ):
            with pytest.raises(ValueError):
                client.download_message_resource("secret-token", message_id, key, resource_type, limit)
        assert len(calls) == before


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


def test_broadcast_preflight_checks_live_members_posting_and_mention_permissions():
    calls = []
    chat = {
        "name": "External support", "chat_mode": "group", "chat_status": "normal", "external": True,
        "moderation_permission": "all_members", "at_all_permission": "only_owner",
        "owner_id_type": "open_id", "owner_id": "ou_owner", "user_manager_id_list": ["ou_cm"],
        "user_count": "101",
    }
    in_chat = [True]
    complete = [True]

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer user-token"
        if request.url.path.endswith("/is_in_chat"):
            data = {"is_in_chat": in_chat[0]}
        elif request.url.path.endswith("/members/list"):
            assert request.url.params["member_id_type"] == "open_id"
            second = request.url.params.get("page_token") == "second"
            data = {"users": [{"member_id": "ou_creator" if second else "ou_other"}],
                    "has_more": not second, "page_token": "second", "truncations": [] if complete[0] else ["user"]}
        elif request.url.path.endswith("/moderation"):
            assert request.url.params["user_id_type"] == "open_id"
            second = request.url.params.get("page_token") == "second"
            data = {"moderation_setting": "moderator_list", "has_more": not second, "page_token": "second",
                    "items": [{"user_id_type": "open_id", "user_id": "ou_cm" if second else "ou_other"}]}
        else:
            assert request.url.params["user_id_type"] == "open_id"
            data = chat
        return httpx.Response(200, json={"code": 0, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        client = LarkClient(settings(), http_client=http_client)
        data = client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm",
                                             mention_all=True, mention_open_ids=("ou_creator",))
        assert data == {"chat_name": "External support", "member_count": 101, "external": True}
        assert len(calls) == 4  # A second roster page is needed; no guessing from local names.
        for changes, reason in (
            ({"at_all_permission": "only_owner", "user_manager_id_list": []}, "mention everyone"),
            ({"moderation_permission": "only_owner"}, "cannot post"),
            ({"moderation_permission": "all_members", "chat_status": "dissolved"}, "unavailable"),
        ):
            chat.update(changes)
            with pytest.raises(LarkApiError, match=reason):
                client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm", mention_all=True)
        in_chat[0] = False
        with pytest.raises(LarkApiError, match="no longer a group member"):
            client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm")
        in_chat[0] = True
        chat.update(chat_status="normal", moderation_permission="all_members")
        complete[0] = False
        with pytest.raises(LarkApiError, match="roster was incomplete"):
            client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm",
                                         mention_open_ids=("ou_creator",))
        complete[0] = True
        with pytest.raises(LarkApiError, match="Mentioned person is no longer a group member"):
            client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm",
                                         mention_open_ids=("ou_missing",))
        chat["moderation_permission"] = "moderator_list"
        assert client.check_chat_send_access("user-token", "oc_group", sender_open_id="ou_cm")["external"]


def test_broadcast_send_uses_explicit_real_mentions_and_fixed_uuid_without_parsing_body():
    requests = []
    message_uuid = str(UUID(int=42))
    body = 'Hello @all <at user_id="all">not a mention</at>\nhttps://example.com/?a=1&b=2'

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent", "chat_id": "oc_group"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        client = LarkClient(settings(), http_client=http_client)
        assert client.send_chat_post("user-token", "oc_group", body, message_uuid=message_uuid,
                                     mentions=[{"id": "ou_creator", "name": "Creator"}]) == "om_sent"
        request = requests[0]
        assert request.method == "POST" and request.url.params["receive_id_type"] == "chat_id"
        assert request.headers["Authorization"] == "Bearer user-token"
        payload = json.loads(request.content)
        assert payload["uuid"] == message_uuid and payload["receive_id"] == "oc_group" and payload["msg_type"] == "post"
        rows = json.loads(payload["content"])["zh_cn"]["content"]
        assert rows == [[{"tag": "at", "user_id": "ou_creator", "user_name": "Creator"}],
                        *[[{"tag": "text", "text": line}] for line in body.split("\n")]]
        client.send_chat_post("user-token", "oc_group", "test", message_uuid=message_uuid, mention_all=True)
        all_rows = json.loads(json.loads(requests[1].content)["content"])["zh_cn"]["content"]
        assert all_rows[0] == [{"tag": "at", "user_id": "all", "user_name": "all"}]
        with pytest.raises(ValueError, match="Invalid Lark identity"):
            client.send_chat_post("user-token", "oc_group", "test", message_uuid=message_uuid,
                                 mentions=[{"id": "all", "name": "do not silently escalate"}])
        with pytest.raises(ValueError, match="size limit"):
            client.send_chat_post("user-token", "oc_group", "test\n" * 1000, message_uuid=message_uuid)
        assert len(requests) == 2
        from app import broadcast_content
        asset = str(UUID(int=99))
        document = {"version": 2, "ops": [
            {"insert": "Styled", "attributes": {"bold": True, "italic": True, "underline": True, "strike": True}},
            {"insert": "\n", "attributes": {"list": "ordered"}},
            {"insert": "link", "attributes": {"link": "https://example.com"}}, {"insert": "\n"},
            {"insert": {"image": asset}}, {"insert": "\n😊\n"}]}
        client.send_chat_post("user-token", "oc_group", document, message_uuid=message_uuid,
                             mention_all=True, image_keys={asset: "img_private"})
        rich = json.loads(json.loads(requests[-1].content)["content"])["zh_cn"]["content"]
        assert rich[1] == [{"tag": "text", "text": "1. "}, {"tag": "text", "text": "Styled",
                           "style": ["bold", "italic", "underline", "lineThrough"]}]
        assert rich[2] == [{"tag": "a", "text": "link", "href": "https://example.com"}]
        assert rich[3] == [{"tag": "img", "image_key": "img_private"}]
        before = len(requests)
        for bad in (
            {"version": 2, "ops": [{"insert": {"image": "https://evil.test/a.png"}}]},
            {"version": 2, "ops": [{"insert": "bad", "attributes": {"link": "javascript:alert(1)"}}]},
            {"version": 2, "ops": [{"retain": 1}]},
            {"version": 2, "ops": [{"insert": {"image": asset}}] * 6},
            {"version": 2, "ops": [{"insert": "a" * 4001 + "\n"}]},
        ):
            with pytest.raises(ValueError):
                client.send_chat_post("user-token", "oc_group", bad, message_uuid=message_uuid)
        assert len(requests) == before
        assert len(broadcast_content.plain_text(broadcast_content.normalize("a" * 4000))) == 4000

    def image_upload(request):
        assert request.headers["Authorization"] == "Bearer user-token"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert b'name="image_type"' in request.content and b"message" in request.content
        assert b'name="image"; filename="example.png"' in request.content
        return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_private"}})
    with httpx.Client(transport=httpx.MockTransport(image_upload)) as http_client:
        assert LarkClient(settings(), http_client=http_client).upload_message_image(
            "user-token", b"tested-by-image-service", "example.png", "image/png") == "img_private"


def test_broadcast_send_distinguishes_known_rejection_from_unknown_delivery_and_hides_raw_errors():
    calls = []
    responses = iter((
        httpx.ConnectError("credentials in upstream message"),
        httpx.ReadTimeout("body in upstream message"),
        httpx.Response(500, json={"code": 0, "msg": "private body"}),
        httpx.Response(400, json={"code": 230049, "msg": "private body"}),
        httpx.Response(200, json={"code": 0, "data": {}}),
        httpx.Response(200, text="private body"),
        httpx.Response(400, json={"code": 230035, "msg": "private body"}),
        httpx.Response(400, json={"code": 230020, "msg": "private body"}),
    ))

    def handle(request):
        calls.append(request)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        client = LarkClient(settings(), http_client=http_client)
        for index, unknown in enumerate((False, True, True, True, True, True, False, False), start=1):
            with pytest.raises(LarkApiError) as raised:
                client.send_chat_post("secret-token", "oc_group", "secret-body", message_uuid=str(UUID(int=42)))
            assert raised.value.delivery_unknown is unknown
            assert len(calls) == index  # No internal retry, even with the idempotency UUID.
            assert "private body" not in str(raised.value) and "secret" not in str(raised.value)
        assert raised.value.rate_limited
