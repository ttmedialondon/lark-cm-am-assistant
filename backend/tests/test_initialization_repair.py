"""A targeted repair preserves checkpoints, billing and confirmed business facts."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import creator_data, creator_handle_matching, creator_importance, creator_responsibility_matching
from scripts.repair_creator_initialization import repair


def test_targeted_initialization_repair_is_readonly_by_default_and_idempotent(pg, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b'x' * 32)
    cm, own, new, processed, unseen, creator, other = [uuid4() for _ in range(7)]
    pg.execute('UPDATE ai_feature_configs SET enabled=FALSE')
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
               "VALUES(%s,'ou_repair_cm','fixture',NOW()-INTERVAL '1 hour')", (cm,))
    for index, contact in enumerate((own, new, processed, unseen, creator, other)):
        pg.execute("INSERT INTO contacts(id,display_name,entity_type,created_at) "
                   "VALUES(%s,%s,'person',NOW()-INTERVAL '1 year')", (contact, f'Repair {index}'))
        open_id = 'ou_repair_cm' if contact == own else f'ou_repair_{index}'
        pg.execute("INSERT INTO lark_identities(open_id,identity_type,contact_id) VALUES(%s,'user',%s)",
                   (open_id, contact))
    pg.execute("INSERT INTO chats(chat_id) VALUES('oc_repair')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,backfill_page_token) "
               "VALUES(%s,'oc_repair','keep-history-page')", (cm,))
    for index in (1, 2, 4):
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_type,sender_open_id,message_type,
            body_ciphertext,message_create_time) VALUES('oc_repair',%s,'user',%s,'text','fixture',NOW())""",
            (f'm{index}', f'ou_repair_{index}'))
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,'oc_repair',%s)", (cm, f'm{index}'))
    for contact in (creator, other):
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                   "VALUES(%s,'creator','confirmed','manual')", (contact,))
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), contact, cm))
    pg.execute("INSERT INTO creator_handle_jobs(contact_id,purpose,status) VALUES(%s,'identity','completed')",
               (processed,))
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE,api_key_ciphertext='fixture',key_suffix='test',"
               "creator_recognition_started_at=NOW() WHERE feature IN "
               "('creator_handle_matching','creator_importance','creator_responsibility_matching')")
    importance_id, untouched_id, responsibility_id, receipt = [uuid4() for _ in range(4)]
    importance_pack = creator_importance._cipher(settings).encrypt(json.dumps({'pack': {}, 'state': {}}))
    for job_id, contact in ((importance_id, creator), (untouched_id, other)):
        pg.execute("""INSERT INTO creator_importance_jobs(id,contact_id,responsible_authorization_id,status,
            reason_code,attempts,snapshot_version,config_fingerprint,pack_ciphertext,checkpoint_ciphertext,
            cursor_message_id,pending_messages) VALUES(%s,%s,%s,'failed','invalid_output',1,1,%s,%s,
            'keep-checkpoint','keep-cursor','{"c/m":{"message_id":"m"}}')""",
            (job_id, contact, cm, creator_importance._config_hash(creator_importance._config(pg)), importance_pack))
    pg.execute("""INSERT INTO creator_importance_requests(id,job_id,fingerprint,error_code,response_ciphertext)
        VALUES(%s,%s,'frozen-fingerprint','invalid_output','keep-failed-receipt')""", (receipt, importance_id))
    envelope = {'candidate_ids': {'CM1': str(own)}, 'pack': {'candidates': [{'id': 'CM1'}]}}
    pg.execute("""INSERT INTO creator_responsibility_jobs(id,contact_id,status,reason_code,attempts,
        snapshot_version,config_fingerprint,pack_ciphertext) VALUES(%s,%s,'failed','invalid_output',1,1,%s,%s)""",
        (responsibility_id, creator,
         creator_responsibility_matching._config_hash(creator_responsibility_matching._config(pg)),
         creator_data.cipher(settings).encrypt(json.dumps(envelope))))
    manifest = {'cm_id': str(cm), 'created_at': '2026-10-15T13:00:00Z',
                'identity_contact_ids': [str(v) for v in (new, processed, unseen, creator)],
                'importance_job_ids': [str(importance_id)], 'responsibility_job_ids': [str(responsibility_id)]}
    tables = ('contact_roles', 'contact_assignments', 'authorization_chats', 'creator_importance_requests')
    before = {table: pg.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    planned = repair(pg, manifest, settings)
    assert planned == {'identity_enrolled': 1, 'identity_already_processed': 1,
                       'identity_no_longer_eligible': 2, 'importance_ready': 1, 'responsibility_queued': 1}
    assert pg.execute('SELECT status FROM creator_importance_jobs WHERE id=%s',
                      (importance_id,)).fetchone()['status'] == 'failed'
    assert repair(pg, manifest, settings, apply=True) == planned
    job = pg.execute('SELECT * FROM creator_importance_jobs WHERE id=%s', (importance_id,)).fetchone()
    assert job['status'] == 'ready' and job['attempts'] == 1 and job['cursor_message_id'] == 'keep-cursor'
    assert job['checkpoint_ciphertext'] == 'keep-checkpoint' and job['pack_ciphertext'] == importance_pack
    assert job['pending_messages'] == {'c/m': {'message_id': 'm'}}
    assert pg.execute('SELECT status FROM creator_importance_jobs WHERE id=%s',
                      (untouched_id,)).fetchone()['status'] == 'failed'
    repeated = repair(pg, manifest, settings, apply=True)
    assert repeated['identity_already_processed'] == 2
    assert repeated['importance_unchanged'] == repeated['responsibility_unchanged'] == 1
    assert {table: pg.execute(f'SELECT * FROM {table}').fetchall() for table in tables} == before
    pg.execute("UPDATE creator_importance_jobs SET status='failed',reason_code='http_400' WHERE id=%s",
               (importance_id,))
    targeted = {**manifest, 'identity_contact_ids': [], 'responsibility_job_ids': []}
    assert repair(pg, targeted, settings, apply=True) == {'importance_ready': 1}
    assert pg.execute('SELECT attempts,cursor_message_id FROM creator_importance_jobs WHERE id=%s',
                      (importance_id,)).fetchone() == {'attempts': 1, 'cursor_message_id': 'keep-cursor'}

    # Only this explicit, known HTTP 400 is eligible for one diagnostic retry.
    retry_id = uuid4()
    pg.execute("""INSERT INTO creator_handle_jobs(id,contact_id,purpose,status,reason_code,attempts,snapshot_version,
        config_fingerprint,pack_ciphertext) VALUES(%s,%s,'identity','failed','provider_failed',1,1,%s,%s)""",
        (retry_id, processed, creator_handle_matching._job_config_hash(creator_handle_matching._config(pg), 'identity'),
         creator_data.cipher(settings).encrypt(json.dumps({'purpose': 'identity'}))))
    pg.execute("INSERT INTO creator_handle_requests(id,job_id,error_code) VALUES(%s,%s,'http_400')",
               (uuid4(), retry_id))
    monkeypatch.setattr(creator_handle_matching.evidence, 'still_valid', lambda *_args: True)
    retry_manifest = {**targeted, 'importance_job_ids': [], 'identity_retry_job_ids': [str(retry_id)]}
    assert repair(pg, retry_manifest, settings) == {'identity_retry_ready': 1}
    assert pg.execute('SELECT status FROM creator_handle_jobs WHERE id=%s',
                      (retry_id,)).fetchone()['status'] == 'failed'
    assert repair(pg, retry_manifest, settings, apply=True) == {'identity_retry_ready': 1}
    pg.execute("UPDATE creator_handle_jobs SET status='failed' WHERE id=%s", (retry_id,))
    assert repair(pg, retry_manifest, settings, apply=True) == {'identity_retry_unchanged': 1}

    # A saved success may be re-prepared only when the actual input changed; a true unknown is untouched.
    storage = creator_data.cipher(settings)
    pg.execute("UPDATE creator_responsibility_jobs SET status='failed',reason_code='request_unknown',"
               "prepared_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (responsibility_id,))
    pg.execute("""INSERT INTO creator_responsibility_requests(id,job_id,key_feature,error_code,request_id,response_id,
        input_tokens,output_tokens,response_ciphertext,material_fingerprint,finished_at)
        VALUES(%s,%s,'creator_responsibility_matching','','req-saved','resp-saved',10,2,%s,%s,NOW())""",
        (uuid4(), responsibility_id, storage.encrypt(json.dumps({'validated': {'suggestions': []}})),
         creator_responsibility_matching._hash(creator_responsibility_matching._payment_material(envelope))))
    monkeypatch.setattr(creator_responsibility_matching, 'recover_saved_result', lambda *_args: False)
    monkeypatch.setattr(creator_responsibility_matching.evidence, 'still_valid', lambda *_args: True)
    monkeypatch.setattr(creator_responsibility_matching, '_build_material', lambda *_args: {
        'pack': {**envelope['pack'], 'new_material': True}, 'candidate_ids': envelope['candidate_ids'], 'refs': []})
    saved_manifest = {**targeted, 'importance_job_ids': [], 'responsibility_saved_job_ids': [str(responsibility_id)]}
    assert repair(pg, saved_manifest, settings) == {'responsibility_new_material_queued': 1}
    assert repair(pg, saved_manifest, settings, apply=True) == {'responsibility_new_material_queued': 1}
    assert repair(pg, saved_manifest, settings, apply=True) == {'responsibility_saved_not_reusable': 1}
    pg.execute("UPDATE creator_responsibility_jobs SET status='failed',reason_code='request_unknown' WHERE id=%s",
               (responsibility_id,))
    pg.execute("UPDATE creator_responsibility_requests SET error_code='request_unknown' WHERE job_id=%s",
               (responsibility_id,))
    assert repair(pg, saved_manifest, settings, apply=True) == {'responsibility_saved_not_reusable': 1}
    assert pg.execute('SELECT reason_code FROM creator_responsibility_jobs WHERE id=%s',
                      (responsibility_id,)).fetchone()['reason_code'] == 'request_unknown'

    # Provider rejection is isolated, not endlessly retried; unknown billing remains unchanged.
    pg.execute("UPDATE creator_importance_jobs SET status='failed',reason_code='http_400',stats=%s::jsonb WHERE id=%s",
               (json.dumps({'provider_error': {'code': 'invalid_prompt'}}), importance_id))
    assert repair(pg, targeted, settings, apply=True) == {'importance_provider_rejected': 1}
    pg.execute("UPDATE creator_importance_jobs SET reason_code='request_unknown' WHERE id=%s", (untouched_id,))
    unknown_before = pg.execute('SELECT * FROM creator_importance_jobs WHERE id=%s', (untouched_id,)).fetchone()
    assert repair(pg, {**targeted, 'importance_job_ids': [str(untouched_id)]}, settings, apply=True) == {
        'importance_unknown_preserved': 1}
    assert pg.execute('SELECT * FROM creator_importance_jobs WHERE id=%s', (untouched_id,)).fetchone() == unknown_before
    assert {table: pg.execute(f'SELECT * FROM {table}').fetchall() for table in tables} == before
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
    with pytest.raises(ValueError, match='reading grant'):
        repair(pg, manifest, settings, apply=True)
