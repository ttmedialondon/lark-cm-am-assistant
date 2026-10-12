"""Presentation metadata from actual event evidence, never business state changes."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def counts(events):
    groups = {key: sum(event.get("action_group") == key for event in events)
              for key in ("reply", "followup", "waiting", "closed")}
    focus = [event for event in events if event.get("importance", {}).get("tier") == "focus"]
    known = all(event.get("importance", {}).get("tier") for event in events)
    return {"events": len(events), "groups": groups,
            "focus_events": len(focus) if known else None,
            "focus_creators": len({event["contact_id"] for event in focus}) if known else None,
            "due_today": sum(bool(event.get("due_today")) for event in events)}


def timestamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def decorate(event, cutoff=None, *, today=None):
    closed = event.get("handling_status") in {"resolved", "not_required"}
    group = event.get("action_group")
    if closed:
        group = "closed"
    elif group not in {"reply", "followup", "waiting"}:
        group = "followup" if event.get("replied_by") or event.get("handling_status") == "uncertain" else "reply"
    messages = {
        (message.get("chat_id"), message.get("message_id"), message.get("created_at", message.get("time"))): message
        for field in ("evidence", "handling_evidence", "reply_evidence", "waiting_evidence")
        for message in event.get(field, [])}
    staff = list(event.get("reply_evidence", []))
    latest_reply = max((at for message in staff if (at := timestamp(
        message.get("created_at", message.get("time"))))), default=None)
    customers = [at for message in messages.values()
                 if not message.get("is_staff") and message.get("speaker") != "CM"
                 and message not in staff and not message.get("unreadable")
                 and (at := timestamp(message.get("created_at", message.get("time"))))
                 and (latest_reply is None or at > latest_reply)]
    started = min(customers, default=None) if group in {"reply", "followup"} else None
    explicit = event.get("waiting_evidence")
    if explicit is not None and group in {"reply", "followup"}:
        started = min((at for message in explicit
                       if (at := timestamp(message.get("created_at", message.get("time"))))
                       and (latest_reply is None or at > latest_reply)), default=None)
    end = timestamp(cutoff)
    today = today or datetime.now(LONDON).date()
    deadlines = [(at, day) for message in messages.values()
                 if (at := timestamp(message.get("created_at", message.get("time"))))
                 and (day := deadline_day(message)) is not None]
    deadline = max(deadlines)[1] if deadlines else None
    return {"action_group": group, "handling_reason": event.get("handling_reason", ""),
            "waiting_since": started.isoformat() if started else None,
            "waiting_seconds": max(0, int((end - started).total_seconds())) if end and started else None,
            "waiting_basis": "event_evidence" if started else None,
            "deadline_day": deadline.isoformat() if deadline else None,
            "deadline_precision": "day" if deadline else None,
            "due_today": bool(deadline and deadline == today and not closed)}


def deadline_day(message):
    if message.get("unreadable"):
        return None
    text = message.get("text", "")
    at = timestamp(message.get("created_at", message.get("time")))
    if not at or not isinstance(text, str):
        return None
    # shortcut: mark only explicit relative/ISO deadlines; add locale dates when their interpretation is defined.
    relative = re.search(r"\b(?:by|due|deadline(?:\s+is)?|before)\s+(today|tonight|tomorrow)\b", text, re.I)
    chinese = re.search(r"(今天|今晚|明天)(?:.{0,12}?)(?:截止|前提交|前回复|前确认|前提供)", text)
    if relative or chinese:
        word = (relative or chinese).group(1).lower()
        return at.astimezone(LONDON).date() + timedelta(days=word in {"tomorrow", "明天"})
    absolute = re.search(r"\b(?:by|due(?:\s+on)?|deadline(?:\s+is)?|before)\s+(\d{4}-\d{2}-\d{2})\b", text, re.I)
    if absolute:
        try:
            return datetime.fromisoformat(absolute.group(1)).date()
        except ValueError:
            pass
    return None
