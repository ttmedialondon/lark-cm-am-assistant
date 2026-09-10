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
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.crypto import TokenCipher  # noqa: E402
from app.db import connection  # noqa: E402
from app.identity_names import format_display_name, update_identity_names  # noqa: E402
from app.lark_api import LarkApiError, LarkClient  # noqa: E402
from app.lark_sync_admission import LarkSyncAdmission  # noqa: E402
from app.worker import ReauthorizationRequired, _authorization, _usable_access_token  # noqa: E402


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
               WHERE id=%s FOR UPDATE""", (authorization_id,),
        ).fetchone()
        if (not current or current["status"] != "active" or current["open_id"] != profile.open_id
                or TokenCipher(settings.token_encryption_key).decrypt(current["access_token_ciphertext"]) != token):
            return "authorization_changed"
        if not conn.execute("SELECT 1 FROM lark_identities WHERE open_id=%s", (profile.open_id,)).fetchone():
            return "identity_missing"
        label = update_identity_names(
            conn, profile.open_id, name=profile.name, en_name=profile.en_name,
            en_name_present=profile.en_name_present, authoritative=True,
        )
        conn.execute(
            """UPDATE authorizations SET display_name=%s,updated_at=NOW()
               WHERE id=%s AND display_name IS DISTINCT FROM %s""",
            (label, authorization_id, label),
        )
    return "refreshed"


def run(*, apply=False, owner_id=None):
    with connection() as conn:
        if apply:
            _owner(conn, owner_id)
        identities = conn.execute(
            "SELECT open_id, name, en_name, display_name FROM lark_identities "
            "WHERE identity_type='user' ORDER BY open_id"
        ).fetchall()
        authorizations = conn.execute("SELECT id FROM authorizations WHERE status='active' ORDER BY id").fetchall()
    stats = {
        "mode": "apply" if apply else "dry_run", "users_scanned": len(identities),
        "active_authorizations": len(authorizations), "profile_results": {},
        "labels_changed": 0,
    }
    if apply:
        settings = get_settings()
        client = LarkClient(settings, admission=LarkSyncAdmission(
            max_inflight=settings.lark_sync_max_inflight,
            requests_per_second=settings.lark_sync_requests_per_second,
        ))
        for authorization in authorizations:
            try:
                outcome = refresh_profile(authorization["id"], client, settings)
            except LarkApiError as exc:
                outcome = f"unavailable_http_{exc.status_code}_code_{exc.lark_code}"
            except ReauthorizationRequired:
                outcome = "reauthorization_required"
            stats["profile_results"][outcome] = stats["profile_results"].get(outcome, 0) + 1
        # One short transaction per person avoids holding the entire directory
        # while normal IM synchronization updates individual identities.
        for identity in identities:
            with connection() as conn, conn.transaction():
                label = update_identity_names(conn, identity["open_id"])
            stats["labels_changed"] += int(label != identity["display_name"])
        with connection() as conn, conn.transaction():
            _owner(conn, owner_id)
            stats.update(conn.execute(
                """SELECT count(*) FILTER (WHERE NULLIF(btrim(name),'') IS NULL
                          AND NULLIF(btrim(en_name),'') IS NULL) AS users_without_name,
                       count(*) FILTER (WHERE NULLIF(btrim(en_name),'') IS NULL) AS users_without_english_name,
                       count(*) FILTER (WHERE NULLIF(btrim(en_name),'') IS NOT NULL) AS users_with_english_name
                   FROM lark_identities WHERE identity_type='user'"""
            ).fetchone())
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
    args = parser.parse_args()
    if args.apply and not args.owner_id:
        parser.error("--apply requires --owner-id")
    print(json.dumps(run(apply=args.apply, owner_id=args.owner_id), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
