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


def test_browser_retry_sources_draft_cancel_and_account_isolation():
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
    const errors=[], posts=[], sourceReads=[], sessionReads=[];
    const remi='22222222-2222-4222-8222-222222222222';
    let html=input.html, view, failFirst=true, owner=true, denyEvidence=false;
    const ownId=()=>html.match(/data-user-id="([^"]+)"/)[1];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{
      window.sources=[];
      window.EventSource=class extends EventTarget {
        constructor(url) {super(); this.url=url; window.sources.push(this);}
        close() {}
        emit(name,value) {this.dispatchEvent(new MessageEvent(name,{data:JSON.stringify(value)}));}
      };
      Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copied=text;}}});
    });
    await page.route('**/*',async route=>{
      const request=route.request(), url=new URL(request.url());
      if(url.origin!=='http://localhost') return route.abort();
      if(url.pathname==='/admin/messages') return route.fulfill({contentType:'text/html',body:html});
      if(url.pathname.startsWith('/static/')) return route.fulfill({
        contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css',
        body:fs.readFileSync(path.join(input.backend,'app',url.pathname))});
      if(url.pathname==='/assistant/scopes') return route.fulfill({json:{
        scopes:[{cm_id:ownId(),cm_name:'CM'},...(owner?[{cm_id:remi,cm_name:'Remi'}]:[])],
        selected_cm_id:ownId(),can_switch_cm:owner}});
      if(request.method()==='POST') {
        const body=request.postDataJSON(); posts.push({url:url.pathname,body});
        assert.equal(request.headers()['x-csrf-token'],'fixture-csrf');
        if(url.pathname.endsWith('/cancel')) view={...view,status:'cancelled',result:null};
        else if(view?.client_request_id!==body.client_request_id) view={
          id:require('node:crypto').randomUUID(),session_id:body.session_id,client_request_id:body.client_request_id,
          message:body.message,status:'queued',scope:{cm_id:body.context.cm_id,cm_name:'CM'},result:null};
        if(failFirst) {failFirst=false; return route.abort();}
        return route.fulfill({status:202,json:view});
      }
      if(url.pathname.includes('/sessions/')) {
        sessionReads.push(url.pathname);
        return route.fulfill({json:{id:view.session_id,turns:[view]}});
      }
      if(url.pathname.startsWith('/assistant/sources/')) {
        sourceReads.push(url.searchParams.get('page'));
        return route.fulfill({json:{page:2,page_count:2,has_more:false,total_messages:2,
          scope:{cm_id:ownId(),cm_name:'CM'},messages:[{message_ref:'m.second',
            conversation_name:'合作群',sender_name:'达人',text:'Second source.'}],gaps:[]}});
      }
      if(url.pathname.startsWith('/assistant/drafts/')) return route.fulfill({json:{new_messages:false}});
      if(url.pathname.startsWith('/assistant/evidence/')) return route.fulfill(denyEvidence?
        {status:403,json:{error:'查看权限已变化。'}}:
        {json:{messages:[{chat_name:'合作群',sender_name:'达人',message_ref:'m.source',
          text:'<img src=x onerror=alert(1)> **原文不是 Markdown**'}],gaps:[]}});
      return route.abort();
    });
    const submit=async message=>{
      const streams=await page.evaluate(()=>window.sources.length);
      await page.locator('#oa-input').fill(message); await page.locator('#oa-input').press('Enter');
      await page.waitForFunction(count=>window.sources.length===count+1,streams);
    };
    const deliver=async result=>{
      view={...view,status:'completed',result};
      await page.evaluate(value=>{
        window.sources.at(-1).emit('result',value);
        window.sources.at(-1).emit('done',{status:'completed'});
      },result);
    };
    await page.goto('http://localhost/admin/messages'); await page.locator('#oa-launcher').click();
    await page.locator('#oa-input').fill('总结今天'); await page.locator('#oa-input').press('Enter');
    await page.getByRole('button',{name:'重试提交',exact:true}).waitFor();
    await page.locator('#oa-send').click(); await page.waitForFunction(()=>window.sources.length===1);
    assert.deepEqual(posts[0].body,posts[1].body);
    const ownerSession=view.session_id;
    const result={source_mode:'detailed',reply_enabled:true,
      answer:'**样品跟进**：需要回复。\n<script>bad()</script> [不安全](javascript:alert(1))',
      scope:{cm_id:ownId(),cm_name:'CM',audience:'all'},
      sections:[{title:'活动报名',summary:'需要确认报名。',message_refs:['m.section'],
        sources:[{message_ref:'m.section',conversation_name:'活动群',sender_name:'Amy',text:'Can I attend?'}]}],
      events:[{event_ref:'event-ref',title:'样品进展',summary:'请核对样品。',
        handling_status:'unresolved',evidence_ref:'m.source'}],
      draft:{draft_ref:'d.fixture',source_ref:'m.source',draft:'I will check.',
        chinese_translation:'我会核对。',language:'en'},
      data:[{kind:'window_messages',query_ref:'q.fixture',source_url:'/assistant/sources/q.fixture?page=1',
        page:1,page_count:2,has_more:true,total_messages:2,
        messages:[{message_ref:'m.first',conversation_name:'合作群',text:'First source.'}]}]};
    await deliver(result);
    assert.equal(await page.locator('.oa-answer strong').textContent(),'样品跟进');
    assert.equal(await page.locator('.oa-history script, .oa-answer a').count(),0);
    assert.equal(await page.locator('.oa-summary-section').count(),1);
    await page.locator('.oa-source-entry > details > summary').click();
    await page.locator('.oa-source-pagination').getByRole('button',{name:'下一页',exact:true}).click();
    await page.getByText('第 2 / 2 页',{exact:true}).waitFor();
    assert.deepEqual(sourceReads,['2']); assert.equal(posts.length,2);
    await page.locator('.oa-event .oa-evidence').click();
    await page.locator('#oa-sources-body blockquote').waitFor();
    assert.equal(await page.locator('#oa-sources-body img, #oa-sources-body strong').count(),0);
    await page.locator('#oa-sources-close').click();
    await page.getByRole('button',{name:'查看回复草稿',exact:true}).click();
    await page.locator('.oa-draft textarea').fill('My edited reply.');
    await page.getByRole('button',{name:'复制回复',exact:true}).click();
    await page.waitForFunction(()=>window.copied==='My edited reply.');
    assert.equal(posts.length,2);
    await page.locator('#oa-reply-close').click();
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-event').waitFor();
    assert.equal(posts.length,2);
    await page.locator('#oa-cm').selectOption(remi);
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-summary-section').count(),0);
    await submit('核对 Remi 的事项');
    assert.equal(posts.at(-1).body.context.cm_id,remi);
    assert.notEqual(posts.at(-1).body.session_id,ownerSession);
    await page.locator('#oa-stop').click();
    await page.waitForFunction(()=>document.getElementById('oa-stop').hidden);
    assert(posts.at(-1).url.endsWith('/cancel'));

    // A different CM cannot inherit the Owner's history or select another CM.
    owner=false;
    html=html.replace(/data-user-id="[^"]+"/,'data-user-id="33333333-3333-4333-8333-333333333333"');
    sessionReads.length=0;
    await page.goto('http://localhost/admin/messages'); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-cm').options.length===1);
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(sessionReads.length,0);
    await page.evaluate(cm_id=>{
      const select=document.getElementById('oa-cm'), option=document.createElement('option');
      option.value=cm_id; select.append(option); select.value=cm_id; select.dispatchEvent(new Event('change'));
    },remi);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    await submit('我的待回复事项');
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    await deliver({...result,scope:{cm_id:ownId(),cm_name:'CM',audience:'all'}});
    denyEvidence=true;
    await page.locator('.oa-event .oa-evidence').click();
    await page.waitForFunction(()=>document.getElementById('oa-input').disabled);
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-summary-section, ' +
      '#oa-sources-body blockquote').count(),0);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"html": html, "backend": str(BACKEND)}),
                            text=True, capture_output=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
