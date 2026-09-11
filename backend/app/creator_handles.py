"""Third-step account matching; only current ownership suggestions become candidates."""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import ai_dispatch, contact_assessments
from . import creator_ownership as ownership
from .ai_feature_config import api_key_cipher
from .config import get_settings
from .db import connection

POLICY_VERSION = "creator-handles-v1"
LOGGER = logging.getLogger(__name__)
TASK_LABELS = {
    "pending": "等待取材",
    "preparing": "正在取材",
    "ready": "待分析",
    "queued": "排队中",
    "running": "正在找 handle",
    "succeeded": "待确认",
    "skipped": "待填写 handle",
    "failed": "分析异常，可手动填写",
    "invalid": "材料已变化，需重新核对",
}
REASONS = {
    **ownership.REASONS,
    "parent_changed": "第二步的归属建议已变化，请重新核对负责人。",
    "prepared": "材料已准备，尚未调用 AI。",
    "classified": "已完成账号匹配。",
    "no_handle_material": "没有找到明确的 handle 线索，可手动填写。",
    "no_handle_clues": "没有找到明确的 handle 线索，可手动填写。",
    "no_handle_clue": "没有找到明确的 handle 线索，可手动填写。",
    "not_found": "没有找到可确认的 handle，可手动填写。",
    "account_conflict": "账号或负责人存在冲突，未覆盖已有记录。",
}
RUN_LABELS = {
    "preparing": "正在准备材料",
    "preview": "等待开始",
    "running": "分析中",
    "paused": "已暂停",
    "completed": "分析完成",
}


def _audit(conn, actor, action, metadata):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s)",
        (uuid4(), actor, "creator_handles_" + action, Jsonb(metadata)),
    )


def _owner(conn, actor):
    try:
        ownership._owner(conn, actor)
        return True
    except PermissionError:
        return False


def _access(conn, actor, cm_id=None, *, manage=False):
    actor = ownership._uuid(actor)
    if not conn.execute("SELECT 1 FROM authorizations WHERE id=%s AND status='active'", (actor,)).fetchone():
        raise PermissionError("请重新登录。")
    owner = _owner(conn, actor)
    cm_id = ownership._uuid(cm_id) if cm_id else actor
    if (manage or actor != cm_id) and not owner:
        raise PermissionError("只能处理您自己的达人候选。")
    if not ownership._cm(conn, cm_id):
        raise PermissionError("该负责人的聊天授权不可用，请先完成授权。")
    return cm_id, owner


def _run(conn, actor, run_id, *, manage=False):
    row = conn.execute(
        "SELECT * FROM creator_handle_runs WHERE id=%s FOR UPDATE", (ownership._uuid(run_id),)
    ).fetchone()
    if not row:
        raise LookupError("找不到该批次。")
    _access(conn, actor, row["cm_authorization_id"], manage=manage)
    if row["policy_version"] != POLICY_VERSION:
        raise ValueError("该批次的账号匹配规则已更新，请重新核对。")
    return row


def _task(conn, actor, task_id):
    task_id = ownership._uuid(task_id)
    row = conn.execute("SELECT run_id FROM creator_handle_tasks WHERE id=%s", (task_id,)).fetchone()
    if not row:
        raise LookupError("找不到该候选。")
    run = _run(conn, actor, row["run_id"])
    task = conn.execute("SELECT * FROM creator_handle_tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
    if task["policy_version"] != POLICY_VERSION:
        raise ValueError("该候选的匹配规则已更新。")
    return task, run


def _parent(conn, task, settings, *, full=True):
    parent = conn.execute(
        "SELECT * FROM creator_ownership_tasks WHERE id=%s FOR SHARE", (task["ownership_task_id"],)
    ).fetchone()
    if (
        not parent
        or parent["contact_id"] != task["contact_id"]
        or parent["cm_authorization_id"] != task["cm_authorization_id"]
        or parent["policy_version"] != ownership.POLICY_VERSION
        or parent["status"] != "succeeded"
        or parent["outcome"] != "suggested"
        or parent["confidence"] is None
        or parent["confidence"] < 0.95
    ):
        raise ValueError(REASONS["parent_changed"])
    newer = conn.execute(
        """SELECT 1 FROM creator_ownership_tasks WHERE contact_id=%s
        AND cm_authorization_id=%s AND status='succeeded' AND (created_at,id)>(%s,%s) LIMIT 1""",
        (task["contact_id"], task["cm_authorization_id"], parent["created_at"], parent["id"]),
    ).fetchone()
    if (
        newer
        or not ownership._creator(conn, task["contact_id"])
        or not ownership._cm(conn, task["cm_authorization_id"])
    ):
        raise ValueError(REASONS["parent_changed"])
    if not ownership._valid_material(conn, parent, settings, check_history=full):
        raise ValueError(REASONS["parent_changed"])
    return parent


def _decode(task, key, settings):
    value = json.loads(ownership._cipher(settings).decrypt(task[key])) if task.get(key) else {}
    if key == "result_ciphertext" and "checked" in value:
        return value["checked"] or {}
    return value


def _valid_handle_material(task, settings, *, full=True):
    from .creator_handle_evidence import validate_evidence

    return bool(
        task.get("pack_ciphertext")
        and validate_evidence(
            task["contact_id"],
            task["cm_authorization_id"],
            settings,
            _decode(task, "pack_ciphertext", settings),
            task["refs"],
            task["fingerprint"],
            check_history=full,
        )
    )


def _seed(conn, source):
    """Free candidate intake, never a formal assignment or a paid queue start."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137033))", (str(source["cm_authorization_id"]),))
    run = conn.execute("SELECT * FROM creator_handle_runs WHERE ownership_run_id=%s", (source["id"],)).fetchone()
    candidates = conn.execute(
        """SELECT t.* FROM creator_ownership_tasks t WHERE t.run_id=%s
        AND t.status='succeeded' AND t.outcome='suggested' AND t.confidence>=.95 AND t.policy_version=%s
        AND NOT EXISTS (SELECT 1 FROM creator_handle_tasks h WHERE h.ownership_task_id=t.id)
        AND NOT EXISTS (SELECT 1 FROM creator_ownership_tasks n WHERE n.contact_id=t.contact_id
          AND n.cm_authorization_id=t.cm_authorization_id AND n.status='succeeded'
          AND (n.created_at,n.id)>(t.created_at,t.id))
        AND NOT EXISTS (SELECT 1 FROM contact_assignments a WHERE a.contact_id=t.contact_id AND a.ended_at IS NULL)
        AND NOT EXISTS (SELECT 1 FROM creator_account_assignments a
          WHERE a.contact_id=t.contact_id AND a.ended_at IS NULL)
        ORDER BY t.contact_id""",
        (source["id"], ownership.POLICY_VERSION),
    ).fetchall()
    if not run:
        run_id = uuid4()
        conn.execute(
            """INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,
            policy_version,status) VALUES(%s,%s,%s,%s,%s,'preparing')""",
            (run_id, source["id"], source["cm_authorization_id"], source["requested_by"], POLICY_VERSION),
        )
    else:
        run_id = run["id"]
    added = 0
    for parent in candidates:
        added += conn.execute(
            """INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,
            cm_authorization_id,policy_version) VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(ownership_task_id) DO NOTHING""",
            (uuid4(), run_id, parent["id"], parent["contact_id"], parent["cm_authorization_id"], POLICY_VERSION),
        ).rowcount
    if added and (not run or run["status"] not in ("running", "paused")):
        conn.execute("UPDATE creator_handle_runs SET status='preparing',updated_at=NOW() WHERE id=%s", (run_id,))
    _settle(conn, run_id)
    return run_id


def create_preview(actor_id, cm_id, ownership_run_id=None):
    actor_id = ownership._uuid(actor_id)
    with connection() as conn, conn.transaction():
        cm_id, _ = _access(conn, actor_id, cm_id, manage=True)
        source = conn.execute(
            """SELECT * FROM creator_ownership_runs WHERE cm_authorization_id=%s
            AND policy_version=%s AND status='completed' """
            + ("AND id=%s " if ownership_run_id else "")
            + "ORDER BY created_at DESC,id DESC LIMIT 1",
            (cm_id, ownership.POLICY_VERSION, ownership._uuid(ownership_run_id))
            if ownership_run_id
            else (cm_id, ownership.POLICY_VERSION),
        ).fetchone()
        if not source:
            raise ValueError("请先完成第二步达人归属判断。")
        run_id = _seed(conn, source)
        run = conn.execute("SELECT * FROM creator_handle_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if run["status"] in ("running", "paused"):
            raise ValueError("请先完成当前批次；运行中不会替换正在使用的材料。")
        # An explicit free preview may refresh changed extraction rules/history, but never replay a paid request.
        count = conn.execute(
            """UPDATE creator_handle_tasks SET status='pending',reason_code='',pack_ciphertext=NULL,
            result_ciphertext=NULL,refs='[]',stats='{}',fingerprint=NULL,config_fingerprint=NULL,
            outcome=NULL,confidence=NULL,reused_from=NULL,finished_at=NULL,updated_at=NOW()
            WHERE run_id=%s AND api_requests=0 AND attempts<2 AND review_status='pending'
            AND status IN ('ready','skipped','failed','invalid','succeeded')""",
            (run_id,),
        ).rowcount
        if count:
            conn.execute("UPDATE creator_handle_runs SET status='preparing',updated_at=NOW() WHERE id=%s", (run_id,))
        _audit(conn, actor_id, "preview", {"run_id": str(run_id), "cm_id": str(cm_id)})
        return run_id


def _settle(conn, run_id):
    conn.execute(
        """UPDATE creator_handle_runs r SET status='preview',updated_at=NOW()
        WHERE id=%s AND status='preparing' AND NOT EXISTS (SELECT 1 FROM creator_handle_tasks t
          WHERE t.run_id=r.id AND t.status IN ('pending','preparing'))""",
        (run_id,),
    )
    conn.execute(
        """UPDATE creator_handle_runs r SET status='completed',finished_at=NOW(),updated_at=NOW()
        WHERE id=%s AND status='running' AND NOT EXISTS (SELECT 1 FROM creator_handle_tasks t
          WHERE t.run_id=r.id AND t.status IN ('pending','preparing','queued','running'))""",
        (run_id,),
    )


def _conflict(conn, task, handles):
    if conn.execute(
        """SELECT 1 FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL
        AND (responsible_authorization_id<>%s OR handle_task_id IS DISTINCT FROM %s)
        UNION ALL SELECT 1 FROM creator_account_assignments WHERE contact_id=%s AND ended_at IS NULL LIMIT 1""",
        (task["contact_id"], task["cm_authorization_id"], task["id"], task["contact_id"]),
    ).fetchone():
        return True
    return bool(
        conn.execute(
            """SELECT 1 FROM social_accounts s WHERE s.platform='tiktok'
        AND lower(s.normalized_handle)=ANY(%s::text[]) AND (
          EXISTS (SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
            AND l.status='confirmed' AND (l.contact_id<>%s OR l.relationship<>'owner'))
          OR EXISTS (SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
            AND l.contact_id=%s AND l.status='rejected')
          OR EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
            AND a.ended_at IS NULL AND (a.contact_id<>%s OR a.responsible_authorization_id<>%s))) LIMIT 1""",
            (handles, task["contact_id"], task["contact_id"], task["contact_id"], task["cm_authorization_id"]),
        ).fetchone()
    )


def _admit(conn, task, handles, actor, settings, *, automatic=False):
    from .creator_handle_evidence import normalize_handle, validate_result

    if not isinstance(handles, list) or not 1 <= len(handles) <= 20 or any(not isinstance(v, str) for v in handles):
        raise ValueError("请填写或选择 1–20 个明确的 handle。")
    handles = sorted({normalize_handle(v) for v in handles})
    if not all(handles):
        raise ValueError("handle 格式不正确。")
    if task["review_status"] == "confirmed":
        if handles == sorted(task["selected_handles"]):
            return 0
        raise ValueError("该候选已经入库，不能用重复请求覆盖已确认账号。")
    _parent(conn, task, settings)
    if task["status"] in ("pending", "preparing", "ready", "queued", "running"):
        raise ValueError("请等待本次取材或分析完成。")
    score = None
    if automatic:
        if task["status"] != "succeeded" or not _valid_handle_material(task, settings):
            raise ValueError(REASONS["authorization_changed"])
        result = validate_result(
            ownership._encode(_decode(task, "result_ciphertext", settings)), _decode(task, "pack_ciphertext", settings)
        )
        matched = [
            c for c in result["candidates"] if c["confidence"] >= 0.95 and not c["conflicts"] and not c["missing_info"]
        ]
        if result["outcome"] != "matched" or set(handles) != {c["handle"] for c in matched}:
            raise ValueError("账号匹配尚未满足自动入库条件。")
        score = min(c["confidence"] for c in matched)
    # All writers use sorted account locks, then the person lock. No account can be silently stolen.
    for handle in handles:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + handle,))
    conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (task["contact_id"],))
    # A newer ownership result may have committed while we waited for the person lock.
    _parent(conn, task, settings, full=False)
    if _conflict(conn, task, handles):
        raise ValueError(REASONS["account_conflict"])
    source = "ai_handle_verified" if automatic else "cm_confirmation"
    for handle in handles:
        account = conn.execute(
            """SELECT id FROM social_accounts WHERE platform='tiktok'
            AND lower(normalized_handle)=%s FOR UPDATE""",
            (handle,),
        ).fetchall()
        if len(account) > 1:
            raise ValueError(REASONS["account_conflict"])
        account = (
            account[0]
            if account
            else conn.execute(
                """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
            VALUES(%s,'tiktok',%s,%s) ON CONFLICT(platform,normalized_handle) DO UPDATE
            SET normalized_handle=EXCLUDED.normalized_handle RETURNING id""",
                (uuid4(), handle, handle),
            ).fetchone()
        )
        conn.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,
            reviewed_by_authorization_id,reviewed_at,handle_task_id) VALUES(%s,%s,'owner','confirmed',%s,%s,NOW(),%s)
            ON CONFLICT(contact_id,social_account_id,relationship) DO NOTHING""",
            (task["contact_id"], account["id"], source, None if automatic else actor, task["id"]),
        )
    conn.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,confidence,
        created_by_authorization_id,ownership_task_id,handle_task_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            uuid4(),
            task["contact_id"],
            task["cm_authorization_id"],
            "ai" if automatic else "manual",
            score,
            None if automatic else actor,
            task["ownership_task_id"],
            task["id"],
        ),
    )
    conn.execute(
        """UPDATE creator_handle_tasks SET review_status='confirmed',selected_handles=%s,
        reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s""",
        (handles, None if automatic else actor, task["id"]),
    )
    _audit(
        conn,
        actor,
        "auto_admitted" if automatic else "confirmed",
        {"task_id": str(task["id"]), "cm_id": str(task["cm_authorization_id"]), "accounts": len(handles)},
    )
    return 1


def confirm_task(actor_id, task_id, handles):
    actor_id = ownership._uuid(actor_id)
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        return _admit(conn, task, handles, actor_id, get_settings())


def defer_task(actor_id, task_id):
    actor_id = ownership._uuid(actor_id)
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        if task["review_status"] == "confirmed":
            raise ValueError("已入库的达人不能暂缓。")
        conn.execute(
            "UPDATE creator_handle_tasks SET review_status='deferred',updated_at=NOW() WHERE id=%s", (task["id"],)
        )
        _audit(conn, actor_id, "deferred", {"task_id": str(task["id"])})


def recheck_task(actor_id, task_id):
    """Explicit free refresh of changed material, retaining the paid attempt history."""
    from .creator_handle_evidence import build_evidence

    actor_id = ownership._uuid(actor_id)
    settings = get_settings()
    with connection() as conn, conn.transaction():
        task, run = _task(conn, actor_id, task_id)
        _access(conn, actor_id, task["cm_authorization_id"], manage=True)
        if run["status"] not in ("preview", "completed") or run["auto_admit"]:
            raise ValueError("请先完成当前批次并关闭自动入库，再更新材料。")
        if (
            task["review_status"] == "confirmed"
            or task["status"] not in ("succeeded", "skipped", "failed", "invalid")
            or task["attempts"] >= 2
        ):
            raise ValueError("该候选不能重测：已入库、仍在处理或已达到两次尝试上限。")
        _parent(conn, task, settings)
        material = build_evidence(task["contact_id"], task["cm_authorization_id"], settings)
        if material["fingerprint"] == task["fingerprint"]:
            raise ValueError("材料没有变化，不重复调用 AI；可以直接人工核对 handle。")
        _audit(
            conn,
            actor_id,
            "recheck_requested",
            {
                "task_id": str(task["id"]),
                "previous_task_ciphertext": ownership._cipher(settings).encrypt(ownership._encode(task)),
            },
        )
        conn.execute(
            """UPDATE creator_handle_tasks SET status='pending',reason_code='',fingerprint=NULL,
            config_fingerprint=NULL,pack_ciphertext=NULL,result_ciphertext=NULL,refs='[]',stats='{}',
            outcome=NULL,confidence=NULL,reused_from=NULL,review_status='pending',selected_handles='{}',
            reviewed_by=NULL,reviewed_at=NULL,lease_until=NULL,scheduled_at=NOW(),started_at=NULL,finished_at=NULL,
            response_id=NULL,request_id=NULL,updated_at=NOW() WHERE id=%s""",
            (task["id"],),
        )
        conn.execute(
            "UPDATE creator_handle_runs SET status='preparing',finished_at=NULL,updated_at=NOW() WHERE id=%s",
            (run["id"],),
        )
        return run["id"]


def _auto_next(conn, settings):
    row = conn.execute(
        """SELECT r.* FROM creator_handle_runs r WHERE r.auto_admit AND r.policy_version=%s
        AND r.status<>'paused'
        AND EXISTS (SELECT 1 FROM creator_handle_tasks t WHERE t.run_id=r.id AND t.status='succeeded'
          AND t.outcome='matched' AND t.review_status='pending') ORDER BY r.created_at
        FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
        (POLICY_VERSION,),
    ).fetchone()
    if not row:
        return False
    task = conn.execute(
        """SELECT * FROM creator_handle_tasks WHERE run_id=%s AND status='succeeded'
        AND outcome='matched' AND review_status='pending' ORDER BY created_at,id FOR UPDATE LIMIT 1""",
        (row["id"],),
    ).fetchone()
    try:
        with conn.transaction():
            ownership._owner(conn, row["requested_by"])
            result = _decode(task, "result_ciphertext", settings)
            _admit(
                conn, task, [c["handle"] for c in result["candidates"]], row["requested_by"], settings, automatic=True
            )
    except (ValueError, PermissionError):
        conn.execute(
            """UPDATE creator_handle_tasks SET outcome='conflict',reason_code='account_conflict',
            updated_at=NOW() WHERE id=%s""",
            (task["id"],),
        )
    return True


def prepare_next_preview(settings):
    from .creator_handle_evidence import build_evidence

    with connection() as conn, conn.transaction():
        if _auto_next(conn, settings):
            return True
        # Seed only owner-requested, completed current-policy suggestions; this performs no paid request.
        sources = conn.execute(
            """SELECT DISTINCT ON (r.cm_authorization_id) r.* FROM creator_ownership_runs r
            JOIN admin_roles a ON a.authorization_id=r.requested_by AND a.role='owner' AND a.is_active
            JOIN authorizations requester ON requester.id=r.requested_by AND requester.status='active'
            WHERE r.status='completed' AND r.policy_version=%s
            ORDER BY r.cm_authorization_id,r.created_at DESC,r.id DESC""",
            (ownership.POLICY_VERSION,),
        ).fetchall()
        for source in sources:
            if ownership._cm(conn, source["cm_authorization_id"]):
                _seed(conn, source)
        conn.execute("""UPDATE creator_handle_tasks SET status='pending',lease_until=NULL
            WHERE status='preparing' AND lease_until<NOW()""")
        job = conn.execute(
            """SELECT t.*,r.requested_by FROM creator_handle_tasks t JOIN creator_handle_runs r
            ON r.id=t.run_id WHERE t.status='pending' AND r.status IN ('preparing','running','paused')
            AND t.policy_version=%s ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION,),
        ).fetchone()
        if not job:
            return False
        conn.execute(
            "UPDATE creator_handle_tasks SET status='preparing',lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s",
            (job["id"],),
        )
    try:
        with connection() as conn:
            ownership._owner(conn, job["requested_by"])
            _parent(conn, job, settings)
        material = build_evidence(job["contact_id"], job["cm_authorization_id"], settings)
        with connection() as conn, conn.transaction():
            conn.execute("SELECT id FROM creator_handle_runs WHERE id=%s FOR UPDATE", (job["run_id"],))
            current = conn.execute(
                "SELECT * FROM creator_handle_tasks WHERE id=%s AND status='preparing' FOR UPDATE", (job["id"],)
            ).fetchone()
            if not current:
                return True
            _parent(conn, current, settings, full=False)
            config = ownership._config(conn)
            config_fp = ownership._config_fingerprint(config) if config else None
            cached = conn.execute(
                """SELECT * FROM creator_handle_tasks WHERE contact_id=%s AND cm_authorization_id=%s
                AND policy_version=%s AND fingerprint=%s AND config_fingerprint=%s AND status='succeeded'
                AND result_ciphertext IS NOT NULL ORDER BY finished_at DESC LIMIT 1""",
                (job["contact_id"], job["cm_authorization_id"], POLICY_VERSION, material["fingerprint"], config_fp),
            ).fetchone()
            eligible = material["eligible"]
            conn.execute(
                """UPDATE creator_handle_tasks SET status=%s,reason_code=%s,fingerprint=%s,config_fingerprint=%s,
                pack_ciphertext=%s,refs=%s,stats=%s,result_ciphertext=%s,outcome=%s,confidence=%s,reused_from=%s,
                lease_until=NULL,finished_at=CASE WHEN %s THEN NOW() ELSE NULL END,updated_at=NOW() WHERE id=%s""",
                (
                    "succeeded" if cached and eligible else "ready" if eligible else "skipped",
                    "reused" if cached and eligible else "prepared" if eligible else material["reason_code"],
                    material["fingerprint"],
                    config_fp,
                    ownership._cipher(settings).encrypt(ownership._encode(material["pack"])),
                    Jsonb(json.loads(ownership._encode(material["refs"]))),
                    Jsonb(material["stats"]),
                    cached["result_ciphertext"] if cached and eligible else None,
                    cached["outcome"] if cached and eligible else None if eligible else "not_found",
                    cached["confidence"] if cached and eligible else None,
                    cached["id"] if cached and eligible else None,
                    bool(cached or not eligible),
                    job["id"],
                ),
            )
            _settle(conn, job["run_id"])
    except Exception:
        LOGGER.warning("handle preparation failed task=%s", job["id"])
        with connection() as conn, conn.transaction():
            conn.execute("SELECT id FROM creator_handle_runs WHERE id=%s FOR UPDATE", (job["run_id"],))
            conn.execute(
                """UPDATE creator_handle_tasks SET status='failed',reason_code='preparation_failed',
                lease_until=NULL,finished_at=NOW() WHERE id=%s AND status='preparing'""",
                (job["id"],),
            )
            _settle(conn, job["run_id"])
    return True


def start_run(actor_id, run_id):
    actor_id = ownership._uuid(actor_id)
    with connection() as conn, conn.transaction():
        run = _run(conn, actor_id, run_id, manage=True)
        if run["status"] == "running":
            return 0
        if run["status"] not in ("preview", "completed"):
            raise ValueError("请等材料准备完成，暂停的批次请点击继续。")
        if conn.execute(
            """SELECT 1 FROM creator_handle_tasks current JOIN creator_handle_tasks previous
            ON previous.contact_id=current.contact_id
            AND previous.cm_authorization_id=current.cm_authorization_id
            WHERE current.run_id=%s AND current.status='ready' AND previous.run_id<>current.run_id
            AND previous.status IN ('queued','running') LIMIT 1""",
            (run["id"],),
        ).fetchone():
            raise ValueError("同一达人还有旧批次在排队或分析，请先结束旧批次，避免重复调用。")
        config = ownership._config(conn)
        if not config:
            raise ValueError(REASONS["configuration_missing"])
        config_fp = ownership._config_fingerprint(config)
        conn.execute(
            """UPDATE creator_handle_tasks SET status='invalid',reason_code='configuration_changed'
            WHERE run_id=%s AND status='ready' AND config_fingerprint IS DISTINCT FROM %s
            AND config_fingerprint IS NOT NULL""",
            (run["id"], config_fp),
        )
        count = conn.execute(
            """UPDATE creator_handle_tasks SET status='queued',config_fingerprint=%s,
            scheduled_at=NOW(),updated_at=NOW() WHERE run_id=%s AND status='ready'""",
            (config_fp, run["id"]),
        ).rowcount
        conn.execute(
            """UPDATE creator_handle_runs SET status='running',started_at=COALESCE(started_at,NOW()),
            updated_at=NOW() WHERE id=%s""",
            (run["id"],),
        )
        _settle(conn, run["id"])
        _audit(conn, actor_id, "started", {"run_id": str(run["id"]), "queued": count})
        return count


def pause_run(actor_id, run_id):
    with connection() as conn, conn.transaction():
        run = _run(conn, actor_id, run_id, manage=True)
        if run["status"] == "running":
            conn.execute("UPDATE creator_handle_runs SET status='paused',updated_at=NOW() WHERE id=%s", (run["id"],))
            _audit(conn, ownership._uuid(actor_id), "paused", {"run_id": str(run["id"])})


def resume_run(actor_id, run_id):
    with connection() as conn, conn.transaction():
        run = _run(conn, actor_id, run_id, manage=True)
        if run["status"] == "paused":
            conn.execute("UPDATE creator_handle_runs SET status='running',updated_at=NOW() WHERE id=%s", (run["id"],))
            _settle(conn, run["id"])
            _audit(conn, ownership._uuid(actor_id), "resumed", {"run_id": str(run["id"])})


def set_auto_admit(actor_id, run_id, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("自动入库设置不正确。")
    with connection() as conn, conn.transaction():
        run = _run(conn, actor_id, run_id, manage=True)
        conn.execute("UPDATE creator_handle_runs SET auto_admit=%s,updated_at=NOW() WHERE id=%s", (enabled, run["id"]))
        _audit(conn, ownership._uuid(actor_id), "auto_admit_changed", {"run_id": str(run["id"]), "enabled": enabled})


def claim_next_handle():
    with connection() as conn, conn.transaction():
        for row in conn.execute("""SELECT DISTINCT run_id FROM creator_handle_tasks
            WHERE status='running' AND lease_until<NOW() ORDER BY run_id""").fetchall():
            if not conn.execute(
                "SELECT id FROM creator_handle_runs WHERE id=%s FOR UPDATE SKIP LOCKED", (row["run_id"],)
            ).fetchone():
                continue
            conn.execute(
                """UPDATE creator_handle_tasks SET status='failed',reason_code='interrupted',
                lease_until=NULL,finished_at=NOW() WHERE run_id=%s AND status='running' AND lease_until<NOW()""",
                (row["run_id"],),
            )
            _settle(conn, row["run_id"])
        if not ai_dispatch.can_claim(conn):
            return None
        task = conn.execute(
            """SELECT t.*,r.requested_by FROM creator_handle_tasks t JOIN creator_handle_runs r ON r.id=t.run_id
            WHERE r.status='running' AND t.status='queued' AND t.attempts<2 AND t.scheduled_at<=NOW()
            AND t.policy_version=%s ORDER BY t.scheduled_at,t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION,),
        ).fetchone()
        if not task:
            return None
        conn.execute(
            """UPDATE creator_handle_tasks SET status='running',attempts=attempts+1,
            lease_until=NOW()+INTERVAL '10 minutes',started_at=NOW(),updated_at=NOW() WHERE id=%s""",
            (task["id"],),
        )
        return {**task, "status": "running", "attempts": task["attempts"] + 1}


def _current_paid(conn, job):
    conn.execute("SELECT id FROM creator_handle_runs WHERE id=%s FOR UPDATE", (job["run_id"],))
    return conn.execute(
        """SELECT t.*,r.status AS run_status,r.requested_by FROM creator_handle_tasks t
        JOIN creator_handle_runs r ON r.id=t.run_id WHERE t.id=%s AND t.status='running'
        AND t.attempts=%s FOR UPDATE OF t""",
        (job["id"], job["attempts"]),
    ).fetchone()


def _finish(job, status, reason, settings, response=None, result=None):
    with connection() as conn, conn.transaction():
        current = _current_paid(conn, job)
        if not current:
            return
        if result:
            try:
                ownership._owner(conn, current["requested_by"])
                _parent(conn, current, settings)
                config = ownership._config(conn)
                if (
                    not config
                    or ownership._config_fingerprint(config) != current["config_fingerprint"]
                    or current["run_status"] not in ("running", "paused")
                    or not _valid_handle_material(current, settings)
                ):
                    raise ValueError("材料已变化")
            except (ValueError, PermissionError):
                status, reason, result = "invalid", "authorization_changed", None
        usage = (response or {}).get("usage", {})
        telemetry = (response or {}).get("telemetry", {})
        score = min((c["confidence"] for c in result["candidates"]), default=None) if result else None
        stored_result = None
        if response and response.get("text"):
            try:
                model_output = json.loads(response["text"])
            except (ValueError, TypeError):
                model_output = response["text"]
            stored_result = {"model_output": model_output, "checked": result}
        elif result:
            stored_result = {"model_output": None, "checked": result}
        conn.execute(
            """UPDATE creator_handle_tasks SET status=%s,reason_code=%s,result_ciphertext=%s,outcome=%s,
            confidence=%s,response_id=COALESCE(%s,response_id),request_id=COALESCE(%s,request_id),
            input_tokens=input_tokens+%s,output_tokens=output_tokens+%s,lease_until=NULL,finished_at=NOW(),updated_at=NOW()
            WHERE id=%s""",
            (
                status,
                reason,
                ownership._cipher(settings).encrypt(ownership._encode(stored_result)) if stored_result else None,
                result["outcome"] if result else None,
                score,
                (response or {}).get("response_id"),
                telemetry.get("openai_request_id"),
                usage.get("input_tokens") or 0,
                usage.get("output_tokens") or 0,
                job["id"],
            ),
        )
        _settle(conn, job["run_id"])


def run_claimed_handle(job, settings):
    from .creator_handle_evidence import INSTRUCTIONS, output_format, validate_result

    response = None
    try:
        with connection() as conn, conn.transaction():
            task = _current_paid(conn, job)
            if not task:
                return
            ownership._owner(conn, task["requested_by"])
            if task["run_status"] == "paused":
                conn.execute(
                    "UPDATE creator_handle_tasks SET status='queued',attempts=attempts-1,lease_until=NULL WHERE id=%s",
                    (task["id"],),
                )
                return
            _parent(conn, task, settings)
            config = ownership._config(conn)
            if not config or ownership._config_fingerprint(config) != task["config_fingerprint"]:
                raise ValueError("配置已变化")
            if task["run_status"] != "running" or not _valid_handle_material(task, settings):
                raise ValueError("材料已变化")
            pack = _decode(task, "pack_ciphertext", settings)
            key = api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
            conn.execute("UPDATE creator_handle_tasks SET api_requests=api_requests+1 WHERE id=%s", (task["id"],))
        response = contact_assessments.call_openai(
            config,
            key,
            pack,
            job["contact_id"],
            instructions=INSTRUCTIONS,
            output_format=output_format(),
            max_output_tokens=2200,
        )
        result = validate_result(response["text"], pack)
        _finish(job, "succeeded", "classified", settings, response, result)
    except contact_assessments.OpenAIRequestError as exc:
        if exc.is_quota:
            ai_dispatch.pause_for_quota()
        if exc.retryable and job["attempts"] < 2:
            delay = min(max(exc.retry_after_seconds or 5, 1), 3600)
            ai_dispatch.throttle(delay)
            with connection() as conn, conn.transaction():
                if _current_paid(conn, job):
                    conn.execute(
                        """UPDATE creator_handle_tasks SET status='queued',reason_code='rate_limited',lease_until=NULL,
                        scheduled_at=NOW()+(%s*INTERVAL '1 second'),request_id=%s WHERE id=%s""",
                        (delay, exc.telemetry.get("openai_request_id"), job["id"]),
                    )
        else:
            _finish(job, "failed", "provider_error", settings, {"telemetry": exc.telemetry})
    except (ValueError, PermissionError, TypeError, KeyError):
        _finish(job, "invalid", "invalid_result" if response else "authorization_changed", settings, response)
    except Exception:
        LOGGER.warning("handle request failed task=%s", job["id"])
        _finish(job, "failed", "provider_error", settings, response)


def page_data(actor_id, cm_id=None, tab="candidates", query="", page=1):
    settings = get_settings()
    with connection() as conn:
        cm_id, owner = _access(conn, actor_id, cm_id)
        cms = [
            {"id": str(r["id"]), "name": r["display_name"] or "未命名用户"}
            for r in conn.execute(
                """SELECT id,display_name FROM authorizations WHERE status='active'
                AND body_consent_at IS NOT NULL ORDER BY display_name,id"""
            ).fetchall()
            if owner or r["id"] == cm_id
        ]
        run = conn.execute(
            "SELECT * FROM creator_handle_runs WHERE cm_authorization_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (cm_id,),
        ).fetchone()
        counts = {key: 0 for key in (*TASK_LABELS, "total", "completed", "candidates", "confirmed")}
        usage = {"api_requests": 0, "input_tokens": 0, "output_tokens": 0}
        if run:
            for row in conn.execute(
                "SELECT status,count(*) n FROM creator_handle_tasks WHERE run_id=%s GROUP BY status", (run["id"],)
            ).fetchall():
                counts[row["status"]] = row["n"]
                counts["total"] += row["n"]
            counts["completed"] = counts["succeeded"]
            usage = conn.execute(
                """SELECT coalesce(sum(api_requests),0) api_requests,coalesce(sum(input_tokens),0) input_tokens,
                coalesce(sum(output_tokens),0) output_tokens FROM creator_handle_tasks WHERE run_id=%s""",
                (run["id"],),
            ).fetchone()
        candidate_base = """FROM creator_handle_tasks t JOIN contacts c ON c.id=t.contact_id
            JOIN creator_ownership_tasks p ON p.id=t.ownership_task_id
            WHERE t.cm_authorization_id=%s AND t.review_status<>'confirmed' AND t.policy_version=%s
            AND p.policy_version=%s AND p.status='succeeded' AND p.outcome='suggested' AND p.confidence>=.95
            AND NOT EXISTS (SELECT 1 FROM creator_ownership_tasks n WHERE n.contact_id=p.contact_id
              AND n.cm_authorization_id=p.cm_authorization_id AND n.status='succeeded'
              AND (n.created_at,n.id)>(p.created_at,p.id))
            AND NOT EXISTS (SELECT 1 FROM contact_assignments a
              WHERE a.contact_id=t.contact_id AND a.ended_at IS NULL)
            AND NOT EXISTS (SELECT 1 FROM creator_account_assignments a
              WHERE a.contact_id=t.contact_id AND a.ended_at IS NULL)"""
        candidate_args = [cm_id, POLICY_VERSION, ownership.POLICY_VERSION]
        # Preserve previously confirmed account-level records, without duplicating people.
        formal_base = """FROM (SELECT DISTINCT ON(contact_id) * FROM (
            SELECT id,contact_id,handle_task_id,0 AS priority FROM contact_assignments
            WHERE responsible_authorization_id=%s AND ended_at IS NULL
            UNION ALL SELECT a.id,a.contact_id,NULL::uuid AS handle_task_id,1 AS priority
            FROM creator_account_assignments a WHERE a.responsible_authorization_id=%s AND a.ended_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM contact_assignments p
                WHERE p.contact_id=a.contact_id AND p.ended_at IS NULL)
            ) assignments ORDER BY contact_id,priority,id) a JOIN contacts c ON c.id=a.contact_id
            LEFT JOIN creator_handle_tasks t ON t.id=a.handle_task_id WHERE TRUE"""
        formal_args = [cm_id, cm_id]
        counts["candidates"] = conn.execute("SELECT count(*) n " + candidate_base, candidate_args).fetchone()["n"]
        counts["confirmed"] = conn.execute("SELECT count(*) n " + formal_base, formal_args).fetchone()["n"]
        query = str(query).strip()[:120]
        page = max(1, int(page))
        if tab not in ("candidates", "confirmed", "mine"):
            tab = "candidates"
        if tab == "candidates":
            base, args = candidate_base, candidate_args.copy()
            columns = "t.*,c.display_name"
        else:
            base, args = formal_base, formal_args.copy()
            columns = "t.*,a.id AS assignment_id,a.contact_id AS formal_contact_id,c.display_name"
        if query:
            base += " AND c.display_name ILIKE %s"
            args.append("%" + query + "%")
        total = conn.execute("SELECT count(*) n " + base, args).fetchone()["n"]
        pages = max(1, (total + 24) // 25)
        page = min(page, pages)
        rows = conn.execute(
            "SELECT " + columns + " " + base + " ORDER BY c.display_name,c.id LIMIT 25 OFFSET %s",
            [*args, (page - 1) * 25],
        ).fetchall()
        items = []
        for row in rows:
            contact_id = row.get("formal_contact_id") or row["contact_id"]
            identity = conn.execute(
                "SELECT open_id FROM lark_identities WHERE contact_id=%s ORDER BY last_seen_at DESC LIMIT 1",
                (contact_id,),
            ).fetchone()
            valid_parent = False
            if row.get("ownership_task_id"):
                try:
                    _parent(conn, row, settings, full=False)
                    valid_parent = True
                except (ValueError, PermissionError):
                    pass
            # Polling checks only cited material, never rescans the full message history.
            # Hide private derived suggestions as soon as their source is no longer readable.
            readable = valid_parent and _valid_handle_material(row, settings, full=False)
            result = _decode(row, "result_ciphertext", settings) if readable else {}
            options = [{"handle": c["handle"], "confidence": c["confidence"]} for c in result.get("candidates", [])]
            handles = row.get("selected_handles") or []
            if tab != "candidates":
                handles = [
                    r["display_handle"]
                    for r in conn.execute(
                        """SELECT s.display_handle FROM social_accounts s WHERE s.id IN (
                            SELECT l.social_account_id FROM contact_social_accounts l
                            WHERE l.contact_id=%s AND l.status='confirmed' AND l.relationship='owner'
                            UNION SELECT a.social_account_id FROM creator_account_assignments a
                            WHERE a.contact_id=%s AND a.responsible_authorization_id=%s AND a.ended_at IS NULL
                        ) ORDER BY s.normalized_handle""",
                        (contact_id, contact_id, cm_id),
                    ).fetchall()
                ]
            status = row.get("status")
            reason = (
                REASONS["account_conflict"]
                if row.get("outcome") == "conflict"
                else result.get("reason") or REASONS.get(row.get("reason_code"), "等待核对 handle。")
            )
            if result.get("outcome") == "review":
                notes = list(
                    dict.fromkeys(
                        note
                        for candidate in result.get("candidates", [])
                        for note in [*candidate.get("conflicts", []), *candidate.get("missing_info", [])]
                    )
                )
                if notes:
                    reason = "待核对：" + "；".join(notes[:3])
            if tab == "candidates" and not valid_parent:
                reason = REASONS["parent_changed"]
            elif row.get("pack_ciphertext") and not readable:
                reason = "匹配材料已变化，请重新核对；不会自动入库。"
            elif tab != "candidates" and not row.get("handle_task_id") and not row.get("ownership_task_id"):
                reason = "原有正式记录，本轮未改动。"
            task_run = None
            if owner and tab == "candidates" and row.get("run_id"):
                task_run = (
                    run
                    if run and run["id"] == row["run_id"]
                    else conn.execute("SELECT * FROM creator_handle_runs WHERE id=%s", (row["run_id"],)).fetchone()
                )
            items.append(
                {
                    "id": str(row.get("id") or row.get("assignment_id")),
                    "display_name": row["display_name"],
                    "open_id": identity["open_id"] if identity else None,
                    "handle_options": options,
                    "handles": handles,
                    "status_label": "已入库"
                    if tab != "candidates"
                    else "已暂缓"
                    if row["review_status"] == "deferred"
                    else "存在冲突"
                    if row.get("outcome") == "conflict"
                    else "匹配通过"
                    if row.get("outcome") == "matched" and readable
                    else TASK_LABELS.get(status, "待确认"),
                    "reason": reason,
                    "can_confirm": tab == "candidates"
                    and valid_parent
                    and status in ("succeeded", "skipped", "failed", "invalid"),
                    "can_defer": tab == "candidates",
                    "can_recheck": bool(
                        task_run
                        and task_run["status"] in ("preview", "completed")
                        and not task_run["auto_admit"]
                        and valid_parent
                        and row["review_status"] != "confirmed"
                        and status in ("succeeded", "skipped", "failed", "invalid")
                        and row["attempts"] < 2
                    ),
                    "has_evidence": readable,
                }
            )
        return {
            "cms": cms,
            "selected_cm_id": str(cm_id),
            "can_manage": owner,
            "run": None
            if not run
            else {
                "id": str(run["id"]),
                "status": run["status"],
                "status_label": RUN_LABELS[run["status"]],
                "auto_admit": run["auto_admit"],
                "can_start": owner and run["status"] in ("preview", "completed") and counts["ready"] > 0,
                "can_pause": owner and run["status"] == "running",
                "can_resume": owner and run["status"] == "paused",
            },
            "counts": counts,
            "usage": usage,
            "items": items,
            "total_count": total,
            "total_pages": pages,
            "page": page,
        }


def task_evidence(actor_id, task_id):
    settings = get_settings()
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        _parent(conn, task, settings)
        if not _valid_handle_material(task, settings):
            raise ValueError("账号匹配材料已变化，请重新核对。")
        pack = _decode(task, "pack_ciphertext", settings)
        result = _decode(task, "result_ciphertext", settings)
        person = conn.execute("SELECT display_name FROM contacts WHERE id=%s", (task["contact_id"],)).fetchone()
        cm = ownership._cm(conn, task["cm_authorization_id"])
        if not person or not cm:
            raise ValueError("人员或负责人授权已变化，请重新核对。")
        messages = []
        for group in pack.get("groups", []):
            messages.append(
                {
                    "ref": group.get("ref"),
                    "sender_name": "群名",
                    "time_label": "当前观察值",
                    "chat_name": "",
                    "text": group.get("name") or group.get("title") or "—",
                    "available": True,
                }
            )
        for message in pack.get("messages", []):
            messages.append(
                {
                    "ref": message.get("ref"),
                    "sender_name": person["display_name"]
                    if message.get("speaker") == "U1"
                    else cm["display_name"]
                    if message.get("speaker") == "CM1"
                    else "其他成员",
                    "time_label": message.get("time", ""),
                    "chat_name": message.get("conversation", ""),
                    "text": message.get("text", ""),
                    "available": True,
                }
            )
        _audit(conn, ownership._uuid(actor_id), "evidence_viewed", {"task_id": str(task["id"])})
        return {
            "display_name": person["display_name"],
            "cm_name": cm["display_name"],
            "summary": result.get("reason", "账号匹配材料"),
            "messages": messages,
        }
