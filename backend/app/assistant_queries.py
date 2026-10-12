"""Scoped, read-only business facts for the operations assistant."""

import json
from datetime import UTC, datetime
from uuid import UUID

from . import admin_reporting, contact_handles, creator_importance, creator_responsibility_matching
from . import offhours_summary as summaries
from .assistant_search import authorize_all, unreadable_reason
from .creator_group_evidence import PURPOSES
from .evidence_access import BODY_READ_SCOPE_SQL

PAGE_SIZE = 25


def _iso(value):
    return value.isoformat() if value is not None else None


def _scope(conn, actor_id, context):
    cm = authorize_all(conn, actor_id, context["scope"]["cm_id"])
    roster = summaries._roster(conn, cm["id"])
    visible = {row["contact_id"] for row in roster}
    selected = context["scope"].get("creator_ids")
    if selected is not None and not {UUID(str(value)) for value in selected} <= visible:
        raise PermissionError("达人名单已变化，请重新查询。")
    return cm, roster, visible


def _ids(args, context, visible):
    values = args.get("creator_ids")
    if values is None:
        values = context["scope"].get("creator_ids")
    ids = visible if values is None else {UUID(str(value)) for value in values}
    if not ids <= visible:
        raise PermissionError("请求包含当前负责人范围外的达人。")
    return sorted(ids, key=str)


def _cursor(args):
    try:
        value = int(args.get("cursor") or 0)
        if value < 0:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError("分页位置无效。") from exc


def _authorization(cm):
    return {"id": str(cm["id"]), "body_consent_at": _iso(cm["body_consent_at"])}


def _page(kind, context, values, args, cm, now):
    cursor = _cursor(args)
    return {"kind": kind, "scope": context["scope"], kind: values[cursor:cursor + PAGE_SIZE],
            "total": len(values), "next_cursor": str(cursor + PAGE_SIZE)
            if cursor + PAGE_SIZE < len(values) else None, "checked_at": _iso(now),
            "authorization": _authorization(cm)}


def _creator_facts(conn, cm, roster, ids):
    handles = contact_handles.for_contacts(conn, ids, cm["id"])
    assignments = creator_responsibility_matching._existing_assignments(conn, ids)
    aliases, identities = {}, {}
    for person in roster:
        if person["name"]:
            aliases.setdefault(person["contact_id"], set()).add(person["name"])
        identities.setdefault(person["contact_id"], set()).add(
            (person["open_id"], str(person["account_id"]), person["display_handle"]))
    return {str(row["id"]): {"id": str(row["id"]), "name": row["display_name"] or "未命名达人",
        "aliases": sorted(aliases.get(row["id"], [])),
        "handles": list(dict.fromkeys(handles[row["id"]])),
        "responsible": sorted([{"id": str(person["authorization_id"]), "name": person["name"],
            "level": person["level"], "handle": person["handle"], "scope_label": person["scope_label"]}
            for person in assignments.get(str(row["id"]), [])],
            key=lambda person: (person["id"], person["level"], person["handle"] or "", person["name"])),
        "_identities": sorted(identities.get(row["id"], []), key=str)}
        for row in conn.execute("SELECT id,display_name FROM contacts WHERE id=ANY(%s::uuid[]) ORDER BY id",
                                (ids,)).fetchall()}


def _validate_creator_snapshot(conn, cm, roster, visible, snapshot):
    if snapshot["authorization"] != _authorization(cm):
        raise ValueError("消息授权已变化，请重新查询。")
    ids = [UUID(person["id"]) for person in snapshot["creators"]]
    if not set(ids) <= visible:
        raise PermissionError("达人负责范围或身份已变化，请重新查询。")
    facts = _creator_facts(conn, cm, roster, ids)
    if summaries._hash(facts) != snapshot["sources"]:
        raise ValueError("达人身份、账号或负责关系已变化，请重新查询。")
    if conn.execute("""SELECT EXISTS(SELECT 1 FROM jsonb_to_recordset(%s::jsonb)
        AS p(contact_id uuid,responsible_authorization_id uuid,sources jsonb)
        CROSS JOIN LATERAL jsonb_array_elements(p.sources) s WHERE NOT ("""
        + creator_importance._VALID_SOURCE + ")) changed",
        (json.dumps(snapshot["importance_sources"]),)).fetchone()["changed"]:
        raise ValueError("达人重要度的消息依据已变化，请重新查询。")


def creators(conn, actor_id, context, args, settings, *, now=None, snapshot=None):
    now = now or datetime.now(UTC)
    cm, roster, visible = _scope(conn, actor_id, context)
    # A named lookup can change the selected person, never the allowed CM roster.
    lookup_context = context if not args.get("query") else {
        **context, "scope": {**context["scope"], "creator_ids": None},
    }
    ids = _ids({}, lookup_context, visible)
    query = str(args.get("query") or "").strip().casefold()
    if "tiktok.com/@" in query:
        query = query.split("tiktok.com/@", 1)[1].split("?", 1)[0].split("/", 1)[0]
    query = query.lstrip("@")
    tier = args.get("importance")
    if tier is not None and tier not in creator_importance.TIERS:
        raise ValueError("重要度筛选无效。")
    query_scope = {"query": query, "importance": tier,
                   "creator_ids": lookup_context["scope"].get("creator_ids")}
    if snapshot is not None:
        if snapshot["query"] != query_scope:
            raise ValueError("达人查询范围已变化，请重新查询。")
        _validate_creator_snapshot(conn, cm, roster, visible, snapshot)
        result = _page("creators", context, snapshot["creators"], args, cm,
                       datetime.fromisoformat(snapshot["checked_at"]))
        result["_snapshot"] = snapshot
        result["last_contact_definition"] = "当前授权记录中达人发言，或本人私聊／明确@／回复该达人的消息。"
        return result
    facts = _creator_facts(conn, cm, roster, ids)
    join, join_args = creator_importance.roster_sql(cm["id"])
    rows = conn.execute("""SELECT c.id,c.display_name,COALESCE(ip.effective_tier,'pending') tier,
        p.cutoff,p.evaluated_at,p.revision,p.sources,p.coverage->>'history_complete' history_complete,
        (""" + creator_importance._PROFILE_VALID + """) source_valid
        FROM contacts c """ + join + """ LEFT JOIN creator_importance_profiles p
        ON p.contact_id=c.id AND p.responsible_authorization_id=%s
        WHERE c.id=ANY(%s::uuid[]) ORDER BY lower(c.display_name),c.id""",
        [*join_args, cm["id"], ids]).fetchall()
    values = []
    for row in rows:
        person = facts[str(row["id"])]
        fields = [person["name"].casefold(), *(value.casefold() for value in person["aliases"]),
                  *(value.lstrip("@").casefold() for value in person["handles"])]
        if (query and not any(query in value for value in fields)) or (tier and row["tier"] != tier):
            continue
        values.append({**{key: value for key, value in person.items() if not key.startswith("_")},
            "exact_match": query in fields,
            "importance": {"tier": row["tier"], "tier_label": creator_importance.TIERS[row["tier"]],
                "cutoff": _iso(row["cutoff"]), "evaluated_at": _iso(row["evaluated_at"]),
                "history_complete": row["history_complete"] == "true",
                "source_valid": bool(row["source_valid"]), "revision": row["revision"]}})
    values.sort(key=lambda person: (not person["exact_match"] if query else False,
        creator_importance.TIER_ORDER[person["importance"]["tier"]], person["name"].casefold(), person["id"]))
    matched_ids = [UUID(person["id"]) for person in values]
    last = conn.execute(_ACTIVITY_SQL + """ SELECT contact_id,MAX(message_create_time) at
        FROM attributed GROUP BY contact_id""",
        (matched_ids, cm["id"], datetime.min.replace(tzinfo=UTC), now)).fetchall()
    latest = {str(row["contact_id"]): _iso(row["at"]) for row in last}
    for person in values:
        person["last_contact_at"] = latest.get(person["id"])
    result = _page("creators", context, values, args, cm, now)
    matched = set(matched_ids)
    result["_snapshot"] = {"query": query_scope, "checked_at": _iso(now),
        "authorization": _authorization(cm), "creators": values,
        "sources": summaries._hash({str(person): facts[str(person)] for person in matched_ids}),
        "importance_sources": [{"contact_id": str(row["id"]), "responsible_authorization_id": str(cm["id"]),
                                "sources": row["sources"]}
            for row in rows if row["id"] in matched and row["source_valid"] and row["sources"]]}
    result["last_contact_definition"] = "当前授权记录中达人发言，或本人私聊／明确@／回复该达人的消息。"
    return result


def groups(conn, actor_id, context, args, settings, *, now=None):
    now = now or datetime.now(UTC)
    cm, _roster, visible = _scope(conn, actor_id, context)
    ids = _ids(args, context, visible)
    selected = args.get("creator_ids") is not None or context["scope"].get("creator_ids") is not None
    purpose = args.get("purpose")
    if purpose is not None and purpose not in PURPOSES:
        raise ValueError("群用途筛选无效。")
    rows = conn.execute("""SELECT c.chat_id,c.name,COALESCE(g.purpose,'unknown') purpose,
        g.updated_at purpose_updated_at,ac.member_count,ac.member_count_status,
        ac.member_snapshot_status,ac.member_snapshot_checked_at,ac.last_seen_at,ac.sync_enabled,
        ARRAY(SELECT DISTINCT i.contact_id FROM chat_members m JOIN lark_identities i ON i.open_id=m.open_id
          WHERE m.chat_id=c.chat_id AND m.member_type='user' AND i.identity_type='user'
          AND i.contact_id=ANY(%s::uuid[]) ORDER BY i.contact_id) creator_ids
        FROM authorization_chats ac JOIN chats c USING(chat_id)
        LEFT JOIN chat_group_classifications g USING(chat_id)
        WHERE ac.authorization_id=%s AND ac.is_visible
        AND COALESCE(c.chat_status,'normal')='normal' AND COALESCE(c.chat_mode,'group')<>'p2p'
        ORDER BY lower(c.name),c.chat_id""", (ids, cm["id"])).fetchall()
    query = str(args.get("query") or "").strip().casefold()
    values = []
    for row in rows:
        name = row["name"] or "未命名群"
        if (query and query not in name.casefold()) or (purpose and row["purpose"] != purpose):
            continue
        if selected and not row["creator_ids"]:
            continue
        values.append({"id": row["chat_id"], "name": name, "purpose": row["purpose"],
            "purpose_label": PURPOSES[row["purpose"]], "purpose_updated_at": _iso(row["purpose_updated_at"]),
            "creator_ids": [str(value) for value in row["creator_ids"]],
            "member_count": row["member_count"] if row["member_count_status"] == "succeeded" else None,
            "membership_status": row["member_snapshot_status"],
            "membership_checked_at": _iso(row["member_snapshot_checked_at"]),
            "visibility_checked_at": _iso(row["last_seen_at"]), "sync_enabled": row["sync_enabled"]})
    result = _page("groups", context, values, args, cm, now)
    result["definition"] = "当前CM实际可见群目录；达人关系来自成员快照，可见不代表正文或发送／@all权限。"
    return result


def _chat_states(conn, cm_id):
    return conn.execute("""SELECT c.chat_id,c.name,COALESCE(c.chat_status,'normal') chat_status,
        COALESCE(c.chat_mode,'group') chat_mode,ac.sync_enabled,ac.sync_status,ac.backfill_complete,
        ac.backfill_window_start_at,ac.last_backfill_at,ac.last_incremental_at,ac.p2p_counterpart_open_id,ac.last_error
        FROM authorization_chats ac JOIN chats c USING(chat_id)
        WHERE ac.authorization_id=%s AND ac.is_visible ORDER BY c.chat_id""", (cm_id,)).fetchall()


def _coverage(chats, start=None, end=None):
    gaps, watermarks = [], []
    for chat in chats:
        watermark = max((value for value in (chat["last_incremental_at"], chat["last_backfill_at"])
                         if value is not None), default=None)
        reasons = []
        if chat["chat_status"] != "normal":
            reasons.append("会话已归档／解散")
        elif not chat["sync_enabled"]:
            reasons.append("未启用消息同步")
        else:
            if watermark:
                watermarks.append(watermark)
            if not chat["backfill_complete"]:
                reasons.append("历史回填尚未完成")
            if chat["sync_status"] not in {"succeeded", "syncing"}:
                reasons.append(unreadable_reason(chat) if chat["sync_status"] == "not_readable" else "同步尚未成功")
            # NULL is an unbounded history start, not a missing sync boundary.
            if start is not None and chat["backfill_window_start_at"] is not None \
                    and chat["backfill_window_start_at"] > start:
                reasons.append("请求早于已确认历史同步范围")
            if watermark is None or (end is not None and watermark < end):
                reasons.append("尚无同步水位" if watermark is None else "同步水位未覆盖请求截止")
        if reasons:
            gaps.append({"chat_id": chat["chat_id"], "name": chat["name"] or "未命名会话",
                "reason": "；".join(reasons), "history_start": _iso(chat["backfill_window_start_at"]),
                "last_synced_at": _iso(watermark)})
    return {"latest": _iso(max(watermarks, default=None)), "oldest": _iso(min(watermarks, default=None))}, gaps


def sync_status(conn, actor_id, context, args, settings, *, now=None):
    now = now or datetime.now(UTC)
    cm, _roster, _visible = _scope(conn, actor_id, context)
    chats = _chat_states(conn, cm["id"])
    job = conn.execute("""SELECT status,mode,scheduled_at,discovery_status,last_http_status,last_error_code
        FROM sync_jobs WHERE authorization_id=%s ORDER BY created_at DESC LIMIT 1""", (cm["id"],)).fetchone()
    normal = [chat for chat in chats if chat["chat_status"] == "normal" and chat["sync_enabled"]]
    counts = {"discovered_conversation_count": len(chats), "syncable_conversation_count": len(normal),
        "archived_conversation_count": sum(chat["chat_status"] != "normal" for chat in chats),
        "sync_disabled_count": sum(not chat["sync_enabled"] for chat in chats),
        "backfill_complete_count": sum(chat["backfill_complete"] for chat in normal),
        "syncing_conversation_count": sum(chat["sync_status"] == "syncing" for chat in normal),
        "waiting_conversation_count": sum(chat["sync_status"] in {"pending", "failed_retryable"}
                                          for chat in normal),
        "not_readable_conversation_count": sum(chat["sync_status"] == "not_readable" for chat in normal)}
    facts = {**counts, "authorization_status": cm["status"]}
    facts.update({"job_" + key: value for key, value in (job or {}).items()})
    label, _badge, detail = admin_reporting.sync_summary(facts, now=now)
    watermarks, gaps = _coverage(chats)
    return {"kind": "sync", "scope": context["scope"], "authorization_status": cm["status"],
        "body_consent_at": _iso(cm["body_consent_at"]), "summary": {"label": label, "detail": detail},
        "conversations": counts, "watermarks": watermarks, "gaps": gaps,
        "job": {"status": job["status"], "mode": job["mode"], "scheduled_at": _iso(job["scheduled_at"])}
        if job else None, "checked_at": _iso(now), "authorization": _authorization(cm),
        "source_chat_ids": [chat["chat_id"] for chat in chats],
        "definition": "同步水位是检查完成时间，不是最后一条消息时间，也不保证全历史已同步。"}


# Only actual sender, private counterpart and explicit @/reply links count; group membership alone never does.
_ACTIVITY_SQL = f"""WITH people AS MATERIALIZED (
    SELECT open_id,contact_id FROM lark_identities WHERE identity_type='user' AND contact_id=ANY(%s::uuid[])
), visible AS MATERIALIZED (
    SELECT m.chat_id,m.message_id,m.message_create_time,m.sender_open_id,m.parent_id,m.root_id,m.mentions_json,
        m.body_ciphertext IS NOT NULL readable,m.sender_open_id=a.open_id is_own,
        ac.p2p_counterpart_open_id,COALESCE(c.chat_mode,'group') chat_mode,a.id cm_id,a.body_consent_at
    FROM authorization_message_observations o JOIN chat_messages m USING(chat_id,message_id)
    JOIN authorization_chats ac ON ac.authorization_id=o.authorization_id AND ac.chat_id=m.chat_id
    JOIN authorizations a ON a.id=o.authorization_id JOIN chats c ON c.chat_id=m.chat_id
    WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL AND o.observed_at>=a.body_consent_at
      AND ac.is_visible AND ac.sync_enabled AND COALESCE(c.chat_status,'normal')='normal' AND NOT m.is_deleted
      AND {BODY_READ_SCOPE_SQL} AND ac.sync_status IS DISTINCT FROM 'not_readable'
      AND COALESCE(m.sender_type,'user')='user' AND m.message_create_time>=%s AND m.message_create_time<%s
      AND (m.sender_open_id=a.open_id OR m.sender_open_id IN (SELECT open_id FROM people))
), links AS (
    SELECT m.chat_id,m.message_id,p.contact_id FROM visible m JOIN people p ON p.open_id=m.sender_open_id
    UNION SELECT m.chat_id,m.message_id,p.contact_id FROM visible m
      JOIN people p ON p.open_id=m.p2p_counterpart_open_id WHERE m.is_own AND m.chat_mode='p2p'
    UNION SELECT m.chat_id,m.message_id,p.contact_id FROM visible m CROSS JOIN LATERAL (
      SELECT MIN(item->>'id') open_id FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(m.mentions_json)='array' THEN m.mentions_json ELSE '[]'::jsonb END) item
      WHERE item->>'key' ~ '^@_user_[0-9]+$' GROUP BY item->>'key'
      HAVING COUNT(DISTINCT (COALESCE(item->>'id_type',''),COALESCE(item->>'id','')))=1
        AND MIN(item->>'id_type')='open_id'
    ) mentions JOIN people p ON p.open_id=mentions.open_id WHERE m.is_own
    UNION SELECT m.chat_id,m.message_id,p.contact_id FROM visible m
      JOIN chat_messages parent ON parent.chat_id=m.chat_id
        AND parent.message_id=ANY(ARRAY[m.parent_id,m.root_id])
      JOIN authorization_message_observations po ON po.chat_id=parent.chat_id AND po.message_id=parent.message_id
        AND po.authorization_id=m.cm_id AND po.observed_at>=m.body_consent_at
      JOIN people p ON p.open_id=parent.sender_open_id WHERE m.is_own AND NOT parent.is_deleted
        AND parent.message_create_time<=m.message_create_time
), attributed AS (
    SELECT m.*,l.contact_id FROM visible m JOIN links l USING(chat_id,message_id)
) """


def _activity(conn, cm, ids, start, end, group_by, chat_ids=None):
    key = "(message_create_time AT TIME ZONE 'Europe/London')::date" if group_by == "day" else "contact_id"
    sql = _ACTIVITY_SQL
    params = [ids, cm["id"], start, end]
    if chat_ids is not None:
        sql = sql.replace("AND (m.sender_open_id=a.open_id", "AND m.chat_id=ANY(%s::text[]) "
                          "AND (m.sender_open_id=a.open_id")
        params.append(chat_ids)
    rows = conn.execute(sql + f"""SELECT {key} bucket,GROUPING({key}) total,
        count(DISTINCT (chat_id,message_id)) messages,
        count(DISTINCT (chat_id,message_id)) FILTER(WHERE is_own) own_messages,
        count(DISTINCT (chat_id,message_id)) FILTER(WHERE readable) readable_messages,
        count(DISTINCT contact_id) active_creators,
        count(DISTINCT contact_id) FILTER(WHERE is_own) contacted_creators,
        count(DISTINCT chat_id) conversations,array_agg(DISTINCT chat_id) source_chats
        FROM attributed GROUP BY GROUPING SETS ((),({key})) ORDER BY bucket NULLS FIRST""",
        params).fetchall()
    metrics, values, source_chats = {}, [], []
    for row in rows:
        counts = {name: row[name] for name in ("messages", "own_messages", "readable_messages", "active_creators",
                                             "contacted_creators", "conversations")}
        if row["total"]:
            metrics = counts
            source_chats = row["source_chats"] or []
        else:
            values.append({group_by: str(row["bucket"]), **counts})
    if group_by == "creator":
        names = {str(row["id"]): row["display_name"] or "未命名达人" for row in conn.execute(
            "SELECT id,display_name FROM contacts WHERE id=ANY(%s::uuid[])", (ids,)).fetchall()}
        for value in values:
            value["creator_name"] = names[value["creator"]]
    return {"window": {"start": _iso(start), "end": _iso(end), "timezone": summaries.TIMEZONE},
            "metrics": metrics, "rows": values, "source_chat_ids": source_chats}


_ALL_ACTIVITY_SQL = f"""WITH visible AS MATERIALIZED (
    SELECT m.chat_id,m.message_id,m.message_create_time,m.sender_open_id,m.sender_id,m.sender_id_type,
        m.parent_id,m.root_id,m.mentions_json,m.body_ciphertext IS NOT NULL readable,
        CASE WHEN COALESCE(m.sender_type,i.identity_type) IN ('app','bot') THEN 'app'
             WHEN m.sender_type='system' OR m.message_type='system' THEN 'system'
             WHEN COALESCE(m.sender_type,i.identity_type)='user' THEN 'user' ELSE 'unknown' END sender_kind,
        i.contact_id,COALESCE(NULLIF(p.display_name,''),NULLIF(i.display_name,'')) sender_name,
        a.open_id cm_open_id,a.id cm_id,a.body_consent_at,a.display_name cm_name,
        COALESCE(own.contact_id::text,a.open_id) cm_person,
        ac.p2p_counterpart_open_id,COALESCE(c.chat_mode,'group') chat_mode,c.name chat_name
    FROM authorization_message_observations o JOIN chat_messages m USING(chat_id,message_id)
    JOIN authorization_chats ac ON ac.authorization_id=o.authorization_id AND ac.chat_id=m.chat_id
    JOIN authorizations a ON a.id=o.authorization_id JOIN chats c ON c.chat_id=m.chat_id
    LEFT JOIN lark_identities i ON i.open_id=m.sender_open_id
    LEFT JOIN contacts p ON p.id=i.contact_id LEFT JOIN lark_identities own ON own.open_id=a.open_id
    WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL AND o.observed_at>=a.body_consent_at
      AND {BODY_READ_SCOPE_SQL}
      AND ac.is_visible AND ac.sync_enabled AND COALESCE(c.chat_status,'normal')='normal'
      AND ac.sync_status IS DISTINCT FROM 'not_readable' AND NOT m.is_deleted
      AND m.message_create_time>=%s AND m.message_create_time<%s
      AND (%s::text[] IS NULL OR m.chat_id=ANY(%s::text[]))
), messages AS MATERIALIZED (
    SELECT v.*,sender_kind='user' AND sender_open_id=cm_open_id is_own,
        CASE WHEN sender_kind='user' AND sender_open_id IS NOT NULL
             THEN COALESCE(contact_id::text,sender_open_id)
             WHEN sender_kind='app' THEN 'app:'||COALESCE(sender_id_type||':'||sender_id,sender_open_id,'unknown')
             ELSE 'unknown:'||sender_kind END person,
        CASE WHEN sender_kind='user' THEN COALESCE(sender_name,
                  CASE WHEN sender_open_id=cm_open_id THEN cm_name END,'未知用户')
             WHEN sender_kind='app' THEN COALESCE(sender_name,'应用／机器人')
             WHEN sender_kind='system' THEN '系统消息' ELSE '未知发送者' END person_name
    FROM visible v
), targets AS (
    SELECT chat_id,message_id,p2p_counterpart_open_id open_id FROM messages WHERE is_own AND chat_mode='p2p'
    UNION SELECT m.chat_id,m.message_id,mentions.open_id FROM messages m CROSS JOIN LATERAL (
      SELECT MIN(item->>'id') open_id FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(m.mentions_json)='array' THEN m.mentions_json ELSE '[]'::jsonb END) item
      WHERE item->>'key' ~ '^@_user_[0-9]+$' GROUP BY item->>'key'
      HAVING COUNT(DISTINCT (COALESCE(item->>'id_type',''),COALESCE(item->>'id','')))=1
        AND MIN(item->>'id_type')='open_id'
    ) mentions WHERE m.is_own
    UNION SELECT m.chat_id,m.message_id,parent.sender_open_id FROM messages m
      JOIN chat_messages parent ON parent.chat_id=m.chat_id
        AND parent.message_id=COALESCE(NULLIF(m.parent_id,''),NULLIF(m.root_id,''))
      JOIN authorization_message_observations po ON po.chat_id=parent.chat_id AND po.message_id=parent.message_id
        AND po.authorization_id=m.cm_id AND po.observed_at>=m.body_consent_at
      WHERE m.is_own AND NOT parent.is_deleted AND COALESCE(parent.sender_type,'user')='user'
        AND parent.message_type IS DISTINCT FROM 'system' AND parent.message_create_time<=m.message_create_time
), recipients AS MATERIALIZED (
    SELECT DISTINCT t.chat_id,t.message_id,COALESCE(i.contact_id::text,i.open_id) person
    FROM targets t JOIN lark_identities i ON i.open_id=t.open_id AND i.identity_type='user'
    JOIN messages m USING(chat_id,message_id)
    WHERE COALESCE(i.contact_id::text,i.open_id)<>m.cm_person
) """


def _activity_all(conn, cm, start, end, group_by, chat_ids):
    key = {"day": "(m.message_create_time AT TIME ZONE 'Europe/London')::date",
           "person": "m.person", "conversation": "m.chat_id"}[group_by]
    rows = conn.execute(_ALL_ACTIVITY_SQL + f"""SELECT {key} bucket,GROUPING({key}) total,
        count(DISTINCT (m.chat_id,m.message_id)) messages,
        count(DISTINCT (m.chat_id,m.message_id)) FILTER(WHERE m.is_own) own_messages,
        count(DISTINCT (m.chat_id,m.message_id)) FILTER(WHERE m.readable) readable_messages,
        count(DISTINCT m.chat_id) conversations,
        count(DISTINCT m.person) FILTER(WHERE m.sender_kind='user' AND m.sender_open_id IS NOT NULL) active_people,
        count(DISTINCT r.person) contacted_people,
        count(DISTINCT (m.chat_id,m.message_id)) FILTER(WHERE m.sender_kind='app') app_messages,
        count(DISTINCT (m.chat_id,m.message_id)) FILTER(WHERE m.sender_kind='system') system_messages,
        count(DISTINCT (m.chat_id,m.message_id)) FILTER(WHERE m.sender_kind='unknown'
            OR m.sender_kind='user' AND m.sender_open_id IS NULL) unknown_sender_messages,
        MIN(m.person_name) person_name,MIN(m.chat_name) conversation_name,
        array_agg(DISTINCT m.chat_id) source_chats
        FROM messages m LEFT JOIN recipients r USING(chat_id,message_id)
        GROUP BY GROUPING SETS ((),({key})) ORDER BY bucket NULLS FIRST""",
        (cm["id"], start, end, chat_ids, chat_ids)).fetchall()
    metrics, values, source_chats = {}, [], []
    for row in rows:
        counts = {name: row[name] for name in ("messages", "own_messages", "readable_messages", "conversations",
            "active_people", "contacted_people", "app_messages", "system_messages", "unknown_sender_messages")}
        if row["total"]:
            metrics, source_chats = counts, row["source_chats"] or []
        else:
            value = {group_by: str(row["bucket"]), **counts}
            if group_by in {"person", "conversation"}:
                value[group_by + "_name"] = row[group_by + "_name"] or "未命名会话"
            values.append(value)
    return {"window": {"start": _iso(start), "end": _iso(end), "timezone": summaries.TIMEZONE},
            "metrics": metrics, "rows": values, "source_chat_ids": source_chats}


def activity(conn, actor_id, context, args, settings, *, now=None):
    now = now or datetime.now(UTC)
    audience = args.get("audience", "all")
    if audience not in {"all", "creators"}:
        raise ValueError("统计对象无效。")
    if audience == "creators":
        cm, roster, visible = _scope(conn, actor_id, context)
        ids = _ids(args, context, visible)
    else:
        from .assistant_search import authorize_all
        cm = authorize_all(conn, actor_id, context["scope"]["cm_id"])
        ids = []
    selected_chats = args.get("chat_ids")
    if selected_chats is None and audience == "creators":
        selected_chats = context["scope"].get("chat_ids")
    if selected_chats is not None:
        if not isinstance(selected_chats, list) or not all(isinstance(value, str) for value in selected_chats):
            raise ValueError("会话范围无效。")
        allowed = {chat["chat_id"] for chat in _chat_states(conn, cm["id"]) if chat["chat_status"] == "normal"}
        if not set(selected_chats) <= allowed:
            raise PermissionError("会话不属于当前可见范围。")
    try:
        start, end = (datetime.fromisoformat(args[key]) for key in ("start", "end"))
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError
        start, end = start.astimezone(UTC), min(end.astimezone(UTC), now)
        if start >= end:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("统计起止时间无效。") from exc
    group_by = args.get("group_by")
    groups = {"day", "person", "conversation"} if audience == "all" else {"day", "creator"}
    if group_by not in groups or args.get("comparison") not in {None, "previous"}:
        raise ValueError("统计分组或比较范围无效。")
    def collect(period_start, period_end):
        return (_activity_all(conn, cm, period_start, period_end, group_by, selected_chats)
                if audience == "all" else _activity(conn, cm, ids, period_start, period_end, group_by, selected_chats))

    result = collect(start, end)
    comparison = None
    coverage_start = start
    comparison_basis = "london_wall_time"
    if args.get("comparison") == "previous":
        local_start, local_end = start.astimezone(summaries.LONDON), end.astimezone(summaries.LONDON)
        wall_duration = local_end - local_start
        previous_local = local_start - wall_duration
        coverage_start = previous_local.astimezone(UTC)
        if wall_duration.total_seconds() <= 0 or local_start.fold or local_end.fold:
            coverage_start = start - (end - start)
            comparison_basis = "elapsed_time_repeated_dst_hour"
        elif (coverage_start >= start or coverage_start.astimezone(summaries.LONDON).replace(tzinfo=None)
              != previous_local.replace(tzinfo=None)):
            coverage_start = start - (end - start)
            comparison_basis = "elapsed_time_dst_transition"
        comparison = collect(coverage_start, start)
    if audience == "creators":
        related = summaries._related_chats(conn, roster, cm_id=cm["id"])
        chat_ids = {chat["chat_id"] for chat in related if set(chat["contact_ids"]) & set(ids)}
        chat_ids.update(result["source_chat_ids"])
        if comparison:
            chat_ids.update(comparison["source_chat_ids"])
        # Keep disabled/unreadable known conversations in the coverage, not in message counts.
        opens = sorted({row["open_id"] for row in roster if row["contact_id"] in ids})
        chat_ids.update(row["chat_id"] for row in conn.execute("""SELECT ac.chat_id FROM authorization_chats ac
        WHERE ac.authorization_id=%s AND ac.is_visible AND (ac.p2p_counterpart_open_id=ANY(%s::text[])
          OR EXISTS(SELECT 1 FROM chat_members m WHERE m.chat_id=ac.chat_id AND m.member_type='user'
                    AND m.open_id=ANY(%s::text[]))
          OR EXISTS(SELECT 1 FROM authorization_message_observations o JOIN chat_messages m USING(chat_id,message_id)
            WHERE o.authorization_id=ac.authorization_id AND o.chat_id=ac.chat_id
            AND m.sender_open_id=ANY(%s::text[]) AND NOT m.is_deleted))""",
            (cm["id"], opens, opens, opens)).fetchall())
    chats = [chat for chat in _chat_states(conn, cm["id"]) if (audience == "all" or chat["chat_id"] in chat_ids)
             and (selected_chats is None or chat["chat_id"] in selected_chats)]
    watermarks, gaps = _coverage(chats, coverage_start, end)
    cursor = _cursor(args)
    count = len(result["rows"])
    result["rows"] = result["rows"][cursor:cursor + PAGE_SIZE]
    scope = context["scope"] if audience == "creators" else {"cm_id": str(cm["id"]),
        "cm_name": cm["display_name"] or "CM", "creator_ids": None,
        "chat_ids": selected_chats, "chat_names": [chat["name"] or "会话" for chat in chats
                                                   if selected_chats is not None]}
    definition = {"messages": "当前CM可读来源内的正式达人发言，以及本人直接指向达人的消息；按会话＋消息ID去重。",
        "active_creators": "有发言或收到本人明确指向消息的达人；不按群成员推算。",
        "contacted_creators": "收到本人私聊、明确@或回复消息的不同达人；不是业务履约量。",
        "rows": "逐达人消息可能一条涉及多人，分组数不能相加代替总量；仅列有活动的分组。"}
    if audience == "all":
        definition = {"messages": "当前CM授权观察、正常且启用同步的可读会话内全部消息，含应用和系统；"
                                 "按会话＋消息ID去重。",
            "active_people": "本时段实际发言的可识别人类，含当前CM；同一contact_id合并，否则按open_id去重，"
                             "不由群成员推算。",
            "contacted_people": "当前CM实际私聊、唯一明确@或直接回复的不同真人；不含本人，不推断群广播受众。",
            "app_messages": "发送类型为app或bot的应用／机器人消息。",
            "system_messages": "明确标识system的发送者或消息类型；不将未知类型猜成系统。",
            "unknown_sender_messages": "发送类型未知，或人类发送者缺少open_id；消息仍计数，不编造人数。",
            "rows": "按实际发送者或会话分组；同一个人可出现在多天／会话中，分组人数不可相加代替总人数。"}
    return {"kind": "activity", "scope": {**scope, "audience": audience}, **result,
        "audience": audience, "group_by": group_by,
        "total_rows": count, "next_cursor": str(cursor + PAGE_SIZE) if cursor + PAGE_SIZE < count else None,
        "comparison": comparison, "comparison_basis": comparison_basis,
        "coverage": {**({"creators": len(ids)} if audience == "creators" else {}), "conversations": len(chats),
        "watermarks": watermarks}, "gaps": gaps, "checked_at": _iso(now),
        "authorization": _authorization(cm),
        "access_fingerprint": summaries._hash([(chat["chat_id"], chat["sync_enabled"], chat["chat_status"],
            chat["sync_status"] != "not_readable") for chat in chats]),
        "_legacy_access_fingerprint": summaries._hash([(chat["chat_id"], chat["sync_enabled"], chat["chat_status"])
                                                     for chat in chats]),
        "definition": {**definition,
            "readable_messages": "有正文密文的消息数；未解密核验文本，不代表附件内容已分析。",
            "comparison": "previous为相邻前一英国墙钟等长时段；涉及重复小时或不存在的钟面时间时按实际经过时间比较。",
            "day_counts": {"current": ((end - start) if comparison_basis.startswith("elapsed_time") else
                end.astimezone(summaries.LONDON).replace(tzinfo=None)
                - start.astimezone(summaries.LONDON).replace(tzinfo=None)).total_seconds() / 86400,
                "previous": ((start - coverage_start) if comparison_basis.startswith("elapsed_time") else
                start.astimezone(summaries.LONDON).replace(tzinfo=None)
                - coverage_start.astimezone(summaries.LONDON).replace(tzinfo=None)).total_seconds() / 86400
                if comparison else None}}}


def validate_result(conn, actor_id, context, result, settings):
    """Invalidate changed business sources, not routine synchronization progress."""
    if result.get("kind") == "activity" and result.get("audience") == "all":
        cm = authorize_all(conn, actor_id, context["scope"]["cm_id"])
    else:
        cm, _roster, _visible = _scope(conn, actor_id, context)
    if result.get("authorization") != _authorization(cm):
        raise ValueError("消息授权已变化，请重新查询。")
    request = result["_request"]
    names = {"query_creators": creators, "list_groups": groups, "get_sync_status": sync_status,
             "activity_stats": activity}
    function = names.get(request.get("name"))
    if function is None:
        raise ValueError("查询引用无效。")
    now = datetime.fromisoformat(result["checked_at"])
    kind = result["kind"]
    if kind == "creators":
        if result.get("_snapshot") is not None:
            _validate_creator_snapshot(conn, cm, _roster, _visible, result["_snapshot"])
        else:
            # Existing saved pages lack a snapshot; never re-sort their rows during validation.
            ids = [UUID(person["id"]) for person in result["creators"]]
            if not set(ids) <= _visible:
                raise PermissionError("达人负责范围或身份已变化，请重新查询。")
            facts = _creator_facts(conn, cm, _roster, ids)
            keys = ("id", "name", "aliases", "handles", "responsible")
            for person in result["creators"]:
                previous = {key: person.get(key) for key in keys}
                current = {key: facts[person["id"]].get(key) for key in keys}
                previous["responsible"] = sorted(previous["responsible"], key=str)
                current["responsible"] = sorted(current["responsible"], key=str)
                if previous != current:
                    raise ValueError("达人身份、账号或负责关系已变化，请重新查询。")
            sources = conn.execute("SELECT p.contact_id,p.revision, (" + creator_importance._PROFILE_VALID
                + ") valid FROM creator_importance_profiles p WHERE p.contact_id=ANY(%s::uuid[]) "
                "AND p.responsible_authorization_id=%s", (ids, cm["id"])).fetchall()
            valid = {str(row["contact_id"]): row for row in sources}
            if any(person["importance"]["source_valid"] and (
                    not valid.get(person["id"], {}).get("valid")
                    or valid[person["id"]]["revision"] != person["importance"].get("revision"))
                   for person in result["creators"]):
                raise ValueError("达人重要度的消息依据已变化，请重新查询。")
        return
    legacy_activity = kind == "activity" and "audience" not in result
    args = {**request["args"], "audience": "creators"} if legacy_activity else request["args"]
    fresh = function(conn, actor_id, context, args, settings, now=now)
    if fresh["kind"] != kind:
        raise ValueError("查询引用无效。")
    if kind == "sync":
        if not set(result["source_chat_ids"]) <= set(fresh["source_chat_ids"]):
            raise ValueError("会话读取范围已变化，请重新查询。")
        return {key: fresh[key] for key in ("gaps", "watermarks")}
    if kind == "activity":
        keys = ("audience", "window", "metrics", "rows", "comparison", "access_fingerprint")
        previous = {key: result.get(key) for key in keys}
        current = {key: fresh.get(key) for key in keys}
        if legacy_activity:
            previous["audience"] = "creators"
            current["access_fingerprint"] = fresh["_legacy_access_fingerprint"]
    else:
        def stable(value):
            output = {key: value.get(key) for key in ("total", "next_cursor", kind)}
            output[kind] = [dict(item) for item in value[kind]]
            for item in output[kind]:
                if kind == "groups":
                    for key in ("visibility_checked_at", "membership_checked_at", "purpose_updated_at"):
                        item.pop(key, None)
            return output
        previous, current = stable(result), stable(fresh)
    if previous != current:
        raise ValueError("查询依据已变化，请重新查询。")
    if kind == "activity":
        return {key: fresh[key] for key in ("gaps", "coverage")}
