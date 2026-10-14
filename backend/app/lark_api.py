"""Thin, auditable wrapper around only the Lark APIs used by this test release."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import broadcast_content
from .config import Settings
from .identity_names import format_display_name
from .lark_sync_admission import LarkSyncAdmission


def private_chat_url(open_id: str | None) -> str | None:
    """Open the clicking user's Lark chat, never another CM's conversation."""
    if not isinstance(open_id, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", open_id):
        return None
    return "https://applink.larksuite.com/client/chat/open?" + urlencode({"openId": open_id})


class LarkApiError(RuntimeError):
    """An API failure without retaining raw response bodies or credentials."""

    def __init__(
        self, status_code: int, lark_code: int | None, message: str, retry_after: int | None = None,
        *, delivery_unknown: bool = False,
    ) -> None:
        super().__init__(message[:300])
        self.status_code = status_code
        self.lark_code = lark_code
        self.retry_after = retry_after
        self.delivery_unknown = delivery_unknown

    @property
    def retryable(self) -> bool:
        return self.rate_limited or self.status_code >= 500

    @property
    def rate_limited(self) -> bool:
        """Lark still returns its documented legacy rate error as HTTP 400."""

        return self.status_code == 429 or self.lark_code in {99991400, 230020}

    @property
    def likely_auth_problem(self) -> bool:
        # A chat/resource denial may also use HTTP 403; only token/scope errors affect the account.
        return self.status_code == 401 or self.lark_code in {
            20026,
            20037,
            20064,
            20073,
            99991672,
            99991661,
            99991663,
            99991668,
            99991669,
            99991671,
            99991676,
            99991677,
            99991679,
        }


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int
    refresh_expires_in: int | None
    scope: str | None


@dataclass(frozen=True)
class LarkProfile:
    open_id: str
    union_id: str | None
    name: str | None
    avatar_url: str | None
    en_name: str | None = None
    en_name_present: bool = False
    avatar_present: bool = False

    @property
    def display_name(self) -> str | None:
        return format_display_name(self.name, self.en_name)


class LarkClient:
    """Small wrapper for user-scoped IM data and tenant-scoped organization mapping."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        admission: LarkSyncAdmission | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._admission = admission

    def authorization_url(self, state: str, code_challenge: str) -> str:
        query = {
            "client_id": self.settings.lark_app_id,
            "redirect_uri": self.settings.lark_redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(self.settings.oauth_scopes),
        }
        return f"{self.settings.oauth_authorize_url}?{urlencode(query)}"

    def exchange_code(self, code: str, code_verifier: str) -> TokenBundle:
        self._require_secret()
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.settings.lark_app_id,
            "client_secret": self.settings.lark_app_secret,
            "code": code,
            "redirect_uri": self.settings.lark_redirect_uri,
            "code_verifier": code_verifier,
            "scope": " ".join(self.settings.oauth_scopes),
        }
        return self._token_request(payload)

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        self._require_secret()
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.settings.lark_app_id,
            "client_secret": self.settings.lark_app_secret,
            "refresh_token": refresh_token,
        }
        return self._token_request(payload)

    def user_info(self, access_token: str) -> LarkProfile:
        data = self._request("GET", self.settings.user_info_url, access_token=access_token)
        open_id = _string(data.get("open_id"))
        if not open_id:
            raise LarkApiError(502, None, "Lark user-info response did not contain open_id")
        return LarkProfile(
            open_id=open_id,
            union_id=_string(data.get("union_id")),
            name=_string(data.get("name")),
            avatar_url=_string(data.get("avatar_url")),
            en_name=_string(data.get("en_name")),
            en_name_present="en_name" in data,
            avatar_present="avatar_url" in data,
        )

    def contact_user(self, access_token: str, open_id: str) -> LarkProfile:
        """Fetch only a permitted user's profile; never associate by name."""
        data = self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/contact/v3/users/{quote(open_id, safe='')}",
            access_token=access_token,
            params={"user_id_type": "open_id"},
        )
        user = data.get("user")
        if not isinstance(user, dict) or user.get("open_id") != open_id:
            raise LarkApiError(502, None, "Lark profile identity did not match the requested user")
        avatar = user.get("avatar")
        if avatar is not None and not isinstance(avatar, dict):
            raise LarkApiError(502, None, "Lark profile avatar had an invalid format")
        avatar_url = next(
            (_string((avatar or {}).get(key)) for key in ("avatar_240", "avatar_72", "avatar_640", "avatar_origin")
             if _string((avatar or {}).get(key))),
            None,
        )
        return LarkProfile(
            open_id=open_id,
            union_id=_string(user.get("union_id")),
            name=_string(user.get("name")),
            en_name=_string(user.get("en_name")),
            en_name_present="en_name" in user,
            avatar_url=avatar_url,
            avatar_present="avatar" in user,
        )

    def list_chats(
        self, access_token: str, page_token: str | None = None, *, types: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "user_id_type": "open_id",
            # Create-time ordering is stable while new messages change active time.
            "sort_type": "ByCreateTimeAsc",
            "page_size": 100,
        }
        if page_token:
            params["page_token"] = page_token
        if types:
            params["types"] = types
        return self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/im/v1/chats",
            access_token=access_token,
            params=params,
        )

    def list_chat_messages(
        self,
        access_token: str,
        chat_id: str,
        *,
        page_token: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        ascending: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "with_sender_name": "true",
            "page_size": 50,
            "sort_type": "ByCreateTimeAsc" if ascending else "ByCreateTimeDesc",
        }
        if page_token:
            params["page_token"] = page_token
        if start_at:
            params["start_time"] = int(start_at.timestamp())
        if end_at:
            params["end_time"] = int(end_at.timestamp())
        return self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/im/v1/messages",
            access_token=access_token,
            params=params,
        )

    def get_message(self, access_token: str, message_id: str) -> dict[str, Any]:
        _require_im_id(message_id, "om_")
        try:
            return self._request(
                "GET", f"{self.settings.lark_api_base_url}/open-apis/im/v1/messages/{message_id}",
                access_token=access_token, params={"user_id_type": "open_id"},
            )
        except LarkApiError as exc:
            raise LarkApiError(
                exc.status_code, exc.lark_code, "Lark message could not be read", exc.retry_after,
            ) from None

    def download_message_resource(
        self, access_token: str, message_id: str, file_key: str, resource_type: str,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Read a message-bound resource into bounded memory, never follow resource URLs."""
        _require_im_id(message_id, "om_")
        if not isinstance(resource_type, str) or resource_type not in {"image", "file"}:
            raise ValueError("Invalid Lark resource type")
        prefix = "img_" if resource_type == "image" else "file_"
        if not isinstance(file_key, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,256}", file_key):
            raise ValueError("Invalid Lark resource key")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("Invalid Lark resource size limit")
        url = f"{self.settings.lark_api_base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        gate = self._admission.request() if self._admission else nullcontext()
        client_context = (nullcontext(self._http_client) if self._http_client else
                          httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)))
        with gate, client_context as client:
            try:
                with client.stream(
                    "GET", url, headers={"Authorization": f"Bearer {access_token}"},
                    params={"type": resource_type}, follow_redirects=False,
                ) as response:
                    retry_after = _rate_limit_reset(response.headers)
                    mime_type = response.headers.get("content-type", "application/octet-stream")
                    mime_type = mime_type.split(";", 1)[0].strip().lower()
                    json_response = mime_type == "application/json" or mime_type.endswith("+json")
                    length = _optional_int(response.headers.get("content-length"))
                    if response.is_success and not json_response and length is not None and length > max_bytes:
                        raise LarkApiError(413, None, "Lark resource exceeds the size limit")
                    content = bytearray()
                    # Error bodies are inspected for codes only, never propagated or persisted.
                    limit = max_bytes if response.is_success and not json_response else min(max_bytes, 64 * 1024)
                    for chunk in response.iter_bytes(chunk_size=min(64 * 1024, limit + 1)):
                        if len(content) + len(chunk) > limit:
                            if response.is_success and not json_response:
                                raise LarkApiError(413, None, "Lark resource exceeds the size limit")
                            break
                        content.extend(chunk)
                    if not response.is_success or json_response:
                        try:
                            raw = json.loads(content)
                        except (ValueError, UnicodeError):
                            raw = None
                        code = _optional_int(raw.get("code")) if isinstance(raw, dict) else None
                        self._pause_for_rate_limit(response.status_code, code, retry_after)
                        raise LarkApiError(
                            response.status_code if not response.is_success else 502,
                            code, "Lark message resource could not be read", retry_after,
                        )
                    if not content:
                        raise LarkApiError(502, None, "Lark message resource was empty")
                    disposition = Message()
                    disposition["content-disposition"] = response.headers.get("content-disposition", "")
                    filename = disposition.get_filename()
                    if filename:
                        filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
                        if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
                            filename = None
                    return {"content": bytes(content), "mime_type": mime_type, "filename": filename}
            except httpx.HTTPError:
                raise LarkApiError(503, None, "Network failure while reading a Lark message resource") from None

    def list_chat_members(
        self, access_token: str, chat_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        """List the human roster with user IDs represented as open IDs."""

        params: dict[str, str | int] = {
            "member_id_type": "open_id",
            "member_types": "user",
            "page_size": 100,
        }
        if page_token:
            params["page_token"] = page_token
        return self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/im/v1/chats/{quote(chat_id, safe='')}/members/list",
            access_token=access_token,
            params=params,
        )

    def get_chat(self, access_token: str, chat_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/im/v1/chats/{quote(chat_id, safe='')}",
            access_token=access_token,
            params={"user_id_type": "open_id"},
        )

    def is_in_chat(self, access_token: str, chat_id: str) -> bool:
        data = self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/im/v1/chats/{quote(chat_id, safe='')}/members/is_in_chat",
            access_token=access_token,
        )
        if not isinstance(data.get("is_in_chat"), bool):
            raise LarkApiError(502, None, "Lark membership response was incomplete")
        return data["is_in_chat"]

    def check_chat_send_access(
        self, access_token: str, chat_id: str, *, sender_open_id: str,
        mention_all: bool = False, mention_open_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Recheck the caller, live group permissions and explicitly selected mentions."""
        _require_im_id(chat_id, "oc_")
        _require_im_id(sender_open_id, "ou_")
        for target in mention_open_ids:
            _require_im_id(target, "ou_")
        if not self.is_in_chat(access_token, chat_id):
            raise LarkApiError(403, None, "Sender is no longer a group member")
        chat = self.get_chat(access_token, chat_id)
        if chat.get("chat_mode") not in {"group", "topic"} or chat.get("chat_status") != "normal":
            raise LarkApiError(403, None, "Group is unavailable for broadcast")
        managers = chat.get("user_manager_id_list")
        manager = (
            chat.get("owner_id_type") == "open_id" and chat.get("owner_id") == sender_open_id
        ) or (isinstance(managers, list) and sender_open_id in managers)
        posting = chat.get("moderation_permission")
        if posting == "moderator_list":
            permitted = manager or self._is_chat_moderator(access_token, chat_id, sender_open_id)
        else:
            permitted = posting == "all_members" or (posting == "only_owner" and manager)
        if not permitted:
            raise LarkApiError(403, None, "Sender cannot post in this group")
        at_all = chat.get("at_all_permission")
        if mention_all and not (at_all == "all_members" or (at_all == "only_owner" and manager)):
            raise LarkApiError(403, None, "Sender cannot mention everyone in this group")
        if mention_open_ids:
            members = self._all_chat_member_ids(access_token, chat_id)
            if not set(mention_open_ids).issubset(members):
                raise LarkApiError(403, None, "Mentioned person is no longer a group member")
        return {
            "chat_name": _string(chat.get("name")),
            "member_count": _optional_int(chat.get("user_count")),
            "external": chat.get("external") is True,
        }

    def _is_chat_moderator(self, access_token: str, chat_id: str, sender_open_id: str) -> bool:
        page_token: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, str | int] = {"user_id_type": "open_id", "page_size": 100}
            if page_token:
                params["page_token"] = page_token
            page = self._request(
                "GET", f"{self.settings.lark_api_base_url}/open-apis/im/v1/chats/{quote(chat_id, safe='')}/moderation",
                access_token=access_token, params=params,
            )
            if page.get("moderation_setting") != "moderator_list":
                raise LarkApiError(403, None, "Group posting permission changed during verification")
            items = page.get("items")
            if not isinstance(items, list):
                raise LarkApiError(502, None, "Lark moderator response was incomplete")
            if any(isinstance(item, dict) and item.get("user_id_type") == "open_id"
                   and item.get("user_id") == sender_open_id for item in items):
                return True
            page_token = _next_roster_page(page, seen)
            if page_token is None:
                return False

    def _all_chat_member_ids(self, access_token: str, chat_id: str) -> set[str]:
        page_token: str | None = None
        seen: set[str] = set()
        members: set[str] = set()
        while True:
            page = self.list_chat_members(access_token, chat_id, page_token)
            items = page.get("users", page.get("items"))
            if not isinstance(items, list) or page.get("truncations"):
                raise LarkApiError(502, None, "Lark member roster was incomplete")
            for item in items:
                if isinstance(item, dict):
                    open_id = _string(item.get("member_id")) or _string(item.get("open_id"))
                    if open_id and re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", open_id):
                        members.add(open_id)
            page_token = _next_roster_page(page, seen)
            if page_token is None:
                return members

    def send_chat_post(
        self, access_token: str, chat_id: str, body: str | dict, *,
        mentions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        mention_all: bool = False, message_uuid: str, image_keys: dict | None = None,
    ) -> str:
        """Send one explicit post with durable UUID; never retry a send here."""
        payload = build_chat_post_payload(
            chat_id, body, mentions=mentions, mention_all=mention_all, message_uuid=message_uuid, image_keys=image_keys,
        )
        data = self._request(
            "POST", f"{self.settings.lark_api_base_url}/open-apis/im/v1/messages",
            access_token=access_token, params={"receive_id_type": "chat_id"}, json=payload, sending=True,
        )
        message_id = _string(data.get("message_id"))
        if not message_id or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,128}", message_id):
            raise LarkApiError(502, None, "Lark did not confirm a valid message ID", delivery_unknown=True)
        if data.get("chat_id") is not None and data["chat_id"] != chat_id:
            raise LarkApiError(502, None, "Lark send response had an unexpected chat", delivery_unknown=True)
        return message_id

    def upload_message_image(self, access_token: str, content: bytes, filename: str, mime_type: str) -> str:
        data = self._request(
            "POST", f"{self.settings.lark_api_base_url}/open-apis/im/v1/images", access_token=access_token,
            data={"image_type": "message"}, files={"image": (filename, content, mime_type)},
        )
        key = _string(data.get("image_key"))
        if not key or not re.fullmatch(r"img_[A-Za-z0-9_-]{1,256}", key):
            raise LarkApiError(502, None, "Lark did not confirm a valid image key")
        return key

    def tenant_access_token(self) -> str:
        """Get a short-lived app tenant token; never persist it."""

        self._require_secret()
        data = self._request(
            "POST",
            f"{self.settings.lark_api_base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.settings.lark_app_id, "app_secret": self.settings.lark_app_secret},
        )
        token = _string(data.get("tenant_access_token"))
        if not token:
            raise LarkApiError(502, None, "Lark tenant-token response did not contain tenant_access_token")
        return token

    def list_collaboration_tenants(
        self, tenant_access_token: str, page_token: str | None = None
    ) -> dict[str, Any]:
        """List only organizations associated with this Lark tenant."""

        params: dict[str, str | int] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        return self._request(
            "GET",
            f"{self.settings.lark_api_base_url}/open-apis/directory/v1/collaboration_tenants",
            access_token=tenant_access_token,
            params=params,
        )

    def _token_request(self, payload: dict[str, str]) -> TokenBundle:
        data = self._request("POST", self.settings.oauth_token_url, json=payload)
        access_token = _string(data.get("access_token"))
        if not access_token:
            raise LarkApiError(502, None, "Lark token response did not contain access_token")
        return TokenBundle(
            access_token=access_token,
            refresh_token=_string(data.get("refresh_token")),
            expires_in=_int(data.get("expires_in"), 7200),
            refresh_expires_in=_optional_int(data.get("refresh_expires_in")),
            scope=data["scope"].strip() if isinstance(data.get("scope"), str) else None,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        params: dict[str, str | int] | None = None,
        json: dict[str, str] | None = None,
        sending: bool = False,
        data: dict[str, str] | None = None,
        files: dict | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        kwargs = {"headers": headers, "params": params, "json": json}
        if files is not None:
            kwargs.update(data=data, files=files)

        gate = self._admission.request() if self._admission else nullcontext()
        with gate:
            try:
                if self._http_client:
                    response = self._http_client.request(method, url, **kwargs)
                else:
                    with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                        response = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if sending:
                    unknown = not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout)
                    raise LarkApiError(
                        503, None, "Network failure while sending to Lark", delivery_unknown=unknown,
                    ) from None
                raise LarkApiError(503, None, "network failure while calling Lark") from exc

            retry_after = _rate_limit_reset(response.headers)
            try:
                raw: Any = response.json()
            except ValueError as exc:
                self._pause_for_rate_limit(response.status_code, None, retry_after)
                raise LarkApiError(
                    response.status_code, None, "Lark returned a non-JSON response", retry_after,
                    delivery_unknown=sending and response.status_code != 429,
                ) from None if sending else exc
            if not isinstance(raw, dict):
                self._pause_for_rate_limit(response.status_code, None, retry_after)
                raise LarkApiError(
                    response.status_code, None, "Lark returned an invalid JSON response", retry_after,
                    delivery_unknown=sending and response.status_code != 429,
                )

            code = _optional_int(raw.get("code"))
            message = ("Lark message send was not confirmed" if sending else
                       _string(raw.get("msg")) or _string(raw.get("message")) or "Lark API request failed")
            if not response.is_success or (code is not None and code != 0):
                self._pause_for_rate_limit(response.status_code, code, retry_after)
                raise LarkApiError(
                    response.status_code, code, message, retry_after,
                    delivery_unknown=sending and (response.status_code >= 500 or code == 230049),
                )

            data = raw.get("data", raw)
            if not isinstance(data, dict):
                raise LarkApiError(502, code, "Lark response data was not an object", delivery_unknown=sending)
            return data

    def _require_secret(self) -> None:
        if not self.settings.app_secret_configured:
            raise RuntimeError("Lark App Secret is not configured")

    def _pause_for_rate_limit(self, status_code: int, lark_code: int | None, retry_after: int | None) -> None:
        if self._admission and (status_code == 429 or lark_code in {99991400, 230020}):
            self._admission.pause(retry_after)


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_im_id(value: str, prefix: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid Lark identity")


def build_chat_post_payload(
    chat_id: str, body: str | dict, *, mentions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
    mention_all: bool = False, message_uuid: str, image_keys: dict | None = None,
) -> dict[str, str]:
    """The same literal-content and wire-size check is used at preview and send."""
    _require_im_id(chat_id, "oc_")
    if not isinstance(body, str | dict) or isinstance(body, str) and not body.strip():
        raise ValueError("Message body cannot be empty")
    message_uuid = str(uuid.UUID(str(message_uuid)))
    if mention_all and mentions:
        raise ValueError("Mention everyone and individual mentions cannot be combined")
    nodes: list[dict[str, str]] = []
    if mention_all:
        nodes.append({"tag": "at", "user_id": "all", "user_name": "all"})
    else:
        seen: set[str] = set()
        for mention in mentions:
            open_id = mention["id"]
            _require_im_id(open_id, "ou_")
            if open_id not in seen:
                seen.add(open_id)
                nodes.append({"tag": "at", "user_id": open_id, "user_name": mention.get("name") or open_id})
    rows = (broadcast_content.post_rows(broadcast_content.normalize(body), image_keys or {}) if isinstance(body, dict)
            else [[{"tag": "text", "text": line}] for line in body.split("\n")])
    if nodes:
        rows.insert(0, nodes)
    content = json.dumps({"zh_cn": {"title": "", "content": rows}}, ensure_ascii=False, separators=(",", ":"))
    payload = {"receive_id": chat_id, "msg_type": "post", "content": content, "uuid": message_uuid}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 30_000:
        raise ValueError("Rich text message exceeds the Lark size limit")
    return payload


def _next_roster_page(page: dict[str, Any], seen: set[str]) -> str | None:
    if page.get("has_more") is False:
        return None
    token = _string(page.get("page_token"))
    if page.get("has_more") is not True or not token or token in seen or len(seen) >= 100:
        raise LarkApiError(502, None, "Lark roster pagination was incomplete")
    seen.add(token)
    return token


def _int(value: object, default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None else default


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rate_limit_reset(headers: httpx.Headers) -> int | None:
    """Lark's reset header takes precedence over the generic Retry-After."""

    for name in ("x-ogw-ratelimit-reset", "Retry-After"):
        value = _optional_int(headers.get(name))
        if value is not None and value >= 0:
            return value
    return None
