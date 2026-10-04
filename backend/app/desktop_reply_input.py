"""Validate the production desktop schema 1.1 without treating labels as identity."""

from __future__ import annotations

import json
import re
from datetime import datetime
from uuid import UUID

MAX_REQUEST_BYTES = 1024 * 1024
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_HINTS = {"internal_id_hint", "sender_label_hint", "quoted_context_hint"}


class ReplyInputError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _object(value: object, required: set[str], optional: set[str], code: str) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= required | optional:
        raise ReplyInputError(code, "Required fields are missing or unsupported fields are present.")
    return value


def _uuid(value: object, code: str, field: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
    except ValueError:
        raise ReplyInputError(code, f"{field} must be a UUID string.") from None


def _timestamp(value: object, code: str) -> None:
    try:
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise ValueError
        if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ReplyInputError(code, "Capture time must be an ISO 8601 timestamp with a timezone.") from None


def _messages(value: object, code: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ReplyInputError(code, "Messages must be a non-empty array.")
    messages = []
    for index, item in enumerate(value, 1):
        item = _object(item, {"ordinal", "text"}, _HINTS, code)
        if type(item["ordinal"]) is not int or item["ordinal"] != index:
            raise ReplyInputError(code, "Message ordinals must start at one and increase consecutively.")
        if not isinstance(item["text"], str) or not item["text"].strip():
            raise ReplyInputError(code, "Each message must contain non-empty text.")
        for hint in _HINTS & item.keys():
            if not isinstance(item[hint], str) or len(item[hint]) > 4096:
                raise ReplyInputError(code, "Unverified text hints must be strings of at most 4096 characters.")
        messages.append(dict(item))
    return messages


def validate_request(payload: object) -> dict:
    """Return an independent, lossless request; labels, hints and text remain untrusted data."""
    payload = _object(payload, {
        "schema_version", "request_id", "captured_at", "conversation", "reply_scope",
        "selected_message_text", "selected_messages", "selection_source", "conversation_context",
    }, set(), "invalid_request")
    if payload["schema_version"] != "1.1":
        raise ReplyInputError("unsupported_schema_version", "schema_version must be '1.1'.")
    _uuid(payload["request_id"], "invalid_request_id", "request_id")
    _timestamp(payload["captured_at"], "invalid_captured_at")

    conversation = _object(payload["conversation"], {
        "label", "label_source", "identity_verified",
    }, set(), "invalid_conversation")
    if any(not isinstance(conversation[key], str) or not conversation[key].strip()
           for key in ("label", "label_source")):
        raise ReplyInputError("invalid_conversation", "Conversation label and label_source must be non-empty strings.")
    if conversation["identity_verified"] is not False:
        raise ReplyInputError("identity_must_be_unverified", "conversation.identity_verified must be false.")

    scope = payload["reply_scope"]
    if scope not in ("selected_messages", "recent_context"):
        raise ReplyInputError("invalid_reply_scope", "reply_scope is invalid.")
    expected_source = "clipboard_html" if scope == "selected_messages" else "none"
    if payload["selection_source"] != expected_source:
        raise ReplyInputError("invalid_selection_source", "selection_source must match the production reply scope.")
    text, selected = payload["selected_message_text"], payload["selected_messages"]
    if not isinstance(text, str) or not isinstance(selected, list):
        raise ReplyInputError("invalid_selection", "Selected-message fields have invalid types.")
    if scope == "recent_context":
        if text or selected:
            raise ReplyInputError("recent_context_must_be_empty",
                                  "recent_context requires empty selected-message fields.")
        selected = []
    else:
        selected = _messages(selected, "invalid_selected_message")
        if text != "\n".join(item["text"] for item in selected):
            raise ReplyInputError("selected_message_text_mismatch",
                                  "selected_message_text must join row text with newlines.")

    context = _object(payload["conversation_context"], {
        "capture_id", "captured_at", "source", "scope", "complete_history", "messages",
    }, set(), "invalid_context")
    _uuid(context["capture_id"], "invalid_context", "capture_id")
    _timestamp(context["captured_at"], "invalid_context")
    if (context["source"] != "lark_accessibility" or context["scope"] != "visible_text"
            or context["complete_history"] is not False):
        raise ReplyInputError("invalid_context",
                              "Context must be a partial visible-text snapshot from Lark accessibility.")
    context_messages = _messages(context["messages"], "invalid_context")

    result = {**payload, "conversation": dict(conversation), "selected_messages": selected,
              "conversation_context": {**context, "messages": context_messages}}
    try:
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except UnicodeEncodeError:
        raise ReplyInputError("invalid_json", "Request strings must contain valid UTF-8 text.") from None
    if size > MAX_REQUEST_BYTES:
        raise ReplyInputError("request_too_large", "Request body exceeds the 1 MiB limit.", status=413)
    return result
