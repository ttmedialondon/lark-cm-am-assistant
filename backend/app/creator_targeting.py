"""Local, bounded evidence selection for one known person, never whole-chat export."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from . import creator_workflow
from .crypto import message_body_cipher
from .message_mentions import mention_targets

POLICY_VERSION = "creator-person-evidence-v1"
MAX_MESSAGES = 30
MAX_CHATS = 3
MAX_BODY_CHARS = 1500
MAX_PACK_CHARS = 16000
MAX_ANCHORS = 9
# Retrieval only: these terms never establish creator status or CM ownership.
_BUSINESS = re.compile(
    r"\b(?:creator|influencer|affiliate|commission|campaign|voucher|sample|sku|"
    r"live|livestream|video|boost|agency|promot\w*|collaborat\w*|tiktok|product|brand)\b"
    r"|达人|直播|短视频|样品|补样|佣金|机构|签约|推品|投流|带货|选品",
    re.I,
)
_EMAIL = re.compile(r"(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d ()-]{7,}\d(?![\w])")
_ADDRESS = re.compile(
    r"(?im)(?:\b(?:shipping|delivery|postal|home)?\s*address\s*[:：]|收货地址\s*[:：])[^\n]+"
    r"|\b\d{1,5}\s+(?:[A-Za-z'-]+\s+){0,5}(?:street|road|avenue|lane|drive|close|way|st|rd)\b[^\n,;]*"
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"
)

_ROWS = """
 SELECT m.chat_id,m.message_id,m.message_type,m.message_create_time,m.message_update_time,
        m.sender_open_id,m.body_ciphertext,m.mentions_json,m.parent_id AS parent_message_id,
        i.contact_id,i.display_name AS sender_name,i.identity_type,i.tenant_classification,
        o.first_observed_at,c.chat_mode AS chat_type,ac.backfill_complete,
        ac.p2p_counterpart_open_id,pi.contact_id AS counterpart_contact_id,
        parent.sender_open_id AS parent_sender_open_id,ip.contact_id AS parent_contact_id
 FROM authorizations a
 JOIN authorization_chats ac ON ac.authorization_id=a.id
 JOIN chats c ON c.chat_id=ac.chat_id
 JOIN authorization_message_observations o ON o.authorization_id=a.id AND o.chat_id=c.chat_id
 JOIN chat_messages m ON m.chat_id=o.chat_id AND m.message_id=o.message_id
 LEFT JOIN lark_identities i ON i.open_id=m.sender_open_id
 LEFT JOIN lark_identities pi ON pi.open_id=ac.p2p_counterpart_open_id
 LEFT JOIN chat_messages parent ON parent.chat_id=m.chat_id AND parent.message_id=m.parent_id
 LEFT JOIN lark_identities ip ON ip.open_id=parent.sender_open_id
 WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
   AND o.observed_at>=a.body_consent_at AND ac.is_visible AND ac.sync_enabled
   AND COALESCE(c.chat_status,'normal')='normal' AND NOT m.is_deleted
   AND m.body_ciphertext IS NOT NULL
   AND GREATEST(COALESCE(m.message_create_time,o.first_observed_at),o.first_observed_at)<%s
"""


def _time(row):
    return row["message_create_time"] or row["first_observed_at"]


def _scope(conn, cm_id):
    from .creator_workflow_ai import ScanInputError

    row = conn.execute(
        """SELECT a.id,a.open_id,a.display_name,a.body_consent_at,i.contact_id FROM authorizations a
           LEFT JOIN lark_identities i ON i.open_id=a.open_id
           WHERE a.id=%s AND a.status='active' AND a.assignment_eligible
             AND a.body_consent_at IS NOT NULL""", (cm_id,),
    ).fetchone()
    if not row:
        raise ScanInputError("CM 授权已失效，未读取任何消息。")
    ids = {row["open_id"]}
    if row["contact_id"]:
        ids.update(r["open_id"] for r in conn.execute(
            "SELECT open_id FROM lark_identities WHERE contact_id=%s AND identity_type='user'",
            (row["contact_id"],),
        ).fetchall())
    return {**dict(row), "open_ids": ids}


def _text(cipher, row, scope, subject_ids, known_names):
    from .creator_workflow_ai import _body

    # Mention metadata and platform IDs are not sent to the model. Free prose
    # redaction is best effort; this does not claim perfect anonymization.
    names = {
        item["open_id"]: "CM1" if item["open_id"] in scope["open_ids"] else
        "U1" if item["open_id"] in subject_ids else "其他用户"
        for item in mention_targets(row.get("mentions_json")) if item["open_id"]
    }
    text = _body(cipher, row, names, clip=False)
    text = _EMAIL.sub("[邮箱已隐藏]", text)
    text = _PHONE.sub("[电话/编号已隐藏]", text)
    text = _ADDRESS.sub("[地址已隐藏]", text)
    for value, alias in known_names.items():
        if value and len(value.strip()) > 2:
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)",
                          alias, text, flags=re.I)
    business = _BUSINESS.search(text)
    start = max(0, business.start() - 300) if business and business.start() >= MAX_BODY_CHARS else 0
    return text[start:start + MAX_BODY_CHARS]


def _direct_pair(rows, scope, contact_id):
    """Prove who exchanged messages; the model still judges business responsibility."""
    own = [r for r in rows if str(r.get("contact_id")) == str(contact_id)
           and r["sender_open_id"] not in scope["open_ids"]]
    managers = [r for r in rows if r["sender_open_id"] in scope["open_ids"]]
    own_ids = {r["sender_open_id"] for r in own}
    for manager in managers:
        for user in own:
            if manager["chat_id"] != user["chat_id"]:
                continue
            if manager.get("_interaction") != user.get("_interaction"):
                continue
            reply = manager.get("parent_message_id") == user["message_id"] or (
                user.get("parent_message_id") == manager["message_id"])
            directed = bool(own_ids & {m["open_id"] for m in mention_targets(manager.get("mentions_json"))})
            private = manager["chat_type"] == "p2p" and (
                str(manager.get("counterpart_contact_id")) == str(contact_id))
            if reply or ((directed or private) and abs(_time(manager) - _time(user)) <= timedelta(days=2)):
                return True
    return False


def _context(conn, cm_id, end, scope, contact_id, anchor):
    rows = []
    for where, identity in (("m.sender_open_id=ANY(%s::text[])", sorted(scope["open_ids"])),
                            ("i.contact_id=%s", contact_id)):
        rows.extend(conn.execute(
            _ROWS + " AND m.chat_id=%s AND (" + where + ")"
            " AND (COALESCE(m.message_create_time,o.first_observed_at) BETWEEN %s AND %s"
            " OR m.message_id=%s)"
            " ORDER BY COALESCE(m.message_id=%s,FALSE) DESC,"
            "ABS(EXTRACT(EPOCH FROM (COALESCE(m.message_create_time,o.first_observed_at)-%s))),"
            "m.message_id LIMIT 5",
            (cm_id, end, anchor["chat_id"], identity, _time(anchor) - timedelta(days=2),
             _time(anchor) + timedelta(days=2), anchor.get("parent_message_id"),
             anchor.get("parent_message_id"), _time(anchor)),
        ).fetchall())
    unique = {(r["chat_id"], r["message_id"]): r for r in rows}
    return sorted(unique.values(), key=lambda r: (_time(r), r["chat_id"], r["message_id"]))


def _make_evidence(conn, settings, scope, contact_id, rows):
    from .creator_workflow_ai import ScanInputError

    if not rows or len(rows) > MAX_MESSAGES or len({r["chat_id"] for r in rows}) > MAX_CHATS:
        raise ScanInputError("个人证据超出发送上限，未提交 AI。")
    subject = [r for r in rows if str(r.get("contact_id")) == str(contact_id)
               and r["sender_open_id"] not in scope["open_ids"]]
    if not subject or any(r.get("identity_type") != "user" or r.get("tenant_classification") == "internal"
                          for r in subject):
        raise ScanInputError("缺少有效的外部联系人，不能生成达人记录。")
    subject_ids = {r["sender_open_id"] for r in subject}
    known_names = {r.get("sender_name"): "U1" for r in subject}
    known_names[scope.get("display_name")] = "CM1"
    if any(r["sender_open_id"] not in subject_ids | scope["open_ids"] for r in rows):
        raise ScanInputError("个人证据含第三方消息，未提交 AI。")
    if not _direct_pair(rows, scope, contact_id):
        raise ScanInputError("未找到双方直接对话，保留在本地待 CM 核对。")
    rows = sorted(rows, key=lambda r: (_time(r), r["chat_id"], r["message_id"]))
    cipher = message_body_cipher(settings.token_encryption_key)
    labels = {(r["chat_id"], r["message_id"]): f"M{n}" for n, r in enumerate(rows, 1)}
    chats = {key: f"C{n}" for n, key in enumerate(sorted({r["chat_id"] for r in rows}), 1)}
    messages, refs, facts = [], [], []
    for row in rows:
        label = labels[row["chat_id"], row["message_id"]]
        manager = row["sender_open_id"] in scope["open_ids"]
        text = _text(cipher, row, scope, subject_ids, known_names)
        if not text.strip():
            raise ScanInputError("个人证据包含空正文，请重新预检。")
        messages.append({
            "ref": label, "user": None if manager else "U1", "speaker": "CM1" if manager else "external_or_unknown",
            "text": text, "time": str(_time(row)), "chat": chats[row["chat_id"]], "chat_type": row["chat_type"],
            "interaction": row.get("_interaction", "E1"),
            "private_user": "U1" if row["chat_type"] == "p2p" and
            str(row.get("counterpart_contact_id")) == str(contact_id) else None,
            "reply_to": labels.get((row["chat_id"], row.get("parent_message_id"))),
            "mentions": ["CM1" if m["open_id"] in scope["open_ids"] else "U1"
                         for m in mention_targets(row.get("mentions_json"))
                         if m["open_id"] in subject_ids | scope["open_ids"]],
        })
        refs.append({"label": label, "authorization_id": str(scope["id"]),
                     "chat_id": row["chat_id"], "message_id": row["message_id"],
                     "interaction": row.get("_interaction", "E1")})
        facts.append([row["chat_id"], row["message_id"], row["sender_open_id"], str(row.get("contact_id")),
                      row["tenant_classification"], str(row["message_update_time"]), str(row["first_observed_at"]),
                      row.get("p2p_counterpart_open_id"), row.get("mentions_json"), row.get("parent_message_id"),
                      hashlib.sha256(cipher.decrypt(row["body_ciphertext"]).encode()).hexdigest()])
    pack = {"cm": {"id": "CM1", "name": "CM1"}, "chat_name": None, "chat_type": "group",
            "users": [{"id": "U1", "name": "U1"}], "messages": messages, "private_user": None,
            "input_truncated": True, "history_synced": all(r["backfill_complete"] for r in rows),
            "evidence_scope": "Selected two-party excerpts, not a complete history; missing context is unknown."}
    encoded = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_PACK_CHARS:
        raise ScanInputError("个人证据超过 16000 字符上限，未提交 AI。")
    fingerprint = hashlib.sha256(json.dumps(
        [POLICY_VERSION, str(scope["id"]), str(scope["body_consent_at"]), str(contact_id), facts, pack],
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode()).hexdigest()
    return {"pack": pack, "refs": refs, "users": {"U1": {
        "contact_id": UUID(str(contact_id)), "name": subject[0].get("sender_name"),
        "open_id": subject[0]["sender_open_id"], "open_ids": sorted(subject_ids),
    }}, "input_fingerprint": fingerprint, "char_count": len(encoded)}


def build_preflight(conn, settings, cm_id, *, limit=30, window_end=None):
    """Stream all readable history locally; retain only bounded candidate anchors."""
    from .creator_workflow_ai import ScanInputError, _body

    cm_id, end = UUID(str(cm_id)), window_end or datetime.now(UTC)
    scope = _scope(conn, cm_id)
    limit = min(max(int(limit), 1), 30)
    confirmed = {str(r["contact_id"]) for r in conn.execute(
        """SELECT contact_id FROM creator_account_assignments
           WHERE responsible_authorization_id=%s AND ended_at IS NULL""", (cm_id,),
    ).fetchall()}
    processed = {(str(r["contact_id"]), r["input_fingerprint"]) for r in conn.execute(
        """SELECT t.contact_id,t.input_fingerprint FROM creator_workflow_ai_tasks t
           JOIN creator_workflow_ai_runs r ON r.id=t.run_id
           WHERE r.responsible_authorization_id=%s AND r.scan_mode='person_v1'
             AND (t.attempts>0 OR t.status='succeeded')""", (cm_id,),
    ).fetchall()}
    cipher = message_body_cipher(settings.token_encryption_key)
    anchors, people, aliases, excluded = defaultdict(dict), {}, {}, set()
    # Metadata first makes CM mentions resolvable only to actually observed users.
    metadata_sql = ("SELECT DISTINCT i.contact_id,i.display_name AS sender_name,i.identity_type,"
                    "i.tenant_classification,m.sender_open_id" + _ROWS[_ROWS.index(" FROM"):])
    # ponytail: two bounded-memory server passes; add a local text index only if measured preflight latency needs it.
    for pass_number in (0, 1):
        with conn.cursor(name="creator_local_" + uuid4().hex) as cursor:
            cursor.itersize = 250
            query, params = metadata_sql, (cm_id, end)
            if pass_number:
                query = _ROWS + " AND (m.sender_open_id=ANY(%s::text[]) OR (i.contact_id IS NOT NULL" \
                    " AND i.identity_type='user' AND i.tenant_classification<>'internal'))"
                params += (sorted(scope["open_ids"]),)
            cursor.execute(query, params)
            for row in cursor:
                contact = str(row["contact_id"]) if row["contact_id"] else None
                external = (contact and row["identity_type"] == "user" and
                            row["tenant_classification"] != "internal"
                            and row["sender_open_id"] not in scope["open_ids"])
                if pass_number == 0:
                    if external:
                        people.setdefault(contact, row.get("sender_name") or "—")
                        aliases[row["sender_open_id"]] = contact
                    elif contact:
                        excluded.add(contact)
                    continue
                if not external and row["sender_open_id"] not in scope["open_ids"]:
                    continue
                text = _body(cipher, row, clip=False)
                if not _BUSINESS.search(text):
                    continue
                targets = {contact} if external else set()
                if not external:
                    if row["chat_type"] == "p2p" and row["counterpart_contact_id"]:
                        targets.add(str(row["counterpart_contact_id"]))
                    if row["parent_contact_id"]:
                        targets.add(str(row["parent_contact_id"]))
                    targets.update(aliases.get(m["open_id"]) for m in mention_targets(row.get("mentions_json")))
                for target in targets & people.keys() - confirmed:
                    key = (row["chat_id"], _time(row).date().toordinal() // 7)
                    rank = ("0" if not external or row["chat_type"] == "p2p" else "1") + hashlib.sha256(
                        (row["chat_id"] + row["message_id"]).encode()).hexdigest()
                    saved = anchors[target]
                    if key not in saved or rank < saved[key][0]:
                        saved[key] = (rank, {key: row[key] for key in (
                            "chat_id", "message_id", "message_create_time", "first_observed_at", "parent_message_id")})
                    if len(saved) > MAX_ANCHORS:
                        del saved[max(saved, key=lambda k: saved[k][0])]
        if pass_number == 0:
            people = {key: name for key, name in people.items() if key not in excluded}
            aliases = {key: contact for key, contact in aliases.items() if contact in people}
    selected, pending, skipped_processed = [], [], 0
    for contact in sorted(anchors, key=lambda c: (-len(anchors[c]), c)):
        if len(selected) >= limit:
            break
        rows, episodes = {}, 0
        for _, anchor in sorted(anchors[contact].values(), key=lambda pair: _time(pair[1])):
            episode = _context(conn, cm_id, end, scope, contact, anchor)
            if not _direct_pair(episode, scope, contact):
                continue
            episode = [{**r, "_interaction": f"E{episodes + 1}"} for r in episode]
            merged = {**rows, **{(r["chat_id"], r["message_id"]): r for r in episode}}
            if len(merged) == len(rows):
                continue
            try:
                evidence = _make_evidence(conn, settings, scope, contact, list(merged.values()))
            except ScanInputError:
                continue
            rows, episodes = merged, episodes + 1
            if episodes == 3:
                break
        if not rows:
            pending.append({"contact_id": contact, "display_name": people[contact],
                            "reason": "有业务线索，但未找到可核实的双方直接对话；不发送 AI。"})
            continue
        if (contact, evidence["input_fingerprint"]) in processed:
            skipped_processed += 1
            continue
        selected.append({"contact_id": contact, "display_name": people[contact], "name": people[contact],
                         "chat_id": next(iter(rows))[0], "evidence_refs": evidence["refs"], **evidence,
                         "message_count": len(rows), "chat_count": len({r[0] for r in rows}),
                         "needs_review": False, "reason": "已选取双方历史业务对话片段；达人身份及归属仍由 AI 判断。"})
    return {"people": selected, "local_pending": pending, "window_end": end, "summary": {
        "scoped_people": len(people), "business_signal_people": len(anchors), "ready_people": len(selected),
        "selected_people": len(selected), "local_pending_people": len(pending),
        "unreviewed_people": max(0, len(anchors) - len(selected) - len(pending) - skipped_processed),
        "skipped_processed": skipped_processed,
        "confirmed_skipped": len(confirmed & people.keys()), "message_count": sum(p["message_count"] for p in selected),
        "char_count": sum(p["char_count"] for p in selected), "history_complete": True,
    }}


def load_person_evidence(conn, job, settings):
    """Reload exactly the frozen evidence; authorization or data changes cancel, never widen."""
    from .creator_workflow_ai import ScanInputError

    cm_id = job["responsible_authorization_id"]
    refs = job.get("evidence_refs") or []
    contact_id = job.get("contact_id")
    if not contact_id or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_MESSAGES:
        raise ScanInputError("缺少指定联系人或冻结证据，不能提交 AI。")
    keys = {(r.get("chat_id"), r.get("message_id")) for r in refs if isinstance(r, dict)}
    if len(keys) != len(refs) or len({k[0] for k in keys}) > MAX_CHATS or any(
        str(r.get("authorization_id", cm_id)) != str(cm_id) for r in refs
    ):
        raise ScanInputError("冻结证据范围不正确。")
    conn.execute("SELECT id FROM authorizations WHERE id=%s FOR SHARE", (cm_id,)).fetchall()
    scope = _scope(conn, cm_id)
    if job.get("consent_at") and job["consent_at"] != scope["body_consent_at"]:
        raise ScanInputError("正文授权发生变化，请重新预检。")
    chat_ids = sorted({k[0] for k in keys})
    conn.execute(
        """SELECT ac.chat_id FROM authorization_chats ac JOIN chats c USING(chat_id)
           WHERE ac.authorization_id=%s AND ac.chat_id=ANY(%s::text[]) ORDER BY ac.chat_id FOR SHARE OF ac,c""",
        (cm_id, chat_ids),
    ).fetchall()
    where = " AND (m.chat_id,m.message_id) IN (SELECT * FROM unnest(%s::text[],%s::text[]))"
    ordered = sorted(keys)
    args = (cm_id, job["window_end"], [k[0] for k in ordered], [k[1] for k in ordered])
    preview = conn.execute(_ROWS + where, args).fetchall()
    identity_ids = sorted({r["sender_open_id"] for r in preview} | scope["open_ids"] |
                          {r["p2p_counterpart_open_id"] for r in preview if r["p2p_counterpart_open_id"]})
    conn.execute(
        "SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id FOR SHARE",
        (identity_ids,),
    ).fetchall()
    current_scope = _scope(conn, cm_id)
    if current_scope["open_ids"] != scope["open_ids"] or current_scope["contact_id"] != scope["contact_id"]:
        raise ScanInputError("CM 的账号身份发生变化，请重新预检。")
    scope = current_scope
    rows = conn.execute(_ROWS + where + " ORDER BY m.chat_id,m.message_id FOR SHARE OF m,o", args).fetchall()
    if {(r["chat_id"], r["message_id"]) for r in rows} != keys or any(
        r["sender_open_id"] not in identity_ids for r in rows
    ):
        raise ScanInputError("消息已删除、不可见或身份发生变化，请重新预检。")
    interactions = {(r["chat_id"], r["message_id"]): r.get("interaction", "E1") for r in refs}
    if any(value not in {"E1", "E2", "E3"} for value in interactions.values()):
        raise ScanInputError("冻结证据的对话片段标识不正确。")
    rows = [{**r, "_interaction": interactions[r["chat_id"], r["message_id"]]} for r in rows]
    if not creator_workflow.selectable_users(conn, cm_id, contact_id=contact_id, limit=1):
        raise ScanInputError("该用户已不在 CM 可读取范围。")
    evidence = _make_evidence(conn, settings, scope, contact_id, rows)
    if evidence["input_fingerprint"] != job["input_fingerprint"]:
        raise ScanInputError("消息或用户资料已变化，本轮不提交旧证据。")
    creator_workflow.validate_evidence(conn, cm_id, evidence["refs"])
    return evidence
