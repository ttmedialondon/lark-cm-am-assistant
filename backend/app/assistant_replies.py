"""Reply material and drafts in the assistant's encrypted turn, never business state."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

from . import assistant_search, summary_replies
from . import offhours_summary as summaries
from .reply_prompts import REPLY_STYLE_INSTRUCTIONS

ACTIONS = ("generate", "shorter", "natural", "follow_up", "english", "translate")
INSTRUCTIONS = """需要起草或改写回复，先 draft_reply 取得真实上下文，
再 save_reply_draft 保存本轮草稿，最后引用返回的 draft_ref。
普通已授权文字消息、商家或未识别联系人消息、已解决后的礼貌回应均可起草，不要求达人或未解决事件。
准备材料里的 nearby/latest 消息只是候选上下文，不能因主题相似就当成同一事项。原问答、目标消息与最新进展分开核对。
只使用已核实事实，不编造日期、价格、付款、发货、公司政策或已完成结果。
缺少内部事实时优先给出不编造事实的实用回复，例如核查后再告知；checks 提醒 CM 确认能否实际执行承诺。
单纯历史同步缺口不禁止当前简单回复；真正影响答案的对应关系、冲突或不可读附件要具体提示。
action=shorter/natural/follow_up/english 基于 edited_draft 和同一来源改写；不把草稿中的新事实自动当作已核实。
action=translate 只忠实翻译 edited_draft，绝不改变英文原稿或添加业务建议。
outcome=ready 有可人工核对发送的草稿；outcome=verify 仅用于确实无法安全确定回复对象或给出合适措辞的情况。
生成、复制和打开 Lark 都不是发送，不修改事件状态。""" + REPLY_STYLE_INSTRUCTIONS


def material(conn, actor_id, context, source, settings, *, cutoff=None, read_cache=None):
    cutoff = cutoff or datetime.now(UTC)
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    if source["kind"] == "message":
        data = assistant_search.read(conn, actor_id, context, {"message_ref": source["source"]},
                                     settings, cutoff=cutoff, read_cache=read_cache)
        target = data["message"]
        text = target.get("text", "").replace("[未解析的@用户]", "").strip()
        if not text or text in {"[附件或正文不可读取，需人工查看原消息]", "[正文不可读取，需人工查看原消息]"}:
            raise ValueError("这条消息没有可读取的文字，请选择已解析的消息。")
        messages, gaps = data["messages"], data["gaps"]
        title = target.get("text", "")[:240]
        scope = data["scope"]
    else:
        data = summary_replies.collect(conn, actor_id, source["source"], settings,
                                       require_unresolved=False, cutoff=cutoff,
                                       feature=summaries.ASSISTANT_FEATURE)
        event = data["event"]
        chats = {item.get("chat_id") for item in event.get("evidence", []) if item.get("chat_id")}
        available = [item for item in data["messages"] if not chats or item["chat_id"] in chats]
        by_id = {}
        for item in available:
            by_id.setdefault((item["chat_id"], item["message_id"]), []).append(item)
        chosen = {item["ref"]: item for item in available[-40:]}
        pending = list(chosen.values())
        while pending:
            item = pending.pop()
            for key in ("parent_message_id", "root_message_id"):
                for parent in by_id.get((item["chat_id"], item.get(key)), []):
                    if parent["ref"] not in chosen and len(chosen) < 60:
                        chosen[parent["ref"]] = parent
                        pending.append(parent)
        messages = sorted(chosen.values(), key=lambda item: (item["time"], item["chat_id"], item["ref"]))
        for index, item in enumerate(event.get("evidence", [])):
            messages.append({**item, "ref": f"H{index + 1}", "relation": "event_evidence"})
        target = next((item for item in reversed(messages) if not item.get("is_staff")),
                      messages[-1] if messages else {})
        if not messages:
            raise ValueError("当前事项没有可读取的原文。")
        gaps = [{"chat_id": value, "reason": "相关会话同步尚未完整"}
                for value in data["sync_gaps"] if not chats or value in chats]
        if len(available) > len(chosen):
            gaps.append({"reason": "仅提供当前事项原证据和有界近期上下文，不是全量会话"})
        title = event["title"]
        scope = context["scope"]
    cm = assistant_search.authorize_all(conn, actor_id, context["scope"]["cm_id"], read_cache=read_cache)
    staff = summaries._staff(conn, cm, read_cache=read_cache)
    ids = list({item.get("message_id") for item in messages if item.get("message_id")})
    speakers = {row["message_id"]: row["sender_open_id"] for row in conn.execute(
        "SELECT message_id,sender_open_id FROM chat_messages WHERE message_id=ANY(%s::text[])",
        (ids,)).fetchall()}
    prepared = []
    for index, item in enumerate(messages):
        sender = staff.get(speakers.get(item.get("message_id")), {})
        safe = {**item}
        for key in ("message_ref", "evidence_ref"):
            if safe.get(key):
                safe[key] = source["ref"]
        prepared.append({**safe, "ref": f"R{index + 1}", "speaker": sender.get("speaker", "other"),
                         "is_staff": bool(sender), "purpose": "context_candidate"})
    chat_id = target.get("chat_id")
    chat_name = target.get("conversation_name") or target.get("chat_name") or "会话"
    checked = data.get("checked_at") or cutoff.isoformat()
    target = {**target, **{key: source["ref"] for key in ("message_ref", "evidence_ref") if target.get(key)}}
    result = {"title": title, "message": target, "messages": prepared, "gaps": gaps,
              "scope": scope, "checked_at": checked, "chat_name": chat_name,
              "chat_url": ("https://applink.larksuite.com/client/chat/open?openChatId=" + quote(chat_id, safe=""))
                          if chat_id else None,
              "context_scope": "target_and_bounded_latest_context", "is_full_conversation": False}
    # Sync watermarks may advance without changing any readable reply evidence.
    if source["kind"] == "event":
        result["event"] = {key: data["event"].get(key) for key in
                           ("title", "summary", "latest_progress", "handling_status", "replied_by")}
    result["fingerprint"] = summaries._hash([source, result.get("event"), title, prepared, scope.get("cm_id")])
    return result


def has_new_messages(conn, material):
    chats = list({item.get("chat_id") for item in material["messages"] if item.get("chat_id")})
    return bool(assistant_search._rows(conn, UUID(material["scope"]["cm_id"]), chats,
        datetime.fromisoformat(material["checked_at"]), datetime.now(UTC), None, limit=1))
