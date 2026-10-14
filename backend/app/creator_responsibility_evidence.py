"""Bounded, authorized evidence for independent, multi-CM responsibility suggestions."""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from . import creator_handle_evidence as source
from . import evidence_access as access
from . import evidence_text as text
from .crypto import message_body_cipher
from .message_mentions import mention_targets
from .openai_client import ModelOutputError

PROMPT_VERSION = "creator-responsibility-v1.3"
MAX_CANDIDATES, MAX_MESSAGES, MAX_CHARS, SEARCH_LIMIT = 24, 96, 32_000, 20_000
DECISIONS = ("responsible", "strong_candidate", "assist", "historical", "insufficient")
PERIODS = ("current", "historical", "uncertain")
SCOPES = ("account_ops", "content_live", "samples_products", "commercial", "technical_support", "other", "unknown")
# Retrieval priority only: wording is never a server-side semantic acceptance gate.
_CHANGE = re.compile(
    r"\b(?:handover|handing\s+over|take\s+over|taking\s+over|no\s+longer|left\s+the|"
    r"unlink|unlinked|new\s+manager|new\s+contact|responsible|in\s+charge)\b|"
    r"交接|接手|不再负责|离职|解绑|解除|负责人|负责对接",
    re.I,
)


@lru_cache(maxsize=1)
def instructions():
    document = (Path(__file__).resolve().parents[2] / "docs/specs/creator-responsibility-matching_v1.0.md").read_text()
    return document.split("```text\n", 1)[1].split("```", 1)[0].strip()


def output_format(pack=None):
    def strings(values=None, limit=8):
        item = {"type": "string"}
        if values is not None:
            item["enum"] = list(values)
        return {"type": "array", "items": item, "maxItems": limit}

    fields = {
        "cm": {"type": "string"},
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "period": {"type": "string", "enum": list(PERIODS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "relation_level": {"type": "string", "enum": ["person", "account", "unknown"]},
        "scope": strings(SCOPES, 4),
        "scope_note": {"type": "string", "maxLength": 120},
        "support_refs": strings(),
        "counter_refs": strings(),
        "reason": {"type": "string", "maxLength": 240},
    }
    suggestions = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
    }
    if pack is not None:
        fields["cm"]["enum"] = [c["id"] for c in pack["candidates"]]
        for name in ("support_refs", "counter_refs"):
            fields[name]["items"]["enum"] = [m["id"] for m in pack["messages"]]
        suggestions.update(minItems=len(pack["candidates"]), maxItems=len(pack["candidates"]))
    return {
        "type": "json_schema",
        "name": "creator_responsibility_v1",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"suggestions": suggestions},
            "required": ["suggestions"],
            "additionalProperties": False,
        },
    }


def _state(conn, contact_id):
    """Only stable directory/assignment facts; never credentials or inferred job titles."""
    directory = conn.execute(
        """SELECT c.id AS contact_id,c.display_name,i.open_id,i.identity_type,i.tenant_classification
           FROM contacts c JOIN lark_identities i ON i.contact_id=c.id
           WHERE i.identity_type='user' AND EXISTS (SELECT 1 FROM lark_identities admitted
               JOIN authorizations a ON a.open_id=admitted.open_id
               WHERE admitted.contact_id=c.id AND admitted.identity_type='user'
                 AND a.status='active' AND a.body_consent_at IS NOT NULL)
           ORDER BY c.id,i.open_id"""
    ).fetchall()
    directory = [r for r in directory if str(r["contact_id"]) not in source.RESTRICTED_CONTACTS]
    authorizations = conn.execute(
        """SELECT a.id,a.open_id,a.status,a.body_consent_at,i.contact_id FROM authorizations a
           LEFT JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
           ORDER BY a.id"""
    ).fetchall()
    # Ended legacy relationships are manual detachments, not active responsibility fallbacks.
    assignments = conn.execute(
        """SELECT 'person' AS level,id,responsible_authorization_id,source,started_at,ended_at,
             ended_by_authorization_id,NULL::uuid AS account_id,
             NULL::text AS handle,NULL::text AS platform
           FROM contact_assignments WHERE contact_id=%s
           UNION ALL SELECT CASE WHEN a.social_account_id IS NULL THEN 'person' ELSE 'account' END,
             a.id,a.responsible_authorization_id,a.source,a.started_at,a.ended_at,
             NULL::uuid AS ended_by_authorization_id,a.social_account_id,
             s.normalized_handle,s.platform
           FROM creator_account_assignments a LEFT JOIN social_accounts s ON s.id=a.social_account_id
           WHERE a.contact_id=%s AND a.ended_at IS NOT NULL
           ORDER BY level,id""",
        (contact_id, contact_id),
    ).fetchall()
    role = conn.execute(
        "SELECT contact_id,role,status,source FROM contact_roles WHERE contact_id=%s AND role='creator' ORDER BY role",
        (contact_id,),
    ).fetchall()
    return {"directory": directory, "authorizations": authorizations, "assignments": assignments, "role": role}


def build(conn, settings, contact_id):
    """One known creator, relevant enrolled CMs, shared authorized messages."""
    contact_id = str(contact_id)
    empty = {"pack": None, "candidate_ids": {}, "refs": [], "fingerprint": None, "stats": {}, "reason": ""}
    if contact_id in source.RESTRICTED_CONTACTS:
        return {**empty, "reason": "restricted"}
    state = _state(conn, contact_id)
    if not any(r["status"] == "confirmed" for r in state["role"]):
        return {**empty, "reason": "not_creator"}
    identities = conn.execute(
        "SELECT " + source._SPEAKER_COLUMNS + " FROM lark_identities "
        "WHERE contact_id=%s AND identity_type='user' ORDER BY open_id",
        (contact_id,),
    ).fetchall()
    if not identities or any(i["tenant_classification"] == "internal" for i in identities):
        return {**empty, "reason": "no_identity"}
    target_ids = [i["open_id"] for i in identities]
    roster = defaultdict(list)
    for row in state["directory"]:
        if str(row["contact_id"]) != contact_id:
            roster[str(row["contact_id"])].append(row)
    staff_ids = {r["open_id"]: cid for cid, rows in roster.items() for r in rows}
    authorization_people = {str(a["id"]): str(a["contact_id"]) for a in state["authorizations"] if a["contact_id"]}
    known = {
        authorization_people[str(a["responsible_authorization_id"])]
        for a in state["assignments"]
        if a["ended_at"] is None and authorization_people.get(str(a["responsible_authorization_id"])) in roster
    }
    chats = conn.execute(
        """SELECT DISTINCT c.chat_id,c.name,c.chat_mode FROM chats c
           JOIN authorization_chats ac ON ac.chat_id=c.chat_id JOIN authorizations a ON a.id=ac.authorization_id
           WHERE a.status='active' AND a.body_consent_at IS NOT NULL AND ac.is_visible AND ac.sync_enabled
             AND COALESCE(c.chat_status,'normal')='normal' AND (
               ac.p2p_counterpart_open_id=ANY(%s::text[]) OR EXISTS (
                 SELECT 1 FROM chat_members cm WHERE cm.chat_id=c.chat_id AND cm.open_id=ANY(%s::text[]))
               OR EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id=c.chat_id
                 AND m.sender_open_id=ANY(%s::text[]))) ORDER BY c.chat_id""",
        (target_ids, target_ids, target_ids),
    ).fetchall()
    chat_ids = [c["chat_id"] for c in chats]
    rows = (
        source._rows(
            conn,
            "m.chat_id=ANY(%s::text[]) AND m.sender_open_id=ANY(%s::text[])",
            (chat_ids, target_ids),
            SEARCH_LIMIT + 1,
        )
        if chat_ids
        else []
    )
    other = (
        source._rows(
            conn,
            "m.chat_id=ANY(%s::text[]) AND NOT (m.sender_open_id=ANY(%s::text[]))",
            (chat_ids, target_ids),
            SEARCH_LIMIT + 1,
        )
        if chat_ids
        else []
    )
    history_truncated = len(rows) > SEARCH_LIMIT or len(other) > SEARCH_LIMIT
    rows = rows[:SEARCH_LIMIT] + other[:SEARCH_LIMIT]
    cipher = message_body_cipher(settings.token_encryption_key)
    material = {}

    def read(part):
        for row in part:
            raw = text._decrypt_body(cipher, row["body_ciphertext"])
            body = source._plain(raw, row) if raw is not None else ""
            if body and "消息没有可展示的文本" not in body:
                material[source._key(row)] = {"row": row, "text": body, "body_hash": access._hash(raw)}

    read(rows)
    parent_ids = sorted({r["parent_id"] for r in rows if r.get("parent_id")})
    if parent_ids:
        read(
            source._rows(
                conn,
                "m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])",
                (chat_ids, parent_ids),
                SEARCH_LIMIT + 1,
            )
        )
    own_times = defaultdict(list)
    for item in material.values():
        row = item["row"]
        if row["sender_open_id"] in target_ids and row["message_create_time"]:
            own_times[row["chat_id"]].append(row["message_create_time"].timestamp())
    for values in own_times.values():
        values.sort()

    def close_to_target(row):
        times = own_times[row["chat_id"]]
        if not times or row["message_create_time"] is None:
            return False
        at = row["message_create_time"].timestamp()
        index = bisect_left(times, at)
        return any(abs(times[i] - at) <= 300 for i in (index - 1, index) if 0 <= i < len(times))

    scores = Counter({cid: 100 for cid in known})
    related = {}
    for key, item in material.items():
        row = item["row"]
        speaker = staff_ids.get(row["sender_open_id"])
        parent = material.get((row["chat_id"], row.get("parent_id")), {}).get("row", {})
        mentioned = {
            staff_ids[t["open_id"]]
            for t in mention_targets(row.get("mentions_json"))
            if not t["ambiguous"] and t["open_id"] in staff_ids
        }
        target_mentioned = any(
            t["open_id"] in target_ids and not t["ambiguous"] for t in mention_targets(row.get("mentions_json"))
        )
        direct = row["chat_mode"] == "p2p" or target_mentioned or parent.get("sender_open_id") in target_ids
        relevant = row["sender_open_id"] in target_ids or direct or close_to_target(row)
        candidates = set()
        if relevant and speaker:
            candidates.add(speaker)
        if relevant:
            candidates.update(mentioned)
            if parent.get("sender_open_id") in staff_ids:
                candidates.add(staff_ids[parent["sender_open_id"]])
        for cid in candidates:
            scores[cid] += 3 if direct else 1
        if candidates or row["sender_open_id"] in target_ids:
            related[key] = candidates
    candidate_contacts = sorted(scores, key=lambda cid: (cid not in known, -scores[cid], cid))[:MAX_CANDIDATES]
    stats = {
        "internal_candidates": len(roster),
        "candidate_count": len(scores),
        "candidates_omitted": max(0, len(scores) - len(candidate_contacts)),
        "no_interaction_candidates": len(roster) - len(scores),
        "history_truncated": history_truncated,
        "searched_messages": len(rows),
        "readable_messages": len(material),
    }
    if not candidate_contacts:
        return {**empty, "reason": "no_candidates", "stats": stats}
    if not material:
        return {**empty, "reason": "no_text", "stats": stats}
    selected_contacts = set(candidate_contacts)
    relevant_items = [
        item
        for key, item in material.items()
        if key in related and (not related[key] or related[key] & selected_contacts)
    ]
    relevant_items.sort(
        key=lambda item: (
            bool(_CHANGE.search(item["text"])),
            bool(access.BUSINESS.search(item["text"])),
            str(item["row"]["message_create_time"]),
        ),
        reverse=True,
    )
    # Give each candidate recent/earlier anchors before a high-volume colleague fills the pack.
    anchors = []
    for cid in candidate_contacts:
        items = [i for i in relevant_items if cid in related[source._key(i["row"])]]
        anchors.extend(items[:4])
        chronological = sorted(items, key=lambda i: str(i["row"]["message_create_time"]))
        anchors.extend(chronological[:1])
    anchors = list(
        {
            source._key(i["row"]): i
            for i in [
                *[i for i in relevant_items if _CHANGE.search(i["text"])][:12],
                *anchors,
                *relevant_items,
            ]
        }.values()
    )[:MAX_MESSAGES]
    # Real predecessors and reply parents are context facts, not synthetic QA links.
    previous = source._previous_ids(conn, [i["row"] for i in anchors])
    previous_ids = sorted({mid for mid in previous.values() if mid})
    if previous_ids:
        read(
            source._rows(
                conn,
                "m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])",
                (chat_ids, previous_ids),
                MAX_MESSAGES + 1,
            )
        )
    chosen, seen, chars = [], set(), 0
    for anchor in anchors:
        row = anchor["row"]
        context_keys = [
            (row["chat_id"], row.get("parent_id")),
            (row["chat_id"], previous.get(source._key(row))),
            source._key(row),
        ]
        part = list({key: material[key] for key in context_keys if key in material and key not in seen}.values())
        size = sum(len(i["text"]) for i in part)
        if len(chosen) + len(part) > MAX_MESSAGES or chars + size > MAX_CHARS:
            continue
        chosen.extend(part)
        seen.update(source._key(i["row"]) for i in part)
        chars += size
    if not chosen:
        return {**empty, "reason": "material_limit", "stats": stats}
    chosen.sort(key=lambda i: (str(i["row"]["message_create_time"]), *source._key(i["row"])))
    aliases = {oid: "U1" for oid in target_ids}
    candidate_ids = {f"CM{n}": cid for n, cid in enumerate(candidate_contacts, 1)}
    for alias, cid in candidate_ids.items():
        aliases.update({r["open_id"]: alias for r in roster[cid]})
    candidate_alias = {cid: alias for alias, cid in candidate_ids.items()}
    refs = [
        {"kind": "responsibility_state", "contact_id": contact_id, "state_hash": access._hash(state)},
        {
            "kind": "person",
            "label": "U1",
            "contact_id": contact_id,
            "identities": identities,
            "identity_hash": access._hash(identities),
        },
    ]
    people = [{"id": "U1", "role": "creator"}]
    candidates = []
    for alias, cid in candidate_ids.items():
        identity_rows = conn.execute(
            "SELECT "
            + source._SPEAKER_COLUMNS
            + " FROM lark_identities WHERE contact_id=%s AND identity_type='user' ORDER BY open_id",
            (cid,),
        ).fetchall()
        refs.append(
            {
                "kind": "person",
                "label": alias,
                "contact_id": cid,
                "identities": identity_rows,
                "identity_hash": access._hash(identity_rows),
            }
        )
        people.append({"id": alias, "role": "internal"})
        candidates.append(
            {
                "id": alias,
                "name": roster[cid][0]["display_name"] or "",
                "known_assignment": cid in known,
            }
        )
    remaining = sorted(
        {
            i["row"]["sender_open_id"]
            for i in chosen
            if i["row"]["sender_open_id"] not in aliases and i["row"]["sender_open_id"]
        }
    )
    for n, identity in enumerate(
        conn.execute(
            "SELECT "
            + source._SPEAKER_COLUMNS
            + " FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id",
            (remaining,),
        ).fetchall(),
        2,
    ):
        alias = f"U{n}"
        aliases[identity["open_id"]] = alias
        role = (
            "bot"
            if identity["identity_type"] != "user"
            else "internal"
            if identity["tenant_classification"] == "internal"
            else "external"
        )
        people.append({"id": alias, "role": role})
        refs.append(
            {"kind": "person", "label": alias, "identities": [identity], "identity_hash": access._hash([identity])}
        )
    used_chats = [c for c in chats if c["chat_id"] in {i["row"]["chat_id"] for i in chosen}]
    groups = {c["chat_id"]: f"G{n}" for n, c in enumerate(used_chats, 1)}
    labels = {source._key(i["row"]): f"M{n}" for n, i in enumerate(chosen, 1)}
    messages = []
    for item in chosen:
        row, key = item["row"], source._key(item["row"])
        messages.append(
            {
                "id": labels[key],
                "chat": groups[row["chat_id"]],
                "speaker": aliases.get(row["sender_open_id"], "unknown"),
                "at": str(row["message_create_time"]) if row["message_create_time"] else None,
                "reply": labels.get((row["chat_id"], row.get("parent_id"))),
                "previous": labels.get((row["chat_id"], previous.get(key))),
                "mentions": sorted(
                    {
                        aliases[t["open_id"]]
                        for t in mention_targets(row.get("mentions_json"))
                        if not t["ambiguous"] and t["open_id"] in aliases
                    }
                ),
                "text": item["text"],
            }
        )
        refs.append(
            {
                "kind": "message",
                "label": labels[key],
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "authorization_id": str(row["authorization_id"]),
                "scope_hash": source._scope_hash(row),
                "body_hash": item["body_hash"],
                "ciphertext_hash": access._hash(row["body_ciphertext"]),
                "relation_hash": access._relation_hash(row),
                **({"previous_id": previous[key]} if key in previous else {}),
            }
        )
    refs.extend(
        {"kind": "chat", "label": groups[c["chat_id"]], "chat_id": c["chat_id"], "name_hash": access._hash(c["name"])}
        for c in used_chats
    )
    coverage = {
        **stats,
        "full_history": False,
        "selected_candidates": len(candidates),
        "readable_messages": len(material),
        "messages": len(messages),
        "body_chars": chars,
        "missing_reply_ids": [
            labels[source._key(i["row"])]
            for i in chosen
            if i["row"].get("parent_id") and (i["row"]["chat_id"], i["row"]["parent_id"]) not in labels
        ],
        "critical_evidence_omitted": any(
            _CHANGE.search(i["text"]) for i in relevant_items if source._key(i["row"]) not in seen
        ),
        "historical_context_omitted": any(source._key(i["row"]) not in seen for i in relevant_items),
        "excerpted_ids": [],
    }
    assignments = []
    for assignment in state["assignments"]:
        cid = authorization_people.get(str(assignment["responsible_authorization_id"]))
        if assignment["ended_at"] is None and cid in selected_contacts:
            assignments.append(
                {
                    "cm": candidate_alias[cid],
                    "level": assignment["level"],
                    "source": assignment["source"],
                    "since": str(assignment["started_at"]),
                    "account_id": str(assignment["account_id"]) if assignment["account_id"] else None,
                    "handle": assignment["handle"],
                    "platform": assignment["platform"],
                }
            )
    pack = {
        "target": "U1",
        "creator_known": True,
        "review_date": datetime.now(UTC).date().isoformat(),
        "candidates": candidates,
        "people": people,
        "existing_assignments": assignments,
        "chats": [
            {"id": groups[c["chat_id"]], "name": c["name"] or "", "mode": c["chat_mode"] or "unknown"}
            for c in used_chats
        ],
        "messages": messages,
        "coverage": coverage,
    }
    return {
        "pack": pack,
        "candidate_ids": candidate_ids,
        "refs": refs,
        "fingerprint": access._hash([PROMPT_VERSION, pack]),
        "stats": coverage,
        "reason": "",
    }


def still_valid(conn, refs, settings, *, lightweight=False):
    state_refs = [r for r in refs if r.get("kind") == "responsibility_state"]
    if len(state_refs) != 1 or state_refs[0]["contact_id"] in source.RESTRICTED_CONTACTS:
        return False
    ref = state_refs[0]
    if access._hash(_state(conn, ref["contact_id"])) != ref["state_hash"]:
        return False
    return source.still_valid(
        conn, [r for r in refs if r["kind"] != "responsibility_state"], settings, ciphertext_only=lightweight
    )


def validate(pack, result):
    """Validate structure and factual citations, not responsibility wording."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError as exc:
            raise ModelOutputError("json") from exc
    value = deepcopy(result)
    if not isinstance(value, dict) or set(value) != {"suggestions"} or not isinstance(value["suggestions"], list):
        raise ModelOutputError("shape")
    schema = output_format()["schema"]["properties"]["suggestions"]["items"]
    candidates = {c["id"]: c for c in pack["candidates"]}
    messages = {m["id"]: m for m in pack["messages"]}
    seen = set()
    for item in value["suggestions"]:
        if not isinstance(item, dict):
            raise ModelOutputError("shape")
        for key in ("validation_error", "association_status", "association_reason"):
            item.pop(key, None)
        source._shape(item, schema)
        if item["cm"] not in candidates or item["cm"] in seen:
            raise ModelOutputError("candidates")
        seen.add(item["cm"])
        if (
            len(item["scope_note"]) > 120
            or len(item["reason"]) > 240
            or len(item["scope"]) > 4
            or any(len(item[name]) > 8 for name in ("support_refs", "counter_refs"))
        ):
            raise ModelOutputError("limits")
        invalid_refs = any(ref not in messages for ref in item["support_refs"] + item["counter_refs"])
        established = (
            candidates[item["cm"]].get("known_assignment")
            and item["decision"] == "responsible"
            and item["period"] == "current"
        )
        cited = item["support_refs"] or item["decision"] == "historical" and item["counter_refs"]
        missing_support = item["decision"] != "insufficient" and not cited and not established
        if invalid_refs or missing_support:
            item.update(
                decision="insufficient",
                period="uncertain",
                scope=["unknown"],
                scope_note="",
                confidence=0,
                relation_level="unknown",
                support_refs=[r for r in item["support_refs"] if r in messages],
                counter_refs=[r for r in item["counter_refs"] if r in messages],
                reason="引用无效，需重新核实。" if invalid_refs else "缺少可核验的负责人证据。",
                validation_error="references" if invalid_refs else "missing_support",
            )
        else:
            item["support_refs"] = list(dict.fromkeys(item["support_refs"]))
            item["counter_refs"] = list(dict.fromkeys(item["counter_refs"]))
    if seen != set(candidates):
        raise ModelOutputError("candidates")
    return value
