"""Preview/ingest the existing Remi source snapshot without changing legacy data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import creator_preparation, creator_workflow  # noqa: E402
from app.db import connection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-id", type=UUID, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    roster = creator_preparation.roster(UUID("24d1ef13-3762-4359-ba11-d26c03cf1f7f"))
    if not roster["available"]:
        raise SystemExit("A valid Remi source snapshot is required; nothing changed.")
    counts = {"total": 0, "verified": 0, "pending": 0, "formal": 0, "source_unavailable": 0}
    with connection() as conn, conn.transaction():
        scope = creator_workflow._scope(conn, args.actor_id, UUID(roster["responsible_id"]), lock=True)
        if not scope["is_owner"]:
            raise PermissionError("Only the current owner can ingest this existing source snapshot.")
        for item in roster["items"]:
            counts["total"] += 1
            contact_id = item.get("contact_id") or item.get("suggested_contact_id")
            options = (
                creator_workflow.selectable_users(conn, UUID(roster["responsible_id"]), contact_id=UUID(contact_id))
                if contact_id
                else []
            )
            verified = (
                item["issue"] == "none"
                and item["category"] == "documented"
                and not item["is_stale"]
                and bool(item.get("contact_id"))
                and len(options) == 1
            )
            reason = "identity_risk" if item["issue"] == "identity_risk" else "identity_uncertain"
            reasons = [] if verified else [reason]
            if not options:
                contact_id = None
                counts["source_unavailable"] += 1
            counts["verified" if verified else "pending"] += 1
            if args.commit:
                result = creator_workflow.ingest_source(
                    args.actor_id,
                    source_type="excel",
                    source_key=f"legacy-roster:{roster['source_sha256']}:{item['handle']}",
                    source_data={
                        "filename": roster["source_filename"],
                        "sheet": "CAP Creator-CM",
                        "rows": item["source_rows"],
                        "cm": item["cm_values"],
                        "tap_cm": item["tap_cm_values"],
                        "reason": item["reason"],
                        "legacy_issue": item["issue"],
                        "stable_open_id": options[0]["open_id"] if verified else None,
                    },
                    responsible_id=UUID(roster["responsible_id"]),
                    handle=item["handle"],
                    contact_id=contact_id,
                    relationship="owner" if verified else None,
                    uncertainties=reasons,
                    trusted_identity=verified,
                    conn=conn,
                )
                counts["formal"] += result["status"] == "confirmed"
    print(json.dumps({"committed": args.commit, "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
