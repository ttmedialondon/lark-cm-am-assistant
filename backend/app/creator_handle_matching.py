"""Opt-in, contact-first account suggestions on the existing worker (no business writes)."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from . import ai_dispatch, ai_feature_config, creator_data
from . import creator_handle_evidence as evidence
from . import openai_client as api
from .creator_access import require_owner
from .db import connection

FEATURE = "creator_handle_matching"
LEASE_SECONDS = 120
MAX_OUTPUT = 3000
CATALOG_PATH = "/var/lib/lark-message-stats/reference-data/curva/2026-09-21-import-20260928"


def catalog():
    path = Path(os.environ.get("CURVA_CATALOG_PATH", CATALOG_PATH)).resolve()
    return _load_catalog(str(path), (path / "manifest.json").stat().st_mtime_ns)


@lru_cache(maxsize=2)
def _load_catalog(directory, _modified):
    root = Path(directory)
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    handles = set()
    if not manifest.get("files"):
        raise ValueError("Curva catalog is empty")
    for item in manifest["files"]:
        path = (root / item["name"]).resolve()
        if path.parent != root or path.suffix != ".csv":
            raise ValueError("Invalid Curva archive path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Curva checksum mismatch")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            handles.update(row["handle"].strip().removeprefix("@").casefold() for row in csv.DictReader(stream))
    if not handles:
        raise ValueError("Curva catalog is empty")
    return frozenset(handles), hashlib.sha256(raw).hexdigest()


def _config(conn, *, lock=False):
    return conn.execute(
        """SELECT provider,project_id,model,enabled,api_key_ciphertext,updated_at,
        rpm_limit,tpm_limit,daily_token_budget,matching_catalog_version,
        automatic_queue_checked_at<NOW()-INTERVAL '1 minute' AS reconcile_due
        FROM ai_feature_configs WHERE feature=%s"""
        + (" FOR UPDATE" if lock else ""),
        (FEATURE,),
    ).fetchone()


def _enabled(config):
    return bool(config and config["enabled"] and config["api_key_ciphertext"] and config["provider"] == "openai")


def _config_hash(config):
    # Credential rotation invalidates a frozen request, without exposing the credential.
    return evidence_hash({k: config[k] for k in ("provider", "project_id", "model", "api_key_ciphertext")})


def evidence_hash(value):
    return hashlib.sha256(creator_data.encode(value).encode()).hexdigest()


def configuration_changed(conn):
    """Called in the settings transaction; never sends a request or enqueues historical scans."""
    config = _config(conn)
    conn.execute(
        """UPDATE creator_handle_jobs SET desired_version=desired_version+1,
        status=CASE WHEN status IN ('running','preparing') THEN status
                    WHEN %s THEN 'queued' ELSE 'cancelled' END,
        not_before=NOW(),updated_at=NOW()
        WHERE status NOT IN ('completed','failed','cancelled')""",
        (_enabled(config),),
    )


def touch_chats(conn, chat_ids):
    """One lightweight signal per committed sync page, not a model call per message."""
    if not chat_ids or not _enabled(_config(conn)):
        return
    conn.execute(
        """SELECT enqueue_creator_handle(r.contact_id) FROM
        (SELECT DISTINCT i.contact_id FROM lark_identities i
         WHERE i.contact_id IS NOT NULL AND (EXISTS(SELECT 1 FROM chat_members m
           WHERE m.open_id=i.open_id AND m.chat_id=ANY(%s::text[])) OR EXISTS(
           SELECT 1 FROM chat_messages m WHERE m.sender_open_id=i.open_id AND m.chat_id=ANY(%s::text[])))) p
        JOIN contact_roles r ON r.contact_id=p.contact_id AND r.role='creator' AND r.status='confirmed'""",
        (chat_ids, chat_ids),
    )


def _recover(conn):
    # Uncertain requests retain their budget reservation. A late response cannot pass its token fence.
    conn.execute("""UPDATE creator_handle_jobs SET
        status=CASE WHEN desired_version<>snapshot_version THEN 'queued'
          WHEN status='preparing' THEN 'queued' WHEN attempts<3 THEN 'retry_wait' ELSE 'failed' END,
        reason_code='lease_expired',claim_token=NULL,lease_until=NULL,not_before=NOW(),updated_at=NOW()
        WHERE status IN ('running','preparing') AND lease_until<NOW()""")
    conn.execute("""UPDATE creator_handle_jobs j SET status='cancelled',desired_version=desired_version+1,
        reason_code='identity_removed',updated_at=NOW()
        WHERE j.status NOT IN ('completed','failed','cancelled') AND NOT EXISTS(
        SELECT 1 FROM contact_roles r WHERE r.contact_id=j.contact_id AND r.role='creator' AND r.status='confirmed')""")


def prepare_next(settings):
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config):
            return False
        _recover(conn)
        # Trigger is primary. This low-frequency indexed check covers clients that predate the hook.
        if config["reconcile_due"]:
            try:
                _, catalog_version = catalog()
            except (OSError, ValueError, KeyError):
                catalog_version = None
            if catalog_version and catalog_version != config["matching_catalog_version"]:
                # Reprepare only previously enrolled contacts; unchanged selected evidence is cached.
                conn.execute("""SELECT enqueue_creator_handle(contact_id,'incremental',TRUE)
                    FROM (SELECT DISTINCT contact_id FROM creator_handle_jobs WHERE status<>'cancelled'
                      AND (catalog_version IS NOT NULL OR reason_code='catalog_unavailable')) enrolled""")
                conn.execute(
                    "UPDATE ai_feature_configs SET matching_catalog_version=%s WHERE feature=%s",
                    (catalog_version, FEATURE),
                )
            conn.execute(
                """SELECT enqueue_creator_handle(r.contact_id,'incremental',TRUE)
                FROM contact_roles r WHERE r.role='creator' AND r.status='confirmed'
                  AND r.created_at>=%s AND NOT EXISTS(
                    SELECT 1 FROM creator_handle_jobs j WHERE j.contact_id=r.contact_id)
                ORDER BY r.created_at LIMIT 100""",
                (config["updated_at"],),
            )
            conn.execute("UPDATE ai_feature_configs SET automatic_queue_checked_at=NOW() WHERE feature=%s", (FEATURE,))
        if conn.execute("SELECT count(*) n FROM creator_handle_jobs WHERE status='ready'").fetchone()["n"] >= 6:
            return False
        recent = conn.execute("""SELECT source FROM creator_handle_jobs WHERE prepared_at IS NOT NULL
            ORDER BY prepared_at DESC LIMIT 4""").fetchall()
        history_turn = len(recent) == 4 and all(row["source"] != "history" for row in recent)
        job = conn.execute(
            """SELECT * FROM creator_handle_jobs WHERE status='queued' AND not_before<=NOW()
            ORDER BY CASE WHEN source='history' THEN %s ELSE %s END,created_at
            FOR UPDATE SKIP LOCKED LIMIT 1""",
            (0 if history_turn else 1, 1 if history_turn else 0),
        ).fetchone()
        if not job:
            return False
        token = uuid4()
        conn.execute(
            """UPDATE creator_handle_jobs SET status='preparing',snapshot_version=desired_version,
            claim_token=%s,lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s""",
            (token, job["id"]),
        )
        version = job["desired_version"]
        config_hash = _config_hash(config)
    try:
        handles, catalog_version = catalog()
        with connection() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout='90s'")
            prepared = evidence.build(conn, settings, job["contact_id"], handles)
    except (OSError, ValueError, KeyError):
        _finish_preparation(job["id"], token, version, "waiting_evidence", "catalog_unavailable")
        return True
    except Exception:
        # Local failures never masquerade as a negative identity decision or spin continuously.
        _finish_preparation(job["id"], token, version, "failed", "preparation_failed")
        raise
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        current = conn.execute("SELECT * FROM creator_handle_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
        if not current or current["claim_token"] != token or current["status"] != "preparing":
            return True
        if current["desired_version"] != version or not _enabled(config) or _config_hash(config) != config_hash:
            _invalidate(conn, current)
            return True
        pack = prepared["pack"]
        fingerprint = evidence_hash([prepared["fingerprint"], config_hash, evidence.PROMPT_VERSION])
        cached = conn.execute(
            """SELECT result_ciphertext,status,stats,attempts,reason_code FROM creator_handle_jobs
            WHERE contact_id=%s AND fingerprint=%s AND (
              result_ciphertext IS NOT NULL AND (status IN ('completed','review_required') OR id=%s)
              OR status='failed' AND attempts>0 AND %s<>'manual')
            ORDER BY updated_at DESC LIMIT 1""",
            (job["contact_id"], fingerprint, job["id"], job["source"]),
        ).fetchone()
        storage = creator_data.cipher(settings)
        if cached:
            cached["stats"] = {**cached["stats"], **prepared["stats"]}
        if cached and cached["result_ciphertext"]:
            saved = json.loads(storage.decrypt(cached["result_ciphertext"]))["result"]
            cached["status"] = "review_required" if _needs_review(saved, cached["stats"]) else "completed"
        conn.execute(
            """UPDATE creator_handle_jobs SET status=%s,reason_code=%s,
            snapshot_version=%s,fingerprint=%s,config_fingerprint=%s,catalog_version=%s,
            pack_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,result_ciphertext=%s,
            attempts=%s,claim_token=NULL,lease_until=NULL,prepared_at=NOW(),updated_at=NOW(),
            debounce_started_at=NOW(),finished_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s""",
            (
                cached["status"] if cached else "ready" if pack else "waiting_evidence",
                "failure_cached"
                if cached and cached["status"] == "failed"
                else "cached"
                if cached
                else prepared["reason"],
                version,
                fingerprint,
                config_hash,
                catalog_version,
                storage.encrypt(creator_data.encode(pack)) if pack else None,
                creator_data.encode(prepared["refs"]),
                creator_data.encode(cached["stats"] if cached else prepared["stats"]),
                cached["result_ciphertext"] if cached else None,
                cached["attempts"] if cached and cached["status"] == "failed" else 0,
                bool(cached),
                job["id"],
            ),
        )
    return True


def _finish_preparation(job_id, token, version, state, reason):
    with connection() as conn, conn.transaction():
        conn.execute(
            """UPDATE creator_handle_jobs SET status=CASE WHEN desired_version=%s THEN %s ELSE 'queued' END,
            reason_code=%s,claim_token=NULL,lease_until=NULL,updated_at=NOW()
            WHERE id=%s AND status='preparing' AND claim_token=%s""",
            (version, state, reason, job_id, token),
        )


def _invalidate(conn, job):
    enabled = _enabled(_config(conn))
    conn.execute(
        """UPDATE creator_handle_jobs SET status=%s,reason_code='snapshot_changed',
        claim_token=NULL,lease_until=NULL,not_before=NOW(),result_ciphertext=NULL,updated_at=NOW() WHERE id=%s""",
        ("queued" if enabled else "cancelled", job["id"]),
    )


def _needs_review(result, stats):
    return any(
        link["decision"] == "review"
        or link["period"] == "historical"
        or link["relation"] in ("operator", "account_creator")
        for link in result["links"]
    ) or any(stats.get(key) for key in ("history_truncated", "candidates_omitted", "critical_evidence_omitted"))


def _reserve(conn, config, job, tokens):
    """Atomic project/model limits for this feature; external project traffic remains provider-controlled."""
    scope = evidence_hash([config["provider"], config["project_id"], config["model"]])
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ai_dispatch._ADMISSION_LOCK,))
    used = conn.execute(
        """SELECT count(*) FILTER(WHERE created_at>NOW()-INTERVAL '1 minute') rpm,
        COALESCE(sum(reserved_tokens) FILTER(WHERE created_at>NOW()-INTERVAL '1 minute'),0) tpm,
        COALESCE(sum(COALESCE(billed_tokens,reserved_tokens)) FILTER(WHERE
          created_at>=date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),0) daily
        FROM creator_handle_requests WHERE quota_scope=%s AND created_at>=LEAST(
          date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',NOW()-INTERVAL '1 minute')""",
        (scope,),
    ).fetchone()
    if tokens > config["tpm_limit"] or tokens > config["daily_token_budget"]:
        conn.execute(
            "UPDATE creator_handle_jobs SET status='failed',reason_code='request_exceeds_budget' WHERE id=%s",
            (job["id"],),
        )
        return None
    if used["daily"] + tokens > config["daily_token_budget"]:
        delay = "date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'+INTERVAL '1 day'"
        reason = "daily_budget"
    elif used["rpm"] >= config["rpm_limit"] or used["tpm"] + tokens > config["tpm_limit"]:
        delay, reason = "NOW()+INTERVAL '1 minute'", "rate_budget"
    else:
        reservation = uuid4()
        conn.execute(
            "INSERT INTO creator_handle_requests(id,job_id,quota_scope,reserved_tokens) VALUES(%s,%s,%s,%s)",
            (reservation, job["id"], scope, tokens),
        )
        return reservation
    conn.execute(f"UPDATE creator_handle_jobs SET not_before={delay},reason_code=%s WHERE id=%s", (reason, job["id"]))
    return None


def claim():
    with connection() as conn, conn.transaction():
        config = _config(conn, lock=True)
        if not _enabled(config) or not ai_dispatch.can_claim(conn):
            return None
        _recover(conn)
        # Four increments followed by an available history item; persisted across worker restarts.
        recent = conn.execute("""SELECT j.source FROM creator_handle_requests r
            JOIN creator_handle_jobs j ON j.id=r.job_id ORDER BY r.created_at DESC LIMIT 4""").fetchall()
        history_turn = len(recent) == 4 and all(r["source"] != "history" for r in recent)
        job = conn.execute(
            """SELECT * FROM creator_handle_jobs WHERE status IN ('ready','retry_wait')
            AND not_before<=NOW() AND attempts<3 ORDER BY
              CASE WHEN source='history' THEN %s ELSE %s END,created_at FOR UPDATE SKIP LOCKED LIMIT 1""",
            (0 if history_turn else 1, 1 if history_turn else 0),
        ).fetchone()
        if not job:
            return None
        if job["config_fingerprint"] != _config_hash(config) or job["snapshot_version"] != job["desired_version"]:
            _invalidate(conn, job)
            return None
        # UTF-8 bytes are a conservative input-token upper bound, not a price estimate.
        from .config import get_settings

        storage = creator_data.cipher(get_settings())
        raw = storage.decrypt(job["pack_ciphertext"])
        tokens = (
            len(raw.encode())
            + len(evidence.instructions().encode())
            + len(creator_data.encode(evidence.output_format()).encode())
            + MAX_OUTPUT
            + 512
        )
        reservation = _reserve(conn, config, job, tokens)
        if not reservation:
            return None
        token = uuid4()
        conn.execute(
            """UPDATE creator_handle_jobs SET status='running',claim_token=%s,
            lease_until=NOW()+(%s*INTERVAL '1 second'),attempts=attempts+1,updated_at=NOW() WHERE id=%s""",
            (token, LEASE_SECONDS, job["id"]),
        )
        return {
            **job,
            "claim_token": token,
            "attempts": job["attempts"] + 1,
            "reservation_id": reservation,
            "config": config,
            "pack": json.loads(raw),
        }


def _current(conn, job):
    config = _config(conn, lock=True)
    row = conn.execute("SELECT * FROM creator_handle_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    if not row or row["status"] != "running" or row["claim_token"] != job["claim_token"]:
        return None
    if (
        row["desired_version"] != job["snapshot_version"]
        or not _enabled(config)
        or _config_hash(config) != job["config_fingerprint"]
    ):
        _invalidate(conn, row)
        return None
    if not conn.execute(
        "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
        (job["contact_id"],),
    ).fetchone():
        conn.execute(
            """UPDATE creator_handle_jobs SET status='cancelled',reason_code='identity_removed',
            lease_until=NULL WHERE id=%s""",
            (job["id"],),
        )
        return None
    return row


def run(job, settings):
    with connection() as conn, conn.transaction():
        current = _current(conn, job)
        if not current:
            # No network call was started: reservation can be safely released.
            conn.execute("UPDATE creator_handle_requests SET billed_tokens=0 WHERE id=%s", (job["reservation_id"],))
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            conn.execute("UPDATE creator_handle_requests SET billed_tokens=0 WHERE id=%s", (job["reservation_id"],))
            return
    result, telemetry, error = None, {}, None
    network_started = False
    try:
        handles, version = catalog()
        if version != job["catalog_version"] or any(h["handle"] not in handles for h in job["pack"]["handles"]):
            raise ValueError("catalog_changed")
        config = job["config"]
        key = ai_feature_config.api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
        network_started = True
        response = api.call_openai(
            config,
            key,
            job["pack"],
            str(job["contact_id"]),
            instructions=evidence.instructions(),
            output_format=evidence.output_format(),
            max_output_tokens=MAX_OUTPUT,
            reasoning_effort="none",
            total_timeout=45,
        )
        telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
        ai_dispatch.observe_limits(response["telemetry"])
        result = evidence.validate(job["pack"], json.loads(response["text"]))
    except Exception as exc:
        error = exc
        telemetry = getattr(exc, "telemetry", {}) or telemetry
        if isinstance(exc, api.OpenAIRequestError):
            if exc.is_quota:
                ai_dispatch.pause_for_quota()
            elif exc.retryable:
                ai_dispatch.throttle(api.retry_delay(exc.retry_after_seconds, job["attempts"]))
    with connection() as conn, conn.transaction():
        # Same lock order as settings saves: config, then jobs.
        _config(conn, lock=True)
        usage = telemetry.get("usage", {})
        input_tokens = int(usage.get("input_tokens", telemetry.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("output_tokens", telemetry.get("output_tokens", 0)) or 0)
        if not network_started:
            conn.execute("UPDATE creator_handle_requests SET billed_tokens=0 WHERE id=%s", (job["reservation_id"],))
        elif usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
            conn.execute(
                "UPDATE creator_handle_requests SET billed_tokens=%s WHERE id=%s",
                (input_tokens + output_tokens, job["reservation_id"]),
            )
        conn.execute(
            "UPDATE creator_handle_jobs SET input_tokens=input_tokens+%s,output_tokens=output_tokens+%s WHERE id=%s",
            (input_tokens, output_tokens, job["id"]),
        )
        current = _current(conn, job)
        if not current:
            return
        if not evidence.still_valid(conn, current["refs"], settings):
            _invalidate(conn, current)
            return
        try:
            current_handles, version = catalog()
            catalog_valid = version == job["catalog_version"] and all(
                h["handle"] in current_handles for h in job["pack"]["handles"]
            )
        except (OSError, ValueError, KeyError):
            catalog_valid = False
        if not catalog_valid or (isinstance(error, ValueError) and str(error) == "catalog_changed"):
            _invalidate(conn, current)
            return
        if error:
            retry = isinstance(error, api.OpenAIRequestError) and error.retryable and job["attempts"] < 3
            delay = api.retry_delay(getattr(error, "retry_after_seconds", None), job["attempts"]) if retry else 0
            state = "retry_wait" if retry else "failed"
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
                """UPDATE creator_handle_jobs SET status=%s,reason_code=%s,lease_until=NULL,
                claim_token=NULL,not_before=NOW()+(%s*INTERVAL '1 second'),updated_at=NOW() WHERE id=%s""",
                (state, reason, delay, job["id"]),
            )
        else:
            by_handle = {h["id"]: h["handle"] for h in job["pack"]["handles"]}
            supported = []
            claimed = []
            for link in result["links"]:
                if link["decision"] == "supported" and link["relation"] == "self_account":
                    handle = by_handle[link["h"]]
                    claimed.append(handle)
                    conflict = conn.execute(
                        """SELECT 1 FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
                        WHERE s.platform='tiktok' AND s.normalized_handle=%s AND l.relationship='owner'
                          AND l.status='confirmed' AND l.contact_id<>%s UNION ALL
                        SELECT 1 FROM creator_handle_jobs WHERE contact_id<>%s
                          AND status IN ('completed','review_required')
                          AND stats->'claimed_handles' ? %s
                          AND NOT EXISTS(SELECT 1 FROM creator_handle_jobs newer
                            WHERE newer.contact_id=creator_handle_jobs.contact_id
                              AND (newer.created_at,newer.id)>(creator_handle_jobs.created_at,creator_handle_jobs.id))
                          LIMIT 1""",
                        (handle, job["contact_id"], job["contact_id"], handle),
                    ).fetchone()
                    if conflict:
                        link.update(decision="review", relation="uncertain", code="conflict")
                    else:
                        supported.append(handle)
            review = _needs_review(result, current["stats"])
            view = {"handles": by_handle, "result": result}
            stats = {**current["stats"], "supported_handles": supported, "claimed_handles": claimed}
            conn.execute(
                """UPDATE creator_handle_jobs SET status=%s,reason_code='suggestions_saved',
                result_ciphertext=%s,stats=%s::jsonb,lease_until=NULL,claim_token=NULL,
                response_id=%s,request_id=%s,finished_at=NOW(),updated_at=NOW() WHERE id=%s""",
                (
                    "review_required" if review else "completed",
                    creator_data.cipher(settings).encrypt(creator_data.encode(view)),
                    creator_data.encode(stats),
                    telemetry.get("response_id"),
                    telemetry.get("openai_request_id"),
                    job["id"],
                ),
            )
        conn.execute(
            "UPDATE ai_feature_configs SET last_tested_at=NOW(),last_test_status=%s WHERE feature=%s",
            ("failed" if error else "succeeded", FEATURE),
        )


def enqueue_history(conn, actor):
    require_owner(conn, actor)
    if not _enabled(_config(conn)):
        raise ValueError("请先在 API Key 列表配置并启用达人—handle 匹配。")
    rows = conn.execute("""SELECT DISTINCT r.contact_id FROM contact_roles r
        WHERE r.role='creator' AND r.status='confirmed'
        AND NOT EXISTS(SELECT 1 FROM creator_handle_jobs j WHERE j.contact_id=r.contact_id
           AND j.status NOT IN ('cancelled','failed')) ORDER BY r.contact_id""").fetchall()
    for row in rows:
        conn.execute("SELECT enqueue_creator_handle(%s,'history',TRUE)", (row["contact_id"],))
    _audit(conn, actor, "creator_handle_history_enqueued", {"contacts": len(rows)})
    return len(rows)


def page_data(conn, actor, settings, page=1):
    require_owner(conn, actor)
    counts = conn.execute("""SELECT status,count(*) n FROM (
        SELECT DISTINCT ON(contact_id) status FROM creator_handle_jobs
        ORDER BY contact_id,created_at DESC,id DESC) latest GROUP BY status""").fetchall()
    jobs = conn.execute(
        """SELECT j.*,c.display_name FROM creator_handle_jobs j JOIN contacts c ON c.id=j.contact_id
        ORDER BY j.updated_at DESC,j.id LIMIT 50 OFFSET %s""",
        ((page - 1) * 50,),
    ).fetchall()
    storage = creator_data.cipher(settings)
    try:
        current_handles, version = catalog()
    except (OSError, ValueError, KeyError):
        current_handles = frozenset()
        version = None
    for job in jobs:
        job["suggestions"] = []
        if (
            job["result_ciphertext"]
            and job["catalog_version"] == version
            and evidence.still_valid(conn, job["refs"], settings)
        ):
            view = json.loads(storage.decrypt(job["result_ciphertext"]))
            job["suggestions"] = [
                {**link, "handle": view["handles"][link["h"]]}
                for link in view["result"]["links"]
                if view["handles"][link["h"]] in current_handles
            ]
            for link in job["suggestions"]:
                if (
                    link["decision"] == "supported"
                    and link["relation"] == "self_account"
                    and conn.execute(
                        """SELECT 1 FROM creator_handle_jobs WHERE contact_id<>%s
                    AND status IN ('completed','review_required') AND stats->'claimed_handles' ? %s
                    AND NOT EXISTS(SELECT 1 FROM creator_handle_jobs newer
                      WHERE newer.contact_id=creator_handle_jobs.contact_id
                        AND (newer.created_at,newer.id)>(creator_handle_jobs.created_at,creator_handle_jobs.id))
                    UNION ALL SELECT 1 FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
                    WHERE l.contact_id<>%s AND s.platform='tiktok' AND s.normalized_handle=%s
                      AND l.relationship='owner' AND l.status='confirmed' LIMIT 1""",
                        (job["contact_id"], link["handle"], job["contact_id"], link["handle"]),
                    ).fetchone()
                ):
                    link.update(decision="review", relation="uncertain", code="conflict")
        for field in ("pack_ciphertext", "result_ciphertext", "refs"):
            job.pop(field, None)
    return {"jobs": jobs, "counts": counts, "enabled": _enabled(_config(conn)), "page": page}


def action(conn, actor, job_id, operation):
    require_owner(conn, actor)
    row = conn.execute("SELECT * FROM creator_handle_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not row:
        raise LookupError("任务不存在。")
    if operation == "cancel":
        conn.execute(
            """UPDATE creator_handle_jobs SET status='cancelled',desired_version=desired_version+1,
            reason_code='owner_cancelled',updated_at=NOW() WHERE id=%s""",
            (job_id,),
        )
    elif operation == "recheck":
        if not _enabled(_config(conn)):
            raise ValueError("匹配功能未启用。")
        conn.execute("SELECT enqueue_creator_handle(%s,'manual',TRUE)", (row["contact_id"],))
    else:
        raise ValueError("操作无效。")
    _audit(conn, actor, "creator_handle_job_" + operation, {"job_id": str(job_id)})


def _audit(conn, actor, action, data):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor, action, creator_data.encode(data)),
    )
