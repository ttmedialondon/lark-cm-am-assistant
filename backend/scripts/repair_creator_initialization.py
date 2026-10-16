"""Repair an explicit initialization manifest; dry-run unless --apply.

Stop the worker and verify a backup before applying. Never reset history or
resend unknown requests; saved successful replies still require current validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPAIRABLE_ERRORS = ('invalid_output', 'http_400')


def repair(conn, manifest, settings, *, apply=False):
    from app import creator_data, summary_periods
    from app import creator_handle_matching as handles
    from app import creator_importance as importance
    from app import creator_responsibility_matching as responsibility
    from app.db import lock_sync_material
    from app.evidence_access import _READABLE, BODY_READ_SCOPE_SQL

    lock_sync_material(conn)
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
    retries = {row['id']: row for row in conn.execute("""SELECT * FROM creator_handle_jobs
        WHERE id=ANY(%s::uuid[])""", (manifest.get('identity_retry_job_ids', []),)).fetchall()}
    material_ids = sorted({UUID(value) for value in manifest.get('identity_material_job_ids', [])})
    material_jobs = {row['id']: row for row in conn.execute("""SELECT * FROM creator_handle_jobs
        WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE""", (material_ids,)).fetchall()}
    cutoff = datetime.fromisoformat(manifest['created_at']) if material_ids else None
    if cutoff and cutoff.tzinfo is None:
        raise ValueError('The material repair manifest requires a timezone-aware timestamp.')
    contacts = sorted(set(groups['identity_contact_ids']) | {row['contact_id'] for row in
                      [*retries.values(), *material_jobs.values()]})
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

    for job in retries.values():
        if conn.execute("""SELECT 1 FROM audit_events WHERE action='creator_initialization_repaired'
            AND metadata->'identity_retry_jobs' ? %s LIMIT 1""", (str(job['id']),)).fetchone():
            counts['identity_retry_unchanged'] += 1
            continue
        receipt = conn.execute("""SELECT error_code FROM creator_handle_requests
            WHERE job_id=%s ORDER BY created_at DESC,id DESC LIMIT 1""", (job['id'],)).fetchone()
        if (job['purpose'] != 'identity' or job['status'] != 'failed' or not receipt
                or receipt['error_code'] != 'http_400' or job['reason_code'] == 'provider_rejected'):
            counts['identity_retry_unchanged'] += 1
            continue
        if conn.execute("""SELECT 1 FROM creator_handle_jobs WHERE contact_id=%s AND id<>%s
            AND purpose='identity' AND status NOT IN ('completed','failed','cancelled')""",
            (job['contact_id'], job['id'])).fetchone():
            counts['identity_retry_unchanged'] += 1
            continue
        if conn.execute("SELECT 1 FROM creator_handle_requests WHERE job_id=%s "
                        "AND error_code='request_unknown'", (job['id'],)).fetchone():
            counts['identity_unknown_preserved'] += 1
            continue
        if job['contact_id'] not in eligible or job['contact_id'] not in readable:
            counts['identity_retry_no_longer_eligible'] += 1
            continue
        if not handles._enabled(config) or job['attempts'] >= 3:
            counts['identity_retry_unavailable'] += 1
            continue
        pack = json.loads(creator_data.cipher(settings).decrypt(job['pack_ciphertext'])) \
            if job['pack_ciphertext'] else None
        valid = bool(pack and job['snapshot_version'] == job['desired_version']
                     and job['config_fingerprint'] == handles._job_config_hash(config, 'identity')
                     and handles.evidence.still_valid(conn, job['refs'], settings))
        if valid:
            handles._manual_access(conn, pack)
        status = 'ready' if valid else 'queued'
        if apply:
            conn.execute("""UPDATE creator_handle_jobs SET status=%s,reason_code='targeted_retry',
                source=CASE WHEN %s='queued' THEN 'manual' ELSE source END,
                claim_token=NULL,lease_until=NULL,finished_at=NULL,not_before=NOW(),updated_at=NOW()
                WHERE id=%s AND status='failed'""", (status, status, job['id']))
        counts['identity_retry_' + status] += 1

    replayed_material = []
    for job_id in material_ids:
        job = material_jobs.get(job_id)
        completed = bool(job and job['status'] == 'completed' and job['stats'].get('identity_state') == 'not_supported')
        if (not job or job['purpose'] != 'identity' or job['source'] == 'manual'
                or not (completed or job['status'] in {'review_required', 'waiting_evidence'})
                or not job['prepared_at'] or job['prepared_at'] >= cutoff
                or job['claim_token'] or job['lease_until'] or job['snapshot_version'] != job['desired_version']
                or job['contact_id'] not in eligible or job['contact_id'] not in readable
                or conn.execute("""SELECT 1 FROM creator_handle_jobs WHERE contact_id=%s
                    AND id<>%s AND ((created_at,id)>(%s,%s)
                      OR status NOT IN ('completed','failed','cancelled')) LIMIT 1""",
                    (job['contact_id'], job_id, job['created_at'], job_id)).fetchone()
                or conn.execute("""SELECT 1 FROM audit_events WHERE action='creator_initialization_repaired'
                    AND metadata->>'manifest_created_at'=%s AND metadata->'identity_material_jobs' ? %s LIMIT 1""",
                    (manifest['created_at'], str(job_id))).fetchone()):
            counts['identity_material_unchanged'] += 1
            continue
        if conn.execute("""SELECT 1 FROM creator_handle_requests r JOIN creator_handle_jobs j ON j.id=r.job_id
            WHERE j.contact_id=%s AND (r.error_code='request_unknown' OR r.error_code=''
              AND r.billed_tokens IS NULL AND r.response_ciphertext IS NULL) LIMIT 1""",
            (job['contact_id'],)).fetchone():
            counts['identity_material_unknown_preserved'] += 1
            continue
        if not handles._enabled(config):
            raise ValueError('Automatic creator recognition is not enabled.')
        old = json.loads(creator_data.cipher(settings).decrypt(job['pack_ciphertext'])) \
            if job['pack_ciphertext'] else {}
        catalog, _ = handles.reference_catalog()
        fresh = handles.evidence.build(conn, settings, job['contact_id'], catalog, purpose='identity', recheck=False)
        pack = fresh['pack']
        if not pack or {k: v for k, v in pack.items() if k != '_rules'} == {
                k: v for k, v in old.items() if k != '_rules'}:
            counts['identity_material_unchanged'] += 1
            continue
        own = {m['id'] for m in pack['messages'] if m['speaker'] == pack['target']}
        related = own | {m.get(key) for m in pack['messages'] if m['id'] in own for key in ('reply', 'previous')}
        related |= {m['id'] for m in pack['messages'] if m.get('reply') in own or m.get('previous') in own}
        previous = {(ref['chat_id'], ref['message_id']) for ref in job['refs'] if ref['kind'] == 'message'}
        added = [ref for ref in fresh['refs'] if ref['kind'] == 'message' and ref['label'] in related
                 and (ref['chat_id'], ref['message_id']) not in previous]
        changed = added and conn.execute("SELECT 1 " + _READABLE + """
            AND m.message_type IN ('text','post') AND o.observed_at>%s
            AND (m.chat_id,m.message_id) IN (SELECT * FROM unnest(%s::text[],%s::text[])) LIMIT 1""",
            (cm, job['prepared_at'], [r['chat_id'] for r in added], [r['message_id'] for r in added])).fetchone()
        if not changed:
            counts['identity_material_unchanged'] += 1
            continue
        if apply:
            queued = conn.execute('SELECT enqueue_creator_identity(%s,NOW(),TRUE) id',
                                  (job['contact_id'],)).fetchone()['id']
            if not queued or (queued == job_id if completed else queued != job_id):
                raise ValueError('Selected material job changed during repair.')
        replayed_material.append(str(job_id))
        counts['identity_material_queued'] += 1
        if completed:
            counts['identity_material_completed_queued'] += 1

    for job_id in sorted({UUID(value) for value in manifest.get('responsibility_saved_job_ids', [])}):
        job = conn.execute('SELECT * FROM creator_responsibility_jobs WHERE id=%s', (job_id,)).fetchone()
        envelope = json.loads(creator_data.cipher(settings).decrypt(job['pack_ciphertext'])) \
            if job and job['pack_ciphertext'] else None
        if not actor['contact_id'] or not envelope:
            counts['responsibility_saved_not_reusable'] += 1
            continue
        if str(actor['contact_id']) not in {str(v) for v in envelope['candidate_ids'].values()}:
            raise ValueError('Responsibility job does not include the selected CM.')
        with conn.transaction(force_rollback=not apply):
            restored = responsibility.recover_saved_result(conn, settings, job_id)
        if restored:
            counts['responsibility_saved_restored'] += 1
            continue
        receipt = conn.execute("""SELECT * FROM creator_responsibility_requests WHERE job_id=%s
            ORDER BY created_at DESC,id DESC LIMIT 1""", (job_id,)).fetchone()
        diagnostic = json.loads(creator_data.cipher(settings).decrypt(receipt['response_ciphertext'])) \
            if receipt and receipt['response_ciphertext'] else {}
        if (not job or job['status'] != 'failed' or job['source'] == 'sample'
                or job['reason_code'] not in {'request_unknown', 'application_pending'}
                or not receipt or receipt['error_code'] or not receipt['finished_at']
                or not receipt['request_id'] or not receipt['response_id'] or not diagnostic.get('validated')
                or receipt['input_tokens'] is None or receipt['output_tokens'] is None
                or not envelope or not job['prepared_at'] or receipt['created_at'] < job['prepared_at']
                or receipt['material_fingerprint'] != responsibility._hash(responsibility._payment_material(envelope))
                or not responsibility._enabled(responsibility._config(conn))
                or conn.execute("""SELECT 1 FROM creator_responsibility_jobs WHERE contact_id=%s
                    AND id<>%s AND status NOT IN ('completed','failed','cancelled')""",
                    (job['contact_id'], job_id)).fetchone()):
            counts['responsibility_saved_not_reusable'] += 1
            continue
        current = responsibility._build_material(conn, settings, job['contact_id'])
        current_envelope = {'pack': current['pack'], 'candidate_ids': current['candidate_ids']}
        changed = bool(current['pack'] and responsibility.evidence.still_valid(conn, current['refs'], settings)
                       and responsibility._payment_material(current_envelope)
                           != responsibility._payment_material(envelope))
        if not changed:
            counts['responsibility_saved_not_reusable'] += 1
            continue
        if apply:
            conn.execute("""UPDATE creator_responsibility_jobs SET status='queued',reason_code='application_pending',
                claim_token=NULL,lease_until=NULL,finished_at=NULL,not_before=NOW(),updated_at=NOW()
                WHERE id=%s AND status='failed'""", (job_id,))
        counts['responsibility_new_material_queued'] += 1

    restored_profiles = []
    for contact, revision in sorted({(UUID(item['contact_id']), item['revision'])
                                    for item in manifest.get('importance_profile_contacts', [])}):
        profile = conn.execute("""SELECT * FROM creator_importance_profiles
            WHERE contact_id=%s AND responsible_authorization_id=%s AND revision=%s""",
            (contact, cm, revision)).fetchone()
        if profile and apply:
            importance._source_checks(conn, profile['sources'], contact, cm, lock=True)
        # A shared-group CM broadcast is not enough to restore a particular creator's old profile.
        predicate = """p.contact_id=%s AND p.responsible_authorization_id=%s AND p.revision=%s
            AND NOT p.auto_valid AND p.manual_tier IS NULL
            AND p.coverage->>'history_complete'='true' AND jsonb_array_length(p.sources)>0
            AND (""" + importance._PROFILE_SOURCE_VALID + """)
            AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.sources) s WHERE NOT (
              EXISTS(SELECT 1 FROM lark_identities i WHERE i.open_id=s->>'sender_open_id'
                AND i.contact_id=p.contact_id AND i.identity_type='user')
              OR EXISTS(SELECT 1 FROM authorization_chats ac JOIN chats ch ON ch.chat_id=ac.chat_id
                JOIN lark_identities i ON i.open_id=ac.p2p_counterpart_open_id
                WHERE ac.authorization_id=p.responsible_authorization_id AND ac.chat_id=s->>'chat_id'
                  AND ch.chat_mode='p2p' AND i.contact_id=p.contact_id AND i.identity_type='user')))"""
        args = (contact, cm, revision)
        reusable = conn.execute('SELECT 1 FROM creator_importance_profiles p WHERE ' + predicate, args).fetchone()
        if not reusable:
            counts['importance_profile_unchanged'] += 1
            continue
        if apply:
            updated = conn.execute("""UPDATE creator_importance_profiles p
                SET auto_valid=TRUE,revision=revision+1 WHERE """ + predicate + ' RETURNING revision', args).fetchone()
            if not updated:
                raise ValueError('Selected importance profile changed during repair.')
        restored_profiles.append({'contact_id': str(contact), 'revision': revision})
        counts['importance_profile_restored'] += 1

    for feature, module, table, key in (
        ('importance', importance, 'creator_importance_jobs', 'importance_job_ids'),
        ('responsibility', responsibility, 'creator_responsibility_jobs', 'responsibility_job_ids'),
    ):
        for job_id in groups[key]:
            job = conn.execute(f'SELECT * FROM {table} WHERE id=%s', (job_id,)).fetchone()
            if job and (job['reason_code'] == 'request_unknown' or feature == 'importance' and conn.execute(
                    """SELECT 1 FROM creator_importance_requests WHERE job_id=%s
                    AND (error_code='request_unknown' OR error_code='' AND finished_at IS NULL) LIMIT 1""",
                    (job_id,)).fetchone()):
                counts[feature + '_unknown_preserved'] += 1
                continue
            if (feature == 'importance' and job and job['status'] == 'failed'
                    and job['stats'].get('provider_error', {}).get('code')
                    in {'invalid_prompt', 'content_policy_violation'}):
                if job['responsible_authorization_id'] != cm:
                    raise ValueError('Importance job belongs to another CM.')
                if apply:
                    conn.execute("UPDATE creator_importance_jobs SET reason_code='provider_rejected',updated_at=NOW() "
                                 "WHERE id=%s", (job_id,))
                counts['importance_provider_rejected'] += 1
                continue
            if not job or job['status'] != 'failed' or job['reason_code'] not in REPAIRABLE_ERRORS:
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
                    WHERE id=%s AND status='failed' AND reason_code=ANY(%s::text[])""",
                    (status, reason, job_id, list(REPAIRABLE_ERRORS))).rowcount
                if changed != 1:
                    raise ValueError('Selected failed job changed during repair.')
            counts[feature + '_' + status] += 1
    if manifest.get('event_chat_ids'):
        with conn.transaction(force_rollback=not apply):
            counts['event_pairs_enqueued'] = summary_periods.touch_chats(conn, cm, manifest['event_chat_ids'])
    if apply:
        conn.execute("""INSERT INTO audit_events(id,authorization_id,action,metadata)
            VALUES(%s,%s,'creator_initialization_repaired',%s::jsonb)""",
            (uuid4(), cm, json.dumps({'manifest_created_at': manifest['created_at'], 'counts': dict(counts),
                                     'identity_contacts': [str(v) for v in groups['identity_contact_ids']],
                                     'importance_jobs': [str(v) for v in groups['importance_job_ids']],
                                     'importance_profiles': restored_profiles,
                                     'responsibility_jobs': [str(v) for v in groups['responsibility_job_ids']],
                                     'identity_retry_jobs': sorted(str(v) for v in retries),
                                     'identity_material_jobs': replayed_material,
                                     'responsibility_saved_jobs': manifest.get('responsibility_saved_job_ids', []),
                                     'event_chats': manifest.get('event_chat_ids', [])})))
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
