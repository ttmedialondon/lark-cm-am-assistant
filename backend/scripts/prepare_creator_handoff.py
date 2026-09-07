"""Publish a validated handoff snapshot, not creator/handle/owner decisions.

Dry-run by default. Use --apply only after the deployment runbook backup gate.
Input data stays outside the repository. Only append-only audit events are written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import creator_preparation, creator_review  # noqa: E402
from app.db import connection  # noqa: E402


def publish(conn, payload, owner_id, *, apply=False):
    payload = creator_preparation.parse_payload(payload)
    # This small, manually prepared roster is published while services are stopped.
    conn.execute("LOCK TABLE contact_roles, contact_assignments, authorizations, admin_roles, "
                 "ai_feature_configs, audit_events IN SHARE ROW EXCLUSIVE MODE")
    actor = conn.execute("""
        SELECT a.id FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id
        WHERE a.id=%s AND a.status='active' AND r.role='owner' AND r.is_active
    """, (owner_id,)).fetchone()
    if not actor:
        raise ValueError("The publishing actor must be the active owner")
    flags = conn.execute("""
        SELECT feature, enabled FROM ai_feature_configs
        WHERE feature IN ('contact_assessment', 'contact_assignment')
    """).fetchall()
    if len(flags) != 2 or any(row["enabled"] for row in flags):
        raise ValueError("Creator AI must remain disabled")
    jobs = conn.execute("""
        SELECT (SELECT COUNT(*) FROM contact_assessments WHERE status IN ('queued','running'))
          + (SELECT COUNT(*) FROM contact_assignment_assessments WHERE status IN ('queued','running')) AS count
    """).fetchone()
    if jobs["count"]:
        raise ValueError("Creator AI work must remain stopped")
    rows = conn.execute(creator_review._CREATORS_SQL).fetchall()
    current = {str(row["id"]): row for row in rows}
    mappings = {}
    for row in rows:
        for handle in creator_review._evidence(row)["handles"]:
            if handle in mappings:
                raise ValueError("A current handle maps to multiple contacts")
            mappings[handle] = str(row["id"])
    listed_existing = set()
    for item in payload["items"]:
        contact_id = item["contact_id"] or item["suggested_contact_id"]
        if contact_id and not creator_preparation.binding_matches(
                item, current.get(contact_id), payload["responsible_id"]):
            raise ValueError("Formal state changed since preparation")
        if item["contact_id"]:
            listed_existing.add(item["handle"])
            if mappings.get(item["handle"]) != item["contact_id"]:
                raise ValueError("Existing handle mapping does not match the current registry")
            if item["category"] == "documented" and not creator_review._evidence(
                    current[item["contact_id"]])["documented"]:
                raise ValueError("Snapshot cannot promote undocumented evidence")
        elif item["handle"] in mappings:
            raise ValueError("An unresolved handle is already recorded in the registry")
    owner_handles = {handle for handle, cid in mappings.items()
                     if str(current[cid]["responsible_id"]) == payload["responsible_id"]}
    if listed_existing != owner_handles:
        raise ValueError("Preparation does not cover this owner's current handle registry exactly")
    digest = creator_preparation.payload_digest(payload)
    previous = creator_preparation.load_snapshot(conn)
    counts = dict(Counter(item["category"] for item in payload["items"]))
    if previous and creator_preparation.payload_digest(previous) == digest:
        return {"state": "already_prepared", "items": len(payload["items"]), "categories": counts}
    if apply:
        batch_id = uuid4()
        conn.execute("INSERT INTO audit_events (id,authorization_id,action,metadata) VALUES (%s,%s,%s,%s::jsonb)",
                     (batch_id, owner_id, creator_preparation.ACTION,
                      json.dumps({"payload": payload, "payload_sha256": digest}, ensure_ascii=False)))
        for item in payload["items"]:
            if item["issue"] != "identity_risk":
                continue
            note = {key: item[key] for key in ("handle", "contact_id", "source_rows", "reason",
                                              "existing_role_source", "expected_assignment_id",
                                              "expected_role_updated_at")}
            note.update(batch_id=str(batch_id), responsible_id=payload["responsible_id"])
            conn.execute("INSERT INTO audit_events (id,authorization_id,action,metadata) VALUES (%s,%s,%s,%s::jsonb)",
                         (uuid4(), owner_id, creator_preparation.RISK_ACTION, json.dumps(note, ensure_ascii=False)))
    return {"state": "prepared" if apply else "dry_run", "items": len(payload["items"]), "categories": counts,
            "payload_sha256": digest, "formal_changes": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--sha256", required=True, help="Pinned SHA-256 of the input JSON file")
    parser.add_argument("--owner", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError("Input size or SHA-256 mismatch")
    with connection() as conn:
        result = publish(conn, json.loads(raw), args.owner, apply=args.apply)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
