"""Opt-in account matching; verified self-account evidence can add a formal association."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from . import ai_dispatch, ai_feature_config, contact_handles, creator_data, creator_imports
from . import creator_handle_evidence as evidence
from . import openai_client as api
from .creator_access import require_owner
from .db import connection

FEATURE = "creator_handle_matching"
LEASE_SECONDS = 120
MAX_OUTPUT = 3000
CATALOG_PATH = "/var/lib/lark-message-stats/reference-data/curva/2026-09-21-import-20260928"
OUTPUT_ERRORS = (
    frozenset(
        {
            "shape",
            "array",
            "enum",
            "candidates",
            "known_identity",
            "unknown_identity",
            "references",
            "needed",
            "unsupported_evidence",
            "supported_relation",
            "excerpted_support",
            "owner_code",
            "unconnected_owner_evidence",
            "invalid_response",
            "unfinished_response",
            "invalid_json",
        }
    )
    | evidence.VALIDATION_ERRORS
)


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
        matching_catalog_version,
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


def _confirmation_changed(conn, job, pack):
    current = {row["handle"] for row in contact_handles.confirmed_pairs(conn, [job["contact_id"]])}
    snapshot = job["stats"].get("confirmed_handles")
    allowed = set(job["stats"].get("recheck_handles", []))
    return (
        snapshot is None or set(snapshot) != current
        or job["stats"].get("manual_recheck", False) != (job["source"] == "manual")
        or bool((current - allowed) & {h["handle"] for h in (pack or {}).get("handles", [])})
    )


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
    # Uncertain requests retain their usage estimate. A late response cannot pass its token fence.
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
            prepared = evidence.build(conn, settings, job["contact_id"], handles, recheck=job["source"] == "manual")
            prepared["stats"] = {**prepared["stats"], "prompt_version": evidence.PROMPT_VERSION}
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
        if prepared["reason"] != "restricted" and _confirmation_changed(
            conn, {**current, "stats": prepared["stats"]}, pack
        ):
            _invalidate(conn, current)
            return True
        fingerprint = evidence_hash(
            [
                prepared["fingerprint"],
                config["provider"],
                config["project_id"],
                config["model"],
                evidence.PROMPT_VERSION,
            ]
        )
        cached = conn.execute(
            """SELECT result_ciphertext,status,stats,attempts,reason_code FROM creator_handle_jobs
            WHERE contact_id=%s AND fingerprint=%s AND (
              result_ciphertext IS NOT NULL AND (status IN ('completed','review_required') OR id=%s)
              OR status='failed' AND attempts>0 AND %s<>'manual' AND config_fingerprint=%s)
            ORDER BY updated_at DESC LIMIT 1""",
            (job["contact_id"], fingerprint, job["id"], job["source"], config_hash),
        ).fetchone()
        storage = creator_data.cipher(settings)
        if cached:
            cached["stats"] = {**cached["stats"], **prepared["stats"]}
        if cached and cached["result_ciphertext"]:
            if (
                not evidence.still_valid(conn, prepared["refs"], settings)
                or not conn.execute(
                    "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                    (job["contact_id"],),
                ).fetchone()
            ):
                _invalidate(conn, current)
                return True
            saved = evidence.validate(pack, json.loads(storage.decrypt(cached["result_ciphertext"]))["result"])
            cached["stats"] = _apply_links(
                conn,
                current,
                pack,
                saved,
                cached["stats"],
                prepared["refs"],
                settings,
                catalog_version,
            )
            if cached["stats"] is None:
                _invalidate(conn, current)
                return True
            cached["result_ciphertext"] = storage.encrypt(
                creator_data.encode(
                    {
                        "handles": {h["id"]: h["handle"] for h in pack["handles"]},
                        "result": saved,
                    }
                )
            )
            cached["status"] = "review_required" if _needs_review(saved, cached["stats"]) else "completed"
        conn.execute(
            """UPDATE creator_handle_jobs SET status=%s,reason_code=%s,
            snapshot_version=%s,fingerprint=%s,config_fingerprint=%s,catalog_version=%s,
            pack_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,result_ciphertext=%s,
            attempts=%s,claim_token=NULL,lease_until=NULL,prepared_at=NOW(),updated_at=NOW(),
            debounce_started_at=NOW(),finished_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s""",
            (
                cached["status"]
                if cached
                else "ready"
                if pack
                else "completed"
                if prepared["reason"] == "already_confirmed"
                else "waiting_evidence",
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
                bool(cached) or prepared["reason"] == "already_confirmed",
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
        or link["needed"]
        or link["period"] == "historical"
        or link["relation"] in ("operator", "account_creator")
        for link in result["links"]
    ) or any(stats.get(key) for key in ("history_truncated", "candidates_omitted", "critical_evidence_omitted"))


def auto_link_eligible(pack, link, stats):
    """Evidence eligibility; live authorization and account conflicts are checked separately."""
    if not (
        pack["creator_known"]
        and link["decision"] == "supported"
        and link["relation"] == "self_account"
        and link["period"] == "at_message"
        and link["code"] in {"self_claim", "qa_confirm", "own_page"}
        and not link["needed"]
    ):
        return False
    if "critical_evidence_omitted_handles" not in stats:
        return not any(stats.get(k) for k in ("history_truncated", "candidates_omitted", "critical_evidence_omitted"))
    omitted = stats["critical_evidence_omitted_handles"]
    handle = next(h["handle"] for h in pack["handles"] if h["id"] == link["h"])
    return isinstance(omitted, list) and handle not in omitted


def _apply_links(conn, job, pack, result, stats, refs, settings, catalog_version):
    """Add only verified self-account links; preserve all existing human decisions."""
    by_handle = {h["id"]: h["handle"] for h in pack["handles"]}
    claims = [
        link for link in result["links"] if link["decision"] == "supported" and link["relation"] == "self_account"
    ]
    claimed = [by_handle[link["h"]] for link in claims]
    # Same account -> person lock order as manual roster admission; sort multiple handles.
    for handle in sorted(claimed):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + handle,))
    if claimed:
        conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (job["contact_id"],))
        # Locks may have waited: recheck catalog, authorization and identity before writing.
        try:
            current_handles, version = catalog()
            if version != catalog_version or any(h["handle"] not in current_handles for h in pack["handles"]):
                return None
        except (OSError, ValueError, KeyError):
            return None
        if (
            not evidence.still_valid(conn, refs, settings)
            or not conn.execute(
                "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                (job["contact_id"],),
            ).fetchone()
        ):
            return None
    # Reuse the admission rules read-only: never call its identity/CM-writing _admit.
    conflicts = dict(
        zip(
            claimed,
            creator_imports._conflicts(
                conn,
                [(None, job["contact_id"], handle) for handle in claimed],
            ),
            strict=True,
        )
    )
    other_claims = (
        {
            row["handle"]
            for row in conn.execute(
                """SELECT handle FROM unnest(%s::text[]) handles(handle) WHERE EXISTS (
        SELECT 1 FROM creator_handle_jobs j WHERE j.contact_id<>%s
          AND j.status IN ('completed','review_required') AND j.stats->'claimed_handles' ? handles.handle
          AND NOT EXISTS (SELECT 1 FROM creator_handle_jobs newer WHERE newer.contact_id=j.contact_id
            AND (newer.created_at,newer.id)>(j.created_at,j.id)))""",
                (claimed, job["contact_id"]),
            ).fetchall()
        }
        if claimed
        else set()
    )
    accounts = {}
    for account in (
        conn.execute(
            """SELECT id,lower(normalized_handle) handle FROM social_accounts
        WHERE platform='tiktok' AND lower(normalized_handle)=ANY(%s::text[]) ORDER BY id FOR UPDATE""",
            (claimed,),
        ).fetchall()
        if claimed
        else []
    ):
        accounts.setdefault(account["handle"], []).append(account["id"])
    associated, added = [], []
    for link in claims:
        handle = by_handle[link["h"]]
        if conflicts[handle] or handle in other_claims or len(accounts.get(handle, [])) > 1:
            link.update(decision="review", relation="uncertain", code="conflict")
            continue
        if not auto_link_eligible(pack, link, stats):
            continue
        account_id = accounts[handle][0] if accounts.get(handle) else None
        if account_id is None:
            account = conn.execute(
                """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
                VALUES(%s,'tiktok',%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                (uuid4(), handle, handle),
            ).fetchone()
            if not account:
                link.update(decision="review", relation="uncertain", code="conflict")
                continue
            account_id = account["id"]
        inserted = conn.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,reviewed_at)
            VALUES(%s,%s,'owner','confirmed','creator_handle_auto_verified',NOW())
            ON CONFLICT DO NOTHING RETURNING social_account_id""",
            (job["contact_id"], account_id),
        ).fetchone()
        if inserted:
            added.append(handle)
            _audit(
                conn,
                None,
                "creator_handle_auto_linked",
                {
                    "job_id": str(job["id"]),
                    "contact_id": str(job["contact_id"]),
                    "social_account_id": str(account_id),
                    "handle": handle,
                    "prompt_version": evidence.PROMPT_VERSION,
                    "refs": link["refs"],
                    "evidence_refs": [ref for ref in refs if ref["kind"] == "message" and ref["label"] in link["refs"]],
                },
            )
        elif not conn.execute(
            """SELECT 1 FROM contact_social_accounts WHERE contact_id=%s AND social_account_id=%s
            AND relationship='owner' AND status='confirmed'""",
            (job["contact_id"], account_id),
        ).fetchone():
            link.update(decision="review", relation="uncertain", code="conflict")
            continue
        associated.append(handle)
    supported = [by_handle[link["h"]] for link in claims if link["decision"] == "supported"]
    return {
        **stats,
        "supported_handles": supported,
        "claimed_handles": claimed,
        "associated_handles": associated,
        "auto_linked_handles": added,
    }


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
        # Usage estimate only: neither tokens nor recent request counts limit admission.
        # UTF-8 bytes are a conservative input-token upper bound, not a price estimate.
        from .config import get_settings

        storage = creator_data.cipher(get_settings())
        raw = storage.decrypt(job["pack_ciphertext"])
        pack = json.loads(raw)
        if _confirmation_changed(conn, job, pack):
            _invalidate(conn, job)
            return None
        tokens = (
            len(raw.encode())
            + len(evidence.instructions().encode())
            + len(creator_data.encode(evidence.output_format(pack)).encode())
            + MAX_OUTPUT
            + 512
        )
        reservation = uuid4()
        scope = evidence_hash([config["provider"], config["project_id"], config["model"]])
        conn.execute(
            "INSERT INTO creator_handle_requests(id,job_id,quota_scope,reserved_tokens) VALUES(%s,%s,%s,%s)",
            (reservation, job["id"], scope, tokens),
        )
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
            "pack": pack,
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
        or _confirmation_changed(conn, row, job["pack"])
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


def _output_error(error):
    if isinstance(error, api.ModelOutputError):
        code = str(error).replace(" ", "_")
        return code if code in OUTPUT_ERRORS else "invalid_output"
    if isinstance(error, api.OpenAIRequestError):
        status = error.telemetry.get("http_status")
        if isinstance(status, int) and 100 <= status <= 599:
            return f"http_{status}"
        return "quota_paused" if error.is_quota else "provider_retry" if error.retryable else "provider_failed"
    return "" if error is None else "internal_error"


def _compact_result(pack, value):
    """Whitelist protocol fields: never persist model prose, quotations or arbitrary strings."""
    if not isinstance(value, dict):
        return {"parseable": False}
    schema = evidence.output_format()["schema"]["properties"]
    handle_ids = {h["id"] for h in pack["handles"]}
    message_ids = {m["id"] for m in pack["messages"]}

    def fields(item, properties):
        if not isinstance(item, dict):
            return {"invalid_shape": True}
        clean = {}
        for key, spec in properties.items():
            child = item.get(key)
            if key in {"refs", "needed"}:
                allowed = message_ids if key == "refs" else set(spec["items"]["enum"])
                clean[key] = (
                    [v if isinstance(v, str) and v in allowed else "[invalid]" for v in child[:7]]
                    if isinstance(child, list)
                    else "[invalid]"
                )
            elif key == "h":
                clean[key] = child if isinstance(child, str) and child in handle_ids else "[invalid]"
            else:
                clean[key] = child if isinstance(child, str) and child in spec["enum"] else "[invalid]"
        code = item.get("validation_error")
        if isinstance(code, str) and code in OUTPUT_ERRORS:
            clean["validation_error"] = code
        return clean

    links = value.get("links")
    return {
        "creator": fields(value.get("creator"), schema["creator"]["properties"]),
        "links": [fields(link, schema["links"]["items"]["properties"]) for link in links[:12]]
        if isinstance(links, list)
        else "[invalid]",
    }


def review(pack, config, contact_id, settings):
    """One paid call, shared by the worker and the bounded, no-write probe."""
    result, telemetry, error, diagnostic = None, {}, None, None
    try:
        key = ai_feature_config.api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
        response = api.call_openai(
            config,
            key,
            pack,
            str(contact_id),
            instructions=evidence.instructions(),
            output_format=evidence.output_format(pack),
            max_output_tokens=MAX_OUTPUT,
            reasoning_effort="none",
            total_timeout=45,
        )
        telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
        try:
            parsed = json.loads(response["text"])
        except (ValueError, TypeError) as exc:
            diagnostic = {"returned": {"parseable": False}}
            raise api.ModelOutputError("invalid_json") from exc
        diagnostic = {"returned": _compact_result(pack, parsed)}
        result = evidence.validate(pack, parsed)
        diagnostic["validated"] = _compact_result(pack, result)
    except Exception as exc:
        error = exc
        telemetry = getattr(exc, "telemetry", {}) or telemetry
    return result, telemetry, error, diagnostic


def record_response(conn, reservation_id, pack, telemetry, error, diagnostic, settings):
    usage = telemetry.get("usage", {})
    conn.execute(
        """UPDATE creator_handle_requests SET error_code=%s,request_id=%s,response_id=%s,
        input_tokens=%s,output_tokens=%s,response_ciphertext=%s WHERE id=%s""",
        (
            _output_error(error),
            telemetry.get("openai_request_id"),
            telemetry.get("response_id"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            creator_data.cipher(settings).encrypt(creator_data.encode(diagnostic)) if diagnostic else None,
            reservation_id,
        ),
    )


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
    result, telemetry, error, diagnostic = None, {}, None, None
    network_started = False
    try:
        handles, version = catalog()
        if version != job["catalog_version"] or any(h["handle"] not in handles for h in job["pack"]["handles"]):
            raise ValueError("catalog_changed")
        network_started = True
        result, telemetry, error, diagnostic = review(job["pack"], job["config"], job["contact_id"], settings)
        ai_dispatch.observe_limits(telemetry)
        if error:
            raise error
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
        record_response(conn, job["reservation_id"], job["pack"], telemetry, error, diagnostic, settings)
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
            stats = _apply_links(
                conn,
                current,
                job["pack"],
                result,
                current["stats"],
                current["refs"],
                settings,
                job["catalog_version"],
            )
            if stats is None:
                _invalidate(conn, current)
                return
            needs_review = _needs_review(result, current["stats"])
            view = {"handles": by_handle, "result": result}
            conn.execute(
                """UPDATE creator_handle_jobs SET status=%s,reason_code='suggestions_saved',
                result_ciphertext=%s,stats=%s::jsonb,lease_until=NULL,claim_token=NULL,
                response_id=%s,request_id=%s,finished_at=NOW(),updated_at=NOW() WHERE id=%s""",
                (
                    "review_required" if needs_review else "completed",
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
    rows = conn.execute(
        """SELECT DISTINCT r.contact_id FROM contact_roles r
        WHERE r.role='creator' AND r.status='confirmed'
        AND NOT EXISTS(SELECT 1 FROM creator_handle_jobs j WHERE j.contact_id=r.contact_id
           AND (j.status NOT IN ('completed','review_required','cancelled','failed')
             OR j.status IN ('completed','review_required') AND j.stats->>'prompt_version'=%s))
        ORDER BY r.contact_id""",
        (evidence.PROMPT_VERSION,),
    ).fetchall()
    for row in rows:
        enqueued = conn.execute("SELECT enqueue_creator_handle(%s,'history',TRUE) id", (row["contact_id"],)).fetchone()
        conn.execute("UPDATE creator_handle_jobs SET source='history' WHERE id=%s", (enqueued["id"],))
    _audit(conn, actor, "creator_handle_history_enqueued", {"contacts": len(rows)})
    return len(rows)


def page_data(conn, actor, settings, page=1):
    require_owner(conn, actor)
    counts = conn.execute("""SELECT status,count(*) n FROM (
        SELECT DISTINCT ON(contact_id) status FROM creator_handle_jobs
        ORDER BY contact_id,created_at DESC,id DESC) latest GROUP BY status""").fetchall()
    jobs = conn.execute(
        """SELECT j.*,c.display_name,(SELECT r.error_code FROM creator_handle_requests r
        WHERE r.job_id=j.id ORDER BY r.created_at DESC,r.id DESC LIMIT 1) AS validation_error
        FROM (SELECT DISTINCT ON(contact_id) * FROM creator_handle_jobs
              ORDER BY contact_id,created_at DESC,id DESC) j JOIN contacts c ON c.id=j.contact_id
        ORDER BY j.updated_at DESC,j.id LIMIT 50 OFFSET %s""",
        ((page - 1) * 50,),
    ).fetchall()
    storage = creator_data.cipher(settings)
    try:
        current_handles, version = catalog()
    except (OSError, ValueError, KeyError):
        current_handles = frozenset()
        version = None
    associations = {}
    for row in contact_handles.confirmed_pairs(conn, [j["contact_id"] for j in jobs]):
        associations.setdefault(str(row["contact_id"]), []).append(
            {"handle": row["handle"], "human_confirmed": row["human_confirmed"]}
        )
    for job in jobs:
        job["associations"] = associations.get(str(job["contact_id"]), [])
        job["suggestions"] = []
        if (
            job["result_ciphertext"]
            and job["catalog_version"] == version
            and evidence.still_valid(conn, job["refs"], settings)
        ):
            view = json.loads(storage.decrypt(job["result_ciphertext"]))
            pack = json.loads(storage.decrypt(job["pack_ciphertext"]))
            job["suggestions"] = [
                {
                    **link,
                    "handle": view["handles"][link["h"]],
                }
                for link in view["result"]["links"]
                if view["handles"][link["h"]] in current_handles
            ]
            conflicts = creator_imports._conflicts(
                conn, [(None, job["contact_id"], link["handle"]) for link in job["suggestions"]]
            )
            for link, actual_conflict in zip(job["suggestions"], conflicts, strict=True):
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
                candidate = next(h for h in pack["handles"] if h["handle"] == link["handle"])
                _, claims = evidence._support_groups(pack, candidate)
                link["conflict"] = actual_conflict or bool(evidence._counterevidence(pack, link["handle"], claims))
        rows = {item["handle"]: {**item, "associated": True, "check": None, "conflict": False}
                for item in job["associations"]}
        for link in job["suggestions"]:
            row = rows.setdefault(link["handle"], {"handle": link["handle"], "associated": False,
                                                  "human_confirmed": False, "check": None, "conflict": False})
            # Old paid reproof does not override a live formal fact. Explicit manual
            # reviews and concrete conflicts remain visible on that same handle row.
            if not row["associated"] or job["source"] == "manual" or link["conflict"]:
                row.update(check=link, conflict=link["conflict"])
        job["handle_rows"] = [rows[h] for h in sorted(rows)]
        if (job["source"] != "manual" and job["status"] in {"completed", "review_required"}
                and job["suggestions"] and not job["stats"].get("candidates_omitted")
                and all(rows[link["handle"]]["associated"] and rows[link["handle"]]["check"] is None
                        for link in job["suggestions"])):
            job.update(display_status="completed", display_reason_code="already_confirmed")
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
