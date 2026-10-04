"""One-person, multi-handle evidence; never changes identities or relationships."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import contact_handles, creator_imports
from . import evidence_access as access
from . import evidence_text as text
from . import handle_evidence as handles
from .crypto import message_body_cipher
from .openai_client import ModelOutputError

PROMPT_VERSION = "creator-handle-research-v1.10"
MAX_MESSAGES, MAX_CHARS, MAX_HANDLES, SEARCH_LIMIT = 48, 20_000, 12, 20_000
# A research hold is an explicit access restriction, not a negative identity finding.
RESTRICTED_CONTACTS = frozenset({"6abf6bf9-12dc-4a22-9654-2010aa4c7785"})
_TOKEN = re.compile(r"(?<![A-Za-z0-9._])[A-Za-z0-9._]{1,64}(?![A-Za-z0-9._])", re.ASCII)
_SELF = re.compile(
    r"\b(?:my\s+(?:(?:own|main|other|second|backup|new|old|tiktok|tt)\s+)*(?:account|page|handle|handel|user\s*name)|"
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
_QUESTION = re.compile(r"\b(?:tiktok|tt|handles?|handels?|user\s*names?|accounts?|pages?)\b|账号|用户名|主页", re.I)
_THIRD_PARTY = re.compile(r"\b(?:friend|client|referred|manage|operate|approve|approval)\b|朋友|转发|审批|代运营", re.I)
_DENIAL = re.compile(r"\b(?:no|not|isn't|isn’t|incorrect|wrong)\b|不是|不对", re.I)
_COURTESY = re.compile(r"\bno\s+(?:worries|problem)\b", re.I)
# Retired semantic error codes remain readable for historical diagnostics, never generated anew.
VALIDATION_ERRORS = frozenset({
    "references", "needed", "unsupported_evidence", "supported_relation", "excerpted_support",
    "owner_code", "unconnected_owner_evidence", "counterevidence", "out_of_catalog", "reply_relation",
})
_TRANSFER = re.compile(
    r"\b(?:transfer\w*|sold|deleted|banned|suspended)\b.*\b(?:account|page)\b|"
    r"\b(?:account|page)\b.*\b(?:transfer\w*|sold|deleted|banned|suspended)\b|转让|封禁|注销", re.I)
_OWNER_NEGATION = re.compile(
    r"\b(?:not|isn't|isn’t|wasn't|wasn’t)\s+(?:mine|my|me|owned)\b|"
    r"\bmy\s+(?:(?:tiktok|tt)\s+)?(?:account|page|handle|handel|user\s*name)\s+(?:is|was)\s+not\b|"
    r"\bi\s+(?:do\s+not|don't|don’t|no\s+longer)\s+(?:own|use|operate|manage)\b|"
    r"不是我的|不是本人|不属于我", re.I)
_ACCOUNT_HISTORY = re.compile(
    r"\b(?:renam\w*|changed?\s+(?:my\s+)?(?:handle|user\s*name|account)|"
    r"(?:old|previous|former)\s+(?:handle|user\s*name|account)|no\s+longer\s+(?:use|own))\b|"
    r"改名|旧账号|旧帐号|以前的账号", re.I)
_SHARED_ACCOUNT = re.compile(
    r"\b(?:our\s+(?:(?:tiktok|tt|team(?:['’]s)?|other|new|main|backup)\s+)*|"
    r"(?:shared|joint|team(?:['’]s)?|company(?:['’]s)?|business(?:['’]s)?)\s+)"
    r"(?:account|page|handle|handel|user\s*name)\b|\bours\b|"
    r"\bour\s+(?:tiktok|tt)(?=\s+(?:is\b|https?://|@)|\s*[:=])|"
    r"\b(?:account|page)(?:\s+(?!link\b|url\b)@?[A-Za-z0-9._]{1,64})?\s+(?:is|was)\s+(?:shared|joint)\b|"
    r"我们的(?:账号|帐号|主页)|共同(?:账号|帐号)|团队(?:账号|帐号)", re.I)
_INTRO_PROXY = re.compile(
    r"\b(?:assistant|representative|represent\w*|manager|agency|company|merchant|seller|business|brand)\b|"
    r"on\s+behalf|助理|代理|商家|公司|代表", re.I)
_PROXY_ROLE = re.compile(
    r"\bi(?:['’]m|\s+am|\s+work\s+as)\s+(?:\w+\s+){0,5}"
    r"(?:assistant|representative|manager|agent|seller|merchant)\b|"
    r"\bi\s+represent\b|\bon\s+behalf\b|我是.*(?:助理|代理|商家)|代表.*(?:联系|回复)", re.I)
_EXPLICIT_ACCOUNT = re.compile(
    r"\bmy\s+(?:tiktok|tt)\s*(?:is\b|[:=])\s*@?([A-Za-z0-9._]{1,64})|"
    r"\b(?:on|from)\s+(?:(?:the|my)\s+)?(?:(?:tiktok|tt)\s+)?(?:account|page)\s+"
    r"@?([A-Za-z0-9._]{1,64})", re.I)
_CREATOR_ACTIVITY = re.compile(
    r"\b(?:filmed|filming|editing|posted|posting|uploaded|uploading|created|creating)\s+"
    r"(?:(?:my|a|the|three|two|one|new|latest|first|\d+)\s+){0,3}(?:videos?|content)\b|"
    r"\b(?:livestreaming|streaming)\s+on\s+(?:tik\s*tok|tt)\b|我.*(?:发了视频|正在剪辑|直播)", re.I)
_OWN_BUSINESS = re.compile(
    r"\bmy\s+(?:videos?|content|livestreams?|commissions?|commission\s+rate|shop\s+links?|"
    r"showcase|sample\s+(?:requests?|management))\b|"
    r"\bi\s+(?:(?:have|already|just|will|am|was|tried\s+to|want\s+to)\s+){0,3}"
    r"(?:post|film|upload|create|edit)(?:ed|ing)?\s+(?:(?:my|a|the|new|latest)\s+){0,3}"
    r"(?:videos?|content)\b", re.I)
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
    return {"type": "json_schema", "name": "creator_handle_v1_10", "strict": True, "schema": schema}


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


def _group_handle(chat):
    """Only the exact primary account in an established company cooperation title."""
    match = handles._GROUP_NAME.match(re.sub(r"\([^()\r\n]*\)", "", chat.get("name") or ""))
    if (chat.get("mode", chat.get("chat_mode")) == "group" and match
            and re.search(r"[xX×*＊&|·。]\s*(?:TT(?:\s*Media)?|Trutonic\s*Media)\b", match[0], re.I)):
        return match[1].casefold()
    return None


def _other_account(body, handle, contexts=()):
    return (any(_mentions(body, h) for h in contexts if h != handle)
            or any(c["handle"] != handle and c["handle"] not in {"shared", "joint", "mine", "ours"}
                   for c in handles._clues(body))
            or any(token.casefold().rstrip(".") != handle for match in _EXPLICIT_ACCOUNT.finditer(body)
                   if (token := next(t for t in match.groups() if t))))


def _personal_context(body, handle, contexts=()):
    """First-person account/creation context, not a prescribed self-introduction."""
    if (_other_account(body, handle, contexts) or _PROXY_ROLE.search(body)
            or _NOT_OWNER.search(body) or _SHARED_ACCOUNT.search(body)):
        return False
    for part in _parts(body):
        if (re.search(r"\b(?:bank|seller|shop|business|payment)\s+account\b", part, re.I)
                or _OWNER_NEGATION.search(part) or _ACCOUNT_HISTORY.search(part)
                or not _mentions(part, handle) and re.search(
                    r"\b(?:other|another|second|backup|new|old|previous|former)\s+(?:(?:tiktok|tt)\s+)?"
                    r"(?:account|page|handle|user\s*name)\b|备用账号|另一个账号|新账号|旧账号", part, re.I)):
            continue
        if _QUESTION.search(part) and _self_claim(part):
            return True
        if _OWN_BUSINESS.search(part) and not _denied(part):
            return True
        activity = _CREATOR_ACTIVITY.search(part)
        if (activity and re.search(r"\b(?:i|my)\b|我", part[:activity.start() + 1], re.I)
                and not re.search(r"\b(?:you|he|she|they|not|never|haven't|haven’t|didn't|didn’t)\b",
                                  part[:activity.start()], re.I)):
            return True
    return False


def _group_context(pack, message, handle):
    contexts = [*(h["handle"] for h in pack.get("handles", [])), *pack.get("confirmed_handles", [])]
    return (pack["creator_known"] and any(c["id"] == message["chat"] and _group_handle(c) == handle
                                         for c in pack.get("chats", []))
            and _personal_context(message["text"], handle, contexts))


def _group_warning(chat, body, handle, contexts=()):
    """Implicit counterevidence must use the same exact group binding as positive context."""
    return (_group_handle(chat) == handle and not _other_account(body, handle, contexts)
            and not re.search(r"\b(?:bank|seller|shop|business|payment)\s+account\b", body, re.I)
            and (_PROXY_ROLE.search(body) or (
                # Bind each original clause to the group account for the existing denial parser.
                any(_QUESTION.search(part) and (_negative(handle + " " + part, handle)
                    or _ACCOUNT_HISTORY.search(part)) for part in _parts(body)))))


def _key(row):
    return row["chat_id"], row["message_id"]


def _scope_hash(row):
    return access._hash([str(row["authorization_id"]), row["authorization_open_id"], row["body_consent_at"]])


def _previous_ids(conn, rows):
    """Use the actual conversation predecessor, including unreadable/non-text messages."""
    if not rows:
        return {}
    return {_key(r): r["previous_id"] for r in conn.execute(
        """SELECT m.chat_id,m.message_id,(SELECT p.message_id FROM chat_messages p
             WHERE p.chat_id=m.chat_id AND (p.message_create_time,p.message_id)<(m.message_create_time,m.message_id)
             ORDER BY p.message_create_time DESC,p.message_id DESC LIMIT 1) AS previous_id
           FROM chat_messages m WHERE m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])""",
        (sorted({r["chat_id"] for r in rows}), [r["message_id"] for r in rows]),
    ).fetchall()}


def build(conn, settings, contact_id, catalog, *, recheck=False):
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
    pairs = contact_handles.confirmed_pairs(conn, [contact_id])
    confirmed = {r["handle"] for r in pairs}
    empty["stats"] = {"confirmed_handles": sorted(confirmed), "manual_recheck": recheck,
                      "recheck_handles": [],
                      "human_confirmed_handles": sorted({r["handle"] for r in pairs if r["human_confirmed"]})}
    open_ids = [i["open_id"] for i in identities]
    chats = conn.execute(
        """SELECT DISTINCT c.chat_id,c.name,c.chat_mode FROM chats c
           JOIN authorization_chats ac ON ac.chat_id=c.chat_id JOIN authorizations a ON a.id=ac.authorization_id
           WHERE a.status='active' AND a.body_consent_at IS NOT NULL AND ac.is_visible AND ac.sync_enabled
             AND COALESCE(c.chat_status,'normal')='normal' AND (
               ac.p2p_counterpart_open_id=ANY(%s::text[]) OR EXISTS (
                 SELECT 1 FROM chat_members cm WHERE cm.chat_id=c.chat_id AND cm.open_id=ANY(%s::text[]))
               OR EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id=c.chat_id
                 AND m.sender_open_id=ANY(%s::text[]))) ORDER BY c.chat_id""",
        (open_ids, open_ids, open_ids),
    ).fetchall()
    known = bool(conn.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s "
                              "AND role='creator' AND status='confirmed'", (contact_id,)).fetchone())
    chats_by_id = {c["chat_id"]: c for c in chats}
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

    def previous_context(rows):
        previous, items = _previous_ids(conn, rows), []
        frontier = {(chat, mid) for (chat, _), mid in previous.items() if mid}
        for _ in range(3):
            if not frontier:
                break
            part = readable(_rows(conn, "m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])",
                                  (sorted({c for c, _ in frontier}), sorted({m for _, m in frontier}))))
            items.extend(part)
            prior = _previous_ids(conn, [i["row"] for i in part])
            previous.update(prior)
            frontier = {(chat, mid) for (chat, _), mid in prior.items() if mid}
        return previous, items

    own = readable(own_rows)
    if not own:
        return {**empty, "reason": "no_text", "stats": {**empty["stats"], "history_truncated": truncated}}
    parent_keys = {_key(r) for r in own_rows if r.get("parent_id")}
    parent_rows = _rows(conn,
        "EXISTS (SELECT 1 FROM chat_messages child WHERE child.chat_id=m.chat_id AND child.parent_id=m.message_id "
        "AND child.message_id=ANY(%s::text[]) AND child.sender_open_id=ANY(%s::text[]))",
        ([message_id for _, message_id in parent_keys], open_ids)) if parent_keys else []
    parents = readable(parent_rows)
    denial_previous, denial_context = previous_context([i["row"] for i in own if i["row"].get("chat_mode") == "p2p"
        and not i["row"].get("parent_id")
        and re.match(r"\s*(?:no\b|(?:that|this|it)\s+(?:is\s+)?not\s+mine\b|"
                           r"not\s+(?:mine|my)\b|不是我的|不对)", i["text"], re.I)])
    parents.extend(denial_context)
    parents = list({_key(i["row"]): i for i in parents}.values())
    staff = {r["open_id"] for r in conn.execute(
        "SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) "
        "AND identity_type='user' AND tenant_classification='internal'",
        (sorted({i["row"]["sender_open_id"] for i in parents if i["row"]["sender_open_id"]}),)
    ).fetchall()} if denial_context else set()
    scores = Counter()
    for item in [*own, *parents]:
        for handle in item["handles"]:
            scores[handle] += 4 if item["row"]["sender_open_id"] in open_ids else 2
    for chat in chats:
        for handle in _tokens(chat["name"] or "", catalog):
            scores[handle] += 8 if _group_handle(chat) == handle else 1
    previous_answers = dict(denial_previous)
    by_key = {_key(i["row"]): i for i in [*own, *parents]}
    answer_chains = {}

    def parent_key(item):
        row = item["row"]
        if row.get("parent_id"):
            return row["chat_id"], row["parent_id"]
        key = row["chat_id"], previous_answers.get(_key(row))
        speaker, chain = by_key.get(key, {}).get("row", {}).get("sender_open_id"), []
        for _ in range(3):
            item = by_key.get(key)
            if (not item or speaker in open_ids or not speaker or item["row"]["sender_open_id"] != speaker
                    or not _near(row, item["row"])):
                break
            chain.append(key)
            if _account_request(item["text"]):
                answer_chains[_key(row)] = list(reversed(chain))
                return key
            if speaker not in staff or item["handles"] or not _staff_supplement(item["text"]):
                break
            key = row["chat_id"], previous_answers.get(key)
        return row["chat_id"], None

    # Existing business facts are not paid-proof candidates. Reopen only for explicit
    # manual review or concrete account-specific conflicts, never merely a second page.
    recalled = bool(scores)
    existing = sorted(confirmed & scores.keys())
    conflicts = creator_imports._conflicts(conn, [(None, contact_id, h) for h in existing])
    rechecks = {h for h, conflict in zip(existing, conflicts, strict=True) if recheck or conflict}
    empty["stats"]["recheck_handles"] = sorted(rechecks)
    scores = Counter({h: score for h, score in scores.items() if h not in confirmed or h in rechecks})
    candidates = sorted(scores, key=lambda h: (-scores[h], h))[:MAX_HANDLES]
    if not candidates:
        return {**empty, "reason": "already_confirmed" if recalled else "no_candidates",
                "stats": {**empty["stats"], "readable_messages": len(own)}}
    candidate_set = set(candidates)

    ranked = sorted(own, key=lambda i: (
        bool(i["handles"] & candidate_set or by_key.get(parent_key(i), {})
             .get("handles", set()) & candidate_set), bool(handles._CHANGE.search(i["text"])),
        bool(_SELF.search(i["text"])), bool(text._IMPORTANT.search(i["text"])),
        str(i["row"]["message_create_time"])), reverse=True)
    changes = [i for i in ranked if handles._CHANGE.search(i["text"]) or any(
        _negative(i["text"], h, by_key.get(parent_key(i), {}).get("text"))
        or _group_warning(chats_by_id[i["row"]["chat_id"]], i["text"], h, confirmed | candidate_set)
        for h in candidates)]
    group_anchors = {h: next((i for i in ranked if known and _group_handle(chats_by_id[i["row"]["chat_id"]]) == h
                             and _personal_context(i["text"], h, confirmed | candidate_set)), None) for h in candidates}
    anchors = list({_key(i["row"]): i for i in [*changes[:8],
        *(i for i in group_anchors.values() if i), *ranked]}.values())[:24]
    # Only selected private handle answers need an extra question lookup (at most 24, not whole history).
    answer_previous, answer_context = previous_context([i["row"] for i in anchors if i["row"].get("chat_mode") == "p2p"
        and not i["row"].get("parent_id") and i["handles"] & candidate_set])
    previous_answers.update(answer_previous)
    by_key.update({_key(i["row"]): i for i in answer_context})
    if answer_context:
        staff.update(r["open_id"] for r in conn.execute(
            "SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) "
            "AND identity_type='user' AND tenant_classification='internal'",
            (sorted({i["row"]["sender_open_id"] for i in answer_context if i["row"]["sender_open_id"]}),)
        ).fetchall())
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
    bundles, extras = [], {}
    for anchor in anchors:
        row = anchor["row"]
        anchor_parent = parent_key(anchor)
        # Add a parent and answer atomically: never clip one half to fit a token budget.
        chain = answer_chains.get(_key(row), [anchor_parent])
        bundles.append([*(by_key[key] for key in chain if key in by_key), anchor])
        extras.update({_key(i["row"]): i for i in by_key.values() if i["row"]["chat_id"] == row["chat_id"]
                       and _near(i["row"], row)})
    chosen, seen, chars = [], set(), 0
    # Fill core statements/whole QA first. Early anchors' neighbours cannot evict later core evidence.
    for part in [*bundles, *([extra] for extra in extras.values())]:
        part = [i for i in part if _key(i["row"]) not in seen]
        size = sum(len(i["text"]) for i in part)
        if len(chosen) + len(part) > MAX_MESSAGES or chars + size > MAX_CHARS:
            continue
        chosen.extend(part)
        seen.update(_key(i["row"]) for i in part)
        chars += size
    if not chosen:
        return {**empty, "reason": "material_limit", "stats": {**empty["stats"], "readable_messages": len(own)}}
    chosen.sort(key=lambda i: (str(i["row"]["message_create_time"]), *_key(i["row"])))
    speakers = conn.execute("SELECT " + _SPEAKER_COLUMNS
                            + " FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id",
                            (sorted({i["row"]["sender_open_id"] for i in by_key.values()
                                     if i["row"]["sender_open_id"]}),)
                            ).fetchall()
    speaker_types = {s["open_id"]: s for s in speakers}
    chosen_speakers = {i["row"]["sender_open_id"] for i in chosen}
    aliases = {oid: "U1" for oid in open_ids}
    people = [{"id": "U1", "role": "external"}]
    refs = [{"kind": "person", "label": "U1", "contact_id": contact_id,
             "identities": identities, "identity_hash": access._hash(identities)}]
    for identity in speakers:
        oid = identity["open_id"]
        if oid in aliases or oid not in chosen_speakers:
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
    previous = _previous_ids(conn, [i["row"] for i in chosen if i["row"].get("chat_mode") == "p2p"])
    used_chats = [c for c in chats if c["chat_id"] in {i["row"]["chat_id"] for i in chosen}]
    groups = {c["chat_id"]: f"G{index}" for index, c in enumerate(used_chats, 1)}
    messages = []
    for item in chosen:
        row, label = item["row"], labels[_key(item["row"])]
        messages.append({"id": label, "chat": groups[row["chat_id"]],
                         "speaker": aliases.get(row["sender_open_id"], "unknown"),
                         "at": str(row["message_create_time"]) if row["message_create_time"] else None,
                         "reply": labels.get((row["chat_id"], row.get("parent_id"))),
                         "previous": labels.get((row["chat_id"], previous.get(_key(row)))), "text": item["text"]})
        refs.append({"kind": "message", "label": label, "chat_id": row["chat_id"], "message_id": row["message_id"],
                     "authorization_id": str(row["authorization_id"]), "scope_hash": _scope_hash(row),
                     "body_hash": item["body_hash"], "relation_hash": access._relation_hash(row),
                     **({"previous_id": previous.get(_key(row))} if row.get("chat_mode") == "p2p" else {})})
    refs.extend({"kind": "chat", "label": groups[c["chat_id"]], "chat_id": c["chat_id"],
                 "name_hash": access._hash(c["name"])} for c in used_chats)
    pack = {"target": "U1", "creator_known": known, "confirmed_handles": sorted(confirmed - candidate_set),
            "handles": [{"id": f"H{i}", "handle": h, "catalog_verified": True} for i, h in enumerate(candidates, 1)],
            "people": people, "chats": [{"id": groups[c["chat_id"]], "name": c["name"] or "",
                                          "mode": c["chat_mode"] or "unknown"} for c in used_chats],
            "messages": messages, "coverage": {"full_history": False,
                "missing_reply_ids": [labels[_key(i["row"])] for i in chosen
                                      if i["row"].get("parent_id") and
                                      (i["row"]["chat_id"], i["row"]["parent_id"]) not in labels],
                "excerpted_ids": []}}
    # Coverage is a source/budget fact, not a keyword-based ownership verdict.
    # Track dropped core bundles and external account references in selected context.
    dropped = [item for bundle in bundles if any(_key(i["row"]) not in seen for i in bundle) for item in bundle]
    dropped.extend(item for item in extras.values() if _key(item["row"]) not in seen
                   and item["row"]["sender_open_id"] not in open_ids
                   and speaker_types.get(item["row"]["sender_open_id"], {}).get("tenant_classification") != "internal")
    omitted_handles = sorted({h for item in dropped for h in item["handles"] & candidate_set})
    pack["coverage"]["omitted_handle_ids"] = [h["id"] for h in pack["handles"] if h["handle"] in omitted_handles]
    stats = {**empty["stats"], "messages": len(messages), "body_chars": chars, "readable_messages": len(own),
             "history_truncated": truncated, "candidates_omitted": max(0, len(scores) - len(candidates)),
             "critical_evidence_omitted": bool(omitted_handles),
             "critical_evidence_omitted_handles": omitted_handles}
    pack["evidence_hints"] = []
    for candidate in pack["handles"]:
        support, _ = _support_groups(pack, candidate)
        if support:
            pack["evidence_hints"].append({"h": candidate["id"], "groups": support[:3]})
    # Let the model assess title + conversation context even without a recognized self-introduction.
    if not any(_mentions(m["text"], h) for m in messages for h in candidates) and not any(
            _tokens(c["name"], candidate_set) for c in pack["chats"]):
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


def _parts(body):
    return re.split(r"[\n,;!?]|\.\s+|\b(?:but|however|and\s+(?:my|i|now|the|it))\b", body, flags=re.I)


def _self_claim(body, handle=None, *, allow_intro=False):
    return any((_SELF.search(part) or handle is not None and re.search(
                   r"\bmy\s+(?:tiktok|tt)\s+@?" + re.escape(handle) + r"(?=\.*(?:[^A-Za-z0-9_.]|$))", part, re.I)
               or allow_intro and handle is not None and not _INTRO_PROXY.search(body)
               and not _THIRD_PARTY.search(body) and not _OWNER_NEGATION.search(body)
               and not _SHARED_ACCOUNT.search(body) and re.search(
                   r"\b(?:i['’]m|i\s+am)\s+(?:(?:[A-Za-z][A-Za-z'’-]*\s+){1,3}from\s+)?@?"
                   + re.escape(handle) + r"\s+on\s+tik\s*tok\b", part, re.I))
               and not _NOT_OWNER.search(part) and not _SHARED_ACCOUNT.search(part) and not _denied(part)
               and (handle is None or _mentions(part, handle)) for part in _parts(body))


def _account_request(body):
    """A request/confirmation of the person's account, not a sample or award question."""
    for part in _parts(body):
        if (re.search(r"\btik\s*tok\b", part, re.I) and re.search(
                r"\b(?:can(?:not|['’]t)|unable\s+to)\s+(?:find|locate|verify)\s+(?:your|ur)\s+"
                r"(?:uid|handle|handel|user\s*name)\b", part, re.I)
                and not _THIRD_PARTY.search(part)
                and not re.search(r"\b(?:bank|seller|shop|business|payment)\s+account\b", part, re.I)):
            return True
        for match in re.finditer(r"\b(?:what|which|send|share|provide|tell|drop|confirm)\b|"
                                 r"\b(?:can|could|may)\s+i\s+(?:please\s+)?(?:have|get|see)\b|"
                                 r"\b(?:is|was)\s+(?:your|this|that|it)\b|请|哪个|是", part, re.I):
            request = re.split(r"\b(?:if|unless|when|because)\b", part[match.start():], maxsplit=1, flags=re.I)[0]
            if re.match(r"(?:is|was)\b", request, re.I) and re.search(
                    r"\b(?:receive\w*|samples?|prizes?|awards?|invites?|eligible|messages?)\b", request, re.I):
                continue
            if (not re.search(r"\b(?:not|don't|don’t)\s*$", part[:match.start()], re.I)
                    and re.search(r"\b(?:handles?|handels?|user\s*names?)\b|"
                                  r"\b(?:tiktok|tt)\s+(?:account|page)\b|"
                                  r"\b(?:is|was)\s+your\s+(?:tiktok|tt)\b|账号|用户名", request, re.I)
                    and not re.search(r"\b(?:bank|seller|shop|business|payment)\s+account\b", request, re.I)
                    and not _THIRD_PARTY.search(request) and not _denied(request)
                    and re.search(r"\b(?:your|yours)\b|你的|您的|\b(?:send|share|provide|tell|drop)\b", request, re.I)):
                return True
    return False


def _negative(body, handle, parent=None):
    # Separate a second account's predicate without changing the shared positive-claim parser.
    parts = [part for clause in _parts(body) for part in re.split(
        r"\band\b(?=\s+@?[A-Za-z0-9._]+\s+(?:is|was|isn't|isn’t|wasn't|wasn’t)\b)", clause, flags=re.I)]
    direct = False
    for part in parts:
        if not _mentions(part, handle):
            continue
        owner_denial = re.search(
            r"\b(?:not|isn't|isn’t|wasn't|wasn’t)\s+(?:mine|me|owned|my\s+"
            r"(?:(?:tiktok|tt)\s+)?(?:account|page|handle|handel|user\s*name))\b|"
            r"\bi\s+(?:do\s+not|don't|don’t|no\s+longer)\s+(?:own|use|operate|manage)\b|"
            r"不是我的|不是本人|不属于我", part, re.I) or re.search(
                r"\bmy\s+(?:(?:tiktok|tt)\s+)?(?:account|page|handle|handel|user\s*name)\s+(?:is|was)\s+not\s+"
                r"(?:https?://(?:www\.)?tiktok\.com/@|@)?" + re.escape(handle)
                + r"(?=\.*(?:[^A-Za-z0-9_.]|$))", part, re.I)
        proxy = re.search(
            r"\b(?:my\s+(?:friend|client|sister|brother|husband|wife|daughter|son)(?:'s|’s)?|"
            r"(?:manage|operate|run)\s+(?:\w+\s+){0,3}(?:for|on behalf))\b|朋友的|替.*(?:管理|运营)|代运营",
            part, re.I)
        transfer = _TRANSFER.search(part) and not re.search(
            r"\btransfer\w*\s+(?:(?:to|over|another|a|an|the)\s+)*agenc(?:y|ies)\b", part, re.I) and any(
                not re.search(r"\b(?:not|never|(?:did|does|do|has|have|had|was|were|is|are)n['’]t)"
                              r"\s+(?:\w+\s+)*$", part[:verb.start()], re.I)
                and not re.match(r"\s*[- ]?\s*proof\b", part[verb.end():], re.I)
                for verb in re.finditer(r"\b(?:transfer\w*|sold|deleted|banned|suspended)\b|转让|封禁|注销",
                                        part, re.I))
        direct = bool(owner_denial or _SHARED_ACCOUNT.search(part) or transfer
                      or proxy and (_QUESTION.search(part) or re.search(r"['’]s\b", part)))
        if direct:
            break
    # A bare denial answers an ownership question; "Not yet" about a prize is not one.
    reply = (parent and _mentions(parent, handle) and _account_request(parent)
             and re.search(r"^(?:no\b(?!\s+(?:worries|problem)\b)|(?:that|this|it)\s+(?:is\s+)?not\s+mine\b|"
                           r"not\s+(?:mine|my\s+(?:(?:tiktok|tt)\s+)?(?:account|page|handle|handel|user\s*name))\b|"
                           r"不是我的|不对)", body.strip(), re.I)
             and not re.search(r"\b(?:not\s+(?:yet|sure)|no\s+(?:message|sample|invite|prize))\b", body, re.I))
    return bool(direct or reply)


def _qa(pack, answer, question, handle, candidates, refs, *, continuous=False):
    if (question["id"] not in refs or question["chat"] != answer["chat"]
            or question["speaker"] in {pack["target"], "unknown"}
            or not _account_request(question["text"])
            or _denied(answer["text"]) or _NOT_OWNER.search(answer["text"])
            or any(_SHARED_ACCOUNT.search(part) for part in _parts(answer["text"]))
            or not (_CONFIRM.search(answer["text"]) or _mentions(answer["text"], handle))
            or not (_mentions(question["text"], handle) or _mentions(answer["text"], handle))
            or any(_mentions(answer["text"], h["handle"]) or
                   not _mentions(answer["text"], handle) and _mentions(question["text"], h["handle"])
                   for h in candidates if h["handle"] != handle)):
        return False
    if not continuous:
        return True
    return _continuous(pack, answer, question)


def _staff_supplement(body):
    return not (re.search(r"[?？]|\b(?:what|which|send|share|provide|tell|drop|confirm)\b", body, re.I)
                or _QUESTION.search(body) or _THIRD_PARTY.search(body)
                or _SHARED_ACCOUNT.search(body) or _denied(body))


def _continuous(pack, answer, question):
    chat = next((c for c in pack.get("chats", []) if c["id"] == answer["chat"]), {})
    actors = {m["speaker"] for m in pack["messages"] if m["chat"] == answer["chat"]}
    if not (question["chat"] == answer["chat"] and question["speaker"] not in {pack["target"], "unknown"}
            and answer["id"] not in pack["coverage"]["missing_reply_ids"]
            and chat.get("mode") == "p2p" and len(actors) == 2 and "unknown" not in actors
            and 0 < _timestamp(answer) - _timestamp(question) <= 300):
        return False
    messages = {m["id"]: m for m in pack["messages"]}
    previous = answer.get("previous")
    # At most two staff supplements, linked by real database predecessors, not excerpt order.
    for _ in range(3):
        message = messages.get(previous)
        if not message or message["chat"] != answer["chat"]:
            return False
        if message["id"] == question["id"]:
            return True
        reply = messages.get(message.get("reply"))
        if (message["speaker"] != question["speaker"]
                or not any(p["id"] == message["speaker"] and p["role"] == "staff" for p in pack.get("people", []))
                or message["id"] in pack["coverage"]["missing_reply_ids"] + pack["coverage"]["excerpted_ids"]
                or not _timestamp(question) < _timestamp(message) < _timestamp(answer)
                or not _staff_supplement(message["text"])
                or reply and reply["id"] != question["id"] and _QUESTION.search(reply["text"])
                or any(_mentions(message["text"], h) for h in [
                    *(c["handle"] for c in pack["handles"]), *pack.get("confirmed_handles", [])])):
            return False
        previous = message.get("previous")
    return False


def _main_claim(pack, message, handle):
    """A main-account shorthand needs personal account context and a matching company group."""
    body = message["text"]
    return (pack["creator_known"] and re.search(
                r"\bmy\s+main(?=\s+to\b|\s*[.!?,]|\s*$)|\bmy\s+main\s*(?:is\b|[:=])\s*@?"
                + re.escape(handle) + r"(?=\.*(?:[^A-Za-z0-9_.]|$))", body, re.I)
            and _QUESTION.search(body) and not _INTRO_PROXY.search(body) and not _NOT_OWNER.search(body)
            and not _SHARED_ACCOUNT.search(body) and not _denied(body)
            and any(c["id"] == message["chat"] and c.get("mode") == "group" and (
                match := handles._GROUP_NAME.match(re.sub(r"\([^()\r\n]*\)", "", c.get("name", ""))))
                and match[1].casefold() == handle for c in pack.get("chats", [])))


def _main_pair(pack, claim, account, handle):
    return (_main_claim(pack, claim, handle) and claim["speaker"] == account["speaker"]
            and _near_messages(claim, account) and _timestamp(claim) < _timestamp(account)
            and re.fullmatch(r"\s*@?" + re.escape(handle) + r"\.*\s*", account["text"], re.I))


def _support_groups(pack, candidate, refs=None):
    """Retrieval hints only; never used to veto or promote model conclusions."""
    messages = {m["id"]: m for m in pack["messages"]}
    allowed = messages.keys() if refs is None else set(refs)
    allowed = set(allowed) - set(pack["coverage"]["excerpted_ids"])
    own = sorted((m for m in pack["messages"] if m["speaker"] == pack["target"] and m["id"] in allowed),
                 key=_timestamp, reverse=True)
    handle = candidate["handle"]
    # Confirmed pairs are not output candidates, but their claims cannot be inherited by a new handle.
    contexts = [*pack["handles"], *({"handle": h} for h in pack.get("confirmed_handles", []))]
    others = [h for h in contexts if h["handle"] != handle]
    allow_intro = pack["creator_known"] and any(
        not _NOT_OWNER.search(part) and not _THIRD_PARTY.search(part) and not _SHARED_ACCOUNT.search(part)
        and not re.search(r"\b(?:not|never|haven't|haven’t|didn't|didn’t)\s+(?:\w+\s+)*$",
                          part[:activity.start()], re.I)
        and not re.search(r"\b(?:you|he|she|they)\b", part[:activity.start()], re.I)
        for m in pack["messages"] if m["speaker"] == pack["target"]
        for part in _parts(m["text"]) if (activity := _CREATOR_ACTIVITY.search(part)))
    groups, claims = [], {}

    def add(kind, references, claim):
        group = {"kind": kind, "refs": list(dict.fromkeys(references))}
        if group not in groups:
            groups.append(group)
        claims[claim["id"]] = claim

    for message in own:
        if (_self_claim(message["text"], handle, allow_intro=allow_intro)
                or _main_claim(pack, message, handle) and _mentions(message["text"], handle)):
            add("self_claim", [message["id"]], message)
        if _group_context(pack, message, handle):
            add("group_context", [message["id"]], message)
    for answer in own:
        parent = answer.get("reply") or answer.get("previous")
        questions = [messages[parent]] if answer.get("reply") and parent in messages else (
            [] if answer.get("reply") else [m for m in messages.values() if m["id"] in allowed])
        for question in questions:
            if _qa(pack, answer, question, handle, contexts, allowed, continuous=not bool(answer.get("reply"))):
                add("qa", [question["id"], answer["id"]], answer)
    for claim in own:
        if (not (_self_claim(claim["text"]) or _main_claim(pack, claim, handle)) or _mentions(claim["text"], handle)
                or any(_mentions(claim["text"], h["handle"]) for h in others)):
            continue
        for account in own:
            if (_near_messages(claim, account) and _mentions(account["text"], handle)
                    and not _THIRD_PARTY.search(account["text"]) and not _NOT_OWNER.search(account["text"])
                    and not _denied(account["text"]) and not _SHARED_ACCOUNT.search(account["text"])
                    and (not _main_claim(pack, claim, handle) or _main_pair(pack, claim, account, handle))):
                add("self_claim", [account["id"], claim["id"]], claim)
    return groups, list(claims.values())


def validate(pack, result):
    """Validate source facts and protocol, never reinterpret the model's language judgment."""
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
        candidate = candidates[link["h"]]
        if candidate["catalog_verified"] and link["code"] == "out_of_catalog":
            # Curva membership is a server fact. Correct the reason without upgrading evidence.
            _review(link, "unsupported_evidence", messages)
            link["code"] = "no_link_evidence"
            continue
        if link["decision"] != "supported":
            continue
        cited = [messages[ref] for ref in link["refs"]]
        own = [m for m in cited if m["speaker"] == pack["target"]]
        if not candidate["catalog_verified"]:
            _review(link, "out_of_catalog", messages)
            continue
        if not own or (link["code"] != "group_context"
                       and not any(_mentions(m["text"], candidate["handle"]) for m in cited)):
            _review(link, "unsupported_evidence", messages)
            continue
        if (link["relation"] not in {"self_account", "account_creator", "operator"} or link["period"] == "unknown"
                or link["code"] == "group_context" and link["relation"] != "self_account"):
            _review(link, "supported_relation", messages)
            continue
        if set(link["refs"]) & set(pack["coverage"]["excerpted_ids"]):
            _review(link, "excerpted_support", messages)
            continue
        if link["relation"] != "self_account":
            continue
        if link["code"] not in {"self_claim", "qa_confirm", "own_page", "group_context"}:
            _review(link, "owner_code", messages)
            continue
        if link["code"] == "group_context" and (not pack["creator_known"] or not any(
                c["id"] == m["chat"] and _group_handle(c) == candidate["handle"]
                for m in own for c in pack.get("chats", []))):
            _review(link, "unsupported_evidence", messages)
        elif link["code"] == "qa_confirm" and not _reply_refs(pack, own, cited):
            _review(link, "reply_relation", messages)
    return result


def _review(link, error, messages):
    """An invalid item cannot become writable by trimming its bad references."""
    needed = "parent_reply" if link["code"] == "qa_confirm" else "account_context"
    link.update(decision="review", relation="uncertain", period="unknown",
                code="out_of_catalog" if error == "out_of_catalog" else
                     "ambiguous_reply" if needed == "parent_reply" else "no_link_evidence",
                refs=list(dict.fromkeys(r for r in link["refs"] if r in messages))[:6],
                needed=list(dict.fromkeys([*link["needed"], needed])), validation_error=error)


def _reply_refs(pack, own, cited):
    """Check real reply/predecessor metadata; question intent and negation belong to AI."""
    messages = {m["id"]: m for m in pack["messages"]}
    cited_ids = {m["id"] for m in cited}
    unavailable = set(pack["coverage"]["missing_reply_ids"]) | set(pack["coverage"]["excerpted_ids"])
    for answer in own:
        if answer["id"] in unavailable:
            continue
        chat = next((c for c in pack.get("chats", []) if c["id"] == answer["chat"]), {})
        actors = {m["speaker"] for m in pack["messages"] if m["chat"] == answer["chat"]}
        if not answer.get("reply") and (len(actors) != 2 or "unknown" in actors):
            continue
        previous = answer.get("reply") or answer.get("previous")
        # Actual replies need the cited parent. Private adjacency needs an unbroken source chain.
        for _ in range(1 if answer.get("reply") else 3):
            question = messages.get(previous)
            if (not question or question["chat"] != answer["chat"] or question["id"] in unavailable
                    or question["speaker"] in {pack["target"], "unknown"}
                    or _timestamp(question) >= _timestamp(answer)):
                break
            if not answer.get("reply") and chat.get("mode") != "p2p":
                break
            if question["id"] in cited_ids:
                return True
            previous = question.get("previous")
    return False


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
    previous = _previous_ids(conn, [r for r in messages if "previous_id" in r])
    for ref in messages:
        row = current.get((ref["chat_id"], ref["message_id"], ref["authorization_id"]))
        raw = text._decrypt_body(cipher, row["body_ciphertext"]) if row else None
        if not row or raw is None or (_scope_hash(row) != ref["scope_hash"] or access._hash(raw) != ref["body_hash"]
                       or access._relation_hash(row) != ref["relation_hash"]
                       or "previous_id" in ref
                       and previous.get((ref["chat_id"], ref["message_id"])) != ref["previous_id"]):
            return False
    return bool(messages)
