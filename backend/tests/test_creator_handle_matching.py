"""Durable matching workflow on isolated PostgreSQL; provider responses are synthetic."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cap_fixtures import _snapshot
from cap_fixtures import pilot as parent_pilot

from app import ai_dispatch, ai_feature_config, config, contact_handles, creator_data, openai_client
from app import creator_handle_matching as matching
from app.crypto import message_body_cipher

pilot = parent_pilot


@pytest.fixture
def matching_flow(pg, pilot, monkeypatch):
    state = SimpleNamespace(
        settings=pilot.settings,
        contact=pilot.ids["creator"],
        calls=[],
        valid=True,
        before_response=None,
        error=None,
        review=False,
        relation="self_account",
        period="at_message",
        needed=[],
        stats={},
        legacy_stats=False,
        missing_reply_ids=[],
        material_version=1,
        reference_version=1,
        catalog_handles=frozenset({"first.handle", "backup_2"}),
        real_build=matching.evidence.build,
        real_still_valid=matching.evidence.still_valid,
    )
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: state.settings)
    monkeypatch.setattr(matching, "catalog", lambda: (state.catalog_handles, "catalog-v1"))
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_, **__: state.valid)

    def prepare(_conn, _settings, contact, _catalog, *, recheck=False):
        pairs = contact_handles.confirmed_pairs(_conn, [contact])
        confirmed = sorted({p["handle"] for p in pairs})
        pack = {
            "target": "U1",
            "creator_known": True,
            "synthetic_material_revision": state.material_version,
            "handles": [
                {"id": "H1", "handle": "first.handle", "catalog_verified": "first.handle" in _catalog},
                {"id": "H2", "handle": "backup_2", "catalog_verified": "backup_2" in _catalog},
            ],
            "people": [{"id": "U1", "role": "external"}],
            "chats": [{"id": "G1", "name": "Synthetic test conversation", "mode": "group"}],
            "messages": [
                {
                    "id": "M1",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:00:00+00:00",
                    "reply": "M_absent" if "M1" in state.missing_reply_ids else None,
                    "text": "My TikTok account is first.handle",
                },
                {
                    "id": "M2",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:01:00+00:00",
                    "reply": "M_absent" if "M2" in state.missing_reply_ids else None,
                    "text": "My backup account is backup_2",
                },
            ],
            "coverage": {"full_history": False, "missing_reply_ids": state.missing_reply_ids, "excerpted_ids": []},
        }
        if not recheck:
            pack["handles"] = [h for h in pack["handles"] if h["handle"] not in confirmed]
        return {
            "pack": pack if pack["handles"] else None,
            "refs": [
                {"kind": "message", "label": label, "synthetic_version": state.reference_version}
                for label in ("M1", "M2")
            ],
            "fingerprint": f"synthetic-{contact}-{state.material_version}-{[h['handle'] for h in pack['handles']]}",
            "reason": "ready" if pack["handles"] else "already_confirmed",
            "stats": {
                "messages": 2,
                "confirmed_handles": confirmed,
                "human_confirmed_handles": sorted({p["handle"] for p in pairs if p["human_confirmed"]}),
                "manual_recheck": recheck,
                "recheck_handles": confirmed if recheck else [],
                **({"critical_evidence_omitted_handles": []} if not state.legacy_stats else {}),
                **state.stats,
            },
        }

    def respond(_config, _key, pack, _contact, **kwargs):
        state.calls.append(kwargs)
        if state.before_response:
            state.before_response()
        if state.error:
            raise state.error
        result = {
            "creator": {"state": "known", "refs": []},
            "links": [
                {
                    "h": handle["id"],
                    "decision": "supported",
                    "relation": state.relation,
                    "period": state.period,
                    "code": "self_claim",
                    "refs": ["M" + handle["id"][1:]],
                    "needed": state.needed,
                }
                for handle in pack["handles"]
            ],
        }
        if state.review:
            result["links"][-1].update(
                decision="review", relation="uncertain", code="ambiguous_reply", needed=["parent_reply"]
            )
        return {
            "text": json.dumps(result),
            "response_id": "resp_synthetic",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            },
            "telemetry": {"openai_request_id": "req_synthetic"},
        }

    monkeypatch.setattr(matching.evidence, "build", prepare)
    monkeypatch.setattr(matching.api, "call_openai", respond)

    def enable(api_key="sk-synthetic-test-only"):
        ai_feature_config.save_feature_config(
            pg,
            ai_feature_config.api_key_cipher(state.settings.token_encryption_key),
            matching.FEATURE,
            provider="openai",
            project_id="proj_synthetic",
            model="gpt-5.6-luna",
            enabled=True,
            api_key=api_key,
            updated_by_authorization_id=pilot.ids["owner"],
        )
        # The fallback reconciliation is covered separately; keep this test to one chosen contact.
        pg.execute(
            "UPDATE ai_feature_configs SET automatic_queue_checked_at=NOW() WHERE feature=%s", (matching.FEATURE,)
        )

    state.enable = enable
    return state


def _enqueue(pg, contact, source="manual"):
    return pg.execute("SELECT enqueue_creator_handle(%s,%s,TRUE) id", (contact, source)).fetchone()["id"]


def test_material_round_debounce_and_targeted_schedule_repair_preserve_live_requests(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    protected = _snapshot(pg, ('contact_roles', 'contact_assignments', 'contact_social_accounts'))
    unknown = uuid4()
    pg.execute("INSERT INTO contacts(id,created_at) VALUES(%s,NOW()-INTERVAL '30 days')", (unknown,))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id) VALUES('ou_debounce_identity',%s)", (unknown,))
    signals = iter(range(1, 100))
    now = pg.execute('SELECT NOW() now').fetchone()['now']

    def wake(purpose, contact, immediate=False):
        if purpose == 'identity':
            return pg.execute("SELECT enqueue_creator_identity(%s,NOW()+(%s*INTERVAL '1 second'),%s) id",
                              (contact, next(signals), immediate)).fetchone()['id']
        return pg.execute("SELECT enqueue_creator_handle(%s,'incremental',%s) id",
                          (contact, immediate)).fetchone()['id']

    for purpose, contact in (('handles', flow.contact), ('identity', unknown)):
        job = wake(purpose, contact, True)
        for status in ('waiting_evidence', 'review_required', 'ready'):
            pg.execute("""UPDATE creator_handle_jobs SET status=%s,prepared_at=NOW()-INTERVAL '20 minutes',
                debounce_started_at=NOW()-INTERVAL '20 minutes',not_before=NOW()-INTERVAL '18 minutes'
                WHERE id=%s""", (status, job))
            assert wake(purpose, contact) == job
            row = pg.execute('SELECT * FROM creator_handle_jobs WHERE id=%s', (job,)).fetchone()
            assert row['status'] == 'queued' and row['debounce_started_at'] == now
            assert row['not_before'] == now + timedelta(seconds=30)
        pg.execute("UPDATE creator_handle_jobs SET status='retry_wait',not_before=NOW()+INTERVAL '5 minutes' "
                   "WHERE id=%s", (job,))
        wake(purpose, contact)
        assert pg.execute('SELECT not_before FROM creator_handle_jobs WHERE id=%s',
                          (job,)).fetchone()['not_before'] == now + timedelta(minutes=5)
        pg.execute("UPDATE creator_handle_jobs SET debounce_started_at=NOW()-INTERVAL '110 seconds',"
                   "not_before=NOW()+INTERVAL '5 seconds' WHERE id=%s", (job,))
        wake(purpose, contact)
        assert pg.execute('SELECT debounce_started_at,not_before FROM creator_handle_jobs WHERE id=%s',
                          (job,)).fetchone() == {
                              'debounce_started_at': now - timedelta(seconds=110),
                              'not_before': now + timedelta(seconds=10)}
        pg.execute("UPDATE creator_handle_jobs SET debounce_started_at=NOW()-INTERVAL '20 minutes',"
                   "not_before=NOW() WHERE id=%s", (job,))
        wake(purpose, contact)
        assert pg.execute('SELECT not_before FROM creator_handle_jobs WHERE id=%s',
                          (job,)).fetchone()['not_before'] == now
        pg.execute("INSERT INTO creator_handle_requests(id,job_id,error_code) VALUES(%s,%s,'request_unknown')",
                   (uuid4(), job))
        receipts = _snapshot(pg, ('creator_handle_requests',))
        for status in ('preparing', 'running'):
            token = uuid4()
            pg.execute("""UPDATE creator_handle_jobs SET status=%s,claim_token=%s,
                lease_until=NOW()+INTERVAL '5 minutes',snapshot_version=desired_version,
                pack_ciphertext='frozen-synthetic-material' WHERE id=%s""", (status, token, job))
            wake(purpose, contact)
            row = pg.execute('SELECT * FROM creator_handle_jobs WHERE id=%s', (job,)).fetchone()
            assert row['status'] == status and row['claim_token'] == token
            assert row['lease_until'] == now + timedelta(minutes=5)
            assert row['pack_ciphertext'] == 'frozen-synthetic-material'
            assert row['desired_version'] == row['snapshot_version'] + 1
        assert _snapshot(pg, receipts.keys()) == receipts

    affected = set()
    for purpose, source, prepared, version in (
        ('handles', 'incremental', True, 2), ('identity', 'incremental', True, 2),
        ('handles', 'incremental', False, 2), ('handles', 'manual', True, 2), ('handles', 'incremental', True, 1),
    ):
        contact = uuid4()
        pg.execute('INSERT INTO contacts(id) VALUES(%s)', (contact,))
        job = pg.execute("""INSERT INTO creator_handle_jobs(contact_id,purpose,source,desired_version,snapshot_version,
            prepared_at,debounce_started_at,not_before,updated_at)
            VALUES(%s,%s,%s,%s,1,CASE WHEN %s THEN NOW()-INTERVAL '20 minutes' END,
              NOW()-INTERVAL '20 minutes',NOW()-INTERVAL '18 minutes',NOW()-INTERVAL '1 minute') RETURNING id""",
            (contact, purpose, source, version, prepared)).fetchone()['id']
        if source == 'incremental' and prepared and version == 2:
            affected.add(job)
    before = {row['id']: row for row in pg.execute('SELECT * FROM creator_handle_jobs').fetchall()}
    migration = (Path(__file__).parents[1] / 'migrations/110_creator_matching_debounce_windows.sql').read_text()
    for _ in range(2):
        pg.execute(migration)
        for row in pg.execute('SELECT * FROM creator_handle_jobs').fetchall():
            expected = before[row['id']]
            if row['id'] in affected:
                expected = {**expected, 'not_before': expected['updated_at'] + timedelta(seconds=30),
                            'debounce_started_at': expected['updated_at']}
            assert row == expected
    assert _snapshot(pg, protected.keys()) == protected
    assert not flow.calls


def test_page_signals_wake_actual_people_and_reply_context_not_all_group_members(pg, matching_flow, pilot):
    flow = matching_flow
    flow.enable()
    protected = _snapshot(pg, ('contact_roles', 'contact_social_accounts', 'contact_assignments'))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('signal_group','Creator group','group'),"
               "('other_group','Other group','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'signal_group')",
               (pilot.ids['cm'],))
    for name in ('creator', 'silent', 'legacy'):
        pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('signal_group',%s,'user')",
                   ('ou_' + name,))
    now = pg.execute('SELECT NOW() now').fetchone()['now']

    def message(key, sender, minutes, *, parent=None, chat='signal_group', mentions=None):
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
            message_create_time,parent_id,mentions_json) VALUES(%s,%s,%s,'user','text',%s,%s,%s::jsonb)""",
            (chat, key, 'ou_' + sender, now + timedelta(minutes=minutes), parent, json.dumps(mentions)))

    message('old_creator', 'creator', -1440)
    message('unrelated_creator', 'legacy', 0, chat='other_group')
    message('announcement', 'cm', 0)
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['announcement'])
    assert not pg.execute('SELECT 1 FROM creator_handle_jobs').fetchone()
    message('creator_answer', 'creator', 1)
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['creator_answer'])
    assert pg.execute('SELECT contact_id FROM creator_handle_jobs').fetchall() == [{'contact_id': flow.contact}]
    pg.execute("UPDATE creator_handle_jobs SET status='completed'")
    message('cm_question', 'cm', 2)
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['cm_question'])
    assert pg.execute('SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s',
                      (flow.contact,)).fetchone()['n'] == 2
    # A late original question must wake its answer even outside the nearby-context window.
    message('old_question', 'cm', -2880)
    message('late_answer', 'silent', -1440, parent='old_question')
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['old_question'])
    assert pg.execute('SELECT 1 FROM creator_handle_jobs WHERE contact_id=%s', (pilot.ids['silent'],)).fetchone()
    message('new_handle', 'legacy', 1440)
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['new_handle'])
    assert pg.execute('SELECT 1 FROM creator_handle_jobs WHERE contact_id=%s', (pilot.ids['legacy'],)).fetchone()
    before = _snapshot(pg, ('creator_handle_jobs',))
    matching.touch_chats(pg, ['signal_group'], recognition_message_ids=['unrelated_creator'])
    assert _snapshot(pg, before.keys()) == before
    pg.execute("UPDATE chats SET name='A new account & TT Media' WHERE chat_id='signal_group'")
    assert all(row['desired_version'] > 1 for row in pg.execute(
        "SELECT desired_version FROM creator_handle_jobs WHERE status='queued'").fetchall())
    assert _snapshot(pg, protected.keys()) == protected
    assert not flow.calls


def test_new_person_identity_queue_admits_without_handle_and_preserves_existing_facts(
    pg, matching_flow, pilot, monkeypatch
):
    flow = matching_flow
    flow.enable()
    monkeypatch.setattr(matching.evidence, 'build', flow.real_build)
    monkeypatch.setattr(matching.evidence, 'still_valid', flow.real_still_valid)
    protected = _snapshot(pg, ('contact_assignments', 'contact_social_accounts'))
    roles = _snapshot(pg, ('contact_roles',))['contact_roles']
    new_id, old_id = uuid4(), uuid4()
    for contact, name, age in ((old_id, 'old_unread', 2), (new_id, 'new_creator', 0)):
        pg.execute("INSERT INTO contacts(id,entity_type,display_name,created_at) "
                   "VALUES(%s,'person',%s,NOW()-(%s*INTERVAL '1 day'))", (contact, name, age))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,identity_type,tenant_classification) "
                   "VALUES(%s,%s,'user','known_external')", ('ou_' + name, contact))
    assert not pg.execute('SELECT 1 FROM creator_handle_jobs WHERE contact_id=%s', (old_id,)).fetchone()
    assert pg.execute('SELECT purpose FROM creator_handle_jobs WHERE contact_id=%s',
                      (new_id,)).fetchone()['purpose'] == 'identity'
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('new_private','New creator','p2p')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
               "VALUES(%s,'new_private','ou_new_creator')", (pilot.ids['cm'],))
    cipher = message_body_cipher(flow.settings.token_encryption_key)
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
               "message_create_time,body_ciphertext) VALUES('new_private','new_activity','ou_new_creator',"
               "'user','text',NOW(),%s)",
               (cipher.encrypt(json.dumps({'text': 'I film TikTok videos and post my sponsored video tonight.'})),))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'new_private','new_activity')", (pilot.ids['cm'],))
    matching.touch_chats(pg, ['new_private'], recognition_message_ids=['new_activity'])
    version = pg.execute('SELECT desired_version FROM creator_handle_jobs WHERE contact_id=%s',
                         (new_id,)).fetchone()['desired_version']
    matching.touch_chats(pg, ['new_private'], recognition_message_ids=['new_activity'])
    assert pg.execute('SELECT desired_version FROM creator_handle_jobs WHERE contact_id=%s',
                      (new_id,)).fetchone()['desired_version'] == version
    # A historic first-body hydration cannot requeue an old identity task.
    pg.execute("INSERT INTO creator_handle_jobs(contact_id,purpose,status,identity_signal_at) "
               "VALUES(%s,'identity','waiting_evidence',NOW())", (old_id,))
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
               "message_create_time,message_update_time) VALUES('new_private','old_hydration','ou_old_unread',"
               "'user','text',NOW()-INTERVAL '2 days',NOW()-INTERVAL '2 days')")
    pg.execute("UPDATE chat_messages SET body_fingerprint=%s WHERE message_id='old_hydration'", ('f' * 64,))
    assert pg.execute('SELECT desired_version FROM creator_handle_jobs WHERE contact_id=%s',
                      (old_id,)).fetchone()['desired_version'] == 1
    pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE contact_id=%s", (new_id,))
    calls = []

    def identity_response(_config, _key, payload, _contact, **kwargs):
        calls.append(kwargs)
        assert '_rules' not in payload and payload['handles'] == []
        assert '达人身份证据规则' in kwargs['instructions'] and '历史案例与识别验收' not in kwargs['instructions']
        own = next(m['id'] for m in payload['messages'] if m['speaker'] == payload['target'])
        return {'text': json.dumps({'creator': {'state': 'supported', 'code': 'own_activity',
            'confidence': 0.99, 'refs': [own]}, 'links': []}), 'response_id': 'resp_identity_synthetic',
            'usage': {'input_tokens': 100, 'output_tokens': 50}, 'telemetry': {}}

    monkeypatch.setattr(matching.api, 'call_openai', identity_response)
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job and job['purpose'] == 'identity'
    matching.run(job, flow.settings)
    saved = pg.execute('SELECT status,stats FROM creator_handle_jobs WHERE id=%s', (job['id'],)).fetchone()
    assert saved['status'] == 'completed' and saved['stats']['identity_confirmed'] is True
    assert pg.execute("SELECT source FROM contact_roles WHERE contact_id=%s AND role='creator'",
                      (new_id,)).fetchone() == {
        'source': 'creator_identity_auto_verified'}
    assert _snapshot(pg, protected.keys()) == protected
    assert all(role in _snapshot(pg, ('contact_roles',))['contact_roles'] for role in roles)
    assert len(calls) == 1 and not matching.prepare_next(flow.settings)
    assert pg.execute('SELECT enqueue_creator_identity(%s,NOW(),TRUE) id', (new_id,)).fetchone()['id'] is None
    assert not pg.execute('SELECT 1 FROM contact_assignments WHERE contact_id=%s', (new_id,)).fetchone()

    def add_private_person(name, handle):
        contact = uuid4()
        chat, message, open_id = 'private_' + name, 'activity_' + name, 'ou_' + name
        pg.execute("INSERT INTO contacts(id,entity_type,display_name) VALUES(%s,'person',%s)", (contact, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,identity_type,tenant_classification) "
                   "VALUES(%s,%s,'user','known_external')", (open_id, contact))
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'p2p')", (chat, name))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
                   "VALUES(%s,%s,%s)", (pilot.ids['cm'], chat, open_id))
        pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
                   "message_create_time,body_ciphertext) VALUES(%s,%s,%s,'user','text',NOW(),%s)",
                   (chat, message, open_id, cipher.encrypt(json.dumps({'text':
                       f'My TikTok account is {handle}. I film TikTok videos and post my sponsored review tonight.'}))))
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,%s,%s)", (pilot.ids['cm'], chat, message))
        matching.touch_chats(pg, [chat], recognition_message_ids=[message])
        pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE contact_id=%s", (contact,))
        return contact

    def identity_with_handle(_config, _key, payload, _contact, **kwargs):
        calls.append(kwargs)
        assert payload['purpose'] == 'identity' and '_rules' not in payload
        assert len(payload['handles']) == 1
        own = next(m['id'] for m in payload['messages'] if m['speaker'] == payload['target'])
        if flow.before_response:
            flow.before_response()
        return {'text': json.dumps({'creator': {'state': 'supported', 'code': 'own_activity',
            'confidence': 0.99, 'refs': [own]}, 'links': [{
                'h': payload['handles'][0]['id'], 'decision': 'supported', 'relation': 'self_account',
                'period': 'at_message', 'code': 'self_claim', 'refs': [own], 'needed': []}]}),
            'response_id': 'resp_identity_handle', 'usage': {'input_tokens': 110, 'output_tokens': 60},
            'telemetry': {'openai_request_id': 'req_identity_handle'}}

    monkeypatch.setattr(matching.api, 'call_openai', identity_with_handle)
    with_handle = add_private_person('new_with_handle', 'fresh.creator')
    original_assignments = _snapshot(pg, ('contact_assignments', 'creator_account_assignments'))
    old_links = _snapshot(pg, ('contact_social_accounts',))['contact_social_accounts']
    assert matching.prepare_next(flow.settings)
    linked_job = matching.claim()
    assert linked_job and linked_job['contact_id'] == with_handle and linked_job['purpose'] == 'identity'
    # A duplicate sync signal can advance a version while the same evidence's paid response is in flight.
    flow.before_response = lambda: pg.execute(
        "SELECT enqueue_creator_identity(%s,NOW()+INTERVAL '1 second',TRUE)", (with_handle,))
    matching.run(linked_job, flow.settings)
    pending = pg.execute('SELECT status,desired_version FROM creator_handle_jobs WHERE id=%s',
                         (linked_job['id'],)).fetchone()
    assert pending['status'] == 'queued' and pending['desired_version'] > linked_job['snapshot_version']
    receipt = pg.execute('SELECT error_code,billed_tokens,response_ciphertext FROM creator_handle_requests '
                         'WHERE id=%s', (linked_job['reservation_id'],)).fetchone()
    assert receipt['error_code'] == '' and receipt['billed_tokens'] == 170 and receipt['response_ciphertext']
    assert not pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s", (with_handle,)).fetchone()
    flow.before_response = None
    assert matching.prepare_next(flow.settings)
    saved = pg.execute('SELECT id,status,reason_code,stats FROM creator_handle_jobs WHERE contact_id=%s',
                       (with_handle,)).fetchone()
    assert saved['id'] == linked_job['id'] and saved['status'] == 'completed' and saved['reason_code'] == 'cached'
    assert saved['stats']['identity_confirmed'] is True and saved['stats']['auto_linked_handles'] == ['fresh.creator']
    assert contact_handles.for_contacts(pg, [with_handle])[with_handle] == ['fresh.creator']
    assert pg.execute("SELECT source FROM contact_roles WHERE contact_id=%s AND role='creator'",
                      (with_handle,)).fetchone() == {'source': 'creator_identity_auto_verified'}
    assert len(calls) == 2 and matching.claim() is None
    assert pg.execute('SELECT count(*) n FROM creator_handle_requests WHERE job_id=%s',
                      (linked_job['id'],)).fetchone()['n'] == 1
    assert _snapshot(pg, original_assignments.keys()) == original_assignments
    assert all(link in _snapshot(pg, ('contact_social_accounts',))['contact_social_accounts'] for link in old_links)
    assert not pg.execute('SELECT 1 FROM contact_assignments WHERE contact_id=%s', (with_handle,)).fetchone()

    revoked = add_private_person('new_revoked', 'revoked.creator')
    unchanged = _snapshot(pg, ('contact_roles', 'contact_social_accounts', 'contact_assignments',
                               'creator_account_assignments'))
    assert matching.prepare_next(flow.settings)
    revoked_job = matching.claim()
    assert revoked_job and revoked_job['contact_id'] == revoked
    flow.before_response = lambda: pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s",
                                              (pilot.ids['cm'],))
    matching.run(revoked_job, flow.settings)
    assert _snapshot(pg, unchanged.keys()) == unchanged
    assert not pg.execute('SELECT 1 FROM contact_roles WHERE contact_id=%s', (revoked,)).fetchone()
    assert contact_handles.for_contacts(pg, [revoked])[revoked] == []
    paid = pg.execute('SELECT billed_tokens,response_ciphertext,error_code FROM creator_handle_requests WHERE id=%s',
                      (revoked_job['reservation_id'],)).fetchone()
    assert paid['billed_tokens'] == 170 and paid['response_ciphertext'] and paid['error_code'] == ''
    assert len(calls) == 3


def _prepared_job(pg, flow, source="manual"):
    _enqueue(pg, flow.contact, source)
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job is not None
    return job


def test_paid_handle_receipt_survives_inflight_signal_without_another_request(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    flow.review = True
    job = _prepared_job(pg, flow, source='incremental')
    flow.before_response = lambda: _enqueue(pg, flow.contact, source='incremental')
    matching.run(job, flow.settings)
    assert pg.execute('SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s',
                      (job['id'],)).fetchone() == {'status': 'queued', 'result_ciphertext': None}
    flow.before_response = None
    pg.execute("UPDATE creator_handle_jobs SET created_at=NOW()-INTERVAL '7 days' WHERE id=%s", (job['id'],))
    waiting = uuid4()
    pg.execute("INSERT INTO contacts(id,created_at) VALUES(%s,NOW()-INTERVAL '30 days')", (waiting,))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id) VALUES('ou_waiting_identity',%s)", (waiting,))
    pg.execute("INSERT INTO creator_handle_jobs(contact_id,purpose,not_before) "
               "VALUES(%s,'identity',NOW()-INTERVAL '1 second')", (waiting,))

    def earlier_identity(_conn, _settings, contact, _catalog, *, purpose, recheck=False):
        assert contact == waiting and purpose == 'identity' and not recheck
        return {'pack': None, 'refs': [], 'fingerprint': 'empty-identity', 'reason': 'no_text',
                'stats': {'confirmed_handles': [], 'manual_recheck': False}}

    with monkeypatch.context() as material:
        material.setattr(matching.evidence, 'build', earlier_identity)
        assert matching.prepare_next(flow.settings)
    assert pg.execute('SELECT status FROM creator_handle_jobs WHERE contact_id=%s',
                      (waiting,)).fetchone()['status'] == 'waiting_evidence'
    assert pg.execute('SELECT status FROM creator_handle_jobs WHERE id=%s',
                      (job['id'],)).fetchone()['status'] == 'queued'
    flow.valid = False
    assert matching.prepare_next(flow.settings)
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    flow.valid = True
    assert matching.prepare_next(flow.settings)
    assert pg.execute('SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s',
                      (job['id'],)).fetchone() == {'status': 'review_required', 'reason_code': 'cached'}
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ['first.handle']
    assert matching.claim() is None and len(flow.calls) == 1
    assert pg.execute('SELECT count(*) n FROM creator_handle_requests WHERE job_id=%s',
                      (job['id'],)).fetchone()['n'] == 1


def test_opt_in_coalescing_multi_handle_auto_links_and_exact_cache(pg, matching_flow):
    flow = matching_flow
    assert _enqueue(pg, flow.contact) is None
    assert not matching.prepare_next(flow.settings) and matching.claim() is None
    assert not flow.calls
    flow.enable()
    before = _snapshot(
        pg,
        ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"),
    )
    original_links = _snapshot(pg, ("contact_social_accounts",))["contact_social_accounts"]
    identifier = _enqueue(pg, flow.contact)
    assert _enqueue(pg, flow.contact) == identifier
    assert pg.execute("SELECT count(*) n FROM creator_handle_jobs").fetchone()["n"] == 1
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job["attempts"] == 1 and job["snapshot_version"] == job["desired_version"] == 2
    matching.run(job, flow.settings)
    row = pg.execute("SELECT * FROM creator_handle_jobs WHERE id=%s", (identifier,)).fetchone()
    assert row["status"] == "completed" and row["stats"]["supported_handles"] == ["first.handle", "backup_2"]
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    assert row["response_id"] == "resp_synthetic" and row["request_id"] == "req_synthetic"
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] == 150
    view = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))
    assert len(view["result"]["links"]) == 2
    assert flow.calls[0]["reasoning_effort"] == "none" and flow.calls[0]["total_timeout"] == 45
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    links = pg.execute(
        "SELECT relationship,status,source FROM contact_social_accounts WHERE contact_id=%s", (flow.contact,)
    ).fetchall()
    assert links == [{"relationship": "owner", "status": "confirmed", "source": "creator_handle_auto_verified"}] * 2
    audits = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchall()
    assert {row["metadata"]["handle"] for row in audits} == {"first.handle", "backup_2"}
    assert all(
        row["metadata"]["job_id"] == str(identifier) and row["metadata"]["contact_id"] == str(flow.contact)
        for row in audits
    )
    assert all(
        row["metadata"]["refs"] == ["M1" if row["metadata"]["handle"] == "first.handle" else "M2"]
        and row["metadata"]["prompt_version"] == matching.evidence.PROMPT_VERSION
        and row["metadata"]["evidence_refs"]
        == [{"kind": "message", "label": row["metadata"]["refs"][0], "synthetic_version": 1}]
        and "text" not in row["metadata"]
        for row in audits
    )
    # Rotating credentials or a readable grant does not change the model's semantic input.
    flow.enable("sk-success-cache-replacement")
    flow.reference_version += 1
    new_id = _enqueue(pg, flow.contact)
    assert new_id != identifier and matching.prepare_next(flow.settings)
    cached = pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (new_id,)).fetchone()
    assert cached == {"status": "completed", "reason_code": "cached"}
    assert matching.claim() is None and len(flow.calls) == 1
    assert pg.execute("SELECT refs FROM creator_handle_jobs WHERE id=%s", (new_id,)).fetchone()["refs"] == [
        {"kind": "message", "label": label, "synthetic_version": 2} for label in ("M1", "M2")
    ]
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 2
    )
    assert (
        pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchall() == audits
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    # Simulate a pre-upgrade suggestion-only cache using isolated fixture data.
    pg.execute(
        "DELETE FROM contact_social_accounts WHERE contact_id=%s AND source='creator_handle_auto_verified'",
        (flow.contact,),
    )
    pg.execute("DELETE FROM audit_events WHERE action='creator_handle_auto_linked'")
    migrated_id = _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    assert (
        pg.execute(
            "SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked' AND metadata->>'job_id'=%s",
            (str(migrated_id),),
        ).fetchone()["n"]
        == 2
    )
    assert all(row in _snapshot(pg, ("contact_social_accounts",))["contact_social_accounts"] for row in original_links)
    assert _snapshot(pg, before.keys()) == before


def test_stale_version_and_revoked_evidence_never_publish_suggestions(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    version = matching.evidence.PROMPT_VERSION
    # A ready pack built under retired rules must reprepare before reserving or sending a request.
    with monkeypatch.context() as rules:
        rules.setattr(matching.evidence, "PROMPT_VERSION", version + "-retired")
        old_id = _enqueue(pg, flow.contact)
        assert matching.prepare_next(flow.settings)
        old_hash = pg.execute("SELECT config_fingerprint FROM creator_handle_jobs WHERE id=%s", (old_id,)).fetchone()[
            "config_fingerprint"
        ]
    assert matching.claim() is None and not flow.calls
    assert not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (old_id,)).fetchone() == {
        "status": "queued", "reason_code": "snapshot_changed"
    }
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job is not None and job["stats"]["prompt_version"] == version
    assert job["config_fingerprint"] != old_hash
    flow.before_response = lambda: _enqueue(pg, flow.contact)
    matching.run(job, flow.settings)
    current = pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert current == {"status": "queued", "result_ciphertext": None}
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] == 150
    flow.before_response = None
    assert matching.prepare_next(flow.settings)
    next_job = matching.claim()
    assert next_job["snapshot_version"] > job["snapshot_version"]
    flow.valid = False
    matching.run(next_job, flow.settings)
    assert len(flow.calls) == 1  # Revocation before network releases the reservation, without another API call.
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (next_job["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 0
    )
    # Revocation after networking must also fence the formal association, not only the suggestion.
    flow.valid = True
    flow.before_response = lambda: setattr(flow, "valid", False)
    after_network = _prepared_job(pg, flow)
    matching.run(after_network, flow.settings)
    assert len(flow.calls) == 2
    assert (
        pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)).fetchone()[
            "result_ciphertext"
        ]
        is None
    )
    # A grant can disappear while waiting for writer locks, after the post-response check passed.
    flow.valid, flow.before_response = True, None
    at_write = _prepared_job(pg, flow)
    validity = iter((True, True, False))
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_: next(validity))
    matching.run(at_write, flow.settings)
    assert len(flow.calls) == 3
    assert (
        pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (at_write["id"],)).fetchone()[
            "result_ciphertext"
        ]
        is None
    )
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (at_write["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # Rules changed during a paid request fence publication, but preserve the billed response ledger.
    with monkeypatch.context() as rules:
        rules.setattr(matching.evidence, "still_valid", lambda *_: True)
        changed_rules = _prepared_job(pg, flow)
        flow.before_response = lambda: rules.setattr(matching.evidence, "PROMPT_VERSION", version + "-next")
        matching.run(changed_rules, flow.settings)
        assert len(flow.calls) == 4
        assert pg.execute(
            "SELECT status,result_ciphertext,reason_code FROM creator_handle_jobs WHERE id=%s", (changed_rules["id"],)
        ).fetchone() == {"status": "queued", "result_ciphertext": None, "reason_code": "snapshot_changed"}
        response = pg.execute("SELECT billed_tokens,response_ciphertext FROM creator_handle_requests WHERE id=%s",
                              (changed_rules["reservation_id"],)).fetchone()
        assert response["billed_tokens"] == 150 and response["response_ciphertext"] is not None
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    flow.before_response = None


def test_provider_delay_quota_pause_and_uncertain_usage_survive_retries(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic overload", retry_after_seconds=7200, retryable=True,
                                                telemetry={"http_status": 429})
    delays = []
    monkeypatch.setattr(matching.api, "retry_delay", lambda wait, attempts: delays.append((wait, attempts)) or 7200.5)
    matching.run(job, flow.settings)
    row = pg.execute(
        "SELECT status,EXTRACT(EPOCH FROM not_before-NOW()) seconds FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert row["status"] == "retry_wait" and row["seconds"] >= 7200
    assert delays == [(7200, job["attempts"])]
    assert pg.execute("SELECT j.not_before=s.not_before AS same_deadline FROM creator_handle_jobs j "
                      "CROSS JOIN ai_admission_state s WHERE j.id=%s", (job["id"],)).fetchone()["same_deadline"]
    assert ai_dispatch.status(pg)["state"] == "throttled" and matching.claim() is None
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] is None
    ai_dispatch.resume(pg)
    pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (job["id"],))
    retry = matching.claim()
    assert retry["attempts"] == 2 and retry["reservation_id"] != job["reservation_id"]
    flow.error = openai_client.OpenAIRequestError("synthetic quota", is_quota=True)
    matching.run(retry, flow.settings)
    assert ai_dispatch.status(pg)["state"] == "paused" and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "failed",
        "reason_code": "quota_paused",
    }
    assert len(flow.calls) == 2
    ai_dispatch.resume(pg)
    unknown = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    matching.run(unknown, flow.settings)
    assert matching.claim() is None and len(flow.calls) == 3
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s",
                      (unknown["id"],)).fetchone() == {"status": "failed", "reason_code": "request_unknown"}
    logged = pg.execute("SELECT error_code,billed_tokens FROM creator_handle_requests WHERE id=%s",
                        (unknown["reservation_id"],)).fetchone()
    assert logged == {"error_code": "request_unknown", "billed_tokens": None}
    flow.enable("sk-rotated-after-unknown")
    blocked = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (blocked,)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown",
    }
    assert len(flow.calls) == 3
    flow.error = None
    retried = _prepared_job(pg, flow)  # Explicit Owner recheck is still allowed.
    matching.run(retried, flow.settings)
    assert len(flow.calls) == 4

    flow.material_version += 1
    rejected = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("HTTP 400", telemetry={
        "http_status": 400, "provider_error": {"code": "invalid_prompt"}},
        provider_diagnostic={"code": "invalid_prompt", "message": "PRIVATE REJECTION DETAIL"})
    matching.run(rejected, flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s",
                      (rejected["id"],)).fetchone() == {"status": "failed", "reason_code": "provider_rejected"}
    receipt = pg.execute("SELECT error_code,response_ciphertext FROM creator_handle_requests WHERE id=%s",
                         (rejected["reservation_id"],)).fetchone()
    assert receipt["error_code"] == "http_400"
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(receipt["response_ciphertext"]))
    assert diagnostic["provider_diagnostic"]["message"] == "PRIVATE REJECTION DETAIL"
    assert "PRIVATE REJECTION DETAIL" not in str(pg.execute(
        "SELECT stats FROM creator_handle_jobs WHERE id=%s", (rejected["id"],)).fetchone())
    assert matching.claim() is None and len(flow.calls) == 5


def test_config_change_during_unknown_call_does_not_redispatch_same_material(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    flow.before_response = lambda: flow.enable("sk-rotated-during-call")
    matching.run(job, flow.settings)
    flow.before_response = None
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert len(flow.calls) == 1
    assert pg.execute("SELECT reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "reason_code": "request_unknown",
    }
    # New actual message content is distinct material, not another attempt at the frozen pack.
    original_build = matching.evidence.build

    def changed(*args, **kwargs):
        material = original_build(*args, **kwargs)
        material["pack"]["messages"][0]["text"] += " I confirm it is mine."
        material["fingerprint"] += "-new-message"
        return material

    monkeypatch.setattr(matching.evidence, "build", changed)
    flow.error = None
    fresh = _prepared_job(pg, flow, "incremental")
    matching.run(fresh, flow.settings)
    assert len(flow.calls) == 2


def test_legacy_limits_do_not_block_restart_and_retry_fences(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    # Historical limits must not reject requests or delay them until the next minute/day.
    pg.execute(
        "UPDATE ai_feature_configs SET rpm_limit=1,daily_token_budget=1000,tpm_limit=1000 WHERE feature=%s",
        (matching.FEATURE,),
    )
    job = _prepared_job(pg, flow)
    assert pg.execute("SELECT quota_scope,reserved_tokens FROM creator_handle_requests WHERE id=%s",
                      (job["reservation_id"],)).fetchone() == {
        "quota_scope": None, "reserved_tokens": None,
    }
    pg.execute("UPDATE creator_handle_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (job["id"],))
    assert matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown",
    }
    assert pg.execute("SELECT count(*) n FROM creator_handle_requests WHERE job_id IS NOT NULL").fetchone()["n"] == 1
    matching.run(job, flow.settings)
    assert not flow.calls
    recovered = _prepared_job(pg, flow)  # Only an explicit owner recheck allows another request.
    matching.run(recovered, flow.settings)
    assert len(flow.calls) == 1
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (recovered["id"],)).fetchone()["status"]
        == "completed"
    )
    # Reopen only fixture-generated pairs to exercise provider failure caching for unlinked candidates.
    pg.execute(
        "DELETE FROM contact_social_accounts WHERE contact_id=%s AND source='creator_handle_auto_verified'",
        (flow.contact,),
    )
    flow.error = openai_client.OpenAIRequestError("synthetic transient failure", retryable=True,
                                                telemetry={"http_status": 503})
    flow.material_version += 1
    retry = _prepared_job(pg, flow)
    for attempt in range(1, 4):
        assert retry["attempts"] == attempt
        matching.run(retry, flow.settings)
        ai_dispatch.resume(pg)
        pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (retry["id"],))
        if attempt < 3:
            retry = matching.claim()
    assert matching.claim() is None
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (retry["id"],)).fetchone()["status"]
        == "failed"
    )
    previous_calls = len(flow.calls)
    cached_id = pg.execute("SELECT enqueue_creator_handle(%s,'incremental',TRUE) id", (flow.contact,)).fetchone()["id"]
    assert matching.prepare_next(flow.settings)
    assert matching.claim() is None
    assert len(flow.calls) == previous_calls
    assert (
        pg.execute("SELECT reason_code FROM creator_handle_jobs WHERE id=%s", (cached_id,)).fetchone()["reason_code"]
        == "failure_cached"
    )
    # A new credential may fix a provider failure: do not reuse the old Key's failed request.
    flow.enable("sk-replacement-test-only")
    flow.error = None
    refreshed_id = pg.execute("SELECT enqueue_creator_handle(%s,'incremental',TRUE) id", (flow.contact,)).fetchone()[
        "id"
    ]
    assert matching.prepare_next(flow.settings)
    refreshed = matching.claim()
    assert refreshed is not None and refreshed["id"] == refreshed_id
    matching.run(refreshed, flow.settings)
    assert len(flow.calls) == previous_calls + 1
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (refreshed_id,)).fetchone()["status"]
        == "completed"
    )


def test_review_and_incomplete_evidence_never_auto_link_and_cache_can_reuse(pg, matching_flow):
    flow = matching_flow
    flow.catalog_handles = frozenset()
    flow.enable()
    # A supported answer is not enough if it describes historical ownership or still needs evidence.
    for period, needed, stats, legacy in (
        ("historical", [], {}, False),
        ("at_message", ["recent_state"], {}, False),
        ("at_message", [], {"history_truncated": True}, True),
        ("at_message", [], {"candidates_omitted": 1}, True),
        ("at_message", [], {"critical_evidence_omitted": True}, True),
    ):
        flow.period, flow.needed, flow.stats, flow.legacy_stats = period, needed, stats, legacy
        flow.material_version += 1
        matching.run(_prepared_job(pg, flow), flow.settings)
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    for relation in ("operator", "account_creator", "other_person"):
        flow.relation, flow.period, flow.needed, flow.stats, flow.legacy_stats = relation, "at_message", [], {}, False
        flow.material_version += 1
        matching.run(_prepared_job(pg, flow), flow.settings)
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # Typed coverage limits only the affected handle; other strong claims are not collateral damage.
    flow.relation = "self_account"
    flow.period, flow.needed, flow.legacy_stats = "at_message", [], False
    flow.stats = {
        "history_truncated": True,
        "candidates_omitted": 1,
        "critical_evidence_omitted": True,
        "critical_evidence_omitted_handles": ["backup_2"],
    }
    # This message independently names its own account: an unrelated unavailable reply is unnecessary.
    flow.missing_reply_ids = ["M1"]
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    # Both model claims are strong, but only the backup account has a critical evidence gap.
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    original_audit = pg.execute(
        "SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'"
    ).fetchone()
    assert original_audit["metadata"]["evidence_refs"] == [{"kind": "message", "label": "M1", "synthetic_version": 1}]
    flow.reference_version += 1
    assert _enqueue(pg, flow.contact) == job["id"]
    assert matching.prepare_next(flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "review_required",
        "reason_code": "cached",
    }
    previous_calls = len(flow.calls)
    assert matching.claim() is None
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 1
    )
    assert (
        pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
        == original_audit
    )
    assert pg.execute("SELECT refs FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["refs"] == [
        {"kind": "message", "label": label, "synthetic_version": 2} for label in ("M1", "M2")
    ]
    # Legacy snapshots without per-handle coverage retain conservative behavior, including cache reuse.
    flow.legacy_stats, flow.missing_reply_ids = True, []
    flow.stats = {"critical_evidence_omitted": True}
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert matching.claim() is None and len(flow.calls) == previous_calls + 1
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]


def test_auto_link_preserves_rejections_non_owner_links_and_other_account_claims(pg, matching_flow, pilot):
    flow = matching_flow
    flow.catalog_handles = frozenset()
    flow.enable()
    accounts = {handle: uuid4() for handle in ("first.handle", "backup_2")}
    for handle, account in accounts.items():
        pg.execute(
            "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok',%s,%s)",
            (account, handle, handle),
        )
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,'owner','rejected','manual'),(%s,%s,'representative','confirmed','manual')",
        (flow.contact, accounts["first.handle"], flow.contact, accounts["backup_2"]),
    )
    before = _snapshot(pg, ("contact_social_accounts", "creator_account_assignments"))
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert _snapshot(pg, before.keys()) == before
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # The alternate non-owner relation must be protected on the cached-result path too.
    pg.execute(
        "UPDATE contact_social_accounts SET relationship='associated' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, accounts["backup_2"]),
    )
    cached = pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    view = json.loads(creator_data.cipher(flow.settings).decrypt(cached["result_ciphertext"]))
    for link in view["result"]["links"]:
        link.update(decision="supported", relation="self_account", code="self_claim")
    pg.execute(
        "UPDATE creator_handle_jobs SET result_ciphertext=%s WHERE id=%s",
        (creator_data.cipher(flow.settings).encrypt(creator_data.encode(view)), job["id"]),
    )
    before = _snapshot(pg, before.keys())
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert _snapshot(pg, before.keys()) == before
    # Replace only fixture-created blockers with another person's confirmed account relations.
    pg.execute("DELETE FROM contact_social_accounts WHERE contact_id=%s", (flow.contact,))
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,'owner','confirmed','manual')",
        (pilot.ids["legacy"], accounts["first.handle"]),
    )
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,'associated','confirmed','manual')", (pilot.ids["legacy"], accounts["backup_2"]),
    )
    before = _snapshot(pg, before.keys())
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert _snapshot(pg, before.keys()) == before
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()

    other = _enqueue(pg, pilot.ids["legacy"])
    pg.execute("UPDATE creator_handle_jobs SET status='review_required',result_ciphertext=%s,"
               "stats=jsonb_build_object('claimed_handles',jsonb_build_array('first.handle','backup_2')) WHERE id=%s",
               (creator_data.cipher(flow.settings).encrypt(creator_data.encode(view)), other))
    queries = []

    class Meter:
        def execute(self, sql, *args, **kwargs):
            queries.append(sql)
            return pg.execute(sql, *args, **kwargs)

    displayed = matching.page_data(Meter(), pilot.ids["owner"], flow.settings)["jobs"]
    target = next(row for row in displayed if row["id"] == job["id"])
    assert all(link["decision"] == "review" and link["conflict"] for link in target["suggestions"])
    assert sum("AS conflict FROM unnest" in sql for sql in queries) == 1
    assert sum("JOIN unnest(%s::text[]) h(handle)" in sql for sql in queries) == 1


def test_role_and_chat_events_shared_slots_and_catalog_changes(pg, matching_flow, pilot, monkeypatch, tmp_path):
    flow = matching_flow
    (tmp_path / "handles.csv").write_text("handle\nfirst.handle\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"files": [{"name": "handles.csv", "sha256": "0" * 64}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        matching._load_catalog(str(tmp_path), 0)  # Optional reference must not weaken archive integrity.
    with monkeypatch.context() as reference:
        reference.setattr(matching, "catalog", lambda: matching._load_catalog(str(tmp_path), 0))
        assert matching.reference_catalog() == (frozenset(), "unavailable")
    flow.enable()
    # Role confirmation from any SQL writer is durable in the same transaction.
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (flow.contact,))
    pg.execute("UPDATE contact_roles SET status='confirmed' WHERE contact_id=%s", (flow.contact,))
    job_id = pg.execute("SELECT id FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,)).fetchone()["id"]
    pg.execute("UPDATE chats SET name='first.handle & TT Media' WHERE chat_id='chat_creator'")
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,)).fetchone()["n"]
        == 1
    )
    pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (job_id,))
    assert matching.prepare_next(flow.settings)
    # Matching leases consume the shared admission counter, just as summary/reply leases do.
    for who in ("legacy", "silent", "cm", "owner"):
        pg.execute(
            """INSERT INTO creator_handle_jobs(contact_id,status,lease_until)
            VALUES(%s,'running',NOW()+INTERVAL '2 minutes')""",
            (pilot.ids[who],),
        )
    assert ai_dispatch.can_claim(pg)  # Four leases still leave one of the five shared slots free.
    pg.execute("SAVEPOINT admission_slots")
    fifth = pilot.person("fifth_slot")
    pg.execute(
        "UPDATE creator_handle_jobs SET status='running',lease_until=NOW()+INTERVAL '2 minutes' WHERE contact_id=%s",
        (fifth,),
    )
    assert matching.claim() is None
    pg.execute("ROLLBACK TO SAVEPOINT admission_slots")
    pg.execute("UPDATE creator_handle_jobs SET status='completed' WHERE id<>%s", (job_id,))
    job = matching.claim()
    assert job is not None
    # Curva changes do not invalidate real chat evidence before or after the model call.
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v2"))
    flow.before_response = lambda: monkeypatch.setattr(
        matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v3")
    )
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1
    finished = pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone()
    assert finished["status"] == "completed"
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert set(contact_handles.for_contacts(pg, [flow.contact])[flow.contact]) == {"first.handle", "backup_2"}
    displayed = matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"]
    assert any(item["id"] == job_id and item["suggestions"] for item in displayed)
    pg.execute("UPDATE ai_feature_configs SET automatic_queue_checked_at=NOW()-INTERVAL '2 minutes'")
    before_jobs = pg.execute("SELECT COUNT(*) n FROM creator_handle_jobs").fetchone()["n"]
    assert not matching.prepare_next(flow.settings)
    assert pg.execute("SELECT COUNT(*) n FROM creator_handle_jobs").fetchone()["n"] == before_jobs

    # History admission upgrades unlinked prior-version contacts, never existing formal owners.
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v4"))
    pg.execute(
        "UPDATE creator_handle_jobs SET stats=jsonb_build_object('prompt_version','retired-prompt'),"
        "created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s",
        (pilot.ids["legacy"],),
    )
    pg.execute(
        "UPDATE creator_handle_jobs SET stats=jsonb_build_object('prompt_version','retired-prompt'),"
        "created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s",
        (pilot.ids["silent"],),
    )
    old_id = pg.execute("SELECT id FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["silent"],)).fetchone()[
        "id"
    ]
    assert matching.enqueue_history(pg, pilot.ids["owner"]) == 1
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["legacy"],)).fetchone()[
            "n"
        ]
        == 1
    )
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["silent"],)).fetchone()[
            "n"
        ]
        == 2
    )
    latest = matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"]
    assert len({row["contact_id"] for row in latest}) == len(latest)
    assert old_id not in {row["id"] for row in latest}
    # Bulk history admission skips formal owners and preserves explicit manual review.
    pg.execute(
        "UPDATE creator_handle_jobs SET status='review_required',source='manual',"
        "stats=jsonb_build_object('prompt_version','retired-prompt') WHERE id=%s",
        (job_id,),
    )
    query = "SELECT status,reason_code,source FROM creator_handle_jobs WHERE id=%s"
    manual = pg.execute(query, (job_id,)).fetchone()
    assert matching.enqueue_history(pg, pilot.ids["owner"]) == 0
    assert matching.prepare_next(flow.settings)  # Only the earlier, unlinked silent history item.
    assert pg.execute(query, (job_id,)).fetchone() == manual
    assert len(flow.calls) == 1


def test_failed_diagnostics_partial_results_and_queue_cleanup_preserve_data(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    respond = matching.api.call_openai
    secret = "synthetic private message must never enter diagnostics"
    broken = True

    def reply(*args, **kwargs):
        response = respond(*args, **kwargs)
        value = json.loads(response["text"])
        value["links"][1]["refs"] = [secret]
        if broken:
            value["links"][1]["h"] = "H1"
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", reply)
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"] == "failed"
    )
    logged = pg.execute("SELECT * FROM creator_handle_requests WHERE job_id=%s", (job["id"],)).fetchone()
    assert logged["error_code"] == "candidates"
    assert (logged["input_tokens"], logged["output_tokens"], logged["billed_tokens"]) == (100, 50, 150)
    assert logged["response_id"] == "resp_synthetic" and logged["request_id"] == "req_synthetic"
    assert secret not in creator_data.cipher(flow.settings).decrypt(logged["response_ciphertext"])
    broken = False
    flow.material_version += 1
    partial = _prepared_job(pg, flow)
    matching.run(partial, flow.settings)
    row = pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (partial["id"],)
    ).fetchone()
    assert row["status"] == "review_required"
    result = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))["result"]
    assert [link["decision"] for link in result["links"]] == ["supported", "review"]
    assert result["links"][1]["validation_error"] == "references"
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    assert not matching.claim()
    # The worker can safely re-use a downgraded cached result without another paid call.
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 2
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_social_accounts", "creator_account_assignments"))
    pg.execute("DELETE FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,))
    assert _snapshot(pg, before.keys()) == before
    retained = pg.execute(
        "SELECT count(*) n,sum(billed_tokens) tokens FROM creator_handle_requests WHERE job_id IS NULL"
    ).fetchone()
    assert retained == {"n": 2, "tokens": 300}


def test_fixed_group_context_auto_links_with_live_scope_and_auditable_title(pg, matching_flow, pilot, monkeypatch):
    flow = matching_flow
    flow.catalog_handles = frozenset()  # A dedicated group can prove an account absent from Curva.
    def unavailable_catalog():
        raise OSError("synthetic reference unavailable")

    monkeypatch.setattr(matching, "catalog", unavailable_catalog)
    flow.enable()
    monkeypatch.setattr(matching.evidence, "build", flow.real_build)
    monkeypatch.setattr(matching.evidence, "still_valid", flow.real_still_valid)
    title = "❤️ first.handle X TT Media"
    body = "My account was accessed in US. Now all my shop links are gone."
    cipher = message_body_cipher(flow.settings.token_encryption_key)
    pg.execute("UPDATE chats SET name=%s,chat_mode='group' WHERE chat_id='chat_creator'", (title,))
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat_creator'",
        (cipher.encrypt(json.dumps({"text": "Thanks for the update"})),),
    )
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s,message_create_time=NOW() "
        "WHERE chat_id='chat_creator' AND message_id='creator_0_question'",
        (cipher.encrypt(json.dumps({"text": body})),),
    )
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"))
    respond = matching.api.call_openai

    def contextual_response(*args, **kwargs):
        response = respond(*args, **kwargs)
        pack, value = args[2], json.loads(response["text"])
        reference = next(m["id"] for m in pack["messages"] if m["speaker"] == pack["target"] and m["text"] == body)
        for link in value["links"]:
            link.update(code="group_context", refs=[reference])
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", contextual_response)
    pg.execute("SAVEPOINT ordinary_group_name")
    pg.execute("UPDATE chats SET name='MAC Creators' WHERE chat_id='chat_creator'")
    ordinary = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles)
    assert ordinary["reason"] == "no_candidates" and ordinary["pack"] is None
    pg.execute("ROLLBACK TO SAVEPOINT ordinary_group_name")
    material = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles)
    assert material["reason"] == "ready" and [h["handle"] for h in material["pack"]["handles"]] == ["first.handle"]
    assert material["pack"]["handles"][0]["catalog_verified"] is False
    assert all("first.handle" not in m["text"] for m in material["pack"]["messages"])

    # A title edited during a paid response invalidates the old context, but keeps the usage ledger.
    job = _prepared_job(pg, flow, "incremental")
    flow.before_response = lambda: pg.execute(
        "UPDATE chats SET name='Other collaboration' WHERE chat_id='chat_creator'"
    )
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone() == {"status": "queued", "result_ciphertext": None}
    assert pg.execute(
        "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
    ).fetchone()["billed_tokens"] == 150
    flow.before_response = None
    pg.execute("UPDATE chats SET name=%s WHERE chat_id='chat_creator'", (title,))

    # Explicit review still checks the actual grant before calling or creating a relation.
    job = _prepared_job(pg, flow, "manual")
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (pilot.ids["cm"],))
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert pg.execute(
        "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
    ).fetchone()["billed_tokens"] == 0
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (pilot.ids["cm"],))

    _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    job = pg.execute('SELECT * FROM creator_handle_jobs WHERE id=%s', (job['id'],)).fetchone()
    job['pack'] = json.loads(creator_data.cipher(flow.settings).decrypt(job['pack_ciphertext']))
    assert job['reason_code'] == 'cached' and len(flow.calls) == 1
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    saved = pg.execute("SELECT status,stats FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    assert saved["status"] == "completed" and saved["stats"]["auto_linked_handles"] == ["first.handle"]
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()[
        "metadata"
    ]
    reference = next(m["id"] for m in job["pack"]["messages"] if m["text"] == body)
    chat_ref = next(ref for ref in job["refs"] if ref["kind"] == "chat" and ref["chat_id"] == "chat_creator")
    assert audit["refs"] == [reference] and audit["prompt_version"] == matching.evidence.PROMPT_VERSION
    assert chat_ref in audit["evidence_refs"]
    assert any(ref["kind"] == "message" and ref["label"] == reference for ref in audit["evidence_refs"])
    assert all("text" not in ref and "body" not in ref for ref in audit["evidence_refs"])
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"])
    assert [item["handle"] for item in row["handle_rows"]] == ["first.handle"]
    assert row["handle_rows"][0]["associated"] and row["handle_rows"][0]["check"] is None
    assert row["suggestions"][0]["handle"] == "first.handle" and row["suggestions"][0]["decision"] == "supported"
    assert _snapshot(pg, before.keys()) == before

    # The same contextual signal from another person is a claimant conflict, not a second owner.
    pg.execute("SAVEPOINT group_competing_owner")
    pilot.person("group_peer")
    pg.execute(
        "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,body_ciphertext,"
        "message_create_time) VALUES('chat_creator','group_peer_control','ou_group_peer','user','text',%s,NOW())",
        (cipher.encrypt(json.dumps({"text": "My account login has a 2FA problem"})),),
    )
    pg.execute(
        "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
        "VALUES(%s,'chat_creator','group_peer_control')",
        (pilot.ids["cm"],),
    )
    competing = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles, recheck=True)
    reference = next(m["id"] for m in competing["pack"]["messages"] if m["text"] == body)
    result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": competing["pack"]["handles"][0]["id"], "decision": "supported", "relation": "self_account",
         "period": "at_message", "code": "group_context", "refs": [reference], "needed": []}]}
    reviewed = matching.evidence.validate(competing["pack"], result)["links"][0]
    assert reviewed == result["links"][0]  # Competing chat text is judged by AI, not backend regex.
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    pg.execute("ROLLBACK TO SAVEPOINT group_competing_owner")


def test_formal_pairs_skip_paid_matching_without_freezing_new_handles_or_manual_review(
    pg, matching_flow, pilot, monkeypatch
):
    flow = matching_flow
    flow.enable()
    monkeypatch.setattr(matching.evidence, "build", flow.real_build)
    monkeypatch.setattr(matching.evidence, "still_valid", flow.real_still_valid)
    respond = matching.api.call_openai

    def real_references(*args, **kwargs):
        response = respond(*args, **kwargs)
        pack = args[2]
        value = json.loads(response["text"])
        by_handle = {h["id"]: h["handle"] for h in pack["handles"]}
        for link in value["links"]:
            link["refs"] = [
                next(
                    m["id"]
                    for m in pack["messages"]
                    if m["speaker"] == pack["target"] and by_handle[link["h"]] in m["text"]
                )
            ]
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", real_references)
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"))
    cipher = message_body_cipher(flow.settings.token_encryption_key)
    start = datetime.now(UTC)

    def message(handle, minute, body=None):
        message_id = f"{handle}-{minute}"
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
            "body_ciphertext,message_create_time) VALUES('chat_creator',%s,'ou_creator','user','text',%s,%s)",
            (
                message_id,
                cipher.encrypt(json.dumps({"text": body or "My TikTok account is " + handle})),
                start + timedelta(minutes=minute),
            ),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
            "VALUES(%s,'chat_creator',%s)",
            (pilot.ids["cm"], message_id),
        )

    first = uuid4()
    pg.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
        "VALUES(%s,'tiktok','first.handle','first.handle')",
        (first,),
    )
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,"
        "reviewed_by_authorization_id,reviewed_at) "
        "VALUES(%s,%s,'owner','confirmed','owner_reviewed_curva',%s,NOW())",
        (flow.contact, first, pilot.ids["owner"]),
    )
    message("first.handle", 0)
    message("backup_2", 1)
    existing_pairs = _snapshot(pg, ("contact_social_accounts",))
    all_confirmed = _enqueue(pg, flow.contact, "history")
    with monkeypatch.context() as no_material:
        no_material.setattr(matching.evidence, "build",
                            lambda *_a, **_k: pytest.fail("Linked history needs no material"))
        assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute(
        "SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (all_confirmed,)
    ).fetchone() == {"status": "completed", "reason_code": "already_confirmed"}
    assert not flow.calls and not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
    page = matching.page_data(pg, pilot.ids["owner"], flow.settings)
    assert page["jobs"][0]["associations"] == [{"handle": "first.handle", "human_confirmed": True}]
    assert page["jobs"][0]["suggestions"] == []
    assert {r["status"]: r["n"] for r in page["counts"]} == {"completed": 1}
    assert _snapshot(pg, existing_pairs.keys()) == existing_pairs

    # A formal association gained after preparation/admission closes history before networking.
    for after_claim in (False, True):
        pg.execute("SAVEPOINT late_formal")
        pg.execute("UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s", (flow.contact,))
        late = _enqueue(pg, flow.contact, "history")
        assert matching.prepare_next(flow.settings)
        running = matching.claim() if after_claim else None
        pg.execute("UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s", (flow.contact,))
        if running:
            matching.run(running, flow.settings)
            assert pg.execute("SELECT billed_tokens FROM creator_handle_requests WHERE id=%s",
                              (running["reservation_id"],)).fetchone()["billed_tokens"] == 0
        else:
            assert matching.claim() is None
            assert not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
        assert not flow.calls
        assert pg.execute("SELECT status,reason_code,claim_token,lease_until FROM creator_handle_jobs WHERE id=%s",
                          (late,)).fetchone() == {"status": "completed", "reason_code": "already_confirmed",
                                                "claim_token": None, "lease_until": None}
        pg.execute("ROLLBACK TO SAVEPOINT late_formal")

    # The formal exact pair is excluded, not the entire contact.
    job = _prepared_job(pg, flow, "incremental")
    assert [h["handle"] for h in job["pack"]["handles"]] == ["backup_2"]
    assert job["stats"]["prompt_version"] == matching.evidence.PROMPT_VERSION
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == [
        "backup_2",
        "first.handle",
    ]
    pairs = {row["handle"]: row for row in contact_handles.confirmed_pairs(pg, [flow.contact])}
    assert pairs["first.handle"]["human_confirmed"] and not pairs["backup_2"]["human_confirmed"]
    assert pairs["backup_2"]["source"] == "creator_handle_auto_verified"
    pg.execute(
        "UPDATE contact_social_accounts SET source='excel_ai_verified',reviewed_by_authorization_id=%s,"
        "reviewed_at=NOW() WHERE contact_id=%s AND social_account_id=(SELECT id FROM social_accounts "
        "WHERE platform='tiktok' AND normalized_handle='backup_2')",
        (pilot.ids["owner"], flow.contact),
    )
    assert not next(row for row in contact_handles.confirmed_pairs(pg, [flow.contact]) if row["handle"] == "backup_2")[
        "human_confirmed"
    ]
    assert flow.real_build(pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}))["reason"] == (
        "already_confirmed"
    )
    assert {
        h["handle"]
        for h in flow.real_build(
            pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}), recheck=True
        )["pack"]["handles"]
    } == {"first.handle", "backup_2"}
    pg.execute(
        "UPDATE contact_social_accounts SET source='creator_handle_auto_verified' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    assert not next(row for row in contact_handles.confirmed_pairs(pg, [flow.contact]) if row["handle"] == "backup_2")[
        "human_confirmed"
    ]
    pairs_before = _snapshot(pg, ("contact_social_accounts",))
    automatic = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (automatic,)).fetchone() == {
        "status": "completed",
        "reason_code": "already_confirmed",
    }
    # Skipping does not invent a human review or rewrite source.
    assert _snapshot(pg, pairs_before.keys()) == pairs_before
    pg.execute(
        "UPDATE creator_handle_jobs SET created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s", (flow.contact,)
    )

    # Explicit manual review includes existing pairs, but the DB does not add duplicate links or audit events.
    job = _prepared_job(pg, flow)
    assert {h["handle"] for h in job["pack"]["handles"]} == {"first.handle", "backup_2"}
    assert job["stats"]["manual_recheck"] and set(job["stats"]["recheck_handles"]) == {"first.handle", "backup_2"}
    matching.run(job, flow.settings)
    assert len(flow.calls) == 2 and _snapshot(pg, pairs_before.keys()) == pairs_before
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 1
    )

    # Formal relationships and this round's AI conclusion are independent page fields.
    saved = pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    view = json.loads(creator_data.cipher(flow.settings).decrypt(saved["result_ciphertext"]))
    link = view["result"]["links"][0]
    link.update(
        decision="review", relation="uncertain", period="unknown", code="no_link_evidence", needed=["account_context"]
    )
    for code, validation_error, conflict in (
        ("no_link_evidence", None, False),
        ("conflict", None, False),
        ("no_link_evidence", "counterevidence", False),
    ):
        link["code"] = code
        if validation_error:
            link["validation_error"] = validation_error
        else:
            link.pop("validation_error", None)
        pg.execute(
            "UPDATE creator_handle_jobs SET result_ciphertext=%s,status='review_required' WHERE id=%s",
            (creator_data.cipher(flow.settings).encrypt(creator_data.encode(view)), job["id"]),
        )
        row = next(
            row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
        )
        assert {p["handle"]: p["human_confirmed"] for p in row["associations"]} == {
            "first.handle": True,
            "backup_2": False,
        }
        assert row["suggestions"][0]["decision"] == "review" and row["suggestions"][0]["conflict"] is conflict
        assert "associated" not in row["suggestions"][0]
        assert len(row["handle_rows"]) == 2
        assert {item["handle"] for item in row["handle_rows"]} == {"first.handle", "backup_2"}
        assert all(item["associated"] and item["check"] is not None for item in row["handle_rows"])
        assert row["display_status"] == "review_required"  # Explicit manual review is never presented as completed.

    # Historical weak reproof is hidden on the existing handle row, not duplicated or shown as a false conflict.
    pg.execute("UPDATE creator_handle_jobs SET source='history' WHERE id=%s", (job["id"],))
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
    )
    assert len(row["handle_rows"]) == 2 and all(
        item["associated"] and item["check"] is None and not item["conflict"] for item in row["handle_rows"]
    )
    assert row["display_status"] == "completed" and row["display_reason_code"] == "already_confirmed"
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"] == (
        "review_required"
    )  # Derived presentation does not rewrite the paid historical outcome.
    # Opening a history page does not reinterpret denial keywords as a verified database conflict.
    pg.execute("SAVEPOINT actual_counter")
    message("first.handle", 3, "first.handle is not my account")
    material = flow.real_build(pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}), recheck=True)
    denial = next(m["id"] for m in material["pack"]["messages"] if "not my account" in m["text"])
    counter_view = {
        "handles": {h["id"]: h["handle"] for h in material["pack"]["handles"]},
        "result": {
            "creator": {"state": "known", "refs": []},
            "links": [
                {
                    "h": h["id"],
                    "decision": "review",
                    "relation": "uncertain",
                    "period": "unknown",
                    "code": "no_link_evidence",
                    "refs": [denial] if h["handle"] == "first.handle" else [],
                    "needed": ["account_context"],
                }
                for h in material["pack"]["handles"]
            ],
        },
    }
    pg.execute(
        "UPDATE creator_handle_jobs SET pack_ciphertext=%s,result_ciphertext=%s,refs=%s::jsonb WHERE id=%s",
        (
            creator_data.cipher(flow.settings).encrypt(creator_data.encode(material["pack"])),
            creator_data.cipher(flow.settings).encrypt(creator_data.encode(counter_view)),
            creator_data.encode(material["refs"]),
            job["id"],
        ),
    )
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
    )
    assert not next(item for item in row["handle_rows"] if item["handle"] == "first.handle")["conflict"]
    assert next(item for item in row["handle_rows"] if item["handle"] == "backup_2")["check"] is None
    pg.execute("ROLLBACK TO SAVEPOINT actual_counter")

    # Any formal pair gained before admission invalidates the automatic snapshot before billing.
    message("backup_2", 2)
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    job_id = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings)
    pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    assert matching.claim() is None and len(flow.calls) == 2
    assert pg.execute("SELECT count(*) n FROM creator_handle_requests").fetchone()["n"] == 2
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone() == {
        "status": "completed",
        "reason_code": "already_confirmed",
    }

    # Removing a formal pair after admission is fenced before networking, even when it is another handle.
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    ready = _prepared_job(pg, flow, "incremental")
    assert [h["handle"] for h in ready["pack"]["handles"]] == ["backup_2"]
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, first),
    )
    matching.run(ready, flow.settings)
    assert len(flow.calls) == 2
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (ready["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 0
    )
    pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, first),
    )
    # The same full-set fence after networking retains usage without publishing a stale result.
    after_network = _prepared_job(pg, flow, "incremental")
    flow.before_response = lambda: pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    matching.run(after_network, flow.settings)
    assert len(flow.calls) == 3
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (after_network["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)
    ).fetchone() == {"status": "queued", "result_ciphertext": None}
    assert _snapshot(pg, pairs_before.keys()) == pairs_before
    assert _snapshot(pg, before.keys()) == before
    # An explicitly restricted contact stops at waiting_evidence, not an endless snapshot-changed requeue.
    monkeypatch.setattr(matching.evidence, "RESTRICTED_CONTACTS", frozenset({str(flow.contact)}))
    assert matching.prepare_next(flow.settings)
    assert pg.execute(
        "SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)
    ).fetchone() == {"status": "waiting_evidence", "reason_code": "restricted"}
    assert not matching.prepare_next(flow.settings) and len(flow.calls) == 3
