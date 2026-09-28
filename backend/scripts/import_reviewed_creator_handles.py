"""Import owner-reviewed research links, restricted to a verified Curva archive.

Dry-run by default. Never assigns a CM, changes historical classification fields or calls a model.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import creator_access, creator_imports
from app.db import connection

SOURCE = 'owner_reviewed_curva'


def catalog_handles(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    handles = set()
    for entry in manifest['files']:
        path = directory / entry['name']
        if path.parent != directory or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Curva archive verification failed')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            handles.update(row['handle'].strip().lstrip('@').lower() for row in csv.DictReader(stream))
    return handles


def admit(conn, actor, rows, catalog):
    creator_access.require_owner(conn, actor)
    if not rows:
        raise ValueError('Empty import')
    seen = set()
    for row in rows:
        handle = row['handle']
        UUID(row['contact_id'])
        if not re.fullmatch(r'[a-z0-9_.]{1,160}', handle) or handle not in catalog:
            raise ValueError('Handle is not in the Curva catalog: ' + handle)
        if handle in seen or not row.get('message_ids'):
            raise ValueError('Duplicate handle or missing reviewed evidence')
        seen.add(handle)
    added = 0
    with conn.transaction():
        for row in sorted(rows, key=lambda r: r['handle']):
            contact, handle = UUID(row['contact_id']), row['handle']
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))', ('tiktok:' + handle,))
            if not conn.execute('SELECT id FROM contacts WHERE id=%s FOR UPDATE', (contact,)).fetchone():
                raise ValueError('Contact not found')
            if creator_imports._identity_conflict(conn, contact):
                raise ValueError('Identity conflict: ' + handle)
            account = conn.execute("""INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
                VALUES(%s,'tiktok',%s,%s) ON CONFLICT(platform,normalized_handle)
                DO UPDATE SET normalized_handle=EXCLUDED.normalized_handle RETURNING id""",
                (uuid4(), handle, handle)).fetchone()['id']
            if conn.execute("""SELECT 1 FROM contact_social_accounts WHERE social_account_id=%s
                AND (status='confirmed' AND contact_id<>%s AND relationship='owner'
                  OR contact_id=%s AND (status='rejected' OR relationship<>'owner'))
                UNION ALL SELECT 1 FROM creator_account_assignments WHERE social_account_id=%s
                  AND ended_at IS NULL AND contact_id<>%s LIMIT 1""",
                (account, contact, contact, account, contact)).fetchone():
                raise ValueError('Account relationship conflict: ' + handle)
            inserted = conn.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,
                relationship,status,source,reviewed_by_authorization_id,reviewed_at)
                VALUES(%s,%s,'owner','confirmed',%s,%s,NOW())
                ON CONFLICT DO NOTHING RETURNING contact_id""", (contact, account, SOURCE, actor)).fetchone()
            role = conn.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' "
                                "AND status='confirmed' AND is_primary", (contact,)).fetchone()
            if not role:
                conn.execute('UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=%s AND is_primary', (contact,))
                conn.execute("""INSERT INTO contact_roles(contact_id,role,status,source,is_primary,
                    reviewed_by_authorization_id,reviewed_at) VALUES(%s,'creator','confirmed',%s,TRUE,%s,NOW())
                    ON CONFLICT(contact_id,role) DO UPDATE SET status='confirmed',source=EXCLUDED.source,
                    is_primary=TRUE,reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,
                    reviewed_at=NOW(),updated_at=NOW()""", (contact, SOURCE, actor))
            if inserted or not role:
                creator_imports._audit(conn, actor, 'reviewed_curva_admitted', row)
            added += bool(inserted)
    return {'people': len({r['contact_id'] for r in rows}), 'relationships': len(rows), 'new_links': added}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--actor', type=UUID, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    rows = json.loads(args.input.read_text())
    catalog = catalog_handles(args.catalog)
    with connection() as conn, conn.transaction(force_rollback=not args.apply):
        result = admit(conn, args.actor, rows, catalog)
    print(json.dumps(dict(result, applied=args.apply)))


if __name__ == '__main__':
    main()
