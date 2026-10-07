"""Owner-only routing previews and personal static lists; never send or call AI."""

import re
import unicodedata
from difflib import get_close_matches

from psycopg.types.json import Jsonb

from . import contact_handles, creator_access
from .creator_group_evidence import PURPOSES as GROUP_PURPOSES
from .db import connection
from .handle_evidence import normalize_handle

PURPOSES = {key: value for key, value in GROUP_PURPOSES.items() if key != "unknown"}
MEMBERSHIP_NOTE = "来自当前库快照，发送前需实测核验成员及 @ 权限"
BLOCKED_REASON = "发送权限未就绪，当前仅预览"


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _scope(conn, actor_id, cm_id):
    creator_access.require_owner(conn, creator_access.parse_id(actor_id))
    if cm_id == "all":
        return None
    cm = creator_access.parse_id(cm_id)
    return creator_access.access(conn, actor_id, cm, read_only=True)[0]


def _purpose(value):
    if not isinstance(value, str) or value not in PURPOSES:
        raise ValueError("请选择有效的群类型。")
    return value


def _people(conn, cm_id, contact_ids=None):
    clauses = ["EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=c.id "
               "AND r.role='creator' AND r.status='confirmed')"]
    params = []
    if cm_id is not None:
        clauses.append("""c.id IN (SELECT contact_id FROM contact_assignments
            WHERE responsible_authorization_id=%s AND ended_at IS NULL
            UNION SELECT contact_id FROM creator_account_assignments
            WHERE responsible_authorization_id=%s AND ended_at IS NULL)""")
        params.extend((cm_id, cm_id))
    if contact_ids is not None:
        clauses.append("c.id=ANY(%s::uuid[])")
        params.append(contact_ids)
    people = conn.execute("""SELECT c.id,c.display_name,
        ARRAY(SELECT DISTINCT i.display_name FROM lark_identities i WHERE i.contact_id=c.id
          AND i.identity_type='user' AND NULLIF(btrim(i.display_name),'') IS NOT NULL) AS aliases
        FROM contacts c WHERE """ + " AND ".join(clauses)
        + " ORDER BY c.display_name NULLS LAST,c.id", params).fetchall()
    handles = contact_handles.for_contacts(conn, [person["id"] for person in people], cm_id)
    return [{"id": str(person["id"]), "name": person["display_name"] or "未命名用户",
             "aliases": person["aliases"], "handles": handles[person["id"]]} for person in people]


def _groups(conn, contact_ids, cm_id, purpose):
    if not contact_ids:
        return []
    rows = conn.execute("""SELECT DISTINCT ch.chat_id,ch.name,g.purpose,i.contact_id,
        snapshot.member_count,snapshot.member_count_status,snapshot.member_snapshot_checked_at,
        snapshot.member_snapshot_status
        FROM lark_identities i JOIN chat_members m ON m.open_id=i.open_id
        JOIN chats ch ON ch.chat_id=m.chat_id
        JOIN chat_group_classifications g ON g.chat_id=ch.chat_id AND g.purpose=%s
        JOIN LATERAL (
          SELECT ac.member_count,ac.member_count_status,ac.member_snapshot_checked_at,
                 ac.member_snapshot_status
          FROM authorization_chats ac JOIN authorizations a ON a.id=ac.authorization_id
          WHERE ac.chat_id=ch.chat_id AND ac.is_visible AND ac.sync_enabled
            AND a.status='active' AND a.body_consent_at IS NOT NULL
            AND (%s::uuid IS NULL OR a.id=%s::uuid)
          ORDER BY ac.member_snapshot_checked_at DESC NULLS LAST,a.id LIMIT 1
        ) snapshot ON TRUE
        WHERE i.contact_id=ANY(%s::uuid[]) AND i.identity_type='user' AND m.member_type='user'
          AND COALESCE(ch.chat_status,'normal')='normal'
          AND COALESCE(ch.chat_mode,'group')<>'p2p'
        ORDER BY ch.name,ch.chat_id,i.contact_id""", (purpose, cm_id, cm_id, contact_ids)).fetchall()
    groups = {}
    for row in rows:
        group = groups.setdefault(row["chat_id"], {
            "id": row["chat_id"], "name": row["name"] or "未命名群", "purpose": row["purpose"],
            "members": [], "member_count": row["member_count"] if row["member_count_status"] == "succeeded" else None,
            "membership_checked_at": row["member_snapshot_checked_at"].isoformat()
                if row["member_snapshot_checked_at"] else None,
            "membership_status": row["member_snapshot_status"], "membership_note": MEMBERSHIP_NOTE,
        })
        contact_id = str(row["contact_id"])
        if contact_id not in group["members"]:
            group["members"].append(contact_id)
    return list(groups.values())


def _match_entries(raw, people):
    if not isinstance(raw, str) or len(raw) > 65536:
        raise ValueError("名单不能超过 65,536 字符。")
    values = [value.strip() for value in re.split(r"[\n\r,，;；\t]+", raw) if value.strip()]
    if not values or len(values) > 500 or any(len(value) > 256 for value in values):
        raise ValueError("请填写 1–500 项，每项不超过 256 字符。")
    views = {p["id"]: {"id": p["id"], "name": p["name"], "handles": p["handles"], "groups": []}
             for p in people}
    names, handles = {}, {}
    for person in people:
        for name in [person["name"], *person.get("aliases", [])]:
            key = _normalized(name)
            if key and key != "未命名用户":
                names.setdefault(key, {})[person["id"]] = views[person["id"]]
        for handle in person["handles"]:
            try:
                key = normalize_handle(handle)
            except ValueError:
                continue
            handles.setdefault(key, {})[person["id"]] = views[person["id"]]
    combined = {key: dict(matches) for key, matches in names.items()}
    for key, matches in handles.items():
        combined.setdefault(key, {}).update(matches)
    entries = {}
    for value in values:
        text = unicodedata.normalize("NFKC", value).strip()
        explicit_handle = text.startswith("@") or bool(re.match(r"https?://", text, re.I))
        if explicit_handle:
            try:
                token = normalize_handle(re.sub(r"^https?://", lambda m: m[0].lower(), text, flags=re.I))
            except ValueError:
                token = None
            lookup = handles
            key = "handle:" + (token if token is not None else _normalized(text))
        else:
            token, lookup = _normalized(text), combined
            key = "text:" + token
        if key in entries:
            entries[key]["inputs"].append(value)
            continue
        exact = dict(lookup.get(token, {})) if token is not None else {}
        candidates = exact
        if not candidates and token is not None and len(token) >= 2:
            options = handles if explicit_handle else combined
            candidates = {}
            for option in sorted(options):
                if token in option:
                    candidates.update(options[option])
            if not candidates:
                for option in get_close_matches(token, options, n=5, cutoff=0.75):
                    candidates.update(options[option])
        matches = sorted(candidates.values(), key=lambda p: (_normalized(p["name"]), p["id"]))
        entries[key] = {"key": key, "inputs": [value], "candidates": matches[:5] if not exact else matches,
                        "person": next(iter(exact.values())) if len(exact) == 1 else None,
                        "selected": True, "approximate": not bool(exact)}
    return list(entries.values()), len(values) - len(entries)


def page_data(actor_id):
    with connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        _scope(conn, actor_id, "all")
        cms = conn.execute("""SELECT id,display_name FROM authorizations
            WHERE status='active' AND body_consent_at IS NOT NULL ORDER BY display_name,id""").fetchall()
        return {"purposes": PURPOSES, "cms": [{"id": str(cm["id"]), "name": cm["display_name"] or "未命名用户"}
                                             for cm in cms],
                "selected_cm_id": "all", "preview_only": True, "lists": _lists(conn, actor_id)}


def match(actor_id, cm_id, raw, purpose):
    with connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        cm = _scope(conn, actor_id, cm_id)
        _purpose(purpose)
        entries, duplicates = _match_entries(raw, _people(conn, cm))
        candidates = {candidate["id"]: candidate for entry in entries for candidate in entry["candidates"]}
        groups = _groups(conn, list(candidates), cm, purpose)
        for group in groups:
            for contact_id in group["members"]:
                candidates[contact_id]["groups"].append(group["id"])
        return {"entries": entries, "groups": groups, "count": len(entries), "duplicate_inputs": duplicates,
                "purpose": purpose, "selected_cm_id": "all" if cm is None else str(cm),
                "preview_only": True, "can_send": False, "blocked_reason": BLOCKED_REASON}


def _targets(values, *, one_per_contact=False):
    if not isinstance(values, list) or not 1 <= len(values) <= 500:
        raise ValueError("请选择 1–500 个接收对象。")
    targets, chosen = [], {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("接收对象格式无效。")
        contact_id = str(creator_access.parse_id(item.get("contact_id")))
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip() or len(chat_id) > 256:
            raise ValueError("目标群标识无效。")
        if one_per_contact and contact_id in chosen and chosen[contact_id] != chat_id:
            raise ValueError("同一名单中，每位达人只能选择一个目标群。")
        chosen[contact_id] = chat_id
        pair = (contact_id, chat_id)
        if pair not in targets:
            targets.append(pair)
    return targets


def _resolve_scope(conn, actor_id, cm_id):
    creator_access.require_owner(conn, creator_access.parse_id(actor_id))
    if cm_id == "all":
        return None, ""
    cm = creator_access.parse_id(cm_id)
    return cm, "" if creator_access.authorized_cm(conn, cm) else "名单所属 CM/AM 的授权已失效"


def _resolve(conn, cm, purpose, contact_ids, selections=None, scope_reason=""):
    _purpose(purpose)
    if not isinstance(contact_ids, list) or not 1 <= len(contact_ids) <= 500:
        raise ValueError("请选择 1–500 位达人。")
    ids = list(dict.fromkeys(str(creator_access.parse_id(value)) for value in contact_ids))
    fixed = None if selections is None else dict(_targets(selections, one_per_contact=True))
    if fixed is not None and set(fixed) != set(ids):
        raise ValueError("保存的目标必须与固定达人名单一致。")
    people = {} if scope_reason else {p["id"]: p for p in _people(conn, cm, ids)}
    groups = [] if scope_reason else _groups(conn, list(people), cm, purpose)
    memberships = {contact_id: [] for contact_id in ids}
    for group in groups:
        for contact_id in group["members"]:
            memberships[contact_id].append(group["id"])
    entries, routes, issues = [], [], []
    for contact_id in ids:
        current = people.get(contact_id)
        person = ({"id": contact_id, "name": current["name"], "handles": current["handles"],
                   "groups": memberships[contact_id]} if current else None)
        reason = scope_reason or ("达人已不在当前入库或负责人范围内" if not person else "")
        chat_id = fixed.get(contact_id, "") if fixed is not None else (
            memberships[contact_id][0] if len(memberships[contact_id]) == 1 else "")
        if not reason and fixed is not None and chat_id not in memberships[contact_id]:
            reason = "保存的目标群授权、分类或人员快照已变化，请重新选择"
        if reason:
            issues.append({"contact_id": contact_id, "reason": reason})
        entries.append({"key": "contact:" + contact_id, "inputs": [person["name"] if person else contact_id],
                        "candidates": [person] if person else [], "person": person,
                        "selected": not bool(reason), "approximate": False, "reason": reason})
        routes.append({"contact_id": contact_id, "chat_id": "" if reason else chat_id})
    return {"entries": entries, "groups": groups, "issues": issues, "routes": routes, "count": len(entries),
            "duplicate_inputs": len(contact_ids) - len(ids), "purpose": purpose,
            "selected_cm_id": "all" if cm is None else str(cm),
            "preview_only": True, "can_send": False, "blocked_reason": BLOCKED_REASON}


def resolve(actor_id, cm_id, purpose, contact_ids, selections=None):
    with connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        cm, reason = _resolve_scope(conn, actor_id, cm_id)
        return _resolve(conn, cm, purpose, contact_ids, selections, reason)


def _list_metadata(row):
    return {"id": str(row["id"]), "name": row["name"], "notes": row["notes"], "revision": row["revision"],
            "purpose": row["purpose"], "mention": row["mention"],
            "cm_id": str(row["cm_id"]) if row["cm_id"] else "all",
            "target_count": row.get("target_count", len(row.get("targets", [])))}


def _lists(conn, actor_id):
    rows = conn.execute("""SELECT id,name,notes,revision,purpose,mention,cm_id,
        jsonb_array_length(targets) AS target_count FROM creator_broadcast_lists
        WHERE owner_authorization_id=%s AND deleted_at IS NULL ORDER BY updated_at DESC,id""",
        (creator_access.parse_id(actor_id),)).fetchall()
    return [_list_metadata(row) for row in rows]


def _owned_list(conn, actor_id, list_id):
    row = conn.execute("""SELECT * FROM creator_broadcast_lists
        WHERE id=%s AND owner_authorization_id=%s AND deleted_at IS NULL""",
        (creator_access.parse_id(list_id), creator_access.parse_id(actor_id))).fetchone()
    if not row:
        raise LookupError("找不到该名单。")
    return row


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int | str) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)):
        raise ValueError("名单版本无效，请刷新后重试。")
    revision = int(value)
    if revision > 2_147_483_647:
        raise ValueError("名单版本无效，请刷新后重试。")
    return revision


def list_save(actor_id, fields):
    with connection() as conn, conn.transaction():
        _scope(conn, actor_id, "all")
        if not isinstance(fields, dict):
            raise ValueError("名单格式无效。")
        list_id = fields.get("id")
        existing = _owned_list(conn, actor_id, list_id) if list_id else None
        metadata_only = fields.get("metadata_only", False)
        if not isinstance(metadata_only, bool) or (metadata_only and not existing):
            raise ValueError("请先选择已保存的名单。")
        revision = _revision(fields.get("revision")) if existing else None
        if existing and existing["revision"] != revision:
            raise ValueError("名单已被修改，请重新载入后再保存。")
        name = fields.get("name")
        notes = fields.get("notes", existing["notes"] if metadata_only else "")
        if not isinstance(name, str) or "\x00" in name or not 1 <= len(name.strip()) <= 80:
            raise ValueError("名单名称须为 1–80 字符。")
        if not isinstance(notes, str) or "\x00" in notes or len(notes) > 500:
            raise ValueError("名单备注不能超过 500 字符。")
        if metadata_only:
            row = conn.execute("""UPDATE creator_broadcast_lists
                SET name=%s,notes=%s,revision=revision+1,updated_at=NOW()
                WHERE id=%s AND owner_authorization_id=%s AND revision=%s AND deleted_at IS NULL RETURNING *""",
                (name.strip(), notes, existing["id"], creator_access.parse_id(actor_id), revision)).fetchone()
        else:
            cm = _scope(conn, actor_id, fields.get("cm_id"))
            purpose = _purpose(fields.get("purpose"))
            mention = fields.get("mention")
            if not isinstance(mention, bool):
                raise ValueError("@ 选项无效。")
            targets = _targets(fields.get("selections"), one_per_contact=True)
            selections = [{"contact_id": contact, "chat_id": chat} for contact, chat in targets]
            checked = _resolve(conn, cm, purpose, [contact for contact, _ in targets], selections)
            if checked["issues"]:
                raise ValueError("名单中有已失效的达人或目标群，请重新匹配后保存。")
            snapshot = {"cm_id": "all" if cm is None else str(cm), "purpose": purpose, "channel": "group"}
            values = (name.strip(), notes, cm, purpose, mention, Jsonb(selections), Jsonb(snapshot))
            if existing:
                row = conn.execute("""UPDATE creator_broadcast_lists
                    SET name=%s,notes=%s,cm_id=%s,purpose=%s,mention=%s,targets=%s,snapshot=%s,
                        revision=revision+1,updated_at=NOW()
                    WHERE id=%s AND owner_authorization_id=%s AND revision=%s AND deleted_at IS NULL RETURNING *""",
                    (*values, existing["id"], creator_access.parse_id(actor_id), revision)).fetchone()
            else:
                row = conn.execute("""INSERT INTO creator_broadcast_lists
                    (name,notes,cm_id,purpose,mention,targets,snapshot,owner_authorization_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (*values, creator_access.parse_id(actor_id))).fetchone()
        if not row:
            raise ValueError("名单已被修改或删除，请重新载入。")
        return {"list": _list_metadata(row), "lists": _lists(conn, actor_id)}


def list_load(actor_id, list_id):
    with connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        _scope(conn, actor_id, "all")
        row = _owned_list(conn, actor_id, list_id)
        cm_id = str(row["cm_id"]) if row["cm_id"] else "all"
        cm, reason = _resolve_scope(conn, actor_id, cm_id)
        data = _resolve(conn, cm, row["purpose"], [target["contact_id"] for target in row["targets"]],
                        row["targets"], reason)
        data.update(list={**_list_metadata(row), "targets": row["targets"], "snapshot": row["snapshot"],
                          "channel": "group"}, cm_id=cm_id, mention=row["mention"])
        return data


def list_delete(actor_id, list_id, revision):
    with connection() as conn, conn.transaction():
        _scope(conn, actor_id, "all")
        existing = _owned_list(conn, actor_id, list_id)
        row = conn.execute("""UPDATE creator_broadcast_lists
            SET deleted_at=NOW(),updated_at=NOW(),revision=revision+1
            WHERE id=%s AND owner_authorization_id=%s AND revision=%s AND deleted_at IS NULL RETURNING id""",
            (existing["id"], creator_access.parse_id(actor_id), _revision(revision))).fetchone()
        if not row:
            raise ValueError("名单已被修改或删除，请重新载入。")
        return {"lists": _lists(conn, actor_id)}


def preview(actor_id, cm_id, purpose, selections, message, mention):
    with connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        cm = _scope(conn, actor_id, cm_id)
        _purpose(purpose)
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ValueError("请填写 1–4,000 字符的消息。")
        if not isinstance(mention, bool):
            raise ValueError("@ 选项无效。")
        selected = _targets(selections)
        contact_ids = list(dict.fromkeys(contact_id for contact_id, _ in selected))
        people = {p["id"]: {"id": p["id"], "name": p["name"]} for p in _people(conn, cm, contact_ids)}
        available = {g["id"]: g for g in _groups(conn, list(people), cm, purpose)}
        groups, issues = {}, []
        for contact_id, chat_id in selected:
            person, group = people.get(contact_id), available.get(chat_id)
            if not person:
                issues.append({"contact_id": contact_id, "reason": "达人已不在当前入库或负责人范围内"})
            elif not group or contact_id not in group["members"]:
                issues.append({"contact_id": contact_id, "reason": "目标群授权、分类或人员快照已变化，请重新匹配"})
            else:
                target = groups.setdefault(chat_id, {**group, "people": [], "mentions": []})
                target["people"].append(person)
                if mention:
                    target["mentions"].append(person)
        return {"groups": list(groups.values()), "issues": issues, "people": list(people.values()),
                "message": message, "purpose": purpose, "selected_cm_id": "all" if cm is None else str(cm),
                "preview_only": True, "can_send": False, "blocked_reason": BLOCKED_REASON}


if __name__ == "__main__":
    sample = [{"id": "p1", "name": "Ava Collins", "handles": ["demo_ava", "demo_ava_backup"]},
              {"id": "p2", "name": "Ben Carter", "handles": ["demo_ben"]},
              {"id": "p3", "name": "Ben Carter", "handles": ["demo_ben_live"]},
              {"id": "p4", "name": "Demo Ava", "handles": ["shared_demo"], "aliases": ["Ava Collins", "Demo"]},
              {"id": "p5", "name": "Other Ava", "handles": ["shared_demo", "demo"]}]
    entries, duplicates = _match_entries("@DEMO_AVA\nhttps://tiktok.com/@demo_ava\nBen Carter\nAva", sample)
    assert duplicates == 1 and len(entries) == 3
    assert entries[0]["person"]["id"] == "p1"
    assert entries[1]["person"] is None and len(entries[1]["candidates"]) == 2
    assert entries[2]["person"] is None and entries[2]["approximate"]
    assert _match_entries("https://tiktok.com.evil.test/@demo_ava", sample)[0][0]["candidates"] == []
    alias = _match_entries("Ａｖａ　Ｃｏｌｌｉｎｓ", sample)[0][0]
    assert alias["person"] is None and {p["id"] for p in alias["candidates"]} == {"p1", "p4"}
    shared = _match_entries("@shared_demo", sample)[0][0]
    assert shared["person"] is None and {p["id"] for p in shared["candidates"]} == {"p4", "p5"}
    approximate = _match_entries("dem", sample)[0][0]
    assert approximate["person"] is None and {p["id"] for p in approximate["candidates"]} >= {"p4", "p5"}
    try:
        _match_entries("name\n" * 501, sample)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized input accepted")
    contact = "00000000-0000-0000-0000-000000000001"
    target = {"contact_id": contact, "chat_id": "demo-fixed"}
    assert _targets([target, target], one_per_contact=True) == [(contact, "demo-fixed")]
    try:
        _targets([target, {**target, "chat_id": "demo-other"}], one_per_contact=True)
    except ValueError:
        pass
    else:
        raise AssertionError("multiple target groups accepted in a static list")
    revoked_scope = _resolve(None, contact, "support", [contact], [target], "CM 授权已失效")
    assert revoked_scope["entries"][0]["person"] is None and not revoked_scope["entries"][0]["selected"]
    assert revoked_scope["routes"] == [{"contact_id": contact, "chat_id": ""}]
    assert len(revoked_scope["issues"]) == 1 and not revoked_scope["can_send"]
    print("Creator broadcast matching/list check passed; no database or API accessed.")
