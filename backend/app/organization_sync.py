"""Verified organization mapping from Lark Trust Party tenant facts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .config import Settings
from .db import connection
from .lark_api import LarkApiError, LarkClient

LOGGER = logging.getLogger("lark_message_stats.organization_sync")


def sync_if_due(settings: Settings, client: LarkClient) -> bool:
    """Refresh associated organizations without making message sync depend on it."""

    with connection() as conn:
        state = conn.execute(
            "SELECT status, checked_at FROM organization_sync_state WHERE singleton"
        ).fetchone()
    checked_at = state.get("checked_at") if state else None
    if (
        state
        and state.get("status") == "succeeded"
        and checked_at
        and checked_at > datetime.now(UTC) - timedelta(seconds=settings.organization_sync_interval_seconds)
    ):
        _refresh_known_links()
        return False

    try:
        token = client.tenant_access_token()
        organizations, visible_tenant_keys = _fetch_all(client, token)
    except LarkApiError as exc:
        status = "unavailable" if exc.likely_auth_problem else "failed"
        _record_state(
            status,
            "Lark 关联组织权限或可见性暂不可用。" if status == "unavailable" else "Lark 关联组织同步暂时未完成。",
        )
        LOGGER.info("organization sync did not run: status=%s code=%s", status, exc.lark_code)
        return False
    except RuntimeError:
        _record_state("unavailable", "Lark App Secret 尚未配置，无法同步关联组织。")
        return False

    _store_snapshot(organizations, visible_tenant_keys)
    return True


def verified_subject_organizations(conn: psycopg.Connection, contact_id: object) -> list[dict[str, str]]:
    """Return only compact, source-verified affiliation context for one person."""

    rows = conn.execute(
        """
        SELECT DISTINCT organization.canonical_name, organization.organization_kind,
               'tenant_affiliation' AS relationship
        FROM lark_identities identity
        JOIN organization_tenants mapping
          ON mapping.tenant_key = identity.tenant_key AND mapping.status = 'verified'
        JOIN organizations organization
          ON organization.id = mapping.organization_id AND organization.verification_status = 'verified'
        WHERE identity.contact_id = %s AND identity.identity_type = 'user'
        ORDER BY organization.canonical_name
        LIMIT 3
        """,
        (contact_id,),
    ).fetchall()
    return [
        {
            "name": row["canonical_name"],
            "kind": row["organization_kind"],
            "relationship": row["relationship"],
            "verification": "verified",
        }
        for row in rows
    ]


def _fetch_all(client: LarkClient, tenant_access_token: str) -> tuple[list[dict[str, str]], set[str]]:
    page_token: str | None = None
    result: list[dict[str, str]] = []
    visible_tenant_keys: set[str] = set()
    while True:
        payload = client.list_collaboration_tenants(tenant_access_token, page_token)
        result.extend(_organization_rows(payload))
        visible_tenant_keys.update(_organization_tenant_keys(payload))
        if not payload.get("has_more"):
            return result, visible_tenant_keys
        page_token = _text(payload.get("page_token"))
        if not page_token:
            raise LarkApiError(502, None, "Lark organization pagination did not return a continuation token")


def _organization_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = payload.get("collaboration_tenants")
    rows: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        tenant_key = _text(item.get("tenant_key"))
        name = _organization_name(item)
        if tenant_key and name:
            rows.append({"tenant_key": tenant_key, "name": name})
    return rows


def _organization_tenant_keys(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = payload.get("collaboration_tenants")
    if not isinstance(raw, list):
        return set()
    return {
        tenant_key
        for item in raw if isinstance(item, dict)
        if (tenant_key := _text(item.get("tenant_key")))
    }


def _organization_name(item: dict[str, Any]) -> str | None:
    for value in (item.get("name"), item.get("short_name"), item.get("tenant_name")):
        if isinstance(value, dict):
            for key in ("default_value", "en_us", "zh_cn"):
                text = _text(value.get(key))
                if text:
                    return text
        text = _text(value)
        if text:
            return text
    return None


def _store_snapshot(rows: list[dict[str, str]], visible_tenant_keys: set[str]) -> None:
    now = datetime.now(UTC)
    with connection() as conn, conn.transaction():
        for row in rows:
            organization = conn.execute(
                """
                INSERT INTO organizations (
                    canonical_name, verification_status, source, updated_at
                ) VALUES (%s, 'verified', 'lark_trust_party', %s)
                ON CONFLICT ((LOWER(canonical_name))) DO UPDATE SET
                    verification_status = 'verified', source = 'lark_trust_party', updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (row["name"], now),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO organization_tenants (
                    tenant_key, organization_id, source, status, verified_at, last_synced_at, updated_at
                ) VALUES (%s, %s, 'lark_trust_party', 'verified', %s, %s, %s)
                ON CONFLICT (tenant_key) DO UPDATE SET
                    organization_id = EXCLUDED.organization_id, source = EXCLUDED.source, status = EXCLUDED.status,
                    verified_at = EXCLUDED.verified_at, last_synced_at = EXCLUDED.last_synced_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (row["tenant_key"], organization["id"], now, now, now),
            )
        _reconcile_snapshot(conn, visible_tenant_keys, now)
        _link_known_identities(conn, now, refresh_existing=True)
        conn.execute(
            """
            UPDATE organization_sync_state
            SET status = 'succeeded', checked_at = %s, succeeded_at = %s,
                detail = %s, updated_at = %s
            WHERE singleton
            """,
            (now, now, f"已同步 {len(rows)} 个 Lark 关联组织。", now),
        )


def _refresh_known_links() -> None:
    """Attach recently discovered people to an already verified tenant map."""

    with connection() as conn, conn.transaction():
        _link_known_identities(conn, datetime.now(UTC), refresh_existing=False)


def _reconcile_snapshot(conn: psycopg.Connection, visible_tenant_keys: set[str], now: datetime) -> None:
    """Remove only stale Lark-derived links after one complete successful page walk."""

    conn.execute(
        """
        UPDATE organization_tenants
        SET status = 'rejected', updated_at = %s
        WHERE source = 'lark_trust_party' AND status = 'verified'
          AND NOT (tenant_key = ANY(%s))
        """,
        (now, list(visible_tenant_keys)),
    )
    conn.execute(
        """
        UPDATE contact_organization_links link
        SET status = 'rejected', last_seen_at = %s
        WHERE link.source = 'lark_trust_party' AND link.status = 'verified'
          AND NOT EXISTS (
              SELECT 1
              FROM lark_identities identity
              JOIN organization_tenants mapping
                ON mapping.tenant_key = identity.tenant_key
               AND mapping.organization_id = link.organization_id
               AND mapping.status = 'verified'
              WHERE identity.contact_id = link.contact_id
                AND identity.identity_type = 'user'
          )
        """,
        (now,),
    )


def _link_known_identities(conn: psycopg.Connection, now: datetime, *, refresh_existing: bool) -> None:
    """Use one set-based write; a tenant map is not an employment claim."""

    conflict = (
        """
        DO UPDATE SET status = 'verified', source = 'lark_trust_party', confidence = 1.000,
                      last_seen_at = EXCLUDED.last_seen_at
        """
        if refresh_existing
        else "DO NOTHING"
    )
    conn.execute(
        f"""
        INSERT INTO contact_organization_links (
            contact_id, organization_id, relationship, status, source, confidence, first_seen_at, last_seen_at
        )
        SELECT DISTINCT identity.contact_id, mapping.organization_id, 'tenant_affiliation', 'verified',
               'lark_trust_party', 1.000, %s, %s
        FROM lark_identities identity
        JOIN organization_tenants mapping
          ON mapping.tenant_key = identity.tenant_key AND mapping.status = 'verified'
        JOIN organizations organization
          ON organization.id = mapping.organization_id AND organization.verification_status = 'verified'
        WHERE identity.identity_type = 'user' AND identity.contact_id IS NOT NULL
        ON CONFLICT (contact_id, organization_id, relationship) {conflict}
        """,
        (now, now),
    )


def _record_state(status: str, detail: str) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE organization_sync_state
            SET status = %s, checked_at = NOW(), detail = %s, updated_at = NOW()
            WHERE singleton
            """,
            (status, detail),
        )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
