"""Small write path for the owner-maintained creator identity library."""

from __future__ import annotations

import json
import re
from uuid import UUID, uuid4

import psycopg
from psycopg.errors import UniqueViolation

from .db import connection

IDENTIFIER_KINDS = {"handle", "email", "phone", "crm_id", "alias"}


class CreatorInputError(ValueError):
    """A form value that cannot become a stable creator identifier."""


def create_creator(actor_id: UUID, display_name: str) -> UUID:
    name = _required_text(display_name, "达人名称", 160)
    creator_id = uuid4()
    with connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO creators (id, display_name) VALUES (%s, %s)",
            (creator_id, name),
        )
        _audit(conn, actor_id, "creator_created", {"creator_id": str(creator_id)})
    return creator_id


def add_identifier(
    actor_id: UUID,
    creator_id: UUID,
    *,
    kind: str,
    platform: str,
    value: str,
) -> UUID:
    kind = kind.strip()
    if kind not in IDENTIFIER_KINDS:
        raise CreatorInputError("请选择有效的身份类型。")
    display_value = _required_text(value, "身份标识", 320)
    normalized_value, platform = _normalize_identifier(kind, platform, display_value)
    identifier_id = uuid4()
    try:
        with connection() as conn, conn.transaction():
            if not conn.execute("SELECT 1 FROM creators WHERE id = %s", (creator_id,)).fetchone():
                raise CreatorInputError("未找到该达人。")
            conn.execute(
                """
                INSERT INTO creator_identifiers (
                    id, creator_id, kind, platform, normalized_value, display_value
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (identifier_id, creator_id, kind, platform, normalized_value, display_value),
            )
            _audit(
                conn,
                actor_id,
                "creator_identifier_created",
                {"creator_id": str(creator_id), "kind": kind, "platform": platform},
            )
    except UniqueViolation as exc:
        raise CreatorInputError("该身份标识已存在，不能重复归属。") from exc
    return identifier_id


def _normalize_identifier(kind: str, platform: str, value: str) -> tuple[str, str]:
    if kind == "handle":
        normalized_platform = _platform(platform, required=True)
        normalized = value.lstrip("@").strip().casefold()
        if not normalized or len(normalized) > 320:
            raise CreatorInputError("账号格式无效。")
        return normalized, normalized_platform
    if kind == "email":
        normalized = value.casefold()
        if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
            raise CreatorInputError("邮箱格式无效。")
        return normalized, ""
    if kind == "phone":
        digits = "".join(character for character in value if character.isdigit())
        if not 7 <= len(digits) <= 20:
            raise CreatorInputError("电话号码格式无效。")
        return (f"+{digits}" if value.lstrip().startswith("+") else digits), ""
    if kind == "crm_id":
        return value, _platform(platform, required=False)
    return value.casefold(), _platform(platform, required=False)


def _required_text(value: str, label: str, limit: int) -> str:
    text = value.strip()
    if not text:
        raise CreatorInputError(f"{label}不能为空。")
    if len(text) > limit:
        raise CreatorInputError(f"{label}不能超过 {limit} 个字符。")
    return text


def _platform(value: str, *, required: bool) -> str:
    platform = value.strip().casefold()
    if not platform and required:
        raise CreatorInputError("账号需要填写平台，例如 tiktok 或 instagram。")
    if platform and (len(platform) > 40 or not re.fullmatch(r"[a-z0-9_-]+", platform)):
        raise CreatorInputError("平台只能使用小写字母、数字、短横线或下划线。")
    return platform


def _audit(conn: psycopg.Connection, actor_id: UUID, action: str, metadata: dict[str, str]) -> None:
    conn.execute(
        "INSERT INTO audit_events (id, authorization_id, action, metadata) VALUES (%s, %s, %s, %s::jsonb)",
        (uuid4(), actor_id, action, json.dumps(metadata)),
    )
