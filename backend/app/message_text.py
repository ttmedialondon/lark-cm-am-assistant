"""Lark text extraction; rich display and conservative AI evidence remain distinct."""

import json
from typing import Any

from .message_mentions import resolve_mentions, rich_mention_text


def body_text_values(value: Any, mentions=None, names=None, *, rich=True) -> list[str]:
    texts: list[str] = []

    def collect(current):
        if isinstance(current, dict):
            if rich and current.get("tag") == "at":
                texts.append(rich_mention_text(current.get("user_id"), mentions, names))
                return
            title = current.get("title")
            if rich and isinstance(title, str) and title and isinstance(current.get("content"), list):
                texts.append(title)
            text = current.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            content = current.get("content")
            if rich and current.get("tag") in ("plain_text", "lark_md") and isinstance(content, str) and content:
                texts.append(content)
            for child in current.values():
                collect(child)
        elif isinstance(current, list):
            for child in current:
                collect(child)

    collect(value)
    return texts


def display_body(raw: str, message_type: str | None, mentions=None, names=None) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return resolve_mentions(raw, mentions, names)
    texts = body_text_values(parsed, mentions, names)
    if texts:
        return resolve_mentions("\n".join(texts), mentions, names)
    return f"（{message_type or '未知类型'} 消息没有可展示的文本）"


def evidence_body(raw: str, message_type: str | None) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    texts = body_text_values(parsed, rich=False)
    return "\n".join(texts) if texts else f"（{message_type or '未知类型'} 消息没有可展示的文本）"
