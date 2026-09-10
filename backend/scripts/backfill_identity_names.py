"""Normalize every stored human label; refresh authorized users' bilingual profiles.

Dry-run by default. Run --apply --owner-id UUID after the deployment backup gate.
Only existing identities are touched; no chats, roles, ownership or permissions
are inferred. IM member/message APIs do not provide a separate English name.
Do not invent one for external users without an authorized profile response.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.errors import DeadlockDetected, SerializationFailure

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.crypto import TokenCipher  # noqa: E402
from app.db import connection  # noqa: E402
from app.identity_names import format_display_name, update_identity_names  # noqa: E402
from app.lark_api import LarkApiError, LarkClient  # noqa: E402
from app.lark_sync_admission import LarkSyncAdmission  # noqa: E402
from app.worker import (  # noqa: E402
    ReauthorizationRequired,
    _authorization,
    _read_member_snapshot,
    _usable_access_token,
)

MEMBER_TARGETS = """SELECT DISTINCT ON (ac.chat_id) ac.authorization_id,ac.chat_id,ac.sync_generation
    FROM authorization_chats ac JOIN authorizations a ON a.id=ac.authorization_id
    JOIN chats c USING(chat_id)
    WHERE a.status='active' AND ac.is_visible AND ac.sync_enabled
      AND 'im:chat.members:read'=ANY(a.scopes)
      AND COALESCE(c.chat_mode,'group')<>'p2p' AND COALESCE(c.chat_status,'normal')='normal'
    ORDER BY ac.chat_id,(ac.member_snapshot_status='succeeded') DESC,
             ac.member_snapshot_checked_at DESC NULLS LAST,ac.authorization_id"""

MEMBER_ACCESS = """SELECT a.access_token_ciphertext FROM authorizations a
    JOIN authorization_chats ac ON ac.authorization_id=a.id JOIN chats c USING(chat_id)
    WHERE a.id=%s AND ac.chat_id=%s AND ac.sync_generation=%s AND a.status='active'
      AND ac.is_visible AND ac.sync_enabled AND 'im:chat.members:read'=ANY(a.scopes)
      AND COALESCE(c.chat_mode,'group')<>'p2p' AND COALESCE(c.chat_status,'normal')='normal'"""


def refresh_members(target, client, settings):
    """Read current roster names only; never create users or change membership/roles."""
    authorization = _authorization(target["authorization_id"])
    if not authorization or authorization["status"] != "active":
        return {"status": "authorization_changed", "users": []}
    params = (target["authorization_id"], target["chat_id"], target["sync_generation"])
    with connection() as conn:
        if not conn.execute(MEMBER_ACCESS, params).fetchone():
            return {"status": "authorization_changed", "users": []}
    token = _usable_access_token(target["authorization_id"], authorization, client, settings)
    measurement = _read_member_snapshot(client, token, target["chat_id"])
    if measurement.snapshot_status not in {"succeeded", "partial"}:
        return {"status": measurement.snapshot_status, "users": []}
    with connection() as conn, conn.transaction():
        current = conn.execute(MEMBER_ACCESS + " FOR SHARE OF a,ac,c", params).fetchone()
        if (
            not current
            or TokenCipher(settings.token_encryption_key).decrypt(current["access_token_ciphertext"]) != token
        ):
            return {"status": "authorization_changed", "users": []}
        members = {m["open_id"]: m for m in measurement.members}
        known = conn.execute(
            """SELECT i.open_id,EXISTS(SELECT 1 FROM authorizations a WHERE a.open_id=i.open_id) AS authorized
               FROM lark_identities i WHERE i.identity_type='user' AND i.open_id=ANY(%s::text[])
               ORDER BY i.open_id""",
            (list(members),),
        ).fetchall()
        for identity in known:
            member = members[identity["open_id"]]
            update_identity_names(
                conn,
                identity["open_id"],
                name=member.get("name"),
                en_name=member.get("en_name"),
                en_name_present=bool(member.get("en_name_present")),
                authoritative=not identity["authorized"],
            )
    return {"status": measurement.snapshot_status, "users": [i["open_id"] for i in known]}


def refresh_members_safely(target, client, settings):
    # Normal sync may acquire the same rows in a different order. Each retry
    # starts fresh and rechecks access; one busy chat must not abort the batch.
    for _ in range(3):
        try:
            return refresh_members(target, client, settings)
        except (DeadlockDetected, SerializationFailure):
            continue
        except LarkApiError as exc:
            return {"status": f"unavailable_http_{exc.status_code}_code_{exc.lark_code}", "users": []}
        except ReauthorizationRequired:
            return {"status": "reauthorization_required", "users": []}
    return {"status": "database_busy", "users": []}


def _owner(conn, owner_id):
    if not conn.execute(
        """SELECT a.id FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id
           WHERE a.id=%s AND a.status='active' AND r.role='owner' AND r.is_active""",
        (owner_id,),
    ).fetchone():
        raise ValueError("An active system owner is required")


def refresh_profile(authorization_id, client, settings):
    authorization = _authorization(authorization_id)
    if not authorization or authorization["status"] != "active":
        return "inactive"
    token = _usable_access_token(authorization_id, authorization, client, settings)
    profile = client.user_info(token)
    if profile.open_id != authorization["open_id"]:
        return "identity_mismatch"
    with connection() as conn, conn.transaction():
        current = conn.execute(
            """SELECT open_id, status, access_token_ciphertext FROM authorizations
               WHERE id=%s FOR UPDATE""",
            (authorization_id,),
        ).fetchone()
        if (
            not current
            or current["status"] != "active"
            or current["open_id"] != profile.open_id
            or TokenCipher(settings.token_encryption_key).decrypt(current["access_token_ciphertext"]) != token
        ):
            return "authorization_changed"
        if not conn.execute("SELECT 1 FROM lark_identities WHERE open_id=%s", (profile.open_id,)).fetchone():
            return "identity_missing"
        label = update_identity_names(
            conn,
            profile.open_id,
            name=profile.name,
            en_name=profile.en_name,
            en_name_present=profile.en_name_present,
            authoritative=True,
        )
        conn.execute(
            """UPDATE authorizations SET display_name=%s,updated_at=NOW()
               WHERE id=%s AND display_name IS DISTINCT FROM %s""",
            (label, authorization_id, label),
        )
    return "refreshed"


def run(*, apply=False, owner_id=None, refresh_member_names=False):
    with connection() as conn:
        if apply:
            _owner(conn, owner_id)
        identities = conn.execute(
            "SELECT open_id, name, en_name, display_name FROM lark_identities "
            "WHERE identity_type='user' ORDER BY open_id"
        ).fetchall()
        authorizations = conn.execute("SELECT id FROM authorizations WHERE status='active' ORDER BY id").fetchall()
        targets = conn.execute(MEMBER_TARGETS).fetchall() if refresh_member_names else []
    stats = {
        "mode": "apply" if apply else "dry_run",
        "users_scanned": len(identities),
        "active_authorizations": len(authorizations),
        "profile_results": {},
        "labels_changed": 0,
        "member_chats": len(targets),
        "member_results": {},
        "members_observed": 0,
    }
    if apply:
        settings = get_settings()
        client = LarkClient(
            settings,
            admission=LarkSyncAdmission(
                max_inflight=settings.lark_sync_max_inflight,
                requests_per_second=settings.lark_sync_requests_per_second,
            ),
        )
        for authorization in authorizations:
            try:
                outcome = refresh_profile(authorization["id"], client, settings)
            except LarkApiError as exc:
                outcome = f"unavailable_http_{exc.status_code}_code_{exc.lark_code}"
            except ReauthorizationRequired:
                outcome = "reauthorization_required"
            stats["profile_results"][outcome] = stats["profile_results"].get(outcome, 0) + 1
        if targets:
            observed = set()

            def read(target):
                return refresh_members_safely(target, client, settings)

            # The existing cross-process gate bounds all API traffic; keep one
            # slot available to the normal message sync when its limit is three.
            with ThreadPoolExecutor(max_workers=min(2, settings.lark_sync_max_inflight)) as executor:
                for number, result in enumerate(executor.map(read, targets), 1):
                    outcome = result["status"]
                    stats["member_results"][outcome] = stats["member_results"].get(outcome, 0) + 1
                    observed.update(result["users"])
                    if number % 100 == 0:
                        print(
                            json.dumps({"member_chats_checked": number, "member_chats_total": len(targets)}), flush=True
                        )
            stats["members_observed"] = len(observed)
        # One short transaction per person avoids holding the entire directory
        # while normal IM synchronization updates individual identities.
        for identity in identities:
            with connection() as conn, conn.transaction():
                label = update_identity_names(conn, identity["open_id"])
            stats["labels_changed"] += int(label != identity["display_name"])
        with connection() as conn, conn.transaction():
            _owner(conn, owner_id)
            stats.update(
                conn.execute(
                    """SELECT count(*) FILTER (WHERE NULLIF(btrim(name),'') IS NULL
                          AND NULLIF(btrim(en_name),'') IS NULL) AS users_without_name,
                       count(*) FILTER (WHERE NULLIF(btrim(en_name),'') IS NULL) AS users_without_english_name,
                       count(*) FILTER (WHERE NULLIF(btrim(en_name),'') IS NOT NULL) AS users_with_english_name
                   FROM lark_identities WHERE identity_type='user'"""
                ).fetchone()
            )
            conn.execute(
                "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
                (uuid4(), owner_id, "identity_names.backfilled", json.dumps(stats)),
            )
    else:
        stats["labels_changed"] = sum(
            format_display_name(row["name"], row["en_name"]) != row["display_name"] for row in identities
        )
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--owner-id", type=UUID)
    parser.add_argument("--refresh-members", action="store_true", help="Refresh current permitted group-member names")
    args = parser.parse_args()
    if args.apply and not args.owner_id:
        parser.error("--apply requires --owner-id")
    print(
        json.dumps(
            run(apply=args.apply, owner_id=args.owner_id, refresh_member_names=args.refresh_members), ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
