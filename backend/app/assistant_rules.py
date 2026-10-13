"""Git defaults and encrypted owner overrides, pinned once for each assistant turn."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import summary_dialogues as codec

DOCUMENTS = (
    {"key": "core", "title": "核心规则", "description": "身份、权限和基本边界"},
    {"key": "understand", "title": "理解规则", "description": "时间、范围和连续追问"},
    {"key": "evidence", "title": "证据规则", "description": "查询、来源和分段归纳"},
    {"key": "answer", "title": "回答规则", "description": "业务归纳、卡片和回复表达"},
)
KEYS = frozenset(item["key"] for item in DOCUMENTS)
MAX_DOCUMENT_CHARS = 24_000
MAIN_HEADING = "## 主对话\n"
SUMMARY_HEADING = "## 分段归纳\n"


class RuleConflict(ValueError):
    """The editor's revision no longer matches the saved document."""


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_text(key):
    if key not in KEYS:
        raise ValueError("未知的助手规则文档。")
    return (Path(__file__).with_suffix("") / f"{key}.md").read_text(encoding="utf-8")


def _snapshot(documents):
    return {"documents": documents, "digest": _digest(json.dumps(
        {key: {name: documents[key][name] for name in ("revision", "digest")} for key in sorted(KEYS)},
        sort_keys=True, separators=(",", ":"),
    ))}


def builtin_snapshot():
    documents = {}
    for item in DOCUMENTS:
        text = default_text(item["key"])
        documents[item["key"]] = {"text": text, "revision": 0, "digest": _digest(text),
                                  "updated_by": None, "updated_at": None}
    return _snapshot(documents)


def snapshot(conn, settings):
    documents = builtin_snapshot()["documents"]
    for row in conn.execute("SELECT * FROM assistant_rule_overrides ORDER BY rule_key").fetchall():
        text = codec.decode(row["content_ciphertext"], settings)
        validate(row["rule_key"], text)
        documents[row["rule_key"]] = {"text": text, "revision": row["revision"], "digest": _digest(text),
            "updated_by": str(row["updated_by_authorization_id"]), "updated_at": row["updated_at"].isoformat()}
    return _snapshot(documents)


def validate(key, text):
    if key not in KEYS:
        raise ValueError("未知的助手规则文档。")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DOCUMENT_CHARS or "\x00" in text:
        raise ValueError(f"规则不能为空、含空字符或超过 {MAX_DOCUMENT_CHARS} 字。")
    if (text.count(MAIN_HEADING) != 1
            or not text.split(MAIN_HEADING, 1)[1].split(SUMMARY_HEADING, 1)[0].strip()):
        raise ValueError("请保留一个「## 主对话」标题及其规则内容。")
    if key in {"core", "evidence"}:
        if text.count(SUMMARY_HEADING) != 1 or not text.split(SUMMARY_HEADING, 1)[1].strip():
            raise ValueError("请保留一个「## 分段归纳」标题及其规则内容。")
        if text.index(SUMMARY_HEADING) < text.index(MAIN_HEADING):
            raise ValueError("「## 分段归纳」应位于「## 主对话」之后。")
    elif SUMMARY_HEADING in text:
        raise ValueError("此文档仅用于主对话，请将分段归纳规则放在核心或证据文档。")


def main_instructions(pinned):
    return "\n\n".join(pinned["documents"][item["key"]]["text"].split(MAIN_HEADING, 1)[1]
                       .split(SUMMARY_HEADING, 1)[0].strip() for item in DOCUMENTS)


def summary_instructions(pinned):
    return "\n\n".join(pinned["documents"][key]["text"].split(SUMMARY_HEADING, 1)[1].strip()
                       for key in ("core", "evidence"))


def save_override(conn, settings, actor_id, key, text, revision):
    validate(key, text)
    if type(revision) is not int or not 0 <= revision < 2_147_483_647:
        raise ValueError("规则版本无效，请刷新页面后重试。")
    account = conn.execute("SELECT status FROM authorizations WHERE id=%s FOR UPDATE", (actor_id,)).fetchone()
    role = conn.execute("SELECT role,is_active FROM admin_roles WHERE authorization_id=%s FOR UPDATE",
                        (actor_id,)).fetchone()
    if (not account or account["status"] != "active"
            or not role or role["role"] != "owner" or not role["is_active"]):
        raise PermissionError("只有当前有效的系统主人可以修改助手规则。")
    # A missing override row needs the same lock as existing rows for the first concurrent save.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("assistant-rules:" + key,))
    current = conn.execute("SELECT revision FROM assistant_rule_overrides WHERE rule_key=%s FOR UPDATE",
                           (key,)).fetchone()
    if (current["revision"] if current else 0) != revision:
        raise RuleConflict("规则已被更新。你的修改已保留在编辑框，请核对当前版本后重新保存。")
    next_revision = revision + 1
    conn.execute("""INSERT INTO assistant_rule_overrides
        (rule_key,content_ciphertext,revision,updated_by_authorization_id) VALUES(%s,%s,%s,%s)
        ON CONFLICT(rule_key) DO UPDATE SET content_ciphertext=EXCLUDED.content_ciphertext,
        revision=EXCLUDED.revision,updated_by_authorization_id=EXCLUDED.updated_by_authorization_id,updated_at=NOW()""",
        (key, codec.encode(text, settings), next_revision, actor_id))
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor_id, "assistant_rule_updated",
         json.dumps({"document": key, "before_revision": revision,
                     "revision": next_revision, "digest": _digest(text)})))
    return next_revision
