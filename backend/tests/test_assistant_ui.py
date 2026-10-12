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
    state = SimpleNamespace(active=True, submits=[], cancels=[], reads=0, scope_reads=0)
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

    def scopes(_conn, current):
        assert current == actor
        state.scope_reads += 1
        return {"scopes": [{"cm_id": str(actor), "cm_name": "CM"}],
                "selected_cm_id": str(actor), "can_switch_cm": False}

    monkeypatch.setattr(routes.assistant, "submit", submit)
    monkeypatch.setattr(routes.assistant, "turn_view", read)
    monkeypatch.setattr(routes.assistant, "cancel", cancel)
    monkeypatch.setattr(routes.assistant, "session_view", lambda *_: {"id": str(session), "turns": [view]})
    monkeypatch.setattr(routes.assistant_tools, "evidence", lambda *_: {"messages": [{"text": "原文"}]})
    monkeypatch.setattr(routes.assistant_tools, "scopes_for", scopes)
    app = FastAPI()
    app.include_router(routes.create_router(
        current_authorization=lambda _: {"id": actor, "status": "active"} if state.active else None,
        csrf_token=lambda _: "fixture-csrf",
    ))
    return SimpleNamespace(client=TestClient(app), state=state, view=view, turn=turn, session=session)


def test_submission_csrf_json_limit_and_read_only_progress(api):
    scopes = api.client.get("/assistant/scopes")
    assert scopes.status_code == 200 and scopes.json()["can_switch_cm"] is False
    assert api.state.scope_reads == 1 and api.state.submits == api.state.cancels == []
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
                 "/assistant/evidence/fixture-ref", "/assistant/scopes"):
        denied = api.client.get(path)
        assert denied.status_code == 401 and "原文" not in denied.text
    assert api.state.scope_reads == 1


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
    const errors = [], posts = [], remiId='22222222-2222-4222-8222-222222222222';
    let view, html = input.html, failFirstPost = true, scopeReads=0, sessionGone=false, turnsEmpty=false;
    const ownId=()=>html.match(/data-user-id="([^"]+)"/)[1];
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
      if (url.pathname==='/assistant/scopes') {
        scopeReads++; const owner=ownId()!=='33333333-3333-4333-8333-333333333333';
        return route.fulfill({json:{scopes:[{cm_id:ownId(),cm_name:'CM'},
          ...(owner?[{cm_id:remiId,cm_name:'Remi'}]:[])],selected_cm_id:ownId(),can_switch_cm:owner}});
      }
      if (route.request().method()==='POST') {
        const body=route.request().postDataJSON(); posts.push({url:url.pathname,body});
        assert.equal(route.request().headers()['x-csrf-token'],'fixture-csrf');
        if (url.pathname.endsWith('/cancel')) view={...view,status:'cancelled',result:null};
        else if (view?.client_request_id !== body.client_request_id) view={
          id:require('node:crypto').randomUUID(),session_id:body.session_id,client_request_id:body.client_request_id,
          message:body.message,status:'queued',scope:{cm_id:body.context.cm_id,
            cm_name:body.context.cm_id===remiId?'Remi':'CM'},result:null};
        if (failFirstPost) {failFirstPost=false; return route.abort();}
        return route.fulfill({status:202,json:view});
      }
      if (url.pathname.includes('/sessions/')) return route.fulfill(sessionGone?
        {status:404,json:{error:'对话不存在或无权查看。'}}:{json:{id:view.session_id,turns:turnsEmpty?[]:[view]}});
      if (url.pathname.includes('/evidence/')) return route.fulfill({json:{messages:[{
        chat_name:'群',sender_name:'达人',time:'2026-10-12T09:00:00Z',
        text:'<img src=x onerror=alert(1)> **本人原文**'}],gaps:[]}});
      return route.abort();
    });
    await page.goto('http://localhost/');
    assert.equal(scopeReads,0);
    await page.locator('#oa-launcher').click(); await page.locator('#oa-input').fill('总结今天上午');
    await page.locator('#oa-input').press('Enter');
    await page.getByRole('button',{name:'重试提交',exact:true}).waitFor();
    assert.equal(scopeReads,1);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('#oa-cm').isDisabled(),true);
    await page.evaluate(value=>{const select=document.getElementById('oa-cm');
      select.value=value;select.dispatchEvent(new Event('change'));},remiId);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    await page.locator('#oa-send').click();
    await page.waitForFunction(() => window.sources.length===1);
    assert.equal(posts.length,2); assert.match(posts[0].body.session_id,/^[0-9a-f-]{36}$/);
    assert.deepEqual(posts[0].body,posts[1].body);
    await page.locator('#oa-collapse').click(); assert.equal(await page.evaluate(() => window.sources[0].closed),false);
    const result={answer:'昨天值得优先关注：\n1. **账号权限故障**：请跟进 [帮助](https://example.com/help)\n' +
      '2. **活动报名**：暂未回复。\n\n<script>bad()</script> [不安全](javascript:alert(1)) ' +
      '[数据](data:text/html;base64,PHNjcmlwdD4=)\n`**原样代码**`\n\n- 核对群聊\n- 联系同事',
      events:[{event_ref:'ref',title:'样品进展',creator_handle:'fixture',
      handling_status:'unresolved',evidence_ref:'ref'}],scope:{cm_id:ownId(),cm_name:'CM',audience:'all',creator_ids:null},
      window:{start:'2026-10-12T07:00:00Z',end:'2026-10-12T11:00:00Z'},
      draft:{draft:'**I will check.**',chinese_translation:'我会核对。',language:'en'},
      data:[{kind:'window_messages',scope:{audience:'all'},total_messages:20,
        messages:[{conversation_name:'业务群',sender_name:'未识别联系人',time:'2026-10-12T09:00:00Z',
          text:'<script>source()</script> **本人原文**',evidence_ref:'raw-ref'}],coverage:{source_complete:false},
        gaps:Array.from({length:25},(_,i)=>({chat_id:`chat-${i}`,reason:'历史同步未覆盖检索起点'})),
        gap_count:1273,gap_conversation_count:1272,gap_summary:[
          {reason:'历史同步未覆盖检索起点',conversations:1272,items:0,
            names:['受限群一','<img src=x onerror=alert(1)>','群三','群四','群五']},
          {reason:'附件正文不可读取',conversations:0,items:5}]}]};
    view={...view,status:'completed',result};
    await page.evaluate(value => {
      window.sources[0].emit('result',value);window.sources[0].emit('done',{status:'completed'});
    },result);
    await page.locator('#oa-launcher').click();
    assert.deepEqual(await page.locator('.oa-answer strong').allTextContents(),['账号权限故障','活动报名']);
    assert.equal(await page.locator('.oa-answer ol li').count(),2);
    assert.equal(await page.locator('.oa-answer ul li').count(),2);
    assert.equal(await page.locator('.oa-answer a').count(),1);
    assert.equal(await page.locator('.oa-answer a').getAttribute('href'),'https://example.com/help');
    assert.equal(await page.locator('.oa-answer a').getAttribute('rel'),'noopener noreferrer');
    assert.match(await page.locator('.oa-answer').textContent(),/<script>bad\(\)<\/script>/);
    assert.match(await page.locator('.oa-answer').textContent(),/javascript:alert\(1\)/);
    assert.equal(await page.locator('.oa-answer code').textContent(),'**原样代码**');
    assert.equal(await page.locator('.oa-history script').count(),0);
    assert.match(await page.locator('.oa-scope').textContent(),/全部授权消息/);
    assert.match(await page.locator('.oa-data').textContent(),/本页 1 条.*时段 20 条/);
    assert.equal(await page.locator('.oa-data details.oa-coverage').getAttribute('open'),null);
    assert.equal(await page.locator('.oa-data .oa-gaps summary').textContent(),
      '部分资料尚不完整 · 1272 个会话 · 5 项记录');
    assert.equal(await page.locator('.oa-data .oa-gaps > div').count(),2);
    assert.match(await page.locator('.oa-data .oa-gaps > div').first().textContent(),
      /历史同步未覆盖检索起点 · 1272 个会话.*另有 1267 个会话/);
    assert.equal(await page.locator('.oa-data .oa-gap-names').textContent(),
      '会话：受限群一、<img src=x onerror=alert(1)>、群三、群四、群五；另有 1267 个会话');
    assert.equal(await page.locator('.oa-data .oa-gaps img').count(),0);
    assert.equal(await page.locator('.oa-hit strong, .oa-hit a').count(),0);
    assert.match(await page.locator('.oa-hit').textContent(),/\*\*本人原文\*\*/);
    assert.equal(await page.locator('.oa-draft textarea').inputValue(),'**I will check.**');
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    assert.equal(await page.locator('#oa-sources-body img').count(),0);
    assert.equal(await page.locator('#oa-sources-body strong').count(),0);
    assert.match(await page.locator('#oa-sources-body blockquote').textContent(),/\*\*本人原文\*\*/);
    await page.locator('#oa-sources-close').click();
    await page.locator('.oa-draft textarea').fill('Edited reply');
    await page.getByRole('button',{name:'复制回复',exact:true}).click();
    await page.waitForFunction(() => window.copied==='Edited reply');
    assert.equal(posts.length,2);
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-event').waitFor();
    assert.equal(posts.length,2);
    await page.locator('#oa-input').fill('再看最新进展');
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    await page.locator('#oa-cm').selectOption(remiId);
    assert.equal(await page.locator('#oa-input').inputValue(),'再看最新进展');
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data').count(),0);
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    assert.equal(posts.length,2);
    await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(() => window.sources.length===1); assert.equal(posts.length,3);
    assert.notEqual(posts[2].body.session_id,posts[0].body.session_id);
    assert.equal(posts[2].body.context.cm_id,remiId);
    assert.equal(posts[2].body.context.audience,'all');
    assert.equal(posts[2].body.context.creator_ids,null);
    assert.equal(await page.locator('#oa-cm').isDisabled(),true);
    await page.locator('#oa-stop').click(); await page.waitForFunction(() => document.getElementById('oa-stop').hidden);
    assert.equal(posts.length,4); assert(posts[3].url.endsWith('/cancel'));
    await page.locator('#oa-input').fill('再查一次'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(()=>window.sources.length===2); assert.equal(posts.length,5);
    view={...view,status:'completed',result:{...result,scope:{cm_id:remiId,cm_name:'Remi',audience:'creators',
      creator_ids:['44444444-4444-4444-8444-444444444444'],event_ref:'old-event',event_title:'旧事项'}}};
    await page.evaluate(value=>window.sources.at(-1).emit('result',value),view.result);
    assert.match(await page.locator('#oa-scope').textContent(),/旧事项/);
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    turnsEmpty=true;
    await page.evaluate(()=>window.sources.at(-1).dispatchEvent(new Event('error')));
    await page.waitForFunction(()=>document.getElementById('oa-stop').hidden);
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data').count(),0);
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    assert.match(await page.locator('#oa-scope').textContent(),/Remi.*全部授权消息/);
    assert.doesNotMatch(await page.locator('#oa-scope').textContent(),/旧事项/);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id),posts[4].body.session_id);
    assert.equal(posts.length,5);
    await page.locator('#oa-fullscreen').click();
    assert.equal(await page.locator('#oa-panel').getAttribute('aria-modal'),'true');
    await page.locator('#oa-input').press('Escape');
    assert.equal(await page.locator('#oa-panel').getAttribute('aria-modal'),'false');
    const previous = await page.evaluate(() => JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id);
    sessionGone=true; await page.reload(); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-status').textContent.includes('上次会话已不可读取'));
    assert.equal(await page.locator('#oa-cm').isDisabled(),false);
    assert.equal(posts.length,5);
    html=html.replace(/data-user-id="[^"]+"/,'data-user-id="33333333-3333-4333-8333-333333333333"');
    const reads=[]; page.on('request',request => {if(request.url().includes('/sessions/')) reads.push(request.url());});
    await page.reload(); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-cm').options.length===1);
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('.oa-event').count(),0); assert(!reads.some(url=>url.includes(previous)));
    assert.deepEqual(errors,[]); console.log('assistant browser workflow passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"html": html, "backend": str(BACKEND)}),
                            text=True, capture_output=True, timeout=60, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
