"""Assistant HTTP boundaries and isolated browser workflow; no provider calls."""

import json
import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import assistant_routes as routes

BACKEND = Path(__file__).parents[1]


@pytest.fixture
def api(monkeypatch):
    actor, session, turn = uuid4(), uuid4(), uuid4()
    state = SimpleNamespace(active=True, submits=[], cancels=[], reads=0)
    view = {"id": str(turn), "session_id": str(session), "client_request_id": str(uuid4()),
            "message": "总结今天上午", "status": "completed", "scope": {"cm_name": "CM"},
            "progress": "", "result": {"answer": "已整理", "events": []}, "error": None}
    conn = SimpleNamespace(transaction=lambda: nullcontext())
    monkeypatch.setattr(routes, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace())

    def read(_conn, current, reference, _settings):
        assert current == actor
        state.reads += 1
        if str(reference) != str(turn):
            raise LookupError("这轮对话不存在。")
        return dict(view)

    def submit(_conn, current, body, _settings):
        assert current == actor
        state.submits.append(body)
        return view

    def cancel(_conn, current, reference, _settings):
        assert current == actor
        state.cancels.append(reference)
        return {**view, "status": "cancelled"}

    monkeypatch.setattr(routes.assistant, "submit", submit)
    monkeypatch.setattr(routes.assistant, "turn_view", read)
    monkeypatch.setattr(routes.assistant, "cancel", cancel)
    monkeypatch.setattr(routes.assistant, "session_view", lambda *_: {"id": str(session), "turns": [view]})
    monkeypatch.setattr(routes.assistant_tools, "evidence", lambda *_: {"messages": [{"text": "原文"}]})
    app = FastAPI()
    app.include_router(routes.create_router(
        current_authorization=lambda _: {"id": actor, "status": "active"} if state.active else None,
        csrf_token=lambda _: "fixture-csrf",
    ))
    return SimpleNamespace(client=TestClient(app), state=state, view=view, turn=turn, session=session)


def test_submission_csrf_json_limit_and_read_only_progress(api):
    body = {"session_id": str(api.session), "client_request_id": str(uuid4()), "message": "今天上午"}
    assert api.client.post("/assistant/turns", json=body).status_code == 403
    headers = {"X-CSRF-Token": "fixture-csrf"}
    assert api.client.post("/assistant/turns", content="x", headers=headers).status_code == 415
    oversized = api.client.post("/assistant/turns", json={**body, "message": "x" * 33000}, headers=headers)
    assert oversized.status_code == 413 and api.state.submits == []
    assert api.client.post("/assistant/turns", json=body, headers=headers).status_code == 202
    events = api.client.get(f"/assistant/turns/{api.turn}/events")
    assert events.status_code == 200 and events.headers["x-accel-buffering"] == "no"
    assert events.headers["cache-control"] == "no-store, max-age=0"
    assert "event: result" in events.text and "event: done" in events.text
    assert len(api.state.submits) == 1 and api.state.cancels == []
    assert api.client.get(f"/assistant/turns/{api.turn}/events").status_code == 200
    assert len(api.state.submits) == 1
    assert api.client.post(f"/assistant/turns/{api.turn}/cancel", json={}).status_code == 403
    assert api.client.post(f"/assistant/turns/{api.turn}/cancel", json={}, headers=headers).status_code == 200
    assert len(api.state.cancels) == 1
    assert api.client.get("/assistant/evidence/fixture-ref").json()["messages"] == [{"text": "原文"}]
    api.state.active = False
    for path in (f"/assistant/sessions/{api.session}", f"/assistant/turns/{api.turn}/events",
                 "/assistant/evidence/fixture-ref"):
        denied = api.client.get(path)
        assert denied.status_code == 401 and "原文" not in denied.text


def test_sse_changes_only_and_revoked_stream_clears_without_cancel(api, monkeypatch):
    views = [dict(api.view, status="queued", result=None, progress="等待处理"),
             dict(api.view, status="queued", result=None, progress="等待处理"), api.view]

    async def tick(_):
        pass

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(sleep=tick))
    monkeypatch.setattr(routes.assistant, "turn_view", lambda *_: views.pop(0))
    value = api.client.get(f"/assistant/turns/{api.turn}/events")
    assert value.text.count("event: scope") == 1
    assert value.text.count("event: progress") == 2
    assert value.text.count("event: result") == 1 and api.state.cancels == []

    async def revoke(_):
        api.state.active = False

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(sleep=revoke))
    monkeypatch.setattr(routes.assistant, "turn_view", lambda *_: dict(api.view, status="running", result=None))
    value = api.client.get(f"/assistant/turns/{api.turn}/events")
    assert '"clear":true' in value.text and "event: done" in value.text
    assert api.state.cancels == []
    api.state.active = True
    monkeypatch.setattr(routes.assistant, "turn_view", lambda *_: dict(
        api.view, result=None, scope={}, error_code="permission", error="授权或查询范围已变化。"
    ))
    value = api.client.get(f"/assistant/turns/{api.turn}/events")
    assert '"clear":true' in value.text and '"status":"failed"' in value.text
    assert "event: result" not in value.text


def test_browser_resume_evidence_cancel_and_account_isolation():
    environment = Environment(loader=FileSystemLoader(BACKEND / "app/templates"), autoescape=select_autoescape())
    html = environment.get_template("base.html").render(
        assistant_enabled=True, assistant_configured=True, assistant_user_id=str(uuid4()),
        csrf_token="fixture-csrf", assistant_context={},
    )
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require('playwright');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [], posts = []; let view, html = input.html, failFirstPost = true;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.sources = [];
      window.EventSource = class extends EventTarget {
        constructor(url) { super(); this.url=url; this.closed=false; window.sources.push(this); }
        close() { this.closed=true; }
        emit(name,value) { this.dispatchEvent(new MessageEvent(name,{data:JSON.stringify(value)})); }
      };
      Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text => {window.copied=text;}}});
    });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'http://localhost') return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:html});
      if (url.pathname.startsWith('/static/')) return route.fulfill({
        contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css',
        body:fs.readFileSync(path.join(input.backend,'app',url.pathname))});
      if (route.request().method()==='POST') {
        const body=route.request().postDataJSON(); posts.push({url:url.pathname,body});
        assert.equal(route.request().headers()['x-csrf-token'],'fixture-csrf');
        if (url.pathname.endsWith('/cancel')) view={...view,status:'cancelled',result:null};
        else if (view?.client_request_id !== body.client_request_id) view={
          id:require('node:crypto').randomUUID(),session_id:body.session_id,client_request_id:body.client_request_id,
          message:body.message,status:'queued',scope:{cm_name:'CM'},result:null};
        if (failFirstPost) {failFirstPost=false; return route.abort();}
        return route.fulfill({status:202,json:view});
      }
      if (url.pathname.includes('/sessions/')) return route.fulfill({json:{id:view.session_id,turns:[view]}});
      if (url.pathname.includes('/evidence/')) return route.fulfill({json:{messages:[{
        chat_name:'群',sender_name:'达人',time:'2026-10-12T09:00:00Z',text:'<img src=x onerror=alert(1)>'}],gaps:[]}});
      return route.abort();
    });
    await page.goto('http://localhost/');
    await page.locator('#oa-launcher').click(); await page.locator('#oa-input').fill('总结今天上午');
    await page.locator('#oa-input').press('Enter');
    await page.getByRole('button',{name:'重试提交',exact:true}).waitFor();
    await page.locator('#oa-send').click();
    await page.waitForFunction(() => window.sources.length===1);
    assert.equal(posts.length,2); assert.match(posts[0].body.session_id,/^[0-9a-f-]{36}$/);
    assert.deepEqual(posts[0].body,posts[1].body);
    await page.locator('#oa-collapse').click(); assert.equal(await page.evaluate(() => window.sources[0].closed),false);
    const result={answer:'<script>bad()</script>',events:[{event_ref:'ref',title:'样品进展',creator_handle:'fixture',
      handling_status:'unresolved',evidence_ref:'ref'}],scope:{cm_name:'CM'},
      window:{start:'2026-10-12T07:00:00Z',end:'2026-10-12T11:00:00Z'},
      draft:{draft:'I will check.',chinese_translation:'我会核对。',language:'en'}};
    view={...view,status:'completed',result};
    await page.evaluate(value => {
      window.sources[0].emit('result',value);window.sources[0].emit('done',{status:'completed'});
    },result);
    await page.locator('#oa-launcher').click();
    assert.equal(await page.locator('.oa-answer').textContent(),result.answer);
    assert.equal(await page.locator('.oa-history script').count(),0);
    await page.locator('.oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    assert.equal(await page.locator('#oa-sources-body img').count(),0);
    await page.locator('#oa-sources-close').click();
    await page.locator('.oa-draft textarea').fill('Edited reply');
    await page.getByRole('button',{name:'复制回复',exact:true}).click();
    await page.waitForFunction(() => window.copied==='Edited reply');
    assert.equal(posts.length,2);
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-event').waitFor();
    assert.equal(posts.length,2);
    await page.locator('#oa-input').fill('再看最新进展'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(() => window.sources.length===1); assert.equal(posts.length,3);
    await page.locator('#oa-stop').click(); await page.waitForFunction(() => document.getElementById('oa-stop').hidden);
    assert.equal(posts.length,4); assert(posts[3].url.endsWith('/cancel'));
    await page.locator('#oa-fullscreen').click();
    assert.equal(await page.locator('#oa-panel').getAttribute('aria-modal'),'true');
    await page.locator('#oa-input').press('Escape');
    assert.equal(await page.locator('#oa-panel').getAttribute('aria-modal'),'false');
    const previous = await page.evaluate(() => JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id);
    html=html.replace(/data-user-id="[^"]+"/,'data-user-id="33333333-3333-4333-8333-333333333333"');
    const reads=[]; page.on('request',request => {if(request.url().includes('/sessions/')) reads.push(request.url());});
    await page.reload(); await page.locator('#oa-launcher').click();
    assert.equal(await page.locator('.oa-event').count(),0); assert(!reads.some(url=>url.includes(previous)));
    assert.deepEqual(errors,[]); console.log('assistant browser workflow passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"html": html, "backend": str(BACKEND)}),
                            text=True, capture_output=True, timeout=60, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
