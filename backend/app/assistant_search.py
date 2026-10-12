"""Bounded, consented keyword search over a CM's formal-creator conversations."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from uuid import UUID

from cryptography.exceptions import InvalidTag

from . import offhours_summary as core
from .crypto import TokenCipher, message_body_cipher
from .message_mentions import mention_names

SCAN_LIMIT = 2000
PAGE_SIZE = 12
BATCH_SIZE = 100
TEXT_LIMIT = 480
CONTEXT_LIMIT = 8

_BASE_SQL = core._MESSAGE_SQL.split("    ORDER BY", 1)[0].replace(
    "m.chat_id=%s", "m.chat_id=ANY(%s::text[])"
)
_ORDER = " ORDER BY m.message_create_time DESC,m.chat_id DESC,m.message_id DESC LIMIT %s FOR SHARE OF m,o"


def _cipher(settings):
    return TokenCipher(hmac.digest(settings.token_encryption_key, b"assistant-history/v1", "sha256"))


def _seal(value, prefix, settings):
    return prefix + _cipher(settings).encrypt(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _open(value, prefix, settings):
    try:
        if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 100_000:
            raise ValueError
        result = json.loads(_cipher(settings).decrypt(value[len(prefix):]))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, InvalidTag) as exc:
        raise ValueError("消息或分页引用无效，请重新检索。") from exc


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("检索时间必须是包含时区的 ISO 8601。") from exc


def _scope(conn, actor_id, context, creator_ids, chat_ids, settings):
    actor_id = UUID(str(actor_id))
    cm_id = UUID(context["scope"]["cm_id"])
    cm, _ = core._authorize(conn, actor_id, cm_id=cm_id)
    roster = core._roster(conn, cm_id)
    visible = {str(row["contact_id"]) for row in roster}
    current = context["scope"].get("creator_ids")
    selected = creator_ids if creator_ids is not None else current
    if selected is not None:
        if not isinstance(selected, list) or not all(isinstance(value, str) for value in selected):
            raise ValueError("达人范围无效。")
        selected = sorted({str(UUID(value)) for value in selected})
        allowed = visible
        if not set(selected) <= allowed:
            raise PermissionError("达人范围已变化，请重新检索。")
        roster = [row for row in roster if str(row["contact_id"]) in selected]
    chats = core._related_chats(conn, roster, settings=settings, cm_id=cm_id)
    if chat_ids is not None:
        if (not isinstance(chat_ids, list) or not all(isinstance(value, str) for value in chat_ids)
                or not set(chat_ids) <= {chat["chat_id"] for chat in chats}):
            raise PermissionError("会话不存在或不属于当前可读范围。")
        chats = [chat for chat in chats if chat["chat_id"] in set(chat_ids)]
    staff = core._staff(conn, cm)
    speakers = sorted({row["open_id"] for row in roster} | set(staff))
    return cm, roster, chats, speakers, selected


def _rows(conn, cm_id, chats, start, end, speakers, *, limit, after=None, extra="", extra_args=()):
    if not chats or not speakers:
        return []
    sql = _BASE_SQL + extra
    values = [cm_id, chats, start, end, speakers, *extra_args]
    if after:
        sql += " AND (m.message_create_time,m.chat_id,m.message_id)<(%s,%s,%s)"
        values.extend((_time(after[0]), after[1], after[2]))
    return conn.execute(sql + _ORDER, (*values, limit)).fetchall()


def _text(row, cipher, names):
    try:
        return core._body(row, cipher, names)
    except (ValueError, TypeError, UnicodeError, InvalidTag):
        return "[正文不可读取，需人工查看原消息]", True


def _version(row):
    return core._hash([row.get("body_fingerprint") or row.get("body_ciphertext"),
                       row.get("sender_open_id"), row.get("message_type"), row.get("mentions_json"),
                       row.get("parent_id"), row.get("root_id")])


def _reference(actor_id, cm, chat, row, start, end, selected, settings):
    return _seal({"actor_id": str(actor_id), "cm_id": str(cm["id"]),
                  "consent_at": cm["body_consent_at"].isoformat(),
                  "creator_ids": selected,
                  "chat_id": chat["chat_id"], "message_id": row["message_id"],
                  "start": start.isoformat(), "end": end.isoformat(), "version": _version(row)},
                 "m.", settings)


def _message(row, chat, text, unavailable, *, reference=None, terms=(), clip=True):
    matched = [term for term in terms if term.casefold() in text.casefold()]
    offset = 0
    if clip and matched and len(text) > TEXT_LIMIT:
        offset = max(0, min(text.casefold().find(term.casefold()) for term in matched) - 200)
    snippet = text[offset:offset + TEXT_LIMIT] if clip else text
    return {"message_ref": reference, "evidence_ref": reference, "message_id": row["message_id"],
            "chat_id": chat["chat_id"], "conversation_name": chat["name"] or "会话",
            "sender_name": row.get("sender_name") or "未命名人员",
            "contact_id": str(row["contact_id"]) if row.get("contact_id") else None,
            "time": row["message_create_time"].isoformat(),
            "created_at": row["message_create_time"].isoformat(), "text": snippet,
            "unreadable": unavailable, "unavailable_or_truncated": unavailable or len(snippet) < len(text),
            "parent_message_id": row.get("parent_id"), "root_message_id": row.get("root_id"),
            "matched_terms": matched}


def _coverage(chats, start, end):
    gaps, sources = [], []
    for chat in chats:
        watermark = max((value for value in (chat["last_incremental_at"], chat["last_backfill_at"])
                         if value), default=None)
        reasons = []
        if not chat["backfill_complete"] or not chat["backfill_window_start_at"] \
                or chat["backfill_window_start_at"] > start:
            reasons.append("历史同步未覆盖检索起点")
        if chat["sync_status"] != "succeeded" or not watermark or watermark < end:
            reasons.append("同步尚未覆盖检索截止")
        sources.append({"chat_id": chat["chat_id"], "last_sync_at": watermark.isoformat() if watermark else None,
                        "source_complete": not reasons})
        gaps.extend({"chat_id": chat["chat_id"], "reason": reason} for reason in reasons)
    return sources, gaps


def _scope_version(cm, roster, chats, selected):
    return core._hash([str(cm["id"]), cm["body_consent_at"], selected,
                      sorted((str(row["contact_id"]), row["open_id"]) for row in roster),
                      sorted(chat["chat_id"] for chat in chats)])


def query(conn, actor_id, context, args, settings):
    terms = args.get("query")
    if (not isinstance(terms, list) or not 1 <= len(terms) <= 8
            or not all(isinstance(term, str) and term.strip() and len(term) <= 256 for term in terms)):
        raise ValueError("请提供一至八个有效检索词。")
    terms = list(dict.fromkeys(term.strip() for term in terms))
    start, end = _time(args.get("start")), _time(args.get("end"))
    if start >= end:
        raise ValueError("检索结束时间必须晚于开始时间。")
    cm, roster, chats, speakers, selected = _scope(
        conn, actor_id, context, args.get("creator_ids"), args.get("chat_ids"), settings)
    by_chat = {chat["chat_id"]: chat for chat in chats}
    scope_version = _scope_version(cm, roster, chats, selected)
    signature = core._hash([str(actor_id), scope_version, start, end,
                           sorted(term.casefold() for term in terms)])
    after = None
    if args.get("cursor") is not None:
        cursor = _open(args["cursor"], "c.", settings)
        if cursor.get("signature") != signature:
            raise ValueError("检索范围或授权已变化，请重新检索。")
        after = cursor.get("after")
        if (not isinstance(after, list) or len(after) != 3 or not all(isinstance(value, str) for value in after)
                or after[1] not in by_chat or not start <= _time(after[0]) < end):
            raise ValueError("分页引用无效，请重新检索。")
    cipher = message_body_cipher(settings.token_encryption_key)
    messages, scanned, readable, unreadable, more = [], 0, 0, 0, False
    while scanned < SCAN_LIMIT and len(messages) < PAGE_SIZE:
        size = min(BATCH_SIZE, SCAN_LIMIT - scanned)
        rows = _rows(conn, cm["id"], sorted(by_chat), start, end, speakers, limit=size + 1, after=after)
        names = mention_names(conn, rows[:size])
        consumed = 0
        for row in rows[:size]:
            consumed += 1
            scanned += 1
            after = [row["message_create_time"].isoformat(), row["chat_id"], row["message_id"]]
            text, unavailable = _text(row, cipher, names)
            unreadable += int(unavailable)
            readable += int(not unavailable)
            if not unavailable and any(term.casefold() in text.casefold() for term in terms):
                chat = by_chat[row["chat_id"]]
                ref = _reference(actor_id, cm, chat, row, start, end, selected, settings)
                messages.append(_message(row, chat, text, unavailable, reference=ref, terms=terms))
            if len(messages) >= PAGE_SIZE:
                break
        more = len(rows) > consumed
        if not more:
            break
    sources, gaps = _coverage(chats, start, end)
    if unreadable:
        gaps.append({"reason": "本页存在不可读正文或附件", "count": unreadable})
    status = "partial" if more else ("completed" if messages else
             "no_messages" if not scanned else "no_readable_messages" if not readable else "no_matches")
    return {"kind": "messages", "query_id": signature, "status": status, "messages": messages,
            "scope": {**context["scope"], "creator_ids": selected,
                      "chat_ids": args.get("chat_ids"), "query": terms},
            "window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": core.TIMEZONE},
            "coverage": {"scanned": scanned, "readable": readable, "unreadable": unreadable,
                         "matched": len(messages), "scan_complete": not more,
                         "conversations": len(chats),
                         "source_complete": bool(chats) and not gaps, "counts_scope": "this_page"},
            "sources": sources, "gaps": gaps,
            "snapshot": _seal({"actor_id": str(actor_id), "cm_id": str(cm["id"]),
                               "creator_ids": selected, "chat_ids": args.get("chat_ids"),
                               "version": scope_version, "start": start.isoformat(), "end": end.isoformat()},
                              "s.", settings),
            "next_cursor": _seal({"signature": signature, "after": after}, "c.", settings) if more else None}


def validate_result(conn, actor_id, context, result, settings):
    """Recheck at most one page of hits, never rerun a negative full-text scan."""
    if result.get("kind") == "message_context":
        fresh = read(conn, actor_id, context, {"message_ref": result.get("message_ref")}, settings)
        fields = ("message", "messages", "window", "scope")
        if core._hash([result.get(key) for key in fields]) != core._hash([fresh.get(key) for key in fields]):
            raise ValueError("消息上下文已变化，请重新查看。")
        # Routine sync watermarks can close a gap without changing source text or grants.
        if ([gap for gap in result.get("gaps", []) if not gap.get("chat_id")]
                != [gap for gap in fresh.get("gaps", []) if not gap.get("chat_id")]):
            raise ValueError("引用消息的可读上下文已变化。")
        return
    value = _open(result.get("snapshot"), "s.", settings)
    if value.get("actor_id") != str(actor_id) or context["scope"]["cm_id"] != value.get("cm_id"):
        raise PermissionError("检索结果不属于当前账号或范围。")
    cm, roster, chats, speakers, selected = _scope(
        conn, actor_id, context, value.get("creator_ids"), value.get("chat_ids"), settings)
    if _scope_version(cm, roster, chats, selected) != value.get("version"):
        raise PermissionError("检索授权或正式达人范围已变化。")
    messages = result.get("messages")
    if not isinstance(messages, list) or len(messages) > PAGE_SIZE:
        raise ValueError("检索结果无效。")
    by_chat = {chat["chat_id"]: chat for chat in chats}
    start, end = _time(value.get("start")), _time(value.get("end"))
    for message in messages:
        ref = _open(message.get("message_ref"), "m.", settings)
        if (ref.get("actor_id") != str(actor_id) or ref.get("cm_id") != str(cm["id"])
                or ref.get("chat_id") not in by_chat or ref.get("consent_at") != cm["body_consent_at"].isoformat()
                or _time(ref.get("start")) != start or _time(ref.get("end")) != end):
            raise PermissionError("消息引用不属于当前检索范围。")
        rows = _rows(conn, cm["id"], [ref["chat_id"]], start, end, speakers, limit=1,
                     extra=" AND m.message_id=%s", extra_args=(ref.get("message_id"),))
        if not rows:
            raise PermissionError("命中消息已删除或当前授权无法读取。")
        if _version(rows[0]) != ref.get("version"):
            raise ValueError("命中消息已更新，请重新检索。")


def read(conn, actor_id, context, args, settings):
    ref = args.get("message_ref")
    value = _open(ref, "m.", settings)
    if value.get("actor_id") != str(actor_id):
        raise PermissionError("消息不属于当前账号。")
    if context is None:
        context = {"scope": {"cm_id": value.get("cm_id"), "creator_ids": value.get("creator_ids")}}
    if context["scope"]["cm_id"] != value.get("cm_id"):
        raise PermissionError("消息不属于当前查询范围。")
    start, end = _time(value.get("start")), _time(value.get("end"))
    cm, _roster, chats, speakers, _selected = _scope(
        conn, actor_id, context, value.get("creator_ids"), [value.get("chat_id")], settings)
    if start >= end or not chats:
        raise ValueError("消息引用无效。")
    if value.get("consent_at") != cm["body_consent_at"].isoformat():
        raise PermissionError("消息正文授权已变化，请重新检索。")
    chat = chats[0]
    target = _rows(conn, cm["id"], [chat["chat_id"]], start, end, speakers, limit=1,
                   extra=" AND m.message_id=%s", extra_args=(value.get("message_id"),))
    if not target:
        raise PermissionError("消息已删除或当前授权无法读取。")
    target = target[0]
    if value.get("version") != _version(target):
        raise ValueError("消息已更新，请重新检索后查看。")
    collected = {target["message_id"]: (target, "matched")}
    gaps, pending = [], [(target.get("parent_id"), "parent"), (target.get("root_id"), "root")]
    visited = {target["message_id"]}
    while pending and len(visited) < CONTEXT_LIMIT:
        message_id, relation = pending.pop(0)
        if not message_id or message_id in visited:
            continue
        visited.add(message_id)
        rows = _rows(conn, cm["id"], [chat["chat_id"]], datetime.min.replace(tzinfo=UTC), end,
                     speakers, limit=1, extra=" AND m.message_id=%s", extra_args=(message_id,))
        if not rows:
            gaps.append({"reason": "引用的原问或线程消息无法在当前授权下读取", "relation": relation})
            continue
        row = rows[0]
        collected[message_id] = (row, relation)
        pending.extend(((row.get("parent_id"), "parent"), (row.get("root_id"), "root")))
    if any(message_id and message_id not in visited for message_id, _ in pending):
        gaps.append({"reason": "引用链超过本次上下文上限"})
    nearby = _rows(conn, cm["id"], [chat["chat_id"]], start, end, speakers, limit=7,
                   extra=" AND m.message_create_time<=%s", extra_args=(target["message_create_time"],))
    following_sql = (_BASE_SQL + " AND m.message_create_time>%s"
                     + _ORDER.replace(" DESC", " ASC"))
    following = conn.execute(following_sql, (cm["id"], [chat["chat_id"]], start, end,
        speakers, target["message_create_time"], 4)).fetchall()
    for row in [*nearby, *following]:
        collected.setdefault(row["message_id"], (row, "nearby_context"))
    rows = [row for row, _ in collected.values()]
    names, cipher = mention_names(conn, rows), message_body_cipher(settings.token_encryption_key)
    messages = []
    for row, relation in sorted(collected.values(), key=lambda item: (
            item[0]["message_create_time"], item[0]["message_id"])):
        text, unavailable = _text(row, cipher, names)
        message = _message(row, chat, text, unavailable, reference=ref if relation == "matched" else None,
                           clip=False)
        message.update(relation=relation, in_window=row["message_create_time"] >= start)
        messages.append(message)
        if message["unavailable_or_truncated"]:
            gaps.append({"reason": "上下文正文不可读取或超过展示长度", "message_id": row["message_id"]})
    sources, sync_gaps = _coverage(chats, start, end)
    return {"kind": "message_context", "message_ref": ref, "evidence_ref": ref,
            "scope": {"cm_id": str(cm["id"]), "creator_ids": _selected, "chat_ids": [chat["chat_id"]]},
            "message": next(message for message in messages if message["relation"] == "matched"),
            "messages": messages, "gaps": [*gaps, *sync_gaps], "sources": sources,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": core.TIMEZONE},
            "context_complete": not gaps and not sync_gaps,
            "context_scope": "referenced_messages_and_bounded_nearby", "is_full_conversation": False}
