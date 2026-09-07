"""Read-only, owner-facing import handoff; never changes creator membership."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from .admin_reporting import _format_time, _page_result, _page_window
from .db import connection

ACTION = "creator_handle_roster_prepared"
RISK_ACTION = "creator_handle_identity_review_flagged"
CATEGORIES = {"documented": "依据已归档", "archive_needed": "依据未完整归档", "decision_needed": "待你判断"}
ISSUES = {"none": "documented", "missing_archive": "archive_needed", "alias_missing": "decision_needed",
          "identity_risk": "decision_needed", "unresolved": "decision_needed"}
ISSUE_LABELS = {"none": "来源可追溯", "missing_archive": "归档字段待补", "alias_missing": "别名尚未正式记录",
                "identity_risk": "历史映射待确认", "unresolved": "联系人尚未确定"}
PAGE_SIZE = 20
PAYLOAD_FIELDS = {"version", "source_filename", "source_sha256", "prepared_at", "responsible_id",
                  "responsible_name", "items"}
ITEM_FIELDS = {"handle", "source_rows", "cm_values", "tap_cm_values", "source_group_names", "contact_id",
               "suggested_contact_id", "display_name", "category", "issue", "reason", "next_step",
               "existing_role_source", "expected_assignment_id", "expected_role_updated_at"}


def normalize_handle(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lstrip("@").casefold()
    return value if re.fullmatch(r"[\w.\-]{1,160}", value) else ""


def normalize_query(value: str) -> str:
    return " ".join(value.split()).lstrip("@").casefold()[:120]


def _date(value: Any) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return result


def parse_payload(payload: Any) -> dict[str, Any]:
    """Reject the whole snapshot on malformed or ambiguous input, never partial-import it."""
    if (not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("Unsupported preparation format")
    if (not isinstance(payload["source_filename"], str) or not 1 <= len(payload["source_filename"]) <= 200
            or "/" in payload["source_filename"] or "\\" in payload["source_filename"]
            or not re.fullmatch(r"[a-f0-9]{64}", str(payload["source_sha256"]))):
        raise ValueError("Invalid source reference")
    UUID(payload["responsible_id"])
    _date(payload["prepared_at"])
    if not isinstance(payload["responsible_name"], str) or not 1 <= len(payload["responsible_name"]) <= 160:
        raise ValueError("Invalid responsible label")
    if not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= 2000:
        raise ValueError("Invalid preparation size")
    handles = set()
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            raise ValueError("Invalid preparation item fields")
        handle = normalize_handle(item["handle"])
        if not handle or handle != item["handle"] or handle in handles:
            raise ValueError("Invalid or duplicate handle")
        handles.add(handle)
        if item["category"] not in CATEGORIES or ISSUES.get(item["issue"]) != item["category"]:
            raise ValueError("Invalid preparation category")
        refs = item["source_rows"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 100 or len(set(refs)) != len(refs)
                or any(type(n) is not int or not 2 <= n <= 1_000_000 for n in refs)):
            raise ValueError("Invalid source rows")
        for field, limit in (("cm_values", 160), ("tap_cm_values", 160), ("source_group_names", 500)):
            values = item[field]
            if (not isinstance(values, list) or len(values) > 100
                    or any(not isinstance(v, str) or len(v) > limit for v in values)):
                raise ValueError("Invalid source labels")
        for field, limit in (("display_name", 200), ("reason", 500), ("next_step", 300)):
            if not isinstance(item[field], str) or not 1 <= len(item[field]) <= limit:
                raise ValueError("Invalid preparation text")
        for field in ("contact_id", "suggested_contact_id", "expected_assignment_id"):
            if item[field] is not None:
                UUID(item[field])
        if item["contact_id"] and item["suggested_contact_id"]:
            raise ValueError("Suggested and existing mappings must be separate")
        if item["issue"] in {"none", "missing_archive", "identity_risk"} and not item["contact_id"]:
            raise ValueError("Existing mapping required")
        if item["issue"] in {"alias_missing", "unresolved"} and item["contact_id"]:
            raise ValueError("Unconfirmed mapping cannot be formal")
        if item["contact_id"] or item["suggested_contact_id"]:
            if (not item["expected_assignment_id"] or not isinstance(item["existing_role_source"], str)
                    or not 1 <= len(item["existing_role_source"]) <= 100):
                raise ValueError("Missing state binding")
            _date(item["expected_role_updated_at"])
        elif any(item[k] is not None for k in (
                "existing_role_source", "expected_assignment_id", "expected_role_updated_at")):
            raise ValueError("Unresolved row cannot claim a state binding")
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def binding_matches(item: dict[str, Any], row: dict[str, Any] | None, responsible_id: str) -> bool:
    try:
        return bool(row and str(row["id"]) == (item.get("contact_id") or item.get("suggested_contact_id"))
                    and item.get("expected_assignment_id")
                    and str(row["assignment_id"]) == item["expected_assignment_id"]
                    and str(row["responsible_id"]) == responsible_id
                    and row["role_source"] == item["existing_role_source"]
                    and _date(row["role_updated_at"]) == _date(item["expected_role_updated_at"]))
    except (KeyError, TypeError, ValueError):
        return False


def load_snapshot(conn: Any) -> dict[str, Any] | None:
    row = conn.execute("""
        SELECT metadata FROM audit_events WHERE action = %s ORDER BY created_at DESC, id DESC LIMIT 1
    """, (ACTION,)).fetchone()
    if not row:
        return None
    try:
        envelope = row["metadata"]
        payload = parse_payload(envelope["payload"])
        if payload_digest(payload) != envelope["payload_sha256"]:
            return None
        return payload
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def directory(page: int = 1, query: str = "", category: str = "all") -> dict[str, Any]:
    from . import creator_review

    query = normalize_query(query)
    category = category if category in CATEGORIES else "all"
    with connection() as conn:
        payload = load_snapshot(conn)
        rows = conn.execute(creator_review._CREATORS_SQL).fetchall() if payload else []
    result = {"available": bool(payload), "query": query, "category": category,
              "summary": {"total": 0, "documented": 0, "archive_needed": 0, "decision_needed": 0, "stale": 0}}
    items = []
    if payload:
        result.update({k: payload[k] for k in ("source_filename", "source_sha256", "responsible_name")})
        result["prepared_at_label"] = _format_time(_date(payload["prepared_at"]))
        current = {str(row["id"]): row for row in rows}
        current_handles = {h for row in rows for h in creator_review._evidence(row)["handles"]}
        for source in payload["items"]:
            item = dict(source)
            contact_id = item["contact_id"] or item["suggested_contact_id"]
            row = current.get(contact_id)
            stale = (not binding_matches(item, row, payload["responsible_id"]) if contact_id
                     else item["handle"] in current_handles)
            facts = creator_review._evidence(row) if row else None
            if item["contact_id"] and facts and item["handle"] not in facts["handles"]:
                stale = True
            if item["issue"] == "alias_missing" and item["handle"] in current_handles:
                stale = True
            if item["category"] == "documented" and (not facts or not facts["documented"]):
                stale = True
            item["is_stale"] = stale
            item["detail_url"] = (f"/admin/creator-review/{item['contact_id']}"
                                  if item["contact_id"] and row and not stale else None)
            if stale:
                item.update(category="decision_needed", reason="整理后正式记录已变化，需要重新核对当前状态。",
                            next_step="不要沿用这份旧建议；重新确认账号、联系人和负责人。")
            item["category_label"] = CATEGORIES[item["category"]]
            item["issue_label"] = "正式记录已变化" if stale else ISSUE_LABELS[item["issue"]]
            result["summary"][item["category"]] += 1
            result["summary"]["stale"] += int(stale)
            items.append(item)
        result["summary"]["total"] = len(items)
    items = [item for item in items if (category == "all" or item["category"] == category)
             and (not query or query in f"{item['handle']} {item['display_name']}".casefold())]
    order = {"decision_needed": 0, "archive_needed": 1, "documented": 2}
    items.sort(key=lambda item: (order[item["category"]], item["handle"]))
    page, _, offset = _page_window(page, PAGE_SIZE, total_count=len(items))
    return {**result, **_page_result(items[offset:offset + PAGE_SIZE], page, PAGE_SIZE, total_count=len(items))}
