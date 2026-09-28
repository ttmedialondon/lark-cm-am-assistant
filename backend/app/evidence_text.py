"""Shared creator evidence helpers; no identity or ownership scanning."""

from __future__ import annotations

import re

from cryptography.exceptions import InvalidTag

from .message_text import evidence_body as _readable_body  # noqa: F401 - shared creator reader
from .openai_client import ModelOutputError as _ModelOutputError

_IMPORTANT = re.compile(
    r"creator|influencer|my (?:live|video|content|account|shop|brand|team)|"
    r"agency|mcn|manage|merchant|seller|brand|campaign|sample|commission|"
    r"tiktok|bytedance|official|colleague|employee|work for|our company|"
    r"达人|主播|直播|机构|品牌|商家|同事|官方|样品|佣金",
    re.I,
)

_ROUTINE = re.compile(
    r"^(?:(?:hi|hello|hey|thanks|thank you|okay|ok|yes|no|perfect|amazing|"
    r"sounds good|got it|good morning|good night|cheers|你好|谢谢|好的|收到)[\s!.,?🙏👍❤️]*)+$",
    re.I,
)

_EMAIL = re.compile(r"(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}")

_PHONE = re.compile(r"(?<![\w])\+?\d[\d ()-]{7,}\d(?![\w])")

_ADDRESS = re.compile(
    r"(?im)(?:\b(?:shipping|delivery|postal|home)?\s*address\s*[:：]|收货地址\s*[:：])[^\n]+"
    r"|\b\d{1,5}\s+(?:[A-Za-z'-]+\s+){0,5}(?:street|road|avenue|lane|drive|close|way|st|rd)\b[^\n,;]*"
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"
)

def _decrypt_body(cipher, ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return cipher.decrypt(ciphertext)
    except (InvalidTag, UnicodeDecodeError, ValueError):
        return None

def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise _ModelOutputError("confidence")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise _ModelOutputError("confidence") from exc
    if not 0 <= confidence <= 1:
        raise _ModelOutputError("confidence")
    return confidence
