"""Confirmed, user-scoped delivery. Unknown outcomes are never blindly retried."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.fernet import InvalidToken
from psycopg.types.json import Jsonb

from . import broadcast_content, broadcast_images, creator_access, creator_broadcast
from .config import get_settings
from .crypto import TokenCipher, message_body_cipher, message_body_fingerprint
from .db import connection
from .lark_api import LarkApiError, LarkClient, build_chat_post_payload

STATUSES = ("pending", "sending", "succeeded", "failed", "unknown", "cancelled", "duplicate")
UNKNOWN_MESSAGE = "发送结果未知，请到 Lark 核实；不会自动重发"


def _body(ciphertext, snapshot, settings):
    value = message_body_cipher(settings.token_encryption_key).decrypt(ciphertext)
    return broadcast_content.normalize(json.loads(value)) if snapshot.get("content_version") == 2 else value


def _images(conn, actor, body, settings):
    ids = broadcast_content.image_ids(body) if isinstance(body, dict) else []
    return broadcast_images.resolve(conn, actor, ids, settings)


def _target_fingerprint(body, group, image_hashes, settings):
    content = broadcast_content.post_rows(broadcast_content.normalize(body), image_hashes)
    canonical = {"chat": group["id"], "content": content, "mention_all": bool(group.get("mention_all")),
                 "mentions": sorted({item["id"] for item in group.get("mentions", [])})}
    return message_body_fingerprint(settings.token_encryption_key, json.dumps(canonical, sort_keys=True))


def _fingerprint_material(conn, actor, ciphertext, snapshot, settings):
    body = _body(ciphertext, snapshot, settings)
    ids = broadcast_content.image_ids(body) if isinstance(body, dict) else []
    hashes = {str(row["id"]): "img_" + row["content_sha256"] for row in conn.execute(
        """SELECT id,content_sha256 FROM creator_broadcast_images
        WHERE owner_authorization_id=%s AND id=ANY(%s::uuid[])""",
        (actor, ids)).fetchall()} if ids else {}
    return body, hashes


def _recent_duplicates(conn, actor, fingerprints, settings, *, exclude_batch=None):
    recent = conn.execute(
        """SELECT t.id,t.chat_id,t.batch_id,t.snapshot,p.body_ciphertext,p.snapshot AS preview_snapshot
        FROM creator_broadcast_batches b
        JOIN creator_broadcast_targets t ON t.batch_id=b.id
        JOIN creator_broadcast_previews p ON p.id=b.preview_id
        WHERE b.sender_authorization_id=%s AND t.chat_id=ANY(%s::text[])
          AND b.created_at>NOW()-INTERVAL '10 minutes' AND t.status IN ('pending','sending','succeeded','unknown')
          AND (%s::uuid IS NULL OR b.id<>%s)
        ORDER BY b.created_at DESC""", (actor, list(fingerprints), exclude_batch, exclude_batch),
    ).fetchall()
    duplicates, legacy = {}, {}
    for target in recent:
        fingerprint = target["snapshot"].get("content_fingerprint")
        if not fingerprint:
            # Old receipts remain protected during the same ten-minute window without a data migration.
            if target["batch_id"] not in legacy:
                legacy[target["batch_id"]] = _fingerprint_material(
                    conn, actor, target["body_ciphertext"], target["preview_snapshot"], settings)
            old_body, old_hashes = legacy[target["batch_id"]]
            fingerprint = _target_fingerprint(old_body, target["snapshot"], old_hashes, settings)
        if fingerprint == fingerprints[target["chat_id"]]:
            duplicates.setdefault(target["chat_id"], target)
    return duplicates


def _audit(conn, actor, action, metadata):
    conn.execute(
        """INSERT INTO audit_events(id,authorization_id,action,metadata)
        VALUES(%s,%s,%s,%s)""",
        (uuid4(), actor, "broadcast." + action, Jsonb(metadata)),
    )


def _authorization(conn, actor):
    creator_access.require_broadcast_access(conn, actor)
    row = conn.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
    if not row or row["status"] != "active" or not row["body_consent_at"]:
        raise PermissionError("本人聊天授权已失效，请先完成接入。")
    required = set(get_settings().oauth_scopes) | set(creator_broadcast.SEND_SCOPES)
    if not required.issubset(row["scopes"] or ()):
        raise creator_broadcast.BroadcastAuthorizationRequired(actor)
    return row


def _client_token(actor, client, settings):
    from .worker import ReauthorizationRequired, _usable_access_token

    with connection() as conn:
        authorization = _authorization(conn, actor)
    try:
        token = _usable_access_token(actor, authorization, client, settings)
    except ReauthorizationRequired as exc:
        raise creator_broadcast.BroadcastAuthorizationRequired(actor) from exc
    with connection() as conn:
        authorization = _authorization(conn, actor)
    if TokenCipher(settings.token_encryption_key).decrypt(authorization["access_token_ciphertext"]) != token:
        raise PermissionError("授权已更新，请重新准备发送。")
    if client.user_info(token).open_id != authorization["open_id"]:
        raise PermissionError("Lark 账号与当前登录账号不一致，请重新授权。")
    return authorization, token


def _unchanged(conn, actor, checked):
    current = _authorization(conn, actor)
    if any(
        current[key] != checked[key]
        for key in (
            "open_id",
            "access_token_ciphertext",
            "body_consent_at",
            "scopes",
        )
    ):
        raise PermissionError("授权已变化，请重新检查后发送。")
    return current


def _routes_current(conn, actor, purpose, groups):
    chat_ids = [group["id"] for group in groups]
    contacts = list({person["id"] for group in groups for person in group.get("people", [])})
    if purpose == "support":
        people = creator_broadcast._people(conn, actor, contacts)
        if {person["id"] for person in people} != set(contacts):
            raise PermissionError("达人或负责人范围已变化，请重新选择。")
        current = {item["id"]: item for item in creator_broadcast._groups(conn, contacts, actor, purpose, chat_ids)}
        for group in groups:
            if group["id"] not in current or not {person["id"] for person in group.get("people", [])}.issubset(
                current.get(group["id"], {}).get("members", [])
            ):
                raise PermissionError("目标群或人员关系已变化，请重新选择。")
        mentions = {(mention["id"], creator_access.parse_id(mention["contact_id"]), group["id"])
                    for group in groups for mention in group.get("mentions", [])}
        if mentions:
            valid = {(row["open_id"], row["contact_id"], row["chat_id"]) for row in conn.execute(
                """SELECT i.open_id,i.contact_id,m.chat_id FROM lark_identities i
                JOIN chat_members m ON m.open_id=i.open_id
                WHERE i.open_id=ANY(%s::text[]) AND i.contact_id=ANY(%s::uuid[])
                  AND m.chat_id=ANY(%s::text[]) AND m.member_type='user'""",
                (list({item[0] for item in mentions}), list({item[1] for item in mentions}), chat_ids),
            ).fetchall()}
            if not mentions.issubset(valid):
                raise PermissionError("被 @ 人员的身份关联已变化，请重新选择。")
    elif set(chat_ids) != {item["id"] for item in creator_broadcast._joined_groups(conn, actor, purpose, chat_ids)}:
        raise PermissionError("目标群授权、分类或可见性已变化，请重新选择。")


def _live_group(client, token, authorization, group):
    return client.check_chat_send_access(
        token,
        group["id"],
        sender_open_id=authorization["open_id"],
        mention_all=bool(group.get("mention_all")),
        mention_open_ids=[mention["id"] for mention in group.get("mentions", [])],
    )


def _issue(group, error):
    if isinstance(error, LarkApiError) and error.status_code == 403:
        reason = {
            "Sender is no longer a group member": "您已不在该群中",
            "Group is unavailable for broadcast": "群聊已失效或不支持群发",
            "Sender cannot post in this group": "您没有该群的发言权限",
            "Sender cannot mention everyone in this group": "您没有该群的 @all 权限",
            "Mentioned person is no longer a group member": "被 @ 的达人已不在该群中",
            "Group posting permission changed during verification": "群发言权限已变化，请重新检查",
        }.get(str(error), "您无权访问或发送到该群")
    elif isinstance(error, LarkApiError) and error.rate_limited:
        reason = "Lark 暂时限流，请稍后重新检查"
    else:
        reason = "暂时无法核验群权限，请稍后重新检查"
    return {"chat_id": group["id"], "name": group["name"], "reason": reason}


def prepare(actor_id, fields):
    if not isinstance(fields, dict):
        raise ValueError("发送参数无效。")
    actor = creator_access.parse_id(actor_id)
    if fields.get("cm_id") != str(actor):
        raise PermissionError("只能以本人账号发送，请选择您自己。")
    settings = get_settings()
    with connection() as conn:
        _authorization(conn, actor)
    data = creator_broadcast.preview(
        actor,
        str(actor),
        fields.get("purpose"),
        fields.get("selections"),
        fields.get("message"),
        fields.get("mention"),
    )
    groups, issues = [], list(data.get("issues", []))
    with connection() as conn:
        image_assets = _images(conn, actor, data["message"], settings)
        image_keys = {key: image["image_key"] for key, image in image_assets.items()}
        targets = {(person["id"], group["id"]) for group in data["groups"] for person in group.get("mentions", [])}
        identities = {}
        if targets:
            contacts, chats = zip(*targets, strict=True)
            for row in conn.execute("""SELECT DISTINCT i.contact_id,m.chat_id,i.open_id FROM lark_identities i
                JOIN chat_members m ON m.open_id=i.open_id
                JOIN unnest(%s::uuid[],%s::text[]) wanted(contact_id,chat_id)
                  ON wanted.contact_id=i.contact_id AND wanted.chat_id=m.chat_id
                WHERE i.identity_type='user' AND m.member_type='user'""", (list(contacts), list(chats))).fetchall():
                identities.setdefault((str(row["contact_id"]), row["chat_id"]), []).append(row["open_id"])
        for original in data["groups"]:
            group = {**original, "mentions": [], "mention_all": bool(original.get("mention_all"))}
            invalid = False
            for person in original.get("mentions", []):
                candidates = identities.get((person["id"], group["id"]), [])
                if len(candidates) != 1:
                    issues.append(
                        {"chat_id": group["id"], "name": group["name"], "reason": "被 @ 人员的群内身份不唯一，请先核实"}
                    )
                    invalid = True
                    break
                group["mentions"].append(
                    {"id": candidates[0], "name": person["name"], "contact_id": person["id"]}
                )
            if not invalid:
                try:
                    build_chat_post_payload(
                        group["id"],
                        data["message"],
                        mentions=group["mentions"],
                        mention_all=group["mention_all"],
                        message_uuid=str(uuid4()),
                        image_keys=image_keys,
                    )
                except ValueError:
                    issues.append(
                        {
                            "chat_id": group["id"],
                            "name": group["name"],
                            "reason": "消息格式或长度不受 Lark 支持，请减少内容或 @ 人数",
                        }
                    )
                else:
                    groups.append(group)
    if not groups:
        return {**data, "groups": [], "issues": issues, "preview_id": None, "can_send": False}
    try:
        with httpx.Client(timeout=8) as http:
            client = LarkClient(settings, http_client=http)
            authorization, _token = _client_token(actor, client, settings)
    except (LarkApiError, httpx.HTTPError, InvalidToken, InvalidTag, ValueError):
        raise ValueError("暂时无法核验本人连接，请稍后重试") from None
    rich = isinstance(data["message"], dict)
    content_version = 2 if rich else 1
    image_ids = broadcast_content.image_ids(data["message"]) if rich else []
    image_hashes = {key: "img_" + image["content_sha256"] for key, image in image_assets.items()}
    groups = [{**group, "content_version": content_version, "image_ids": image_ids,
               "content_fingerprint": _target_fingerprint(data["message"], group, image_hashes, settings)}
              for group in groups]
    snapshot = {"groups": groups, "mention": fields["mention"], "content_version": content_version,
                "image_ids": image_ids}
    fingerprint = message_body_fingerprint(
        settings.token_encryption_key, json.dumps(sorted(group["content_fingerprint"] for group in groups))
    )
    with connection() as conn, conn.transaction():
        _unchanged(conn, actor, authorization)
        _images(conn, actor, data["message"], settings)
        _routes_current(conn, actor, data["purpose"], groups)
        row = conn.execute(
            """INSERT INTO creator_broadcast_previews
            (sender_authorization_id,sender_open_id,sender_name,purpose,body_ciphertext,snapshot,fingerprint)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,expires_at""",
            (
                actor,
                authorization["open_id"],
                authorization["display_name"] or "本人",
                data["purpose"],
                message_body_cipher(settings.token_encryption_key).encrypt(
                    json.dumps(data["message"], ensure_ascii=False, separators=(",", ":"))
                    if rich else data["message"]),
                Jsonb(snapshot),
                fingerprint,
            ),
        ).fetchone()
        _audit(conn, actor, "prepare", {"preview_id": str(row["id"]), "targets": len(groups)})
    return {
        **data,
        "groups": groups,
        "issues": issues,
        "preview_id": str(row["id"]),
        "account_name": authorization["display_name"] or "本人",
        "expires_at": row["expires_at"].isoformat(),
        "preview_only": False,
        "can_send": True,
        "blocked_reason": "",
    }


def _owned_preview(conn, actor, preview_id):
    row = conn.execute(
        """SELECT * FROM creator_broadcast_previews
        WHERE id=%s AND sender_authorization_id=%s""",
        (preview_id, actor),
    ).fetchone()
    if not row:
        raise LookupError("发送预览不存在，请重新准备。")
    return row


def _owned_batch(conn, actor, batch_id):
    # Own receipts remain available after scope expiry; other people's data never does.
    creator_access.require_broadcast_access(conn, actor)
    row = conn.execute(
        """SELECT b.*,p.sender_name,p.purpose FROM creator_broadcast_batches b
        JOIN creator_broadcast_previews p ON p.id=b.preview_id
        WHERE b.id=%s AND b.sender_authorization_id=%s""",
        (batch_id, actor),
    ).fetchone()
    if not row:
        raise LookupError("发送记录不存在。")
    return row


def _summary(conn, row, targets=None):
    counts = dict.fromkeys(STATUSES, 0)
    can_retry = False
    if targets is None:
        targets = conn.execute(
            """SELECT CASE WHEN status='cancelled' AND snapshot ? 'duplicate_of' THEN 'duplicate'
            ELSE status END AS status,COUNT(*) AS n,BOOL_OR(retryable) AS retryable FROM creator_broadcast_targets
            WHERE batch_id=%s GROUP BY 1""", (row["id"],),
        ).fetchall()
    for item in targets:
        counts[item["status"]] += item.get("n", 1)
        can_retry = can_retry or bool(item["retryable"])
    total = sum(counts.values())
    remaining = total - counts["duplicate"]
    if not remaining:
        status = "duplicate"
    elif counts["sending"]:
        status = "sending"
    elif counts["pending"]:
        status = "pending" if counts["pending"] == remaining else "sending"
    elif counts["unknown"]:
        status = "unknown"
    else:
        status = next((key for key in ("succeeded", "failed", "cancelled") if counts[key] == remaining), "partial")
    return {
        "id": str(row["id"]),
        "status": status,
        "created_at": row["created_at"].isoformat(),
        "account_name": row["sender_name"],
        "purpose": row["purpose"],
        "total": total,
        **counts,
        "can_retry": can_retry,
    }


def _existing(conn, actor, preview_id, request_id):
    row = conn.execute(
        """SELECT b.*,p.sender_name,p.purpose FROM creator_broadcast_batches b
        JOIN creator_broadcast_previews p ON p.id=b.preview_id
        WHERE b.sender_authorization_id=%s AND (b.request_id=%s OR b.preview_id=%s)
        ORDER BY b.created_at LIMIT 1""",
        (actor, request_id, preview_id),
    ).fetchone()
    if row and row["request_id"] == request_id and row["preview_id"] != preview_id:
        raise ValueError("请求编号已用于其他发送，请重新准备。")
    return row


def submit(actor_id, preview_id, request_id):
    actor, preview_id, request_id = map(creator_access.parse_id, (actor_id, preview_id, request_id))
    with connection() as conn, conn.transaction():
        # One actor lock serializes submissions, cancellation and worker admission.
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR UPDATE", (actor,))
        creator_access.require_broadcast_access(conn, actor)
        existing = _existing(conn, actor, preview_id, request_id)
        if existing:
            return {"batch": _summary(conn, existing), "deduplicated": True}
        preview = _owned_preview(conn, actor, preview_id)
        if preview["expires_at"] <= datetime.now(UTC):
            raise ValueError("预览已过期，请重新准备发送。")
        authorization = _authorization(conn, actor)
        if authorization["open_id"] != preview["sender_open_id"]:
            raise PermissionError("发送账号已变化，请重新准备。")
        settings = get_settings()
        groups = preview["snapshot"]["groups"]
        body = _body(preview["body_ciphertext"], preview["snapshot"], settings)
        image_assets = _images(conn, actor, body, settings)
        image_hashes = {key: "img_" + image["content_sha256"] for key, image in image_assets.items()}
        fingerprints = {group["id"]: _target_fingerprint(body, group, image_hashes, settings) for group in groups}
        duplicates = _recent_duplicates(conn, actor, fingerprints, settings)
        if len(duplicates) == len(groups) and len({target["batch_id"] for target in duplicates.values()}) == 1:
            existing = _owned_batch(conn, actor, next(iter(duplicates.values()))["batch_id"])
            previous_chats = {row["chat_id"] for row in conn.execute(
                "SELECT chat_id FROM creator_broadcast_targets WHERE batch_id=%s", (existing["id"],)).fetchall()}
            if previous_chats == set(fingerprints):
                return {"batch": _summary(conn, existing), "deduplicated": True}
        _routes_current(conn, actor, preview["purpose"], groups)
        row = conn.execute(
            """INSERT INTO creator_broadcast_batches(sender_authorization_id,preview_id,request_id)
            VALUES(%s,%s,%s) RETURNING id""",
            (actor, preview_id, request_id),
        ).fetchone()
        for group in groups:
            duplicate = duplicates.get(group["id"])
            conn.execute(
                """INSERT INTO creator_broadcast_targets(batch_id,chat_id,chat_name,snapshot,status,error_message)
                VALUES(%s,%s,%s,%s,%s,%s)""",
                (row["id"], group["id"], group["name"],
                 Jsonb({**group, "content_fingerprint": fingerprints[group["id"]],
                        **({"duplicate_of": str(duplicate["id"])} if duplicate else {})}),
                 "cancelled" if duplicate else "pending", "近期已有相同消息，未重复发送" if duplicate else None),
            )
        _audit(conn, actor, "submit", {"batch_id": str(row["id"]), "targets": len(preview["snapshot"]["groups"])})
        return {"batch": _summary(conn, _owned_batch(conn, actor, row["id"]))}


def history(actor_id):
    actor = creator_access.parse_id(actor_id)
    with connection() as conn:
        creator_broadcast._require_broadcast_grant(conn, actor)
        rows = conn.execute(
            """SELECT b.*,p.sender_name,p.purpose FROM creator_broadcast_batches b
            JOIN creator_broadcast_previews p ON p.id=b.preview_id
            WHERE b.sender_authorization_id=%s ORDER BY b.created_at DESC,b.id DESC LIMIT 30""",
            (actor,),
        ).fetchall()
        targets = {row["id"]: [] for row in rows}
        if rows:
            for item in conn.execute("""SELECT batch_id,
                CASE WHEN status='cancelled' AND snapshot ? 'duplicate_of' THEN 'duplicate' ELSE status END AS status,
                COUNT(*) AS n,BOOL_OR(retryable) AS retryable
                FROM creator_broadcast_targets WHERE batch_id=ANY(%s::uuid[]) GROUP BY 1,2""",
                (list(targets),)).fetchall():
                targets[item["batch_id"]].append(item)
        return {"batches": [_summary(conn, row, targets[row["id"]]) for row in rows]}


def batch(actor_id, batch_id):
    actor, batch_id = map(creator_access.parse_id, (actor_id, batch_id))
    with connection() as conn:
        row = _owned_batch(conn, actor, batch_id)
        targets = conn.execute(
            """SELECT id,chat_id,chat_name,
            CASE WHEN status='cancelled' AND snapshot ? 'duplicate_of' THEN 'duplicate' ELSE status END AS status,
            error_message,message_id,retryable
            FROM creator_broadcast_targets WHERE batch_id=%s ORDER BY created_at,id""",
            (batch_id,),
        ).fetchall()
        preview = conn.execute(
            "SELECT body_ciphertext,snapshot FROM creator_broadcast_previews WHERE id=%s", (row["preview_id"],)
        ).fetchone()
        return {
            "batch": _summary(conn, row, targets),
            "targets": [{**target, "id": str(target["id"])} for target in targets],
            "message": _body(preview["body_ciphertext"], preview["snapshot"], get_settings()),
        }


def cancel(actor_id, batch_id):
    actor, batch_id = map(creator_access.parse_id, (actor_id, batch_id))
    with connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR UPDATE", (actor,))
        row = _owned_batch(conn, actor, batch_id)
        conn.execute(
            "UPDATE creator_broadcast_batches SET cancel_requested_at=COALESCE(cancel_requested_at,NOW()) WHERE id=%s",
            (batch_id,),
        )
        stopped = conn.execute(
            """UPDATE creator_broadcast_targets SET status='cancelled',lease_token=NULL,
            lease_until=NULL,updated_at=NOW(),retryable=FALSE,error_message=NULL
            WHERE batch_id=%s AND (status='pending' OR (status='sending' AND send_started_at IS NULL))""",
            (batch_id,),
        ).rowcount
        _audit(conn, actor, "cancel", {"batch_id": str(batch_id), "stopped": stopped})
        return {"batch": _summary(conn, row)}


def retry(actor_id, batch_id):
    actor, batch_id = map(creator_access.parse_id, (actor_id, batch_id))
    with connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR UPDATE", (actor,))
        _authorization(conn, actor)
        row = _owned_batch(conn, actor, batch_id)
        targets = conn.execute("""SELECT id,chat_id,snapshot FROM creator_broadcast_targets
            WHERE batch_id=%s AND status='failed' AND retryable""", (batch_id,)).fetchall()
        if not targets:
            raise ValueError("没有可重试的明确失败项；成功和结果未知的消息不会重发。")
        settings = get_settings()
        fingerprints = {target["chat_id"]: target["snapshot"].get("content_fingerprint") for target in targets}
        if not all(fingerprints.values()):
            preview = _owned_preview(conn, actor, row["preview_id"])
            body, hashes = _fingerprint_material(conn, actor, preview["body_ciphertext"], preview["snapshot"], settings)
            fingerprints = {target["chat_id"]: fingerprints[target["chat_id"]]
                            or _target_fingerprint(body, target["snapshot"], hashes, settings) for target in targets}
        duplicates = _recent_duplicates(conn, actor, fingerprints, settings, exclude_batch=batch_id)
        changed = conn.execute(
            """UPDATE creator_broadcast_targets SET status='pending',retryable=FALSE,
            next_attempt_at=NOW(),send_started_at=NULL,lease_token=NULL,lease_until=NULL,error_message=NULL,
            http_status=NULL,lark_code=NULL,updated_at=NOW()
            WHERE batch_id=%s AND status='failed' AND retryable""",
            (batch_id,),
        ).rowcount
        for target in targets:
            duplicate = duplicates.get(target["chat_id"])
            if duplicate:
                conn.execute("""UPDATE creator_broadcast_targets SET status='cancelled',
                    snapshot=snapshot||%s,error_message='近期已有相同消息，未重复发送'
                    WHERE id=%s AND status='pending'""",
                    (Jsonb({"duplicate_of": str(duplicate["id"])}), target["id"]))
        conn.execute("UPDATE creator_broadcast_batches SET cancel_requested_at=NULL WHERE id=%s", (batch_id,))
        _audit(conn, actor, "retry", {"batch_id": str(batch_id), "targets": changed - len(duplicates),
                                    "duplicates": len(duplicates)})
        return {"batch": _summary(conn, row)}


def recover():
    with connection() as conn, conn.transaction():
        return conn.execute(
            """UPDATE creator_broadcast_targets t SET
            status=CASE WHEN send_started_at IS NOT NULL THEN 'unknown'
                        WHEN b.cancel_requested_at IS NOT NULL THEN 'cancelled' ELSE 'pending' END,
            error_message=CASE WHEN send_started_at IS NOT NULL THEN %s ELSE NULL END,
            retryable=FALSE,lease_token=NULL,lease_until=NULL,updated_at=NOW()
            FROM creator_broadcast_batches b
            WHERE b.id=t.batch_id AND t.status='sending' AND t.lease_until<NOW()""",
            (UNKNOWN_MESSAGE,),
        ).rowcount


def claim_next():
    with connection() as conn, conn.transaction():
        row = conn.execute("""SELECT t.id,b.sender_authorization_id FROM creator_broadcast_targets t
            JOIN creator_broadcast_batches b ON b.id=t.batch_id
            JOIN authorizations a ON a.id=b.sender_authorization_id
            WHERE t.status='pending' AND t.next_attempt_at<=NOW() AND b.cancel_requested_at IS NULL
              AND NOT EXISTS(SELECT 1 FROM creator_broadcast_targets running
                JOIN creator_broadcast_batches rb ON rb.id=running.batch_id
                WHERE rb.sender_authorization_id=a.id AND running.status='sending')
            ORDER BY b.created_at,t.created_at,t.id FOR UPDATE OF a,t SKIP LOCKED LIMIT 1""").fetchone()
        if not row:
            return None
        # Re-read after the actor lock: the first statement may have an older snapshot.
        if conn.execute(
            """SELECT 1 FROM creator_broadcast_targets t
            JOIN creator_broadcast_batches b ON b.id=t.batch_id
            WHERE b.sender_authorization_id=%s AND t.status='sending' LIMIT 1""",
            (row["sender_authorization_id"],),
        ).fetchone():
            return None
        token = uuid4()
        conn.execute(
            """UPDATE creator_broadcast_targets SET status='sending',lease_token=%s,
            lease_until=NOW()+INTERVAL '120 seconds',attempts=attempts+1,updated_at=NOW() WHERE id=%s""",
            (token, row["id"]),
        )
        return conn.execute(
            """SELECT t.*,b.sender_authorization_id,p.sender_open_id,p.purpose,p.body_ciphertext
            FROM creator_broadcast_targets t JOIN creator_broadcast_batches b ON b.id=t.batch_id
            JOIN creator_broadcast_previews p ON p.id=b.preview_id WHERE t.id=%s""",
            (row["id"],),
        ).fetchone()


def _mark_started(job, checked):
    actor = job["sender_authorization_id"]
    with connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR UPDATE", (actor,))
        _unchanged(conn, actor, checked)
        _routes_current(conn, actor, job["purpose"], [job["snapshot"]])
        _images(conn, actor, _body(job["body_ciphertext"], job["snapshot"], get_settings()), get_settings())
        return bool(
            conn.execute(
                """UPDATE creator_broadcast_targets t SET send_started_at=NOW(),
            lease_until=NOW()+INTERVAL '120 seconds',updated_at=NOW()
            FROM creator_broadcast_batches b WHERE b.id=t.batch_id AND t.id=%s AND t.lease_token=%s
              AND t.status='sending' AND t.lease_until>NOW() AND b.cancel_requested_at IS NULL RETURNING t.id""",
                (job["id"], job["lease_token"]),
            ).fetchone()
        )


def _finish(job, status, error=None, *, retryable=False, message_id=None, api_error=None, delay=None):
    with connection() as conn, conn.transaction():
        conn.execute(
            """UPDATE creator_broadcast_targets t SET
            status=CASE WHEN %s='pending' AND b.cancel_requested_at IS NOT NULL THEN 'cancelled' ELSE %s END,
            error_message=CASE WHEN %s='pending' AND b.cancel_requested_at IS NOT NULL THEN NULL ELSE %s END,
            retryable=%s,
            message_id=%s,http_status=%s,lark_code=%s,lease_token=NULL,lease_until=NULL,
            next_attempt_at=NOW()+(%s * INTERVAL '1 second'),updated_at=NOW(),
            send_started_at=CASE WHEN %s='pending' THEN NULL ELSE t.send_started_at END
            FROM creator_broadcast_batches b
            WHERE b.id=t.batch_id AND t.id=%s AND t.lease_token=%s AND t.status='sending'""",
            (
                status,
                status,
                status,
                error,
                retryable,
                message_id,
                getattr(api_error, "status_code", None),
                getattr(api_error, "lark_code", None),
                delay or 0,
                status,
                job["id"],
                job["lease_token"],
            ),
        )


def run_claimed(job, settings=None):
    settings = settings or get_settings()
    actor = job["sender_authorization_id"]
    send_attempted = False
    try:
        with connection() as conn:
            _routes_current(conn, actor, job["purpose"], [job["snapshot"]])
        with httpx.Client(timeout=8) as http:
            client = LarkClient(settings, http_client=http)
            authorization, access_token = _client_token(actor, client, settings)
            if authorization["open_id"] != job["sender_open_id"]:
                raise PermissionError("发送账号已变化，请重新准备。")
            _live_group(client, access_token, authorization, job["snapshot"])
            body = _body(job["body_ciphertext"], job["snapshot"], settings)
            with connection() as conn:
                image_keys = {key: image["image_key"] for key, image in _images(conn, actor, body, settings).items()}
            build_chat_post_payload(
                job["chat_id"],
                body,
                mentions=job["snapshot"].get("mentions", []),
                mention_all=bool(job["snapshot"].get("mention_all")),
                message_uuid=str(job["message_uuid"]),
                image_keys=image_keys,
            )
            if not _mark_started(job, authorization):
                return
            send_attempted = True
            try:
                message_id = client.send_chat_post(
                    access_token,
                    job["chat_id"],
                    body,
                    mentions=job["snapshot"].get("mentions", []),
                    mention_all=bool(job["snapshot"].get("mention_all")),
                    message_uuid=str(job["message_uuid"]),
                    **({"image_keys": image_keys} if image_keys else {}),
                )
            except LarkApiError as exc:
                if getattr(exc, "delivery_unknown", False):
                    _finish(job, "unknown", UNKNOWN_MESSAGE, api_error=exc)
                elif exc.rate_limited and job["attempts"] < 3:
                    _finish(
                        job,
                        "pending",
                        "Lark 限流，稍后自动重试",
                        api_error=exc,
                        delay=max(5, exc.retry_after or job["attempts"] * 5),
                    )
                else:
                    message = (
                        "Lark 未接受消息，请检查群权限或重新授权"
                        if exc.status_code in (401, 403)
                        else "Lark 未接受消息，可重试"
                    )
                    _finish(job, "failed", message, retryable=True, api_error=exc)
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                _finish(job, "failed", "未连接到 Lark，可重试", retryable=True)
                return
            except ValueError:
                _finish(job, "failed", "消息格式或长度不受 Lark 支持，请修改后重新准备")
                return
            except httpx.HTTPError:
                _finish(job, "unknown", UNKNOWN_MESSAGE)
                return
            except Exception:
                # Once the POST starts, even an unexpected local error cannot prove non-delivery.
                _finish(job, "unknown", UNKNOWN_MESSAGE)
                return
            if not isinstance(message_id, str) or not message_id:
                _finish(job, "unknown", UNKNOWN_MESSAGE)
            else:
                _finish(job, "succeeded", message_id=message_id)
    except creator_broadcast.BroadcastAuthorizationRequired:
        _finish(job, "failed", "缺少本人发送授权，请重新授权后重试", retryable=True)
    except PermissionError as exc:
        _finish(job, "failed", str(exc)[:300], retryable=True)
    except ValueError:
        _finish(job, "failed", "消息格式或长度不受 Lark 支持，请修改后重新准备")
    except (LarkApiError, httpx.HTTPError) as exc:
        _finish(
            job,
            "failed",
            _issue(job["snapshot"], exc)["reason"] + "；消息未发送，可重试",
            retryable=True,
            api_error=exc if isinstance(exc, LarkApiError) else None,
        )
    except Exception:
        if send_attempted:
            _finish(job, "unknown", UNKNOWN_MESSAGE)
        else:
            _finish(job, "failed", "发送准备失败，消息未发送，可重试", retryable=True)
