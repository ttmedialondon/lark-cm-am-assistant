"""Shared creator evidence helpers; no identity or ownership scanning."""

from __future__ import annotations

import hashlib
import json
import re

BODY_READ_SCOPES = frozenset({"im:chat:read", "im:message:readonly"})
BODY_READ_SCOPE_SQL = "a.scopes @> ARRAY['im:chat:read','im:message:readonly']::text[]"

BUSINESS = re.compile(
    r"\b(?:sample\w*|voucher\w*|campaign\w*|commission\w*|product\w*|sku|brand\w*|"
    r"live|livestream\w*|video\w*|boost\w*|affiliate\w*|collaborat\w*|agency|"
    r"payment\w*|invoice\w*|violation\w*|appeal\w*|account|manager|cm|"
    r"subsid(?:y|ies)|le|fs|gmv|flash\s+sale)\b"
    r"|达人|直播|视频|样品|补样|佣金|机构|签约|推品|投流|带货|选品|负责|交接|接手",
    re.I,
)

_READABLE = f"""FROM chat_messages m
    JOIN authorization_message_observations o ON o.chat_id=m.chat_id AND o.message_id=m.message_id
    JOIN authorizations a ON a.id=o.authorization_id
    JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=m.chat_id
    JOIN chats c ON c.chat_id=m.chat_id
    WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
      AND {BODY_READ_SCOPE_SQL}
      AND o.observed_at>=a.body_consent_at AND ac.is_visible AND ac.sync_enabled
      AND ac.sync_status IS DISTINCT FROM 'not_readable'
      AND COALESCE(c.chat_status,'normal')='normal' AND NOT m.is_deleted
      AND m.body_ciphertext IS NOT NULL"""

_COLUMNS = "m.*,o.authorization_id,o.first_observed_at,c.chat_mode,ac.p2p_counterpart_open_id"

def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()

def _relation_hash(row):
    return _hash({key: row.get(key) for key in (
        "sender_open_id", "sender_type", "message_type", "message_create_time", "parent_id", "root_id",
        "thread_id", "mentions_json", "chat_mode", "p2p_counterpart_open_id",
    )})
