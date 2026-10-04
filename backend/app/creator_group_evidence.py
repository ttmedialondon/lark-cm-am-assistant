"""One canonical group's purpose evidence, using existing current-grant checks."""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
from pathlib import Path

from . import creator_handle_evidence as source
from . import evidence_access as access
from . import evidence_text as text
from .crypto import message_body_cipher
from .openai_client import ModelOutputError

CLASSIFICATION_VERSION = "creator-group-purpose-v1.1"
PROMPT_VERSION = "creator-group-purpose-v1.3"
COMPATIBLE_PROMPT_VERSIONS = frozenset({
    CLASSIFICATION_VERSION, "creator-group-purpose-v1.2", PROMPT_VERSION,
})
PURPOSES = {"support": "客服群", "community": "机构达人群", "training": "培训群",
            "category": "类目群", "campaign": "活动群", "unknown": "未确认"}
MAX_MESSAGES, MAX_CHARS, SEARCH_LIMIT = 64, 24_000, 4_000
_RETENTION_PLACEHOLDER = "The message has exceeded the retention period and has been deleted."


@lru_cache(maxsize=1)
def instructions():
    return (Path(__file__).resolve().parents[2] / "docs/specs/creator-group-classification-prompt_v1.1.md"
            ).read_text().split("```text\n", 1)[1].split("```", 1)[0].strip()


def state(conn, chat_id):
    """Stable facts only: refresh timestamps/token rotation are not a purpose change."""
    chat = conn.execute("SELECT chat_id,name,COALESCE(chat_mode,'group') AS chat_mode,external,"
                        "COALESCE(chat_status,'normal') AS chat_status FROM chats WHERE chat_id=%s",
                        (chat_id,)).fetchone()
    grants = conn.execute("""SELECT a.id,a.open_id,a.status,a.body_consent_at,a.scopes,
        ac.is_visible,ac.sync_enabled,ac.sync_generation,ac.member_count,ac.member_count_status,
        ac.member_snapshot_status,ac.backfill_complete
        FROM authorization_chats ac JOIN authorizations a ON a.id=ac.authorization_id
        WHERE ac.chat_id=%s ORDER BY a.id""", (chat_id,)).fetchall()
    members = conn.execute("""SELECT cm.open_id,cm.member_type,i.contact_id,i.identity_type,
        i.tenant_classification,EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=i.contact_id
          AND r.role='creator' AND r.status='confirmed') AS creator,
        ARRAY(SELECT r.role FROM contact_roles r WHERE r.contact_id=i.contact_id
          AND r.status='confirmed' ORDER BY r.role) AS roles
        FROM chat_members cm LEFT JOIN lark_identities i ON i.open_id=cm.open_id
        WHERE cm.chat_id=%s ORDER BY cm.open_id""", (chat_id,)).fetchall()
    related = conn.execute("""SELECT DISTINCT i.contact_id FROM lark_identities i
        JOIN contact_roles r ON r.contact_id=i.contact_id AND r.role='creator' AND r.status='confirmed'
        WHERE i.contact_id<>ALL(%s::uuid[]) AND (EXISTS(SELECT 1 FROM chat_members cm
          WHERE cm.chat_id=%s AND cm.open_id=i.open_id) OR EXISTS(SELECT 1 FROM chat_messages m
          WHERE m.chat_id=%s AND m.sender_open_id=i.open_id AND NOT m.is_deleted))
        ORDER BY i.contact_id""", (sorted(source.RESTRICTED_CONTACTS), chat_id, chat_id)).fetchall()
    return {"chat": chat, "grants": grants, "members": members, "creators": related}


def _active_grants(snapshot):
    return [g for g in snapshot["grants"] if g["status"] == "active" and g["body_consent_at"] is not None
            and g["is_visible"] and g["sync_enabled"]]


def build(conn, settings, chat_id):
    snapshot = state(conn, chat_id)
    empty = {"pack": None, "refs": [], "stats": {}, "fingerprint": None}
    chat = snapshot["chat"]
    if not chat or chat["chat_mode"] == "p2p" or chat["chat_status"] != "normal":
        return {**empty, "reason": "not_group"}
    if not snapshot["creators"]:
        return {**empty, "reason": "no_creators"}
    grants = _active_grants(snapshot)
    if not grants:
        return {**empty, "reason": "no_authorization"}
    recent = source._rows(conn, "m.chat_id=%s", (chat_id,), SEARCH_LIMIT + 1)
    # Fetch original early context too; a recent promotional burst is not the group's founding purpose.
    earliest = conn.execute("SELECT DISTINCT m.message_id,m.message_create_time " + source._COMPANY_READABLE
        + " AND m.chat_id=%s AND m.message_type IN ('text','post') "
          "ORDER BY m.message_create_time ASC NULLS LAST,m.message_id LIMIT 24", (chat_id,)).fetchall()
    early = source._rows(conn, "m.chat_id=%s AND m.message_id=ANY(%s::text[])",
                         (chat_id, [r["message_id"] for r in earliest]), 24) if earliest else []
    rows = {source._key(r): r for r in [*early, *recent[:SEARCH_LIMIT]]}
    cipher, material, placeholders = message_body_cipher(settings.token_encryption_key), {}, set()

    def read(values):
        for row in values:
            raw = text._decrypt_body(cipher, row["body_ciphertext"])
            body = source._plain(raw, row) if raw is not None else ""
            if body.strip() == _RETENTION_PLACEHOLDER:
                placeholders.add(source._key(row))
                continue
            if body and "消息没有可展示的文本" not in body:
                material[source._key(row)] = {"row": row, "text": body, "body_hash": access._hash(raw)}

    read(rows.values())
    looked_up = set(rows)
    ordered = sorted(material.values(), key=lambda i: (str(i["row"]["message_create_time"]), *source._key(i["row"])))
    count = len(ordered)
    # Chronological strata, not keyword pre-filtering. Every message can be an anchor.
    indices = sorted({*range(min(4, count)), *range(max(0, count - 8), count),
                      *(round(n * (count - 1) / 23) for n in range(24) if count)})
    anchors = [ordered[n] for n in indices]
    predecessors = source._previous_ids(conn, [i["row"] for i in anchors])
    parent_ids = {i["row"]["parent_id"] for i in anchors if i["row"].get("parent_id")}
    context_ids = sorted(parent_ids | {mid for mid in predecessors.values() if mid})
    read(source._rows(conn, "m.chat_id=%s AND m.message_id=ANY(%s::text[])",
                      (chat_id, context_ids), len(context_ids)) if context_ids else [])
    looked_up.update((chat_id, message_id) for message_id in context_ids)
    selected, seen, characters = [], set(), 0
    for anchor in [*anchors[-8:], *anchors[:4], *anchors]:
        row = anchor["row"]
        keys = [(chat_id, row.get("parent_id")), (chat_id, predecessors.get(source._key(row))), source._key(row)]
        bundle = list({k: material[k] for k in keys if k in material and k not in seen}.values())
        length = sum(len(i["text"]) for i in bundle)
        if len(selected) + len(bundle) > MAX_MESSAGES or characters + length > MAX_CHARS:
            continue  # Keep full original messages and their referenced question; never manufacture a reply chain.
        selected.extend(bundle)
        seen.update(source._key(i["row"]) for i in bundle)
        characters += length
    # A sampled reply or predecessor can itself reply to an older message. Complete every selected
    # parent chain while the same model-input bounds still hold; partial chains remain background only.
    edge_failures = {}
    for root in [source._key(i["row"]) for i in selected]:
        current, trail = root, set()
        while current not in trail:
            trail.add(current)
            row = material[current]["row"]
            if not row.get("parent_id"):
                break
            parent = (chat_id, row["parent_id"])
            if parent in trail:  # Completeness is resolved below so all descendants inherit the cycle.
                break
            if parent not in material and parent not in looked_up:
                read(source._rows(conn, "m.chat_id=%s AND m.message_id=ANY(%s::text[])",
                                  (chat_id, [row["parent_id"]]), 1))
                looked_up.add(parent)
            if parent not in material:
                edge_failures[current] = "unavailable"
                break
            if parent not in seen:
                item = material[parent]
                if len(selected) >= MAX_MESSAGES or characters + len(item["text"]) > MAX_CHARS:
                    edge_failures[current] = "budget"
                    break
                selected.append(item)
                seen.add(parent)
                characters += len(item["text"])
            current = parent

    selected.sort(key=lambda i: (str(i["row"]["message_create_time"]), *source._key(i["row"])))
    selected_by_key = {source._key(i["row"]): i for i in selected}
    context_status = {}
    for start in selected_by_key:
        current, path, path_set = start, [], set()
        while current not in path_set:
            if current in context_status:
                reason = context_status[current]
                break
            path.append(current)
            path_set.add(current)
            if current in edge_failures:
                reason = edge_failures[current]
                break
            row = selected_by_key[current]["row"]
            if not row.get("parent_id"):
                reason = None
                break
            parent = (chat_id, row["parent_id"])
            if parent not in selected_by_key:
                reason = "unavailable"
                break
            current = parent
        else:
            reason = "cycle"
        for key in path:
            context_status[key] = reason
    stats = {"searched_messages": len(rows), "readable_messages": len(material), "included_messages": len(selected),
             "omitted_messages": max(0, len(material) - len(selected)), "history_truncated": len(recent) > SEARCH_LIMIT,
             "placeholder_messages": len(placeholders),
             "selected_characters": characters, "related_creators": len(snapshot["creators"])}
    if not selected:
        return {**empty, "reason": "no_text" if not material else "material_limit", "stats": stats}
    aliases = {oid: f"U{n}" for n, oid in enumerate(sorted({i["row"]["sender_open_id"] or "unknown"
                                                         for i in selected}), 1)}
    identity_rows = conn.execute("SELECT " + source._SPEAKER_COLUMNS
        + " FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id", (list(aliases),)).fetchall()
    identities = {r["open_id"]: r for r in identity_rows}
    messages, refs = [], [{"kind": "group_state", "chat_id": chat_id, "state_hash": access._hash(snapshot)},
                         {"kind": "chat", "chat_id": chat_id, "name_hash": access._hash(chat["name"])}]
    labels = {source._key(i["row"]): f"M{n}" for n, i in enumerate(selected, 1)}
    for oid, alias in aliases.items():
        identity = identities.get(oid)
        refs.append({"kind": "person", "label": alias, "contact_id": None,
                     "identities": [identity] if identity else [],
                     "identity_hash": access._hash([identity] if identity else [])})
    missing, missing_reasons = [], {}
    creator_ids = {str(r["contact_id"]) for r in snapshot["creators"]}
    for item in selected:
        row = item["row"]
        label, identity = labels[source._key(row)], identities.get(row["sender_open_id"])
        message = {"id": label, "speaker": aliases[row["sender_open_id"] or "unknown"],
                   "role": "internal" if identity and identity["tenant_classification"] == "internal" else
                           "creator" if identity and str(identity["contact_id"]) in creator_ids else
                           "bot" if row.get("sender_type") in {"app", "bot"} else "external_or_unknown",
                   "at": str(row["message_create_time"]), "text": item["text"]}
        if context_status[source._key(row)] is not None:
            message["context_complete"] = False
            missing.append(label)
            missing_reasons[label] = context_status[source._key(row)]
        if row.get("parent_id"):
            message["reply"] = labels.get((chat_id, row["parent_id"]))
        if source._key(row) in predecessors:
            message["previous"] = labels.get((chat_id, predecessors[source._key(row)]))
        messages.append(message)
        refs.append({"kind": "message", "label": label, "chat_id": chat_id, "message_id": row["message_id"],
                     "authorization_id": str(row["authorization_id"]), "scope_hash": source._scope_hash(row),
                     "body_hash": item["body_hash"], "ciphertext_hash": access._hash(row["body_ciphertext"]),
                     "relation_hash": access._relation_hash(row)})
        if source._key(row) in predecessors:
            refs[-1]["previous_id"] = predecessors[source._key(row)]
    structure = Counter("creator" if m["creator"] else "internal" if m["tenant_classification"] == "internal"
                        else "bot" if m["member_type"] != "user" else "unconfirmed" for m in snapshot["members"])
    complete = [g for g in grants if g["member_snapshot_status"] == "succeeded"
                and g["member_count_status"] == "succeeded"]
    counts = {int(g["member_count"]) for g in complete if g["member_count"] is not None}
    role_counts = Counter(role for member in snapshot["members"] for role in member["roles"])
    stats.update(missing_reply_ids=missing, backfill_complete=any(g["backfill_complete"] for g in grants))
    if missing_reasons:
        stats["missing_reply_reasons"] = missing_reasons
    pack = {"chat": {"id": "G1", "name": chat["name"] or "未命名群", "mode": chat["chat_mode"],
                     "external": chat["external"], "member_count": next(iter(counts)) if len(counts) == 1 else None,
                     "known_creators": structure["creator"], "internal_members": structure["internal"],
                     "unknown_members": structure["unconfirmed"], "member_snapshot_complete": bool(complete)},
            "known_member_roles": dict(role_counts),
            "messages": messages, "coverage": {**stats, "missing_reply_ids": missing,
                "backfill_complete": any(g["backfill_complete"] for g in grants)}}
    return {"pack": pack, "refs": refs, "stats": stats, "reason": "ready",
            "fingerprint": access._hash([CLASSIFICATION_VERSION, pack, access._hash(snapshot)])}


def still_valid(conn, refs, settings, *, lightweight=False):
    group = [r for r in refs if r.get("kind") == "group_state"]
    if len(group) != 1:
        return False
    snapshot = state(conn, group[0]["chat_id"])
    if access._hash(snapshot) != group[0]["state_hash"] or not _active_grants(snapshot) or not snapshot["creators"]:
        return False
    material_refs = [r for r in refs if r.get("kind") != "group_state"]
    if lightweight and source.still_valid(conn, material_refs, settings, ciphertext_only=True):
        return True
    # Sync can re-encrypt identical plaintext. Verify content before invalidating a paid result.
    return source.still_valid(conn, material_refs, settings)


def output_format(pack):
    messages = {message["id"]: message for message in pack["messages"]}
    explicitly_incomplete = set(pack.get("coverage", {}).get("missing_reply_ids", []))
    explicitly_incomplete.update(message_id for message_id, message in messages.items()
                                 if message.get("context_complete") is False)
    completeness = {}
    for start in messages:
        current, path, path_set = start, [], set()
        while current not in path_set:
            if current in explicitly_incomplete:
                complete = False
                break
            if current in completeness:
                complete = completeness[current]
                break
            message = messages.get(current)
            if message is None:
                complete = False
                break
            path.append(current)
            path_set.add(current)
            if "reply" not in message:
                complete = True
                break
            current = message.get("reply")
        else:
            complete = False
        for message_id in path:
            completeness[message_id] = complete
    references = [pack["chat"]["id"], *(message_id for message_id in messages
                  if completeness.get(message_id, False))]
    schema = {"type": "object", "additionalProperties": False,
        "required": ["purpose", "confidence", "tags", "refs"], "properties": {
            "purpose": {"type": "string", "enum": list(PURPOSES)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "tags": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 80}},
            "refs": {"type": "array", "maxItems": 8, "items": {"type": "string", "enum": references}}}}
    return {"type": "json_schema", "name": "creator_group_purpose_v2", "strict": True, "schema": schema}


def validate(pack, result):
    schema = output_format(pack)["schema"]
    # Paid v1.1/v1.2 results may cite a now-known incomplete message. Keep that citation and score
    # visible to the admission fence; only reject references that are not actual material at all.
    schema["properties"]["refs"]["items"]["enum"] = [
        pack["chat"]["id"], *(message["id"] for message in pack["messages"]),
    ]
    legacy = set(schema["required"]) | {"reason"}
    if isinstance(result, dict) and set(result) == legacy:
        if not isinstance(result["reason"], str) or len(result["reason"]) > 600:
            raise ModelOutputError("reason")
        result = {key: value for key, value in result.items() if key != "reason"}
    source._shape(result, schema)
    if len(result["tags"]) > 8 or any(not t.strip() or len(t) > 80 for t in result["tags"]):
        raise ModelOutputError("tags")
    if len(result["refs"]) > 8:
        raise ModelOutputError("bounds")
    result = {**result, "tags": list(dict.fromkeys(t.strip() for t in result["tags"])),
              "refs": list(result["refs"])}
    if result["purpose"] != "unknown" and not result["refs"]:
        result["purpose"] = "unknown"
    return result
