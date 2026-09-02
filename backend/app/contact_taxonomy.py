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
AI_ROLE_CODES = CONTACT_ROLE_CODES - {"internal_colleague"}
MODEL_ROLE_CODES = AI_ROLE_CODES | {"insufficient"}

ROLE_LABELS = {
    "creator": "达人 / 内容创作者",
    "partner_contact": "合作机构对接人",
    "internal_colleague": "TT Media London 同事",
    "merchant_contact": "商家 / 品牌方对接人",
    "platform_contact": "字节系平台官方对接人",
    "other_external_contact": "其他已确认外部联系人",
    "insufficient": "信息不足",
}
ROLE_ORDER = (
    "creator",
    "partner_contact",
    "internal_colleague",
    "merchant_contact",
    "platform_contact",
    "other_external_contact",
)


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", "待识别")
