"""Read-only projection of Remi's real preparation roster for named reviewers."""

from uuid import UUID

from . import creator_preparation

REMI_ID = UUID("24d1ef13-3762-4359-ba11-d26c03cf1f7f")
ACCOUNT_FIELDS = (
    "handle", "display_name", "source_rows", "cm_values", "tap_cm_values", "source_group_names",
    "category", "category_label", "issue", "issue_label", "reason", "next_step", "is_stale",
    "contact_id", "suggested_contact_id",
)


def data() -> dict:
    """Expose roster metadata only; never infer a chat binding or read message bodies."""
    roster = creator_preparation.roster(REMI_ID)
    accounts = []
    for item in roster["items"]:
        account = {key: item[key] for key in ACCOUNT_FIELDS}
        account["can_view_conversations"] = bool(item["contact_id"] and not item["is_stale"])
        if item["is_stale"]:
            mapping_label = "历史整理记录已变化，当前映射待核对"
        elif item["contact_id"]:
            mapping_label = ("已有历史映射，身份关系仍待确认" if item["issue"] == "identity_risk"
                             else "当前正式关联 Remi")
        elif item["suggested_contact_id"]:
            mapping_label = "候选联系人，别名尚未确认"
        else:
            mapping_label = "联系人尚未确定"
        account["mapping_label"] = mapping_label
        accounts.append(account)
    summary = {**roster["summary"], **roster["formal_summary"]}
    summary.update({issue: sum(item["issue"] == issue for item in accounts)
                    for issue in ("unresolved", "alias_missing", "identity_risk")})
    return {
        "available": roster["available"], "responsible_name": roster.get("responsible_name") or "Remi",
        "responsible_id": str(REMI_ID), "source_filename": roster.get("source_filename", ""),
        "prepared_at_label": roster.get("prepared_at_label", ""), "summary": summary, "accounts": accounts,
    }
