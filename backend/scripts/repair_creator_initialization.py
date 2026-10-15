"""Repair an explicit initialization manifest; dry-run unless --apply.

Stop the worker and verify a backup before applying. Never reset history, retry
unknown requests, or write confirmed identities, handles or responsibilities.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def repair(conn, manifest, settings, *, apply=False):
    from app import creator_data
    from app import creator_handle_matching as handles
    from app import creator_importance as importance
    from app import creator_responsibility_matching as responsibility
    from app.evidence_access import _READABLE, BODY_READ_SCOPE_SQL

    cm = UUID(manifest['cm_id'])
    groups = {name: sorted({UUID(value) for value in manifest[name]}) for name in
              ('identity_contact_ids', 'importance_job_ids', 'responsibility_job_ids')}
    actor = conn.execute("""SELECT a.id,i.contact_id FROM authorizations a
        LEFT JOIN lark_identities i ON i.open_id=a.open_id AND i.identity_type='user'
        WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL AND """
        + BODY_READ_SCOPE_SQL, (cm,)).fetchone()
    if not actor:
        raise ValueError('The selected CM does not have a current reading grant.')
    counts = Counter()
    contacts = groups['identity_contact_ids']
    processed = {row['contact_id'] for row in conn.execute("""SELECT DISTINCT contact_id
        FROM creator_handle_jobs WHERE contact_id=ANY(%s::uuid[]) AND purpose='identity'""", (contacts,))}
    eligible = {row['id'] for row in conn.execute("""SELECT id FROM contacts
        WHERE id=ANY(%s::uuid[]) AND creator_identity_eligible(id)""", (contacts,))}
    readable = {row['contact_id'] for row in conn.execute("""SELECT DISTINCT i.contact_id
        FROM lark_identities i JOIN (SELECT m.sender_open_id """ + _READABLE + """
          AND m.sender_type='user' AND m.message_type IN ('text','post')) material
        ON material.sender_open_id=i.open_id WHERE i.identity_type='user'
          AND i.contact_id=ANY(%s::uuid[])""", (cm, contacts))} if contacts else set()
    config = handles._config(conn)
    for contact in groups['identity_contact_ids']:
        if contact in processed:
            counts['identity_already_processed'] += 1
            continue
        if contact not in eligible or contact not in readable:
            counts['identity_no_longer_eligible'] += 1
            continue
        if not handles._enabled(config):
            raise ValueError('Automatic creator recognition is not enabled.')
        if apply:
            job = conn.execute('SELECT enqueue_creator_identity(%s,NOW(),TRUE) id', (contact,)).fetchone()['id']
            if not job:
                raise ValueError('Selected contact could not enter recognition.')
        counts['identity_enrolled'] += 1

    for feature, module, table, key in (
        ('importance', importance, 'creator_importance_jobs', 'importance_job_ids'),
        ('responsibility', responsibility, 'creator_responsibility_jobs', 'responsibility_job_ids'),
    ):
        for job_id in groups[key]:
            job = conn.execute(f'SELECT * FROM {table} WHERE id=%s', (job_id,)).fetchone()
            if not job or job['status'] != 'failed' or job['reason_code'] != 'invalid_output':
                counts[feature + '_unchanged'] += 1
                continue
            if feature == 'importance' and job['responsible_authorization_id'] != cm:
                raise ValueError('Importance job belongs to another CM.')
            storage = importance._cipher(settings) if feature == 'importance' else creator_data.cipher(settings)
            material = json.loads(storage.decrypt(job['pack_ciphertext'])) if job['pack_ciphertext'] else None
            if feature == 'responsibility' and (not actor['contact_id'] or not material or
                    str(actor['contact_id']) not in {str(v) for v in material['candidate_ids'].values()}):
                raise ValueError('Responsibility job does not include the selected CM.')
            config = module._config(conn)
            if not module._enabled(config):
                raise ValueError('Selected AI feature is not enabled.')
            if job['attempts'] >= 3:
                counts[feature + '_retry_limit'] += 1
                continue
            valid = bool(material and job['snapshot_version'] == job['desired_version']
                         and job['config_fingerprint'] == module._config_hash(config))
            if valid:
                valid = (all(module._source_checks(conn, job['refs'], job['contact_id'], cm).values())
                         and conn.execute('SELECT creator_importance_is_eligible(%s,%s) ok',
                                          (job['contact_id'], cm)).fetchone()['ok']) if feature == 'importance' \
                    else module.evidence.still_valid(conn, job['refs'], settings)
            if valid:
                module._manual_access(conn, material if feature == 'importance' else material['pack'])
            status, reason = ('ready', 'prepared') if valid else ('queued', 'snapshot_changed')
            if apply:
                changed = conn.execute(f"""UPDATE {table} SET status=%s,reason_code=%s,claim_token=NULL,
                    lease_until=NULL,finished_at=NULL,not_before=NOW(),updated_at=NOW()
                    WHERE id=%s AND status='failed' AND reason_code='invalid_output'""",
                    (status, reason, job_id)).rowcount
                if changed != 1:
                    raise ValueError('Selected failed job changed during repair.')
            counts[feature + '_' + status] += 1
    if apply:
        conn.execute("""INSERT INTO audit_events(id,authorization_id,action,metadata)
            VALUES(%s,%s,'creator_initialization_repaired',%s::jsonb)""",
            (uuid4(), cm, json.dumps({'manifest_created_at': manifest['created_at'], 'counts': dict(counts),
                                     'identity_contacts': [str(v) for v in groups['identity_contact_ids']],
                                     'importance_jobs': [str(v) for v in groups['importance_job_ids']],
                                     'responsibility_jobs': [str(v) for v in groups['responsibility_job_ids']]})))
    return dict(counts)


def main():
    from app.config import get_settings
    from app.db import connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    with connection() as conn, conn.transaction(force_rollback=not args.apply):
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='30s'")
        result = repair(conn, manifest, get_settings(), apply=args.apply)
    print(json.dumps({'applied': args.apply, 'counts': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
