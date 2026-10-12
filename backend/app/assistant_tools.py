"""Five model-selected business tools; identity and message grants stay server-side."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from . import offhours_summary as summaries
from . import summary_periods, summary_replies


def _function(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


_STRING = {"type": "string"}
_NULL_STRING = {"type": ["string", "null"]}
_IDS = {"type": ["array", "null"], "items": _STRING}


def _choice(values):
    return {"type": ["string", "null"], "enum": [*values, None]}


TOOLS = [
    _function("find_creators", "按姓名或 TikTok handle 查找当前可见的正式达人；返回稳定编号与负责人。",
              {"query": _STRING}),
    _function("summarize_messages", "读取指定起止时间及达人的完整聊天事件；缺少有效报告时自动分析。"
              "时间用带时区的 ISO 8601；无时区按英国时间。creator_ids=null 为当前范围全部达人。",
              {"start": _STRING, "end": _STRING, "creator_ids": _IDS}),
    _function("filter_events", "筛选已有报告，计数与分页，不重新分析聊天。主题只检索事件，不检索全部历史。",
              {"result_id": _STRING, "creator_ids": _IDS, "priority": _choice(["P0", "P1", "P2", "P3"]),
               "reply_status": _choice(["replied", "unreplied"]),
               "handling_status": _choice(["resolved", "unresolved", "uncertain", "not_required"]),
               "topic": _NULL_STRING, "cursor": _NULL_STRING}),
    _function("read_event_context", "读取实际展示事件的原文和问答；latest 补充同一事项的后续消息及同步缺口。"
              "原时段结论和最新材料必须分开，不把相似主题当作同一事项。",
              {"event_ref": _STRING, "mode": {"type": "string", "enum": ["period", "latest"]}}),
    _function("draft_reply", "基于实际事件和最新消息起草回复及中文参考，不发送、不修改处理状态。",
              {"event_ref": _STRING, "instruction": _NULL_STRING, "language": _NULL_STRING}),
]
_SCHEMAS = {tool["name"]: tool["parameters"] for tool in TOOLS}


def _validate(name, args):
    spec = _SCHEMAS.get(name)
    if spec is None or not isinstance(args, dict) or set(args) != set(spec["properties"]):
        raise ValueError("工具或参数无效。")
    for key, prop in spec["properties"].items():
        value = args[key]
        types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        valid = ((value is None and "null" in types)
                 or (isinstance(value, str) and "string" in types)
                 or (isinstance(value, list) and "array" in types
                     and all(isinstance(item, str) for item in value)))
        if not valid or ("enum" in prop and value not in prop["enum"]):
            raise ValueError(f"工具参数 {key} 无效。")
        if isinstance(value, str) and len(value) > 100_000:
            raise ValueError("工具参数过长。")


def current_context(conn, actor_id, selected_context, settings, *, now=None):
    selected_context = selected_context or {}
    if not isinstance(selected_context, dict):
        raise ValueError("页面范围无效。")
    actor_id = UUID(str(actor_id))
    now = now or datetime.now(UTC)
    cm_id, _start, _end, selection = summary_periods.selection(
        conn, actor_id, selected_context.get("cm_id"), now=now,
    )
    cm, _ = summaries._authorize(conn, actor_id, cm_id=cm_id)
    actor = conn.execute("SELECT display_name FROM authorizations WHERE id=%s", (actor_id,)).fetchone()
    roster = summaries._roster(conn, cm_id)
    visible = {str(row["contact_id"]) for row in roster}
    selected = _creator_ids(selected_context.get("creator_ids"), visible)
    return {
        "actor": {"id": str(actor_id), "name": actor["display_name"] or "CM"},
        "scope": {"cm_id": str(cm_id), "cm_name": cm["display_name"] or "CM", "creator_ids": selected},
        "available_scopes": [{"cm_id": cm["id"], "cm_name": cm["name"]} for cm in selection["cms"]],
        "timezone": summaries.TIMEZONE,
        "now": now.astimezone(summaries.LONDON).isoformat(),
        "defaults": {"morning_start": now.astimezone(summaries.LONDON).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "morning_end": min(now, now.astimezone(summaries.LONDON).replace(
                hour=12, minute=0, second=0, microsecond=0)).astimezone(summaries.LONDON).isoformat()},
    }


def _creator_ids(values, visible):
    if values is None:
        return None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("达人范围无效。")
    ids = list(dict.fromkeys(str(UUID(value)) for value in values))
    if not set(ids) <= visible:
        raise PermissionError("请求包含当前范围外的达人。")
    return ids


def _scope(conn, actor_id, context):
    cm_id = UUID(context["scope"]["cm_id"])
    cm, _ = summaries._authorize(conn, actor_id, cm_id=cm_id)
    roster = summaries._roster(conn, cm_id)
    visible = {str(row["contact_id"]) for row in roster}
    _creator_ids(context["scope"].get("creator_ids"), visible)
    return cm, roster, visible


def _result_ref(ids):
    return "r." + base64.urlsafe_b64encode(json.dumps([str(value) for value in ids]).encode()).decode().rstrip("=")


def _run_ids(ref):
    try:
        if not isinstance(ref, str) or not ref.startswith("r."):
            raise ValueError
        value = json.loads(base64.b64decode(ref[2:] + "=" * (-len(ref[2:]) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise ValueError
        return list(dict.fromkeys(UUID(item) for item in value))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("报告引用无效。") from exc


def _event_parts(ref):
    try:
        task_id, index, version = ref.split(":")
        task_id, index = UUID(task_id), int(index)
        if index < 0 or not version:
            raise ValueError
        return task_id, index, version
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("事件引用无效。") from exc


def _event_id(conn, actor_id, ref, settings, context=None):
    task_id, index, version = _event_parts(ref)
    task = conn.execute("SELECT t.*,r.responsible_authorization_id FROM offhours_summary_tasks t "
                        "JOIN offhours_summary_runs r ON r.id=t.run_id WHERE t.id=%s", (task_id,)).fetchone()
    if (not task or not task["is_current"] or task["status"] != "succeeded"
            or task["input_fingerprint"] != version):
        raise ValueError("事件材料已更新，请重新读取报告。")
    if context and str(task["responsible_authorization_id"]) != context["scope"]["cm_id"]:
        raise PermissionError("事件不属于当前查询范围。")
    # The core verifies current roster, source messages and consent, not just an old stored report.
    summary_replies.source(conn, actor_id, f"{task_id}:{index}", settings, require_unresolved=False)
    return f"{task_id}:{index}"


def _card(event, version):
    ref = f"{event['id']}:{version}"
    return {
        "event_ref": ref, "evidence_ref": ref, "contact_id": str(event["contact_id"]),
        "title": event["title"], "summary": event["summary"], "latest_progress": event["latest_progress"],
        "creator_name": event.get("lark_user_name", event.get("creator_name", "")),
        "creator_handle": event.get("creator_handle"), "creator_handles": event.get("creator_handles", []),
        "conversation_name": event.get("conversation_name", ""), "priority": event["priority"],
        "handling_status": event["handling_status"],
        "reply_status": "replied" if event.get("replied_by") else "unreplied",
        "replied_by": event.get("replied_by", []), "evidence_count": len(event.get("evidence", [])),
    }


def _read_results(conn, actor_id, run_ids, settings, context):
    scopes = conn.execute("SELECT id,responsible_authorization_id FROM offhours_summary_runs "
                          "WHERE id=ANY(%s::uuid[])", ([UUID(str(value)) for value in run_ids],)).fetchall()
    if (len(scopes) != len(set(str(value) for value in run_ids))
            or any(str(row["responsible_authorization_id"]) != context["scope"]["cm_id"] for row in scopes)):
        raise PermissionError("报告不存在或不属于当前查询范围。")
    events, views, gaps = [], [], []
    pending = False
    for run_id in run_ids:
        view = summaries.read_assistant_run(conn, actor_id, run_id, settings)
        if str(view["responsible"]["id"]) != context["scope"]["cm_id"]:
            raise PermissionError("报告不属于当前查询范围。")
        views.append(view)
        gaps.extend(view.get("run", {}).get("gaps", []))
        pending |= view.get("run", {}).get("status") in {"queued", "running", "preparing"}
        task_ids = list({UUID(event["id"].split(":")[0]) for event in view["events"]})
        versions = {str(row["id"]): row["input_fingerprint"] for row in conn.execute(
            "SELECT id,input_fingerprint FROM offhours_summary_tasks WHERE id=ANY(%s::uuid[])", (task_ids,),
        ).fetchall()} if task_ids else {}
        for event in view["events"]:
            card = _card(event, versions[event["id"].split(":")[0]])
            card["window"] = view["window"]
            events.append(card)
    events.sort(key=lambda event: ({"P0": 0, "P1": 1, "P2": 2, None: 3, "P3": 4}[event["priority"]],
                                   event["window"]["start"], event["event_ref"]))
    coverage = {key: sum(view.get("coverage", {}).get(key, 0) for view in views)
                for key in ("messages", "conversations", "confirmed_creators", "private_conversations",
                            "group_conversations")}
    if len(views) > 1:
        coverage["confirmed_creators"] = max(view.get("coverage", {}).get("confirmed_creators", 0) for view in views)
        chats = conn.execute("""SELECT count(DISTINCT t.chat_id) conversations,
            count(DISTINCT t.chat_id) FILTER(WHERE c.chat_mode='p2p') private_conversations,
            count(DISTINCT t.chat_id) FILTER(WHERE c.chat_mode<>'p2p') group_conversations
            FROM offhours_summary_tasks t JOIN chats c ON c.chat_id=t.chat_id
            WHERE t.run_id=ANY(%s::uuid[]) AND t.is_current""", ([UUID(str(value)) for value in run_ids],)).fetchone()
        coverage.update(chats)
    return {
        "result_id": _result_ref(run_ids), "events": events, "total": len(events), "pending": pending,
        "scope": context["scope"], "window": {"start": views[0]["window"]["start"],
            "end": views[-1]["window"]["end"], "timezone": summaries.TIMEZONE},
        "windows": [view["window"] for view in views], "coverage": coverage,
        "gaps": list(dict.fromkeys(gaps)),
        "coverage_windows": [{"window": view["window"], "coverage": view.get("coverage", {})} for view in views],
    }


def _find(conn, cm, roster, query):
    query = query.strip().casefold()
    if "tiktok.com/@" in query:
        query = query.split("tiktok.com/@", 1)[1].split("?", 1)[0].split("/", 1)[0]
    query = query.lstrip("@")
    people = {}
    for row in roster:
        person = people.setdefault(str(row["contact_id"]), {
            "id": str(row["contact_id"]), "name": row["name"] or "未命名达人", "handles": [],
            "responsible": [{"id": str(cm["id"]), "name": cm["display_name"] or "CM"}],
        })
        if row["display_handle"] and row["display_handle"] not in person["handles"]:
            person["handles"].append(row["display_handle"])
    matches = []
    for person in people.values():
        fields = [person["name"].casefold(), *(handle.lstrip("@").casefold() for handle in person["handles"])]
        if not query or any(query in field for field in fields):
            person["exact_match"] = query in fields
            matches.append(person)
    matches.sort(key=lambda person: (not person["exact_match"], person["name"].casefold(), person["id"]))
    return {"creators": matches[:25], "total": len(matches)}


def execute(conn, actor_id, turn, name, args, settings, context):
    _validate(name, args)
    cm, roster, visible = _scope(conn, actor_id, context)
    if name == "find_creators":
        return _find(conn, cm, roster, args["query"])
    if name == "summarize_messages":
        ids = _creator_ids(args["creator_ids"], visible)
        if ids is None:
            ids = context["scope"].get("creator_ids")
        start, end = _window(args["start"], args["end"], turn["created_at"])
        run_ids = []
        while start < end:
            stop = min(start + timedelta(days=7),
                       (start.astimezone(summaries.LONDON) + timedelta(days=7)).astimezone(UTC), end)
            excess = stop.astimezone(summaries.LONDON) - start.astimezone(summaries.LONDON) - timedelta(days=7)
            if excess > timedelta():
                stop -= excess
            view = summaries.ensure_assistant_run(conn, actor_id, turn, cm["id"], start, stop, ids, settings)
            run_ids.append(view["run"]["id"])
            start = stop
        result = _read_results(conn, actor_id, run_ids, settings, context)
        if result["pending"]:
            return {"waiting": {"kind": "summary", "ids": [str(value) for value in run_ids]},
                    "scope": result["scope"], "window": result["window"]}
        return _page(result, 0)
    if name == "filter_events":
        ids = _creator_ids(args["creator_ids"], visible)
        result = _read_results(conn, actor_id, _run_ids(args["result_id"]), settings, context)
        events = result["events"]
        for field in ("priority", "reply_status", "handling_status"):
            if args[field] is not None:
                events = [event for event in events if event[field] == args[field]]
        if ids is not None:
            events = [event for event in events if event["contact_id"] in ids]
        if args["topic"]:
            topic = args["topic"].casefold().strip()
            events = [event for event in events if topic in " ".join(str(event[field] or "")
                for field in ("title", "summary", "latest_progress", "conversation_name")).casefold()]
        result.update(events=events, total=len(events))
        try:
            cursor = int(args["cursor"] or 0)
            if cursor < 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("分页位置无效。") from exc
        return _page(result, cursor)
    event_id = _event_id(conn, actor_id, args["event_ref"], settings, context)
    if name == "read_event_context":
        result = summary_replies.read_event_context(conn, actor_id, event_id, settings, mode=args["mode"],
                                                    cutoff=datetime.now(UTC))
        _audit_context(conn, actor_id, event_id, args["mode"], len(result.get("messages", [])))
        return {**result, "event_ref": args["event_ref"], "mode": args["mode"]}
    job_id = summary_replies.enqueue(conn, actor_id, event_id, settings, feature="operations_assistant",
                                     instruction=args["instruction"] or "", language=args["language"])
    return {"waiting": {"kind": "reply", "ids": [job_id], "event_ref": args["event_ref"]}}


def _window(start, end, cutoff):
    try:
        values = [datetime.fromisoformat(value) for value in (start, end)]
        values = [value.replace(tzinfo=summaries.LONDON) if value.tzinfo is None
                  else value.astimezone(summaries.LONDON) for value in values]
        for value in values:
            if value.astimezone(UTC).astimezone(summaries.LONDON).replace(tzinfo=None) != value.replace(tzinfo=None):
                raise ValueError
        start, end = (value.astimezone(UTC) for value in values)
        end = min(end, cutoff)
        if start >= end:
            raise ValueError
        return start, end
    except (TypeError, ValueError) as exc:
        raise ValueError("起止时间无效，结束须晚于开始；未来部分尚无消息。") from exc


def _page(result, cursor):
    count = len(result["events"])
    return {**result, "events": result["events"][cursor:cursor + 25],
            "next_cursor": str(cursor + 25) if cursor + 25 < count else None}


def resume_wait(conn, actor_id, turn, waiting, settings, context):
    _scope(conn, actor_id, context)
    if waiting["kind"] == "summary":
        result = _read_results(conn, actor_id, [UUID(value) for value in waiting["ids"]], settings, context)
        return None if result["pending"] else _page(result, 0)
    if waiting["kind"] != "reply" or len(waiting["ids"]) != 1:
        raise ValueError("等待任务无效。")
    _event_id(conn, actor_id, waiting["event_ref"], settings, context)
    job_id = waiting["ids"][0]
    result = summary_replies.read(conn, actor_id, job_id, settings, fresh=True)
    if result["status"] in {"queued", "running"}:
        return None
    if result["status"] != "succeeded":
        return {"draft_ref": job_id, "status": result["status"], "error": result["error"], "draft": None}
    return {"draft_ref": job_id, "status": "completed", "draft": result["result"],
            "event_ref": waiting["event_ref"]}


def evidence(conn, actor_id, ref, settings):
    event_id = _event_id(conn, actor_id, ref, settings)
    result = summary_replies.read_event_context(conn, actor_id, event_id, settings, mode="period")
    messages = result.get("messages", result.get("event", {}).get("evidence", []))
    _audit_context(conn, actor_id, event_id, "evidence", len(messages))
    return {**result, "event_ref": ref, "messages": [{**message,
        "time": message.get("time", message.get("created_at")),
        "sender_name": message.get("sender_name", message.get("speaker", ""))} for message in messages]}


def _audit_context(conn, actor_id, event_id, mode, count):
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
                 (uuid4(), actor_id, "operations_assistant_context_read",
                  json.dumps({"event_id": event_id, "mode": mode, "messages": count})))


def validate_tool_results(conn, actor_id, turn, results, settings, context):
    """Recheck all reused source text before it goes into another paid request."""
    cm, roster, _visible = _scope(conn, actor_id, context)
    people, views, cards, drafts = {}, {}, {}, {}
    for row in roster:
        person = people.setdefault(str(row["contact_id"]), {"name": row["name"] or "未命名达人", "handles": set()})
        if row["display_handle"]:
            person["handles"].add(row["display_handle"])

    def read_report(ref):
        if ref not in views:
            views[ref] = _read_results(conn, actor_id, _run_ids(ref), settings, context)
            cards.update({event["event_ref"]: event for event in views[ref]["events"]})
        return views[ref]

    def read_event(ref):
        if ref not in cards:
            task_id, _index, _version = _event_parts(ref)
            task = conn.execute("SELECT run_id FROM offhours_summary_tasks WHERE id=%s", (task_id,)).fetchone()
            if not task:
                raise ValueError("事件已更新。")
            read_report(_result_ref([task["run_id"]]))
        if ref not in cards:
            raise ValueError("事件材料已更新，请重新查询。")

    for result in results:
        if not isinstance(result, dict):
            raise ValueError("工具结果无效。")
        if result.get("result_id"):
            read_report(result["result_id"])
    for result in results:
        for person in result.get("creators", []):
            current = people.get(person["id"])
            if (not current or current["name"] != person["name"]
                    or set(current["handles"]) != set(person["handles"])):
                raise ValueError("达人名单或账号已变化，请重新查询。")
        refs = [event["event_ref"] for event in result.get("events", [])]
        if result.get("event_ref"):
            refs.append(result["event_ref"])
        for ref in dict.fromkeys(refs):
            read_event(ref)
            if result.get("event_ref") == ref and result.get("mode"):
                task_id, index, _version = _event_parts(ref)
                cutoff = result.get("checked_at")
                fresh = summary_replies.read_event_context(
                    conn, actor_id, f"{task_id}:{index}", settings, mode=result["mode"],
                    cutoff=datetime.fromisoformat(cutoff) if cutoff else turn["created_at"],
                )
                if result.get("fingerprint") and result["fingerprint"] != fresh.get("fingerprint"):
                    raise ValueError("原文材料已变化，请重新查询。")
        if result.get("draft_ref"):
            ref = result["draft_ref"]
            if ref not in drafts:
                drafts[ref] = summary_replies.read(conn, actor_id, ref, settings, fresh=True)
            fresh = drafts[ref]
            if result.get("draft") and fresh["status"] != "succeeded":
                raise ValueError("回复材料已更新，请重新生成。")
    return {"views": views, "cards": cards, "drafts": drafts}


def build_result(conn, actor_id, turn, final, settings, context, tool_results):
    """Render only references actually returned by tools and still readable now."""
    validated = validate_tool_results(conn, actor_id, turn, tool_results, settings, context)
    known_results, known_events, known_drafts = set(), set(), set()
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        if result.get("result_id"):
            known_results.add(result["result_id"])
        known_events.update(event["event_ref"] for event in result.get("events", []))
        if result.get("event_ref"):
            known_events.add(result["event_ref"])
        if result.get("draft_ref"):
            known_drafts.add(result["draft_ref"])
    refs = final.get("result_refs", [])
    event_refs = final.get("event_refs", [])
    draft_ref = final.get("draft_ref")
    if (not isinstance(refs, list) or not isinstance(event_refs, list)
            or not all(isinstance(ref, str) for ref in [*refs, *event_refs])
            or not set(refs) <= known_results or not set(event_refs) <= known_events
            or (draft_ref is not None and draft_ref not in known_drafts)):
        raise ValueError("回答引用未取得的资料。")
    views = [validated["views"][ref] for ref in dict.fromkeys(refs)]
    cards = validated["cards"]
    draft = None
    if draft_ref:
        reply = validated["drafts"][draft_ref]
        if reply["status"] == "succeeded":
            draft = reply["result"]
    return {"turn_id": str(turn["id"]), "status": "completed", "answer": final["answer"],
            "result_refs": list(dict.fromkeys(refs)), "event_refs": list(dict.fromkeys(event_refs)),
            "events": [cards[ref] for ref in dict.fromkeys(event_refs)], "scope": context["scope"],
            "window": views[-1]["window"] if views else cards[event_refs[0]]["window"] if event_refs else None,
            "coverage": views[-1]["coverage"] if views else {},
            "gaps": list(dict.fromkeys([gap for view in views for gap in view["gaps"]]
                + [gap for result in tool_results if result.get("event_ref") in event_refs
                   for gap in result.get("gaps", result.get("sync_gaps", []))])), "draft": draft,
            "draft_ref": draft_ref}
