"""One creator, one evidence request; verified high-confidence CMs are added independently."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from . import admin_permissions, ai_dispatch, ai_feature_config, assistant_rules, creator_data
from . import creator_responsibility_evidence as evidence
from . import openai_client as api
from .creator_access import require_owner
from .db import connection

FEATURE = "creator_responsibility_matching"
LEASE_SECONDS = 180
DECISIONS = {
    "responsible": "明确负责",
    "strong_candidate": "强负责人候选",
    "assist": "业务协助",
    "historical": "历史负责",
    "insufficient": "证据不足",
}
PERIODS = {"current": "当前", "historical": "历史", "uncertain": "时段待核实"}
SCOPES = {
    "account_ops": "账号运营",
    "content_live": "内容 / 直播",
    "samples_products": "样品 / 商品",
    "commercial": "商务合作",
    "technical_support": "技术支持",
    "other": "其他",
    "unknown": "范围待核实",
}
ASSOCIATIONS = {
    "auto_linked": "已自动关联",
    "already_linked": "现有关联保留",
    "review_required": "待核实",
    "blocked": "未自动关联",
}
STATUSES = {
    "queued": "排队中",
    "preparing": "准备材料",
    "ready": "等待调用",
    "running": "分析中",
    "retry_wait": "等待重试",
    "waiting_evidence": "材料不足",
    "completed": "已分析",
    "failed": "调用失败",
    "cancelled": "已取消",
}
REASONS = {
    "no_text": "没有可读文字",
    "no_candidates": "没有找到已接入的 CM/AM 候选",
    "not_creator": "不是已确认达人",
    "no_identity": "没有可用的外部人员身份",
    "material_limit": "单条材料超过准备范围",
    "restricted": "此人的数据使用已暂缓",
    "identity_removed": "不再是已确认达人",
    "snapshot_changed": "材料或授权变化，需要重新准备",
    "preparation_failed": "材料准备失败",
    "invalid_output": "返回格式无效",
    "provider_failed": "接口调用失败",
    "provider_retry": "接口暂时不可用",
    "quota_paused": "接口配额暂停",
    "owner_cancelled": "已取消",
    "lease_expired": "任务租约已过期",
    "request_unknown": "请求结果未知，需核实后重新检查",
    "cached": "复用相同材料的结果",
    "suggestions_saved": "结果已保存",
    "failure_cached": "相同材料上次调用失败",
    "rules_unavailable": "负责人匹配手册未发布，任务暂停",
}


def _hash(value):
    return hashlib.sha256(creator_data.encode(value).encode()).hexdigest()


def _config(conn, *, lock=False):
    return conn.execute(
        "SELECT provider,project_id,model,enabled,api_key_ciphertext,auto_confirm_threshold FROM ai_feature_configs "
        "WHERE feature=%s" + (" FOR UPDATE" if lock else ""),
        (FEATURE,),
    ).fetchone()


def _enabled(config):
    return bool(config and config["enabled"] and config["api_key_ciphertext"] and config["provider"] == "openai")


def _config_hash(config):
    return _hash(
        {
            **{k: config[k] for k in
               ("provider", "project_id", "model", "api_key_ciphertext", "auto_confirm_threshold")},
            "prompt_version": evidence.PROMPT_VERSION,
        }
    )


def _audit(conn, actor, action, metadata):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor, action, creator_data.encode(metadata)),
    )


def configuration_changed(conn):
    # A setting save never scans the company or starts a new historical batch.
    conn.execute("""UPDATE creator_responsibility_jobs SET desired_version=desired_version+1,
        status=CASE WHEN status IN ('running','preparing') THEN status ELSE 'queued' END,
        not_before=NOW(),updated_at=NOW() WHERE status NOT IN ('completed','failed','cancelled')
          AND NOT(reason_code='rules_unavailable' AND EXISTS(
            SELECT 1 FROM creator_responsibility_requests r WHERE r.job_id=creator_responsibility_jobs.id
              AND (r.finished_at IS NULL OR r.error_code<>'not_sent')))""")


def touch_chats(conn, chat_ids, *, authorization_id, message_ids=(), observed_message_ids=(), new_scope=False):
    """Wake affected pairs from new material, not from overlapping sync observations."""
    if not chat_ids or not (message_ids or observed_message_ids or new_scope) or not _enabled(_config(conn)):
        return 0
    from .evidence_access import BODY_READ_SCOPE_SQL

    actor = conn.execute("""SELECT a.id AS authorization_id,a.open_id AS authorization_open_id,
        a.body_consent_at,i.contact_id,ARRAY(SELECT open_id FROM lark_identities alias
          WHERE alias.contact_id=i.contact_id AND alias.identity_type='user') AS aliases
        FROM authorizations a JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
        WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL AND """ + BODY_READ_SCOPE_SQL,
        (authorization_id,)).fetchone()
    if not actor or not actor["contact_id"]:
        return 0
    columns = """m.chat_id,m.message_id,m.sender_open_id,m.message_type,m.message_create_time,m.parent_id,
        m.root_id,m.thread_id,m.mentions_json,m.body_fingerprint,c.chat_mode,ac.p2p_counterpart_open_id,
        (SELECT sender_open_id FROM chat_messages parent
          WHERE parent.chat_id=m.chat_id AND parent.message_id=m.parent_id) AS parent_sender"""
    readable = evidence.access._READABLE + " AND m.sender_type='user' AND m.message_type IN ('text','post') "
    ids = sorted(set(message_ids) | set(observed_message_ids))
    if ids:
        anchors = ("SELECT " + columns + " " + readable
                   + " AND m.chat_id=ANY(%s::text[]) AND m.message_id=ANY(%s::text[])")
        params = [authorization_id, sorted(set(chat_ids)), ids]
    else:
        # shortcut: discovery checks 256 recent headers/chat and 256 anchors total; message pages cover older history.
        anchors = """SELECT material.* FROM unnest(%s::text[]) chats(chat_id) CROSS JOIN LATERAL (
            SELECT * FROM (SELECT """ + columns + " " + readable + """ AND m.chat_id=chats.chat_id
              ORDER BY m.message_create_time DESC NULLS LAST,m.message_id LIMIT 256) recent
            WHERE chat_mode='p2p' OR sender_open_id=ANY(%s::text[]) OR parent_sender=ANY(%s::text[])
              OR EXISTS(SELECT 1 FROM unnest(%s::text[]) own(open_id) WHERE
                mentions_json @> jsonb_build_array(jsonb_build_object('id_type','open_id','id',own.open_id)))
            LIMIT 32) material LIMIT 256"""
        params = [sorted(set(chat_ids)), authorization_id, actor["aliases"], actor["aliases"], actor["aliases"]]
    rows = conn.execute("WITH anchors AS (" + anchors + """ )
        SELECT anchors.*,COALESCE((SELECT jsonb_agg(to_jsonb(neighbor)) FROM (
          SELECT """ + columns + " " + readable + """ AND m.chat_id=anchors.chat_id
            AND m.message_create_time BETWEEN anchors.message_create_time-INTERVAL '5 minutes'
              AND anchors.message_create_time+INTERVAL '5 minutes'
            AND m.message_id<>anchors.message_id
          ORDER BY abs(extract(epoch FROM m.message_create_time-anchors.message_create_time)),m.message_id LIMIT 32
        ) neighbor),'[]'::jsonb) AS neighbors FROM anchors""",
        [*params, authorization_id]).fetchall()
    if not rows:
        return 0
    opens = set(actor["aliases"])
    for row in rows:
        for message in [row, *row["neighbors"]]:
            opens.update([message["sender_open_id"], message["parent_sender"]])
            opens.update(t["open_id"] for t in evidence.mention_targets(message["mentions_json"])
                         if not t["ambiguous"])
    creators = {r["open_id"]: r["contact_id"] for r in conn.execute("""SELECT i.open_id,i.contact_id
        FROM lark_identities i JOIN contact_roles r ON r.contact_id=i.contact_id AND r.role='creator'
          AND r.status='confirmed' WHERE i.open_id=ANY(%s::text[]) AND i.identity_type='user'
          AND i.contact_id<>%s AND i.contact_id<>ALL(%s::uuid[])""",
        ([oid for oid in opens if oid], actor["contact_id"], sorted(evidence.source.RESTRICTED_CONTACTS))).fetchall()}
    grouped = {}
    for row in rows:
        mentions = {t["open_id"] for t in evidence.mention_targets(row["mentions_json"]) if not t["ambiguous"]}
        own = row["sender_open_id"] in actor["aliases"]
        addressed = bool(mentions.intersection(actor["aliases"]))
        targets = set()
        if own or addressed:
            targets.update(creators[oid] for oid in mentions | {row["parent_sender"]} if oid in creators)
            targets.update(creators[m["sender_open_id"]] for m in row["neighbors"] if m["sender_open_id"] in creators)
        if row["sender_open_id"] in creators and (row["chat_mode"] == "p2p" or addressed
                or row["parent_sender"] in actor["aliases"]
                or any(m["sender_open_id"] in actor["aliases"] for m in row["neighbors"])):
            targets.add(creators[row["sender_open_id"]])
        key = str(actor["contact_id"]) + ":" + row["chat_id"]
        for target in targets:
            grouped.setdefault(target, {}).setdefault(key, []).append(row)
    blocked = {r["contact_id"] for r in conn.execute("""SELECT x.contact_id FROM (
        SELECT contact_id,responsible_authorization_id,ended_at,source,ended_by_authorization_id
          FROM contact_assignments
        UNION ALL SELECT contact_id,responsible_authorization_id,ended_at,source,NULL::uuid
          FROM creator_account_assignments WHERE ended_at IS NOT NULL) x
        JOIN authorizations a ON a.id=x.responsible_authorization_id
        JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
        WHERE x.contact_id=ANY(%s::uuid[]) AND i.contact_id=%s AND (x.ended_at IS NULL
          OR x.ended_by_authorization_id IS NOT NULL OR x.source IN ('manual','cm_confirmation','excel_verified'))
        UNION SELECT contact_id FROM creator_responsibility_jobs WHERE contact_id=ANY(%s::uuid[])
          AND source='sample' AND status NOT IN ('completed','failed','cancelled')""",
        (list(grouped), actor["contact_id"], list(grouped))).fetchall()} if grouped else set()
    grant = evidence.source._scope_hash(actor)
    changed = 0
    for contact_id, materials in grouped.items():
        if contact_id in blocked:
            continue
        latest = conn.execute("""SELECT * FROM creator_responsibility_jobs WHERE contact_id=%s AND source<>'sample'
            ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE""", (contact_id,)).fetchone()
        previous = (latest or {}).get("stats", {}).get("auto_signals", {})
        known = {ref["contact_id"] for ref in (latest or {}).get("refs", [])
                 if ref.get("kind") == "person" and ref.get("label", "").startswith("CM")}
        known.update(key.split(":", 1)[0] for key in previous)
        scopes = {ref["scope_hash"] for ref in (latest or {}).get("refs", [])
                  if ref.get("kind") == "message" and ref.get("authorization_id") == str(authorization_id)}
        scope_changed = (grant not in scopes if scopes else bool(latest and latest["prepared_at"]
                         and actor["body_consent_at"] > latest["prepared_at"]))
        signals = {key: {"material": _hash(material), "grant": grant} for key, material in materials.items()}
        if not any(previous.get(key) != signature and (
                ids or key.split(":", 1)[0] not in known or scope_changed
                or previous.get(key, {}).get("grant", grant) != grant) for key, signature in signals.items()):
            continue
        job_id = conn.execute("SELECT enqueue_creator_responsibility(%s,'history') id", (contact_id,)).fetchone()["id"]
        if job_id:
            not_before = latest["not_before"] if latest and latest["status"] == "queued" else None
            conn.execute("""UPDATE creator_responsibility_jobs SET stats=stats||%s::jsonb,
                not_before=COALESCE(%s,NOW()+INTERVAL '30 seconds') WHERE id=%s""",
                (creator_data.encode({"auto_signals": {**previous, **signals}}), not_before, job_id))
            changed += 1
    return changed


def enqueue(conn, actor):
    require_owner(conn, actor)
    if not _enabled(_config(conn)):
        raise ValueError("请先配置并启用达人负责人匹配。")
    rows = conn.execute(
        """SELECT DISTINCT r.contact_id FROM contact_roles r
        WHERE r.role='creator' AND r.status='confirmed'
        AND NOT EXISTS(SELECT 1 FROM creator_responsibility_jobs j WHERE j.contact_id=r.contact_id
          AND (j.status NOT IN ('completed','failed','cancelled') OR j.status='completed'
            AND j.stats->>'prompt_version'=%s)) ORDER BY r.contact_id""",
        (evidence.PROMPT_VERSION,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "SELECT enqueue_creator_responsibility(%s,'history')", (row["contact_id"],)
        )
    counts = {"queued": len(rows)}
    _audit(
        conn,
        actor,
        "creator_responsibility_enqueued",
        {**counts, "all_contacts": True, "prompt_version": evidence.PROMPT_VERSION},
    )
    return counts


def _recover(conn):
    conn.execute("""UPDATE creator_responsibility_jobs SET status=CASE
        WHEN status='preparing' AND source<>'sample' THEN 'queued' ELSE 'failed' END,
        reason_code=CASE WHEN status='running' THEN 'request_unknown' ELSE 'lease_expired' END,
        claim_token=NULL,lease_until=NULL,not_before=NOW(),updated_at=NOW()
        WHERE status IN ('running','preparing') AND lease_until<NOW()""")
    conn.execute("""UPDATE creator_responsibility_requests r SET error_code='request_unknown',finished_at=NOW()
        FROM creator_responsibility_jobs j WHERE r.job_id=j.id AND j.reason_code='request_unknown'
        AND r.finished_at IS NULL""")
    conn.execute("""UPDATE creator_responsibility_jobs j SET status='cancelled',desired_version=desired_version+1,
        reason_code='identity_removed',claim_token=NULL,lease_until=NULL,updated_at=NOW()
        WHERE status NOT IN ('completed','failed','cancelled') AND NOT EXISTS(
          SELECT 1 FROM contact_roles r WHERE r.contact_id=j.contact_id
            AND r.role='creator' AND r.status='confirmed')""")


def _invalidate(conn, job):
    conn.execute(
        """UPDATE creator_responsibility_jobs SET status='queued',reason_code='snapshot_changed',
        source=CASE WHEN status='running' AND source<>'sample' AND EXISTS(
          SELECT 1 FROM creator_responsibility_requests r WHERE r.job_id=creator_responsibility_jobs.id
          AND r.error_code='request_unknown') THEN 'history' ELSE source END,
        claim_token=NULL,lease_until=NULL,result_ciphertext=NULL,not_before=NOW(),updated_at=NOW() WHERE id=%s""",
        (job["id"],),
    )


def _payment_material(material):
    return {**material, "pack": {key: value for key, value in material["pack"].items() if key != "_rules"}}


def _manual_access(conn, pack):
    if pack and "_rules" in pack:
        assistant_rules.responsibility_validate_access(conn, pack["_rules"])


def _pause_rules(conn, job):
    conn.execute("""UPDATE creator_responsibility_jobs SET status='waiting_evidence',
        reason_code='rules_unavailable',claim_token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=%s""",
        (job["id"],))


def _store_prepared(conn, job, material, config, settings):
    # Rotation fences an in-flight request, but does not invalidate a semantic result cache.
    pack = material["pack"]
    try:
        _manual_access(conn, pack)
    except PermissionError:
        _pause_rules(conn, job)
        return
    fingerprint = _hash(
        [material["fingerprint"], config["provider"], config["project_id"], config["model"], evidence.PROMPT_VERSION]
    )
    stats = {**material["stats"], "prompt_version": evidence.PROMPT_VERSION,
             **({"auto_signals": job["stats"]["auto_signals"]} if job["stats"].get("auto_signals") else {})}
    cache = (
        conn.execute(
            """SELECT result_ciphertext FROM creator_responsibility_jobs
        WHERE contact_id=%s AND fingerprint=%s AND result_ciphertext IS NOT NULL AND status='completed'
        ORDER BY updated_at DESC LIMIT 1""",
            (job["contact_id"], fingerprint),
        ).fetchone()
        if pack
        else None
    )
    if pack and not evidence.still_valid(conn, material["refs"], settings):
        _invalidate(conn, job)
        return
    storage = creator_data.cipher(settings)
    envelope = {"pack": pack, "candidate_ids": material["candidate_ids"]}
    unknown = False
    if pack and job["source"] != "manual" and not cache:
        unknown = conn.execute("""SELECT r.error_code FROM creator_responsibility_requests r
            JOIN creator_responsibility_jobs j ON j.id=r.job_id WHERE j.contact_id=%s
            AND r.material_fingerprint=%s
            AND (r.error_code IN ('request_unknown','rules_unavailable') OR r.finished_at IS NULL) LIMIT 1""",
            (job["contact_id"], _hash(_payment_material(envelope)))).fetchone()
        if not unknown:
            prior = conn.execute("""SELECT pack_ciphertext FROM creator_responsibility_jobs WHERE contact_id=%s
                AND status='failed' AND reason_code='request_unknown' AND pack_ciphertext IS NOT NULL""",
                (job["contact_id"],)).fetchall()
            unknown = any(_payment_material(json.loads(storage.decrypt(row["pack_ciphertext"])))
                          == _payment_material(envelope) for row in prior)
    rules_paused = isinstance(unknown, dict) and unknown.get("error_code") == "rules_unavailable"
    if cache:
        cached = json.loads(storage.decrypt(cache["result_ciphertext"]))
        cached["result"] = evidence.validate(pack, cached["result"])
        cached["candidate_ids"] = material["candidate_ids"]
        applied = _apply_assignments(conn, job, material, cached["result"], config, settings)
        if applied is None:
            _invalidate(conn, job)
            return
        cached["result"], material["refs"], stats["auto_linked"] = applied
        cache["result_ciphertext"] = storage.encrypt(creator_data.encode(cached))
    conn.execute(
        """UPDATE creator_responsibility_jobs SET status=%s,reason_code=%s,
        fingerprint=%s,config_fingerprint=%s,pack_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,
        result_ciphertext=%s,attempts=0,claim_token=NULL,lease_until=NULL,prepared_at=NOW(),updated_at=NOW(),
        finished_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s""",
        (
            "completed" if cache else "waiting_evidence" if rules_paused else "failed" if unknown
                else "ready" if pack else "waiting_evidence",
            "cached" if cache else "rules_unavailable" if rules_paused else "request_unknown" if unknown
                else material["reason"],
            fingerprint,
            _config_hash(config),
            storage.encrypt(creator_data.encode(envelope))
            if pack
            else None,
            creator_data.encode(material["refs"]),
            creator_data.encode(stats),
            cache["result_ciphertext"] if cache else None,
            bool(cache or unknown),
            job["id"],
        ),
    )


def prepare_next(settings):
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config):
            return False
        _recover(conn)
        if conn.execute("SELECT count(*) n FROM creator_responsibility_jobs WHERE status='ready'").fetchone()["n"] >= 5:
            return False
        job = conn.execute("""SELECT * FROM creator_responsibility_jobs WHERE status='queued' AND not_before<=NOW()
            AND source<>'sample' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return False
        token = uuid4()
        conn.execute(
            """UPDATE creator_responsibility_jobs SET status='preparing',snapshot_version=desired_version,
            claim_token=%s,lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s""",
            (token, job["id"]),
        )
    try:
        with connection() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout='90s'")
            pinned = assistant_rules.responsibility_snapshot(conn, settings)
            assistant_rules.responsibility_validate_access(conn, pinned)
            material = evidence.build(conn, settings, job["contact_id"])
            if material["pack"]:
                material["pack"]["_rules"] = pinned
                material["fingerprint"] = _hash([material["fingerprint"], pinned["digest"]])
            material["stats"] = {**material["stats"], "rules_digest": pinned["digest"],
                                 "rules_protocol": pinned["protocol"]}
    except PermissionError:
        with connection() as conn, conn.transaction():
            current = conn.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE",
                                   (job["id"],)).fetchone()
            if current and current["status"] == "preparing" and current["claim_token"] == token:
                _pause_rules(conn, current)
        return True
    except Exception:
        with connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE creator_responsibility_jobs SET status=CASE WHEN desired_version=%s
                THEN 'failed' ELSE 'queued' END,reason_code='preparation_failed',claim_token=NULL,lease_until=NULL,
                updated_at=NOW() WHERE id=%s AND status='preparing' AND claim_token=%s""",
                (job["desired_version"], job["id"], token),
            )
        raise
    with connection() as conn, conn.transaction():
        current_config = _config(conn, lock=True)
        current = conn.execute(
            "SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job["id"],)
        ).fetchone()
        if not current or current["status"] != "preparing" or current["claim_token"] != token:
            return True
        if (
            current["desired_version"] != job["desired_version"]
            or not _enabled(current_config)
            or (_config_hash(config) != _config_hash(current_config))
        ):
            _invalidate(conn, current)
        else:
            _store_prepared(conn, current, material, current_config, settings)
    return True


def claim():
    from .config import get_settings

    settings = get_settings()
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config) or not ai_dispatch.can_claim(conn):
            return None
        _recover(conn)
        job = conn.execute("""SELECT * FROM creator_responsibility_jobs WHERE status IN ('ready','retry_wait')
            AND source<>'sample' AND not_before<=NOW() AND attempts<3
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return None
        if job["desired_version"] != job["snapshot_version"] or job["config_fingerprint"] != _config_hash(config):
            _invalidate(conn, job)
            return None
        if not evidence.still_valid(conn, job["refs"], settings):
            _invalidate(conn, job)
            return None
        material = json.loads(creator_data.cipher(settings).decrypt(job["pack_ciphertext"]))
        try:
            _manual_access(conn, material["pack"])
        except PermissionError:
            _pause_rules(conn, job)
            return None
        token, reservation = uuid4(), uuid4()
        conn.execute(
            "INSERT INTO creator_responsibility_requests(id,job_id,key_feature,material_fingerprint) "
            "VALUES(%s,%s,%s,%s)",
            (reservation, job["id"], FEATURE, _hash(_payment_material(material))),
        )
        conn.execute(
            """UPDATE creator_responsibility_jobs SET status='running',claim_token=%s,
            lease_until=NOW()+(%s*INTERVAL '1 second'),attempts=attempts+1,updated_at=NOW() WHERE id=%s""",
            (token, LEASE_SECONDS, job["id"]),
        )
        return {
            **job,
            **material,
            "claim_token": token,
            "attempts": job["attempts"] + 1,
            "reservation_id": reservation,
            "config": config,
            "key_feature": FEATURE,
        }


def _current(conn, job):
    config = _config(conn, lock=True)
    row = conn.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    if not row or row["status"] != "running" or row["claim_token"] != job["claim_token"]:
        return None
    if (
        row["desired_version"] != job["snapshot_version"]
        or not _enabled(config)
        or _config_hash(config) != job["config_fingerprint"]
    ):
        _invalidate(conn, row)
        return None
    return row


def _error_code(error):
    if error is None:
        return ""
    if isinstance(error, api.ModelOutputError):
        return "invalid_output"
    if isinstance(error, api.OpenAIRequestError):
        if error.request_unknown:
            return "request_unknown"
        status = error.telemetry.get("http_status")
        return f"http_{status}" if isinstance(status, int) else "provider_failed"
    return "internal_error"


def review(pack, config, contact_id, settings):
    """One request for all candidate CMs. Original quotations are resolved locally from refs."""
    result, telemetry, error, diagnostic = None, {}, None, None
    try:
        key = ai_feature_config.api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
        response = api.call_openai(
            config,
            key,
            {key: value for key, value in pack.items() if key != "_rules"},
            contact_id,
            instructions=(assistant_rules.responsibility_instructions(pack["_rules"])
                          if "_rules" in pack else evidence.instructions())
                         + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。",
            output_format=evidence.output_format(pack),
            max_output_tokens=min(12000, 4000 + 800 * len(pack["candidates"])),
            reasoning_effort="none",
            total_timeout=70,
        )
        telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
        diagnostic = {"raw": response["text"][:64_000], "output_shape": telemetry.get("output_shape", [])}
        parsed = api.parse_json(response["text"])
        result = evidence.validate(pack, parsed)
        # Only canonical, bounded result fields; no raw returned text in plaintext logs.
        diagnostic = {"validated": result, "output_shape": telemetry.get("output_shape", [])}
    except Exception as exc:
        error = exc
        telemetry = getattr(exc, "telemetry", {}) or telemetry
        diagnostic = {**(diagnostic or {}), "error": str(exc)[:160],
                      "output_shape": telemetry.get("output_shape", []),
                      "response_status": telemetry.get("response_status"),
                      "incomplete_reason": telemetry.get("incomplete_reason"),
                      "usage": telemetry.get("usage", {})}
        if isinstance(exc, json.JSONDecodeError | TypeError):
            error = api.ModelOutputError("invalid_json", telemetry=telemetry)
    return result, telemetry, error, diagnostic


def _apply_assignments(conn, job, material, result, config, settings):
    """Append verified person-level pairs; lock and recheck the original evidence first."""
    refs = material["refs"]
    state_refs = [ref for ref in refs if ref.get("kind") == "responsibility_state"]
    if len(state_refs) != 1 or str(job["contact_id"]) in evidence.source.RESTRICTED_CONTACTS:
        return None
    before = evidence._state(conn, job["contact_id"])
    # OAuth writers lock authorization before identity/contact; retain that order.
    conn.execute("SELECT id FROM authorizations WHERE id=ANY(%s::uuid[]) ORDER BY id FOR SHARE",
                 ([str(row["id"]) for row in before["authorizations"]],)).fetchall()
    conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (job["contact_id"],)).fetchone()
    identity_ids = {row["open_id"] for row in before["directory"]}
    identity_ids.update(row["open_id"] for ref in refs for row in ref.get("identities", []))
    conn.execute("SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id FOR SHARE",
                 (sorted(identity_ids),)).fetchall()
    conn.execute("SELECT contact_id FROM contact_roles WHERE contact_id=%s ORDER BY role FOR SHARE",
                 (job["contact_id"],)).fetchall()
    conn.execute("SELECT id FROM contact_assignments WHERE contact_id=%s ORDER BY id FOR SHARE",
                 (job["contact_id"],)).fetchall()
    messages = [ref for ref in refs if ref.get("kind") == "message"]
    if not messages or any(not all(key in ref for key in ("chat_id", "message_id", "authorization_id"))
                           for ref in messages):
        return None
    locked = conn.execute("""SELECT m.chat_id,m.message_id,o.authorization_id
        FROM unnest(%s::text[],%s::text[],%s::uuid[]) ref(chat_id,message_id,authorization_id)
        JOIN chat_messages m ON m.chat_id=ref.chat_id AND m.message_id=ref.message_id
        JOIN authorization_message_observations o ON o.chat_id=m.chat_id AND o.message_id=m.message_id
          AND o.authorization_id=ref.authorization_id
        JOIN authorizations a ON a.id=o.authorization_id
        JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=m.chat_id
        JOIN chats c ON c.chat_id=m.chat_id
        ORDER BY a.id,m.chat_id,m.message_id FOR SHARE OF m,o,a,ac,c""",
        ([ref["chat_id"] for ref in messages], [ref["message_id"] for ref in messages],
         [ref["authorization_id"] for ref in messages])).fetchall()
    state = evidence._state(conn, job["contact_id"])
    if (len(locked) != len(messages) or evidence.access._hash(state) != state_refs[0]["state_hash"]
            or not any(row["status"] == "confirmed" for row in state["role"])
            or not evidence.source.still_valid(conn, [ref for ref in refs if ref["kind"] != "responsibility_state"],
                                               settings)):
        return None
    people = {str(row["id"]): str(row["contact_id"]) for row in state["authorizations"] if row["contact_id"]}
    admitted = {}
    for row in state["authorizations"]:
        if row["contact_id"] and row["status"] == "active" and row["body_consent_at"] is not None:
            admitted.setdefault(str(row["contact_id"]), []).append(row["id"])
    value = evidence.validate(material["pack"], result)
    coverage = material["pack"].get("coverage", {})
    threshold = Decimal(str(config["auto_confirm_threshold"]))
    added = 0
    for suggestion in value["suggestions"]:
        cm = str(material["candidate_ids"][suggestion["cm"]])
        history = [row for row in state["assignments"] if people.get(str(row["responsible_authorization_id"])) == cm]
        active = any(row["ended_at"] is None for row in history)
        suggestion.update(association_status="review_required", association_reason="")
        if active:
            suggestion["association_status"] = "already_linked"
            continue
        if (suggestion["decision"] != "responsible" or suggestion["period"] != "current"
                or suggestion["relation_level"] != "person" or suggestion.get("validation_error")
                or not suggestion["support_refs"] or suggestion["counter_refs"]
                or Decimal(str(suggestion["confidence"])) < threshold):
            continue
        if coverage.get("critical_evidence_omitted"):
            suggestion["association_reason"] = "材料遗漏了关键交接或解除记录，需要核实。"
            continue
        if set(suggestion["support_refs"]) & set(coverage.get("missing_reply_ids", [])):
            suggestion["association_reason"] = "支撑消息缺少实际回复上下文，需要核实。"
            continue
        if len(admitted.get(cm, [])) != 1:
            suggestion.update(association_status="blocked", association_reason="有效接入身份缺失或存在重复，需要核实。")
            continue
        if any(row["ended_at"] is not None and (row["ended_by_authorization_id"]
               or row["source"] in {"manual", "cm_confirmation", "excel_verified"}) for row in history):
            suggestion.update(association_status="blocked", association_reason="存在人工解除记录，不自动重新关联。")
            continue
        inserted = conn.execute("""INSERT INTO contact_assignments
            (id,contact_id,responsible_authorization_id,source,confidence)
            VALUES(%s,%s,%s,'ai',%s)
            ON CONFLICT(contact_id,responsible_authorization_id) WHERE ended_at IS NULL DO NOTHING
            RETURNING 'person' AS level,id,responsible_authorization_id,source,started_at,ended_at,
                ended_by_authorization_id,NULL::uuid AS account_id,NULL::text AS handle,NULL::text AS platform""",
            (uuid4(), job["contact_id"], admitted[cm][0], Decimal(str(suggestion["confidence"])))).fetchone()
        suggestion["association_status"] = "auto_linked" if inserted else "already_linked"
        if inserted:
            added += 1
            state["assignments"].append(inserted)
            _audit(conn, None, "creator_responsibility_auto_linked", {
                "job_id": str(job["id"]), "assignment_id": str(inserted["id"]),
                "contact_id": str(job["contact_id"]), "cm_contact_id": cm,
                "responsible_authorization_id": str(admitted[cm][0]),
                "confidence": suggestion["confidence"], "threshold": str(threshold),
                "support_refs": suggestion["support_refs"],
                "rules_digest": material["pack"].get("_rules", {}).get("digest"),
                "rules_protocol": material["pack"].get("_rules", {}).get("protocol"),
            })
    refreshed = deepcopy(refs)
    if added:
        # Refresh only our own additions, never bless concurrent source/directory changes.
        state["assignments"].sort(key=lambda row: (row["level"], str(row["id"])))
        state_ref = next(ref for ref in refreshed if ref["kind"] == "responsibility_state")
        state_ref["state_hash"] = evidence.access._hash(state)
    return value, refreshed, added


def run(job, settings):
    with connection() as conn, conn.transaction():
        current = _current(conn, job)
        if not current:
            _no_request(conn, job)
            return
        try:
            _manual_access(conn, job["pack"])
        except PermissionError:
            _pause_rules(conn, current)
            _no_request(conn, job)
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            _no_request(conn, job)
            return
    result, telemetry, error, diagnostic = review(job["pack"], job["config"], job["contact_id"], settings)
    with connection() as conn, conn.transaction():
        _config(conn, lock=True)
        usage = telemetry.get("usage", {})
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        conn.execute(
            """UPDATE creator_responsibility_requests SET request_id=%s,response_id=%s,error_code=%s,
            input_tokens=%s,output_tokens=%s,response_ciphertext=%s,finished_at=NOW() WHERE id=%s""",
            (
                telemetry.get("openai_request_id"),
                telemetry.get("response_id"),
                _error_code(error),
                input_tokens,
                output_tokens,
                creator_data.cipher(settings).encrypt(creator_data.encode(diagnostic)) if diagnostic else None,
                job["reservation_id"],
            ),
        )
        conn.execute(
            """UPDATE creator_responsibility_jobs SET input_tokens=input_tokens+%s,
            output_tokens=output_tokens+%s WHERE id=%s""",
            (input_tokens or 0, output_tokens or 0, job["id"]),
        )
    # Commit actual usage before admission or source/authorization queries can fail.
    ai_dispatch.observe_limits(telemetry)
    delay = (api.retry_delay(error.retry_after_seconds, job["attempts"])
             if isinstance(error, api.OpenAIRequestError) and error.retryable else 0)
    if isinstance(error, api.OpenAIRequestError):
        if error.is_quota:
            ai_dispatch.pause_for_quota()
        elif error.retryable:
            ai_dispatch.throttle(delay)
    with connection() as conn, conn.transaction():
        current = _current(conn, job)
        if not current:
            return
        try:
            _manual_access(conn, job["pack"])
        except PermissionError:
            _pause_rules(conn, current)
            if error is None:
                conn.execute("UPDATE creator_responsibility_requests SET error_code='rules_unavailable' WHERE id=%s",
                             (job["reservation_id"],))
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            return
        if error:
            retry = (
                isinstance(error, api.OpenAIRequestError)
                and error.retryable
                and job["attempts"] < 3
            )
            reason = (
                "request_unknown"
                if isinstance(error, api.OpenAIRequestError) and error.request_unknown
                else "quota_paused"
                if getattr(error, "is_quota", False)
                else "provider_retry"
                if retry
                else "invalid_output"
                if isinstance(error, api.ModelOutputError)
                else "provider_failed"
            )
            conn.execute(
                """UPDATE creator_responsibility_jobs SET status=%s,reason_code=%s,claim_token=NULL,
                lease_until=NULL,not_before=NOW()+(%s*INTERVAL '1 second'),updated_at=NOW() WHERE id=%s""",
                (
                    "retry_wait" if retry else "failed",
                    reason,
                    delay if retry else 0,
                    job["id"],
                ),
            )
        else:
            applied = _apply_assignments(conn, current, job, result, job["config"], settings)
            if applied is None:
                _invalidate(conn, current)
                return
            result, refs, added = applied
            view = {"candidate_ids": job["candidate_ids"], "result": result}
            conn.execute(
                """UPDATE creator_responsibility_jobs SET status='completed',reason_code='suggestions_saved',
                result_ciphertext=%s,refs=%s::jsonb,stats=stats||%s::jsonb,claim_token=NULL,lease_until=NULL,
                finished_at=NOW(),updated_at=NOW() WHERE id=%s""",
                (creator_data.cipher(settings).encrypt(creator_data.encode(view)), creator_data.encode(refs),
                 creator_data.encode({"auto_linked": added}), job["id"]),
            )
        conn.execute(
            "UPDATE ai_feature_configs SET last_tested_at=NOW(),last_test_status=%s WHERE feature=%s",
            ("failed" if error else "succeeded", FEATURE),
        )


def _no_request(conn, job):
    conn.execute(
        """UPDATE creator_responsibility_requests SET error_code='not_sent',input_tokens=0,
        output_tokens=0,finished_at=NOW() WHERE id=%s AND finished_at IS NULL""",
        (job["reservation_id"],),
    )


def _existing_assignments(conn, contact_ids):
    rows = (
        conn.execute(
            """SELECT DISTINCT s.contact_id,a.id authorization_id,'person' level,NULL::text handle,
        COALESCE(c.display_name,a.display_name,'未命名负责人') name,i.contact_id cm_contact_id
        FROM contact_assignments s
        JOIN authorizations a ON a.id=s.responsible_authorization_id
        LEFT JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
        LEFT JOIN contacts c ON c.id=i.contact_id WHERE s.contact_id=ANY(%s::uuid[]) AND s.ended_at IS NULL
        ORDER BY s.contact_id,name,a.id""",
            (list(contact_ids),),
        ).fetchall()
        if contact_ids
        else []
    )
    grouped = {}
    for row in rows:
        row["scope_label"] = "达人本人"
        grouped.setdefault(str(row["contact_id"]), []).append(row)
    return grouped


def _results(view, names, existing=()):
    results = []
    known = {str(row["cm_contact_id"]) for row in existing if row["cm_contact_id"]}
    for suggestion in view["result"]["suggestions"]:
        person_id = str(view["candidate_ids"][suggestion["cm"]])
        results.append(
            {
                **suggestion,
                "name": names.get(person_id) or suggestion["cm"],
                "existing": person_id in known,
                "confidence_label": f"{suggestion['confidence']:.0%}" if 'confidence' in suggestion else "—",
                "association_label": ASSOCIATIONS.get(suggestion.get("association_status"), "旧版建议"),
                "label": DECISIONS[suggestion["decision"]],
                "scope_codes": suggestion["scope"],
                "scope": "、".join(SCOPES[scope] for scope in suggestion["scope"]),
                "period": PERIODS[suggestion["period"]],
            }
        )
    return results


def page_data(conn, actor, settings, page=1):
    access = admin_permissions.require_page(conn, actor, "ai")
    config = _config(conn)
    latest = (
        "WITH latest AS (SELECT DISTINCT ON(contact_id) * FROM creator_responsibility_jobs "
        "ORDER BY contact_id,created_at DESC,id DESC) "
    )
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(latest + "SELECT status,count(*) n FROM latest GROUP BY status").fetchall()
    }
    jobs = conn.execute(
        latest
        + """SELECT j.id,j.contact_id,j.status,j.reason_code,j.stats,j.created_at,j.updated_at,
        j.result_ciphertext,j.refs,c.display_name FROM latest j JOIN contacts c ON c.id=j.contact_id
        ORDER BY j.updated_at DESC,j.id LIMIT 26 OFFSET %s""",
        ((page - 1) * 25,),
    ).fetchall()
    has_more = len(jobs) > 25
    jobs = jobs[:25]
    existing = _existing_assignments(conn, [job["contact_id"] for job in jobs])
    views = {}
    for job in jobs:
        job.update(
            name=job.pop("display_name"),
            status_label=STATUSES[job["status"]],
            reason=REASONS.get(job["reason_code"], job["reason_code"]),
            candidate_count=job["stats"].get("candidate_count", 0),
            outdated=job["stats"].get("prompt_version") != evidence.PROMPT_VERSION,
            results=[],
        )
        job["existing_assignments"] = existing.get(str(job["contact_id"]), [])
        ciphertext = job.pop("result_ciphertext")
        refs = job.pop("refs")
        if ciphertext:
            if evidence.still_valid(conn, refs, settings, lightweight=True):
                views[job["id"]] = json.loads(creator_data.cipher(settings).decrypt(ciphertext))
            else:
                job.update(reason="规则或材料已更新，请重新分析", stale=True)
    names = {str(row["id"]): row["display_name"] for row in conn.execute(
        "SELECT id,display_name FROM contacts WHERE id=ANY(%s::uuid[])",
        (sorted({str(cid) for view in views.values() for cid in view["candidate_ids"].values()}),)
    ).fetchall()} if views else {}
    for job in jobs:
        if job["id"] in views:
            job["results"] = _results(views[job["id"]], names, job["existing_assignments"])
            for result in job["results"]:
                for field in ("reason", "scope_note", "association_reason"):
                    result.pop(field, None)
    if any(job["results"] for job in jobs):
        _audit(
            conn, actor, "creator_responsibility_results_viewed", {"jobs": [str(j["id"]) for j in jobs if j["results"]]}
        )
    return {
        "jobs": jobs,
        "counts": counts,
        "enabled": _enabled(config),
        "threshold_label": f"{config['auto_confirm_threshold']:.0%}",
        "page": page,
        "has_more": has_more,
        "prompt_version": evidence.PROMPT_VERSION,
        "admin_access": access,
    }


def task_evidence(conn, actor, settings, job_id):
    require_owner(conn, actor)
    job = conn.execute(
        "SELECT j.*,c.display_name FROM creator_responsibility_jobs j JOIN contacts c ON c.id=j.contact_id "
        "WHERE j.id=%s",
        (job_id,),
    ).fetchone()
    if not job:
        raise LookupError("任务不存在。")
    if not job["pack_ciphertext"] or not evidence.still_valid(conn, job["refs"], settings):
        raise PermissionError("材料或授权已变化，请重新分析。")
    material = json.loads(creator_data.cipher(settings).decrypt(job["pack_ciphertext"]))
    pack = material["pack"]
    result = (
        json.loads(creator_data.cipher(settings).decrypt(job["result_ciphertext"]))
        if job["result_ciphertext"]
        else None
    )
    chat_names = {c["id"]: c["name"] for c in pack["chats"]}
    person_ids = {**material["candidate_ids"], pack["target"]: str(job["contact_id"])}
    for ref in job["refs"]:
        if ref.get("kind") == "person" and ref.get("label") not in person_ids:
            contact = ref.get("contact_id") or next(
                (i.get("contact_id") for i in ref.get("identities", []) if i.get("contact_id")), None
            )
            if contact:
                person_ids[ref["label"]] = str(contact)
    names = {
        str(r["id"]): r["display_name"]
        for r in conn.execute(
            "SELECT id,display_name FROM contacts WHERE id=ANY(%s::uuid[])", (list(person_ids.values()),)
        ).fetchall()
    }
    messages = [
        {
            "ref": m["id"],
            "chat_id": m["chat"],
            "chat_name": chat_names.get(m["chat"], m["chat"]),
            "sender_name": names.get(person_ids.get(m["speaker"], ""), m["speaker"]),
            "sent_at": m["at"],
            "body": m["text"],
            "reply_to": m.get("reply"),
        }
        for m in pack["messages"]
    ]
    _audit(
        conn, actor, "creator_responsibility_evidence_viewed", {"job_id": str(job_id), "message_count": len(messages)}
    )
    existing = _existing_assignments(conn, [job["contact_id"]]).get(str(job["contact_id"]), [])
    return {
        "creator_name": job["display_name"],
        "outdated": job["stats"].get("prompt_version") != evidence.PROMPT_VERSION,
        "job_id": str(job_id),
        "chats": pack["chats"],
        "messages": messages,
        "results": _results(result, names, existing) if result else [],
        "existing_assignments": existing,
        "coverage": pack.get("coverage", job["stats"]),
    }


def action(conn, actor, job_id, operation):
    require_owner(conn, actor)
    row = conn.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not row:
        raise LookupError("任务不存在。")
    if operation == "cancel":
        conn.execute(
            """UPDATE creator_responsibility_jobs SET status='cancelled',desired_version=desired_version+1,
            reason_code='owner_cancelled',claim_token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=%s""",
            (job_id,),
        )
    elif operation in {"retry", "recheck"}:
        if not _enabled(_config(conn)):
            raise ValueError("请先配置并启用达人负责人匹配。")
        conn.execute("SELECT enqueue_creator_responsibility(%s,'manual')", (row["contact_id"],))
    else:
        raise ValueError("操作无效。")
    _audit(conn, actor, "creator_responsibility_job_" + operation, {"job_id": str(job_id)})
