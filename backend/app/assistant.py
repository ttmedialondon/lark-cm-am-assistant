"""Durable, model-first conversations over the existing business tools."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from . import ai_dispatch
from . import assistant_rules as rules
from . import assistant_tools as tools
from . import assistant_window as windows
from . import openai_client as api
from . import summary_dialogues as codec
from .ai_feature_config import api_key_cipher
from .assistant_replies import INSTRUCTIONS as REPLY_INSTRUCTIONS
from .db import connection

FEATURE = 'operations_assistant'
MAX_MODEL_STEPS = 12
ACTIVE = ('queued', 'running', 'waiting')
ERRORS = {
    'cancelled': '已停止。', 'unknown': '请求结果未知，未自动重复调用。',
    'provider_error': 'AI 服务暂时不可用。', 'quota': '助手 API 配额不足。',
    'invalid_output': '本轮分析结果未通过校验，请重新提问。',
    'permission': '账号连接或访问权限已变化，请检查当前连接。',
    'source_changed': '资料在处理期间已变化，请重新提问；系统未自动重复调用 AI。',
    'disabled': '运营助手尚未启用。', 'step_limit': '本轮处理步骤已达上限。',
    'internal': '本轮处理未完成，请重新提问。',
}
OUTPUT_FORMAT = {
    'type': 'json_schema', 'name': 'operations_answer', 'strict': True,
    'schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'answer': {'type': 'string'},
            'sections': {'type': 'array', 'maxItems': 40, 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                    'summary': {'type': 'string', 'minLength': 1, 'maxLength': 2500},
                    'message_refs': {'type': 'array', 'minItems': 1, 'maxItems': 30,
                        'items': {'type': 'string'}}},
                'required': ['title', 'summary', 'message_refs']}},
            'event_refs': {'type': 'array', 'items': {'type': 'string'}},
            'result_refs': {'type': 'array', 'items': {'type': 'string'}},
            'data_refs': {'type': 'array', 'items': {'type': 'string'}},
            'draft_ref': {'type': ['string', 'null']},
        },
        'required': ['answer', 'sections', 'event_refs', 'result_refs', 'data_refs', 'draft_ref'],
    },
}


def _config(conn):
    return conn.execute('''SELECT provider,project_id,model,enabled,api_key_ciphertext
        FROM ai_feature_configs WHERE feature=%s''', (FEATURE,)).fetchone()


def configured(conn):
    cfg = _config(conn)
    return bool(cfg and cfg['enabled'] and cfg['provider'] == 'openai' and cfg['api_key_ciphertext'])


def _actor(conn, actor):
    actor = UUID(str(actor))
    row = conn.execute('SELECT id,status,body_consent_at FROM authorizations WHERE id=%s', (actor,)).fetchone()
    if not row or row['status'] != 'active':
        raise PermissionError('当前登录或消息授权已失效。')
    return actor


def _turn(conn, actor, turn_id, *, lock=False):
    actor = _actor(conn, actor)
    row = conn.execute('''SELECT t.*,s.actor_id FROM assistant_turns t
        JOIN assistant_sessions s ON s.id=t.session_id WHERE t.id=%s AND s.actor_id=%s
        AND t.error_code IS DISTINCT FROM 'cleared' '''
        + (' FOR UPDATE OF t' if lock else ''), (UUID(str(turn_id)), actor)).fetchone()
    if not row:
        raise LookupError('对话不存在或无权查看。')
    return dict(row)


def submit(conn, actor, payload, settings):
    actor = _actor(conn, actor)
    if not isinstance(payload, dict) or set(payload) - {'session_id', 'client_request_id', 'message', 'context'}:
        raise ValueError('请求格式不正确。')
    session_id, request_id = UUID(str(payload.get('session_id'))), UUID(str(payload.get('client_request_id')))
    message = payload.get('message')
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 24000:
        raise ValueError('请填写问题，正文最多 24000 字节。')
    conn.execute('''INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)
        ON CONFLICT(id) DO NOTHING''', (session_id, actor))
    session = conn.execute('SELECT * FROM assistant_sessions WHERE id=%s FOR UPDATE', (session_id,)).fetchone()
    if session['actor_id'] != actor:
        raise PermissionError('对话不属于当前用户。')
    duplicate = conn.execute('SELECT * FROM assistant_turns WHERE session_id=%s AND client_request_id=%s',
                             (session_id, request_id)).fetchone()
    if duplicate:
        if duplicate['error_code'] == 'cleared':
            raise ValueError('这条对话已清空，请重新提问。')
        original = codec.decode(duplicate['input_ciphertext'], settings)
        if original['message'] != message.strip() or original.get('selected_context', {}) != payload.get('context', {}):
            raise ValueError('同一请求编号不能用于不同问题。')
        return turn_view(conn, actor, duplicate['id'], settings)
    if not configured(conn):
        raise ValueError(ERRORS['disabled'])
    if conn.execute('SELECT 1 FROM assistant_turns WHERE session_id=%s AND status=ANY(%s)',
                    (session_id, list(ACTIVE))).fetchone():
        raise ValueError('请先等待或停止当前问题。')
    now = datetime.now(UTC)
    selection = payload.get('context') or {}
    if not isinstance(selection, dict):
        raise ValueError('页面上下文无效。')
    selection = {key: value for key, value in selection.items() if key in {
        'cm_id', 'event_ref', 'message_ref', 'reply_action', 'draft_ref', 'edited_draft', 'query_ref', 'cursor',
    }}
    context = tools.current_context(conn, actor, selection, settings, now=now)
    data = {'message': message.strip(), 'selected_context': payload.get('context', {})}
    checkpoint = {'context': context, 'model_steps': 0, 'tool_results': [], 'pending_calls': [],
                  'tool_outputs': {}, 'tool_cache': {}, 'items': [], 'in_flight': False, 'progress': '等待处理',
                  'rules': rules.snapshot(conn, settings)}
    turn_id = uuid4()
    conn.execute('''INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,
        checkpoint_ciphertext,created_at) VALUES(%s,%s,%s,%s,%s,%s)''',
        (turn_id, session_id, request_id, codec.encode(data, settings), codec.encode(checkpoint, settings), now))
    conn.execute('UPDATE assistant_sessions SET updated_at=NOW(),context_ciphertext=%s WHERE id=%s',
                 (codec.encode({'cm_id': context['scope']['cm_id']}, settings), session_id))
    return turn_view(conn, actor, turn_id, settings)


def _context(conn, row, checkpoint, settings, *, read_cache=None):
    selected = checkpoint.get('context', {}).get('scope', {})
    if 'audience' not in selected:
        selected = {**selected, 'audience': 'creators'}  # Old answers used the creator-only material protocol.
    context = tools.current_context(conn, row['actor_id'], selected, settings, now=row['created_at'],
                                    read_cache=read_cache)
    scopes = [result.get('scope', {}) for result in checkpoint.get('tool_results', [])]
    scopes.extend([checkpoint.get('waiting', {}).get('scope', {}),
                   checkpoint.get('window_job', {}).get('task', {}).get('scope', {})])
    checked = {context['scope']['cm_id']}
    for scope in scopes:
        if scope.get('cm_id') and scope['cm_id'] not in checked:
            tools.context_for_scope(conn, row['actor_id'], context, scope, read_cache=read_cache)
            checked.add(scope['cm_id'])
    return context


def _result(conn, row, checkpoint, settings, *, context=None, read_cache=None):
    stored = codec.decode(row['result_ciphertext'], settings)
    if not stored:
        return None
    context = context or _context(conn, row, checkpoint, settings, read_cache=read_cache)
    result = tools.build_result(conn, row['actor_id'], row, stored['final'], settings,
                                context, checkpoint.get('tool_results', []), read_cache=read_cache)
    return _source_cards(result, checkpoint.get('tool_results', []))


def _source_cards(result, tool_results):
    data, seen = [], set()
    for item in result.get('data', []):
        if item.get('kind') != 'window_messages':
            if item.get('kind') == 'messages':
                page = next(value for value in tool_results if value.get('query_ref') == item['query_ref'])
                identity = (page['query_ref'].split('.')[1], page.get('query_id', page['query_ref']))
                if identity in seen:
                    continue
                seen.add(identity)
                first = next(value for value in tool_results if value.get('kind') == 'messages'
                             and (value['query_ref'].split('.')[1], value.get('query_id')) == identity)
                item = tools.public_query(first)
                item.update(page=1, has_more=bool(first.get('next_cursor')),
                            source_url=_source_url(first['query_ref'], 1))
                item['next_source_url'] = _source_url(first['query_ref'], 2, first.get('next_cursor')) \
                    if first.get('next_cursor') else None
            data.append(item)
            continue
        page = next(value for value in tool_results if value.get('query_ref') == item['query_ref'])
        identity = (page['query_ref'].split('.')[1], page.get('query_id', page['query_ref']))
        if identity in seen:
            continue
        seen.add(identity)
        found = list({value['query_ref']: value for value in tool_results
                      if value.get('kind') == 'window_messages'
                      and (value['query_ref'].split('.')[1], value.get('query_id')) == identity}.values())
        source = windows.source({'tool_results': found}, {'page_refs': [value['query_ref'] for value in found]})
        source.update(messages=found[0]['messages'], page=1, has_more=len(found) > 1,
                      source_url=f"/assistant/sources/{found[0]['query_ref']}?page=1")
        source['next_source_url'] = _source_url(found[0]['query_ref'], 2) if len(found) > 1 else None
        data.append(source)
    result['data'] = data
    result['data_refs'] = [item['query_ref'] for item in data if item.get('query_ref')]
    if not result.get('window'):
        result['window'] = next((item['window'] for item in reversed(data) if item.get('window')), None)
    return result


def _source_url(reference, page, cursor=None):
    return f'/assistant/sources/{reference}?' + urlencode({'page': page, **({'cursor': cursor} if cursor else {})})


def source_page(conn, actor, reference, settings, *, page=1, cursor=None):
    """Read a saved source page without changing assistant jobs or invoking a model."""
    read_cache = {}
    actor = _actor(conn, actor)
    first = tools._saved_query(conn, actor, reference, settings)
    if first.get('kind') not in {'window_messages', 'messages'} or not isinstance(page, int) or page < 1:
        raise ValueError('来源页无效。')
    row = _turn(conn, actor, reference.split('.')[1])
    checkpoint = codec.decode(row['checkpoint_ciphertext'], settings) or {}
    context = _context(conn, row, checkpoint, settings, read_cache=read_cache)
    if first['kind'] == 'messages' and cursor is not None:
        tools._validate_query(conn, actor, context, first, settings, read_cache=read_cache)
        args = {**first['_request']['args'], 'cursor': cursor}
        source_context = tools.effective_context(conn, actor, context, args, read_cache=read_cache)
        value = tools.assistant_search.query(conn, actor, source_context, args, settings, read_cache=read_cache)
        if value['query_id'] != first['query_id']:
            raise ValueError('检索范围已变化。')
        _, turn_id, digest = reference.split('.')
        for message in value['messages']:
            signed = tools.assistant_search._open(message['message_ref'], 'm.', settings)
            source = tools.assistant_search._seal({'query_ref': reference, 'chat_id': signed['chat_id'],
                'message_id': signed['message_id'], 'version': signed['version']}, 'p.', settings)
            ref = f'u.{turn_id}.{digest}.{source}'
            message.update(message_ref=ref, evidence_ref=ref)
        value = tools.public_query(value)
        value.update(query_ref=reference, page=page, has_more=bool(value['next_cursor']),
                     source_url=_source_url(reference, page, cursor),
                     next_source_url=_source_url(reference, page + 1, value['next_cursor'])
                     if value['next_cursor'] else None)
        return value
    found = list({value['query_ref']: value for value in checkpoint.get('tool_results', [])
                  if value.get('kind') == first['kind'] and value.get('query_id') == first.get('query_id')
                  and value['query_ref'].split('.')[1] == reference.split('.')[1]}.values())
    if page > len(found):
        raise ValueError('来源页超出范围。')
    selected = found[page - 1]
    tools.validate_tool_results(conn, actor, row, found, settings, context, read_cache=read_cache)
    if first['kind'] == 'window_messages':
        value = windows.source({'tool_results': found}, {'page_refs': [item['query_ref'] for item in found]})
    else:
        value = tools.public_query(selected)
    value.update(messages=selected['messages'], page=page, page_count=len(found), has_more=page < len(found),
                 source_url=f'/assistant/sources/{reference}?page={page}')
    if first['kind'] == 'messages':
        value.update(has_more=bool(selected.get('next_cursor')),
                     next_source_url=_source_url(reference, page + 1, selected.get('next_cursor'))
                     if selected.get('next_cursor') else None)
    else:
        value['next_source_url'] = _source_url(reference, page + 1) if page < len(found) else None
    return value


def turn_view(conn, actor, turn_id, settings, *, read_cache=None):
    read_cache = {} if read_cache is None else read_cache
    row = _turn(conn, actor, turn_id)
    checkpoint = codec.decode(row['checkpoint_ciphertext'], settings) or {}
    result, error = None, ERRORS.get(row['error_code'])
    error_code = row['error_code']
    try:
        context = _context(conn, row, checkpoint, settings, read_cache=read_cache)
        result = _result(conn, row, checkpoint, settings, context=context, read_cache=read_cache)
        scope = context.get('scope', {})
    except tools.SourceChangedError:
        scope, error = {}, ERRORS['source_changed']
        error_code = 'source_changed'
    except (PermissionError, LookupError, ValueError):
        scope, error = {}, ERRORS['permission']
        error_code = 'permission'
    return {'id': str(row['id']), 'session_id': str(row['session_id']),
            'client_request_id': str(row['client_request_id']),
            'message': '' if error_code == 'permission' else codec.decode(row['input_ciphertext'], settings)['message'],
            'status': row['status'], 'scope': scope,
            'progress': '' if error_code in {'permission', 'source_changed'} else checkpoint.get('progress', ''),
            'result': result, 'error': error,
            'error_code': error_code}


def session_view(conn, actor, session_id, settings):
    actor = _actor(conn, actor)
    session_id = UUID(str(session_id))
    if not conn.execute('SELECT 1 FROM assistant_sessions WHERE id=%s AND actor_id=%s',
                        (session_id, actor)).fetchone():
        raise LookupError('对话不存在或无权查看。')
    ids = conn.execute("SELECT id FROM assistant_turns WHERE session_id=%s "
                       "AND error_code IS DISTINCT FROM 'cleared' ORDER BY created_at,id",
                       (session_id,)).fetchall()
    read_cache = {}
    return {'id': str(session_id), 'turns': [turn_view(conn, actor, row['id'], settings, read_cache=read_cache)
                                          for row in ids]}


def clear_conversations(conn, actor, settings):
    """Clear only this actor's chat content; keep billing and request IDs for replay safety."""
    actor = _actor(conn, actor)
    sessions = conn.execute('SELECT id FROM assistant_sessions WHERE actor_id=%s FOR UPDATE', (actor,)).fetchall()
    turns = conn.execute('''SELECT t.* FROM assistant_turns t JOIN assistant_sessions s ON s.id=t.session_id
        WHERE s.actor_id=%s AND t.error_code IS DISTINCT FROM 'cleared' FOR UPDATE OF t''', (actor,)).fetchall()
    if any(turn['status'] == 'running' and turn['lease_until'] and turn['lease_until'] > datetime.now(UTC)
           for turn in turns):
        raise ValueError('仍有请求正在处理，请完成或停止后再清空。')
    for turn in turns:
        if turn['status'] in ACTIVE:
            cancel(conn, actor, turn['id'], settings)
        else:
            _stop_dependents(conn, turn['id'])
    if turns:
        conn.execute('''UPDATE assistant_turns SET status='cancelled',error_code='cleared',lease_until=NULL,
            input_ciphertext=%s,checkpoint_ciphertext=%s,result_ciphertext=NULL,
            finished_at=COALESCE(finished_at,NOW()) WHERE id=ANY(%s::uuid[])''',
            (codec.encode({'message': '对话已清空', 'selected_context': {}}, settings),
             codec.encode({}, settings), [turn['id'] for turn in turns]))
    conn.execute('UPDATE assistant_sessions SET context_ciphertext=NULL,updated_at=NOW() WHERE actor_id=%s', (actor,))
    counts = {'sessions': len(sessions), 'turns': len(turns)}
    if turns:
        conn.execute('INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)',
                     (uuid4(), actor, 'assistant_conversations_cleared', json.dumps(counts)))
    return counts


def _stop_dependents(conn, turn_id):
    conn.execute('''UPDATE offhours_summary_tasks t SET
        error_message=CASE WHEN t.status='running' THEN '请求结果未知，未自动重复调用；请求已停止。'
            ELSE '请求已停止。' END,
        status=CASE WHEN t.status='running' THEN 'failed' ELSE 'cancelled' END,
        lease_until=NULL,finished_at=NOW()
        FROM offhours_summary_runs r WHERE r.id=t.run_id AND r.origin='assistant'
        AND r.assistant_turn_id=%s AND t.status IN ('queued','running')''', (turn_id,))


def cancel(conn, actor, turn_id, settings):
    row = _turn(conn, actor, turn_id, lock=True)
    if row['status'] in ACTIVE:
        checkpoint = codec.decode(row['checkpoint_ciphertext'], settings) or {}
        wait = checkpoint.get('waiting', {})
        if wait.get('kind') == 'reply':
            conn.execute('''UPDATE summary_reply_tasks SET status='failed',lease_until=NULL,
                error_message='请求已停止。' WHERE id=ANY(%s::uuid[]) AND actor_id=%s
                AND feature=%s AND status='queued' ''', (wait.get('ids', []), actor, FEATURE))
        conn.execute('''UPDATE assistant_turns SET status='cancelled',error_code='cancelled',
            lease_until=NULL,finished_at=NOW() WHERE id=%s''', (row['id'],))
        _stop_dependents(conn, row['id'])
    return turn_view(conn, actor, row['id'], settings)


def configuration_changed(conn):
    if configured(conn):
        return
    conn.execute('''UPDATE assistant_turns SET status='cancelled',error_code='disabled',
        lease_until=NULL,finished_at=NOW() WHERE status=ANY(%s)''', (list(ACTIVE),))
    from . import offhours_summary, summary_replies
    offhours_summary.stop_assistant_tasks(conn)
    summary_replies.stop_feature(conn, FEATURE)


def _fence(conn, job):
    row = conn.execute('''SELECT t.*,s.actor_id FROM assistant_turns t
        JOIN assistant_sessions s ON s.id=t.session_id
        WHERE t.id=%s AND t.status='running' AND t.lease_until=%s FOR UPDATE OF t''',
        (job['id'], job['lease_until'])).fetchone()
    if not row:
        raise LookupError('请求已停止或更新。')
    return dict(row)


def _save(conn, job, checkpoint, settings, *, status='queued', usage=None, response_id=None, final=None):
    row = _fence(conn, job)
    total = dict(row['usage'] or {})
    for key, count in (usage or {}).items():
        if isinstance(count, int) and count >= 0:
            total[key] = total.get(key, 0) + count
    conn.execute('''UPDATE assistant_turns SET checkpoint_ciphertext=%s,status=%s,lease_until=NULL,
        usage=%s::jsonb,response_id=COALESCE(%s,response_id),result_ciphertext=COALESCE(%s,result_ciphertext),
        finished_at=CASE WHEN %s='completed' THEN NOW() ELSE NULL END WHERE id=%s''',
        (codec.encode(checkpoint, settings), status, json.dumps(total), response_id,
         codec.encode({'final': final}, settings) if final is not None else None, status, job['id']))
    conn.execute('UPDATE assistant_sessions SET updated_at=NOW() WHERE id=%s', (row['session_id'],))


def _finish_error(job, settings, code, telemetry=None, *, exception=None):
    with connection() as conn, conn.transaction():
        try:
            row = _fence(conn, job)
        except LookupError:
            return
        total = dict(row['usage'] or {})
        for key, value in (telemetry or {}).get('usage', {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
        checkpoint = codec.decode(row['checkpoint_ciphertext'], settings) or {}
        receipt = checkpoint.get('response_received') or next(iter(reversed(checkpoint.get('receipts', []))), {})
        call_telemetry = telemetry or receipt.get('telemetry', {})
        call_usage = (telemetry or {}).get('usage', {}) if telemetry else receipt.get('usage', {})
        failed_call_usage = {key: value for key, value in call_usage.items()
            if key in ('input_tokens', 'output_tokens') and type(value) is int and value >= 0}
        request_id = call_telemetry.get('openai_request_id')
        location, seen, cause, error_chain = None, set(), exception, []
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            error_chain.append(type(cause).__name__)
            trace = cause.__traceback__
            while trace is not None:
                frame = trace.tb_frame.f_code
                if '/app/' in frame.co_filename.replace('\\', '/'):
                    location = {'file': frame.co_filename.replace('\\', '/').rsplit('/', 1)[-1],
                                'function': frame.co_name, 'line': trace.tb_lineno}
                trace = trace.tb_next
            cause = cause.__cause__ or (cause.__context__ if not cause.__suppress_context__ else None)
        checkpoint['failure'] = {'code': code, 'stage': checkpoint.get('progress'),
            'model_steps': checkpoint.get('model_steps'),
            'error_chain': error_chain[:8], 'request_started_at': checkpoint.get('request_started_at'),
            'response_latency_ms': call_telemetry.get('response_latency_ms'),
            'openai_request_id': request_id, 'error_type': type(exception).__name__ if exception else None,
            'response_status': call_telemetry.get('response_status'),
            'incomplete_reason': call_telemetry.get('incomplete_reason'), 'failed_call_usage': failed_call_usage,
            'location': location}
        conn.execute('''UPDATE assistant_turns SET status=%s,error_code=%s,usage=%s::jsonb,
            checkpoint_ciphertext=%s,
            response_id=COALESCE(%s,response_id),lease_until=NULL,finished_at=NOW() WHERE id=%s''',
            ('unknown' if code == 'unknown' else 'failed', code, json.dumps(total),
             codec.encode(checkpoint, settings), (telemetry or {}).get('response_id'), job['id']))
        _stop_dependents(conn, job['id'])
        logging.getLogger(__name__).warning('assistant_failed turn=%s code=%s type=%s location=%s stage=%s steps=%s',
            job['id'], code, checkpoint['failure']['error_type'], location,
            checkpoint.get('progress'), checkpoint.get('model_steps'))


def claim():
    from .config import get_settings
    settings = get_settings()
    with connection() as conn, conn.transaction():
        expired = conn.execute("SELECT * FROM assistant_turns WHERE status='running' AND lease_until<NOW() "
                               'FOR UPDATE SKIP LOCKED').fetchall()
        for row in expired:
            checkpoint = codec.decode(row['checkpoint_ciphertext'], settings) or {}
            sent = checkpoint.get('in_flight', False)
            conn.execute('''UPDATE assistant_turns SET status=%s,error_code=%s,lease_until=NULL,
                finished_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s''',
                ('unknown' if sent else 'queued', 'unknown' if sent else None, sent, row['id']))
        if not configured(conn):
            return None
        if not ai_dispatch.can_claim(conn):
            return None
        row = conn.execute('''SELECT t.*,s.actor_id FROM assistant_turns t
            JOIN assistant_sessions s ON s.id=t.session_id WHERE t.status='queued' AND t.scheduled_at<=NOW()
            ORDER BY t.scheduled_at,t.created_at FOR UPDATE OF t SKIP LOCKED LIMIT 1''').fetchone()
        if not row:
            return None
        lease = datetime.now(UTC) + timedelta(minutes=3)
        conn.execute("UPDATE assistant_turns SET status='running',lease_until=%s WHERE id=%s", (lease, row['id']))
        return {**dict(row), 'status': 'running', 'lease_until': lease}


def prepare(settings):
    with connection() as conn, conn.transaction():
        read_cache = {}
        row = conn.execute('''SELECT t.*,s.actor_id FROM assistant_turns t
            JOIN assistant_sessions s ON s.id=t.session_id
            WHERE t.status='waiting' AND t.scheduled_at<=NOW() ORDER BY t.created_at
            FOR UPDATE OF t SKIP LOCKED LIMIT 1''').fetchone()
        if not row or not configured(conn):
            return False
        row = dict(row)
        checkpoint = codec.decode(row['checkpoint_ciphertext'], settings)
        try:
            context = _context(conn, row, checkpoint, settings, read_cache=read_cache)
            result = tools.resume_wait(conn, row['actor_id'], row, checkpoint['waiting'], settings, context)
        except (PermissionError, LookupError, ValueError) as exc:
            code = 'permission' if isinstance(exc, PermissionError | LookupError) else 'source_changed'
            checkpoint['failure'] = {'code': code, 'stage': '读取分析结果',
                                     'model_steps': checkpoint.get('model_steps')}
            conn.execute("UPDATE assistant_turns SET status='failed',error_code=%s,"
                         "checkpoint_ciphertext=%s,finished_at=NOW() "
                         "WHERE id=%s",
                         (code, codec.encode(checkpoint, settings), row['id']))
            _stop_dependents(conn, row['id'])
            return True
        if result is None:
            waiting = checkpoint['waiting']
            counts = conn.execute("""SELECT count(*) total,
                count(*) FILTER (WHERE status IN ('succeeded','failed','cancelled')) completed
                FROM offhours_summary_tasks WHERE run_id=ANY(%s::uuid[]) AND is_current""",
                (waiting.get('ids', []),)).fetchone() if waiting.get('kind') == 'summary' else None
            checkpoint['progress'] = ({'stage': 'summarize', 'message': '正在分析记录',
                'conversations_completed': counts['completed'], 'conversations_total': counts['total']}
                if counts else '正在分析记录')
            conn.execute('UPDATE assistant_turns SET scheduled_at=NOW()+INTERVAL \'1 second\',checkpoint_ciphertext=%s '
                         'WHERE id=%s', (codec.encode(checkpoint, settings), row['id']))
            return False
        call = checkpoint['pending_calls'][0]
        if checkpoint.get('waiting_cache_key'):
            checkpoint.setdefault('tool_cache', {})[checkpoint.pop('waiting_cache_key')] = result
        _accept_tool(checkpoint, call, result)
        checkpoint.pop('waiting', None)
        checkpoint['progress'] = '整理结果'
        conn.execute("UPDATE assistant_turns SET status='queued',scheduled_at=NOW(),checkpoint_ciphertext=%s "
                     "WHERE id=%s",
                     (codec.encode(checkpoint, settings), row['id']))
        return True


def _accept_tool(checkpoint, call, result, *, model_result=None, record_result=True, cached=False):
    checkpoint['no_progress_calls'] = (checkpoint.get('no_progress_calls', 0) + 1
                                       if cached or result.get('error_code') else 0)
    checkpoint['tool_outputs'][call['call_id']] = result
    if record_result:
        checkpoint['tool_results'].append(result)
    supplied = model_result if model_result is not None else tools.public_query(result, for_model=True)
    checkpoint['items'].append({'type': 'function_call_output', 'call_id': call['call_id'],
                               'output': json.dumps(supplied, ensure_ascii=False, default=str)})
    checkpoint['pending_calls'].pop(0)


def _complete_window(checkpoint, model_result):
    job = checkpoint.pop('window_job')
    first = windows.pages(checkpoint, job)[0]
    first['_window_model_result'] = model_result
    key = json.dumps(['summarize_messages', {**first['_request']['args'], 'cursor': None}],
                     sort_keys=True, ensure_ascii=False)
    checkpoint.setdefault('tool_cache', {})[key] = first
    _accept_tool(checkpoint, job['call'], first, model_result=model_result, record_result=False)
    checkpoint.setdefault('window_refs', []).append(first['query_ref'])
    checkpoint.setdefault('raw_window_calls', []).append(job['call']['call_id'])
    checkpoint['window_final_pending'] = True


def _collect_window(conn, row, checkpoint, settings, context, *, read_cache=None):
    window = checkpoint['window_job']
    current_turn = {**row, 'checkpoint_ciphertext': codec.encode(checkpoint, settings)}
    previous = windows.pages(checkpoint, window)
    counts = {key: previous[0][key] for key in ('total_messages', 'total_conversations')} if previous else None
    result = tools.execute(conn, row['actor_id'], current_turn, 'summarize_messages',
                           window['args'], settings, context, read_cache=read_cache, window_counts=counts)
    window['task'] = {'question': codec.decode(row['input_ciphertext'], settings)['message'],
                      'scope': result['scope'], 'window': result['window']}
    try:
        if windows.add_page(checkpoint, window, result):
            source_context = tools.effective_context(conn, row['actor_id'], context, window['args'],
                                                     read_cache=read_cache)
            cm, _roster, chats, speakers, _selected = tools.assistant_search._scope(
                conn, row['actor_id'], source_context, None, window['args'].get('chat_ids'), settings, 'all',
                read_cache=read_cache)
            fresh = tools.assistant_search._totals(conn, cm['id'], [chat['chat_id'] for chat in chats],
                tools.assistant_search._time(result['window']['start']),
                tools.assistant_search._time(result['window']['end']), speakers)
            if any(result[key] != fresh[key] for key in ('total_messages', 'total_conversations')):
                raise ValueError('时段消息数量已变化，请重新读取。')
            supplied = windows.prepare(checkpoint, window)
            if supplied is not None:
                _complete_window(checkpoint, supplied)
    except ValueError as exc:
        raise tools.SourceChangedError(str(exc)) from exc
    count = sum(len(value['messages']) for value in windows.pages(checkpoint, window))
    checkpoint['progress'] = {'stage': 'collect', 'message': '正在读取消息',
        'messages': count, 'total_messages': result['total_messages'],
        'conversations': result['total_conversations'], 'pages': len(window['page_refs'])}
    if window.get('phase') != 'collect' or 'window_job' not in checkpoint:
        checkpoint['progress']['message'] = '正在按会话整理消息'
        checkpoint['progress']['stage'] = 'summarize'


def _compact_window_inputs(checkpoint):
    calls = set(checkpoint.pop('raw_window_calls', []))
    for item in checkpoint['items']:
        if item.get('type') == 'function_call_output' and item.get('call_id') in calls:
            supplied = json.loads(item['output'])
            compact = windows.metadata(supplied)
            if supplied.get('summaries') and len(windows.encoded(supplied['summaries'])) <= windows.INPUT_CHARS:
                compact['summaries'] = supplied['summaries']
            item['output'] = windows.encoded(compact)


def _saved_window_material(checkpoint, references, *, turn_id=None):
    for ref in reversed(references):
        first = next((item for item in checkpoint.get('tool_results', []) if item.get('query_ref') == ref), None)
        if (not first or first.get('kind') != 'window_messages'
                or (turn_id and ref.split('.')[1:2] != [str(turn_id)])):
            continue
        material = first.get('_window_model_result', {})
        if (material.get('query_ref') != ref or material.get('scope', {}).get('audience') != 'all'
                or not material.get('coverage', {}).get('scan_complete')
                or len(windows.encoded(material)) > windows.INPUT_CHARS):
            continue
        identity = (ref.split('.')[1], first.get('query_id'))
        found = list({item['query_ref']: item for item in checkpoint.get('tool_results', [])
                      if item.get('kind') == 'window_messages'
                      and (item['query_ref'].split('.')[1], item.get('query_id')) == identity}.values())
        source = windows.source({'tool_results': found}, {'page_refs': [item['query_ref'] for item in found]})
        if (source['coverage']['scan_complete'] and all(material.get(key) == source.get(key)
                for key in ('scope', 'window', 'total_messages', 'total_conversations', 'page_count'))):
            return material
    return None


def _historical_window_result(conn, previous, checkpoint, settings, context, *, read_cache=None):
    stored = codec.decode(previous['result_ciphertext'], settings) or {}
    final = stored.get('final', {})
    material = _saved_window_material(checkpoint, final.get('data_refs', []), turn_id=previous['id'])
    independent = bool(material and material.get('summaries'))
    if not material or (not independent and (final.get('event_refs') or final.get('result_refs')
                                            or final.get('draft_ref'))):
        return None
    try:
        original = checkpoint.get('tool_results', [])
        if independent:
            # Segment summaries saw only this window; the final answer can also depend on older sources.
            first = next(item for item in original if item.get('query_ref') == material['query_ref'])
            original = [item for item in original if item.get('kind') == 'window_messages'
                and item.get('query_ref', '').split('.')[1:2] == [str(previous['id'])]
                and item.get('query_id') == first.get('query_id')]
        else:
            _context(conn, previous, checkpoint, settings, read_cache=read_cache)
        for item in original:
            if item.get('kind') == 'window_messages':
                tools._validate_query(conn, previous['actor_id'], context, item, settings, history_only=True,
                                      read_cache=read_cache)
        tools.validate_tool_results(conn, previous['actor_id'], previous,
            [item for item in original if item.get('kind') != 'window_messages'], settings, context,
            read_cache=read_cache)
    except (PermissionError, LookupError, ValueError):
        return None
    # Revalidated old facts remain useful, but their stale query references cannot execute new actions.
    return {'answer': '' if independent else final['answer'], 'scope': material['scope'], 'window': material['window'],
            'checked_at': material.get('checked_at'), 'requires_refresh': True,
            'summaries': [{'summary': note['summary'], **({'items': [
                {'summary': item['summary']} for item in note['items']]} if note.get('items') else {})}
                for note in material.get('summaries', [])]}


def _history(conn, row, settings, context, *, read_cache=None):
    read_cache = {} if read_cache is None else read_cache
    history, results, seen = [], [], set()
    rows = conn.execute('''SELECT t.*,s.actor_id FROM assistant_turns t JOIN assistant_sessions s ON s.id=t.session_id
        WHERE t.session_id=%s AND t.id<>%s AND t.status='completed' ORDER BY t.created_at DESC LIMIT 6''',
        (row['session_id'], row['id'])).fetchall()
    for previous in reversed(rows):
        checkpoint = codec.decode(previous['checkpoint_ciphertext'], settings) or {}
        if checkpoint.get('context', {}).get('scope', {}).get('cm_id') != context['scope']['cm_id']:
            continue
        try:
            checked = _result(conn, previous, checkpoint, settings, read_cache=read_cache)
        except (PermissionError, LookupError, ValueError):
            checked = _historical_window_result(conn, previous, checkpoint, settings, context, read_cache=read_cache)
        if checked:
            coverage_changed = any(result.get('_coverage_refreshed') for result in checkpoint.get('tool_results', []))
            material = _saved_window_material(checkpoint, checked.get('data_refs', []))
            history.append({'role': 'user', 'content': codec.decode(previous['input_ciphertext'], settings)['message']})
            history.append({'role': 'assistant', 'content': json.dumps({
                'answer': checked['answer'], 'events': checked.get('events', [])[:25],
                'scope': checked.get('scope', {}), 'window': checked.get('window'),
                'checked_at': checked.get('checked_at') or next((item.get('checked_at')
                    for item in reversed(checked.get('data', [])) if item.get('checked_at')), None),
                'coverage_refreshed': coverage_changed, 'requires_refresh': checked.get('requires_refresh', False),
                'summaries': checked.get('summaries', [] if checked.get('sections')
                                        else material.get('summaries', []) if material else []),
                'section_index': [{'index': index + 1, 'title': section['title'],
                    'summary': section['summary'], 'message_refs': section['message_refs'],
                    'sources': [{key: source.get(key) for key in (
                        'message_ref', 'chat_id', 'conversation_name', 'sender_name', 'time')}
                        for source in section['sources']]}
                    for index, section in enumerate(checked.get('sections', []))],
                'event_count': len(checked.get('events', [])),
                'event_index': [{'index': index + 1, 'event_ref': event['event_ref'], 'title': event['title']}
                                for index, event in enumerate(checked.get('events', []))],
                'result_refs': checked.get('result_refs', []), 'draft': checked.get('draft'),
                'data_refs': checked.get('data_refs', []),
                'data': [windows.metadata(data) for data in checked.get('data', [])],
            }, ensure_ascii=False, default=str)})
            if checked.get('requires_refresh'):
                continue
            refs = (set(checked.get('result_refs', [])) | set(checked.get('event_refs', []))
                    | set(checked.get('data_refs', [])))
            if checked.get('draft_ref'):
                refs.add(checked['draft_ref'])
            message_refs = {ref for section in checked.get('sections', []) for ref in section['message_refs']}
            own = list(checkpoint.get('tool_outputs', {}).values())
            query_ids = {(result['query_ref'].split('.')[1], result['query_id'])
                         for result in checkpoint.get('tool_results', []) if result.get('query_id')
                         and (result.get('query_ref') in refs or any(message.get('message_ref') in message_refs
                              for message in result.get('messages', [])))}
            retained = own + [result for result in checkpoint.get('tool_results', [])
                              if any(result.get(key) in refs
                                     for key in ('result_id', 'event_ref', 'draft_ref', 'query_ref'))
                              or (result.get('query_ref', '').split('.')[1:2]
                                  and (result['query_ref'].split('.')[1], result.get('query_id')) in query_ids)
                              or any(message.get('message_ref') in message_refs
                                     for message in result.get('messages', []))
                              or any(event.get('event_ref') in refs for event in result.get('events', []))]
            for result in retained:
                signature = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
                if signature not in seen:
                    seen.add(signature)
                    results.append(result)
    return history, results


def _apply_response(conn, row, job, checkpoint, settings, context, *, read_cache=None):
    response = checkpoint.pop('response_received')
    if checkpoint.pop('summary_request', None):
        window = checkpoint['window_job']
        _, allowed_refs = windows.summary_input(checkpoint, window)
        try:
            complete = windows.accept_summary(window, response, allowed_refs)
        except ValueError as exc:
            raise api.ModelOutputError('invalid window summary') from exc
        if complete:
            supplied = windows.reduce_or_finish(checkpoint, window)
            if supplied is not None:
                _complete_window(checkpoint, supplied)
        checkpoint['progress'] = {'stage': 'summarize', 'message': '正在按会话整理消息',
            'segments_completed': window['chunk_index'], 'segments_total': len(window['chunks'])}
        _save(conn, job, checkpoint, settings)
        return
    _compact_window_inputs(checkpoint)
    checkpoint['items'].extend(response['output'])
    calls = [item for item in response['output'] if item.get('type') == 'function_call']
    if checkpoint.pop('final_only', False) and calls:
        raise api.ModelOutputError('tool call after retrieval stopped')
    if calls:
        if len(calls) > 10 or any(not isinstance(c.get('call_id'), str) or not isinstance(c.get('arguments'), str)
                                for c in calls):
            raise api.ModelOutputError('invalid calls')
        checkpoint['pending_calls'] = calls
        checkpoint['progress'] = '读取记录'
        _save(conn, job, checkpoint, settings)
    else:
        try:
            final = json.loads(response['text'])
        except ValueError as exc:
            raise api.ModelOutputError('invalid final JSON') from exc
        if not isinstance(final, dict):
            raise api.ModelOutputError('invalid final shape')
        if checkpoint.get('window_refs'):
            final['data_refs'] = list(dict.fromkeys([*final.get('data_refs', []), *checkpoint['window_refs']]))
        try:
            result = tools.build_result(conn, row['actor_id'], row, final, settings, context,
                                        checkpoint['tool_results'], read_cache=read_cache)
        except tools.SourceChangedError:
            raise
        except ValueError as exc:
            raise api.ModelOutputError('invalid final references') from exc
        checkpoint['progress'] = ''
        _save(conn, job, checkpoint, settings, status='completed', final=final)
        selection = {'cm_id': result['scope']['cm_id']}
        conn.execute('UPDATE assistant_sessions SET context_ciphertext=%s WHERE id=%s',
                     (codec.encode(selection, settings), row['session_id']))


def step(job, settings):
    with connection() as conn, conn.transaction():
        read_cache = {}
        row = _fence(conn, job)
        checkpoint = codec.decode(row['checkpoint_ciphertext'], settings)
        if 'rules' not in checkpoint:
            checkpoint['rules'] = rules.builtin_snapshot()
        context = _context(conn, row, checkpoint, settings, read_cache=read_cache)
        if not configured(conn):
            raise PermissionError('助手已停止。')
        if checkpoint.get('response_received'):
            _apply_response(conn, row, job, checkpoint, settings, context, read_cache=read_cache)
            return
        window = checkpoint.get('window_job')
        if window and window['phase'] == 'collect':
            _collect_window(conn, row, checkpoint, settings, context, read_cache=read_cache)
            _save(conn, job, checkpoint, settings)
            return
        if checkpoint['pending_calls'] and not window:
            while checkpoint['pending_calls']:
                call = checkpoint['pending_calls'][0]
                was_cached = False
                try:
                    args = json.loads(call['arguments'])
                    if call['name'] == 'summarize_messages' and args.get('audience') == 'all':
                        tools._validate('summarize_messages', args)
                        start, end = tools._window(args['start'], args['end'], row['created_at'])
                        args.update(start=start.isoformat(), end=end.isoformat(), cursor=None, creator_ids=None)
                        if args.get('chat_ids') is not None:
                            args['chat_ids'] = sorted(set(args['chat_ids']))
                        key = json.dumps([call['name'], args], sort_keys=True, ensure_ascii=False)
                        cached_window = checkpoint.setdefault('tool_cache', {}).get(key)
                        if cached_window and cached_window.get('_window_model_result'):
                            pages = [value for value in checkpoint['tool_results']
                                     if value.get('query_id') == cached_window['query_id']
                                     and value.get('query_ref', '').split('.')[1:2] == [str(row['id'])]]
                            tools.validate_tool_results(conn, row['actor_id'], row, pages, settings, context,
                                                        read_cache=read_cache)
                            _accept_tool(checkpoint, call, cached_window,
                                         model_result=cached_window['_window_model_result'], record_result=False,
                                         cached=True)
                            checkpoint.setdefault('raw_window_calls', []).append(call['call_id'])
                            continue
                        checkpoint['window_job'] = windows.begin(call, args)
                        _collect_window(conn, row, checkpoint, settings, context, read_cache=read_cache)
                        _save(conn, job, checkpoint, settings)
                        return
                    cache_key = json.dumps([call['name'], args], sort_keys=True, ensure_ascii=False)
                    cached = checkpoint.setdefault('tool_cache', {}).get(cache_key)
                    if cached is not None:
                        was_cached = True
                        tools.validate_tool_results(conn, row['actor_id'], row, [cached], settings, context,
                                                    read_cache=read_cache)
                        result = cached
                    else:
                        current_turn = {**row, 'checkpoint_ciphertext': codec.encode(checkpoint, settings)}
                        result = tools.execute(conn, row['actor_id'], current_turn,
                                               call['name'], args, settings, context, read_cache=read_cache)
                        if not result.get('waiting'):
                            checkpoint['tool_cache'][cache_key] = result
                except (PermissionError, LookupError, ValueError) as exc:
                    window = checkpoint.get('window_job')
                    if window and (isinstance(exc, tools.SourceChangedError) or window.get('page_refs')):
                        raise
                    checkpoint.pop('window_job', None)
                    code = ('permission' if isinstance(exc, PermissionError) else
                            'source_changed' if isinstance(exc, tools.SourceChangedError) else
                            'source_not_found' if isinstance(exc, LookupError) else 'invalid_tool_input')
                    checkpoint.setdefault('tool_errors', []).append({'tool': call['name'], 'code': code})
                    result = {'error': str(exc), 'error_code': code}
                if result.get('waiting'):
                    checkpoint['waiting'] = result['waiting']
                    checkpoint['waiting_cache_key'] = cache_key
                    checkpoint['progress'] = '正在分析记录'
                    _save(conn, job, checkpoint, settings, status='waiting')
                    return
                _accept_tool(checkpoint, call, result, cached=was_cached)
            checkpoint['progress'] = '整理结果'
            _save(conn, job, checkpoint, settings)
            return
        if (not window and not checkpoint.get('window_final_pending')
                and checkpoint['model_steps'] >= MAX_MODEL_STEPS):
            final = {'answer': '本轮处理步骤已达上限，以下是已取得的结果；剩余问题可以继续提问。',
                     'event_refs': list(dict.fromkeys(event['event_ref']
                         for result in checkpoint['tool_results'] for event in result.get('events', []))),
                     'result_refs': list(dict.fromkeys(result['result_id']
                         for result in checkpoint['tool_results'] if result.get('result_id'))),
                     'data_refs': list(dict.fromkeys(result['query_ref']
                         for result in checkpoint['tool_results'] if result.get('query_ref'))),
                     'draft_ref': next((result['draft_ref'] for result in reversed(checkpoint['tool_results'])
                                        if result.get('draft_ref')), None)}
            tools.build_result(conn, row['actor_id'], row, final, settings, context, checkpoint['tool_results'],
                               read_cache=read_cache)
            checkpoint['progress'] = ''
            _save(conn, job, checkpoint, settings, status='completed', final=final)
            return
        if not checkpoint['items']:
            history, results = _history(conn, row, settings, context, read_cache=read_cache)
            checkpoint['tool_results'] = results
            checkpoint['followup_scope'] = json.loads(history[-1]['content']).get('scope', {}) if history else {}
            checkpoint['items'] = history + [{'role': 'user', 'content': json.dumps({
                'context': context, 'question': codec.decode(row['input_ciphertext'], settings)['message'],
            }, ensure_ascii=False, default=str)}]
        cfg = _config(conn)
        if window:
            segment, allowed_refs = windows.summary_input(checkpoint, window)
            tools.validate_tool_results(conn, row['actor_id'], row, windows.pages(checkpoint, window),
                settings, context, read_cache=read_cache, message_refs=set(allowed_refs.values()))
            model_items = [{'role': 'user', 'content': windows.encoded(segment)}]
            model_instructions, model_tools = rules.summary_instructions(checkpoint['rules']), []
            model_format = windows.summary_format(allowed_refs,
                                                   structured_items=window.get('structured_items', False))
            checkpoint['summary_request'] = True
            checkpoint['summary_steps'] = checkpoint.get('summary_steps', 0) + 1
        else:
            tools.validate_tool_results(conn, row['actor_id'], row, checkpoint['tool_results'], settings, context,
                                        read_cache=read_cache)
            model_items = checkpoint['items']
            model_instructions = rules.main_instructions(checkpoint['rules']) + '\n' + REPLY_INSTRUCTIONS
            model_tools, model_format = tools.tools_for(context), OUTPUT_FORMAT
            if checkpoint.get('no_progress_calls', 0) >= 3:
                checkpoint['final_only'] = True
                model_tools = []
                model_instructions += ('\n本轮连续补查未取得新资料，已结束检索。请依据已取得材料回答并说明'
                    '真正缺少的依据；不把没有材料当作没有发生，不再请求工具，不编造结论。')
            checkpoint['model_steps'] += 1
            checkpoint.pop('window_final_pending', None)
        checkpoint['in_flight'] = True
        checkpoint['request_started_at'] = datetime.now(UTC).isoformat()
        checkpoint['progress'] = ({'stage': 'summarize', 'message': '正在按会话整理消息',
            'segments_completed': window['chunk_index'], 'segments_total': len(window['chunks'])} if window else
            '理解问题' if checkpoint['model_steps'] == 1 else '整理结果')
        conn.execute('UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s',
                     (codec.encode(checkpoint, settings), row['id']))
    key = api_key_cipher(settings.token_encryption_key).decrypt(cfg['api_key_ciphertext'])
    response = api.call_openai_tools(cfg, key, model_items, row['actor_id'],
        instructions=model_instructions, tools=model_tools, output_format=model_format,
        max_output_tokens=16000 if window else 8000)
    with connection() as conn, conn.transaction():
        try:
            row = _fence(conn, job)
        except LookupError:
            row = conn.execute('''SELECT * FROM assistant_turns WHERE id=%s
                AND status IN ('cancelled','failed','unknown') FOR UPDATE''', (job['id'],)).fetchone()
            if not row:
                return
            saved = codec.decode(row['checkpoint_ciphertext'], settings) or {}
            receipts = saved.setdefault('receipts', [])
            if any(r.get('response_id') == response.get('response_id') for r in receipts):
                return
            receipts.append({key: response.get(key) for key in ('response_id', 'usage', 'telemetry')})
            saved['in_flight'] = False
            total = dict(row['usage'] or {})
            for name, value in response.get('usage', {}).items():
                if isinstance(value, int) and value >= 0:
                    total[name] = total.get(name, 0) + value
            conn.execute('''UPDATE assistant_turns SET checkpoint_ciphertext=%s,usage=%s::jsonb,
                response_id=%s WHERE id=%s''',
                (codec.encode(saved, settings), json.dumps(total), response.get('response_id'), job['id']))
            return
        checkpoint['in_flight'] = False
        checkpoint.setdefault('receipts', []).append({
            'response_id': response.get('response_id'), 'usage': response.get('usage', {}),
            'telemetry': response.get('telemetry', {}),
        })
        checkpoint['response_received'] = response
        total = dict(row['usage'] or {})
        for name, value in response.get('usage', {}).items():
            if isinstance(value, int) and value >= 0:
                total[name] = total.get(name, 0) + value
        conn.execute('''UPDATE assistant_turns SET checkpoint_ciphertext=%s,usage=%s::jsonb,
            response_id=%s WHERE id=%s''',
            (codec.encode(checkpoint, settings), json.dumps(total), response.get('response_id'), row['id']))
    with connection() as conn, conn.transaction():
        row = _fence(conn, job)
        read_cache = {}
        context = _context(conn, row, checkpoint, settings, read_cache=read_cache)
        _apply_response(conn, row, job, checkpoint, settings, context, read_cache=read_cache)


def run(job, settings):
    try:
        step(job, settings)
    except api.OpenAIRequestError as exc:
        code = 'unknown' if exc.request_unknown else 'quota' if exc.is_quota else 'provider_error'
        _finish_error(job, settings, code, exc.telemetry, exception=exc)
        if exc.retry_after_seconds is not None and not exc.is_quota:
            ai_dispatch.throttle(exc.retry_after_seconds)
    except api.ModelOutputError as exc:
        _finish_error(job, settings, 'invalid_output', exc.telemetry, exception=exc)
    except tools.SourceChangedError as exc:
        _finish_error(job, settings, 'source_changed', exception=exc)
    except PermissionError as exc:
        _finish_error(job, settings, 'permission', exception=exc)
    except LookupError:
        return
    except Exception as exc:
        _finish_error(job, settings, 'internal', exception=exc)
