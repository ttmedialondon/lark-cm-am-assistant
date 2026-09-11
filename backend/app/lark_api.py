"""Thin, auditable wrapper around only the Lark APIs used by this test release."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .config import Settings
from .identity_names import format_display_name
from .lark_sync_admission import LarkSyncAdmission


class LarkApiError(RuntimeError):
    """An API failure without retaining raw response bodies or credentials."""

    def __init__(self, status_code: int, lark_code: int | None, message: str, retry_after: int | None = None) -> None:
        super().__init__(message[:300])
        self.status_code = status_code
        self.lark_code = lark_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.rate_limited or self.status_code >= 500

    @property
    def rate_limited(self) -> bool:
        """Lark still returns its documented legacy rate error as HTTP 400."""

        return self.status_code == 429 or self.lark_code == 99991400

    @property
    def likely_auth_problem(self) -> bool:
        return self.status_code in {401, 403} or self.lark_code in {
            20037,
            20064,
            20073,
            99991672,
            99991661,
            99991663,
            99991668,
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
            "scope": " ".join(self.settings.oauth_scopes),
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
            scope=_string(data.get("scope")),
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        params: dict[str, str | int] | None = None,
        json: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        gate = self._admission.request() if self._admission else nullcontext()
        with gate:
            try:
                if self._http_client:
                    response = self._http_client.request(method, url, headers=headers, params=params, json=json)
                else:
                    with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                        response = client.request(method, url, headers=headers, params=params, json=json)
            except httpx.HTTPError as exc:
                raise LarkApiError(503, None, "network failure while calling Lark") from exc

            retry_after = _rate_limit_reset(response.headers)
            try:
                raw: Any = response.json()
            except ValueError as exc:
                self._pause_for_rate_limit(response.status_code, None, retry_after)
                raise LarkApiError(
                    response.status_code, None, "Lark returned a non-JSON response", retry_after
                ) from exc
            if not isinstance(raw, dict):
                self._pause_for_rate_limit(response.status_code, None, retry_after)
                raise LarkApiError(response.status_code, None, "Lark returned an invalid JSON response", retry_after)

            code = _optional_int(raw.get("code"))
            message = _string(raw.get("msg")) or _string(raw.get("message")) or "Lark API request failed"
            if not response.is_success or (code is not None and code != 0):
                self._pause_for_rate_limit(response.status_code, code, retry_after)
                raise LarkApiError(response.status_code, code, message, retry_after)

            data = raw.get("data", raw)
            if not isinstance(data, dict):
                raise LarkApiError(502, code, "Lark response data was not an object")
            return data

    def _require_secret(self) -> None:
        if not self.settings.app_secret_configured:
            raise RuntimeError("Lark App Secret is not configured")

    def _pause_for_rate_limit(self, status_code: int, lark_code: int | None, retry_after: int | None) -> None:
        if self._admission and (status_code == 429 or lark_code == 99991400):
            self._admission.pause(retry_after)


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
