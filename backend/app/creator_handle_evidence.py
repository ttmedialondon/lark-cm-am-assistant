"""One-person, multi-handle evidence; never changes identities or relationships."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import evidence_access as access
from . import evidence_text as text
from . import handle_evidence as handles
from .crypto import message_body_cipher
from .openai_client import ModelOutputError

PROMPT_VERSION = "creator-handle-research-v1.3"
MAX_MESSAGES, MAX_CHARS, MAX_HANDLES, SEARCH_LIMIT = 48, 20_000, 12, 20_000
# A research hold is an explicit access restriction, not a negative identity finding.
RESTRICTED_CONTACTS = frozenset({"6abf6bf9-12dc-4a22-9654-2010aa4c7785"})
_TOKEN = re.compile(r"(?<![A-Za-z0-9._])[A-Za-z0-9._]{1,64}(?![A-Za-z0-9._])", re.ASCII)
_SELF = re.compile(
    r"\b(?:my\s+(?:(?:own|other|second|backup|new|old|tiktok|tt)\s+)*(?:account|page|handle|username)|"
    r"my\s+(?:tiktok|tt)(?=\s+(?:is\b|https?://|@)|\s*[:=])|"
    r"(?:both|these|they|that|this|it)(?:\s+(?:are|is)|['’]s)?\s+(?:mine|my)|"
    r"mine\b|i\s+(?:have|own|use)\s+(?:\w+\s+){0,3}accounts?)\b|"
    r"我的(?:抖音|TikTok|账号|帐号|主页)|都是我的|这是我的|我(?:有|用的|的)账号",
    re.I,
)
_NOT_OWNER = re.compile(
    r"\b(?:my\s+(?:friend|client|sister|brother|husband|wife|daughter|son)(?:'s|’s)?|"
    r"(?:not|isn't|isn’t|wasn't|wasn’t)\s+(?:my|mine)|"
    r"(?:manage|operate|run)\s+(?:\w+\s+){0,3}(?:for|on behalf))\b|"
    r"不是我的|朋友的|替.*(?:管理|运营)|代运营",
    re.I,
)
_CONFIRM = re.compile(r"\b(?:yes|yep|yeah|correct|right|mine)\b|是的|正确|没错|我的", re.I)
_QUESTION = re.compile(r"\b(?:tiktok|tt|handle|username|account|page)\b|账号|用户名|主页", re.I)
_THIRD_PARTY = re.compile(r"\b(?:friend|client|referred|manage|operate|approve|approval)\b|朋友|转发|审批|代运营", re.I)
_DENIAL = re.compile(r"\b(?:no|not|isn't|isn’t|incorrect|wrong)\b|不是|不对", re.I)
_COURTESY = re.compile(r"\bno\s+(?:worries|problem)\b", re.I)
VALIDATION_ERRORS = frozenset({
    "references", "needed", "unsupported_evidence", "supported_relation", "excerpted_support",
    "owner_code", "unconnected_owner_evidence", "counterevidence", "out_of_catalog",
})
_TRANSFER = re.compile(
    r"\b(?:transfer\w*|sold|deleted|shared|banned|suspended)\b.*\b(?:account|page)\b|"
    r"\b(?:account|page)\b.*\b(?:transfer\w*|sold|deleted|shared|banned|suspended)\b|转让|封禁|注销", re.I)
_COMPANY_READABLE = access._READABLE.replace("WHERE a.id=%s", "WHERE TRUE") + """ AND NOT EXISTS (
    SELECT 1 FROM lark_identities blocked WHERE blocked.open_id=m.sender_open_id
      AND blocked.contact_id='6abf6bf9-12dc-4a22-9654-2010aa4c7785'::uuid)"""
_ROW_COLUMNS = access._COLUMNS + ",a.open_id AS authorization_open_id,a.body_consent_at"
_SPEAKER_COLUMNS = "open_id,contact_id,identity_type,tenant_classification"


@lru_cache(maxsize=1)
def instructions():
    source = (Path(__file__).resolve().parents[2] / "docs/specs/creator-handle-association-prompt_v1.2.md").read_text()
    return source.split("```text\n", 1)[1].split("```", 1)[0].strip()


@lru_cache(maxsize=1)
def _schema():
    return json.loads((Path(__file__).resolve().parents[2]
                       / "docs/specs/creator-handle-association_v1.2.schema.json").read_text())


def output_format(pack=None):
    schema = deepcopy(_schema())
    if pack is not None:
        creator = schema["properties"]["creator"]["properties"]
        links = schema["properties"]["links"]
        fields = links["items"]["properties"]
        fields["h"]["enum"] = [h["id"] for h in pack["handles"]]
        links.update(minItems=len(pack["handles"]), maxItems=len(pack["handles"]))
        for refs in (creator["refs"], fields["refs"]):
            refs["items"]["enum"] = [m["id"] for m in pack["messages"]]
        creator["state"]["enum"] = ["known"] if pack["creator_known"] else ["supported", "review", "not_supported"]
        if pack["creator_known"]:
            creator["refs"]["maxItems"] = 0
    return {"type": "json_schema", "name": "creator_handle_v1_3", "strict": True, "schema": schema}


def _denied(value):
    return _DENIAL.search(_COURTESY.sub("", value))


def _rows(conn, condition, params, limit=SEARCH_LIMIT + 1):
    # Deduplicate overlapping OAuth grants without widening any individual grant.
    return conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON(m.chat_id,m.message_id) " + _ROW_COLUMNS + " "
        + _COMPANY_READABLE + " AND m.message_type IN ('text','post') AND " + condition
        + " ORDER BY m.chat_id,m.message_id,a.id) material "
        "ORDER BY message_create_time DESC NULLS LAST,chat_id,message_id LIMIT %s",
        (*params, limit),
    ).fetchall()


def _plain(raw, row):
    """Preserve concrete video paths; account parsing must not turn videos into profiles."""
    if row["message_type"] == "text":
        return text._readable_body(raw, "text").strip()
    try:
        value = json.loads(raw)
    except ValueError:
        return raw.strip()

    def pieces(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"text", "title", "href", "url"} and isinstance(child, str):
                    yield child
                elif isinstance(child, list | dict):
                    yield from pieces(child)
        elif isinstance(node, list):
            for child in node:
                yield from pieces(child)

    return "\n".join(dict.fromkeys(p.strip() for p in pieces(value) if p.strip()))


def _tokens(value, catalog):
    # Exact catalog membership is recall only, never account ownership evidence.
    return {token.casefold().rstrip(".") for token in _TOKEN.findall(value)} & catalog


def _key(row):
    return row["chat_id"], row["message_id"]


def _scope_hash(row):
    return access._hash([str(row["authorization_id"]), row["authorization_open_id"], row["body_consent_at"]])


def build(conn, settings, contact_id, catalog):
    """Prepare bounded material locally. Missing evidence never means a non-creator."""
    contact_id = str(contact_id)
    empty = {"pack": None, "refs": [], "fingerprint": None, "reason": "restricted", "stats": {}}
    if contact_id in RESTRICTED_CONTACTS:
        return empty
    identities = conn.execute("SELECT " + _SPEAKER_COLUMNS
                              + " FROM lark_identities WHERE contact_id=%s AND identity_type='user' ORDER BY open_id",
                              (contact_id,)).fetchall()
    if not identities or any(i["tenant_classification"] == "internal" for i in identities):
        return empty
    open_ids = [i["open_id"] for i in identities]
    chats = conn.execute(
        """SELECT DISTINCT c.chat_id,c.name FROM chats c
           JOIN authorization_chats ac ON ac.chat_id=c.chat_id JOIN authorizations a ON a.id=ac.authorization_id
           WHERE a.status='active' AND a.body_consent_at IS NOT NULL AND ac.is_visible AND ac.sync_enabled
             AND COALESCE(c.chat_status,'normal')='normal' AND (
               ac.p2p_counterpart_open_id=ANY(%s::text[]) OR EXISTS (
                 SELECT 1 FROM chat_members cm WHERE cm.chat_id=c.chat_id AND cm.open_id=ANY(%s::text[]))
               OR EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id=c.chat_id
                 AND m.sender_open_id=ANY(%s::text[]))) ORDER BY c.chat_id""",
        (open_ids, open_ids, open_ids),
    ).fetchall()
    chat_ids = [c["chat_id"] for c in chats]
    own_rows = _rows(conn, "m.sender_open_id=ANY(%s::text[]) AND m.chat_id=ANY(%s::text[])",
                     (open_ids, chat_ids)) if chat_ids else []
    truncated = len(own_rows) > SEARCH_LIMIT
    own_rows = own_rows[:SEARCH_LIMIT]
    cipher = message_body_cipher(settings.token_encryption_key)

    def readable(rows):
        items = []
        for row in rows:
            raw = text._decrypt_body(cipher, row["body_ciphertext"])
            body = _plain(raw, row) if raw is not None else ""
            if body and "消息没有可展示的文本" not in body:
                items.append({"row": row, "text": body, "handles": _tokens(body, catalog),
                              "body_hash": access._hash(raw)})
        return items

    own = readable(own_rows)
    if not own:
        return {**empty, "reason": "no_text", "stats": {"history_truncated": truncated}}
    parent_keys = {_key(r) for r in own_rows if r.get("parent_id")}
    parent_rows = _rows(conn,
        "EXISTS (SELECT 1 FROM chat_messages child WHERE child.chat_id=m.chat_id AND child.parent_id=m.message_id "
        "AND child.message_id=ANY(%s::text[]) AND child.sender_open_id=ANY(%s::text[]))",
        ([message_id for _, message_id in parent_keys], open_ids)) if parent_keys else []
    parents = readable(parent_rows)
    scores = Counter()
    for item in [*own, *parents]:
        for handle in item["handles"]:
            scores[handle] += 4 if item["row"]["sender_open_id"] in open_ids else 2
    for chat in chats:
        for handle in _tokens(chat["name"] or "", catalog):
            scores[handle] += 1
    candidates = sorted(scores, key=lambda h: (-scores[h], h))[:MAX_HANDLES]
    if not candidates:
        return {**empty, "reason": "no_candidates", "stats": {"readable_messages": len(own)}}
    candidate_set = set(candidates)
    by_key = {_key(i["row"]): i for i in [*own, *parents]}
    ranked = sorted(own, key=lambda i: (
        bool(i["handles"] & candidate_set or by_key.get((i["row"]["chat_id"], i["row"].get("parent_id")), {})
             .get("handles", set()) & candidate_set), bool(handles._CHANGE.search(i["text"])),
        bool(_SELF.search(i["text"])), bool(text._IMPORTANT.search(i["text"])),
        str(i["row"]["message_create_time"])), reverse=True)
    changes = [i for i in ranked if handles._CHANGE.search(i["text"]) or _NOT_OWNER.search(i["text"])
               or _TRANSFER.search(i["text"]) or _denied(i["text"]) and (
                   i["handles"] & candidate_set or _QUESTION.search(i["text"]) or
                   by_key.get((i["row"]["chat_id"], i["row"].get("parent_id")), {})
                   .get("handles", set()) & candidate_set)]
    anchors = list({_key(i["row"]): i for i in [*changes[:8], *ranked]}.values())[:24]
    # Small same-chat context windows preserve split links/statements and negative replies.
    context_rows = []
    for anchor in anchors[:12]:
        row = anchor["row"]
        if row.get("message_create_time") is not None:
            context_rows.extend(_rows(conn,
                "m.chat_id=%s AND m.message_create_time BETWEEN %s-INTERVAL '5 minutes' AND %s+INTERVAL '5 minutes'",
                (row["chat_id"], row["message_create_time"], row["message_create_time"]), 12))
    for item in readable(context_rows):
        by_key[_key(item["row"])] = item
    chosen, seen, chars = [], set(), 0
    for anchor in anchors:
        row = anchor["row"]
        parent_key = (row["chat_id"], row.get("parent_id"))
        # Add a parent and answer atomically: never clip one half to fit a token budget.
        bundle = [by_key[parent_key], anchor] if parent_key in by_key else [anchor]
        extras = [i for i in by_key.values() if i["row"]["chat_id"] == row["chat_id"]
                  and _near(i["row"], row)]
        for part in (bundle, *([extra] for extra in extras)):
            part = [i for i in part if _key(i["row"]) not in seen]
            size = sum(len(i["text"]) for i in part)
            if len(chosen) + len(part) > MAX_MESSAGES or chars + size > MAX_CHARS:
                continue
            chosen.extend(part)
            seen.update(_key(i["row"]) for i in part)
            chars += size
    if not chosen:
        return {**empty, "reason": "material_limit", "stats": {"readable_messages": len(own)}}
    chosen.sort(key=lambda i: (str(i["row"]["message_create_time"]), *_key(i["row"])))
    speakers = conn.execute("SELECT " + _SPEAKER_COLUMNS
                            + " FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id",
                            (sorted({i["row"]["sender_open_id"] for i in chosen if i["row"]["sender_open_id"]}),)
                            ).fetchall()
    aliases = {oid: "U1" for oid in open_ids}
    people = [{"id": "U1", "role": "external"}]
    refs = [{"kind": "person", "label": "U1", "contact_id": contact_id,
             "identities": identities, "identity_hash": access._hash(identities)}]
    for identity in speakers:
        oid = identity["open_id"]
        if oid in aliases:
            continue
        alias = "U" + str(len(people) + 1)
        aliases[oid] = alias
        role = "staff" if identity["tenant_classification"] == "internal" else "external"
        if identity["identity_type"] != "user":
            role = "bot"
        people.append({"id": alias, "role": role})
        refs.append({"kind": "person", "label": alias, "identities": [identity],
                     "identity_hash": access._hash([identity])})
    labels = {_key(i["row"]): f"M{index}" for index, i in enumerate(chosen, 1)}
    used_chats = [c for c in chats if c["chat_id"] in {i["row"]["chat_id"] for i in chosen}]
    groups = {c["chat_id"]: f"G{index}" for index, c in enumerate(used_chats, 1)}
    messages = []
    for item in chosen:
        row, label = item["row"], labels[_key(item["row"])]
        messages.append({"id": label, "chat": groups[row["chat_id"]],
                         "speaker": aliases.get(row["sender_open_id"], "unknown"),
                         "at": str(row["message_create_time"]) if row["message_create_time"] else None,
                         "reply": labels.get((row["chat_id"], row.get("parent_id"))), "text": item["text"]})
        refs.append({"kind": "message", "label": label, "chat_id": row["chat_id"], "message_id": row["message_id"],
                     "authorization_id": str(row["authorization_id"]), "scope_hash": _scope_hash(row),
                     "body_hash": item["body_hash"], "relation_hash": access._relation_hash(row)})
    refs.extend({"kind": "chat", "label": groups[c["chat_id"]], "chat_id": c["chat_id"],
                 "name_hash": access._hash(c["name"])} for c in used_chats)
    known = bool(conn.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s "
                              "AND role='creator' AND status='confirmed'",
                              (contact_id,)).fetchone())
    pack = {"target": "U1", "creator_known": known,
            "handles": [{"id": f"H{i}", "handle": h, "catalog_verified": True} for i, h in enumerate(candidates, 1)],
            "people": people, "chats": [{"id": groups[c["chat_id"]], "name": c["name"] or ""} for c in used_chats],
            "messages": messages, "coverage": {"full_history": False,
                "missing_reply_ids": [labels[_key(i["row"])] for i in chosen
                                      if i["row"].get("parent_id") and
                                      (i["row"]["chat_id"], i["row"]["parent_id"]) not in labels],
                "excerpted_ids": []}}
    stats = {"messages": len(messages), "body_chars": chars, "readable_messages": len(own),
             "history_truncated": truncated, "candidates_omitted": max(0, len(scores) - len(candidates)),
             "critical_evidence_omitted": any(_key(i["row"]) not in seen for i in changes)}
    # Group names alone cannot satisfy any supported relation; wait without paying for a guess.
    if not any(_mentions(m["text"], h) for m in messages for h in candidates):
        return {**empty, "reason": "no_account_evidence", "stats": stats}
    # Cache semantic input only. Fresh grant/body references remain mandatory before use.
    return {"pack": pack, "refs": refs, "fingerprint": access._hash([PROMPT_VERSION, pack]),
            "reason": "ready", "stats": stats}


def _near(left, right):
    a, b = left.get("message_create_time"), right.get("message_create_time")
    return a is not None and b is not None and abs((a - b).total_seconds()) <= 300


def _shape(value, schema):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ModelOutputError("shape")
        for key, child in value.items():
            _shape(child, schema["properties"][key])
    elif kind == "array":
        if not isinstance(value, list):
            raise ModelOutputError("array")
        for child in value:
            _shape(child, schema["items"])
    elif not isinstance(value, str) or "enum" in schema and value not in schema["enum"]:
        raise ModelOutputError("enum")


def _near_messages(left, right):
    try:
        return left["chat"] == right["chat"] and abs((datetime.fromisoformat(left["at"])
                                                     - datetime.fromisoformat(right["at"])).total_seconds()) <= 300
    except (ValueError, TypeError):
        return False


def validate(pack, result):
    """Validate protocol and minimum connected evidence; model semantics still require review."""
    result = deepcopy(result)
    # Cached, locally downgraded items carry only a safe diagnostic code, never model prose.
    diagnostics = {}
    if isinstance(result, dict) and isinstance(result.get("links"), list):
        for index, link in enumerate(result["links"]):
            if isinstance(link, dict) and "validation_error" in link:
                error = link.pop("validation_error")
                if not isinstance(error, str) or error not in VALIDATION_ERRORS or link.get("decision") != "review":
                    raise ModelOutputError("shape")
                diagnostics[index] = error
    _shape(result, output_format()["schema"])
    messages = {m["id"]: m for m in pack["messages"]}
    candidates = {h["id"]: h for h in pack["handles"]}
    if len(result["links"]) != len(candidates) or {link["h"] for link in result["links"]} != set(candidates):
        raise ModelOutputError("candidates")
    if pack["creator_known"]:
        result["creator"] = {"state": "known", "refs": []}
    else:
        creator = result["creator"]
        if (creator["state"] == "known" or len(creator["refs"]) > 6
                or len(creator["refs"]) != len(set(creator["refs"]))
                or not set(creator["refs"]) <= messages.keys()):
            creator.update(state="review", refs=list(dict.fromkeys(r for r in creator["refs"] if r in messages))[:6])
    for index, link in enumerate(result["links"]):
        if index in diagnostics:
            link["validation_error"] = diagnostics[index]
        if (len(link["refs"]) > 6 or len(link["refs"]) != len(set(link["refs"]))
                or not set(link["refs"]) <= messages.keys()):
            _review(link, "references", messages)
            continue
        if len(link["needed"]) != len(set(link["needed"])):
            _review(link, "needed", messages)
            continue
        if link["decision"] != "supported":
            continue
        candidate = candidates[link["h"]]
        cited = [messages[ref] for ref in link["refs"]]
        own = [m for m in cited if m["speaker"] == pack["target"]]
        if not candidate["catalog_verified"]:
            _review(link, "out_of_catalog", messages)
            continue
        if not own or not any(_mentions(m["text"], candidate["handle"]) for m in cited):
            _review(link, "unsupported_evidence", messages)
            continue
        if link["relation"] not in {"self_account", "account_creator", "operator"} or link["period"] == "unknown":
            _review(link, "supported_relation", messages)
            continue
        if set(link["refs"]) & set(pack["coverage"]["excerpted_ids"]):
            _review(link, "excerpted_support", messages)
            continue
        if link["relation"] != "self_account":
            continue
        if link["code"] not in {"self_claim", "qa_confirm", "own_page"}:
            _review(link, "owner_code", messages)
            continue
        affirmative = [m for m in own if _SELF.search(m["text"]) and not _NOT_OWNER.search(m["text"])
                       and not _denied(m["text"])]
        explicit = any(_mentions(m["text"], candidate["handle"]) for m in affirmative)
        split = any(_near_messages(a, b) for a in affirmative for b in own
                    if not any(_mentions(a["text"], other["handle"]) for other in candidates.values()
                               if other["id"] != link["h"])
                    if _mentions(b["text"], candidate["handle"]) and not _THIRD_PARTY.search(b["text"])
                    and not _NOT_OWNER.search(b["text"]))
        qa = any(m["reply"] in link["refs"]
                 and (_CONFIRM.search(m["text"]) or _mentions(m["text"], candidate["handle"]))
                 and not _denied(m["text"]) and not _NOT_OWNER.search(m["text"])
                 and _QUESTION.search(messages[m["reply"]]["text"])
                 and re.search(r"\b(?:your|yours)\b|你的|您的", messages[m["reply"]]["text"], re.I)
                 and not _THIRD_PARTY.search(messages[m["reply"]]["text"])
                 and not _denied(messages[m["reply"]]["text"])
                 and (_mentions(m["text"], candidate["handle"]) or not any(
                     _mentions(m["text"], other["handle"]) for other in candidates.values()
                     if other["id"] != link["h"]))
                 and (_mentions(messages[m["reply"]]["text"], candidate["handle"])
                      or _mentions(m["text"], candidate["handle"]))
                 and messages[m["reply"]]["chat"] == m["chat"]
                 and messages[m["reply"]]["speaker"] != pack["target"]
                 for m in own if m["reply"])
        if link["code"] == "qa_confirm" and not qa or not (explicit or split or qa):
            _review(link, "unconnected_owner_evidence", messages)
            continue
        counter_refs = _counterevidence(pack, candidate["handle"], affirmative or own)
        if counter_refs and link["period"] != "historical":
            # A model cannot select only a positive quote and ignore a later provided denial.
            own_counter = any(messages[ref]["speaker"] == pack["target"] for ref in counter_refs)
            link.update(decision="review", relation="uncertain", period="historical" if own_counter else "unknown",
                        code="conflict", refs=list(dict.fromkeys([*counter_refs, *link["refs"]]))[:6],
                        needed=["recent_state" if own_counter else "other_claimants"],
                        validation_error="counterevidence")
    return result


def _review(link, error, messages):
    """An invalid item cannot become writable by trimming its bad references."""
    needed = "parent_reply" if link["code"] == "qa_confirm" else "account_context"
    link.update(decision="review", relation="uncertain", period="unknown",
                code="out_of_catalog" if error == "out_of_catalog" else
                     "ambiguous_reply" if needed == "parent_reply" else "no_link_evidence",
                refs=list(dict.fromkeys(r for r in link["refs"] if r in messages))[:6],
                needed=list(dict.fromkeys([*link["needed"], needed])), validation_error=error)


def _counterevidence(pack, handle, claims):
    messages = {m["id"]: m for m in pack["messages"]}
    actors = {p["id"]: p["role"] for p in pack.get("people", [])}
    claim_time = max((_timestamp(m) for m in claims), default=float("-inf"))
    conflicts = []
    for message in pack["messages"]:
        body = message["text"]
        parts = re.split(r"[\n,;!?]|\.\s+|\b(?:but|however|and\s+(?:my|i|now|the|it))\b", body, flags=re.I)
        exact_parts = [part for part in parts if _mentions(part, handle)]
        parent = messages.get(message.get("reply"))
        denial_reply = (parent and _mentions(parent["text"], handle) and _denied(body)
                        and not _mentions(body, handle))
        if message["speaker"] == pack["target"]:
            negative = denial_reply or any(_NOT_OWNER.search(part) or _TRANSFER.search(part)
                                          or _denied(part) for part in exact_parts)
            if negative and (_timestamp(message) >= claim_time or not message.get("at")):
                conflicts.append(message["id"])
                if denial_reply:
                    conflicts.append(parent["id"])
        elif (actors.get(message["speaker"], "unknown") in {"external", "unknown"} and exact_parts
              and _SELF.search(body) and not _NOT_OWNER.search(body) and not _denied(body)):
            conflicts.append(message["id"])
    return list(dict.fromkeys(conflicts))


def _timestamp(message):
    try:
        return datetime.fromisoformat(message.get("at") or "").timestamp()
    except (ValueError, TypeError):
        return float("-inf")


def _mentions(body, handle):
    return bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(handle) + r"(?=\.*(?:[^A-Za-z0-9_.]|$))", body, re.I))


def still_valid(conn, refs, settings=None):
    """Recheck original grants, identities, plaintext and reply relations before use."""
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    cipher = message_body_cipher(settings.token_encryption_key)
    for person in (r for r in refs if r["kind"] == "person"):
        if person.get("contact_id") in RESTRICTED_CONTACTS:
            return False
        condition = "contact_id=%s AND identity_type='user'" if person.get("contact_id") else "open_id=ANY(%s::text[])"
        value = person["contact_id"] if person.get("contact_id") else [i["open_id"] for i in person["identities"]]
        current = conn.execute("SELECT " + _SPEAKER_COLUMNS + " FROM lark_identities WHERE " + condition
                               + " ORDER BY open_id", (value,)).fetchall()
        if access._hash(current) != person["identity_hash"]:
            return False
    chats = [r for r in refs if r["kind"] == "chat"]
    names = {r["chat_id"]: access._hash(r["name"]) for r in conn.execute(
        "SELECT chat_id,name FROM chats WHERE chat_id=ANY(%s::text[]) AND COALESCE(chat_status,'normal')='normal'",
        ([r["chat_id"] for r in chats],)).fetchall()}
    if any(names.get(r["chat_id"]) != r["name_hash"] for r in chats):
        return False
    messages = [r for r in refs if r["kind"] == "message"]
    rows = conn.execute("SELECT " + _ROW_COLUMNS + " " + _COMPANY_READABLE
        + " AND a.id=ANY(%s::uuid[]) AND m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])",
        (sorted({r["authorization_id"] for r in messages}), [r["chat_id"] for r in chats],
         [r["message_id"] for r in messages])).fetchall() if messages else []
    current = {(r["chat_id"], r["message_id"], str(r["authorization_id"])): r for r in rows}
    for ref in messages:
        row = current.get((ref["chat_id"], ref["message_id"], ref["authorization_id"]))
        raw = text._decrypt_body(cipher, row["body_ciphertext"]) if row else None
        if not row or raw is None or (_scope_hash(row) != ref["scope_hash"] or access._hash(raw) != ref["body_hash"]
                       or access._relation_hash(row) != ref["relation_hash"]):
            return False
    return bool(messages)
