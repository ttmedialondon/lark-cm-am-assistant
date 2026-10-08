"""Grounded cooperation facts and deterministic tiers; no provider or database access."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "creator-importance-facts-v1.0.1"
RULE_VERSION = "creator-importance-v1.0"
MAX_FACTS, MAX_REFS = 12, 8
KINDS = ("intent", "agreed", "ongoing", "completed", "cancelled", "progress", "uncertain")
_ORDER = {kind: index for index, kind in enumerate(KINDS)}
_COMPLETION = re.compile(
    r"\b(?:published|posted|uploaded|released|live\s+(?:finished|ended|done)|"
    r"(?:finished|completed|done)\b.{0,24}\b(?:live|stream|video|campaign)|"
    r"(?:live|stream|video|campaign)\b.{0,16}\b(?:done|over|ended|finished|complete|up))\b|"
    r"(?:直播|视频|内容|活动).{0,12}(?:结束|完成|发布|发出)|已发布|发了(?:视频|作品)|"
    r"https?://(?:www\.)?tiktok\.com/@[^\s/]+/video/", re.I,
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
    r"\b(?:my|our)\s+(?:friend|colleague)\b|\b(?:his|her|their)\s+(?:video|live|stream|campaign)\b|"
    r"朋友|同事|他的视频|她的视频|他们的直播", re.I,
)


def _completion_evidence(cited, messages, chats):
    """Require a positive execution witness, including an explicit completed-action Q&A."""
    refs = {m["ref"] for m in cited}
    for message in cited:
        if message["role"] != "creator":
            continue
        body = message.get("text", "")
        clauses = re.split(r"[\n.!?;。！？；]", body)
        if any(_COMPLETION.search(part) and not _NOT_FINISHED.search(part)
               and not _THIRD_DELIVERY.search(part) for part in clauses):
            return True
        question = messages.get(message.get("reply_to"))
        if not message.get("reply_to") and chats.get(message.get("chat_id"), {}).get("private"):
            earlier = [m for m in messages.values() if m.get("chat_id") == message.get("chat_id")
                       and _time(m["created_at"]) < _time(message["created_at"])]
            previous = max(earlier, key=lambda m: _time(m["created_at"]), default=None)
            if previous and _time(message["created_at"]) - _time(previous["created_at"]) <= timedelta(minutes=30):
                question = previous
        if (question and question["ref"] in refs and question["role"] == "cm"
                and _FINISHED_REPLY.search(body) and not _NOT_FINISHED.search(body)
                and not _THIRD_DELIVERY.search(body)
                and _COMPLETION.search(question.get("text", ""))
                and not _NOT_FINISHED.search(question.get("text", ""))):
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
        normalized.append({"key": key, "kind": kind, "object": obj, "refs": refs,
                           "occurred_at": at.isoformat()})

    for fact in normalized:
        roles = {messages[ref]["role"] for ref in fact["refs"]}
        if fact["kind"] in {"agreed", "ongoing"} and not {"creator", "cm"} <= roles:
            fact["kind"] = "intent"
    all_facts = [*previous, *normalized]
    for fact in normalized:
        cited = [messages[ref] for ref in fact["refs"]]
        established = any(f["key"] == fact["key"] and f["kind"] in {"agreed", "ongoing"}
                          and _time(f["occurred_at"]) <= _time(fact["occurred_at"]) for f in all_facts)
        adverse = max((_time(f["occurred_at"]) for f in all_facts
                       if f["key"] == fact["key"] and f["kind"] in {"cancelled", "uncertain"}
                       and _time(f["occurred_at"]) <= _time(fact["occurred_at"])), default=None)
        if adverse is not None:
            established = any(f["key"] == fact["key"] and f["kind"] in {"agreed", "ongoing"}
                              and adverse < _time(f["occurred_at"]) <= _time(fact["occurred_at"])
                              for f in all_facts)
        if fact["kind"] == "completed":
            if not established or not _completion_evidence(cited, messages, pack.get("chats", {})):
                fact["kind"] = "intent"
        elif fact["kind"] == "progress" and not established:
            fact["kind"] = "intent"
    return merge_facts((), normalized)


def prompt_previous(previous):
    """Compact model context, not storage: retain each object and necessary lifecycle anchors."""
    by_object = defaultdict(list)
    for fact in merge_facts((), previous):
        by_object[fact["key"]].append(fact)
    output = []
    for events in by_object.values():
        selected = []
        for kind in ({"agreed", "ongoing", "completed", "cancelled", "uncertain"}, {"agreed", "ongoing"},
                     {"completed"}, {"cancelled", "uncertain"}):
            match = next((fact for fact in reversed(events) if fact["kind"] in kind), None)
            if match is not None and match not in selected:
                selected.append(match)
        if not selected:
            selected = [events[-1]]
        output.extend({name: fact[name] for name in ("key", "kind", "object", "occurred_at")} for fact in selected)
    return sorted(output, key=lambda fact: (fact["key"], _time(fact["occurred_at"]), _ORDER[fact["kind"]]))


def merge_facts(previous, incoming):
    facts = {}
    for fact in [*previous, *incoming]:
        identity = (fact["key"], fact["kind"], tuple(sorted(set(fact["refs"]))))
        facts[identity] = {**fact, "refs": sorted(set(fact["refs"]))}
    return sorted(facts.values(), key=lambda f: (_time(f["occurred_at"]), f["key"], _ORDER[f["kind"]], f["refs"]))


def rank(facts, now=None):
    """Sort factual lifecycle events, not messages, handles or model confidence."""
    now = _time(now) if now is not None else datetime.now(UTC)
    groups = defaultdict(list)
    for fact in merge_facts((), facts):
        groups[fact["key"]].append(fact)
    focus, single, history, intent = [], [], [], []
    for events in groups.values():
        state, repeat, completed = None, False, False
        progress, last_completed = None, None
        for event in events:
            kind = event["kind"]
            if kind in {"agreed", "ongoing"}:
                repeat = repeat or completed or kind == "ongoing"
                state = event
            elif kind == "completed":
                if state and state["kind"] in {"cancelled", "uncertain"}:
                    continue
                state = event
                completed, last_completed = True, event
            elif kind in {"cancelled", "uncertain"}:
                state, repeat = event, False
                if kind == "uncertain":
                    completed, last_completed = False, None
            elif kind == "intent" and state is None:
                state = event
            if kind in {"agreed", "ongoing", "completed", "progress"}:
                progress = event["occurred_at"]
        if state is None:
            continue
        row = {"object": state["object"], "at": progress, "kind": state["kind"]}
        if state["kind"] in {"agreed", "ongoing"}:
            (focus if repeat else single).append(row)
        elif state["kind"] == "completed":
            history.append(row)
        elif state["kind"] == "cancelled" and last_completed is not None:
            history.append({"object": last_completed["object"], "at": last_completed["occurred_at"],
                            "kind": "completed"})
        elif state["kind"] == "intent":
            intent.append(row)
    if focus:
        selected, tier, phase, label = focus, "focus", "ongoing", "持续／再次合作"
    elif single:
        selected, tier, phase, label = single, "normal", "single", "已成立单次合作"
    elif history:
        selected, tier, phase, label = history, "normal", "history", "已完成合作"
    else:
        selected, tier = intent, "pending"
        phase, label = ("intent", "仅有合作意向") if intent else ("unknown", "暂无可靠合作事实")
    selected.sort(key=lambda row: (row["at"] or "", row["object"]), reverse=True)
    at = max((row["at"] for row in selected if row["at"]), default=None)
    return {"tier": tier, "phase": phase, "last_progress_at": at,
            "grounds": [f"{row['object'][:48]}：{label}" for row in selected[:2]] or [label],
            "needs_update": bool(tier != "pending" and at and _time(at) < now - timedelta(days=30))}
