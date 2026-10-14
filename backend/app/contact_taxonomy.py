"""One small vocabulary for contact classification across worker, AI, and UI."""

from __future__ import annotations

CONTACT_ROLE_CODES = frozenset(
    {
        "creator",
        "partner_contact",
        "internal_colleague",
        "merchant_contact",
        "platform_contact",
        "other_external_contact",
    }
)
ROLE_LABELS = {
    "creator": "达人",
    "partner_contact": "其他机构负责人",
    "internal_colleague": "同事",
    "merchant_contact": "商家",
    "platform_contact": "字节官方",
    "other_external_contact": "其他人员",
    "insufficient": "待识别",
}
ROLE_ORDER = (
    "creator",
    "partner_contact",
    "internal_colleague",
    "merchant_contact",
    "platform_contact",
    "other_external_contact",
)

# Organization type is deliberately separate from a person's business role.
# Keep the vocabulary next to the contact taxonomy so model validation and the
# owner UI cannot drift from the database constraint.
ORGANIZATION_KINDS = frozenset(
    {
        "unknown",
        "mcn_or_agency",
        "merchant_or_brand",
        "platform",
        "partner_service",
        "other",
    }
)
ORGANIZATION_KIND_LABELS = {
    "mcn_or_agency": "MCN / 代理机构",
    "merchant_or_brand": "商家 / 品牌",
    "platform": "平台",
    "partner_service": "合作服务商",
    "other": "其他组织",
    "unknown": "类型未标注",
}


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", "待识别")
