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

POLICY_VERSION = "creator-person-staged-evidence-v1"
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
_ACCOUNT = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/@[\w.\-]+[^\s<>\"']*"
    r"|\b(?:handle|username|tiktok(?:\s+(?:handle|account))?)\s*(?:is\b|:|=)\s*@?[\w.\-]+"
    r"|(?:账号|帐号|用户名)\s*(?:是|为|:|：|=)\s*@?[\w.\-]+"
    r"|(?<![\w@])@[\w.\-]+", re.I,
)
_HANDLE_SIGNAL = re.compile(r"\b(?:handle|username|account|tiktok)\b|账号|帐号|用户名", re.I)
STAGES = frozenset({"identity", "ownership", "handle"})

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


def _text(cipher, row, scope, subject_ids, known_names, stage):
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
    # Protect exact account strings before redacting names/numeric identifiers:
    # @alice and @123456789 are account clues, not prose names or phone numbers.
    pieces, position = [], 0
    for account in [*_ACCOUNT.finditer(text), None]:
        end = account.start() if account else len(text)
        piece = _ADDRESS.sub("[地址已隐藏]", _PHONE.sub("[电话/编号已隐藏]", text[position:end]))
        for value, alias in known_names.items():
            if value and len(value.strip()) > 2:
                piece = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", alias, piece, flags=re.I)
        pieces.append(piece)
        if account:
            pieces.append(account.group())
            position = account.end()
    text = "".join(pieces)
    signal = (_ACCOUNT.search(text) or _HANDLE_SIGNAL.search(text)) if stage == "handle" else _BUSINESS.search(text)
    start = max(0, signal.start() - 300) if signal and signal.start() >= MAX_BODY_CHARS else 0
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


def _context(conn, cm_id, end, scope, contact_id, anchor, stage):
    rows = []
    speakers = [("i.contact_id=%s", contact_id)]
    if stage == "ownership":
        speakers.append(("m.sender_open_id=ANY(%s::text[])", sorted(scope["open_ids"])))
    for where, identity in speakers:
        rows.extend(conn.execute(
            _ROWS + " AND m.chat_id=%s AND (" + where + ")"
            " AND (COALESCE(m.message_create_time,o.first_observed_at) BETWEEN %s AND %s"
            " OR m.message_id=%s)"
            " ORDER BY (m.message_id=ANY(%s::text[])) DESC,"
            "ABS(EXTRACT(EPOCH FROM (COALESCE(m.message_create_time,o.first_observed_at)-%s))),"
            "m.message_id LIMIT 5",
            (cm_id, end, anchor["chat_id"], identity, _time(anchor) - timedelta(days=2),
             _time(anchor) + timedelta(days=2), anchor.get("parent_message_id"),
             [anchor["message_id"], anchor.get("parent_message_id")], _time(anchor)),
        ).fetchall())
    unique = {(r["chat_id"], r["message_id"]): r for r in rows}
    return sorted(unique.values(), key=lambda r: (_time(r), r["chat_id"], r["message_id"]))


def _make_evidence(conn, settings, scope, contact_id, rows, stage, excel_refs=None):
    from .creator_workflow_ai import ScanInputError

    if stage not in STAGES:
        raise ScanInputError("未知的达人核对阶段。")
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
    if stage != "ownership" and len(subject) != len(rows):
        raise ScanInputError("当前阶段仅允许该用户本人的发言。")
    if stage == "ownership" and not _direct_pair(rows, scope, contact_id):
        raise ScanInputError("未找到双方直接对话，保留在本地待 CM 核对。")
    rows = sorted(rows, key=lambda r: (_time(r), r["chat_id"], r["message_id"]))
    cipher = message_body_cipher(settings.token_encryption_key)
    labels = {(r["chat_id"], r["message_id"]): f"M{n}" for n, r in enumerate(rows, 1)}
    chats = {key: f"C{n}" for n, key in enumerate(sorted({r["chat_id"] for r in rows}), 1)}
    messages, refs, facts = [], [], []
    for row in rows:
        label = labels[row["chat_id"], row["message_id"]]
        manager = row["sender_open_id"] in scope["open_ids"]
        text = _text(cipher, row, scope, subject_ids, known_names, stage)
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
    pack = {"stage": stage, "cm": {"id": "CM1", "name": "CM1"}, "chat_name": None, "chat_type": "group",
            "users": [{"id": "U1", "name": "U1"}], "messages": messages, "private_user": None,
            "input_truncated": True, "history_synced": all(r["backfill_complete"] for r in rows),
            "evidence_scope": "Selected authorized excerpts, not complete history; missing context is unknown."}
    excel_refs = excel_refs or []
    if stage == "handle":
        pack["handle_candidates"] = [
            {"handle": r["handle"], "source": "excel", "ref": f"X{n}"}
            for n, r in enumerate(excel_refs, 1)
        ]
    encoded = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_PACK_CHARS:
        raise ScanInputError("个人证据超过 16000 字符上限，未提交 AI。")
    fingerprint = hashlib.sha256(json.dumps(
        [POLICY_VERSION, str(scope["id"]), str(scope["body_consent_at"]), str(contact_id), facts, pack, excel_refs],
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode()).hexdigest()
    return {"pack": pack, "refs": refs, "users": {"U1": {
        "contact_id": UUID(str(contact_id)), "name": subject[0].get("sender_name"),
        "open_id": subject[0]["sender_open_id"], "open_ids": sorted(subject_ids),
    }}, "input_fingerprint": fingerprint, "char_count": len(encoded), "excel_refs": excel_refs}


def _add_anchor(saved, row):
    key = (row["chat_id"], _time(row).date().toordinal() // 7)
    rank = hashlib.sha256((row["chat_id"] + row["message_id"]).encode()).hexdigest()
    if key not in saved or rank < saved[key][0]:
        saved[key] = (rank, {name: row[name] for name in (
            "chat_id", "message_id", "message_create_time", "first_observed_at", "parent_message_id")})
    if len(saved) > MAX_ANCHORS:
        del saved[max(saved, key=lambda k: saved[k][0])]


def _from_anchors(conn, settings, scope, contact_id, end, anchors, stage, excel_refs=None):
    from .creator_workflow_ai import ScanInputError

    rows, episodes, evidence = {}, 0, None
    for _, anchor in sorted(anchors.values(), key=lambda pair: _time(pair[1])):
        episode = _context(conn, scope["id"], end, scope, contact_id, anchor, stage)
        if stage == "ownership" and not _direct_pair(episode, scope, contact_id):
            continue
        episode = [{**r, "_interaction": f"E{episodes + 1}"} for r in episode]
        merged = {**rows, **{(r["chat_id"], r["message_id"]): r for r in episode}}
        if len(merged) == len(rows):
            continue
        try:
            current = _make_evidence(conn, settings, scope, contact_id, list(merged.values()), stage, excel_refs)
        except ScanInputError:
            continue
        rows, episodes, evidence = merged, episodes + 1, current
        if episodes == 3:
            break
    return evidence


def _excel_refs(conn, cm_id, contact_id, frozen=None):
    """An imported handle is a clue, never proof that the Lark user owns it."""
    from .creator_workflow_ai import ScanInputError

    rows = conn.execute(
        """SELECT s.id,s.fingerprint,c.id AS candidate_id,
                  c.normalized_handle AS handle
           FROM creator_workflow_sources s JOIN creator_workflow_candidates c ON c.id=s.candidate_id
           WHERE s.source_type='excel' AND c.responsible_authorization_id=%s
             AND c.contact_id=%s AND c.merged_into IS NULL AND c.status IN ('pending','deferred','confirmed')
             AND c.normalized_handle IS NOT NULL
             AND s.source_data->>'proposed_cm_id'=%s
             AND s.source_data->>'proposed_handle'=c.normalized_handle
           ORDER BY s.id LIMIT 10 FOR SHARE OF s,c""", (cm_id, contact_id, str(cm_id)),
    ).fetchall()
    refs = [{"id": str(r["id"]), "fingerprint": r["fingerprint"],
             "candidate_id": str(r["candidate_id"]), "handle": r["handle"]} for r in rows]
    if frozen is not None and refs != frozen:
        raise ScanInputError("Excel 账号或对应用户已变化，请重新核对账号。")
    return refs


def build_stage_evidence(conn, settings, cm_id, contact_id, stage, window_end=None):
    """Called only after the preceding gate passes; absence is unknown, never no."""
    from .creator_workflow_ai import ScanInputError, _body

    if stage not in STAGES:
        raise ScanInputError("未知的达人核对阶段。")
    cm_id, contact_id, end = UUID(str(cm_id)), UUID(str(contact_id)), window_end or datetime.now(UTC)
    scope = _scope(conn, cm_id)
    if not creator_workflow.selectable_users(conn, cm_id, contact_id=contact_id, limit=1):
        raise ScanInputError("该用户已不在 CM 可读取范围。")
    subject_ids = {r["open_id"] for r in conn.execute(
        "SELECT open_id FROM lark_identities WHERE contact_id=%s AND identity_type='user'", (contact_id,),
    ).fetchall()}
    excel_refs = _excel_refs(conn, cm_id, contact_id) if stage == "handle" else []
    cipher = message_body_cipher(settings.token_encryption_key)
    anchors, fallback = {}, {}
    where = " AND i.contact_id=%s"
    params = (cm_id, end, contact_id)
    if stage == "ownership":
        # CM speech is considered only in chats where this person was observed.
        where = """ AND (i.contact_id=%s OR (m.sender_open_id=ANY(%s::text[]) AND m.chat_id IN (
            SELECT DISTINCT x.chat_id FROM authorization_message_observations ox
            JOIN chat_messages x ON x.chat_id=ox.chat_id AND x.message_id=ox.message_id
            JOIN lark_identities ix ON ix.open_id=x.sender_open_id
            WHERE ox.authorization_id=%s AND ix.contact_id=%s)))"""
        params += (sorted(scope["open_ids"]), cm_id, contact_id)
    with conn.cursor(name="creator_stage_" + uuid4().hex) as cursor:
        cursor.itersize = 250
        cursor.execute(_ROWS + where, params)
        for row in cursor:
            own = str(row.get("contact_id")) == str(contact_id) and row["sender_open_id"] not in scope["open_ids"]
            if own and (row["identity_type"] != "user" or row["tenant_classification"] == "internal"):
                raise ScanInputError("当前用户不是有效的外部联系人。")
            text = _body(cipher, row, clip=False)
            if stage == "handle":
                if not own:
                    continue
                if _ACCOUNT.search(_EMAIL.sub("", text)) or _HANDLE_SIGNAL.search(text):
                    _add_anchor(anchors, row)
                elif excel_refs and _BUSINESS.search(text):
                    _add_anchor(fallback, row)
                continue
            if not _BUSINESS.search(text):
                continue
            if not own and not (
                str(row.get("counterpart_contact_id")) == str(contact_id)
                or str(row.get("parent_contact_id")) == str(contact_id)
                or subject_ids & {m["open_id"] for m in mention_targets(row.get("mentions_json"))}
            ):
                continue
            _add_anchor(anchors, row)
    return _from_anchors(conn, settings, scope, contact_id, end, anchors or fallback, stage, excel_refs)


def build_preflight(conn, settings, cm_id, *, limit=30, window_end=None):
    """Stage one retrieves the person's own speech, never a presumed CM relationship."""
    from .creator_workflow_ai import SCAN_MODE, _body

    cm_id, end = UUID(str(cm_id)), window_end or datetime.now(UTC)
    scope = _scope(conn, cm_id)
    limit = min(max(int(limit), 1), 30)
    confirmed = {str(r["contact_id"]) for r in conn.execute(
        """SELECT contact_id FROM creator_account_assignments
           WHERE responsible_authorization_id=%s AND ended_at IS NULL""", (cm_id,),
    ).fetchall()}
    processed = {str(r["contact_id"]) for r in conn.execute(
        """SELECT t.contact_id
           FROM creator_workflow_ai_tasks t
           JOIN creator_workflow_ai_runs r ON r.id=t.run_id
           WHERE r.responsible_authorization_id=%s AND r.scan_mode=%s
             AND (t.attempts>0 OR t.status='succeeded')""", (cm_id, SCAN_MODE),
    ).fetchall()}
    cipher = message_body_cipher(settings.token_encryption_key)
    anchors, people, excluded = defaultdict(dict), {}, set()
    # Metadata first excludes known internal people even if one alias is unclassified.
    metadata_sql = ("SELECT DISTINCT i.contact_id,i.display_name AS sender_name,i.identity_type,"
                    "i.tenant_classification,m.sender_open_id" + _ROWS[_ROWS.index(" FROM"):])
    # ponytail: two bounded-memory server passes; add a local text index only if measured preflight latency needs it.
    for pass_number in (0, 1):
        with conn.cursor(name="creator_local_" + uuid4().hex) as cursor:
            cursor.itersize = 250
            query, params = metadata_sql, (cm_id, end)
            if pass_number:
                query = _ROWS + " AND i.contact_id IS NOT NULL AND i.identity_type='user'" \
                    " AND i.tenant_classification<>'internal'"
            cursor.execute(query, params)
            for row in cursor:
                contact = str(row["contact_id"]) if row["contact_id"] else None
                external = (contact and row["identity_type"] == "user" and
                            row["tenant_classification"] != "internal"
                            and row["sender_open_id"] not in scope["open_ids"])
                if pass_number == 0:
                    if external:
                        people.setdefault(contact, row.get("sender_name") or "—")
                    elif contact:
                        excluded.add(contact)
                    continue
                if not external or contact not in people or contact in confirmed:
                    continue
                text = _body(cipher, row, clip=False)
                if not _BUSINESS.search(text):
                    continue
                _add_anchor(anchors[contact], row)
        if pass_number == 0:
            people = {key: name for key, name in people.items() if key not in excluded}
    selected, pending, skipped_processed = [], [], 0
    for contact in sorted(anchors, key=lambda c: (-len(anchors[c]), c)):
        if len(selected) >= limit:
            break
        evidence = _from_anchors(conn, settings, scope, contact, end, anchors[contact], "identity")
        if not evidence:
            pending.append({"contact_id": contact, "display_name": people[contact],
                            "reason": "有业务线索，但未找到可安全提交的本人发言；待补充资料。"})
            continue
        if contact in processed:
            skipped_processed += 1
            continue
        selected.append({"contact_id": contact, "display_name": people[contact], "name": people[contact],
                         "chat_id": evidence["refs"][0]["chat_id"], "evidence_refs": evidence["refs"], **evidence,
                         "message_count": len(evidence["refs"]),
                         "chat_count": len({r["chat_id"] for r in evidence["refs"]}),
                         "needs_review": False,
                         "reason": "已选取本人历史业务发言；先由 AI 判断是否达人，暂不判断归属或账号。"})
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
    stage = job.get("stage") or "identity"
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
    # Give PostgreSQL selective index predicates before the exact paired-ID
    # guard; otherwise it may join the CM's entire history before filtering 30 rows.
    where = (" AND (m.chat_id,m.message_id) IN (SELECT * FROM unnest(%s::text[],%s::text[]))"
             " AND m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])")
    ordered = sorted(keys)
    frozen_chats, frozen_messages = [k[0] for k in ordered], [k[1] for k in ordered]
    args = (cm_id, job["window_end"], frozen_chats, frozen_messages, frozen_chats, frozen_messages)
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
    metadata = job.get("person_metadata") or {}
    frozen_excel = metadata.get("excel_refs", job.get("excel_refs", []))
    excel_refs = _excel_refs(conn, cm_id, contact_id, frozen_excel) if stage == "handle" else []
    evidence = _make_evidence(conn, settings, scope, contact_id, rows, stage, excel_refs)
    if evidence["input_fingerprint"] != job["input_fingerprint"]:
        raise ScanInputError("消息或用户资料已变化，本轮不提交旧证据。")
    creator_workflow.validate_evidence(conn, cm_id, evidence["refs"])
    return evidence
