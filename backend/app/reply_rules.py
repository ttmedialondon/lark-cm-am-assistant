"""Pinned, owner-approved reply documents shared by the three existing draft paths."""

import json

COMMON = ("reply-policy", "reply-facts", "reply-tone")
ENTRIES = {"desktop": "reply-desktop", "event": "reply-event", "assistant": "reply-writing"}
HEADINGS = {"draft": "## 起草\n", "evidence": "## 取材\n", "translate": "## 翻译\n"}


def internal_only(document):
    return (document["kind"] == "knowledge" or document["kind"] == "manual"
            and document["key"].startswith("doc-")) and not document.get("reply_usable")


def _owner(conn, actor_id):
    row = conn.execute("""SELECT a.status,r.role,r.is_active FROM authorizations a
        LEFT JOIN admin_roles r ON r.authorization_id=a.id WHERE a.id=%s""", (actor_id,)).fetchone()
    if not row or row["status"] != "active":
        raise PermissionError("账号授权已失效。")
    return row["role"] == "owner" and bool(row["is_active"])


def from_snapshot(pinned, entry):
    from . import assistant_rules as rules

    required = {*COMMON, ENTRIES[entry]}
    documents = {key: item for key, item in pinned["documents"].items()
                 if key in required or item.get("reply_usable") and item["kind"] in {"manual", "knowledge"}
                 and key not in rules.REPLY_SHARED_KEYS | rules.EVENT_KEYS | rules.CREATOR_KEYS}
    if not required <= documents.keys() or any(documents[key]["status"] != "published" for key in required):
        raise PermissionError("本入口的回复处理手册尚未发布。")
    return {**rules._snapshot(documents, metadata=True), "protocol": "reply-documents-v1", "entry": entry}


def snapshot(conn, settings, actor_id, entry="desktop"):
    from . import assistant_rules as rules

    return from_snapshot(rules.snapshot(conn, settings, owner=_owner(conn, actor_id)), entry)


def catalog(pinned):
    required = {*COMMON, ENTRIES[pinned["entry"]]}
    return [{**{key: item[key] for key in ("key", "title", "kind")},
             "description": item["description"][:240]} for item in pinned["documents"].values()
            if item["key"] not in required and item["status"] == "published"]


def selection_format(pinned):
    ids = [item["key"] for item in catalog(pinned)]
    field = {"type": "array", "items": {"type": "string"}, "maxItems": min(3, len(ids))}
    if ids:
        field["items"]["enum"] = ids
    return {"type": "json_schema", "name": "reply_documents_v1", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"document_ids": field}, "required": ["document_ids"]}}


def select(pinned, result):
    if (not isinstance(result, dict) or set(result) != {"document_ids"}
            or not isinstance(result["document_ids"], list)):
        raise ValueError("回复文档选择格式无效。")
    ids = result["document_ids"]
    allowed = {item["key"] for item in catalog(pinned)}
    if (len(ids) > 3 or any(not isinstance(key, str) or key not in allowed for key in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("回复文档不在当前可使用范围内。")
    return ids


def instructions(pinned, entry, stage="draft", loaded=()):
    from . import assistant_rules as rules

    documents = pinned["documents"]
    if entry != pinned.get("entry"):
        raise ValueError("回复入口与固定手册不一致。")
    if rules._snapshot(documents, metadata=True)["digest"] != pinned["digest"]:
        raise ValueError("回复手册快照已损坏。")
    for item in documents.values():
        if rules._digest(item["text"]) != item["digest"]:
            raise ValueError("回复文档正文已变化。")
    if stage in {"select", "navigate"}:
        return ("按当前回复诉求选择真正相关的业务手册或知识；只依据目录判断适用性，目录不是业务事实。"
                "最多三份，不相关返回空数组；不得选择其他编号或起草回复。只返回document_ids。\n"
                + json.dumps(catalog(pinned), ensure_ascii=False, separators=(",", ":")))
    keys = select(pinned, {"document_ids": list(loaded)})
    text = documents[ENTRIES[entry]]["text"]
    heading = HEADINGS[stage]
    body = text.split(heading, 1)[1].split("\n## ", 1)[0].strip() if heading in text else text
    if stage == "translate":
        return body
    common = "\n\n".join(documents[key]["text"] for key in COMMON)
    manuals = "\n\n".join(documents[key]["text"] for key in keys if documents[key]["kind"] == "manual")
    return common + "\n\n" + body + ("\n\n已选业务处理手册：\n" + manuals if manuals else "")


def knowledge(pinned, ids):
    selected = select(pinned, {"document_ids": list(ids)})
    return [{**{key: pinned["documents"][name][key] for key in ("key", "title", "text", "revision")},
             "document_ref": "doc." + name} for name in selected
            if pinned["documents"][name]["kind"] == "knowledge"]


def validate_access(conn, actor_id, pinned, loaded=()):
    from . import assistant_rules as rules

    instructions(pinned, pinned["entry"], loaded=loaded)
    ids = select(pinned, {"document_ids": list(loaded)})
    required = {*COMMON, ENTRIES[pinned["entry"]], *ids}
    owner = _owner(conn, actor_id)
    rules.validate_access(conn, pinned, sorted(required), owner=owner)
    current = rules._defaults()
    current.update({row["rule_key"]: row for row in conn.execute(
        "SELECT rule_key,kind,reply_usable FROM assistant_rule_overrides WHERE rule_key=ANY(%s::text[])",
        (ids,)).fetchall()})
    for key in ids:
        item = current.get(key)
        if (not item or item["kind"] != pinned["documents"][key]["kind"]
                or not item.get("reply_usable", key in rules.REPLY_KEYS)):
            raise PermissionError("这份资料已停止用于回复起草。")
