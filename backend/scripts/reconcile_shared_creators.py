"""Recheck existing creator imports against verified identities; dry-run unless --apply.

Never infer an identity, send messages, grant chat access, or undo a deferred review.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def reconcile_row(conn, actor, row_id):
    from app import creator_imports as imports

    row, batch = imports._row(conn, actor, row_id)
    if row['review_status'] != 'pending' or batch['status'] == 'preview':
        return 'unchanged'
    if row['status'] in ('pending', 'preparing', 'queued', 'running'):
        return 'in_progress'
    person = imports._existing(conn, row)
    if not person:
        return 'needs_identity_evidence'
    if row['contact_id'] and row['contact_id'] != person:
        return 'different_person'
    if not conn.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                        (person,)).fetchone():
        return 'needs_identity_review'
    # Reuses the same locks, current visibility, identity and account conflict checks as admission.
    imports._admit(conn, row, actor, row['handle'], person, automatic=True)
    conn.execute("UPDATE creator_import_rows SET reason_code='existing' WHERE id=%s", (row_id,))
    imports._audit(conn, actor, 'shared_cm_reconciled', {
        'row_id': str(row_id), 'cm_id': str(row['cm_authorization_id']),
        'contact_id': str(person), 'previous_reason': row['reason_code'],
    })
    imports._settle(conn, batch['id'])
    return 'admitted'


def library_check(conn):
    return conn.execute("""SELECT
      (SELECT count(*) FROM contacts) people,
      (SELECT count(*) FROM social_accounts) accounts,
      (SELECT count(*) FROM contact_assignments WHERE ended_at IS NULL) responsibilities,
      (SELECT count(*) FROM creator_import_rows WHERE review_status='confirmed') confirmed_imports,
      (SELECT count(*) FROM creator_import_rows r WHERE r.review_status='confirmed' AND (
        NOT EXISTS (SELECT 1 FROM contact_assignments a WHERE a.contact_id=r.contact_id
          AND a.responsible_authorization_id=r.cm_authorization_id AND a.ended_at IS NULL)
        AND NOT EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.contact_id=r.contact_id
          AND a.responsible_authorization_id=r.cm_authorization_id AND a.ended_at IS NULL))) missing_current_assignment,
      (SELECT count(*) FROM creator_import_rows r WHERE r.review_status='confirmed' AND
        NOT EXISTS (SELECT 1 FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
          WHERE l.contact_id=r.contact_id AND l.status='confirmed' AND l.relationship='owner'
          AND s.platform='tiktok' AND lower(s.normalized_handle)=r.handle)) missing_identity_link
      """).fetchone()


def main():
    from app import creator_access
    from app.db import connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor', type=UUID, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connection() as conn:
        creator_access.require_owner(conn, args.actor)
        before = library_check(conn)
        rows = conn.execute("""SELECT r.id,r.handle,r.reason_code,a.display_name cm FROM creator_import_rows r
          JOIN creator_import_batches b ON b.id=r.batch_id JOIN authorizations a ON a.id=r.cm_authorization_id
          WHERE b.policy_version='creator-import-v2' AND r.review_status='pending' AND b.status<>'preview'
          ORDER BY r.handle,r.id""").fetchall()
    results = []
    for row in rows:
        try:
            with connection() as conn, conn.transaction(force_rollback=not args.apply):
                creator_access.require_owner(conn, args.actor)
                outcome = reconcile_row(conn, args.actor, row['id'])
        except (ValueError, PermissionError) as exc:
            outcome = 'still_blocked'
            row['reason'] = str(exc)
        results.append({**row, 'result': outcome})
    with connection() as conn:
        after = library_check(conn)
    print(json.dumps({'checked_at': datetime.now(UTC).isoformat(), 'applied': args.apply,
                      'before': before, 'after': after, 'counts': dict(Counter(r['result'] for r in results)),
                      'rows': results}, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
