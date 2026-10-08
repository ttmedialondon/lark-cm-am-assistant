"""Creator × CM cooperation facts, durable incremental extraction and rule-based ranking."""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import ai_dispatch, ai_feature_config, creator_access, creator_data, offhours_summary
from . import creator_importance_facts as facts
from . import openai_client as api
from .config import get_settings
from .crypto import TokenCipher, message_body_cipher, message_body_fingerprint
from .db import connection
from .message_mentions import mention_names, mention_targets

FEATURE = "creator_importance"
HISTORY_DAYS = 90
MAX_MESSAGES = 80
MAX_CHARS = 24_000
TIERS = {"focus": "重点", "normal": "常规", "pending": "待评估"}
PHASES = {"ongoing": "持续合作", "single": "单次合作", "history": "历史合作", "intent": "具体意向",
          "unknown": "资料不足"}
LONDON = ZoneInfo("Europe/London")


def _cipher(settings):
    key = hmac.digest(settings.token_encryption_key, b"creator-importance/key/v1", "sha256")
    return TokenCipher(key, b"creator-importance/facts/v1")


def _hash(value):
    return hashlib.sha256(creator_data.encode(value).encode()).hexdigest()


def _config(conn, lock=False):
    return conn.execute("SELECT feature,provider,project_id,model,enabled,api_key_ciphertext "
                        "FROM ai_feature_configs WHERE feature=%s" + (" FOR UPDATE" if lock else ""),
                        (FEATURE,)).fetchone()


def _enabled(config):
    return bool(config and config["enabled"] and config["provider"] == "openai" and config["api_key_ciphertext"])


def _config_hash(config):
    return _hash({k: config.get(k) for k in ("provider", "project_id", "model", "api_key_ciphertext")}
                 | {"prompt": facts.PROMPT_VERSION})


# The same predicate protects SQL ordering and decrypted detail reads. It never reads another CM's observation.
_VALID_SOURCE = """EXISTS(SELECT 1 FROM chat_messages m
    JOIN authorization_message_observations o USING(chat_id,message_id)
    JOIN authorization_chats ac ON ac.authorization_id=o.authorization_id AND ac.chat_id=m.chat_id
    JOIN authorizations a ON a.id=o.authorization_id JOIN chats ch ON ch.chat_id=m.chat_id
    LEFT JOIN lark_identities i ON i.open_id=m.sender_open_id
    WHERE o.authorization_id=p.responsible_authorization_id
      AND m.chat_id=s->>'chat_id' AND m.message_id=s->>'message_id'
      AND a.status='active' AND a.body_consent_at=(s->>'consent_at')::timestamptz
      AND o.observed_at>=a.body_consent_at AND ac.is_visible AND ac.sync_enabled
      AND COALESCE(ch.chat_status,'normal')='normal' AND NOT m.is_deleted AND m.body_ciphertext IS NOT NULL
      AND m.body_fingerprint=s->>'body_fingerprint' AND m.sender_open_id=s->>'sender_open_id'
      AND m.sender_type='user'
      AND m.message_type IS NOT DISTINCT FROM s->>'message_type'
      AND m.message_create_time IS NOT DISTINCT FROM (s->>'created_at')::timestamptz
      AND m.parent_id IS NOT DISTINCT FROM s->>'parent_id' AND m.root_id IS NOT DISTINCT FROM s->>'root_id'
      AND m.thread_id IS NOT DISTINCT FROM s->>'thread_id'
      AND md5(m.mentions_json::text) IS NOT DISTINCT FROM s->>'mentions_fingerprint'
      AND (m.sender_open_id=a.open_id OR i.contact_id=p.contact_id AND i.identity_type='user'))"""
_PROFILE_VALID = "creator_importance_is_eligible(p.contact_id,p.responsible_authorization_id) AND p.auto_valid " \
                 "AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.sources) s WHERE NOT " + _VALID_SOURCE + ")"


def roster_sql(cm_id):
    join = """ LEFT JOIN LATERAL(SELECT
        COALESCE(p.manual_tier,CASE WHEN """ + _PROFILE_VALID + """ THEN p.auto_tier END,'pending') effective_tier,
        CASE WHEN """ + _PROFILE_VALID + """ THEN CASE p.phase WHEN 'ongoing' THEN 0 WHEN 'single' THEN 1
          WHEN 'history' THEN 2 WHEN 'intent' THEN 3 ELSE 4 END ELSE 4 END phase_rank,
        CASE WHEN """ + _PROFILE_VALID + """ THEN p.last_substantive_progress_at END last_substantive_progress_at
        FROM creator_importance_profiles p WHERE p.contact_id=c.id AND p.responsible_authorization_id=%s
        AND creator_importance_is_eligible(p.contact_id,p.responsible_authorization_id)
      ) ip ON TRUE """
    return join, [cm_id]


def tier_order(conn, contact_ids, cm_id):
    if not contact_ids:
        return {}
    join, args = roster_sql(cm_id)
    rows = conn.execute("SELECT c.id,COALESCE(ip.effective_tier,'pending') tier FROM contacts c " + join +
                        " WHERE c.id=ANY(%s::uuid[])", [*args, sorted(set(contact_ids))]).fetchall()
    return {str(row["id"]): {"focus": 0, "normal": 1, "pending": 2}[row["tier"]] for row in rows}


def _source_checks(conn, refs, contact_id, cm_id, *, lock=False):
    if not refs:
        return {}
    if lock:
        # Sources precede jobs/profiles: source-update triggers take the same direction.
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR SHARE", (cm_id,)).fetchall()
        conn.execute("SELECT chat_id FROM authorization_chats WHERE authorization_id=%s "
                     "AND chat_id=ANY(%s::text[]) ORDER BY chat_id FOR SHARE",
                     (cm_id, sorted({r["chat_id"] for r in refs}))).fetchall()
        conn.execute("SELECT open_id FROM lark_identities WHERE open_id=ANY(%s::text[]) ORDER BY open_id FOR SHARE",
                     (sorted({r["sender_open_id"] for r in refs}),)).fetchall()
        conn.execute("SELECT contact_id FROM contact_roles WHERE contact_id=%s ORDER BY role FOR SHARE",
                     (contact_id,)).fetchall()
        for table in ("contact_assignments", "creator_account_assignments"):
            conn.execute(f"SELECT id FROM {table} WHERE contact_id=%s AND responsible_authorization_id=%s "
                         "ORDER BY id FOR SHARE", (contact_id, cm_id)).fetchall()
        conn.execute("""SELECT m.message_id FROM chat_messages m JOIN jsonb_to_recordset(%s::jsonb)
            AS r(chat_id text,message_id text) ON r.chat_id=m.chat_id AND r.message_id=m.message_id
            ORDER BY m.chat_id,m.message_id FOR SHARE OF m""", (creator_data.encode(refs),)).fetchall()
    rows = conn.execute("SELECT s->>'ref' ref," + _VALID_SOURCE + " valid FROM jsonb_array_elements(%s::jsonb) s "
                        "CROSS JOIN (SELECT %s::uuid contact_id,%s::uuid responsible_authorization_id) p",
                        (creator_data.encode(refs), contact_id, cm_id)).fetchall()
    return {r["ref"]: r["valid"] for r in rows}


def _state(job, settings):
    return json.loads(_cipher(settings).decrypt(job["checkpoint_ciphertext"])) if job["checkpoint_ciphertext"] else {
        "facts": [], "sources": {}, "seen": {}, "unreadable": 0, "gaps": 0,
    }


def _prune(conn, state, job):
    valid = _source_checks(conn, list(state["sources"].values()), job["contact_id"],
                           job["responsible_authorization_id"])
    invalid_keys = {f["key"] for f in state["facts"] if not all(valid.get(ref, False) for ref in f["refs"])}
    kept = [f for f in state["facts"] if f["key"] not in invalid_keys]
    state["gaps"] += len(state["facts"]) - len(kept)
    state["facts"] = kept
    state["sources"] = {r: s for r, s in state["sources"].items() if valid.get(r)}
    return state


def _save_profile(conn, job, state, settings, *, cutoff=None, history_complete=False):
    selected = {ref for item in state["facts"] for ref in item["refs"]}
    sources = [s for ref, s in state["sources"].items() if ref in selected]
    result = facts.rank(state["facts"])
    conn.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,
        auto_tier,phase,last_substantive_progress_at,facts_ciphertext,sources,auto_valid,coverage,
        window_start,cutoff,evaluated_at,prompt_version)
        VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,NOW(),%s)
        ON CONFLICT(contact_id,responsible_authorization_id) DO UPDATE SET auto_tier=EXCLUDED.auto_tier,
        phase=EXCLUDED.phase,last_substantive_progress_at=EXCLUDED.last_substantive_progress_at,
        facts_ciphertext=EXCLUDED.facts_ciphertext,sources=EXCLUDED.sources,auto_valid=EXCLUDED.auto_valid,
        coverage=EXCLUDED.coverage,window_start=EXCLUDED.window_start,cutoff=EXCLUDED.cutoff,
        evaluated_at=NOW(),prompt_version=EXCLUDED.prompt_version,revision=creator_importance_profiles.revision+1""",
        (job["contact_id"], job["responsible_authorization_id"], result["tier"], result["phase"],
         result.get("last_progress_at"), _cipher(settings).encrypt(creator_data.encode(state["facts"])),
         creator_data.encode(sources), history_complete, creator_data.encode({"history_complete": history_complete,
             "unreadable": state["unreadable"], "gaps": state["gaps"], "processed": len(state["seen"]),
             "source_history_complete": all(state.get("chat_coverage", {}).values())
                 if state.get("chat_coverage") else False}),
         job["history_start"], cutoff or job["history_end"], facts.PROMPT_VERSION))


def view_for(conn, contact_id, cm_id, settings, *, actor_id=None):
    blank = {"tier": "pending", "tier_label": "待评估", "phase_label": "", "grounds": [],
             "cutoff_label": "", "gap_label": "尚未评估", "manual_tier": "", "manual_reason": "", "stale": False}
    row = conn.execute("SELECT p.*, (" + _PROFILE_VALID + ") valid FROM creator_importance_profiles p "
                       "WHERE contact_id=%s AND responsible_authorization_id=%s",
                       (contact_id, cm_id)).fetchone()
    if not row or not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok",
                                   (contact_id, cm_id)).fetchone()["ok"]:
        return blank
    output = {**blank, "manual_tier": row["manual_tier"] or ""}
    if row["manual_tier"]:
        output.update(tier=row["manual_tier"], tier_label=TIERS[row["manual_tier"]], gap_label="")
        output["manual_reason"] = _cipher(settings).decrypt(row["manual_reason_ciphertext"]) \
            if row["manual_reason_ciphertext"] else ""
    if not row["valid"]:
        return {**output, "gap_label": "历史分析中" if row["evaluated_at"]
                and not row["coverage"].get("history_complete") else
                "依据已变化，等待更新" if row["evaluated_at"] else "尚未自动评估"}
    material = json.loads(_cipher(settings).decrypt(row["facts_ciphertext"])) if row["facts_ciphertext"] else []
    result = facts.rank(material)
    tier = row["manual_tier"] or result["tier"]
    coverage = row["coverage"]
    output.update(tier=tier, tier_label=TIERS[tier], phase_label=PHASES[result["phase"]],
                  grounds=result["grounds"][:2], stale=result.get("needs_update", False),
                  cutoff_label=row["cutoff"].astimezone(LONDON).strftime("%m-%d %H:%M") if row["cutoff"] else "",
                  gap_label="历史分析中" if not coverage.get("history_complete") else
                  "部分资料不可读" if coverage.get("unreadable") or coverage.get("gaps") else
                  "历史同步范围不足" if not coverage.get("source_history_complete") else
                  "需要更新依据" if result.get("needs_update") else "")
    if material:
        conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
                     (uuid4(), actor_id or cm_id, "creator_importance_viewed", creator_data.encode({
                         "contact_id": str(contact_id), "cm_id": str(cm_id)})))
    return output


def set_manual(conn, actor_id, contact_id, cm_id, tier, reason, settings=None):
    creator_access.require_owner(conn, actor_id)
    cm_id, _ = creator_access.access(conn, actor_id, cm_id, read_only=True)
    contact_id = creator_access.parse_id(contact_id)
    if tier not in {"", *TIERS} or not isinstance(reason, str) or len(reason.strip()) > 240:
        raise ValueError("等级或依据格式无效。")
    reason = reason.strip()
    if tier and not reason:
        raise ValueError("请填写简短的调整依据。")
    if not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok", (contact_id, cm_id)).fetchone()["ok"]:
        raise PermissionError("该达人不在此负责人的有效名单中。")
    previous = conn.execute("SELECT manual_tier FROM creator_importance_profiles "
                            "WHERE contact_id=%s AND responsible_authorization_id=%s FOR UPDATE",
                            (contact_id, cm_id)).fetchone()
    conn.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,
        manual_tier,manual_reason_ciphertext) VALUES(%s,%s,%s,%s)
        ON CONFLICT(contact_id,responsible_authorization_id) DO UPDATE SET manual_tier=EXCLUDED.manual_tier,
        manual_reason_ciphertext=EXCLUDED.manual_reason_ciphertext,revision=creator_importance_profiles.revision+1""",
        (contact_id, cm_id, tier or None, _cipher(settings or get_settings()).encrypt(reason) if tier else None))
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
                 (uuid4(), actor_id, "creator_importance_manual", creator_data.encode({
                     "contact_id": str(contact_id), "cm_id": str(cm_id),
                     "before": previous["manual_tier"] if previous else None, "after": tier or None})))
    return {"saved": True}


def metrics(conn):
    statuses = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status,count(*) n FROM creator_importance_jobs GROUP BY status").fetchall()}
    usage = conn.execute("SELECT count(*) api_requests,COALESCE(sum(input_tokens),0) input_tokens,"
                         "COALESCE(sum(output_tokens),0) output_tokens FROM creator_importance_requests "
                         "WHERE error_code<>'not_sent'").fetchone()
    return {"counts": {"pending": sum(statuses.get(s, 0) for s in ("queued", "preparing", "ready", "retry_wait")),
                        "running": statuses.get("running", 0), "succeeded": statuses.get("completed", 0),
                        "failed": statuses.get("failed", 0)}, "usage": dict(usage)}


def configuration_changed(conn):
    enabled = _enabled(_config(conn))
    conn.execute("""UPDATE creator_importance_jobs SET claim_token=NULL,lease_until=NULL,
        status=CASE WHEN %s THEN 'queued' ELSE 'cancelled' END,attempts=0,
        reason_code='configuration_changed',not_before=NOW(),updated_at=NOW()
        WHERE status<>'completed' AND (%s OR status NOT IN ('failed','cancelled'))""", (enabled, enabled))
    if enabled:
        conn.execute("""SELECT enqueue_creator_importance(pair.contact_id,pair.cm_id)
            FROM (SELECT contact_id,responsible_authorization_id cm_id FROM contact_assignments WHERE ended_at IS NULL
              UNION SELECT contact_id,responsible_authorization_id FROM creator_account_assignments
              WHERE ended_at IS NULL AND relationship='owner') pair
            WHERE creator_importance_is_eligible(pair.contact_id,pair.cm_id)
              AND NOT EXISTS(SELECT 1 FROM creator_importance_jobs j WHERE j.contact_id=pair.contact_id
                AND j.responsible_authorization_id=pair.cm_id AND j.status NOT IN ('failed','cancelled'))""")


_READABLE = """FROM chat_messages m JOIN authorization_message_observations o USING(chat_id,message_id)
    JOIN authorization_chats ac ON ac.authorization_id=o.authorization_id AND ac.chat_id=m.chat_id
    JOIN authorizations a ON a.id=o.authorization_id JOIN chats ch ON ch.chat_id=m.chat_id
    LEFT JOIN lark_identities i ON i.open_id=m.sender_open_id
    WHERE o.authorization_id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
      AND o.observed_at>=a.body_consent_at AND ac.is_visible AND ac.sync_enabled
      AND COALESCE(ch.chat_status,'normal')='normal' AND NOT m.is_deleted
      AND m.sender_type='user'
      AND (i.contact_id=%s AND i.identity_type='user' OR m.sender_open_id=a.open_id)
      AND (EXISTS(SELECT 1 FROM lark_identities target WHERE target.contact_id=%s AND target.identity_type='user'
            AND target.open_id=ac.p2p_counterpart_open_id)
        OR EXISTS(SELECT 1 FROM chat_members member JOIN lark_identities target ON target.open_id=member.open_id
            WHERE member.chat_id=m.chat_id AND target.contact_id=%s AND target.identity_type='user')
        OR EXISTS(SELECT 1 FROM chat_messages target_message JOIN lark_identities target
            ON target.open_id=target_message.sender_open_id
            JOIN authorization_message_observations target_observation USING(chat_id,message_id)
            WHERE target_message.chat_id=m.chat_id AND target.contact_id=%s AND target.identity_type='user'
              AND target_observation.authorization_id=a.id AND target_observation.observed_at>=a.body_consent_at
              AND NOT target_message.is_deleted))"""
_COLUMNS = """SELECT m.*,i.contact_id,i.display_name sender_name,ch.name chat_name,ch.chat_mode,
    a.open_id cm_open_id,a.body_consent_at,ac.p2p_counterpart_open_id,ac.backfill_complete,
    ac.backfill_window_start_at,ac.member_snapshot_status,ac.member_count,
    md5(m.mentions_json::text) mentions_fingerprint """


def build_batch(conn, job, settings, state):
    """Cursor-based history or specific signalled messages; never fetch a whole 90-day conversation."""
    contact_id, cm_id = job["contact_id"], job["responsible_authorization_id"]
    identity = conn.execute("SELECT open_id FROM lark_identities WHERE contact_id=%s AND identity_type='user'",
                            (contact_id,)).fetchall()
    opens = {r["open_id"] for r in identity}
    cm = creator_access.authorized_cm(conn, cm_id)
    if not cm or not opens:
        return {"pack": None, "sources": [], "cursor": None, "consumed": [], "done": True,
                "state": state, "gap": True}
    history = job["needs_history"]
    suffix, args = "", [cm_id, contact_id, contact_id, contact_id, contact_id]
    if history:
        suffix = " AND m.message_create_time>=%s AND m.message_create_time<%s"
        args.extend((job["history_start"], job["history_end"]))
        if job["cursor_time"]:
            suffix += " AND (m.message_create_time,m.chat_id,m.message_id)>(%s,%s,%s)"
            args.extend((job["cursor_time"], job["cursor_chat_id"], job["cursor_message_id"]))
    else:
        pending = list(job["pending_messages"].items())[:MAX_MESSAGES]
        keys = [value for _, value in pending]
        suffix = " AND EXISTS(SELECT 1 FROM jsonb_to_recordset(%s::jsonb) AS x(chat_id text,message_id text) " \
                 "WHERE x.chat_id=m.chat_id AND x.message_id=m.message_id)"
        args.append(creator_data.encode(keys))
    rows = conn.execute(_COLUMNS + _READABLE + suffix +
        " ORDER BY m.message_create_time,m.chat_id,m.message_id LIMIT %s", [*args, MAX_MESSAGES]).fetchall()
    consumed = [] if history else [key for key, value in pending if not any(
        r["chat_id"] == value["chat_id"] and r["message_id"] == value["message_id"] for r in rows)]
    cursor = None
    done = (len(rows) < MAX_MESSAGES if history
            else not job["pending_messages"] or len(pending) == len(job["pending_messages"]))
    by_key = {(r["chat_id"], r["message_id"]): r for r in rows}

    def read_parents():
        missing = {(r["chat_id"], r["parent_id"]) for r in by_key.values() if r.get("parent_id")} - set(by_key)
        for _ in range(8):
            room = MAX_MESSAGES * 2 - len(by_key)
            if not missing or room <= 0:
                break
            extra = conn.execute(_COLUMNS + _READABLE + " AND EXISTS(SELECT 1 FROM jsonb_to_recordset(%s::jsonb) "
                "AS x(chat_id text,message_id text) WHERE x.chat_id=m.chat_id AND x.message_id=m.message_id) LIMIT %s",
                (cm_id, contact_id, contact_id, contact_id, contact_id,
                 creator_data.encode([{"chat_id": c, "message_id": m} for c, m in sorted(missing)]), room)).fetchall()
            if not extra:
                break
            by_key.update({(r["chat_id"], r["message_id"]): r for r in extra})
            missing = {(r["chat_id"], r["parent_id"]) for r in extra if r.get("parent_id")} - set(by_key)

    read_parents()
    boundaries = {}
    for row in rows:
        boundaries.setdefault(row["chat_id"], row)
    # Natural chat answers often have no parent_id; carry a bounded same-chat tail across batches.
    tail_limit = max(1, min(4, MAX_MESSAGES // max(1, len(boundaries))))
    for chat_id, first in boundaries.items():
        room = MAX_MESSAGES * 2 - len(by_key)
        if room <= 0:
            break
        extra = conn.execute(_COLUMNS + _READABLE + " AND m.chat_id=%s "
            "AND m.message_create_time>=%s AND (m.message_create_time,m.message_id)<(%s,%s) "
            "ORDER BY m.message_create_time DESC,m.message_id DESC LIMIT %s",
            (cm_id, contact_id, contact_id, contact_id, contact_id, chat_id, job["history_start"],
             first["message_create_time"], first["message_id"], min(tail_limit, room))).fetchall()
        by_key.update({(r["chat_id"], r["message_id"]): r for r in extra})
    read_parents()
    body_cipher = message_body_cipher(settings.token_encryption_key)
    names = mention_names(conn, list(by_key.values()), lock=False)
    chats = {}
    coverage = state.setdefault("chat_coverage", {})
    for row in rows:
        if row["chat_id"] in chats:
            continue
        members = conn.execute("""SELECT m.open_id,i.tenant_classification,i.contact_id FROM chat_members m
            LEFT JOIN lark_identities i ON i.open_id=m.open_id WHERE m.chat_id=%s""", (row["chat_id"],)).fetchall()
        unique = (row["member_snapshot_status"] == "succeeded" and row["member_count"] is not None
                  and len(members) >= row["member_count"] and any(m["open_id"] in opens for m in members)
                  and all(m["open_id"] in opens or m["open_id"] == cm["open_id"]
                          or m["tenant_classification"] == "internal" for m in members))
        chats[row["chat_id"]] = {"name": row["chat_name"], "private": row["p2p_counterpart_open_id"] in opens,
                                  "unique_creator": unique}
        coverage[row["chat_id"]] = bool(row["backfill_complete"] and row["backfill_window_start_at"]
                                        and row["backfill_window_start_at"] <= job["history_start"])
    decoded = {}
    for key, row in sorted(by_key.items(), key=lambda item: (item[1]["message_create_time"], *item[0])):
        if row["chat_id"] not in chats:
            continue
        if not row["body_ciphertext"] or not row["message_create_time"] or row["message_type"] not in {
                "text", "post", "share_chat", "share_user"}:
            continue
        raw = body_cipher.decrypt(row["body_ciphertext"])
        fp = message_body_fingerprint(settings.token_encryption_key, raw)
        if row["body_fingerprint"] is None:
            conn.execute("UPDATE chat_messages SET body_fingerprint=%s WHERE chat_id=%s AND message_id=%s "
                         "AND body_ciphertext=%s AND body_fingerprint IS NULL", (fp, *key, row["body_ciphertext"]))
        elif row["body_fingerprint"] != fp:
            raise ValueError("Message content fingerprint changed outside ingestion.")
        text, incomplete = offhours_summary._body(row, body_cipher, names)
        ref = "E" + _hash(key)[:20]
        parent = by_key.get((row["chat_id"], row.get("parent_id")))
        creator = str(row["contact_id"]) == str(contact_id)
        chat = chats[row["chat_id"]]
        direct = any(t["open_id"] in opens and not t["ambiguous"]
                     for t in mention_targets(row["mentions_json"], names))
        linked = (creator or chat["private"] or chat["unique_creator"] or direct
                  or parent is not None and parent["sender_open_id"] in opens
                  or any(r["sender_open_id"] in opens and r.get("parent_id") == row["message_id"]
                         and r["chat_id"] == row["chat_id"] for r in by_key.values()))
        source = {"ref": ref, "chat_id": key[0], "message_id": key[1], "body_fingerprint": fp,
                  "sender_open_id": row["sender_open_id"], "message_type": row["message_type"],
                  "created_at": row["message_create_time"].isoformat(),
                  "consent_at": row["body_consent_at"].isoformat(),
                  "parent_id": row["parent_id"], "root_id": row["root_id"], "thread_id": row["thread_id"],
                  "mentions_fingerprint": row["mentions_fingerprint"]}
        decoded[key] = {"source": source, "incomplete": incomplete, "message": {
            "ref": ref, "chat_id": key[0], "message_id": key[1],
            "author_id": row["sender_open_id"], "contact_id": str(row["contact_id"]) if row["contact_id"] else None,
            "role": "creator" if creator else "cm" if linked else "context", "created_at": source["created_at"],
            "text": text, "reply_to": "E" + _hash((row["chat_id"], row["parent_id"]))[:20]
            if row["parent_id"] else None, "context_complete": False}}
    selected, characters = {}, 0
    for anchor in rows:
        key = anchor["chat_id"], anchor["message_id"]
        ref = "E" + _hash(key)[:20]
        item = decoded.get(key)
        if item is None:
            state["unreadable"] += 1
        elif state["seen"].get(ref) != _hash(item["source"]):
            nearby = sorted((k for k in decoded if k[0] == key[0]
                and (by_key[k]["message_create_time"], k[1]) < (anchor["message_create_time"], key[1])),
                key=lambda k: (by_key[k]["message_create_time"], k[1]))[-min(4, MAX_MESSAGES):]
            chain, visited = [], set()
            for current in [key, *nearby]:
                while current in decoded and current not in visited:
                    visited.add(current)
                    chain.append(current)
                    parent_id = by_key[current].get("parent_id")
                    if not parent_id:
                        break
                    current = current[0], parent_id
            bundle = [k for k in reversed(chain) if k not in selected]
            length = sum(len(decoded[k]["message"]["text"]) for k in bundle)
            if length > MAX_CHARS or len(bundle) > MAX_MESSAGES * 2:
                state["gaps"] += 1
                state["seen"][ref] = _hash(item["source"])
            elif characters + length > MAX_CHARS or len(selected) + len(bundle) > MAX_MESSAGES * 2:
                done = False
                break
            else:
                selected.update({k: decoded[k] for k in bundle})
                characters += length
                state["seen"][ref] = _hash(item["source"])
        consumed.append(key[0] + "/" + key[1])
        cursor = anchor["message_create_time"], *key
    # Completeness is based on the material actually sent, including every required original question.
    for key, item in selected.items():
        current, visited, complete = key, set(), True
        while current in selected and current not in visited:
            visited.add(current)
            if selected[current]["incomplete"]:
                complete = False
                break
            parent_id = by_key[current].get("parent_id")
            if not parent_id:
                break
            current = current[0], parent_id
        else:
            complete = False
        item["message"]["context_complete"] = complete
        if not complete:
            state["gaps"] += 1
    ordered = sorted(selected.values(), key=lambda x: (x["message"]["created_at"], x["message"]["ref"]))
    messages = [item["message"] for item in ordered]
    sources = [item["source"] for item in ordered]
    pack = {"creator_id": str(contact_id), "cm_id": str(cm_id), "cm_open_id": cm["open_id"],
            "chats": chats, "messages": messages, "previous_facts": facts.prompt_previous(state["facts"]),
            "policy": facts.PROMPT_VERSION}
    eligible = any(m["role"] in {"creator", "cm"} and m["context_complete"] for m in messages)
    return {"pack": pack if eligible else None, "sources": sources, "cursor": cursor,
            "consumed": consumed, "done": done, "state": state, "gap": False}


def _recover(conn):
    conn.execute("""UPDATE creator_importance_jobs SET status='queued',claim_token=NULL,lease_until=NULL,
        reason_code='preparation_recovered',updated_at=NOW() WHERE status='preparing' AND lease_until<NOW()""")
    # An expired paid request may have reached the provider. Never silently submit it twice.
    conn.execute("""UPDATE creator_importance_jobs SET status='failed',claim_token=NULL,lease_until=NULL,
        reason_code='request_unknown',finished_at=NOW(),updated_at=NOW()
        WHERE status='running' AND lease_until<NOW()""")
    conn.execute("""UPDATE creator_importance_requests r SET error_code='request_unknown',finished_at=NOW()
        FROM creator_importance_jobs j WHERE r.job_id=j.id AND j.reason_code='request_unknown'
        AND r.finished_at IS NULL""")


def _finish_batch(conn, job, material, settings, incoming=()):
    state = material["state"]
    state["facts"] = facts.merge_facts(state["facts"], incoming)
    state["sources"].update({source["ref"]: source for source in material["sources"]})
    remaining = {key: value for key, value in job["pending_messages"].items() if key not in material["consumed"]}
    history_complete = not job["needs_history"] or material["done"]
    finished = history_complete and not remaining
    cursor = material["cursor"] or (job["cursor_time"], job["cursor_chat_id"], job["cursor_message_id"])
    # Publish only after the full snapshot: a later historical cancellation must not revive an old priority.
    if finished or job["needs_history"]:
        _save_profile(conn, job, state, settings, history_complete=finished,
                      cutoff=job["history_end"] if job["needs_history"] else job["prepared_at"])
    conn.execute("""UPDATE creator_importance_jobs SET status=%s,needs_history=%s,pending_messages=%s::jsonb,
        checkpoint_ciphertext=%s,cursor_time=%s,cursor_chat_id=%s,cursor_message_id=%s,
        pack_ciphertext=NULL,refs='[]',claim_token=NULL,lease_until=NULL,attempts=0,
        reason_code=%s,finished_at=CASE WHEN %s THEN NOW() ELSE NULL END,not_before=NOW(),updated_at=NOW()
        WHERE id=%s""", ("completed" if finished else "queued", not history_complete, creator_data.encode(remaining),
            _cipher(settings).encrypt(creator_data.encode(state)), *cursor,
            "evaluated" if finished else "next_batch", finished, job["id"]))


def prepare_next(settings):
    with connection() as conn, conn.transaction():
        config = _config(conn)
        if not _enabled(config):
            return False
        _recover(conn)
        if conn.execute("SELECT count(*) n FROM creator_importance_jobs WHERE status='ready'").fetchone()["n"] >= 5:
            return False
        job = conn.execute("""SELECT * FROM creator_importance_jobs WHERE status='queued' AND not_before<=NOW()
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return False
        if not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok",
                            (job["contact_id"], job["responsible_authorization_id"])).fetchone()["ok"]:
            conn.execute("UPDATE creator_importance_jobs SET status='cancelled',reason_code='scope_unavailable' "
                         "WHERE id=%s",
                         (job["id"],))
            return True
        token = uuid4()
        job = conn.execute("""UPDATE creator_importance_jobs SET status='preparing',snapshot_version=desired_version,
            claim_token=%s,lease_until=NOW()+INTERVAL '10 minutes',
            history_start=COALESCE(history_start,NOW()-INTERVAL '90 days'),
            history_end=COALESCE(history_end,NOW()),prepared_at=NOW(),updated_at=NOW()
            WHERE id=%s RETURNING *""", (token, job["id"])).fetchone()
    try:
        with connection() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout='90s'")
            state = _prune(conn, _state(job, settings), job)
            if not job["cursor_time"] and job["needs_history"]:
                state["seen"] = {}
            material = build_batch(conn, job, settings, state)
            if material["gap"]:
                state["gaps"] += 1
            checks = _source_checks(conn, material["sources"], job["contact_id"],
                                    job["responsible_authorization_id"], lock=True)
            current_config = _config(conn, lock=True)
            current = conn.execute("SELECT * FROM creator_importance_jobs WHERE id=%s FOR UPDATE",
                                   (job["id"],)).fetchone()
            if not current or current["claim_token"] != token or current["status"] != "preparing":
                return True
            if (current["desired_version"] != job["snapshot_version"] or not _enabled(current_config)
                    or _config_hash(config) != _config_hash(current_config) or not all(checks.values())):
                conn.execute("UPDATE creator_importance_jobs SET status='queued',claim_token=NULL,lease_until=NULL,"
                             "reason_code='snapshot_changed' WHERE id=%s", (job["id"],))
                return True
            pack = material["pack"]
            if not pack:
                _finish_batch(conn, current, material, settings)
                return True
            fingerprint = _hash([pack, _config_hash(config)])
            cached = conn.execute("""SELECT response_ciphertext FROM creator_importance_requests
                WHERE job_id=%s AND fingerprint=%s AND error_code='' AND response_ciphertext IS NOT NULL
                ORDER BY created_at DESC LIMIT 1""", (job["id"], fingerprint)).fetchone()
            if cached:
                result = facts.validate(_cipher(settings).decrypt(cached["response_ciphertext"]), pack, state["facts"])
                _finish_batch(conn, current, material, settings, result)
                return True
            conn.execute("""UPDATE creator_importance_jobs SET status='ready',pack_ciphertext=%s,refs=%s::jsonb,
                fingerprint=%s,config_fingerprint=%s,claim_token=NULL,lease_until=NULL,attempts=0,
                reason_code='prepared',stats=%s::jsonb,updated_at=NOW() WHERE id=%s""",
                (_cipher(settings).encrypt(creator_data.encode(material)), creator_data.encode(material["sources"]),
                 fingerprint, _config_hash(config), creator_data.encode({"messages": len(pack["messages"]),
                    "history": job["needs_history"], "prompt_version": facts.PROMPT_VERSION}), job["id"]))
    except Exception:
        with connection() as conn, conn.transaction():
            conn.execute("""UPDATE creator_importance_jobs SET status='failed',claim_token=NULL,lease_until=NULL,
                reason_code='preparation_failed',finished_at=NOW(),updated_at=NOW()
                WHERE id=%s AND claim_token=%s AND status='preparing'""", (job["id"], token))
        raise
    return True


def _current(conn, job, settings, *, lock=False):
    valid = _source_checks(conn, job["refs"], job["contact_id"], job["responsible_authorization_id"], lock=lock)
    config = _config(conn, lock=lock)
    current = conn.execute("SELECT * FROM creator_importance_jobs WHERE id=%s" + (" FOR UPDATE" if lock else ""),
                           (job["id"],)).fetchone()
    if (not current or current["status"] != "running" or current["claim_token"] != job["claim_token"]
            or current["desired_version"] != job["snapshot_version"] or not _enabled(config)
            or _config_hash(config) != job["config_fingerprint"] or not all(valid.values())
            or not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok",
                (job["contact_id"], job["responsible_authorization_id"])).fetchone()["ok"]):
        return None
    return current


def claim():
    settings = get_settings()
    with connection() as conn, conn.transaction():
        config = _config(conn)
        if not _enabled(config):
            return None
        _recover(conn)
        candidate = conn.execute("""SELECT * FROM creator_importance_jobs WHERE status IN ('ready','retry_wait')
            AND not_before<=NOW() AND attempts<3 ORDER BY created_at,id LIMIT 1""").fetchone()
        if not candidate:
            return None
        valid = _source_checks(conn, candidate["refs"], candidate["contact_id"],
                               candidate["responsible_authorization_id"], lock=True)
        if not all(valid.values()) or candidate["config_fingerprint"] != _config_hash(config):
            conn.execute("UPDATE creator_importance_jobs SET status='queued',reason_code='snapshot_changed' "
                         "WHERE id=%s AND status IN ('ready','retry_wait')", (candidate["id"],))
            return None
        if not ai_dispatch.can_claim(conn):
            return None
        token = uuid4()
        job = conn.execute("""UPDATE creator_importance_jobs SET status='running',claim_token=%s,
            lease_until=NOW()+INTERVAL '180 seconds',attempts=attempts+1,updated_at=NOW()
            WHERE id=%s AND status IN ('ready','retry_wait') AND desired_version=%s
            AND snapshot_version=%s AND config_fingerprint=%s RETURNING *""",
            (token, candidate["id"], candidate["desired_version"], candidate["snapshot_version"],
             candidate["config_fingerprint"])).fetchone()
        if not job:
            return None
        reservation = uuid4()
        conn.execute("INSERT INTO creator_importance_requests(id,job_id,fingerprint) VALUES(%s,%s,%s)",
                     (reservation, job["id"], job["fingerprint"]))
        return {**job, "material": json.loads(_cipher(settings).decrypt(job["pack_ciphertext"])),
                "reservation_id": reservation, "config": dict(config)}


def run(job, settings):
    with connection() as conn, conn.transaction():
        if not _current(conn, job, settings):
            conn.execute("UPDATE creator_importance_requests SET error_code='not_sent',input_tokens=0,output_tokens=0,"
                         "finished_at=NOW() WHERE id=%s", (job["reservation_id"],))
            return
    material = job["material"]
    pack = material["pack"]
    result, raw, telemetry, error = None, None, {}, None
    try:
        key = ai_feature_config.api_key_cipher(settings.token_encryption_key).decrypt(
            job["config"]["api_key_ciphertext"])
        response = api.call_openai(job["config"], key, pack, job["contact_id"], instructions=facts.instructions(),
            output_format=facts.output_format([m["ref"] for m in pack["messages"]
                if m["role"] in {"creator", "cm"} and m["context_complete"]], material["state"]["facts"]),
            max_output_tokens=4_096, reasoning_effort="none", total_timeout=70)
        raw = response["text"]
        telemetry = {**response["telemetry"], "usage": response["usage"], "response_id": response["response_id"]}
        result = facts.validate(raw, pack, material["state"]["facts"])
    except Exception as exc:
        error = api.ModelOutputError(str(exc), telemetry=telemetry) if isinstance(exc, ValueError) \
            and not isinstance(exc, api.OpenAIRequestError) else exc
        telemetry = getattr(error, "telemetry", {}) or telemetry
    status = telemetry.get("http_status")
    unknown = isinstance(error, api.OpenAIRequestError) and not isinstance(status, int)
    retry = isinstance(error, api.OpenAIRequestError) and error.retryable and not unknown and job["attempts"] < 3
    code = "" if not error else "request_unknown" if unknown else "invalid_output" \
        if isinstance(error, api.ModelOutputError) else (
            f"http_{status}" if isinstance(status, int) else "provider_failed")
    with connection() as conn, conn.transaction():
        usage = telemetry.get("usage", {})
        conn.execute("""UPDATE creator_importance_requests SET request_id=%s,response_id=%s,error_code=%s,
            input_tokens=%s,output_tokens=%s,response_ciphertext=%s,finished_at=NOW() WHERE id=%s""",
            (telemetry.get("openai_request_id"), telemetry.get("response_id"), code,
             usage.get("input_tokens"), usage.get("output_tokens"),
             _cipher(settings).encrypt(raw[:64_000]) if raw is not None else None, job["reservation_id"]))
    # Record actual charges even when the source changed while the request was running.
    ai_dispatch.observe_limits(telemetry)
    if isinstance(error, api.OpenAIRequestError) and error.is_quota:
        ai_dispatch.pause_for_quota()
    with connection() as conn, conn.transaction():
        current = _current(conn, job, settings, lock=True)
        if not current:
            return
        if error is None:
            _finish_batch(conn, current, material, settings, result)
        else:
            conn.execute("""UPDATE creator_importance_jobs SET status=%s,reason_code=%s,claim_token=NULL,
                lease_until=NULL,not_before=NOW()+(%s*INTERVAL '1 second'),finished_at=CASE WHEN %s THEN NOW() END,
                updated_at=NOW() WHERE id=%s""", ("retry_wait" if retry else "failed", code,
                api.retry_delay(error.retry_after_seconds, job["attempts"]) if retry else 0, not retry, job["id"]))
        conn.execute("UPDATE ai_feature_configs SET last_tested_at=NOW(),last_test_status=%s WHERE feature=%s",
                     ("failed" if error else "succeeded", FEATURE))
