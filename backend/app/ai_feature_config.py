"""Encrypted, owner-managed AI feature configuration."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

from .crypto import TokenCipher

_KEY_AAD = b"lark-message-stats/ai-feature-config/v1"
_KEY_DERIVATION = b"lark-message-stats/ai-feature-config-key/v1"
_LONDON = ZoneInfo("Europe/London")
DEFAULT_AUTO_CONFIRM_THRESHOLD = Decimal("0.900")
_SAFE_COLUMNS = """
    feature, provider, project_id, model, enabled, key_suffix, key_updated_at,
    last_tested_at, last_test_status, auto_confirm_threshold, created_at, updated_at
"""


class FeatureConfigInputError(ValueError):
    """An AI feature configuration value is invalid."""


def api_key_cipher(master_key: bytes) -> TokenCipher:
    """Create the encryption domain for provider API keys."""

    key = hmac.digest(master_key, _KEY_DERIVATION, "sha256")
    return TokenCipher(key, _KEY_AAD)


def get_feature_config(conn: psycopg.Connection, feature: str) -> dict[str, Any] | None:
    """Return a template-safe feature configuration, never its credential."""

    row = conn.execute(
        f"SELECT {_SAFE_COLUMNS} FROM ai_feature_configs WHERE feature = %s",
        (_required_text(feature, "AI 功能", 80),),
    ).fetchone()
    return _safe_config(row) if row else None


def save_feature_config(
    conn: psycopg.Connection,
    cipher: TokenCipher,
    feature: str,
    *,
    provider: str,
    project_id: str,
    model: str,
    enabled: bool,
    api_key: str | None,
    updated_by_authorization_id: UUID,
    auto_confirm_threshold: Decimal | float | str = DEFAULT_AUTO_CONFIRM_THRESHOLD,
) -> dict[str, Any]:
    """Upsert one feature config; a blank key retains the existing credential."""

    feature = _required_text(feature, "AI 功能", 80)
    provider = _required_text(provider, "服务商", 40).casefold()
    project_id = _required_text(project_id, "Project ID", 160)
    model = _required_text(model, "模型", 160)
    api_key = _normalize_api_key(api_key)
    auto_confirm_threshold = _normalize_auto_confirm_threshold(auto_confirm_threshold)

    current = conn.execute(
        "SELECT api_key_ciphertext, key_suffix FROM ai_feature_configs WHERE feature = %s FOR UPDATE",
        (feature,),
    ).fetchone()
    ciphertext = cipher.encrypt(api_key) if api_key else (current["api_key_ciphertext"] if current else None)
    if enabled and not ciphertext:
        raise FeatureConfigInputError("首次启用前请粘贴 API Key。")

    row = conn.execute(
        f"""
        INSERT INTO ai_feature_configs (
            feature, provider, project_id, model, enabled, auto_confirm_threshold,
            api_key_ciphertext, key_suffix, key_updated_at, key_updated_by_authorization_id
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s,
            CASE WHEN %s THEN NOW() ELSE NULL END,
            CASE WHEN %s THEN %s ELSE NULL END
        )
        ON CONFLICT (feature) DO UPDATE SET
            provider = EXCLUDED.provider,
            project_id = EXCLUDED.project_id,
            model = EXCLUDED.model,
            enabled = EXCLUDED.enabled,
            auto_confirm_threshold = EXCLUDED.auto_confirm_threshold,
            api_key_ciphertext = COALESCE(EXCLUDED.api_key_ciphertext, ai_feature_configs.api_key_ciphertext),
            key_suffix = COALESCE(EXCLUDED.key_suffix, ai_feature_configs.key_suffix),
            key_updated_at = CASE
                WHEN EXCLUDED.key_updated_at IS NULL THEN ai_feature_configs.key_updated_at
                ELSE EXCLUDED.key_updated_at
            END,
            key_updated_by_authorization_id = CASE
                WHEN EXCLUDED.key_updated_at IS NULL THEN ai_feature_configs.key_updated_by_authorization_id
                ELSE EXCLUDED.key_updated_by_authorization_id
            END,
            automatic_queue_checked_at = '-infinity'::timestamptz,
            last_tested_at = NULL,
            last_test_status = 'never',
            updated_at = NOW()
        RETURNING {_SAFE_COLUMNS}
        """,
        (
            feature,
            provider,
            project_id,
            model,
            enabled,
            auto_confirm_threshold,
            # PostgreSQL validates the proposed INSERT before ON CONFLICT;
            # retaining a key must satisfy enabled/key constraints at that point.
            ciphertext,
            api_key[-4:] if api_key else (current["key_suffix"] if current else None),
            bool(api_key),
            bool(api_key),
            updated_by_authorization_id,
        ),
    ).fetchone()
    assert row is not None
    return _safe_config(row)


def display_config(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Make a safe, small template view without copying encrypted fields."""

    values = row or {}
    enabled = bool(values.get("enabled"))
    status = values.get("last_test_status") or "never"
    suffix = values.get("key_suffix")
    last_tested_at = values.get("last_tested_at")
    key_updated_at = values.get("key_updated_at")
    return {
        "feature": values.get("feature"),
        "provider": values.get("provider") or "openai",
        "provider_label": "OpenAI" if values.get("provider", "openai") == "openai" else values.get("provider"),
        "project_id": values.get("project_id") or "",
        "model": values.get("model") or "",
        "auto_confirm_threshold": values.get("auto_confirm_threshold") or DEFAULT_AUTO_CONFIRM_THRESHOLD,
        "auto_confirm_threshold_label": (
            f"{float(values.get('auto_confirm_threshold') or DEFAULT_AUTO_CONFIRM_THRESHOLD):.0%}"
        ),
        "enabled": enabled,
        "enabled_label": "已启用" if enabled else "已停用",
        "enabled_badge": "success" if enabled else "secondary",
        "key_configured": bool(suffix),
        "key_suffix": suffix,
        "api_key_label": f"已配置 · …{suffix}" if suffix else "未配置",
        "key_updated_at": key_updated_at,
        "key_updated_at_label": _time_label(key_updated_at, "尚未配置"),
        "last_tested_at_label": _time_label(last_tested_at, "尚未测试"),
        "last_test_status_label": {"succeeded": "通过", "failed": "失败"}.get(status, "尚未测试"),
        "last_test_badge": {"succeeded": "success", "failed": "danger"}.get(status, "secondary"),
    }


def _normalize_api_key(value: str | None) -> str | None:
    if value is None or not (key := value.strip()):
        return None
    if len(key) < 12 or len(key) > 1024 or not key.isascii() or any(character.isspace() for character in key):
        raise FeatureConfigInputError("API Key 格式无效。")
    return key


def _normalize_auto_confirm_threshold(value: Decimal | float | str) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FeatureConfigInputError("自动确认阈值必须是 0.90 到 1.00 之间的数字。") from exc
    if (
        not threshold.is_finite()
        or threshold.as_tuple().exponent < -3
        or not Decimal("0.900") <= threshold <= Decimal("1.000")
    ):
        raise FeatureConfigInputError("自动确认阈值必须在 0.90 到 1.00 之间。")
    return threshold.quantize(Decimal("0.001"))


def _required_text(value: str, label: str, limit: int) -> str:
    text = value.strip()
    if not text:
        raise FeatureConfigInputError(f"{label}不能为空。")
    if len(text) > limit or "\x00" in text:
        raise FeatureConfigInputError(f"{label}格式无效。")
    return text


def _safe_config(row: Mapping[str, Any]) -> dict[str, Any]:
    return {column.strip(): row.get(column.strip()) for column in _SAFE_COLUMNS.split(",")}


def _time_label(value: datetime | None, fallback: str) -> str:
    return value.astimezone(_LONDON).strftime("%Y-%m-%d %H:%M %Z") if value else fallback
