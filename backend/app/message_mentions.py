"""Render Lark mentions from that message's metadata, never from a name guess."""

from __future__ import annotations

import json
import re

_KEY = re.compile(r"@_user_\d+\Z")
# Lark placeholders can touch Chinese prose; only ASCII identifier characters
# make them part of another token.
_TOKEN = re.compile(r"(?<![\w@])@_user_\d+(?![\w@])", re.ASCII)
# Message text may quote source code or contain URLs/email addresses. Those are
# literal content, not a Lark mention to rewrite.
_LITERAL = re.compile(
    r"```[\s\S]*?(?:```|\Z)|`[^`\n]*`|"
    r"(?a:\b)[a-z][a-z0-9+.-]*://[^\s<>]+|(?a:\b)(?:www\.|mailto:)[^\s<>]+|"
    r"(?a:[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+)",
    re.IGNORECASE,
)


def _items(mentions):
    if isinstance(mentions, str):
        try:
            mentions = json.loads(mentions)
        except ValueError:
            return []
    return [item for item in mentions if isinstance(item, dict)] if isinstance(mentions, list) else []


def _name(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value if value and not _KEY.fullmatch(value) else None


def _identity(item):
    value = item.get("id")
    return value if isinstance(value, str) and value and value == value.strip() else ""


def mention_targets(mentions, names=None):
    """Return key/open_id/name/ambiguous records; IDs require explicit open_id.

    Duplicate keys with different targets remain unresolved, even if both
    people have the same name. Raw mention names are display fallback only.
    """
    grouped = {}
    for item in _items(mentions):
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            continue
        grouped.setdefault(key, []).append(item)
    result = []
    for key, items in grouped.items():
        identities = {(str(i.get("id_type") or ""), _identity(i)) for i in items}
        ambiguous = len(identities) != 1
        id_type, identity = next(iter(identities))
        open_id = identity if not ambiguous and id_type == "open_id" and identity else None
        name = _name((names or {}).get(open_id)) if open_id else None
        if not ambiguous and not name:
            fallbacks = {name for i in items if (name := _name(i.get("name")))}
            name = next(iter(fallbacks)) if len(fallbacks) == 1 else None
        result.append({"key": key, "open_id": open_id, "name": name, "ambiguous": ambiguous})
    return result


def mention_names(conn, rows):
    """Call ONLY after authorizing rows. Read labels for their explicit mentions.

    This is not a directory search and grants no access to anyone's messages.
    No query is made for absent or non-open-id metadata.
    """
    ids = sorted(
        {
            _identity(item)
            for row in rows
            for item in _items(row.get("mentions_json"))
            if item.get("id_type") == "open_id" and _identity(item)
        }
    )
    if not ids:
        return {}
    return {
        row["open_id"]: row["display_name"]
        for row in conn.execute(
            """SELECT open_id,display_name FROM lark_identities
               WHERE open_id=ANY(%s::text[]) AND identity_type='user' FOR SHARE""",
            (ids,),
        ).fetchall()
        if _name(row["display_name"])
    }


def resolve_mentions(text, mentions, names=None):
    """Replace exact, unambiguous placeholders in prose; preserve unknown ones."""
    replacements = {
        item["key"]: "@" + item["name"].lstrip("@")
        for item in mention_targets(mentions, names)
        if item["name"] and not item["ambiguous"]
    }
    if not replacements:
        return text
    parts, offset = [], 0
    for literal in _LITERAL.finditer(text):
        parts.extend((_TOKEN.sub(lambda m: replacements.get(m[0], m[0]), text[offset : literal.start()]), literal[0]))
        offset = literal.end()
    parts.append(_TOKEN.sub(lambda m: replacements.get(m[0], m[0]), text[offset:]))
    return "".join(parts)


def rich_mention_text(user_id, mentions, names=None):
    """Render a rich-post at tag only when its ID matches this message's metadata."""
    targets = [item for item in mention_targets(mentions, names) if item["open_id"] == user_id and user_id]
    labels = {item["name"] for item in targets if item["name"] and not item["ambiguous"]}
    if len(labels) == 1:
        return "@" + next(iter(labels)).lstrip("@")
    return targets[0]["key"] if targets else "[未解析的@用户]"
