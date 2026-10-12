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
