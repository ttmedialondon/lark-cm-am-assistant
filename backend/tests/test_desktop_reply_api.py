import asyncio
import copy
import json
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ai_dispatch, desktop_replies, desktop_reply_routes, main, openai_client
from app.ai_feature_config import api_key_cipher
from app.desktop_reply_input import MAX_REQUEST_BYTES

PATH = "/v1/reply-suggestions"


def payload(scope="selected_messages"):
    return {
        "schema_version": "1.1", "request_id": str(uuid4()), "captured_at": "2026-10-04T08:20:00Z",
        "conversation": {"label": "Unverified target", "label_source": "accessibility_layout",
                         "identity_verified": False},
        "reply_scope": scope,
        "selected_message_text": "好的\n好的" if scope == "selected_messages" else "",
        "selected_messages": [{"ordinal": 1, "text": "好的"}, {"ordinal": 2, "text": "好的"}]
        if scope == "selected_messages" else [],
        "selection_source": "clipboard_html" if scope == "selected_messages" else "none",
        "conversation_context": {
            "capture_id": str(uuid4()), "captured_at": "2026-10-04T08:19:59Z", "source": "lark_accessibility",
            "scope": "visible_text", "complete_history": False,
            "messages": [{"ordinal": 1, "text": "Can you check the dates?"}, {"ordinal": 2, "text": "好的"}],
        },
    }


def model_response(draft="I will check the dates.", language="en", refs=None):
    return {"text": json.dumps({
        "outcome": "ready", "context_reason": "none", "language": language, "draft": draft,
        "chinese_translation": draft if language == "zh" else "我会核对日期。",
        "explanation": "根据本次可见文字，回应核对日期的诉求。", "checks": [], "evidence_refs": refs or ["C1"],
    }, ensure_ascii=False), "usage": {"input_tokens": 30, "output_tokens": 20},
        "telemetry": {"http_status": 200, "openai_request_id": "req_desktop_fixture",
                      "response_id": "resp_desktop_fixture", "response_status": "completed",
                      "body": "PRIVATE RESPONSE", "headers": {"authorization": "sk-fixture-only-test"}}}


def provider_logs(caplog):
    return [json.loads(record.message.removeprefix("desktop_reply_provider ")) for record in caplog.records
            if record.message.startswith("desktop_reply_provider ")]


@pytest.fixture
def api(pg, monkeypatch):
    from app import reply_rules

    settings = SimpleNamespace(token_encryption_key=b"d" * 32)
    for module in (main, desktop_reply_routes, desktop_replies):
        monkeypatch.setattr(module, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(desktop_reply_routes, "get_settings", lambda: settings)
    actor, other_actor = uuid4(), uuid4()
    for actor_id, token, name in ((actor, "desktop-session", "Trusted CM"),
                                 (other_actor, "other-session", "Other CM")):
        pg.execute("INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext) VALUES(%s,%s,%s,%s)",
                   (actor_id, str(actor_id), name, "fixture-lark-token"))
        pg.execute("INSERT INTO web_sessions(session_hash,authorization_id,expires_at) "
                   "VALUES(%s,%s,NOW()+INTERVAL '1 day')", (main._sha256(token), actor_id))
    pg.execute(
        "INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix) "
        "VALUES('desktop_reply','openai','fixture-project','fixture-model',TRUE,%s,'test') "
        "ON CONFLICT(feature) DO UPDATE SET enabled=TRUE,"
        "api_key_ciphertext=EXCLUDED.api_key_ciphertext,key_suffix='test'",
        (api_key_cipher(settings.token_encryption_key).encrypt("sk-fixture-only-test"),),
    )
    state = SimpleNamespace(calls=[], output=None)
    state.catalog = reply_rules.catalog
    # Existing contract checks exercise the supported no-optional-documents path.
    monkeypatch.setattr(reply_rules, 'catalog', lambda _: [])

    async def model(config, api_key, pack, contact_id, **kwargs):
        assert api_key == "sk-fixture-only-test"
        state.calls.append({"pack": pack, "actor": contact_id, "options": kwargs})
        refs = [message["ref"] for message in pack["selected_messages"]] or ["C1"]
        return state.output or model_response(draft=f"Reply for {contact_id}", refs=refs)

    monkeypatch.setattr(openai_client, "call_openai_async", model)
    app = FastAPI()
    app.include_router(desktop_reply_routes.create_router(
        current_authorization=main._current_authorization, csrf_token=lambda _: "fixture-csrf",
    ))
    return SimpleNamespace(
        pg=pg, app=app, client=TestClient(app), actor=actor, other_actor=other_actor, state=state, settings=settings,
        headers={"Authorization": "Bearer desktop-session"},
        other_headers={"Authorization": "Bearer other-session"},
        cookie_headers={"Cookie": f"{main.SESSION_COOKIE}=desktop-session", "X-CSRF-Token": "fixture-csrf"},
    )


def test_document_navigation_pins_facts_and_rechecks_private_cached_drafts(api, monkeypatch, caplog):
    from app import assistant_rules, reply_rules

    monkeypatch.setattr(reply_rules, 'catalog', api.state.catalog)
    api.pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (api.actor,))
    secret = 'PRIVATE POLICY: standard sample handling takes two working days.'
    document = assistant_rules.save_document(api.pg, api.settings, api.actor, None,
        'Sample policy', 'Answer questions about sample handling', 'knowledge', 'published', 'owner',
        secret, 0, reply_usable=True)
    assistant_rules.save_document(api.pg, api.settings, api.actor, None,
        'Unapproved secret', 'Not available to reply generation', 'knowledge', 'published', 'owner',
        'UNAPPROVED PRIVATE FACT', 0)
    caplog.set_level(logging.INFO, logger='app.desktop_reply_routes')
    calls = []

    async def model(config, key, pack, actor, **options):
        calls.append((copy.deepcopy(pack), options))
        assert actor == api.actor and key == 'sk-fixture-only-test'
        if 'document_ids' in options['output_format']['schema']['properties']:
            assert 'Sample policy' in options['instructions'] and secret not in options['instructions']
            assert 'Unapproved secret' not in options['instructions']
            assert 'business_documents' not in pack
            return {'text': json.dumps({'document_ids': [document]}),
                    'usage': {'input_tokens': 12, 'output_tokens': 3},
                    'telemetry': {'http_status': 200, 'response_status': 'completed',
                                  'response_id': 'resp_document_navigation'}}
        assert pack['business_documents'] == [{'key': document, 'title': 'Sample policy', 'text': secret,
                                                'revision': 1, 'document_ref': 'doc.' + document}]
        assert 0 < options['total_timeout'] <= calls[0][1]['total_timeout'] <= 45
        return model_response('Samples usually take two working days.', refs=['S1', 'C1'])

    monkeypatch.setattr(openai_client, 'call_openai_async', model)
    submitted = payload()
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == 200 and len(calls) == 2
    assert set(first.json()) == {'request_id', 'suggestion', 'chinese_translation', 'english_translation', 'is_mock'}
    assert '_reply_rules' not in first.text and 'PRIVATE POLICY' not in first.text
    assert calls[0][0]['selected_messages'] == calls[1][0]['selected_messages']
    receipt = api.pg.execute('SELECT * FROM desktop_reply_requests WHERE request_id=%s',
                             (submitted['request_id'],)).fetchone()
    assert receipt['usage'] == {'input_tokens': 42, 'output_tokens': 23}
    stored = json.loads(desktop_reply_routes.store.result_cipher(api.settings.token_encryption_key).decrypt(
        receipt['result_ciphertext']))
    assert stored['_reply_rules']['document_ids'] == [document]
    assert stored['_reply_rules']['snapshot']['documents'][document]['text'] == secret
    cached = api.client.post(PATH, json=submitted, headers=api.headers)
    assert cached.json() == first.json() and len(calls) == 2
    api.pg.execute('UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s', (api.actor,))
    denied = api.client.post(PATH, json=submitted, headers=api.headers)
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'reply_rules_unavailable'
    assert 'PRIVATE' not in denied.text and len(calls) == 2
    api.pg.execute('UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s', (api.actor,))
    api.pg.execute('UPDATE assistant_rule_overrides SET reply_usable=FALSE WHERE rule_key=%s', (document,))
    assert api.client.post(PATH, json=submitted, headers=api.headers).status_code == 403 and len(calls) == 2
    # Old encrypted results have no document metadata and are not regenerated during release.
    stored.pop('_reply_rules')
    api.pg.execute('UPDATE desktop_reply_requests SET result_ciphertext=%s WHERE request_id=%s', (
        desktop_reply_routes.store.result_cipher(api.settings.token_encryption_key).encrypt(json.dumps(stored)),
        submitted['request_id']))
    assert api.client.post(PATH, json=submitted, headers=api.headers).json() == first.json() and len(calls) == 2
    assert 'PRIVATE POLICY' not in caplog.text and 'UNAPPROVED PRIVATE FACT' not in caplog.text

    api.pg.execute('UPDATE assistant_rule_overrides SET reply_usable=TRUE WHERE rule_key=%s', (document,))
    async def fail_after_navigation(config, key, pack, actor, **options):
        if 'document_ids' in options['output_format']['schema']['properties']:
            return await model(config, key, pack, actor, **options)
        calls.append((copy.deepcopy(pack), options))
        failure = openai_client.OpenAIRequestError('PRIVATE provider error',
                                                  telemetry={'usage': {'input_tokens': 7}})
        raise failure from httpx.ReadTimeout('PRIVATE timeout')

    monkeypatch.setattr(openai_client, 'call_openai_async', fail_after_navigation)
    failed = payload()
    first = api.client.post(PATH, json=failed, headers=api.headers)
    duplicate = api.client.post(PATH, json=failed, headers=api.headers)
    assert first.status_code == duplicate.status_code == 504 and first.json() == duplicate.json()
    assert len(calls) == 4
    receipt = api.pg.execute('SELECT status,usage FROM desktop_reply_requests WHERE request_id=%s',
                             (failed['request_id'],)).fetchone()
    assert receipt == {'status': 'failed', 'usage': {'input_tokens': 19, 'output_tokens': 3}}
    assert provider_logs(caplog)[-1]['request_unknown']


def test_reply_scopes_translation_cached_id_and_actor_isolation(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="app.desktop_reply_routes")
    selected = payload()
    selected["conversation"]["label"] = "Other CM"
    first = api.client.post(PATH, json=selected, headers=api.headers)
    assert first.status_code == 200
    result = first.json()
    assert result == {"request_id": selected["request_id"], "suggestion": f"Reply for {api.actor}",
                      "chinese_translation": "我会核对日期。", "english_translation": None, "is_mock": False}
    assert first.headers["cache-control"] == "no-store, max-age=0"
    records = provider_logs(caplog)
    assert len(records) == 1 and records[0]["request_id"] == selected["request_id"]
    assert records[0]["openai_request_id"] == "req_desktop_fixture" and records[0]["http_status"] == 200
    assert records[0]["usage"] == {"input_tokens": 30, "output_tokens": 20}
    assert "PRIVATE RESPONSE" not in caplog.text and "sk-fixture-only-test" not in caplog.text
    call = api.state.calls[0]
    assert call["actor"] == api.actor and call["pack"]["cm_name"] == "Trusted CM"
    assert call["pack"]["selected_messages"] == [dict(row, ref=f"S{row['ordinal']}")
                                                 for row in selected["selected_messages"]]
    assert call["pack"]["submitted_snapshot"]["messages"] == [dict(row, ref=f"C{row['ordinal']}")
                                                              for row in selected["conversation_context"]["messages"]]
    assert desktop_replies.SCOPE_INSTRUCTIONS["selected_messages"] in call["options"]["instructions"]
    assert desktop_replies.SCOPE_INSTRUCTIONS["recent_context"] not in call["options"]["instructions"]
    api.pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature='desktop_reply'")
    cached = api.client.post(PATH, json=selected, headers=api.headers)
    assert cached.status_code == 200 and cached.json() == result and len(api.state.calls) == 1
    assert len(provider_logs(caplog)) == 1
    changed = copy.deepcopy(selected)
    changed["conversation_context"]["messages"][0]["text"] = "Changed private snapshot"
    conflict = api.client.post(PATH, json=changed, headers=api.headers)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "request_conflict"
    assert len(api.state.calls) == 1
    api.pg.execute("UPDATE ai_feature_configs SET enabled=TRUE WHERE feature='desktop_reply'")
    foreign = api.client.post(PATH, json=selected, headers=api.other_headers)
    assert foreign.status_code == 200 and foreign.json()["suggestion"] == f"Reply for {api.other_actor}"
    assert len(api.state.calls) == 2 and api.state.calls[-1]["actor"] == api.other_actor
    api.state.output = model_response("我会核对日期。", language="zh")
    recent = api.client.post(PATH, json=payload("recent_context"), headers=api.headers)
    assert recent.status_code == 200
    assert recent.json()["suggestion"] == recent.json()["chinese_translation"] == "我会核对日期。"
    assert api.state.calls[-1]["pack"]["reply_scope"] == "recent_context"
    assert api.state.calls[-1]["pack"]["selected_messages"] == []
    assert desktop_replies.SCOPE_INSTRUCTIONS["recent_context"] in api.state.calls[-1]["options"]["instructions"]
    assert desktop_replies.SCOPE_INSTRUCTIONS["selected_messages"] not in api.state.calls[-1]["options"]["instructions"]
    receipts = api.pg.execute("SELECT actor_id,status FROM desktop_reply_requests").fetchall()
    assert len(receipts) == 3 and all(row["status"] == "succeeded" for row in receipts)
    assert len(provider_logs(caplog)) == 3
    abandoned = payload("recent_context")
    with monkeypatch.context() as stale:
        stale.setattr(desktop_reply_routes.store, "finish_success", lambda *args, **kwargs: False)
        first = api.client.post(PATH, json=abandoned, headers=api.headers)
    duplicate = api.client.post(PATH, json=abandoned, headers=api.headers)
    assert first.status_code == duplicate.status_code == 409
    assert first.json() == duplicate.json() and first.json()["error"]["code"] == "abandoned"
    assert len(api.state.calls) == 4 and len(provider_logs(caplog)) == 4
    diagnostic = provider_logs(caplog)[-1]
    assert diagnostic["error_code"] == "abandoned" and diagnostic["response_id"] == "resp_desktop_fixture"
    assert diagnostic["usage"] == {"input_tokens": 30, "output_tokens": 20}
    assert not diagnostic["request_unknown"]
    successful = payload("recent_context")
    with monkeypatch.context() as broken:
        def unavailable(*args, **kwargs):
            raise RuntimeError("logging unavailable")
        broken.setattr(desktop_reply_routes.logger, "log", unavailable)
        first = api.client.post(PATH, json=successful, headers=api.headers)
        duplicate = api.client.post(PATH, json=successful, headers=api.headers)
    assert first.status_code == duplicate.status_code == 200 and first.json() == duplicate.json()
    assert len(api.state.calls) == 5
    assert api.pg.execute("SELECT status FROM desktop_reply_requests WHERE request_id=%s",
                          (successful["request_id"],)).fetchone()["status"] == "succeeded"


def test_cookie_csrf_and_untrusted_cm_identity_reject_before_ai(api):
    no_csrf = {"Cookie": api.cookie_headers["Cookie"]}
    denied = api.client.post(PATH, json=payload(), headers=no_csrf)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "csrf_invalid"
    fake_key = dict(api.cookie_headers, Authorization="Bearer sk-fixture-only-test")
    denied = api.client.post(PATH, json=payload(), headers=fake_key)
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthorized"
    forged = payload()
    forged["cm_id"] = str(api.other_actor)
    denied = api.client.post(PATH, json=forged, headers=api.headers)
    assert denied.status_code == 400 and denied.json()["error"]["code"] == "invalid_request"
    assert api.state.calls == []
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_reply_requests").fetchone()["n"] == 0
    accepted = api.client.post(PATH, json=payload(), headers=api.cookie_headers)
    assert accepted.status_code == 200 and len(api.state.calls) == 1


@pytest.mark.parametrize(("failure_kind", "status", "code"), [
    ("http", 502, "provider_error"), ("timeout", 504, "provider_timeout"),
    ("incomplete", 502, "invalid_ai_response"),
])
def test_provider_failure_is_terminal_and_does_not_retry_paid_work(
        api, monkeypatch, caplog, failure_kind, status, code):
    async def fail(*args, **kwargs):
        api.state.calls.append("attempt")
        if failure_kind == "incomplete":
            raise openai_client.ModelOutputError("PRIVATE RESPONSE", telemetry={
                "http_status": 200, "openai_request_id": "req_incomplete", "response_id": "resp_incomplete",
                "response_status": "incomplete", "incomplete_reason": "max_output_tokens",
                "usage": {"input_tokens": 30}, "body": "PRIVATE RESPONSE", "key": "sk-fixture-only-test",
            })
        failure = openai_client.OpenAIRequestError(
            "private prompt and credential", retryable=True,
            telemetry={"http_status": 503, "usage": {"input_tokens": 30}},
        )
        if failure_kind == "timeout":
            raise failure from httpx.ReadTimeout("private transport detail")
        raise failure

    monkeypatch.setattr(openai_client, "call_openai_async", fail)
    submitted = payload()
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    api.pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature='desktop_reply'")
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == duplicate.status_code == status
    assert first.json() == duplicate.json() and first.json()["error"]["code"] == code
    assert "private" not in first.text and api.state.calls == ["attempt"]
    row = api.pg.execute("SELECT * FROM desktop_reply_requests").fetchone()
    assert row["status"] == "failed" and row["error_code"] == code and row["error_http_status"] == status
    assert row["usage"] == {"input_tokens": 30} and row["result_ciphertext"] is None
    assert "private" not in row["error_message"]
    records = provider_logs(caplog)
    assert len(records) == 1 and records[0]["request_id"] == submitted["request_id"]
    assert records[0]["error_code"] == code and records[0]["usage"]["input_tokens"] == 30
    if failure_kind == "incomplete":
        assert records[0]["openai_request_id"] == "req_incomplete"
        assert records[0]["response_id"] == "resp_incomplete"
        assert records[0]["response_status"] == "incomplete"
        assert records[0]["incomplete_reason"] == "max_output_tokens"
    assert "PRIVATE RESPONSE" not in caplog.text and "sk-fixture-only-test" not in caplog.text


def test_paused_background_queue_does_not_admit_or_throttle_desktop(api, monkeypatch):
    api.pg.execute("UPDATE ai_admission_state SET paused_at=NOW(),pause_reason='fixture paused' WHERE id=TRUE")

    def forbidden(*args, **kwargs):
        pytest.fail("Desktop reply touched the background AI admission scheduler")

    for name in ("can_claim", "throttle", "pause_for_quota", "observe_limits"):
        monkeypatch.setattr(ai_dispatch, name, forbidden)
    assert api.client.post(PATH, json=payload(), headers=api.headers).status_code == 200

    async def limited(*args, **kwargs):
        api.state.calls.append("limited")
        raise openai_client.OpenAIRequestError("private provider detail", retryable=True,
                                              telemetry={"http_status": 429})

    monkeypatch.setattr(openai_client, "call_openai_async", limited)
    submitted = payload()
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == duplicate.status_code == 429 and first.json() == duplicate.json()
    assert first.json()["error"]["code"] == "provider_rate_limit"
    assert api.state.calls[-1] == "limited" and len(api.state.calls) == 2
    state = api.pg.execute("SELECT * FROM ai_admission_state WHERE id=TRUE").fetchone()
    assert state["paused_at"] is not None and state["pause_reason"] == "fixture paused"


@pytest.mark.parametrize("oversized", [False, True])
def test_invalid_model_output_is_rejected_and_cached_as_terminal_error(api, oversized, caplog):
    api.state.output = (
        model_response("x" * (desktop_replies.MAX_RESPONSE_BYTES + 1), refs=["S1", "S2"]) if oversized else {
            "text": json.dumps({"suggestion": "unvalidated model output"}),
            "usage": {"input_tokens": 30, "output_tokens": 20},
        }
    )
    api.state.output["telemetry"] = {"openai_request_id": "req_test\nPRIVATE RESPONSE",
        "response_id": "sk-fixture-only-test", "response_status": ["PRIVATE RESPONSE"],
        "incomplete_reason": "PRIVATE RESPONSE", "body": "PRIVATE RESPONSE"}
    if not oversized:
        api.state.output["usage"] = {"input_tokens": True, "output_tokens": -1}
    submitted = payload()
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == duplicate.status_code == 502 and first.json() == duplicate.json()
    assert first.json()["error"]["code"] == "invalid_ai_response"
    assert "unvalidated model output" not in first.text and len(api.state.calls) == 1
    row = api.pg.execute("SELECT * FROM desktop_reply_requests").fetchone()
    assert row["status"] == "failed" and row["result_ciphertext"] is None
    records = provider_logs(caplog)
    assert len(records) == 1
    assert all(records[0][key] is None for key in
               ("openai_request_id", "response_id", "response_status", "incomplete_reason"))
    if not oversized:
        assert records[0]["usage"] == {"input_tokens": None, "output_tokens": None}
        assert row["usage"] == {}
    assert "PRIVATE RESPONSE" not in caplog.text and "sk-fixture-only-test" not in caplog.text


def test_selected_event_requires_selection_evidence_not_only_visible_tail(api, monkeypatch):
    submitted = payload()
    selected = ["Creator: My order arrived with the wrong colour.", "Creator: Can I exchange it?"]
    submitted["selected_messages"] = [{"ordinal": i, "text": text} for i, text in enumerate(selected, 1)]
    submitted["selected_message_text"] = "\n".join(selected)
    submitted["conversation_context"]["messages"] = [
        {"ordinal": 1, "text": "CM: Which product was it?"},
        {"ordinal": 2, "text": "Creator: Also, what time is tomorrow's livestream?"},
    ]
    api.state.output = model_response("What time works for your livestream?", refs=["C2"])
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == duplicate.status_code == 502 and first.json() == duplicate.json()
    assert first.json()["error"]["code"] == "invalid_ai_response"
    assert len(api.state.calls) == 1  # A failed target check cannot cause a second paid call.
    assert [row["text"] for row in api.state.calls[-1]["pack"]["selected_messages"]] == selected
    assert not api.pg.execute("SELECT 1 FROM desktop_reply_requests WHERE status='succeeded'").fetchone()

    api.state.output = model_response("Could you send the order number so I can check the exchange?",
                                      refs=["S2", "C1"])
    submitted["request_id"] = str(uuid4())
    assert api.client.post(PATH, json=submitted, headers=api.headers).status_code == 200

    api.state.output = {
        "text": json.dumps({"outcome": "verify", "context_reason": "target_unclear", "language": "en",
                            "draft": "", "chinese_translation": "",
                            "explanation": "Private selected message and untrusted sk-fixture-only-test",
                            "checks": [], "evidence_refs": ["S1"]}),
        "usage": {"input_tokens": 30, "output_tokens": 20},
    }
    submitted["request_id"] = str(uuid4())
    with monkeypatch.context() as broken:
        def unavailable(*args, **kwargs):
            raise RuntimeError("logging unavailable")
        broken.setattr(desktop_reply_routes.logger, "warning", unavailable)
        uncertain = api.client.post(PATH, json=submitted, headers=api.headers)
    assert uncertain.status_code == 422 and uncertain.json()["error"]["code"] == "context_unavailable"
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert uncertain.json() == duplicate.json()
    assert uncertain.json()["error"]["message"] == desktop_replies.CONTEXT_MESSAGES["target_unclear"]
    row = api.pg.execute("SELECT * FROM desktop_reply_requests WHERE request_id=%s",
                         (submitted["request_id"],)).fetchone()
    assert row["error_message"] == uncertain.json()["error"]["message"]
    assert "Private" not in repr(row) and "sk-fixture" not in repr(row)
    assert row["result_ciphertext"] is None
    assert len(api.state.calls) == 3  # Duplicate review errors never trigger another paid call.

    api.state.output["text"] = api.state.output["text"].replace('"target_unclear"', '"untrusted reason"')
    submitted["request_id"] = str(uuid4())
    assert api.client.post(PATH, json=submitted, headers=api.headers).status_code == 502


def test_server_total_timeout_cancels_model_and_stays_terminal(api, monkeypatch, caplog):
    monkeypatch.setattr(desktop_reply_routes, "TOTAL_TIMEOUT", 0.3)
    cancelled = []

    async def blocked(*args, **kwargs):
        api.state.calls.append("started")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(openai_client, "call_openai_async", blocked)
    submitted = payload()
    first = api.client.post(PATH, json=submitted, headers=api.headers)
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert first.status_code == duplicate.status_code == 504 and first.json() == duplicate.json()
    assert first.json()["error"]["code"] == "provider_timeout"
    assert api.state.calls == ["started"] and cancelled == [True]
    row = api.pg.execute("SELECT status,error_code FROM desktop_reply_requests").fetchone()
    assert row == {"status": "failed", "error_code": "provider_timeout"}
    records = provider_logs(caplog)
    assert len(records) == 1 and records[0]["request_unknown"]


def test_asgi_disconnect_cancels_model_and_records_cancelled_without_replay(api, monkeypatch, caplog):
    submitted = payload()
    body = json.dumps(submitted).encode()

    async def exercise_disconnect():
        started, cancelled, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            api.state.calls.append("started")
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(openai_client, "call_openai_async", blocked)
        consumed = False
        messages = []

        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect.is_set():
                return {"type": "http.disconnect"}
            await asyncio.Future()

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": PATH, "raw_path": PATH.encode(), "query_string": b"",
            "headers": [(b"authorization", b"Bearer desktop-session"), (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
        }
        task = asyncio.create_task(api.app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pending = await client.post(PATH, json=submitted, headers=api.headers)
            assert pending.status_code == 409 and pending.json()["error"]["code"] == "request_in_progress"
            assert api.state.calls == ["started"]
            disconnect.set()
            await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert started.is_set() and cancelled.is_set()
        assert next(message["status"] for message in messages if message["type"] == "http.response.start") == 499

    asyncio.run(exercise_disconnect())
    row = api.pg.execute("SELECT status,error_code FROM desktop_reply_requests").fetchone()
    assert row == {"status": "cancelled", "error_code": "cancelled"}
    duplicate = api.client.post(PATH, json=submitted, headers=api.headers)
    assert duplicate.status_code == 499 and duplicate.json()["error"]["code"] == "cancelled"
    assert api.state.calls == ["started"]
    records = provider_logs(caplog)
    assert len(records) == 1 and records[0]["error_code"] == "cancelled" and records[0]["request_unknown"]
    for kind in ("completed", "incomplete"):
        simultaneous = payload()

        async def completed_model(*args, _kind=kind, **kwargs):
            api.state.calls.append(_kind)
            if _kind == "incomplete":
                raise openai_client.ModelOutputError("PRIVATE RESPONSE", telemetry={
                    "http_status": 200, "openai_request_id": "req_cancel_incomplete",
                    "response_id": "resp_cancel_incomplete", "response_status": "incomplete",
                    "incomplete_reason": "max_output_tokens", "usage": {"input_tokens": 30, "output_tokens": 20},
                })
            return model_response(refs=["S1", "S2"])

        async def disconnected(*args):
            return True

        async def settled(tasks, **kwargs):
            await asyncio.gather(*tasks, return_exceptions=True)
            return set(tasks), set()

        with monkeypatch.context() as race:
            race.setattr(openai_client, "call_openai_async", completed_model)
            race.setattr(desktop_reply_routes, "disconnected", disconnected)
            race.setattr(desktop_reply_routes.asyncio, "wait", settled)
            first = api.client.post(PATH, json=simultaneous, headers=api.headers)
        duplicate = api.client.post(PATH, json=simultaneous, headers=api.headers)
        assert first.status_code == duplicate.status_code == 499 and first.json() == duplicate.json()
        assert "suggestion" not in first.json() and "PRIVATE RESPONSE" not in first.text
        row = api.pg.execute("SELECT status,result_ciphertext,usage FROM desktop_reply_requests WHERE request_id=%s",
                             (simultaneous["request_id"],)).fetchone()
        assert row == {"status": "cancelled", "result_ciphertext": None,
                       "usage": {"input_tokens": 30, "output_tokens": 20}}
        diagnostic = provider_logs(caplog)[-1]
        assert diagnostic["response_status"] == kind and not diagnostic["request_unknown"]
        assert diagnostic["usage"] == row["usage"]
    assert api.state.calls == ["started", "completed", "incomplete"]
    assert len(provider_logs(caplog)) == 3


def test_wire_limit_rejects_declared_and_streamed_body_without_truncation(api):
    raw = json.dumps(payload()).encode()
    wire = raw + b" " * (MAX_REQUEST_BYTES + 1 - len(raw))
    headers = dict(api.headers, **{"Content-Type": "application/json"})
    declared = api.client.post(PATH, content=wire, headers=headers)
    assert declared.status_code == 413 and declared.json()["error"]["code"] == "request_too_large"
    ambiguous = api.client.post(PATH, content=raw, headers=[
        *headers.items(), ("Content-Length", str(len(raw))), ("Content-Length", str(len(raw))),
    ])
    assert ambiguous.status_code == 400 and ambiguous.json()["error"]["code"] == "invalid_content_length"

    async def stream_request():
        async def chunks():
            yield raw
            yield wire[len(raw):]

        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            streamed = await client.post(PATH, content=chunks(), headers=headers)
        assert "content-length" not in streamed.request.headers
        assert streamed.status_code == 413 and streamed.json()["error"]["code"] == "request_too_large"

    asyncio.run(stream_request())
    assert api.state.calls == []
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_reply_requests").fetchone()["n"] == 0
