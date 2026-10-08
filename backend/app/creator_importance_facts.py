"""Grounded cooperation facts and deterministic tiers; no provider or database access."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPT_VERSION = "creator-importance-facts-v2.0.2"
RULE_VERSION = "creator-importance-v2.0.3"
MAX_FACTS, MAX_REFS = 12, 8
KINDS = ("intent", "opportunity", "agreed", "ongoing", "completed", "progress", "interaction",
         "cancelled", "uncertain")
_ORDER = {kind: index for index, kind in enumerate(KINDS)}
LONDON = ZoneInfo("Europe/London")
_PRIMARY_KINDS = frozenset({"opportunity", "agreed", "ongoing", "progress"})
_PLATFORM_BENEFIT = re.compile(
    r"\b(?:platform\s+(?:benefits?|eligibility|incentives?|subsid(?:y|ies)|vouchers?)|"
    r"subsid(?:y|ies)|vouchers?|coupons?|flash[- ]sales?|(?:live|livestream)[- ]exclusive(?:s)?|"
    r"exclusive\s+(?:prices?|pricing|rates?|discounts?|benefits?)|commission(?:\s+(?:rates?|links?))?)\b|"
    r"平台(?:权益|资格|补贴)|补贴|优惠券|闪购|专属价|佣金", re.I,
)
_PLATFORM_OPERATION = re.compile(
    r"\b(?:activat(?:e|ed|ing|ion)|appl(?:y|ied|ying|ication)|eligib(?:le|ility)|support|benefits?|"
    r"platform|enable(?:d|ment)?|valid(?:ity)?|available|availability|"
    r"contact(?:ing|ed)?\s+(?:the\s+)?platform|links?|prices?|pricing|rates?)\b|"
    r"申请|激活|开通|资格|支持|权益|平台|联系|可用|有效|链接|价格", re.I,
)
_CONCRETE_DELIVERY = re.compile(
    r"\b(?:paid|sponsored)\b(?:\s+[\w'’-]+){0,3}\s+"
    r"(?:videos?|content|posts?|lives?|livestreams?|streams?|campaigns?|deliverables?)\b|"
    r"\bnew\b(?:\s+[\w'’-]+){0,3}\s+(?:videos?|content|posts?|lives?|livestreams?|streams?)\b"
    r".{0,24}\b(?:deliverables?|brief|fees?|quotes?|quotation|scope|deadline|post|publish|film|produce|create)\b|"
    r"(?:£|€|\$)\s*\d+(?:[.,]\d+)?[^\n]{0,24}\b"
    r"(?:videos?|content|posts?|lives?|livestreams?|streams?|campaigns?|deliverables?)\b|"
    r"\b(?:videos?|content|posts?|lives?|livestreams?|streams?|campaigns?|deliverables?)\b"
    r"[^\n]{0,24}(?:£|€|\$)\s*\d+(?:[.,]\d+)?|"
    r"(?:付费|新一轮).{0,12}(?:视频|内容|直播|活动|交付)|"
    r"(?:视频|内容|直播|活动).{0,12}(?:报价|费用|交付|范围|截止)", re.I,
)
_SCHEDULED_DELIVERY = re.compile(
    r"\b(?:for|due|scheduled\s+for|delivery\s+for)\s+"
    r"(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|weekend))\b.{0,12}"
    r"\b(?:videos?|content|posts?|lives?|livestreams?|streams?)\b|"
    r"\b(?:videos?|content|posts?|lives?|livestreams?|streams?)\b.{0,12}"
    r"\b(?:due|scheduled|confirmed)\s+(?:for\s+)?"
    r"(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|weekend))\b|"
    r"(?:安排|确认|交付).{0,8}(?:今天|今晚|明天|今早|本周末).{0,8}(?:视频|内容|直播|活动)|"
    r"(?:视频|内容|直播|活动).{0,8}(?:安排|确认|交付).{0,8}(?:今天|今晚|明天|今早|本周末)", re.I,
)
_STRONG_PLATFORM_OPERATION = re.compile(
    r"\b(?:activat(?:e|ed|ing|ion)|appl(?:y|ied|ying|ication)|eligib(?:le|ility)|"
    r"contact(?:ing|ed)?\s+(?:the\s+)?platform|platform\s+(?:support|benefits?|eligibility))\b|"
    r"申请|激活|开通|资格|联系平台|平台(?:支持|权益)", re.I,
)
_SPECIFIC_VIDEO_PROMOTION = re.compile(
    r"\b(?:ad|spark)[- ]?codes?\b|\b(?:video|ad(?:vertising)?)\s+authorization\s+codes?\b|"
    r"\b(?:videos?|posts?|content)\b.{0,32}\b(?:boost(?:ing|ed)?|paid\s+promotion)\b|"
    r"\b(?:boost(?:ing|ed)?|paid\s+promotion)\b.{0,32}\b(?:videos?|posts?|content)\b|"
    r"投流|广告码|视频授权码", re.I,
)
_COMPLETION = re.compile(
    r"\b(?:published|posted|uploaded|released|live\s+(?:finished|ended|done)|"
    r"(?:finished|completed|done)\b.{0,24}\b(?:lives?|streams?|videos?|campaigns?|content)|"
    r"(?:lives?|streams?|videos?|campaigns?|content)\b.{0,16}\b"
    r"(?:done|over|ended|finished|completed?|up))\b|"
    r"(?:直播|视频|内容|活动).{0,12}(?:结束|完成|发布|发出)|已发布|发了(?:视频|作品)|"
    r"(?:直播|视频|内容|活动)已完成", re.I,
)
_PROMOTION_COMPLETION = re.compile(
    r"\b(?:boost(?:ing)?|(?:paid\s+)?promotion|ad(?:vertising)?\s+campaign)\b.{0,24}"
    r"\b(?:completed?|done|over|ended|finished|ran)\b|"
    r"\b(?:completed?|finished|ran)\b.{0,24}"
    r"\b(?:boost(?:ing)?|(?:paid\s+)?promotion|ad(?:vertising)?\s+campaign)\b|"
    r"(?:投流|推广|广告活动).{0,12}(?:已完成|已结束|已执行)|"
    r"(?:完成|结束|执行了).{0,12}(?:投流|推广|广告活动)", re.I,
)
_NOT_FINISHED = re.compile(
    r"\b(?:not|haven['’]?t|hasn['’]?t|didn['’]?t|will|tomorrow|planning\s+to|going\s+to|need\s+to)\b|"
    r"尚未|还没|未发布|没有|没做|准备|计划|明天|将会", re.I,
)
_FINISHED_REPLY = re.compile(
    r"^\W*(?:yes\b|yep\b|done\b|finished\b|completed\b|I(?:['’]ve| have| already have| did)\b|"
    r"it(?:['’]s| is)\s+(?:done|up|finished)|发布了|已经发了|完成了|发好了|做完了|是的|对)", re.I,
)
_THIRD_DELIVERY = re.compile(
    r"\b(?:my|our)\s+(?:friend|colleague|assistant|partner)\b|"
    r"\b(?:his|her|their)\s+(?:video|live|stream|campaign)\b|"
    r"朋友|同事|助理|他的视频|她的视频|他们的直播", re.I,
)
_DELIVERY_ACTION = (
    r"(?:go\s+live\b(?!\s+exclusiv)|be\s+live\b(?!\s+exclusiv)|"
    r"(?:host|do|run|film|shoot|post|publish|upload|make|produce|deliver)\b"
    r"(?:\s+[\w’'-]+){0,5}\s+(?:lives?\b(?!\s+exclusiv)|streams?\b|videos?\b|content\b)|"
    r"(?:开播|直播|拍摄视频|发布视频|制作视频|发布内容))"
)
_DELIVERY = re.compile(_DELIVERY_ACTION, re.I)
_OWN_DELIVERY = re.compile(
    r"(?:\b(?:I|we)(?:['’]ll|\s+(?:will|am\s+going\s+to|are\s+going\s+to))?\s+"
    r"(?:(?:also|definitely|certainly|still)\s+){0,2}|"
    r"我(?:们)?(?:会|将|确认|每天|每日|每周|每月|每晚|定期|固定|再次){0,2})"
    + _DELIVERY_ACTION, re.I,
)
_REPEATED_DELIVERY = re.compile(
    r"\b(?:daily|weekly|monthly|regular|repeated|multiple|two|three|four|[2-9]\d*)\s+"
    r"(?:\w+\s+){0,2}(?:lives?|streams?|videos?|content)\b|"
    r"\b(?:lives?|streams?|videos?|content)(?:\s+(?:on|with|for)\s+\w+)?\s+"
    r"(?:again|(?:every|each)\s+(?:day|week|month|night|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\b|"
    r"(?:每天|每日|每周|每月|每晚|定期|固定|再次|多场|两场|三场)"
    r"(?:都|会|固定|安排|两场|三场){0,2}(?:直播|开播|视频|内容)|"
    r"(?:直播|开播|视频|内容)(?:每天|每日|每周|每月|每晚)?(?:多场|两场|三场|[2-9]\d*(?:场|次|条))", re.I,
)
_NO_DELIVERY = re.compile(
    r"\b(?:no|not|never|cannot|can't|won't|wouldn't|couldn't|might|maybe|if)\b|"
    r"不能|不会|不做|不播|不发|没法|不确定|如果|可能", re.I,
)
_ACCEPTED_REPLY = re.compile(r"^\W*(?:yes\b|yep\b|sure\b|agreed\b|confirmed\b|sounds\s+good\b|"
                             r"ok(?:ay)?\b|确认|同意|好的|可以|是的)", re.I)


def _linked_question(message, messages, chats):
    question = messages.get(message.get("reply_to"))
    if not message.get("reply_to") and chats.get(message.get("chat_id"), {}).get("private"):
        earlier = [m for m in messages.values() if m.get("chat_id") == message.get("chat_id")
                   and _time(m["created_at"]) < _time(message["created_at"])]
        previous = max(earlier, key=lambda m: _time(m["created_at"]), default=None)
        if previous and _time(message["created_at"]) - _time(previous["created_at"]) <= timedelta(minutes=30):
            question = previous
    return question


def _ongoing_kind(cited, messages, chats):
    """A recurring price benefit is not a creator's recurring delivery commitment."""
    refs, single = {m["ref"] for m in cited}, False
    for message in cited:
        if message["role"] != "creator":
            continue
        body = message.get("text", "")
        parts = re.split(r"(?<=[.!?;。！？；])|\n", body)
        for part in parts:
            if (_NO_DELIVERY.search(part) or _THIRD_DELIVERY.search(part)
                    or "?" in part or "？" in part
                    or re.match(r"\W*(?:can|could|would|will|shall|should|do|did|am|are)\s+(?:I|we)\b", part, re.I)):
                continue
            for own in _OWN_DELIVERY.finditer(part):
                single = True
                if any(repeat.start() < own.end() and repeat.end() > own.start()
                       for repeat in _REPEATED_DELIVERY.finditer(part)):
                    return "ongoing"
        question = _linked_question(message, messages, chats)
        answer = next((part for part in parts if part.strip()), "")
        if (not question or question["ref"] not in refs or question["role"] != "cm"
                or not _ACCEPTED_REPLY.search(answer) or _NO_DELIVERY.search(body)
                or _THIRD_DELIVERY.search(body) or "?" in answer or "？" in answer):
            continue
        for part in re.split(r"[\n.!?;。！？；]", question.get("text", "")):
            if _NO_DELIVERY.search(part) or _THIRD_DELIVERY.search(part):
                continue
            for delivery in _DELIVERY.finditer(part):
                single = True
                if any(repeat.start() < delivery.end() and repeat.end() > delivery.start()
                       for repeat in _REPEATED_DELIVERY.finditer(part)):
                    return "ongoing"
    return "agreed" if single else "intent"


def _completed_action(body, pattern, *, question=False):
    for part in re.split(r"(?<=[.!?;。！？；])|\n", body):
        if _THIRD_DELIVERY.search(part) or not question and ("?" in part or "？" in part):
            continue
        for match in pattern.finditer(part):
            action = re.split(r"[,，]|\b(?:and|but)\b|但是|但", part[:match.start()], flags=re.I)[-1] + match.group()
            if not _NOT_FINISHED.search(action):
                return True
    return False


def _completion_evidence(cited, messages, chats, obj):
    """Require a positive execution witness, including an explicit completed-action Q&A."""
    pattern = _PROMOTION_COMPLETION if _SPECIFIC_VIDEO_PROMOTION.search(obj) else _COMPLETION
    refs = {m["ref"] for m in cited}
    for message in cited:
        if message["role"] != "creator":
            continue
        body = message.get("text", "")
        if _completed_action(body, pattern):
            return True
        question = _linked_question(message, messages, chats)
        if (question and question["ref"] in refs and question["role"] == "cm"
                and _FINISHED_REPLY.search(body) and not _NOT_FINISHED.search(body)
                and not _THIRD_DELIVERY.search(body)
                and not _NOT_FINISHED.search(question.get("text", ""))
                and _completed_action(question.get("text", ""), pattern, question=True)):
            return True
    return False


@lru_cache(maxsize=1)
def instructions():
    document = (Path(__file__).resolve().parents[2] / "docs/specs/creator-importance-facts-prompt_v1.0.md").read_text()
    return document.split("```text\n", 1)[1].split("```", 1)[0].strip()


INSTRUCTIONS = instructions()


def output_format(refs, previous=()):
    fields = {
        "kind": {"type": "string", "enum": list(KINDS)},
        "object": {"type": "string", "minLength": 1, "maxLength": 160},
        "previous_key": {"type": ["string", "null"], "enum": [None, *dict.fromkeys(f["key"] for f in previous)]},
        "refs": {"type": "array", "minItems": 1, "maxItems": MAX_REFS,
                 "items": {"type": "string", "enum": list(dict.fromkeys(refs))}},
    }
    return {"type": "json_schema", "name": "creator_importance_facts_v1", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["facts"], "properties": {
            "facts": {"type": "array", "maxItems": MAX_FACTS, "items": {
                "type": "object", "additionalProperties": False, "required": list(fields), "properties": fields,
            }},
        },
    }}


def _time(value):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("missing source time")
        return value.astimezone(UTC)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("source time") from exc


def _object(value):
    return " ".join(value.split()).casefold()


def _effective_kind(kind, obj):
    """Narrowly keep platform-benefit operations out of maintenance-primary lifecycle kinds."""
    text = _object(obj)
    protected = (_CONCRETE_DELIVERY.search(text) or _SPECIFIC_VIDEO_PROMOTION.search(text)
                 or (_SCHEDULED_DELIVERY.search(text) and not _STRONG_PLATFORM_OPERATION.search(text)))
    support = bool(_PLATFORM_BENEFIT.search(text) and _PLATFORM_OPERATION.search(text) and not protected)
    return "interaction" if kind in _PRIMARY_KINDS and support else kind


def validate(raw, pack, previous=()):
    """Validate shape/author/time; weak claims do not erase previously grounded facts."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("json") from exc
    if not isinstance(raw, dict) or set(raw) != {"facts"}:
        raise ValueError("shape")
    if not isinstance(raw["facts"], list) or len(raw["facts"]) > MAX_FACTS:
        raise ValueError("facts bounds")
    messages = {m["ref"]: m for m in pack["messages"]}
    old = {f["key"]: f for f in previous}
    normalized, keys = [], {}
    for row in raw["facts"]:
        if not isinstance(row, dict) or set(row) != {"kind", "object", "previous_key", "refs"}:
            raise ValueError("fact shape")
        kind, obj, key, refs = row["kind"], row["object"], row["previous_key"], row["refs"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("kind")
        if not isinstance(obj, str) or not obj.strip() or len(obj) > 160:
            raise ValueError("business object")
        obj = " ".join(obj.split())
        if key is not None:
            if not isinstance(key, str) or key not in old:
                raise ValueError("previous object")
            obj = old[key]["object"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REFS
                or any(not isinstance(ref, str) or ref not in messages for ref in refs)):
            raise ValueError("refs")
        refs = sorted(set(refs))
        cited = [messages[ref] for ref in refs]
        for message in cited:
            creator = message.get("role") == "creator" and str(message.get("contact_id")) == str(pack["creator_id"])
            cm = message.get("role") == "cm" and message.get("author_id") == pack.get("cm_open_id")
            if not creator and not cm:
                raise ValueError("source author")
            if message.get("context_complete") is False:
                raise ValueError("incomplete reply")
        at = max(_time(m["created_at"]) for m in cited)
        key = key or keys.setdefault(_object(obj), "C" + hashlib.sha256(
            (_object(obj) + "\0" + min(refs)).encode()).hexdigest()[:20])
        roles = {message["role"] for message in cited}
        bilateral = {"creator", "cm"} <= roles
        normalized.append({"key": key, "kind": kind, "object": obj, "refs": refs,
                           "occurred_at": at.isoformat(), "business_dates": sorted({
                               _time(message["created_at"]).astimezone(LONDON).date().isoformat()
                               for message in cited
                           }) if bilateral else []})

    grounded = []
    for fact in normalized:
        roles = {messages[ref]["role"] for ref in fact["refs"]}
        bilateral = {"creator", "cm"} <= roles
        effective = _effective_kind(fact["kind"], fact["object"])
        if effective != fact["kind"]:
            fact["kind"] = "interaction" if bilateral else "intent"
        elif fact["kind"] == "interaction" and not bilateral:
            continue
        if fact["kind"] in {"opportunity", "agreed", "ongoing"} and not bilateral:
            fact["kind"] = "intent"
        elif fact["kind"] == "ongoing":
            fact["kind"] = _ongoing_kind([messages[ref] for ref in fact["refs"]], messages, pack.get("chats", {}))
        grounded.append(fact)
    normalized = grounded
    all_facts = [*previous, *normalized]
    for fact in normalized:
        cited = [messages[ref] for ref in fact["refs"]]
        progressable = any(f["key"] == fact["key"]
                           and _effective_kind(f["kind"], f["object"])
                           in {"opportunity", "agreed", "ongoing"}
                           and _time(f["occurred_at"]) <= _time(fact["occurred_at"])
                           for f in all_facts)
        adverse = max((_time(f["occurred_at"]) for f in all_facts
                       if f["key"] == fact["key"] and f["kind"] in {"cancelled", "uncertain"}
                       and _time(f["occurred_at"]) <= _time(fact["occurred_at"])), default=None)
        if adverse is not None:
            progressable = any(f["key"] == fact["key"]
                               and _effective_kind(f["kind"], f["object"])
                               in {"opportunity", "agreed", "ongoing"}
                               and adverse < _time(f["occurred_at"]) <= _time(fact["occurred_at"])
                               for f in all_facts)
        if fact["kind"] == "completed":
            if not progressable or not _completion_evidence(cited, messages, pack.get("chats", {}), fact["object"]):
                fact["kind"] = "intent"
        elif fact["kind"] == "progress" and not progressable:
            fact["kind"] = "intent"
    return merge_facts((), normalized)


def prompt_previous(previous):
    """Compact model context, not storage: retain each object and necessary lifecycle anchors."""
    by_object = defaultdict(list)
    for fact in merge_facts((), previous):
        kind = _effective_kind(fact["kind"], fact["object"])
        by_object[fact["key"]].append(fact if kind == fact["kind"] else {**fact, "kind": kind})
    output = []
    for events in by_object.values():
        selected = []
        for kind in ({"opportunity", "agreed", "ongoing", "completed", "cancelled", "uncertain"},
                     {"opportunity", "agreed", "ongoing"}, {"completed"}, {"cancelled", "uncertain"}):
            match = next((fact for fact in reversed(events) if fact["kind"] in kind), None)
            if match is not None and match not in selected:
                selected.append(match)
        if not selected:
            selected = [match] if (match := next((fact for fact in reversed(events)
                                                  if fact["kind"] != "interaction"), None)) else []
        output.extend({name: fact[name] for name in ("key", "kind", "object", "occurred_at")} for fact in selected)
    return sorted(output, key=lambda fact: (fact["key"], _time(fact["occurred_at"]), _ORDER[fact["kind"]]))


def merge_facts(previous, incoming):
    facts = {}
    for fact in [*previous, *incoming]:
        identity = (fact["key"], fact["kind"], tuple(sorted(set(fact["refs"]))))
        current = facts.get(identity, {})
        facts[identity] = {**fact, "refs": sorted(set(fact["refs"])), "business_dates": sorted(set(
            current.get("business_dates", ())) | set(fact.get("business_dates", ())))}
    return sorted(facts.values(), key=lambda f: (_time(f["occurred_at"]), f["key"], _ORDER[f["kind"]], f["refs"]))


def rank(facts, now=None, *, readable=False):
    """Current maintenance priority from grounded events, never message volume or model confidence."""
    now = _time(now) if now is not None else datetime.now(UTC)
    groups = defaultdict(list)
    for fact in merge_facts((), facts):
        groups[fact["key"]].append(fact)
    active, single, history, intent = [], [], [], []
    business_at, progress_at, business_dates, historical = None, None, set(), False
    for events in groups.values():
        state, primary, repeat, completed = None, None, False, False
        last_completed = None
        for event in events:
            if _time(event["occurred_at"]) > now:
                continue
            kind = _effective_kind(event["kind"], event["object"])
            if kind in {"intent", "opportunity", "agreed", "ongoing", "completed", "progress", "interaction"}:
                if business_at is None or _time(event["occurred_at"]) > _time(business_at):
                    business_at = event["occurred_at"]
                business_dates.update(event.get("business_dates", ()))
            if kind in {"agreed", "ongoing", "completed"}:
                historical = True
            if kind == "opportunity":
                state = primary = event
            elif kind in {"agreed", "ongoing"}:
                repeat = repeat or completed or kind == "ongoing"
                state = primary = event
            elif kind == "completed":
                if state and state["kind"] in {"cancelled", "uncertain"}:
                    continue
                state, primary = event, None
                completed, last_completed = True, event
            elif kind in {"cancelled", "uncertain"}:
                state, primary, repeat = event, None, False
                if kind == "uncertain":
                    completed, last_completed = False, None
            elif kind == "intent" and state is None:
                state = event
            elif kind == "progress" and state and state["kind"] in {"opportunity", "agreed", "ongoing"}:
                primary = event
            if kind in {"agreed", "ongoing", "completed", "progress"}:
                if progress_at is None or _time(event["occurred_at"]) > _time(progress_at):
                    progress_at = event["occurred_at"]
        if state is None:
            continue
        row = {"object": state["object"], "at": primary["occurred_at"] if primary else state["occurred_at"],
               "kind": state["kind"]}
        if primary is not None:
            primary_kind = primary["kind"]
            active.append({"object": primary["object"], "at": primary["occurred_at"], "kind": primary_kind,
                           "phase": "intent" if state["kind"] == "opportunity" else
                                    "ongoing" if repeat or state["kind"] == "ongoing" else "single",
                           "label": "具体机会" if primary_kind == "opportunity" else
                                    "实质推进" if primary_kind == "progress" else "交付安排"})
        if state["kind"] in {"agreed", "ongoing"}:
            single.append(row)
        elif state["kind"] == "completed":
            history.append(row)
        elif state["kind"] == "cancelled" and last_completed is not None:
            history.append({"object": last_completed["object"], "at": last_completed["occurred_at"],
                            "kind": "completed"})
        elif state["kind"] == "intent":
            intent.append(row)
    primary_at = max((row["at"] for row in active), key=_time, default=None)
    current = primary_at is not None and _time(primary_at) >= now - timedelta(days=30)
    readable = bool(readable or groups)
    if active:
        selected, phase = active, None
    elif single:
        selected, phase = single, "single"
    elif history:
        selected, phase = history, "history"
    else:
        selected = intent
        phase = "intent" if intent else "unknown"
    tier = "focus" if current else "normal" if readable else "pending"
    selected.sort(key=lambda row: (_time(row["at"]), row["object"]), reverse=True)
    if active:
        phase = selected[0]["phase"]
    recent_date = now.astimezone(LONDON).date() - timedelta(days=29)
    today = now.astimezone(LONDON).date()
    business_dates = sorted(day for day in business_dates if datetime.fromisoformat(day).date() <= today)
    recent_business_dates = [day for day in business_dates
                             if datetime.fromisoformat(day).date() >= recent_date]
    priority = {"primary_at": primary_at, "business_at": business_at,
                "business_days": business_dates, "historical": historical}
    if tier == "focus":
        lead = selected[0]
        grounds = [f"{lead['object'][:48]}：{lead['label']}"]
        latest = max((at for at in (progress_at, primary_at) if at), key=_time)
        latest = _time(latest).astimezone(LONDON).strftime("%m-%d")
        grounds.append(f"近30天业务交流{len(recent_business_dates)}天 · 最近推进{latest}")
    elif tier == "normal":
        grounds = ["近期无重点事项"] + (["已有历史合作记录"] if historical else [])
    else:
        grounds = ["尚无完整可读资料"]
    return {"tier": tier, "phase": phase, "last_progress_at": progress_at,
            "grounds": grounds,
            "needs_update": bool(readable and business_at and _time(business_at) < now - timedelta(days=30)),
            "primary_at": primary_at, "recent_business_dates": recent_business_dates,
            "has_history": historical, "priority": priority}
