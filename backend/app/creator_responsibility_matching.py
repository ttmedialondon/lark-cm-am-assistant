"""One creator, one shared evidence request, many CM suggestions; never assigns a CM."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from . import ai_dispatch, ai_feature_config, creator_data
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
    "no_candidates": "没有找到内部负责人候选",
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
    "request_unknown": "请求结果未知，需手动复查",
    "cached": "复用相同材料的结果",
    "suggestions_saved": "建议已保存",
    "failure_cached": "相同材料上次调用失败",
}


def _hash(value):
    return hashlib.sha256(creator_data.encode(value).encode()).hexdigest()


def _config(conn, *, lock=False, feature=FEATURE):
    return conn.execute(
        "SELECT provider,project_id,model,enabled,api_key_ciphertext FROM ai_feature_configs "
        "WHERE feature=%s" + (" FOR UPDATE" if lock else ""),
        (feature,),
    ).fetchone()


def _enabled(config):
    return bool(config and config["enabled"] and config["api_key_ciphertext"] and config["provider"] == "openai")


def _config_hash(config):
    return _hash(
        {
            **{k: config[k] for k in ("provider", "project_id", "model", "api_key_ciphertext")},
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
        not_before=NOW(),updated_at=NOW() WHERE status NOT IN ('completed','failed','cancelled')""")


def enqueue(conn, actor, contact_ids=None, limit=5, all_contacts=False):
    require_owner(conn, actor)
    if not _enabled(_config(conn)):
        raise ValueError("请先配置并启用达人负责人建议。")
    if not all_contacts and not 1 <= int(limit) <= 10:
        raise ValueError("小样本数量须为 1–10。")
    ids = list(dict.fromkeys(UUID(str(cid)) for cid in contact_ids)) if contact_ids else None
    if ids and len(ids) > 10:
        raise ValueError("一次最多指定 10 位达人。")
    rows = conn.execute(
        """SELECT DISTINCT r.contact_id FROM contact_roles r
        WHERE r.role='creator' AND r.status='confirmed' AND (%s::uuid[] IS NULL OR r.contact_id=ANY(%s::uuid[]))
        AND NOT EXISTS(SELECT 1 FROM creator_responsibility_jobs j WHERE j.contact_id=r.contact_id
          AND (j.status NOT IN ('completed','failed','cancelled') OR j.status='completed'
            AND j.stats->>'prompt_version'=%s)) ORDER BY r.contact_id LIMIT %s""",
        (ids, ids, evidence.PROMPT_VERSION, None if all_contacts or ids else int(limit)),
    ).fetchall()
    if ids and len(
        conn.execute(
            "SELECT DISTINCT contact_id FROM contact_roles WHERE contact_id=ANY(%s::uuid[]) "
            "AND role='creator' AND status='confirmed'",
            (ids,),
        ).fetchall()
    ) != len(ids):
        raise ValueError("只能选择已确认达人。")
    for row in rows:
        conn.execute(
            "SELECT enqueue_creator_responsibility(%s,%s)", (row["contact_id"], "history" if all_contacts else "manual")
        )
    counts = {"queued": len(rows), "skipped": len(ids) - len(rows) if ids else 0}
    _audit(
        conn,
        actor,
        "creator_responsibility_enqueued",
        {**counts, "all_contacts": all_contacts, "prompt_version": evidence.PROMPT_VERSION},
    )
    return counts


def _recover(conn):
    conn.execute("""UPDATE creator_responsibility_jobs SET status=CASE
        WHEN status='preparing' AND source<>'sample' THEN 'queued' ELSE 'failed' END,
        reason_code=CASE WHEN status='running' THEN 'request_unknown' ELSE 'lease_expired' END,
        claim_token=NULL,lease_until=NULL,not_before=NOW(),updated_at=NOW()
        WHERE status IN ('running','preparing') AND lease_until<NOW()""")
    conn.execute("""UPDATE creator_responsibility_jobs j SET status='cancelled',desired_version=desired_version+1,
        reason_code='identity_removed',claim_token=NULL,lease_until=NULL,updated_at=NOW()
        WHERE status NOT IN ('completed','failed','cancelled') AND NOT EXISTS(
          SELECT 1 FROM contact_roles r WHERE r.contact_id=j.contact_id
            AND r.role='creator' AND r.status='confirmed')""")


def _invalidate(conn, job):
    conn.execute(
        """UPDATE creator_responsibility_jobs SET status='queued',reason_code='snapshot_changed',
        claim_token=NULL,lease_until=NULL,result_ciphertext=NULL,not_before=NOW(),updated_at=NOW() WHERE id=%s""",
        (job["id"],),
    )


def _store_prepared(conn, job, material, config, settings):
    # Rotation fences an in-flight request, but does not invalidate a semantic result cache.
    fingerprint = _hash(
        [material["fingerprint"], config["provider"], config["project_id"], config["model"], evidence.PROMPT_VERSION]
    )
    pack = material["pack"]
    stats = {**material["stats"], "prompt_version": evidence.PROMPT_VERSION}
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
    if cache:
        cached = json.loads(storage.decrypt(cache["result_ciphertext"]))
        cached["result"] = evidence.validate(pack, cached["result"])
        cached["candidate_ids"] = material["candidate_ids"]
        cache["result_ciphertext"] = storage.encrypt(creator_data.encode(cached))
    conn.execute(
        """UPDATE creator_responsibility_jobs SET status=%s,reason_code=%s,
        fingerprint=%s,config_fingerprint=%s,pack_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,
        result_ciphertext=%s,attempts=0,claim_token=NULL,lease_until=NULL,prepared_at=NOW(),updated_at=NOW(),
        finished_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s""",
        (
            "completed" if cache else "ready" if pack else "waiting_evidence",
            "cached" if cache else material["reason"],
            fingerprint,
            _config_hash(config),
            storage.encrypt(creator_data.encode({"pack": pack, "candidate_ids": material["candidate_ids"]}))
            if pack
            else None,
            creator_data.encode(material["refs"]),
            creator_data.encode(stats),
            cache["result_ciphertext"] if cache else None,
            bool(cache),
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
            material = evidence.build(conn, settings, job["contact_id"])
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
        token, reservation = uuid4(), uuid4()
        conn.execute(
            "INSERT INTO creator_responsibility_requests(id,job_id,key_feature) VALUES(%s,%s,%s)",
            (reservation, job["id"], FEATURE),
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
    config = _config(conn, lock=True, feature=job.get("key_feature", FEATURE))
    row = conn.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    if not row or row["status"] != "running" or row["claim_token"] != job["claim_token"]:
        return None
    enabled = bool(config and config["api_key_ciphertext"]) if job.get("sample_only") else _enabled(config)
    if (
        row["desired_version"] != job["snapshot_version"]
        or not enabled
        or _config_hash(config) != job["config_fingerprint"]
    ):
        _invalidate(conn, row)
        return None
    if job.get("actor_id"):
        try:
            require_owner(conn, job["actor_id"])
        except PermissionError:
            conn.execute(
                "UPDATE creator_responsibility_jobs SET status='cancelled',claim_token=NULL,lease_until=NULL,"
                "reason_code='owner_cancelled',updated_at=NOW() WHERE id=%s",
                (job["id"],),
            )
            return None
    return row


def _error_code(error):
    if error is None:
        return ""
    if isinstance(error, api.ModelOutputError):
        return "invalid_output"
    if isinstance(error, api.OpenAIRequestError):
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
            pack,
            contact_id,
            instructions=evidence.instructions(),
            output_format=evidence.output_format(pack),
            max_output_tokens=min(12000, 1000 + 400 * len(pack["candidates"])),
            reasoning_effort="none",
            total_timeout=70,
        )
        telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
        parsed = json.loads(response["text"])
        result = evidence.validate(pack, parsed)
        # Only canonical, bounded result fields; no raw returned text in plaintext logs.
        diagnostic = {"validated": result}
    except Exception as exc:
        error = exc
        telemetry = getattr(exc, "telemetry", {}) or telemetry
        if isinstance(exc, json.JSONDecodeError | TypeError):
            error = api.ModelOutputError("invalid_json", telemetry=telemetry)
    return result, telemetry, error, diagnostic


def run(job, settings):
    with connection() as conn, conn.transaction():
        current = _current(conn, job)
        if not current:
            _no_request(conn, job)
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            _no_request(conn, job)
            return
    result, telemetry, error, diagnostic = review(job["pack"], job["config"], job["contact_id"], settings)
    with connection() as conn, conn.transaction():
        _config(conn, lock=True, feature=job.get("key_feature", FEATURE))
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
    if isinstance(error, api.OpenAIRequestError):
        if error.is_quota:
            ai_dispatch.pause_for_quota()
        elif error.retryable:
            ai_dispatch.throttle(api.retry_delay(error.retry_after_seconds, job["attempts"]))
    with connection() as conn, conn.transaction():
        current = _current(conn, job)
        if not current:
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            return
        if error:
            retry = (
                isinstance(error, api.OpenAIRequestError)
                and error.retryable
                and job["attempts"] < 3
                and not job.get("sample_only")
            )
            reason = (
                "quota_paused"
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
                    api.retry_delay(getattr(error, "retry_after_seconds", None), job["attempts"]) if retry else 0,
                    job["id"],
                ),
            )
        else:
            view = {"candidate_ids": job["candidate_ids"], "result": result}
            conn.execute(
                """UPDATE creator_responsibility_jobs SET status='completed',reason_code='suggestions_saved',
                result_ciphertext=%s,claim_token=NULL,lease_until=NULL,
                finished_at=NOW(),updated_at=NOW() WHERE id=%s""",
                (creator_data.cipher(settings).encrypt(creator_data.encode(view)), job["id"]),
            )
        if not job.get("sample_only"):
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
            """SELECT DISTINCT s.contact_id,a.id authorization_id,s.level,s.handle,
        COALESCE(c.display_name,a.display_name,'未命名负责人') name,i.contact_id cm_contact_id
        FROM (SELECT contact_id,responsible_authorization_id,'person' level,NULL::text handle
          FROM contact_assignments WHERE ended_at IS NULL
          UNION SELECT x.contact_id,x.responsible_authorization_id,'account',p.normalized_handle
          FROM creator_account_assignments x LEFT JOIN social_accounts p ON p.id=x.social_account_id
          WHERE x.ended_at IS NULL) s
        JOIN authorizations a ON a.id=s.responsible_authorization_id
        LEFT JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
        LEFT JOIN contacts c ON c.id=i.contact_id WHERE s.contact_id=ANY(%s::uuid[]) ORDER BY s.contact_id,name,a.id""",
            (list(contact_ids),),
        ).fetchall()
        if contact_ids
        else []
    )
    grouped = {}
    for row in rows:
        row["scope_label"] = (
            "达人本人" if row["level"] == "person" else "@" + row["handle"] if row["handle"] else "账号级关系"
        )
        grouped.setdefault(str(row["contact_id"]), []).append(row)
    return grouped


def _results(conn, view, existing=()):
    ids = list(view["candidate_ids"].values())
    ids = [str(cid) for cid in ids]
    people = {
        str(r["id"]): r
        for r in conn.execute(
            """SELECT c.id,c.display_name,EXISTS(
        SELECT 1 FROM lark_identities i JOIN authorizations a ON a.open_id=i.open_id
        WHERE i.contact_id=c.id AND a.status='active' AND a.body_consent_at IS NOT NULL) authorized
        FROM contacts c WHERE c.id=ANY(%s::uuid[])""",
            (ids,),
        ).fetchall()
    }
    results = []
    known = {str(row["cm_contact_id"]) for row in existing if row["cm_contact_id"]}
    for suggestion in view["result"]["suggestions"]:
        person_id = str(view["candidate_ids"][suggestion["cm"]])
        person = people.get(person_id, {})
        results.append(
            {
                **suggestion,
                "name": person.get("display_name") or suggestion["cm"],
                "authorized": person.get("authorized", False),
                "existing": person_id in known,
                "label": DECISIONS[suggestion["decision"]],
                "scope_codes": suggestion["scope"],
                "scope": "、".join(SCOPES[scope] for scope in suggestion["scope"]),
                "period": PERIODS[suggestion["period"]],
            }
        )
    return results


def page_data(conn, actor, settings, page=1):
    require_owner(conn, actor)
    _recover(conn)
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
        j.result_ciphertext,c.display_name FROM latest j JOIN contacts c ON c.id=j.contact_id
        ORDER BY j.updated_at DESC,j.id LIMIT 26 OFFSET %s""",
        ((page - 1) * 25,),
    ).fetchall()
    has_more = len(jobs) > 25
    jobs = jobs[:25]
    existing = _existing_assignments(conn, [job["contact_id"] for job in jobs])
    for job in jobs:
        job.update(
            name=job.pop("display_name"),
            status_label=STATUSES[job["status"]],
            reason=REASONS.get(job["reason_code"], job["reason_code"]),
            candidate_count=job["stats"].get("candidate_count", 0),
            results=[],
        )
        job["existing_assignments"] = existing.get(str(job["contact_id"]), [])
        ciphertext = job.pop("result_ciphertext")
        if ciphertext:
            refs = conn.execute("SELECT refs FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)).fetchone()[
                "refs"
            ]
            if evidence.still_valid(conn, refs, settings, lightweight=True):
                job["results"] = _results(
                    conn, json.loads(creator_data.cipher(settings).decrypt(ciphertext)), job["existing_assignments"]
                )
            else:
                job.update(reason="材料或授权已变化；旧建议不可查看", stale=True)
    if any(job["results"] for job in jobs):
        _audit(
            conn, actor, "creator_responsibility_results_viewed", {"jobs": [str(j["id"]) for j in jobs if j["results"]]}
        )
    return {
        "jobs": jobs,
        "counts": counts,
        "enabled": _enabled(_config(conn)),
        "page": page,
        "has_more": has_more,
        "prompt_version": evidence.PROMPT_VERSION,
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
        "job_id": str(job_id),
        "chats": pack["chats"],
        "messages": messages,
        "results": _results(conn, result, existing) if result else [],
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
            raise ValueError("请先配置并启用达人负责人建议。")
        conn.execute("SELECT enqueue_creator_responsibility(%s,'manual')", (row["contact_id"],))
    else:
        raise ValueError("操作无效。")
    _audit(conn, actor, "creator_responsibility_job_" + operation, {"job_id": str(job_id)})


def probe_sample(settings, actor, contact_ids, *, key_feature=FEATURE):
    """Explicit owner diagnostic, at most five creators; never copies or enables credentials."""
    ids = list(dict.fromkeys(UUID(str(cid)) for cid in contact_ids))
    if not 1 <= len(ids) <= 5 or key_feature not in {FEATURE, "creator_handle_matching"}:
        raise ValueError("测试最多 5 位已确认达人，必须明确选择现有功能凭证。")
    outcomes = []
    for cid in ids:
        with connection() as conn, conn.transaction():
            require_owner(conn, actor)
            config = _config(conn, lock=True, feature=key_feature)
            if not (
                config
                and config["provider"] == "openai"
                and config["api_key_ciphertext"]
                and config["project_id"] != "unconfigured"
            ):
                raise ValueError("所选功能尚未配置有效凭证。")
            if not conn.execute(
                "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'", (cid,)
            ).fetchone():
                raise ValueError("只能测试已确认达人。")
            if conn.execute(
                "SELECT 1 FROM creator_responsibility_jobs WHERE contact_id=%s "
                "AND status NOT IN ('completed','failed','cancelled')",
                (cid,),
            ).fetchone():
                raise ValueError("该达人已有未结束的负责人任务，请先处理已有任务。")
            job = conn.execute(
                """INSERT INTO creator_responsibility_jobs(contact_id,source,status,snapshot_version,
                claim_token,lease_until) VALUES(%s,'sample','preparing',1,%s,NOW()+INTERVAL '10 minutes')
                RETURNING *""",
                (cid, uuid4()),
            ).fetchone()
            _audit(
                conn,
                actor,
                "creator_responsibility_sample_started",
                {
                    "job_id": str(job["id"]),
                    "contact_id": str(cid),
                    "key_feature": key_feature,
                    "suggestions_only": True,
                },
            )
        try:
            with connection() as conn, conn.transaction():
                conn.execute("SET LOCAL statement_timeout='90s'")
                material = evidence.build(conn, settings, cid)
            with connection() as conn, conn.transaction():
                require_owner(conn, actor)
                current_config = _config(conn, lock=True, feature=key_feature)
                current = conn.execute(
                    "SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job["id"],)
                ).fetchone()
                if current["claim_token"] != job["claim_token"] or current["status"] != "preparing":
                    raise ValueError("测试任务已变化。")
                if _config_hash(current_config) != _config_hash(config):
                    raise ValueError("测试凭证已变化，请重新测试。")
                material["stats"] = {**material["stats"], "sample_key_feature": key_feature}
                _store_prepared(conn, current, material, config, settings)
            with connection() as conn, conn.transaction():
                require_owner(conn, actor)
                _config(conn, lock=True, feature=key_feature)
                current = conn.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)).fetchone()
                if current["status"] == "ready":
                    if not ai_dispatch.can_claim(conn):
                        raise ValueError("AI 调度目前暂停或全部槽位忙；本次测试未发送请求。")
                    # Same config -> admission -> job lock order as native worker claims.
                    current = conn.execute(
                        "SELECT * FROM creator_responsibility_jobs WHERE id=%s FOR UPDATE", (job["id"],)
                    ).fetchone()
                    if current["status"] != "ready":
                        raise ValueError("测试任务已变化。")
                    token, reservation = uuid4(), uuid4()
                    conn.execute(
                        "INSERT INTO creator_responsibility_requests(id,job_id,key_feature) VALUES(%s,%s,%s)",
                        (reservation, job["id"], key_feature),
                    )
                    conn.execute(
                        """UPDATE creator_responsibility_jobs SET status='running',claim_token=%s,
                        lease_until=NOW()+(%s*INTERVAL '1 second'),attempts=1 WHERE id=%s""",
                        (token, LEASE_SECONDS, job["id"]),
                    )
                    request = {
                        **current,
                        "pack": material["pack"],
                        "candidate_ids": material["candidate_ids"],
                        "claim_token": token,
                        "attempts": 1,
                        "reservation_id": reservation,
                        "config": config,
                        "key_feature": key_feature,
                        "sample_only": True,
                        "actor_id": actor,
                    }
                else:
                    request = None
            if request:
                run(request, settings)
            with connection() as conn:
                outcome = conn.execute(
                    "SELECT id,contact_id,status,reason_code,input_tokens,output_tokens,stats "
                    "FROM creator_responsibility_jobs WHERE id=%s",
                    (job["id"],),
                ).fetchone()
            outcomes.append(outcome)
            if outcome["status"] == "failed":
                break  # First error stops the bounded diagnostic, never silently retries with another key.
        except Exception:
            with connection() as conn, conn.transaction():
                conn.execute(
                    """UPDATE creator_responsibility_jobs SET status='failed',reason_code='preparation_failed',
                    claim_token=NULL,lease_until=NULL,updated_at=NOW()
                    WHERE id=%s AND status IN ('preparing','ready')""",
                    (job["id"],),
                )
            raise
    return outcomes
