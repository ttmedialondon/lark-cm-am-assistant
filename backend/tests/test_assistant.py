import json
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4

import pytest
from test_offhours_summary import finish_merges, sample_event
from test_offhours_summary import seeded as seeded  # noqa: F401

from app import assistant, assistant_search, assistant_window, config, openai_client, summary_dialogues
from app import offhours_summary as summaries
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


def test_complete_window_loop_segments_long_text_and_pages_sources_without_jobs(pg, chat, monkeypatch):
    ids = []
    for index in range(13):
        message_id = f'window-{index}'
        text = 'LONG_PRIVATE_BODY ' + ('业务明细' * 1800 if index == 0 else str(index))
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
            body_ciphertext,message_create_time) VALUES('chat',%s,'ou_person','text',%s,%s)""",
            (message_id, chat.cipher.encrypt(json.dumps({'text': text})),
             chat.start + timedelta(hours=2, seconds=index)))
        pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
            VALUES(%s,'chat',%s)""", (chat.ids['cm'], message_id))
        ids.append(message_id)
    monkeypatch.setattr(assistant_search, 'WINDOW_PAGE_SIZE', 1)
    monkeypatch.setattr(assistant_window, 'INPUT_CHARS', 3_200)
    monkeypatch.setattr(assistant, 'MAX_MODEL_STEPS', 1)
    calls, parts, raw_ids = [], [], set()

    def model(_cfg, _key, items, _actor, **kwargs):
        calls.append(kwargs['tools'])
        if not kwargs['tools']:
            segment = json.loads(items[0]['content'])
            assert len(assistant_window.encoded(segment)) <= assistant_window.INPUT_CHARS + 20
            messages = [message for conversation in segment.get('conversations', [])
                        for message in conversation['messages']]
            raw_ids.update(message['message_id'] for message in messages)
            for message in messages:
                if message['message_id'] == 'window-0':
                    parts.append((message['text_part'], message['text']))
            refs = [messages[0]['message_ref']] if messages else segment['summaries'][0]['evidence_refs']
            note = {'summary': '保留本段业务事项与当前进展。', 'evidence_refs': refs}
            return {'text': json.dumps(note), 'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1}}
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            return {'text': '', 'output': [{'type': 'function_call', 'call_id': 'window-call',
                'name': 'summarize_messages', 'arguments': json.dumps({'audience': 'all',
                    'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
                    'creator_ids': None, 'chat_ids': None, 'cursor': None})}], 'usage': {}}
        source = json.loads(outputs[-1]['output'])
        assert source['page_count'] == 15 and source['coverage']['scan_complete']
        assert source['total_messages'] == 15 and source['next_cursor'] is None
        assert 'summaries' in source and 'messages' not in source
        final = {'answer': '已整理本时段重点。', 'event_refs': [], 'result_refs': [],
                 'data_refs': [source['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    submitted = payload('总结全部授权聊天')
    turn = assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    progress = []
    for _ in range(60):
        finish(chat)
        value = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
        progress.append(value['progress'])
        if value['status'] not in assistant.ACTIVE:
            break
    assert value['status'] == 'completed' and len(calls) > 2
    assert raw_ids == {*ids, 'm1', 'outside'}
    parts.sort()
    assert parts[0][0][0] == 0 and parts[-1][0][1] == parts[-1][0][2]
    assert all(left[0][1] == right[0][0] for left, right in zip(parts, parts[1:], strict=False))
    assert ''.join(text for _, text in parts) == 'LONG_PRIVATE_BODY ' + '业务明细' * 1800
    assert any(isinstance(item, dict) and item.get('messages') == 15 for item in progress)
    assert len(value['result']['data']) == 1
    source = value['result']['data'][0]
    assert source['page_count'] == 15 and source['page'] == 1 and source['has_more']
    assert source['coverage']['returned'] == 15
    untouched = {table: pg.execute(f'SELECT count(*) n FROM {table}').fetchone()['n']
                 for table in ('assistant_turns', 'offhours_summary_runs', 'summary_reply_tasks')}
    read_ids = set()
    for page in range(1, 16):
        found = assistant.source_page(pg, chat.ids['cm'], source['query_ref'], chat.settings, page=page)
        read_ids.update(message['message_id'] for message in found['messages'])
        assert found['has_more'] == (page < 15)
    assert read_ids == raw_ids and all(pg.execute(f'SELECT count(*) n FROM {table}').fetchone()['n'] == count
                                    for table, count in untouched.items())
    paid_count = len(calls)
    with pytest.raises(LookupError):
        assistant.source_page(pg, chat.ids['reviewer'], source['query_ref'], chat.settings)
    with pytest.raises(ValueError):
        assistant.source_page(pg, chat.ids['cm'], source['query_ref'], chat.settings, page=16)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='window-0'",
               (chat.cipher.encrypt(json.dumps({'text': 'Updated fact'})),))
    with pytest.raises(ValueError, match='已更新'):
        assistant.source_page(pg, chat.ids['cm'], source['query_ref'], chat.settings, page=15)
    assert len(calls) == paid_count


@pytest.mark.parametrize('interruption', ['repeated_page', 'premature_end', 'permission', 'cancel'])
def test_window_collection_never_finalizes_missing_pages_or_replays_model(pg, chat, monkeypatch, interruption):
    monkeypatch.setattr(assistant_search, 'WINDOW_PAGE_SIZE', 1)
    calls = []
    monkeypatch.setattr(openai_client, 'call_openai_tools', lambda *args, **kwargs:
        calls.append(True) or {'text': '', 'output': [{'type': 'function_call', 'call_id': 'window-call',
            'name': 'summarize_messages', 'arguments': json.dumps({'audience': 'all',
                'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
                'creator_ids': None, 'chat_ids': None, 'cursor': None})}], 'usage': {}})
    turn = assistant.submit(pg, chat.ids['cm'], payload('总结全部聊天'), chat.settings)
    finish(chat)
    finish(chat)
    original = assistant._turn(pg, chat.ids['cm'], turn['id'])
    first = summary_dialogues.decode(original['checkpoint_ciphertext'], chat.settings)['tool_results'][0]
    if interruption == 'repeated_page':
        monkeypatch.setattr(assistant.tools, 'execute', lambda *args: first)
    elif interruption == 'premature_end':
        execute = assistant.tools.execute
        monkeypatch.setattr(assistant.tools, 'execute', lambda *args:
                            {**execute(*args), 'messages': [], 'next_cursor': None})
    elif interruption == 'permission':
        pg.execute("UPDATE authorizations SET scopes='{}' WHERE id=%s", (chat.ids['cm'],))
    job = assistant.claim()
    assert job is not None
    if interruption == 'cancel':
        assistant.cancel(pg, chat.ids['cm'], turn['id'], chat.settings)
    assistant.run(job, chat.settings)
    row = assistant._turn(pg, chat.ids['cm'], turn['id'])
    assert row['status'] == ('cancelled' if interruption == 'cancel' else 'failed')
    if interruption in {'repeated_page', 'premature_end'}:
        assert row['error_code'] == 'source_changed'
    assert row['result_ciphertext'] is None and len(calls) == 1
    assert assistant.claim() is None


def test_search_source_get_continues_without_model_and_cleared_refs_stay_unreadable(pg, chat, monkeypatch):
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat'",
               (chat.cipher.encrypt(json.dumps({'text': 'Find fixture facts'})),))
    monkeypatch.setattr(assistant_search, 'PAGE_SIZE', 1)
    calls = []

    def model(_cfg, _key, items, _actor, **_kwargs):
        calls.append(True)
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            return {'text': '', 'output': [{'type': 'function_call', 'call_id': 'search-call',
                'name': 'search_chat_history', 'arguments': json.dumps({'audience': 'all',
                    'query': ['fixture'], 'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
                    'creator_ids': None, 'chat_ids': None, 'cursor': None})}], 'usage': {}}
        source = json.loads(outputs[-1]['output'])
        final = {'answer': '找到相关原文。', 'event_refs': [], 'result_refs': [],
                 'data_refs': [source['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    turn = assistant.submit(pg, chat.ids['cm'], payload('查找fixture原话'), chat.settings)
    for _ in range(3):
        finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    source = view['result']['data'][0]
    assert source['has_more'] and source['next_source_url'].startswith('/assistant/sources/')
    before = pg.execute('SELECT checkpoint_ciphertext,usage FROM assistant_turns WHERE id=%s',
                        (turn['id'],)).fetchone()
    continuation = assistant.source_page(pg, chat.ids['cm'], source['query_ref'], chat.settings,
                                         page=2, cursor=source['next_cursor'])
    assert not continuation['has_more'] and len(continuation['messages']) == 1 and len(calls) == 2
    assert pg.execute('SELECT checkpoint_ciphertext,usage FROM assistant_turns WHERE id=%s',
                      (turn['id'],)).fetchone() == before
    ref = continuation['messages'][0]['message_ref']
    assert ref.startswith('u.') and '.p.' in ref and len(ref) <= 2048
    raw = assistant.tools.evidence(pg, chat.ids['cm'], ref, chat.settings)
    assert raw['message']['text'] == 'Find fixture facts'
    row = assistant._turn(pg, chat.ids['cm'], turn['id'])
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    context = assistant._context(pg, row, checkpoint, chat.settings)
    prepared = assistant.tools.execute(pg, chat.ids['cm'], row, 'draft_reply', {
        'event_ref': None, 'message_ref': ref, 'draft_ref': None, 'edited_draft': None,
        'action': 'generate', 'instruction': None, 'language': 'en'}, chat.settings, context)
    checkpoint['tool_results'].append(prepared)
    row['checkpoint_ciphertext'] = summary_dialogues.encode(checkpoint, chat.settings)
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (row['checkpoint_ciphertext'], turn['id']))
    draft = assistant.tools.execute(pg, chat.ids['cm'], row, 'save_reply_draft', {
        'draft_ref': prepared['draft_ref'], 'outcome': 'ready', 'language': 'en',
        'draft': "I'll check and get back to you.", 'chinese_translation': '我确认后回复你。',
        'explanation': '不编造未知事实。', 'checks': ['确认能够进行核查。'],
        'evidence_refs': [prepared['context']['messages'][0]['ref']]}, chat.settings, context)
    checkpoint['tool_results'].append(draft)
    row['checkpoint_ciphertext'] = summary_dialogues.encode(checkpoint, chat.settings)
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (row['checkpoint_ciphertext'], turn['id']))
    assert draft['draft']['outcome'] == 'ready'
    assert not assistant.tools.reply_status(pg, chat.ids['cm'], draft['draft_ref'], chat.settings)['new_messages']
    message_context = assistant.tools.execute(pg, chat.ids['cm'], row, 'read_message_context',
        {'message_ref': ref}, chat.settings, context)
    assistant.tools.validate_tool_results(pg, chat.ids['cm'], row, [message_context], chat.settings, context)
    assert message_context['message']['text'] == 'Find fixture facts'
    assert len(calls) == 2 and pg.execute('SELECT count(*) n FROM summary_reply_tasks').fetchone()['n'] == 0
    with pytest.raises(LookupError):
        assistant.tools.evidence(pg, chat.ids['reviewer'], ref, chat.settings)
    assistant.clear_conversations(pg, chat.ids['cm'], chat.settings)
    with pytest.raises(LookupError):
        assistant.tools.evidence(pg, chat.ids['cm'], ref, chat.settings)
    assert len(calls) == 2


def test_repeated_window_uses_validated_cache_and_history_does_not_double_source_pages(pg, chat, monkeypatch):
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
               (chat.cipher.encrypt(json.dumps({'text': 'Repeated window facts ' + '业务明细' * 1000})),))
    monkeypatch.setattr(assistant_search, 'WINDOW_PAGE_SIZE', 1)
    monkeypatch.setattr(assistant_window, 'INPUT_CHARS', 3_200)
    monkeypatch.setattr(assistant, 'MAX_MODEL_STEPS', 1)
    calls, repeated = [], True

    def model(_cfg, _key, items, _actor, **kwargs):
        calls.append(bool(kwargs['tools']))
        if not kwargs['tools']:
            segment = json.loads(items[0]['content'])
            messages = [message for conversation in segment.get('conversations', [])
                        for message in conversation['messages']]
            refs = [messages[0]['message_ref']] if messages else segment['summaries'][0]['evidence_refs']
            return {'text': json.dumps({'summary': '保留全部原文的进展。', 'evidence_refs': refs}),
                    'output': [], 'usage': {}}
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs or repeated:
            return {'text': '', 'output': [{'type': 'function_call',
                'call_id': 'repeat-call' if outputs else 'window-call', 'name': 'summarize_messages',
                'arguments': json.dumps({'audience': 'all', 'start': chat.start.isoformat(),
                    'end': chat.end.isoformat(), 'creator_ids': None, 'chat_ids': None, 'cursor': None})}], 'usage': {}}
        source = json.loads(outputs[-1]['output'])
        final = {'answer': '本轮完整时段已整理。', 'event_refs': [], 'result_refs': [],
                 'data_refs': [source['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    submitted = payload('总结完整时段')
    first = assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    for _ in range(20):
        finish(chat)
        view = assistant.turn_view(pg, chat.ids['cm'], first['id'], chat.settings)
        if view['status'] not in assistant.ACTIVE:
            break
    assert view['status'] == 'completed' and calls.count(True) == 2
    saved = summary_dialogues.decode(assistant._turn(pg, chat.ids['cm'], first['id'])['checkpoint_ciphertext'],
                                    chat.settings)
    assert len(saved['tool_results']) == 2 and saved['summary_steps'] == calls.count(False)
    assert saved['tool_outputs']['window-call']['query_ref'] == saved['tool_outputs']['repeat-call']['query_ref']
    repeated = False
    following = assistant.submit(pg, chat.ids['cm'], {
        **submitted, 'client_request_id': str(uuid4()), 'message': '再次总结同一时段',
    }, chat.settings)
    for _ in range(20):
        finish(chat)
        view = assistant.turn_view(pg, chat.ids['cm'], following['id'], chat.settings)
        if view['status'] not in assistant.ACTIVE:
            break
    assert view['status'] == 'completed' and len(view['result']['data']) == 1
    source = view['result']['data'][0]
    assert source['page_count'] == source['coverage']['returned'] == source['total_messages'] == 2
    assert source['coverage']['scan_complete']
    second = assistant.source_page(pg, chat.ids['cm'], source['query_ref'], chat.settings, page=2)
    assert second['page_count'] == 2 and second['coverage']['scan_complete']


def test_model_prepares_saves_and_returns_message_draft_without_child_job(pg, chat, monkeypatch):
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='outside'",
               (chat.cipher.encrypt(json.dumps({'text': 'When will the sample be dispatched?'})),))
    calls = []

    def model(_cfg, _key, items, _actor, **kwargs):
        assert '本人' in kwargs['instructions'] and 'save_reply_draft' in kwargs['instructions']
        calls.append(True)
        outputs = [json.loads(item['output']) for item in items if item.get('type') == 'function_call_output']
        latest = outputs[-1] if outputs else None
        if latest and latest.get('kind') == 'reply_draft':
            final = {'answer': '英文草稿已准备。', 'event_refs': [], 'result_refs': [],
                     'data_refs': [], 'draft_ref': latest['draft_ref']}
            return {'text': json.dumps(final), 'output': [], 'usage': {}}
        if not latest:
            name, args = 'search_chat_history', {'audience': 'all', 'query': ['dispatched'],
                'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
                'creator_ids': None, 'chat_ids': None, 'cursor': None}
        elif latest.get('kind') == 'messages':
            name, args = 'draft_reply', {'event_ref': None, 'message_ref': latest['messages'][0]['message_ref'],
                'draft_ref': None, 'edited_draft': None, 'action': 'generate',
                'instruction': None, 'language': 'en'}
        else:
            correcting = latest.get('error_code') == 'invalid_tool_input'
            if correcting:
                assert '依据' in latest['error'] and '权限' not in latest['error']
                latest = next(output for output in outputs if output.get('kind') == 'reply_material')
            assert latest['kind'] == 'reply_material'
            assert latest['context']['chat_url'] and latest['context']['is_full_conversation'] is False
            name, args = 'save_reply_draft', {'draft_ref': latest['draft_ref'], 'outcome': 'ready', 'language': 'en',
                'draft': "I'll check the dispatch status and get back to you once I have confirmation.",
                'chinese_translation': '我会确认发货状态，确认后回复你。', 'explanation': '不编造发货日期。',
                'checks': ['确认能够执行后续核查。'],
                'evidence_refs': [latest['source_ref'] if correcting else 'm.not-owned']}
        return {'text': '', 'output': [{'type': 'function_call', 'call_id': f'call-{len(calls)}',
            'name': name, 'arguments': json.dumps(args)}], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    turn = assistant.submit(pg, chat.ids['cm'], payload('找到发货询问并准备英文回复'), chat.settings)
    for _ in range(12):
        finish(chat)
        view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
        if view['status'] not in assistant.ACTIVE:
            break
    assert view['status'] == 'completed' and len(calls) == 5
    stored = assistant._turn(pg, chat.ids['cm'], turn['id'])
    assert summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)['tool_errors'] == [
        {'tool': 'save_reply_draft', 'code': 'invalid_tool_input'}]
    assert view['result']['scope']['audience'] == 'all'
    assert view['result']['draft']['outcome'] == 'ready' and view['result']['draft']['checks']
    assert view['result']['draft']['context']['message']['message_id'] == 'outside'
    assert pg.execute('SELECT count(*) n FROM summary_reply_tasks').fetchone()['n'] == 0
    assert pg.execute('SELECT count(*) n FROM offhours_summary_runs').fetchone()['n'] == 0


def test_model_first_lookup_replay_history_and_actor_isolation(pg, chat, monkeypatch):
    calls = []
    owner_scopes = assistant.tools.scopes_for(pg, chat.ids['owner'])
    assert owner_scopes['can_switch_cm'] and owner_scopes['selected_cm_id'] == str(chat.ids['owner'])
    assert {p['cm_id'] for p in owner_scopes['scopes']} == {
        str(chat.ids[name]) for name in ('owner', 'cm', 'reviewer', 'tester')}
    own_scopes = assistant.tools.scopes_for(pg, chat.ids['cm'])
    assert own_scopes == {'can_switch_cm': False, 'selected_cm_id': str(chat.ids['cm']),
                         'scopes': [{'cm_id': str(chat.ids['cm']), 'cm_name': 'cm'}]}
    consent = pg.execute('SELECT body_consent_at FROM authorizations WHERE id=%s',
                         (chat.ids['owner'],)).fetchone()['body_consent_at']
    pg.execute('UPDATE authorizations SET body_consent_at=NULL WHERE id=%s', (chat.ids['owner'],))
    assert assistant.tools.scopes_for(pg, chat.ids['owner'])['selected_cm_id'] is None
    with pytest.raises(ValueError, match='连接尚未就绪'):
        assistant.tools.current_context(pg, chat.ids['owner'], {}, chat.settings)
    pg.execute('UPDATE authorizations SET body_consent_at=%s WHERE id=%s', (consent, chat.ids['owner']))
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
    owner_request = {**payload('检查其他 CM 的消息'), 'context': {'cm_id': str(chat.ids['cm'])}}
    owner_turn = assistant.submit(pg, chat.ids['owner'], owner_request, chat.settings)
    row = pg.execute('SELECT checkpoint_ciphertext FROM assistant_turns WHERE id=%s',
                     (owner_turn['id'],)).fetchone()
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    checkpoint['progress'] = {'messages': 999, 'conversations': 100}
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (summary_dialogues.encode(checkpoint, chat.settings), owner_turn['id']))
    pg.execute('UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s', (chat.ids['owner'],))
    denied = assistant.turn_view(pg, chat.ids['owner'], owner_turn['id'], chat.settings)
    assert denied['error_code'] == 'permission' and denied['result'] is None
    assert denied['scope'] == {} and denied['message'] == denied['progress'] == ''
    pg.execute('UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s', (chat.ids['owner'],))
    assistant.cancel(pg, chat.ids['owner'], owner_turn['id'], chat.settings)
    assert len(calls) == 2
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


def test_natural_queries_replace_stale_filters_and_keep_valid_followup_sources(pg, chat, monkeypatch):
    pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
               "VALUES(%s,%s,%s,'manual')", (uuid4(), chat.ids['other'], chat.ids['cm']))
    calls = []

    def model(_cfg, _key, items, _actor, **_kwargs):
        calls.append(items.copy())
        outputs = [item for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            question = json.loads(next(item['content'] for item in reversed(items)
                                       if item.get('role') == 'user'))['question']
            return {'text': '', 'output': [{'type': 'function_call', 'call_id': str(uuid4()),
                'name': 'query_creators', 'arguments': json.dumps({
                    'query': 'alice' if '@alice' in question else 'other', 'importance': None, 'cursor': None,
                })}], 'usage': {}}
        result = json.loads(outputs[-1]['output'])
        final = {'answer': result['creators'][0]['name'], 'event_refs': [], 'result_refs': [],
                 'data_refs': [result['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    first = payload('查询 @alice 达人资料')
    first['context'] = {'creator_ids': [str(chat.ids['other'])], 'audience': 'creators', 'chat_ids': ['not-visible'],
                        'start': (chat.start - timedelta(days=8)).isoformat(), 'end': chat.start.isoformat()}
    turn = assistant.submit(pg, chat.ids['cm'], first, chat.settings)
    assert turn['scope']['audience'] == 'all' and turn['scope']['creator_ids'] is None
    assert 'chat_ids' not in turn['scope'] and 'window' not in turn['scope']
    for _ in range(3):
        finish(chat)
    assert assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)['status'] == 'completed'
    second = {**first, 'client_request_id': str(uuid4()), 'message': '改查 other 达人资料', 'context': {}}
    turn = assistant.submit(pg, chat.ids['cm'], second, chat.settings)
    for _ in range(3):
        finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['result']['data'][0]['creators'][0]['id'] == str(chat.ids['other'])
    assert any(item.get('role') == 'assistant' for item in calls[2])
    row = assistant._turn(pg, chat.ids['cm'], turn['id'])
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    assert checkpoint['followup_scope']['creator_ids'] == [str(chat.ids['person'])]
    mixed = {**checkpoint, 'tool_results': [{'scope': checkpoint['context']['scope'],
        '_request': {'name': 'search_chat_history', 'args': {'query': ['sample']}}}, *checkpoint['tool_results']]}
    continued = {**row, 'input_ciphertext': summary_dialogues.encode({'message': '改成上周'}, chat.settings),
                 'checkpoint_ciphertext': summary_dialogues.encode(mixed, chat.settings)}
    context = assistant._context(pg, row, checkpoint, chat.settings)
    assert assistant.tools._named_selection(pg, continued, chat.settings, context, [str(chat.ids['person'])])
    assert not assistant.tools._named_selection(pg, continued, chat.settings, context, [str(chat.ids['other'])])

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
        final = {'answer': '本人聊天。', 'event_refs': [], 'result_refs': [],
                 'data_refs': [result['query_ref']], 'draft_ref': None}
        return {'text': json.dumps(final), 'output': [], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', full_model)
    third = {**first, 'client_request_id': str(uuid4()), 'message': '总结全部聊天',
             'context': first['context']}
    turn = assistant.submit(pg, chat.ids['cm'], third, chat.settings)
    for _ in range(3):
        finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['result']['scope']['audience'] == 'all'
    assert any(item.get('role') == 'assistant' for item in calls[4])
    assert view['result']['data'][0]['window']['start'] == chat.start.isoformat()
    stored = pg.execute('SELECT checkpoint_ciphertext,usage FROM assistant_turns WHERE id=%s', (turn['id'],)).fetchone()
    checkpoint = summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)
    material = next(result for result in checkpoint['tool_results'] if result.get('kind') == 'window_messages')
    material['gaps'] = [{'chat_id': 'chat', 'reason': '历史同步未覆盖检索起点'}]
    material['coverage']['source_complete'] = False
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (summary_dialogues.encode(checkpoint, chat.settings), turn['id']))
    restored = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert restored['result']['answer'] == '本人聊天。' and restored['result']['data'][0]['gaps'] == []
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
    history, retained = assistant._history(pg, next_row, chat.settings, next_context)
    previous = [json.loads(item['content']) for item in history if item.get('role') == 'assistant']
    assert previous[-1]['answer'] == '' and previous[-1]['scope']['audience'] == 'all'
    assert previous[-1]['data'][0]['window']['start'] == chat.start.isoformat() and retained
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


def test_unreplied_creator_query_covers_whole_roster_and_attaches_all_events(pg, chat, monkeypatch):
    monkeypatch.setattr(summaries, 'connection', lambda: nullcontext(pg))
    monkeypatch.setattr(summaries, 'get_settings', lambda: chat.settings)
    people = [uuid4() for _ in range(200)]
    pg.execute("""INSERT INTO contacts(id,display_name)
        SELECT id,'Creator ' || n FROM unnest(%s::uuid[]) WITH ORDINALITY AS p(id,n)""", (people,))
    pg.execute("""INSERT INTO contact_roles(contact_id,role,status,source)
        SELECT id,'creator','confirmed','manual' FROM unnest(%s::uuid[]) AS p(id)""", (people,))
    pg.execute("""INSERT INTO lark_identities(open_id,contact_id,display_name)
        SELECT 'ou_' || id,id,'Creator ' || n
        FROM unnest(%s::uuid[]) WITH ORDINALITY AS p(id,n)""", (people,))
    pg.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        SELECT gen_random_uuid(),id,%s,'manual' FROM unnest(%s::uuid[]) AS p(id)""", (chat.ids['cm'], people))
    pg.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,manual_tier)
        SELECT id,%s,'normal' FROM unnest(%s::uuid[]) AS p(id)""",
        (chat.ids['cm'], [chat.ids['person'], *people]))
    for index, person in enumerate(people[:31]):
        chat_id, sender = f'creator-{index}', f'ou_{person}'
        private = index % 2 == 0
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,%s)",
                   (chat_id, f'Creator {index + 1} conversation', 'p2p' if private else 'group'))
        pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
            backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
            VALUES(%s,%s,%s,TRUE,%s,%s,'succeeded')""",
            (chat.ids['cm'], chat_id, sender if private else None, chat.start - timedelta(days=9), chat.end))
        messages = [(f'request-{index}', sender, f'Please check order ORDER-{index}', None)]
        if index == 0:
            messages.append(('staff-reply', 'ou_cm', 'I will check order ORDER-0', 'request-0'))
        for message_id, speaker, text, parent in messages:
            pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
                body_ciphertext,message_create_time,parent_id) VALUES(%s,%s,%s,'text',%s,%s,%s)""",
                (chat_id, message_id, speaker, chat.cipher.encrypt(json.dumps({'text': text})),
                 chat.start + timedelta(hours=2, minutes=bool(parent)), parent))
            pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
                VALUES(%s,%s,%s)""", (chat.ids['cm'], chat_id, message_id))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
               (chat.cipher.encrypt(json.dumps({'text': 'My samples have arrived, thank you'})),))
    api_calls = []

    def model(_cfg, _key, items, _actor, **_kwargs):
        api_calls.append(items.copy())
        outputs = [json.loads(item['output']) for item in items if item.get('type') == 'function_call_output']
        if not outputs:
            name, args = 'query_creators', {'query': None, 'importance': None, 'cursor': None}
        elif outputs[-1].get('kind') == 'creators':
            assert outputs[-1]['total'] == 201 and len(outputs[-1]['creators']) == 25
            name, args = 'query_unreplied_creator_events', {
                'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
            }
        else:
            result = outputs[-1]
            assert result['total'] == 31 and len(result['events']) == 25
            assert result.get('next_cursor') is None
            assert not any(key.startswith('_') for key in result)
            assert result['events'][0]['contact_id'] == str(chat.ids['person'])
            final = {'answer': '已整理 31 件未见回复事项，重点达人优先。',
                     'event_refs': [result['events'][0]['event_ref']], 'result_refs': [result['result_id']],
                     'data_refs': [], 'draft_ref': None}
            return {'text': json.dumps(final), 'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        return {'text': '', 'output': [{'type': 'function_call', 'call_id': f'call-{len(api_calls)}',
                'name': name, 'arguments': json.dumps(args)}], 'usage': {'input_tokens': 1, 'output_tokens': 1}}

    def event_model(_cfg, _key, pack, _actor, **_kwargs):
        user = next(iter(pack['users']))
        request = next(message for message in pack['messages']
                       if message['speaker'] == user and message['ref'] in pack['core_refs'])
        reply = [message['ref'] for message in pack['messages'] if message['message_id'] == 'staff-reply']
        event = sample_event(user=user, handle=next(iter(pack['users'][user]['handles']), None),
            priority='P2' if request['message_id'] == 'm1' else 'P0',
            handling_status='resolved' if request['message_id'] == 'm1' else 'unresolved',
            evidence_refs=[request['ref']], reply_evidence_refs=reply)
        return {'text': json.dumps({'processed_refs': pack['core_refs'], 'events': [event]}), 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', model)
    monkeypatch.setattr(openai_client, 'call_openai', event_model)
    submitted = payload('总结昨天到今天我负责的达人未回复消息，给群名、私聊名和概括，重要达人优先')
    turn = assistant.submit(pg, chat.ids['cm'], submitted, chat.settings)
    finish(chat)
    finish(chat)
    current = assistant._turn(pg, chat.ids['cm'], turn['id'])
    checkpoint = summary_dialogues.decode(current['checkpoint_ciphertext'], chat.settings)
    context = assistant._context(pg, current, checkpoint, chat.settings)
    limited = assistant.tools.execute(pg, chat.ids['cm'], current, 'summarize_messages', {
        'audience': 'creators', 'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
        'creator_ids': [person['id'] for person in checkpoint['tool_results'][0]['creators'][:3]],
        'chat_ids': None, 'cursor': None,
    }, chat.settings, context)
    assert limited['error_code'] == 'creator_selection_not_requested' and len(api_calls) == 1
    blank = assistant.tools.execute(pg, chat.ids['cm'], current, 'query_creators', {
        'query': '  ', 'importance': None, 'cursor': None,
    }, chat.settings, context)
    blank_row = {**current, 'checkpoint_ciphertext': summary_dialogues.encode(
        {**checkpoint, 'tool_results': [*checkpoint['tool_results'], blank]}, chat.settings)}
    whitespace = assistant.tools.execute(pg, chat.ids['cm'], blank_row, 'summarize_messages', {
        'audience': 'creators', 'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
        'creator_ids': [person['id'] for person in blank['creators'][:3]], 'chat_ids': None, 'cursor': None,
    }, chat.settings, context)
    assert whitespace['error_code'] == 'creator_selection_not_requested' and len(api_calls) == 1
    assert pg.execute('SELECT count(*) n FROM offhours_summary_runs').fetchone()['n'] == 0
    pg.execute("""UPDATE creator_importance_profiles SET manual_tier='focus',revision=revision+1,
        evaluated_at=NOW() WHERE contact_id=%s AND responsible_authorization_id=%s""",
        (chat.ids['person'], chat.ids['cm']))
    finish(chat)
    finish(chat)
    row = pg.execute('SELECT * FROM assistant_turns WHERE id=%s', (turn['id'],)).fetchone()
    checkpoint = summary_dialogues.decode(row['checkpoint_ciphertext'], chat.settings)
    assert row['status'] == 'waiting' and checkpoint['waiting']['purpose'] == 'unreplied_creators'
    assert checkpoint['waiting']['kind'] == 'summary' and len(api_calls) == 2
    report = pg.execute('SELECT * FROM offhours_summary_runs WHERE assistant_turn_id=%s', (turn['id'],)).fetchone()
    assert set(report['selected_contact_ids']) == {chat.ids['person'], *people}
    summaries.prepare_next_summary(chat.settings)
    for _ in range(40):
        job = summaries.claim_next_summary()
        if job is None:
            break
        summaries.run_claimed_summary(job, chat.settings)
    else:
        raise AssertionError('conversation processing did not finish')
    finish_merges(pg, chat)
    pg.execute('UPDATE assistant_turns SET scheduled_at=NOW() WHERE id=%s', (turn['id'],))
    assert assistant.prepare(chat.settings)
    assert len(api_calls) == 2
    finish(chat)
    view = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert view['status'] == 'completed' and view['error'] is None and len(api_calls) == 3
    result = view['result']
    assert result['scope']['creator_ids'] is None and result['coverage']['confirmed_creators'] == 201
    assert len(result['events']) == 31 and len(result['event_refs']) == 31
    assert result['events'][0]['contact_id'] == str(chat.ids['person'])
    assert result['events'][0]['handling_status'] == 'resolved'
    assert str(people[0]) not in {event['contact_id'] for event in result['events']}
    assert {event['contact_id'] for event in result['events']} == {str(chat.ids['person']), *map(str, people[1:31])}
    assert {source['type'] for event in result['events'] for source in event['conversations']} == {'group', 'private'}
    assert all(source['name'] for event in result['events'] for source in event['conversations'])
    stored = pg.execute('SELECT * FROM assistant_turns WHERE id=%s', (turn['id'],)).fetchone()
    saved = summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)['tool_results']
    final = summary_dialogues.decode(stored['result_ciphertext'], chat.settings)['final']
    stored_context = assistant._context(pg, assistant._turn(pg, chat.ids['cm'], turn['id']),
        summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings), chat.settings)
    complete = assistant.tools._read_results(pg, chat.ids['cm'], [report['id']], chat.settings, stored_context)
    generic = {**assistant.tools._page(complete, 0), '_assistant_turn_id': str(turn['id'])}
    whole = assistant.tools.build_result(pg, chat.ids['cm'], stored, {
        'answer': '全部事项已整理。', 'event_refs': [], 'result_refs': [complete['result_id']],
        'data_refs': [], 'draft_ref': None}, chat.settings, context, [generic])
    assert len(generic['events']) == 25 and len(whole['events']) == 32
    assert whole['worklist_summary']['events'] == 32 and len(api_calls) == 3
    wide = assistant.tools.execute(pg, chat.ids['cm'], stored, 'search_chat_history', {
        'audience': 'creators', 'query': ['ORDER'], 'start': chat.start.isoformat(),
        'end': chat.end.isoformat(), 'creator_ids': list(map(str, [chat.ids['person'], *people])),
        'chat_ids': None, 'cursor': None}, chat.settings, stored_context)
    assert wide['next_cursor'] and max(len(ref) for ref in wide['_message_refs'].values()) > 2048
    with_wide = summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)
    with_wide['tool_results'].append(wide)
    alternative = assistant.tools.execute(pg, chat.ids['cm'], stored, 'search_chat_history',
        {**wide['_request']['args'], 'query': ['ORDER', 'NOT_FOUND']}, chat.settings, stored_context)
    with_wide['tool_results'].append(alternative)
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (summary_dialogues.encode(with_wide, chat.settings), turn['id']))
    wide_page = assistant.source_page(pg, chat.ids['cm'], wide['query_ref'], chat.settings,
                                      page=2, cursor=wide['next_cursor'])
    assert wide_page['messages'] and all(len(item['message_ref']) <= 2048 for item in wide_page['messages'])
    wide_ref = wide_page['messages'][0]['message_ref']
    assert assistant.tools.evidence(pg, chat.ids['cm'], wide_ref, chat.settings)['message']['text'].startswith(
        'Please check order ORDER-')
    _, source_turn, _digest, token = wide_ref.split('.', 3)
    different = alternative['query_ref'].split('.')[2]
    with pytest.raises(ValueError):
        assistant.tools.evidence(pg, chat.ids['cm'], f'u.{source_turn}.{different}.{token}', chat.settings)
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (stored['checkpoint_ciphertext'], turn['id']))
    assert len(api_calls) == 3
    filter_checkpoint = summary_dialogues.decode(stored['checkpoint_ciphertext'], chat.settings)
    filter_context = assistant._context(pg, assistant._turn(pg, chat.ids['cm'], turn['id']),
                                        filter_checkpoint, chat.settings)
    refused_filter = assistant.tools.execute(pg, chat.ids['cm'], stored, 'filter_events', {
        'result_id': result['result_refs'][0], 'creator_ids': list(map(str, people[:3])),
        'priority': None, 'reply_status': 'unreplied', 'handling_status': None, 'topic': None, 'cursor': None,
    }, chat.settings, filter_context)
    assert refused_filter['error_code'] == 'creator_selection_not_requested' and len(api_calls) == 3
    for tier, count in [('focus', 1), ('normal', 30)]:
        filter_row = assistant._turn(pg, chat.ids['cm'], turn['id'])
        filtered = assistant.tools.execute(pg, chat.ids['cm'], filter_row, 'filter_events', {
            'result_id': result['result_refs'][0], 'creator_ids': None, 'importance': tier,
            'priority': None, 'reply_status': 'unreplied', 'handling_status': None, 'topic': None, 'cursor': None,
        }, chat.settings, filter_context)
        assert filtered['total'] == count and filtered['_event_filter']
        filter_checkpoint['tool_results'].append(filtered)
        pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
                   (summary_dialogues.encode(filter_checkpoint, chat.settings), turn['id']))
        filtered_view = assistant.tools.build_result(pg, chat.ids['cm'], filter_row, final,
            chat.settings, filter_context, filter_checkpoint['tool_results'])
        assert len(filtered_view['events']) == count
        expected = result['event_refs'][:1] if tier == 'focus' else result['event_refs'][1:]
        assert filtered_view['event_refs'] == expected
        assert filtered_view['coverage']['matched_events'] == count
    named_lookup = assistant.tools.execute(pg, chat.ids['cm'], stored, 'find_creators',
        {'query': '@alice'}, chat.settings, filter_context)
    selected_checkpoint = {**filter_checkpoint, 'tool_results': [*filter_checkpoint['tool_results'], named_lookup]}
    selected_row = {**assistant._turn(pg, chat.ids['cm'], turn['id']),
        'input_ciphertext': summary_dialogues.encode({'message': '只看 @alice'}, chat.settings),
        'checkpoint_ciphertext': summary_dialogues.encode(selected_checkpoint, chat.settings)}
    selected = assistant.tools.execute(pg, chat.ids['cm'], selected_row, 'filter_events', {
        'result_id': result['result_refs'][0], 'creator_ids': [str(chat.ids['person'])], 'importance': None,
        'priority': None, 'reply_status': 'unreplied', 'handling_status': None, 'topic': None, 'cursor': None,
    }, chat.settings, filter_context)
    assert selected['scope']['creator_ids'] == [str(chat.ids['person'])]
    selected_view = assistant.tools.build_result(pg, chat.ids['cm'], selected_row, final,
        chat.settings, filter_context, [*selected_checkpoint['tool_results'], selected])
    assert selected_view['scope']['creator_ids'] == [str(chat.ids['person'])]
    assert len(selected_view['events']) == 1 and len(api_calls) == 3
    pg.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
               (stored['checkpoint_ciphertext'], turn['id']))
    assert len(assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)['result']['events']) == 31
    next_turn = assistant.submit(pg, chat.ids['cm'], {
        **submitted, 'client_request_id': str(uuid4()), 'message': '谢谢',
    }, chat.settings)
    next_row = assistant._turn(pg, chat.ids['cm'], next_turn['id'])
    next_checkpoint = summary_dialogues.decode(next_row['checkpoint_ciphertext'], chat.settings)
    context = assistant._context(pg, next_row, next_checkpoint, chat.settings)
    history, _retained = assistant._history(pg, next_row, chat.settings, context)
    previous = json.loads(next(item['content'] for item in history if item['role'] == 'assistant'))
    assert len(previous['events']) == 25 and previous['event_count'] == 31
    assert previous['event_index'][30]['index'] == 31
    assert previous['event_index'][30]['event_ref'] == result['event_refs'][30]
    no_attachment = assistant.tools.build_result(pg, chat.ids['cm'], next_row,
        {**final, 'answer': '不客气。', 'event_refs': [], 'result_refs': []}, chat.settings, context, saved)
    assert no_attachment['events'] == []
    assistant.cancel(pg, chat.ids['cm'], next_turn['id'], chat.settings)

    def named_batch_model(_cfg, _key, items, _actor, **_kwargs):
        api_calls.append(items.copy())
        return {'text': '', 'output': [
            {'type': 'function_call', 'call_id': 'named-lookup', 'name': 'find_creators',
             'arguments': json.dumps({'query': '@alice'})},
            {'type': 'function_call', 'call_id': 'named-summary', 'name': 'summarize_messages',
             'arguments': json.dumps({'audience': 'creators', 'start': chat.start.isoformat(),
                'end': chat.end.isoformat(), 'creator_ids': [str(chat.ids['person'])],
                'chat_ids': None, 'cursor': None})}], 'usage': {}}

    monkeypatch.setattr(openai_client, 'call_openai_tools', named_batch_model)
    named_turn = assistant.submit(pg, chat.ids['cm'], payload('总结 @alice 的消息'), chat.settings)
    finish(chat)
    finish(chat)
    named_row = assistant._turn(pg, chat.ids['cm'], named_turn['id'])
    named_checkpoint = summary_dialogues.decode(named_row['checkpoint_ciphertext'], chat.settings)
    assert named_row['status'] == 'waiting' and named_checkpoint['waiting']['kind'] == 'summary'
    assert named_checkpoint['tool_results'][0]['_request']['args']['query'] == '@alice'
    assert len(api_calls) == 4
    assistant.cancel(pg, chat.ids['cm'], named_turn['id'], chat.settings)
    before_reports = pg.execute('SELECT count(*) n FROM offhours_summary_runs').fetchone()['n']
    empty_turn = assistant.submit(pg, chat.ids['tester'], payload('总结我负责的达人未回复事项'), chat.settings)
    empty_row = assistant._turn(pg, chat.ids['tester'], empty_turn['id'])
    empty_checkpoint = summary_dialogues.decode(empty_row['checkpoint_ciphertext'], chat.settings)
    empty_context = assistant._context(pg, empty_row, empty_checkpoint, chat.settings)
    empty = assistant.tools.execute(pg, chat.ids['tester'], empty_row, 'query_unreplied_creator_events', {
        'start': chat.start.isoformat(), 'end': chat.end.isoformat(),
    }, chat.settings, empty_context)
    empty_view = assistant.tools.build_result(pg, chat.ids['tester'], empty_row, {
        'answer': '当前没有正式负责达人。', 'event_refs': [], 'result_refs': [], 'data_refs': [], 'draft_ref': None,
    }, chat.settings, empty_context, [empty])
    assert empty_view['coverage']['responsible_creators'] == 0 and empty_view['coverage']['matched_events'] == 0
    assert empty_view['events'] == [] and empty_view['window'] == empty['window'] and len(api_calls) == 4
    assert pg.execute('SELECT count(*) n FROM offhours_summary_runs').fetchone()['n'] == before_reports
    assistant.cancel(pg, chat.ids['tester'], empty_turn['id'], chat.settings)
    original = pg.execute("SELECT body_ciphertext FROM chat_messages WHERE message_id='m1'").fetchone()[
        'body_ciphertext']
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
               (chat.cipher.encrypt(json.dumps({'text': 'A corrected different message'})),))
    partial = assistant.tools._read_results(pg, chat.ids['cm'], [report['id']], chat.settings, context)
    assert not partial['pending'] and not partial['coverage']['analysis_complete'] and partial['gaps']
    changed = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert changed['result'] is None and changed['error_code'] == 'source_changed' and len(api_calls) == 4
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'", (original,))
    pg.execute("UPDATE authorizations SET scopes=ARRAY['im:chat:read'] WHERE id=%s", (chat.ids['cm'],))
    denied = assistant.turn_view(pg, chat.ids['cm'], turn['id'], chat.settings)
    assert denied['result'] is None and denied['error_code'] == 'permission' and len(api_calls) == 4
