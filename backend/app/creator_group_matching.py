"""One canonical group, one paid request; strong results classify without overriding humans."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from psycopg.errors import LockNotAvailable

from . import admin_permissions, ai_dispatch, ai_feature_config, assistant_rules, creator_data
from . import creator_group_evidence as evidence
from . import openai_client as api
from .creator_access import require_owner
from .db import connection

FEATURE = "creator_group_classification"
PURPOSES = evidence.PURPOSES
STATUSES = {
    "queued": "待处理", "preparing": "整理证据", "ready": "待调用", "running": "分析中",
    "retry_wait": "等待重试", "waiting_evidence": "等待证据", "completed": "已分析",
    "failed": "失败", "cancelled": "已取消",
}
REASONS = {
    "no_text": "没有可读消息", "no_access": "没有当前有效的聊天授权", "not_eligible": "不在达人群处理范围",
    "not_group": "不是有效群聊", "no_creators": "没有相关已确认达人", "no_authorization": "没有有效聊天授权",
    "restricted": "不在授权处理范围", "material_limit": "材料超过准备范围", "ready": "证据已准备",
    "cached": "复用相同材料结果", "suggestions_saved": "分类建议已保存", "snapshot_changed": "材料或配置变化",
    "preparation_failed": "证据准备失败", "invalid_output": "返回格式无效", "provider_failed": "接口调用失败",
    "provider_retry": "服务暂不可用", "quota_paused": "接口配额暂停", "request_unknown": "请求结果未知，需手动复查",
    "lease_expired": "准备任务已过期", "owner_cancelled": "已取消", "manual_confirmed": "已人工确认并锁定",
    "config_paused": "功能已停用", "scope_changed": "群关系或授权已变化", "auto_linked": "分类已自动入库",
    "unknown_purpose": "群用途不明确", "low_confidence": "置信度未达门槛",
    "no_message_reference": "缺少正文依据", "reply_context_missing": "回复上下文缺失",
    "invalid_reference": "引用无法核对", "manual_protected": "保留人工分类",
    "superseded": "已有更新的结果", "admission_busy": "等待本地入库",
    "evidence_unavailable": "证据或授权已变化",
    "rules_unavailable": "群用途手册已停用",
    "application_pending": "结果已返回，等待本地入库",
}


def _hash(value):
    return hashlib.sha256(creator_data.encode(value).encode()).hexdigest()


def _config(conn, *, lock=False):
    return conn.execute(
        "SELECT provider,project_id,model,enabled,api_key_ciphertext,auto_confirm_threshold "
        "FROM ai_feature_configs WHERE feature=%s"
        + (" FOR UPDATE" if lock else ""), (FEATURE,),
    ).fetchone()


def _enabled(config):
    return bool(config and config["enabled"] and config["api_key_ciphertext"] and config["provider"] == "openai")


def _config_hash(config):
    # Output-only revisions reuse the same decisions; credentials and admission rules still fence requests.
    return _hash({**dict(config or {}), "prompt_version": evidence.CLASSIFICATION_VERSION})


def _audit(conn, actor, action, metadata):
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
                 (uuid4(), actor, "creator_group_" + action, creator_data.encode(metadata)))


def _business_pack(pack):
    return {key: value for key, value in pack.items() if key != "_rules"}


def _manual_access(conn, pack):
    if "_rules" in pack:
        assistant_rules.group_validate_access(conn, pack["_rules"])


def _pause_rules(conn, job, *, keep_material=True):
    # Keep the open-job slot, frozen response and billing history when documents are unpublished.
    conn.execute("""UPDATE creator_group_jobs SET
        status=CASE WHEN status='completed' THEN status ELSE 'waiting_evidence' END,
        reason_code='rules_unavailable',pack_ciphertext=CASE WHEN %s THEN pack_ciphertext END,
        result_ciphertext=CASE WHEN %s THEN result_ciphertext END,
        stats=stats||'{"admission_reason":"rules_unavailable"}'::jsonb
            ||CASE WHEN status='completed' THEN '{"auto_deferred":true}'::jsonb ELSE '{}'::jsonb END,
        claim_token=NULL,lease_until=NULL,
        updated_at=NOW() WHERE id=%s""", (keep_material, keep_material, job["id"]))


def _admission_reason(pack, result, config):
    if not _enabled(config):
        return "config_paused"
    if result["purpose"] == "unknown":
        return "unknown_purpose"
    if Decimal(str(result["confidence"])) < Decimal(str(config["auto_confirm_threshold"])):
        return "low_confidence"
    cited = set(result["refs"])
    if not cited.intersection(m["id"] for m in pack["messages"]):
        return "no_message_reference"
    allowed = set(evidence.output_format(pack)["schema"]["properties"]["refs"]["items"]["enum"])
    if cited - allowed:
        return "reply_context_missing" if cited <= {"G1", *(m["id"] for m in pack["messages"])} else "invalid_reference"
    return ""


def _admission_stats(job, result, admitted):
    return {"suggested_purpose": result["purpose"], "auto_linked": bool(admitted),
            "auto_deferred": admitted is None,
            "admission_reason": "admission_busy" if admitted is None else job.get("admission_reason", "")}


def _auto_classify(conn, job, pack, refs, result, config, settings):
    """The same admission fence for fresh and cached results; never impersonate a human."""
    _manual_access(conn, pack)
    pack = _business_pack(pack)
    threshold = Decimal(str(config["auto_confirm_threshold"]))
    job["admission_reason"] = _admission_reason(pack, result, config)
    if job["admission_reason"]:
        return False
    messages = [ref for ref in refs if ref.get("kind") == "message"]
    existing = conn.execute("SELECT source,locked FROM chat_group_classifications WHERE chat_id=%s",
                            (job["chat_id"],)).fetchone()
    if existing and (existing["source"] == "manual" or existing["locked"]):
        job["admission_reason"] = "manual_protected"
        return False
    # Revocation/edit triggers also touch jobs. Do not wait on their locks or repeat a paid call.
    try:
        with conn.transaction():
            snapshot = evidence.state(conn, job["chat_id"])
            conn.execute("SELECT id FROM authorizations WHERE id=ANY(%s::uuid[]) ORDER BY id FOR SHARE NOWAIT",
                         ([str(g["id"]) for g in snapshot["grants"]],)).fetchall()
            conn.execute("SELECT chat_id FROM chats WHERE chat_id=%s FOR UPDATE NOWAIT",
                         (job["chat_id"],)).fetchone()
            latest = conn.execute("""SELECT id FROM creator_group_jobs WHERE chat_id=%s
                ORDER BY created_at DESC,id DESC LIMIT 1""", (job["chat_id"],)).fetchone()
            if not latest or latest["id"] != job["id"]:
                job["admission_reason"] = "superseded"
                return False  # A deferred old result must never replace a newer analysis.
            conn.execute("SELECT open_id FROM chat_members WHERE chat_id=%s ORDER BY open_id FOR SHARE NOWAIT",
                         (job["chat_id"],)).fetchall()
            identity_ids = {m["open_id"] for m in snapshot["members"]}
            identity_ids.update(i["open_id"] for ref in refs for i in ref.get("identities", []))
            conn.execute("SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) "
                         "ORDER BY open_id FOR SHARE NOWAIT", (sorted(identity_ids),)).fetchall()
            contacts = {str(m["contact_id"]) for m in snapshot["members"] if m["contact_id"]}
            contacts.update(str(c["contact_id"]) for c in snapshot["creators"])
            conn.execute("SELECT contact_id FROM contact_roles WHERE contact_id=ANY(%s::uuid[]) "
                         "ORDER BY contact_id,role FOR SHARE NOWAIT", (sorted(contacts),)).fetchall()
            locked = conn.execute("""SELECT m.message_id FROM
                unnest(%s::text[],%s::text[],%s::uuid[]) ref(chat_id,message_id,authorization_id)
                JOIN chat_messages m ON m.chat_id=ref.chat_id AND m.message_id=ref.message_id
                JOIN authorization_message_observations o ON o.chat_id=m.chat_id AND o.message_id=m.message_id
                  AND o.authorization_id=ref.authorization_id
                JOIN authorizations a ON a.id=o.authorization_id
                JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=m.chat_id
                ORDER BY a.id,m.chat_id,m.message_id FOR SHARE OF m,o,a,ac NOWAIT""",
                ([r["chat_id"] for r in messages], [r["message_id"] for r in messages],
                 [r["authorization_id"] for r in messages])).fetchall()
            if len(locked) != len(messages) or not evidence.still_valid(conn, refs, settings):
                job["admission_reason"] = "evidence_unavailable"
                return False
            saved = conn.execute("""INSERT INTO chat_group_classifications
                (chat_id,purpose,tags,locked,source,confidence,source_job_id)
                VALUES(%s,%s,%s::jsonb,FALSE,'ai',%s,%s) ON CONFLICT(chat_id) DO UPDATE SET
                purpose=EXCLUDED.purpose,tags=EXCLUDED.tags,confidence=EXCLUDED.confidence,
                source_job_id=EXCLUDED.source_job_id,confirmed_at=NOW(),
                revision=chat_group_classifications.revision+1,updated_at=NOW()
                WHERE chat_group_classifications.source='ai' AND NOT chat_group_classifications.locked
                RETURNING chat_id""", (job["chat_id"], result["purpose"], creator_data.encode(result["tags"]),
                                        Decimal(str(result["confidence"])), job["id"])).fetchone()
            if saved:
                _audit(conn, None, "auto_linked", {"chat_id": job["chat_id"], "job_id": str(job["id"]),
                    "purpose": result["purpose"], "confidence": result["confidence"],
                    "threshold": str(threshold), "refs": result["refs"]})
            job["admission_reason"] = "" if saved else "manual_protected"
            return bool(saved)
    except LockNotAvailable:
        job["admission_reason"] = "admission_busy"
        return None  # The paid result survives; only local admission needs another attempt.


def configuration_changed(conn):
    enabled = _enabled(_config(conn))
    conn.execute("""UPDATE creator_group_jobs SET desired_version=desired_version+1,
        status=CASE WHEN NOT %s THEN 'cancelled' WHEN status IN ('preparing','running') THEN status ELSE 'queued' END,
        reason_code=CASE WHEN %s THEN 'snapshot_changed' ELSE 'config_paused' END,
        not_before=NOW(),updated_at=NOW() WHERE status NOT IN ('completed','failed','cancelled')
        AND reason_code<>'rules_unavailable'""",
                 (enabled, enabled))


def touch_chats(conn, chat_ids, settings=None):
    """One debounced signal per committed page/snapshot, not per member or message."""
    if not chat_ids or not _enabled(_config(conn)):
        return
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    current = {r["chat_id"]: r for r in conn.execute("""SELECT DISTINCT ON(chat_id)
        chat_id,status,reason_code,stats,refs FROM creator_group_jobs WHERE chat_id=ANY(%s::text[])
        ORDER BY chat_id,created_at DESC,id DESC""", (sorted(set(chat_ids)),)).fetchall()}
    for chat_id in sorted(set(chat_ids)):
        job = current.get(chat_id)
        if job and job["reason_code"] == "rules_unavailable":
            continue
        if job and (job["status"] == "completed"
                and job["stats"].get("prompt_version") in evidence.COMPATIBLE_PROMPT_VERSIONS
                and job["stats"].get("suggested_purpose") not in {None, "unknown"}
                and evidence.still_valid(conn, job["refs"], settings, lightweight=True)):
            continue
        conn.execute("SELECT enqueue_creator_group(%s)", (chat_id,))


def enqueue(conn, actor):
    require_owner(conn, actor)
    if not _enabled(_config(conn)):
        raise ValueError("请先配置并启用达人群用途匹配。")
    rows = conn.execute("""SELECT c.chat_id FROM chats c WHERE creator_group_is_eligible(c.chat_id)
        AND NOT EXISTS(SELECT 1 FROM chat_group_classifications f WHERE f.chat_id=c.chat_id AND f.locked)
        AND NOT EXISTS(SELECT 1 FROM creator_group_jobs j WHERE j.chat_id=c.chat_id AND (
          j.status NOT IN ('completed','failed','cancelled') OR j.reason_code='rules_unavailable'
          OR j.status='completed'
          AND j.stats->>'prompt_version'=ANY(%s::text[])))
        ORDER BY c.chat_id""", (sorted(evidence.COMPATIBLE_PROMPT_VERSIONS),)).fetchall()
    for row in rows:
        conn.execute("SELECT enqueue_creator_group(%s,'history')", (row["chat_id"],))
    _audit(conn, actor, "enqueued", {"queued": len(rows), "prompt_version": evidence.PROMPT_VERSION})
    return {"queued": len(rows)}


def reprocess_unclassified(conn, actor, *, chat_ids=None):
    """Explicit owner rerun: preserve formal decisions, reuse safe paid results, never replay unknown billing."""
    from .config import get_settings

    require_owner(conn, actor)
    config = _config(conn, lock=True)
    if not _enabled(config):
        raise ValueError("请先配置并启用达人群用途匹配。")
    settings = get_settings()
    storage = creator_data.cipher(settings)
    rows = conn.execute("""SELECT j.*,c.chat_id AS chat_id,f.purpose AS confirmed_purpose,
        f.source AS confirmed_source,f.locked AS classification_locked FROM chats c
        LEFT JOIN LATERAL(SELECT * FROM creator_group_jobs WHERE chat_id=c.chat_id
            ORDER BY created_at DESC,id DESC LIMIT 1) j ON TRUE
        LEFT JOIN chat_group_classifications f ON f.chat_id=c.chat_id
        WHERE creator_group_is_eligible(c.chat_id) AND (%s::text[] IS NULL OR c.chat_id=ANY(%s::text[]))
        ORDER BY c.chat_id""", (chat_ids, chat_ids)).fetchall()
    counts = dict.fromkeys(("admitted", "queued", "active", "protected", "request_unknown"), 0)
    for job in rows:
        if (job["confirmed_purpose"] not in (None, "unknown")
                or job["confirmed_source"] == "manual" or job["classification_locked"]):
            counts["protected"] += 1
            continue
        # Manual confirmation locks this same chat first. Recheck after locking, not just the list snapshot.
        conn.execute("SELECT chat_id FROM chats WHERE chat_id=%s FOR UPDATE", (job["chat_id"],)).fetchone()
        formal = conn.execute("SELECT purpose,source,locked FROM chat_group_classifications WHERE chat_id=%s",
                              (job["chat_id"],)).fetchone()
        if formal and (formal["purpose"] != "unknown" or formal["source"] == "manual" or formal["locked"]):
            counts["protected"] += 1
            continue
        if job["reason_code"] == "request_unknown":
            counts["request_unknown"] += 1
            continue
        if job["reason_code"] == "rules_unavailable":
            counts["active"] += 1
            continue
        if job["id"] and job["status"] not in {"completed", "failed", "cancelled", "waiting_evidence"}:
            counts["active"] += 1
            continue
        admitted, result = False, None
        if (job["status"] == "completed" and job["pack_ciphertext"] and job["result_ciphertext"]
                and job["stats"].get("prompt_version") in evidence.COMPATIBLE_PROMPT_VERSIONS
                and job["config_fingerprint"] == _config_hash(config)
                and evidence.still_valid(conn, job["refs"], settings)):
            pack = json.loads(storage.decrypt(job["pack_ciphertext"]))
            try:
                _manual_access(conn, pack)
                result = evidence.validate(_business_pack(pack), json.loads(storage.decrypt(job["result_ciphertext"])))
                admitted = _auto_classify(conn, job, pack, job["refs"], result, config, settings)
            except PermissionError:
                _pause_rules(conn, job)
                counts["active"] += 1
                continue
            except api.ModelOutputError:
                pass  # Do not remove an unsupported citation and reuse its old confidence.
        if admitted or admitted is None:
            conn.execute("""UPDATE creator_group_jobs SET stats=stats||%s::jsonb,
                reason_code=CASE WHEN %s THEN 'auto_linked' ELSE reason_code END,
                not_before=NOW()+INTERVAL '30 seconds',updated_at=NOW() WHERE id=%s""",
                (creator_data.encode(_admission_stats(job, result, admitted)), bool(admitted), job["id"]))
            counts["admitted" if admitted else "active"] += 1
            continue
        queued = conn.execute("SELECT enqueue_creator_group(%s,'history',TRUE) id", (job["chat_id"],)).fetchone()["id"]
        if queued:
            conn.execute("UPDATE creator_group_jobs SET stats=stats||'{\"force_reclassify\":true}'::jsonb "
                         "WHERE id=%s", (queued,))
            counts["queued"] += 1
    _audit(conn, actor, "unclassified_reprocessed", {**counts, "prompt_version": evidence.PROMPT_VERSION})
    return counts


def _recover(conn):
    # Unknown network billing never causes an automatic second request.
    conn.execute("""WITH expired AS (
        SELECT j.id,r.error_code='' AND r.finished_at IS NOT NULL
          AND r.response_ciphertext IS NOT NULL AND r.created_at>=j.prepared_at AS saved
        FROM creator_group_jobs j LEFT JOIN LATERAL (
          SELECT * FROM creator_group_requests WHERE job_id=j.id
          ORDER BY created_at DESC,id DESC LIMIT 1) r ON TRUE
        WHERE j.status IN ('preparing','running') AND j.lease_until<NOW() FOR UPDATE OF j)
        UPDATE creator_group_jobs j SET
        status=CASE WHEN j.status='preparing' THEN 'queued' WHEN expired.saved THEN 'ready' ELSE 'failed' END,
        reason_code=CASE WHEN j.status='preparing' THEN 'lease_expired'
          WHEN expired.saved THEN 'application_pending' ELSE 'request_unknown' END,
        claim_token=NULL,lease_until=NULL,not_before=NOW(),updated_at=NOW()
        FROM expired WHERE j.id=expired.id""")
    conn.execute("""UPDATE creator_group_requests r SET error_code='request_unknown',finished_at=NOW()
        FROM creator_group_jobs j WHERE r.job_id=j.id AND j.reason_code='request_unknown'
        AND r.finished_at IS NULL""")
    conn.execute("""UPDATE creator_group_jobs j SET status='cancelled',desired_version=desired_version+1,
        reason_code='scope_changed',claim_token=NULL,lease_until=NULL,updated_at=NOW()
        WHERE status NOT IN ('completed','failed','cancelled') AND NOT creator_group_is_eligible(j.chat_id)""")


def _invalidate(conn, job, config):
    active = _enabled(config) and conn.execute("SELECT creator_group_is_eligible(%s) eligible",
                                             (job["chat_id"],)).fetchone()["eligible"]
    conn.execute("""UPDATE creator_group_jobs SET status=%s,reason_code=%s,claim_token=NULL,lease_until=NULL,
        source=CASE WHEN status='running' AND EXISTS(SELECT 1 FROM creator_group_requests r
          WHERE r.job_id=creator_group_jobs.id AND r.error_code='request_unknown') THEN 'incremental' ELSE source END,
        result_ciphertext=NULL,not_before=NOW(),updated_at=NOW() WHERE id=%s""",
                 ("queued" if active else "cancelled", "snapshot_changed" if active else "scope_changed", job["id"]))


def prepare_next(settings):
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config):
            return False
        _recover(conn)
        deferred = conn.execute("""SELECT * FROM creator_group_jobs WHERE status='completed'
            AND stats->>'auto_deferred'='true' AND reason_code<>'rules_unavailable' AND not_before<=NOW()
            ORDER BY updated_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if deferred:
            storage = creator_data.cipher(settings)
            admitted = False
            if (deferred["stats"].get("prompt_version") in evidence.COMPATIBLE_PROMPT_VERSIONS
                    and evidence.still_valid(conn, deferred["refs"], settings)):
                pack = json.loads(storage.decrypt(deferred["pack_ciphertext"]))
                try:
                    _manual_access(conn, pack)
                    result = evidence.validate(_business_pack(pack),
                                               json.loads(storage.decrypt(deferred["result_ciphertext"])))
                    admitted = _auto_classify(conn, deferred, pack, deferred["refs"], result, config, settings)
                except PermissionError:
                    _pause_rules(conn, deferred)
                    return True
                except api.ModelOutputError:
                    deferred["admission_reason"] = "reply_context_missing"
            else:
                deferred["admission_reason"] = "evidence_unavailable"
            conn.execute("""UPDATE creator_group_jobs SET stats=stats||%s::jsonb,
                reason_code=CASE WHEN %s THEN 'auto_linked'
                    WHEN reason_code='rules_unavailable' THEN 'suggestions_saved' ELSE reason_code END,
                not_before=NOW()+INTERVAL '30 seconds',updated_at=NOW() WHERE id=%s""",
                (creator_data.encode({"auto_linked": bool(admitted), "auto_deferred": admitted is None,
                                     "admission_reason": deferred.get("admission_reason", "")}),
                 bool(admitted), deferred["id"]))
            return True
        if conn.execute("SELECT count(*) n FROM creator_group_jobs WHERE status='ready'").fetchone()["n"] >= 5:
            return False
        job = conn.execute("""SELECT * FROM creator_group_jobs WHERE status='queued' AND not_before<=NOW()
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return False
        # SQL source triggers can create a newer job beside a completed, locally deferred paid result.
        if not job["pack_ciphertext"] and job["source"] != "manual":
            paid = conn.execute("""SELECT * FROM creator_group_jobs WHERE chat_id=%s AND status='completed'
                AND reason_code='rules_unavailable' AND pack_ciphertext IS NOT NULL
                AND result_ciphertext IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1""",
                (job["chat_id"],)).fetchone()
            if paid and evidence.still_valid(conn, paid["refs"], settings):
                job = conn.execute("""UPDATE creator_group_jobs SET pack_ciphertext=%s,result_ciphertext=%s,
                    refs=%s::jsonb,stats=%s::jsonb,fingerprint=%s,config_fingerprint=%s,
                    snapshot_version=desired_version,reason_code='rules_unavailable',prepared_at=%s,
                    updated_at=NOW() WHERE id=%s RETURNING *""", (
                    paid["pack_ciphertext"], paid["result_ciphertext"], creator_data.encode(paid["refs"]),
                    creator_data.encode(paid["stats"]), paid["fingerprint"], paid["config_fingerprint"],
                    paid["prepared_at"], job["id"])).fetchone()
                conn.execute("""UPDATE creator_group_jobs SET reason_code='superseded',
                    stats=stats||'{"auto_deferred":false,"admission_reason":"superseded"}'::jsonb,
                    updated_at=NOW() WHERE id=%s""", (paid["id"],))
        if job["reason_code"] == "rules_unavailable" and job["pack_ciphertext"]:
            storage = creator_data.cipher(settings)
            frozen = json.loads(storage.decrypt(job["pack_ciphertext"]))
            try:
                _manual_access(conn, frozen)
            except PermissionError:
                _pause_rules(conn, job)
                return True
            if evidence.still_valid(conn, job["refs"], settings):
                if job["result_ciphertext"]:
                    result = evidence.validate(_business_pack(frozen),
                                               json.loads(storage.decrypt(job["result_ciphertext"])))
                    try:
                        admitted = _auto_classify(conn, job, frozen, job["refs"], result, config, settings)
                    except PermissionError:
                        _pause_rules(conn, job)
                        return True
                    conn.execute("""UPDATE creator_group_jobs SET status='completed',
                        reason_code=CASE WHEN %s THEN 'auto_linked' ELSE 'suggestions_saved' END,
                        stats=stats||%s::jsonb,not_before=NOW()+INTERVAL '30 seconds',
                        finished_at=NOW(),updated_at=NOW() WHERE id=%s""",
                        (bool(admitted), creator_data.encode(_admission_stats(job, result, admitted)), job["id"]))
                    return True
                if job["config_fingerprint"] == _config_hash(config):
                    conn.execute("""UPDATE creator_group_jobs SET status='ready',
                        snapshot_version=desired_version,updated_at=NOW() WHERE id=%s""", (job["id"],))
                    return True
        token = uuid4()
        conn.execute("""UPDATE creator_group_jobs SET status='preparing',snapshot_version=desired_version,
            claim_token=%s,lease_until=NOW()+INTERVAL '10 minutes',updated_at=NOW() WHERE id=%s""", (token, job["id"]))
    try:
        with connection() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout='90s'")
            pinned = assistant_rules.group_snapshot(conn, settings)
            assistant_rules.group_validate_access(conn, pinned)
            material = evidence.build(conn, settings, job["chat_id"])
    except PermissionError:
        with connection() as conn, conn.transaction():
            current = conn.execute("SELECT * FROM creator_group_jobs WHERE id=%s FOR UPDATE",
                                   (job["id"],)).fetchone()
            if current and current["status"] == "preparing" and current["claim_token"] == token:
                _pause_rules(conn, current, keep_material=False)
        return True
    except Exception:
        with connection() as conn, conn.transaction():
            conn.execute("""UPDATE creator_group_jobs
                SET status=CASE WHEN desired_version=%s THEN 'failed' ELSE 'queued' END,
                reason_code='preparation_failed',claim_token=NULL,lease_until=NULL,updated_at=NOW()
                WHERE id=%s AND status='preparing' AND claim_token=%s""", (job["desired_version"], job["id"], token))
        raise
    with connection() as conn, conn.transaction():
        current_config = _config(conn, lock=True)
        current = conn.execute("SELECT * FROM creator_group_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
        if not current or current["status"] != "preparing" or current["claim_token"] != token:
            return True
        pack, refs = material["pack"], material["refs"]
        if (current["desired_version"] != job["desired_version"] or not _enabled(current_config)
                or _config_hash(config) != _config_hash(current_config)
                or pack and not evidence.still_valid(conn, refs, settings)):
            _invalidate(conn, current, current_config)
            return True
        try:
            assistant_rules.group_validate_access(conn, pinned)
        except PermissionError:
            _pause_rules(conn, current, keep_material=False)
            return True
        fingerprint = _hash([material["fingerprint"], config["provider"], config["project_id"],
                             config["model"], evidence.CLASSIFICATION_VERSION, pinned["digest"]])
        cached = conn.execute("""SELECT result_ciphertext FROM creator_group_jobs WHERE chat_id=%s AND fingerprint=%s
            AND status='completed' AND result_ciphertext IS NOT NULL ORDER BY updated_at DESC LIMIT 1""",
                              (job["chat_id"], fingerprint)).fetchone() if (
                                  pack and job["source"] != "manual"
                                  and not job["stats"].get("force_reclassify")) else None
        storage = creator_data.cipher(settings)
        if pack and job["source"] != "manual" and not cached:
            unknown = conn.execute("""SELECT 1 FROM creator_group_requests r JOIN creator_group_jobs j ON j.id=r.job_id
                WHERE j.chat_id=%s AND r.material_fingerprint=%s
                AND (r.error_code='request_unknown' OR r.finished_at IS NULL) LIMIT 1""",
                (job["chat_id"], _hash(pack))).fetchone()
            if not unknown:
                prior = conn.execute("""SELECT pack_ciphertext FROM creator_group_jobs WHERE chat_id=%s
                    AND status='failed' AND reason_code='request_unknown' AND pack_ciphertext IS NOT NULL""",
                    (job["chat_id"],)).fetchall()
                unknown = any(_business_pack(json.loads(storage.decrypt(row["pack_ciphertext"]))) == pack
                              for row in prior)
            if unknown:
                conn.execute("""UPDATE creator_group_jobs SET status='failed',reason_code='request_unknown',
                    claim_token=NULL,lease_until=NULL,finished_at=NOW(),updated_at=NOW() WHERE id=%s""", (job["id"],))
                return True
        try:
            result = (evidence.validate(pack, json.loads(storage.decrypt(cached["result_ciphertext"])))
                      if cached else None)
        except api.ModelOutputError:
            cached, result = None, None
        frozen = {**pack, "_rules": pinned} if pack else None
        auto_linked = _auto_classify(conn, current, frozen, refs, result, current_config, settings) if cached else False
        conn.execute("""UPDATE creator_group_jobs SET status=%s,reason_code=%s,fingerprint=%s,config_fingerprint=%s,
            pack_ciphertext=%s,result_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,attempts=0,
            claim_token=NULL,lease_until=NULL,prepared_at=NOW(),finished_at=CASE WHEN %s THEN NOW() ELSE NULL END,
            updated_at=NOW() WHERE id=%s""", (
                "completed" if cached else "ready" if pack else "waiting_evidence",
                "cached" if cached else material["reason"], fingerprint, _config_hash(current_config),
                storage.encrypt(creator_data.encode(frozen)) if pack else None,
                storage.encrypt(creator_data.encode(result)) if cached else None,
                creator_data.encode(refs), creator_data.encode({
                    **material["stats"], "prompt_version": evidence.PROMPT_VERSION,
                    "rules_digest": pinned["digest"], "rules_protocol": pinned["protocol"],
                    **({"force_reclassify": True} if job["stats"].get("force_reclassify") else {}),
                    **(_admission_stats(current, result, auto_linked) if cached else {}),
                }),
                bool(cached), job["id"],
            ))
    return True


def claim():
    from .config import get_settings

    settings = get_settings()
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config):
            return None
        can_send = ai_dispatch.can_claim(conn)
        _recover(conn)
        job = conn.execute("""SELECT * FROM creator_group_jobs WHERE status IN ('ready','retry_wait')
            AND not_before<=NOW() AND (attempts<3 OR reason_code IN ('application_pending','rules_unavailable'))
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return None
        if (job["stats"].get("prompt_version") != evidence.PROMPT_VERSION
                or job["desired_version"] != job["snapshot_version"]
                or job["config_fingerprint"] != _config_hash(config)
                or not evidence.still_valid(conn, job["refs"], settings)):
            _invalidate(conn, job, config)
            return None
        token, reservation = uuid4(), uuid4()
        frozen = json.loads(creator_data.cipher(settings).decrypt(job["pack_ciphertext"]))
        try:
            _manual_access(conn, frozen)
        except PermissionError:
            _pause_rules(conn, job)
            return None
        pack = _business_pack(frozen)
        receipt = conn.execute("""SELECT * FROM creator_group_requests WHERE job_id=%s
            AND created_at>=%s ORDER BY created_at DESC,id DESC LIMIT 1""",
            (job['id'], job['prepared_at'])).fetchone() if job['reason_code'] in {
                'application_pending', 'rules_unavailable'} else None
        saved = bool(receipt and receipt['error_code'] == '' and receipt['finished_at']
                     and receipt['response_ciphertext'] and receipt['material_fingerprint'] == _hash(pack))
        if (job['reason_code'] == 'application_pending' and not saved
                or receipt and receipt['error_code'] == 'request_unknown'):
            conn.execute("UPDATE creator_group_jobs SET status='failed',reason_code='request_unknown',"
                         "updated_at=NOW() WHERE id=%s", (job['id'],))
            return None
        blocked_error = (receipt and receipt['error_code'] and job['reason_code'] == 'rules_unavailable'
                         and receipt['error_code'] not in {'not_sent'} | {
                             f'http_{status}' for status in api.RETRYABLE_HTTP_STATUSES})
        if not saved and (job['attempts'] >= 3 or blocked_error):
            reason = 'invalid_output' if receipt and receipt['error_code'] == 'invalid_output' else 'provider_failed'
            conn.execute("UPDATE creator_group_jobs SET status='failed',reason_code=%s,"
                         "updated_at=NOW() WHERE id=%s", (reason, job['id']))
            return None
        if not saved and not can_send:
            return None
        if saved:
            try:
                result = evidence.validate(pack, json.loads(creator_data.cipher(settings).decrypt(
                    receipt['response_ciphertext'])))
            except (KeyError, TypeError, ValueError, InvalidTag):
                conn.execute("UPDATE creator_group_jobs SET status='failed',reason_code='invalid_output',"
                             "updated_at=NOW() WHERE id=%s", (job['id'],))
                return None
        else:
            conn.execute("INSERT INTO creator_group_requests(id,job_id,material_fingerprint) VALUES(%s,%s,%s)",
                         (reservation, job["id"], _hash(pack)))
        conn.execute("""UPDATE creator_group_jobs SET status='running',claim_token=%s,
            lease_until=NOW()+INTERVAL '180 seconds',attempts=attempts+%s,updated_at=NOW() WHERE id=%s""",
                     (token, 0 if saved else 1, job["id"]))
        return {**job, "pack": pack, **({"_rules": frozen["_rules"]} if "_rules" in frozen else {}),
                "claim_token": token, "reservation_id": receipt['id'] if saved else reservation,
                **({'saved_response': (result, {'response_id': receipt['response_id'],
                                              'openai_request_id': receipt['request_id']})} if saved else {}),
                "attempts": job["attempts"] + (0 if saved else 1), "config": config}


def _current(conn, job, settings, *, check_rules=True):
    config = _config(conn, lock=True)
    current = conn.execute("SELECT * FROM creator_group_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    if not current or current["status"] != "running" or current["claim_token"] != job["claim_token"]:
        return None
    if (current["desired_version"] != job["snapshot_version"] or not _enabled(config)
            or _config_hash(config) != job["config_fingerprint"]
            or not evidence.still_valid(conn, current["refs"], settings)):
        _invalidate(conn, current, config)
        return None
    if check_rules:
        try:
            _manual_access(conn, job)
        except PermissionError:
            _pause_rules(conn, current)
            return None
    return current


def run(job, settings):
    with connection() as conn, conn.transaction():
        if not _current(conn, job, settings):
            if 'saved_response' not in job:
                conn.execute("""UPDATE creator_group_requests SET error_code='not_sent',input_tokens=0,output_tokens=0,
                    finished_at=NOW() WHERE id=%s AND finished_at IS NULL""", (job["reservation_id"],))
                conn.execute("UPDATE creator_group_jobs SET attempts=GREATEST(attempts-1,0) "
                             "WHERE id=%s AND reason_code='rules_unavailable'", (job["id"],))
            return
    result, telemetry, error = None, {}, None
    if 'saved_response' in job:
        result, telemetry = job['saved_response']
    try:
        if 'saved_response' not in job:
            key = ai_feature_config.api_key_cipher(settings.token_encryption_key).decrypt(
                job["config"]["api_key_ciphertext"])
            instructions = (assistant_rules.group_instructions(job["_rules"])
                            if "_rules" in job else evidence.instructions())
            response = api.call_openai(job["config"], key, job["pack"], job["id"], instructions=instructions,
                                       output_format=evidence.output_format(job["pack"]), max_output_tokens=900,
                                       reasoning_effort="none", total_timeout=70)
            telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
            result = evidence.validate(job["pack"], json.loads(response["text"]))
    except Exception as exc:
        error = (api.ModelOutputError("invalid_json", telemetry=telemetry)
                 if isinstance(exc, json.JSONDecodeError) else exc)
        telemetry = getattr(error, "telemetry", {}) or telemetry
    status = telemetry.get("http_status")
    unknown = isinstance(error, api.OpenAIRequestError) and error.request_unknown
    code = ("" if error is None else "request_unknown" if unknown else "invalid_output"
            if isinstance(error, api.ModelOutputError) else f"http_{status}"
            if isinstance(status, int) else "provider_failed")
    if 'saved_response' not in job:
        with connection() as conn, conn.transaction():
            usage = telemetry.get("usage", {})
            conn.execute("""UPDATE creator_group_requests SET request_id=%s,response_id=%s,error_code=%s,
                input_tokens=%s,output_tokens=%s,response_ciphertext=%s,finished_at=NOW() WHERE id=%s""", (
                    telemetry.get("openai_request_id"), telemetry.get("response_id"), code,
                    usage.get("input_tokens"), usage.get("output_tokens"),
                    creator_data.cipher(settings).encrypt(creator_data.encode(result)) if result is not None else None,
                    job["reservation_id"],
                ))
            conn.execute("""UPDATE creator_group_jobs SET input_tokens=input_tokens+%s,
                output_tokens=output_tokens+%s WHERE id=%s""",
                         (usage.get("input_tokens") or 0, usage.get("output_tokens") or 0, job["id"]))
        # Actual usage commits before any subsequent authorization or admission failure.
        ai_dispatch.observe_limits(telemetry)
    retry = isinstance(error, api.OpenAIRequestError) and error.retryable and not unknown and job["attempts"] < 3
    delay = api.retry_delay(error.retry_after_seconds, job["attempts"]) if retry else 0
    if isinstance(error, api.OpenAIRequestError):
        if error.is_quota:
            ai_dispatch.pause_for_quota()
        elif retry:
            ai_dispatch.throttle(delay)
    with connection() as conn, conn.transaction():
        current = _current(conn, job, settings, check_rules=False)
        if not current:
            return
        try:
            frozen = {**job["pack"], **({"_rules": job["_rules"]} if "_rules" in job else {})}
            auto_linked = _auto_classify(
                conn, current, frozen, current["refs"], result, job["config"], settings) if error is None else False
        except PermissionError:
            conn.execute("""UPDATE creator_group_jobs SET result_ciphertext=%s,
                stats=stats||%s::jsonb,updated_at=NOW() WHERE id=%s""", (
                creator_data.cipher(settings).encrypt(creator_data.encode(result)),
                creator_data.encode({"suggested_purpose": result["purpose"], "auto_linked": False,
                                     "auto_deferred": False, "admission_reason": "rules_unavailable"}), job["id"]))
            _pause_rules(conn, current)
            return
        reason = ("suggestions_saved" if error is None else "request_unknown" if unknown else "quota_paused"
                  if getattr(error, "is_quota", False) else "provider_retry" if retry else "invalid_output"
                  if isinstance(error, api.ModelOutputError) else "provider_failed")
        if auto_linked:
            reason = "auto_linked"
        conn.execute("""UPDATE creator_group_jobs
            SET status=%s,reason_code=%s,result_ciphertext=%s,stats=stats||%s::jsonb,
            claim_token=NULL,lease_until=NULL,not_before=NOW()+(%s*INTERVAL '1 second'),
            finished_at=CASE WHEN %s THEN NOW() ELSE NULL END,updated_at=NOW() WHERE id=%s""", (
                "completed" if error is None else "retry_wait" if retry else "failed", reason,
                creator_data.cipher(settings).encrypt(creator_data.encode(result)) if error is None else None,
                creator_data.encode(_admission_stats(current, result, auto_linked) if error is None else {}),
                delay, not retry, job["id"],
            ))
        conn.execute("UPDATE ai_feature_configs SET last_tested_at=NOW(),last_test_status=%s WHERE feature=%s",
                     ("failed" if error else "succeeded", FEATURE))


def _result(conn, job, settings, *, lightweight=False):
    if not job["result_ciphertext"]:
        return None
    if not evidence.still_valid(conn, job["refs"], settings, lightweight=lightweight):
        return None
    return _decode_result(job, settings)


def _decode_result(job, settings):
    """Only called after current material is checked; formal directory rows need no model payload."""
    if not job["result_ciphertext"]:
        return None
    result = json.loads(creator_data.cipher(settings).decrypt(job["result_ciphertext"]))
    result.pop("reason", None)
    return {**result, "purpose_label": PURPOSES[result["purpose"]], "confidence_label": f"{result['confidence']:.0%}"}


def _diagnostics(job, result, config, *, pack=None):
    stats = job["stats"]
    reason = stats.get("admission_reason", "")
    missing = stats.get("missing_reply_ids", [])
    if job.get("stale"):
        reason = "evidence_unavailable"
    elif result and not reason:
        if pack is not None:
            reason = _admission_reason(pack, result, config)
        elif result["purpose"] == "unknown":
            reason = "unknown_purpose"
        elif Decimal(str(result["confidence"])) < Decimal(str(config["auto_confirm_threshold"])):
            reason = "low_confidence"
        elif set(result["refs"]).intersection(missing):
            reason = "reply_context_missing"
        elif not any(ref.startswith("M") for ref in result["refs"]):
            reason = "no_message_reference"
    if not reason and job["status"] in {"failed", "waiting_evidence", "cancelled"}:
        reason = job["reason_code"]
    return {"admission_reason_label": REASONS.get(reason, "") if reason else "",
            "admission_details": {"missing_reply_ids": missing,
                "missing_reply_reasons": {label: code for label, code in
                    stats.get("missing_reply_reasons", {}).items() if label in missing}}}


def page_data(conn, actor, settings, page=1):
    access = admin_permissions.require_page(conn, actor, "ai")
    config = _config(conn)
    latest = ("WITH latest AS (SELECT DISTINCT ON(chat_id) * FROM creator_group_jobs "
              "ORDER BY chat_id,created_at DESC,id DESC) ")
    counts = {r["status"]: r["n"] for r in conn.execute(
        latest + "SELECT status,count(*) n FROM latest GROUP BY status").fetchall()}
    summary_counts = {r["state"]: r["n"] for r in conn.execute(latest + """SELECT
        CASE WHEN j.status='failed' THEN 'error'
             WHEN j.status IN ('queued','preparing','ready','running','retry_wait')
               OR j.stats->>'auto_deferred'='true' THEN 'pending'
             WHEN f.purpose IS NOT NULL AND f.purpose<>'unknown' THEN 'classified' ELSE 'review' END state,count(*) n
        FROM latest j LEFT JOIN chat_group_classifications f ON f.chat_id=j.chat_id GROUP BY state""").fetchall()}
    rows = conn.execute(latest + """SELECT j.id,j.chat_id,j.status,j.reason_code,j.stats,j.result_ciphertext,j.refs,
        j.source,j.input_tokens,j.output_tokens,j.created_at,j.updated_at,
        c.name,c.chat_mode,f.purpose AS confirmed_purpose,f.tags AS confirmed_tags,
        f.locked,f.source AS confirmed_source,f.confidence AS confirmed_confidence,f.source_job_id
        FROM latest j JOIN chats c ON c.chat_id=j.chat_id
        LEFT JOIN chat_group_classifications f ON f.chat_id=j.chat_id
        ORDER BY j.updated_at DESC,j.id LIMIT 26 OFFSET %s""", ((page - 1) * 25,)).fetchall()
    more, rows = len(rows) > 25, rows[:25]
    viewed = []
    for row in rows:
        row.update(status_label=STATUSES[row["status"]], reason=REASONS.get(row["reason_code"], row["reason_code"]),
                   confirmed_label=PURPOSES.get(row["confirmed_purpose"]),
                   auto_linked=row["confirmed_source"] == "ai" and row["source_job_id"] == row["id"],
                   outdated=row["stats"].get("prompt_version") not in evidence.COMPATIBLE_PROMPT_VERSIONS)
        classified = row["confirmed_purpose"] not in (None, "unknown")
        row["has_evidence"] = bool(row["result_ciphertext"])
        # A stored classification is a business fact, not a request to reopen its chat evidence on every page.
        row["result"] = None if classified else _result(conn, row, settings, lightweight=True)
        row["stale"] = bool(not classified and row["result_ciphertext"] and row["result"] is None)
        result = row["result"]
        purpose = row["confirmed_purpose"] if classified else result["purpose"] if result else "unknown"
        confidence = row["confirmed_confidence"] if classified else result["confidence"] if result else None
        state = ("error" if row["status"] == "failed" else "pending"
                 if row["status"] in {"queued", "preparing", "ready", "running", "retry_wait"}
                 or row["stats"].get("auto_deferred") else "classified" if classified else "review")
        row.update(display_purpose=purpose, display_label=PURPOSES[purpose], display_state=state,
                   display_confidence=f"{confidence:.0%}" if confidence is not None else "—",
                   display_status=("人工分类" if row["confirmed_source"] == "manual" else "已入库")
                       if state == "classified" else "入库中" if row["stats"].get("auto_deferred")
                       else "等待证据" if row["status"] == "waiting_evidence"
                       else {"error": "失败", "pending": "处理中", "review": "待核实"}[state])
        row.update(_diagnostics(row, result, config) if state in {"review", "error"} else
                   {"admission_reason_label": "", "admission_details": {}})
        if row["result"]:
            viewed.append(str(row["id"]))
        for key in ("pack_ciphertext", "result_ciphertext", "refs", "config_fingerprint", "claim_token"):
            row.pop(key, None)
    if viewed:
        _audit(conn, actor, "results_viewed", {"jobs": viewed})
    threshold = config["auto_confirm_threshold"]
    return {"jobs": rows, "counts": counts, "summary_counts": summary_counts, "page": page, "has_more": more,
            "enabled": _enabled(config), "purposes": PURPOSES, "prompt_version": evidence.PROMPT_VERSION,
            "threshold": float(threshold), "threshold_label": f"{threshold:.0%}", "admin_access": access}


def task_evidence(conn, actor, settings, job_id):
    require_owner(conn, actor)
    job = conn.execute("SELECT j.*,c.name FROM creator_group_jobs j JOIN chats c USING(chat_id) WHERE j.id=%s",
                       (job_id,)).fetchone()
    if not job:
        raise LookupError("任务不存在。")
    if not job["pack_ciphertext"] or not evidence.still_valid(conn, job["refs"], settings):
        raise PermissionError("材料或授权已变化，请重新分析。")
    pack = _business_pack(json.loads(creator_data.cipher(settings).decrypt(job["pack_ciphertext"])))
    _audit(conn, actor, "evidence_viewed", {"job_id": str(job_id), "messages": len(pack["messages"])})
    result = _decode_result(job, settings)
    return {"job_id": str(job_id), "chat_id": job["chat_id"], "name": job["name"], "pack": pack,
            "result": result, "stats": job["stats"], **_diagnostics(job, result, _config(conn), pack=pack)}


def action(conn, actor, job_id, operation):
    require_owner(conn, actor)
    job = conn.execute("SELECT * FROM creator_group_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not job:
        raise LookupError("任务不存在。")
    if operation == "cancel":
        conn.execute("""UPDATE creator_group_jobs SET status='cancelled',desired_version=desired_version+1,
            reason_code='owner_cancelled',claim_token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=%s""", (job_id,))
    elif operation in {"retry", "recheck"}:
        if job["reason_code"] == "rules_unavailable":
            raise ValueError("请先恢复群用途分类手册；原任务和已返回结果会继续使用。")
        if not _enabled(_config(conn)):
            raise ValueError("请先配置并启用达人群用途匹配。")
        if not conn.execute("SELECT enqueue_creator_group(%s,'manual',TRUE) id", (job["chat_id"],)).fetchone()["id"]:
            raise PermissionError("该群不在当前授权处理范围。")
    else:
        raise ValueError("操作无效。")
    _audit(conn, actor, "job_" + operation, {"job_id": str(job_id)})


def confirm(conn, actor, chat_id, purpose, tags, *, locked=True, job_id=None):
    require_owner(conn, actor)
    if purpose not in PURPOSES:
        raise ValueError("群用途无效。")
    if not isinstance(tags, list) or len(tags) > 8 or any(
            not isinstance(tag, str) or not tag.strip() or len(tag) > 80 or "\x00" in tag for tag in tags):
        raise ValueError("最多填写 8 个标签，每个不超过 80 个字符。")
    tags = list(dict.fromkeys(tag.strip() for tag in tags))
    if not conn.execute("SELECT chat_id FROM chats WHERE chat_id=%s FOR UPDATE", (chat_id,)).fetchone():
        raise LookupError("群聊不存在。")
    if not conn.execute("SELECT creator_group_is_eligible(%s) eligible", (chat_id,)).fetchone()["eligible"]:
        raise PermissionError("该群不在当前有效的达人群授权范围。")
    if job_id is not None:
        from .config import get_settings

        job = conn.execute("SELECT * FROM creator_group_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
        if (not job or job["chat_id"] != chat_id or job["status"] != "completed" or not job["result_ciphertext"]
                or not evidence.still_valid(conn, job["refs"], get_settings())):
            raise PermissionError("这份建议的材料或授权已经变化，请重新分析或独立人工分类。")
    conn.execute("""INSERT INTO chat_group_classifications
        (chat_id,purpose,tags,locked,source_job_id,confirmed_by_authorization_id,source,confidence)
        VALUES(%s,%s,%s::jsonb,%s,%s,%s,'manual',NULL) ON CONFLICT(chat_id) DO UPDATE SET
        purpose=EXCLUDED.purpose,tags=EXCLUDED.tags,locked=EXCLUDED.locked,source_job_id=EXCLUDED.source_job_id,
        source='manual',confidence=NULL,
        confirmed_by_authorization_id=EXCLUDED.confirmed_by_authorization_id,confirmed_at=NOW(),
        revision=chat_group_classifications.revision+1,updated_at=NOW()""",
                 (chat_id, purpose, creator_data.encode(tags), locked, job_id, actor))
    if locked:
        conn.execute("""UPDATE creator_group_jobs SET status='cancelled',desired_version=desired_version+1,
            reason_code='manual_confirmed',claim_token=NULL,lease_until=NULL,updated_at=NOW()
            WHERE chat_id=%s AND status NOT IN ('completed','failed','cancelled')""", (chat_id,))
    _audit(conn, actor, "classification_confirmed", {"chat_id": chat_id, "purpose": purpose, "locked": locked,
                                                  "job_id": str(job_id) if job_id else None})
