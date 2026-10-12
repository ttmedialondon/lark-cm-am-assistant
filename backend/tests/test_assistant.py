import json
from contextlib import nullcontext
from uuid import uuid4

import pytest
from test_offhours_summary import seeded as seeded  # noqa: F401

from app import assistant, config, openai_client, summary_dialogues
from app.ai_feature_config import api_key_cipher


@pytest.fixture
def chat(pg, seeded, monkeypatch):  # noqa: F811
    monkeypatch.setattr(assistant, 'connection', lambda: nullcontext(pg))
    monkeypatch.setattr(config, 'get_settings', lambda: seeded.settings)
    pg.execute('''UPDATE ai_feature_configs SET enabled=TRUE,project_id='fixture-project',
        api_key_ciphertext=%s,key_suffix='test' WHERE feature='operations_assistant' ''',
        (api_key_cipher(seeded.settings.token_encryption_key).encrypt('fixture-only-key'),))
    return seeded


def payload(message='请帮我找一下 alice'):
    return {'session_id': str(uuid4()), 'client_request_id': str(uuid4()), 'message': message}


def finish(chat):
    job = assistant.claim()
    assert job is not None
    assistant.run(job, chat.settings)
    return job


def test_model_first_lookup_replay_history_and_actor_isolation(pg, chat, monkeypatch):
    calls = []
    owner_scopes = assistant.tools.scopes_for(pg, chat.ids['owner'])
    assert owner_scopes['can_switch_cm'] and owner_scopes['selected_cm_id'] == str(chat.ids['owner'])
    assert {p['cm_id'] for p in owner_scopes['scopes']} == {
        str(chat.ids[name]) for name in ('owner', 'cm', 'reviewer', 'tester')}
    own_scopes = assistant.tools.scopes_for(pg, chat.ids['cm'])
    assert own_scopes == {'can_switch_cm': False, 'selected_cm_id': str(chat.ids['cm']),
                         'scopes': [{'cm_id': str(chat.ids['cm']), 'cm_name': 'cm'}]}
    assert assistant.tools.current_context(pg, chat.ids['tester'], {}, chat.settings)['scope']['cm_id'] == str(
        chat.ids['tester'])
    for candidate in owner_scopes['scopes']:
        assert assistant.tools.current_context(pg, chat.ids['owner'], {
            'cm_id': candidate['cm_id'], 'audience': 'all'}, chat.settings)['scope']['cm_id'] == candidate['cm_id']
    with pytest.raises(PermissionError):
        assistant.tools.current_context(pg, chat.ids['cm'], {'cm_id': str(chat.ids['owner'])}, chat.settings)
    pg.execute('UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s', (chat.ids['owner'],))
    assert not assistant.tools.scopes_for(pg, chat.ids['owner'])['can_switch_cm']
    with pytest.raises(PermissionError):
        assistant.tools.current_context(pg, chat.ids['owner'], {'cm_id': str(chat.ids['cm'])}, chat.settings)
    pg.execute('UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s', (chat.ids['owner'],))

    def model(cfg, key, items, actor, **kwargs):
        calls.append(items.copy())
        assert cfg['model'] == 'gpt-6-luna' and key == 'fixture-only-key' and actor == chat.ids['cm']
        assert kwargs['tools'] == assistant.tools.TOOLS
        if not any(item.get('type') == 'function_call_output' for item in items):
            output = [{'type': 'reasoning', 'id': 'rs_fixture', 'encrypted_content': 'encrypted-reasoning'},
                      {'type': 'function_call', 'call_id': 'find_fixture', 'name': 'find_creators',
                       'arguments': json.dumps({'query': 'alice'})}]
            return {'output': output, 'text': '', 'response_id': 'resp_find',
                    'usage': {'input_tokens': 10, 'output_tokens': 5}}
        answer = {'answer': '找到 @alice。', 'event_refs': [], 'result_refs': [], 'draft_ref': None}
        return {'output': [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': json.dumps(answer)}]}], 'text': json.dumps(answer),
            'response_id': 'resp_answer', 'usage': {'input_tokens': 12, 'output_tokens': 4}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    submitted = payload()
    turn = assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    assert turn['status'] == 'queued' and calls == []
    assert assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)['id'] == turn['id']
    with pytest.raises(LookupError):
        assistant.session_view(pg, chat.ids['reviewer'], submitted['session_id'], chat.settings)
    with pytest.raises(ValueError):
        assistant.submit(pg, chat.ids['cm'], dict(submitted, message='别的问题'), chat.settings)
    finish(chat)
    assert len(calls) == 1
    finish(chat)  # SQL tool execution does not add a model call.
    assert len(calls) == 1
    finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['result']['answer'] == '找到 @alice。'
    assert len(calls) == 2 and any(item.get('encrypted_content') for item in calls[1])
    assert pg.execute('SELECT usage FROM assistant_turns').fetchone()['usage'] == {
        'input_tokens': 22, 'output_tokens': 9,
    }
    assert assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)['status'] == 'completed'
    assert len(calls) == 2
    session = assistant.session_view(pg, chat.ids['cm'], submitted['session_id'], chat.settings)
    assert len(session['turns']) == 1
    assert 'alice' not in pg.execute('SELECT input_ciphertext FROM assistant_turns').fetchone()['input_ciphertext']
    checkpoint = summary_dialogues.decode(pg.execute('SELECT checkpoint_ciphertext FROM assistant_turns').fetchone()[
        'checkpoint_ciphertext'], chat.settings)
    old_ref = next(result['query_ref'] for result in checkpoint['tool_results'] if result.get('query_ref'))
    untouched = {table: pg.execute(f'SELECT count(*) n FROM {table}').fetchone()['n']
                 for table in ('chat_messages', 'contacts', 'contact_assignments',
                               'authorization_message_observations')}
    other = assistant.submit(pg, chat.ids['reviewer'], payload('Other actor question'), chat.settings)
    assert assistant.clear_conversations(pg, chat.ids['cm'], chat.settings) == {'sessions': 1, 'turns': 1}
    assert assistant.session_view(pg, chat.ids['cm'], submitted['session_id'], chat.settings)['turns'] == []
    with pytest.raises(LookupError):
        assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    with pytest.raises(LookupError):
        assistant.tools._saved_query(pg, chat.ids['cm'], old_ref, chat.settings)
    with pytest.raises(ValueError, match='已清空'):
        assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    receipt = pg.execute('SELECT usage,response_id,result_ciphertext,checkpoint_ciphertext FROM assistant_turns '
                         'WHERE id=%s', (turn['id'],)).fetchone()
    assert receipt['usage'] == {'input_tokens': 22, 'output_tokens': 9} and receipt['result_ciphertext'] is None
    assert summary_dialogues.decode(receipt['checkpoint_ciphertext'], chat.settings) == {}
    assert assistant.turn_view(pg, chat.ids['reviewer'], other['id'], chat.settings)['status'] == 'queued'
    assert all(pg.execute(f'SELECT count(*) n FROM {table}').fetchone()['n'] == count
               for table, count in untouched.items()) and len(calls) == 2
    pg.execute('UPDATE authorizations SET status=\'revoked\' WHERE id=%s', (chat.ids['cm'],))
    with pytest.raises(PermissionError):
        assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)


def test_unknown_and_stopped_requests_never_replay_provider(pg, chat, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(True)
        raise openai_client.OpenAIRequestError('private transport', retryable=True,
                                               telemetry={'response_latency_ms': 500})

    monkeypatch.setattr(openai_client, 'call_openai_tools', timeout)
    submitted = payload()
    first = assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    finish(chat)
    assert assistant.turn_view(pg, chat.ids['cm'], first['id'], chat.settings)['status'] == 'unknown'
    assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    assert assistant.claim() is None and len(calls) == 1
    stopped = assistant.submit(pg, chat.ids['cm'], payload('取消这个请求'), chat.settings)
    job = assistant.claim()
    with pytest.raises(ValueError, match='正在处理'):
        assistant.clear_conversations(pg, chat.ids['cm'], chat.settings)
    assert pg.execute('SELECT status FROM assistant_turns WHERE id=%s',
                      (stopped['id'],)).fetchone()['status'] == 'running'
    assistant.cancel(pg, chat.ids['cm'], stopped['id'], chat.settings)
    assistant.run(job, chat.settings)
    assert len(calls) == 1
    assert assistant.turn_view(pg, chat.ids['cm'], stopped['id'], chat.settings)['status'] == 'cancelled'


def test_waiting_releases_slot_and_resume_returns_original_call_id(pg, chat, monkeypatch):
    calls = []
    complete = False

    def model(_cfg, _key, items, _actor, **kwargs):
        calls.append(items.copy())
        return {'text': '', 'output': [{'type': 'function_call', 'call_id': 'original_call',
                'name': 'summarize_messages', 'arguments': '{}'}], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    monkeypatch.setattr(assistant.tools, 'execute', lambda *args: {
        'waiting': {'kind': 'summary', 'ids': []},
    })
    monkeypatch.setattr(assistant.tools, 'resume_wait', lambda *args: {'events': []} if complete else None)
    turn = assistant.submit(pg, chat.ids['cm'], payload('总结今天上午'), chat.settings)
    finish(chat)
    finish(chat)
    row = pg.execute('SELECT * FROM assistant_turns').fetchone()
    assert row['status'] == 'waiting' and row['lease_until'] is None
    assert assistant.claim() is None and not assistant.prepare(chat.settings) and len(calls) == 1
    complete = True
    pg.execute('UPDATE assistant_turns SET scheduled_at=NOW()')
    assert assistant.prepare(chat.settings)
    row = pg.execute('SELECT * FROM assistant_turns').fetchone()
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    assert checkpoint['items'][-1]['type'] == 'function_call_output'
    assert checkpoint['items'][-1]['call_id'] == 'original_call'
    assert checkpoint['pending_calls'] == [] and row['status'] == 'queued'
    assistant.cancel(pg, chat.ids['cm'], turn['id'], chat.settings)


def test_expired_provider_lease_is_unknown_not_a_new_paid_call(pg, chat):
    turn = assistant.submit(pg, chat.ids['cm'], payload(), chat.settings)
    row = assistant.claim()
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    checkpoint['in_flight'] = True
    pg.execute('''UPDATE assistant_turns SET checkpoint_ciphertext=%s,lease_until=NOW()-INTERVAL '1 minute'
        WHERE id=%s''', (summary_dialogues.encode(checkpoint, chat.settings), turn['id']))
    assert assistant.claim() is None
    assert pg.execute('SELECT status FROM assistant_turns').fetchone()['status'] == 'unknown'


def test_repeated_tool_call_uses_current_turn_cache(pg, chat, monkeypatch):
    executions = []
    original = assistant.tools.execute

    def execute(*args):
        executions.append(True)
        return original(*args)

    monkeypatch.setattr(assistant.tools, 'execute', execute)

    def model(*args, **kwargs):
        return {'text': '', 'output': [{'type': 'function_call', 'call_id': str(uuid4()),
                 'name': 'find_creators', 'arguments': json.dumps({'query': 'alice'})}], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    turn = assistant.submit(pg, chat.ids['cm'], payload(), chat.settings)
    for _ in range(4):
        finish(chat)
    assert executions == [True]
    assistant.cancel(pg, chat.ids['cm'], turn['id'], chat.settings)


def test_selected_creator_changes_do_not_reuse_other_persons_context(pg, chat, monkeypatch):
    pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
               "VALUES(%s,%s,%s,'manual')", (uuid4(), chat.ids['other'], chat.ids['cm']))
    calls = []

    def model(_cfg, _key, items, _actor, **_kwargs):
        calls.append(items.copy())
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            return {'text': '', 'output': [{'type': 'function_call', 'call_id': str(uuid4()),
                'name': 'query_creators', 'arguments': json.dumps({
                    'query': None, 'importance': None, 'cursor': None,
                })}], 'usage': {}}
        result = json.loads(outputs[-1]['output'])
        final = {'answer': result['creators'][0]['name'], 'event_refs': [], 'result_refs': [],
                 'data_refs': [result['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    first = payload('查询当前达人资料')
    first['context'] = {'creator_ids': [str(chat.ids['person'])]}
    turn = assistant.submit(pg, chat.ids['cm'], first, chat.settings)
    for _ in range(3):
        finish(chat)
    assert assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)['status'] == 'completed'
    second = {**first, 'client_request_id': str(uuid4()),
              'context': {'creator_ids': [str(chat.ids['other'])]}}
    turn = assistant.submit(pg, chat.ids['cm'], second, chat.settings)
    for _ in range(3):
        finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['result']['data'][0]['creators'][0]['id'] == str(chat.ids['other'])
    assert not any(item.get('role') == 'assistant' for item in calls[2])

    def full_model(_cfg, _key, items, _actor, **_kwargs):
        calls.append(items.copy())
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            return {'text': '', 'output': [{'type': 'function_call', 'call_id': str(uuid4()),
                'name': 'summarize_messages', 'arguments': json.dumps({
                    'audience': 'all', 'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
                    'creator_ids': None, 'chat_ids': None, 'cursor': None,
                })}], 'usage': {}}
        result = json.loads(outputs[-1]['output'])
        assert result['total_messages'] == 2 and result['scope']['creator_ids'] is None
        final = {'answer': '全量授权消息。', 'event_refs': [], 'result_refs': [],
                 'data_refs': [result['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', full_model)
    third = {**first, 'client_request_id': str(uuid4()), 'message': '总结全部聊天',
             'context': {'audience': 'all'}}
    turn = assistant.submit(pg, chat.ids['cm'], third, chat.settings)
    for _ in range(3):
        finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['result']['scope']['audience'] == 'all'
    assert not any(item.get('role') == 'assistant' for item in calls[4])
    stored = pg.execute('SELECT checkpoint_ciphertext,usage FROM assistant_turns WHERE id=%s', (turn['id'],)).fetchone()
    checkpoint = summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)
    checkpoint['tool_results'][0]['gaps'] = [{'chat_id': 'chat', 'reason': '历史同步未覆盖检索起点'}]
    checkpoint['tool_results'][0]['coverage']['source_complete'] = False
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (summary_dialogues.encode(checkpoint, chat.settings), turn['id']))
    restored = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert restored['result']['answer'] == '全量授权消息。' and restored['result']['data'][0]['gaps'] == []
    assert restored['result']['data'][0]['coverage']['source_complete'] is True
    assert len(calls) == 6 and pg.execute('SELECT usage FROM assistant_turns WHERE id=%s', (turn['id'],)).fetchone()[
        'usage'] == stored['usage']
    next_turn = assistant.submit(pg, chat.ids['cm'], {
        **third, 'client_request_id': str(uuid4()), 'message': '继续追问', 'context': {},
    }, chat.settings)
    assert next_turn['scope']['audience'] == 'all' and next_turn['scope']['creator_ids'] is None
    next_row = assistant._turn(pg, chat.ids['cm'], next_turn['id'])
    next_checkpoint = summary_dialogues.decode(next_row['checkpoint_ciphertext'], chat.settings)
    next_context = assistant._context(pg, next_row, next_checkpoint, chat.settings)
    assert assistant._history(pg, next_row, chat.settings, next_context) == ([], [])
    assert len(calls) == 6
    assistant.cancel(pg, chat.ids['cm'], next_turn['id'], chat.settings)


def test_cancel_during_provider_call_keeps_receipt_without_showing_late_result(pg, chat, monkeypatch):
    turn = assistant.submit(pg, chat.ids['cm'], payload(), chat.settings)

    def model(*args, **kwargs):
        assistant.cancel(pg, chat.ids['cm'], turn['id'], chat.settings)
        final = {'answer': 'late private answer', 'event_refs': [], 'result_refs': [], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'response_id': 'late_response',
                'usage': {'input_tokens': 20, 'output_tokens': 10}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    finish(chat)
    row = pg.execute('SELECT * FROM assistant_turns').fetchone()
    assert row['status'] == 'cancelled' and row['result_ciphertext'] is None
    assert row['usage'] == {'input_tokens': 20, 'output_tokens': 10} and row['response_id'] == 'late_response'
    assert assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)['result'] is None
