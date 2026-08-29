"""Runtime configuration. Secrets are injected only through systemd EnvironmentFile."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import lru_cache


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} cannot be negative")
    return value


def _decode_key(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY_B64 is not valid URL-safe base64") from exc
    if len(key) != 32:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY_B64 must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True)
class Settings:
    database_url: str
    lark_app_id: str
    lark_app_secret: str
    lark_redirect_uri: str
    token_encryption_key: bytes
    oauth_authorize_url: str
    oauth_token_url: str
    user_info_url: str
    lark_api_base_url: str
    oauth_scopes: tuple[str, ...]
    sync_lookback_days: int
    sync_interval_seconds: int
    sync_overlap_seconds: int
    member_count_refresh_seconds: int
    max_pages_per_job: int
    worker_idle_seconds: int

    @property
    def app_secret_configured(self) -> bool:
        return bool(self.lark_app_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=_required("DATABASE_URL"),
        lark_app_id=_required("LARK_APP_ID"),
        lark_app_secret=os.environ.get("LARK_APP_SECRET", "").strip(),
        lark_redirect_uri=_required("LARK_REDIRECT_URI"),
        token_encryption_key=_decode_key(_required("TOKEN_ENCRYPTION_KEY_B64")),
        oauth_authorize_url=os.environ.get(
            "LARK_OAUTH_AUTHORIZE_URL", "https://accounts.larksuite.com/open-apis/authen/v1/authorize"
        ).strip(),
        oauth_token_url=os.environ.get(
            "LARK_OAUTH_TOKEN_URL", "https://open.larksuite.com/open-apis/authen/v2/oauth/token"
        ).strip(),
        user_info_url=os.environ.get(
            "LARK_USER_INFO_URL", "https://open.larksuite.com/open-apis/authen/v1/user_info"
        ).strip(),
        lark_api_base_url=os.environ.get("LARK_API_BASE_URL", "https://open.larksuite.com").rstrip("/"),
        oauth_scopes=tuple(
            scope
            for scope in os.environ.get(
                "LARK_OAUTH_SCOPES", "offline_access im:chat:read im:chat.members:read im:message:readonly"
            ).split()
            if scope
        ),
        sync_lookback_days=_nonnegative_int("SYNC_LOOKBACK_DAYS", 0),
        sync_interval_seconds=_positive_int("SYNC_INTERVAL_SECONDS", 180, 30),
        sync_overlap_seconds=_positive_int("SYNC_OVERLAP_SECONDS", 600, 60),
        member_count_refresh_seconds=_positive_int("MEMBER_COUNT_REFRESH_SECONDS", 86400, 300),
        max_pages_per_job=_positive_int("MAX_PAGES_PER_JOB", 30, 1),
        worker_idle_seconds=_positive_int("WORKER_IDLE_SECONDS", 3, 1),
    )
