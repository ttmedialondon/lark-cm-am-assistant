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
    const errors = [], posts = [], sourceReads = [], evidenceReads = [], sessionReads = [], draftReads = [];
    const remiId='22222222-2222-4222-8222-222222222222';
    let view, html = input.html, failFirstPost = true, scopeReads=0, sessionGone=false, turnsEmpty=false;
    let holdEvidence=false, denyEvidence=false, evidenceStarted, olderTurns=[], newDraftMessages=false, selfReady=true;
    const pendingEvidence=[];
    const ownId=()=>html.match(/data-user-id="([^"]+)"/)[1];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().includes('/assistant/sessions/')) sessionReads.push(request.url());
      if (request.url().includes('/assistant/drafts/')) draftReads.push(request.url());
    });
    await page.addInitScript(() => {
      window.sources = [];
      window.EventSource = class extends EventTarget {
        constructor(url) { super(); this.url=url; this.closed=false; window.sources.push(this); }
        close() { this.closed=true; }
        emit(name,value) { this.dispatchEvent(new MessageEvent(name,{data:JSON.stringify(value)})); }
      };
      Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text => {window.copied=text;}}});
      window.open = url => { window.opened=url; };
    });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'http://localhost') return route.abort();
      if (['/','/dashboard','/admin/messages'].includes(url.pathname))
        return route.fulfill({contentType:'text/html',body:html});
      if (url.pathname.startsWith('/static/')) return route.fulfill({
        contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css',
        body:fs.readFileSync(path.join(input.backend,'app',url.pathname))});
      if (url.pathname==='/assistant/scopes') {
        scopeReads++; const owner=ownId()!=='33333333-3333-4333-8333-333333333333';
        return route.fulfill({json:{scopes:[...(selfReady?[{cm_id:ownId(),cm_name:'CM'}]:[]),
          ...(owner?[{cm_id:remiId,cm_name:'Remi'}]:[])],selected_cm_id:selfReady?ownId():null,can_switch_cm:owner}});
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
        {status:404,json:{error:'对话不存在或无权查看。'}}:
        {json:{id:view.session_id,turns:turnsEmpty?[]:[...olderTurns,view]}});
      if (url.pathname.startsWith('/assistant/sources/')) {
        sourceReads.push(url.searchParams.get('page'));
        if (url.pathname.includes('q.search')) return route.fulfill({json:{kind:'messages',query_ref:'q.search',
          messages:[{conversation_name:'普通消息群',sender_name:'商家',
            message_ref:'u.fixture.message',text:'Search continuation.'}],
          coverage:{matched:1,scanned:25,readable:25},has_more:false}});
        return route.fulfill({json:{page:Number(url.searchParams.get('page')),page_count:4,has_more:true,
          scope:{cm_id:url.pathname.endsWith('q.remi')?remiId:ownId(),
            cm_name:url.pathname.endsWith('q.remi')?'Remi':'CM'},
          total_messages:353,messages:[{conversation_name:'商家客服群',sender_name:'未分类商家',
            message_ref:'m.fixture.second',text:'Can you check the dispatch status?',
            time:'2026-10-12T09:00:00Z'}],gaps:[]}});
      }
      if (url.pathname.startsWith('/assistant/drafts/')) return route.fulfill({
        json:{new_messages:newDraftMessages,checked_at:'2026-10-12T11:00:00Z'}});
      if (url.pathname.includes('/evidence/')) {
        const ref=decodeURIComponent(url.pathname.split('/evidence/')[1]); evidenceReads.push(ref);
        if (denyEvidence) return route.fulfill({status:403,json:{error:'查看权限已变化。'}});
        if (holdEvidence) {evidenceStarted?.(); await new Promise(resolve=>pendingEvidence.push(resolve));}
        if (ref==='m.section.private') return route.fulfill({json:{messages:[{conversation_name:'Sara 私聊',
          sender_name:'Sara',message_ref:ref,time:'2026-10-13T08:00:00Z',
          text:'Full private message and question context.'}],gaps:[]}});
        return route.fulfill({json:{messages:[{
          chat_name:'群',sender_name:'达人',time:'2026-10-12T09:00:00Z',
          text:'<img src=x onerror=alert(1)> **本人原文**'}],gaps:[]}});
      }
      return route.abort();
    });
    await page.goto('http://localhost/admin/messages');
    assert.equal(scopeReads,0);
    await page.evaluate(cm_id=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context:{cm_id}}})),remiId);
    await page.waitForFunction(id=>document.getElementById('oa-cm').value===id,remiId);
    assert.equal(await page.locator('#oa-panel').isHidden(),true);
    assert.equal(posts.length,0);
    await page.evaluate(cm_id=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context:{cm_id}}})),ownId());
    await page.waitForFunction(id=>document.getElementById('oa-cm').value===id,ownId());
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
    const progress={status:'running',message:'后台材料诊断',messages:100,total_messages:353,
      conversations:43,segments_completed:1,segments_total:4};
    await page.evaluate(value=>window.sources.at(-1).emit('progress',value),progress);
    assert.match(await page.locator('#oa-status').textContent(),/后台材料诊断.*100 \/ 353 条.*43 个会话.*1 \/ 4 段/);
    await page.locator('#oa-collapse').click(); assert.equal(await page.evaluate(() => window.sources[0].closed),false);
    const diagnosticData=[{kind:'sync',query_ref:'q.sync',summary:{label:'首次同步诊断',detail:'首次同步数量'},
      conversations:{counts:{discovered_conversation_count:50,syncable_conversation_count:40,
        backfill_complete_count:30,syncing_conversation_count:10,not_readable_conversation_count:2}},
      watermarks:{latest:'2026-10-12T11:00:00Z'},definition:'测试用同步定义'},
      {kind:'groups',query_ref:'q.groups',groups:[{name:'客服群',sync_enabled:true,purpose_label:'客服',member_count:3}]}];
    const result={answer:'昨天值得优先关注：\n1. **账号权限故障**：请跟进 [帮助](https://example.com/help)\n' +
      '2. **活动报名**：暂未回复。\n\n<script>bad()</script> [不安全](javascript:alert(1)) ' +
      '[数据](data:text/html;base64,PHNjcmlwdD4=)\n`**原样代码**`\n\n- 核对群聊\n- 联系同事',
      events:[{event_ref:'ref',title:'样品进展',creator_handle:'fixture',
      handling_status:'unresolved',evidence_ref:'ref',summary:'对方询问样品寄送进度。',importance:{tier:'focus',tier_label:'重点'},
      conversations:[{chat_id:'group',type:'group',name:'<img src=x onerror=alert(1)> 样品群'},
        {chat_id:'private',type:'private',name:'Fixture 私聊'}]}],
      coverage:{responsible_creators:201,matched_events:12,preview_count:1,analysis_complete:false},
      scope:{cm_id:ownId(),cm_name:'CM',audience:'all',creator_ids:null},
      window:{start:'2026-10-12T07:00:00Z',end:'2026-10-12T11:00:00Z'},
      draft:{draft:'**I will check.**',chinese_translation:'我会核对。',language:'en'},
      documents:[{document_ref:'doc.fixture',key:'fixture',title:'活动规则',kind:'knowledge',revision:2,
        text:'<img src=x onerror=alert(1)> 固定文档版本',truncated:false}],
      sections:[{title:'活动事项',summary:'活动报名需要回复。',message_refs:['m.admin.event'],
        sources:[{message_ref:'m.admin.event',conversation_name:'活动群',sender_name:'Amy',
          time:'2026-10-12T08:00:00Z',text:'I would like to attend.'}]},
        {title:'样品事项',summary:'样品进展需要核对。',message_refs:['m.admin.sample'],
          sources:[{message_ref:'m.admin.sample',conversation_name:'样品群',sender_name:'Sara',
            time:'2026-10-12T09:00:00Z',text:'Has the sample shipped?'}]}],
      data:[{kind:'window_messages',scope:{audience:'all'},total_messages:20,
        messages:[{conversation_name:'业务群',sender_name:'未识别联系人',time:'2026-10-12T09:00:00Z',
          text:'<script>source()</script> **本人原文**',evidence_ref:'raw-ref'}],coverage:{source_complete:false},
        gaps:[...Array.from({length:25},(_,i)=>({chat_id:`chat-${i}`,reason:'历史同步未覆盖检索起点'})),
          '历史回填尚未完成；请求早于已确认历史同步范围；同步水位未覆盖请求截止'],
        gap_count:1273,gap_conversation_count:1272,gap_summary:[
          {reason:'历史同步未覆盖检索起点',conversations:1272,items:0,
            names:['受限群一','<img src=x onerror=alert(1)>','群三','群四','群五']},
          {reason:'附件正文不可读取',conversations:0,items:5}]},...diagnosticData]};
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
    assert.match(await page.locator('.oa-event-meta').textContent(),/重要度：重点/);
    assert.equal(await page.locator('.oa-event .oa-card-meta').textContent(),
      '群聊：<img src=x onerror=alert(1)> 样品群私聊：Fixture 私聊');
    assert.equal(await page.locator('.oa-event img').count(),0);
    assert.equal(await page.locator('.oa-event p').textContent(),'对方询问样品寄送进度。');
    assert.match(await page.locator('.oa-message--assistant > p.oa-coverage').textContent(),
      /负责达人 201 位 · 匹配事项 12 项 · 部分结果/);
    assert.doesNotMatch(await page.locator('.oa-scope').textContent(),/授权|修改范围/);
    assert.match(await page.locator('.oa-source-entry').textContent(),/来源 · 20 条消息/);
    assert.equal(await page.locator('.oa-summary-section').count(),2);
    assert.equal(await page.locator('.oa-document-sources summary').textContent(),'参考文档 · 1');
    assert.equal(await page.locator('.oa-document-sources').getAttribute('open'),null);
    assert.match(await page.locator('.oa-document-source').textContent(),/活动规则.*版本 2.*固定文档版本/);
    assert.equal(await page.locator('.oa-document-source img').count(),0);
    assert.equal(await page.locator('.oa-section-sources[open]').count(),0);
    assert.match(await page.locator('.oa-data[data-query-ref="q.sync"]').textContent(),
      /首次同步诊断.*首次同步数量.*历史消息已读取.*最近同步时间.*测试用同步定义/);
    assert.match(await page.locator('.oa-data[data-query-ref="q.groups"]').textContent(),/已启用同步/);
    assert.equal(await page.locator('.oa-data details.oa-coverage').getAttribute('open'),null);
    assert.equal(await page.locator('.oa-history .oa-gaps').count(),1);
    assert.equal(await page.locator('.oa-history .oa-gaps summary').textContent(),'读取情况');
    assert.equal(await page.locator('.oa-history .oa-gaps').getAttribute('open'),null);
    assert.equal(await page.locator('.oa-history .oa-gaps > div').count(),3);
    assert.match(await page.locator('.oa-history .oa-gaps').textContent(),
      /历史回填尚未完成；请求早于已确认历史同步范围；同步水位未覆盖请求截止/);
    assert.match(await page.locator('.oa-history .oa-gaps').textContent(),/该时段消息尚未同步完成 · 1272 个会话/);
    assert.match(await page.locator('.oa-history .oa-gap-names').textContent(),
      /受限群一、<img src=x onerror=alert\(1\)>、群三、群四、群五/);
    assert.equal(await page.locator('.oa-history .oa-gaps img').count(),0);
    assert.doesNotMatch(await page.locator('.oa-history').textContent(),/部分资料尚不完整|来源覆盖不完整/);
    assert.equal(await page.locator('.oa-source-page strong, .oa-source-page a').count(),0);
    assert.match(await page.locator('.oa-source-page').textContent(),/\*\*本人原文\*\*/);
    assert.equal(await page.locator('.oa-draft textarea').inputValue(),'**I will check.**');
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    assert.equal(await page.locator('#oa-sources-body img').count(),0);
    assert.equal(await page.locator('#oa-sources-body strong').count(),0);
    assert.match(await page.locator('#oa-sources-body blockquote').textContent(),/\*\*本人原文\*\*/);
    await page.locator('#oa-sources-close').click();
    await page.locator('#oa-sources').waitFor({state:'hidden'});
    await page.getByRole('button',{name:'查看回复草稿',exact:true}).click();
    await page.locator('.oa-draft textarea').fill('Edited reply');
    const sessionsBeforeCopy = sessionReads.length, draftsBeforeCopy = draftReads.length;
    await page.getByRole('button',{name:'复制回复',exact:true}).click();
    await page.waitForFunction(() => window.copied==='Edited reply');
    assert.equal(sessionReads.length,sessionsBeforeCopy);
    assert.equal(draftReads.length,draftsBeforeCopy+1);
    assert.equal(posts.length,2);
    await page.locator('#oa-reply-close').click();
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-event').waitFor();
    assert.equal(posts.length,2);
    await page.locator('#oa-input').fill('再看最新进展');
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    await page.locator('#oa-cm').selectOption(remiId);
    assert.equal(await page.locator('#oa-input').inputValue(),'再看最新进展');
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data, .oa-summary-section').count(),0);
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    assert.equal(posts.length,2);
    await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(() => window.sources.length===1); assert.equal(posts.length,3);
    assert.notEqual(posts[2].body.session_id,posts[0].body.session_id);
    assert.equal(posts[2].body.context.cm_id,remiId);
    assert.deepEqual(posts[2].body.context,{cm_id:remiId});
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
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data, .oa-summary-section').count(),0);
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    assert.match(await page.locator('#oa-scope').textContent(),/Remi/);
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
    sessionGone=false;
    await page.evaluate(cm_id=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context:{cm_id}}})),remiId);
    await page.waitForFunction(id=>document.getElementById('oa-cm').value===id,remiId);
    await page.locator('#oa-input').fill('继续核对'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(()=>window.sources.length===1);
    assert.equal(posts.length,6);
    await page.evaluate(cm_id=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context:{cm_id}}})),ownId());
    assert.equal(await page.locator('#oa-cm').inputValue(),remiId);
    await page.locator('#oa-collapse').click();
    await page.evaluate(value=>{window.sources.at(-1).emit('result',value);
      window.sources.at(-1).emit('done',{status:'completed'});},
      {answer:'这轮已完成',events:[],scope:{cm_id:remiId,cm_name:'Remi',audience:'all'}});
    await page.waitForFunction(id=>document.getElementById('oa-cm').value===id,ownId());
    assert.equal(await page.locator('#oa-panel').isHidden(),true);
    assert.equal(posts.length,6);
    turnsEmpty=false;
    await page.locator('#oa-launcher').click();
    await page.locator('#oa-input').fill('查未回复事项'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(()=>window.sources.length===2); assert.equal(posts.length,7);
    const changedSession=view.session_id;
    await page.evaluate(value=>window.sources.at(-1).emit('result',value),result);
    await page.locator('.oa-event .oa-evidence').click(); await page.locator('#oa-sources-body blockquote').waitFor();
    await page.locator('#oa-sources-close').click();
    await page.locator('#oa-sources').waitFor({state:'hidden'});
    holdEvidence=true;
    const sourceRead=new Promise(resolve=>evidenceStarted=resolve);
    await page.locator('.oa-event .oa-evidence').click(); await sourceRead;
    view={...view,status:'failed',error_code:'source_changed',error:'查询资料已变化，请重新提问。',
      result:null,scope:{cm_id:remiId,cm_name:'Remi',audience:'creators',event_ref:'旧事项'}};
    await page.evaluate(value=>{
      const stream=window.sources.at(-1);
      stream.emit('error',{code:'source_changed',message:'查询资料已变化，请重新提问。',clear:true});
      stream.emit('result',value); stream.emit('done',{status:'completed'});
    },result);
    const sourceResponse=page.waitForResponse(response=>response.url().includes('/evidence/')&&response.status()===200);
    pendingEvidence.shift()(); await (await sourceResponse).finished(); holdEvidence=false;
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data, .oa-summary-section, ' +
      '#oa-sources-body blockquote').count(),0);
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    assert.match(await page.locator('#oa-status').textContent(),/资料已变化.*重新提问/);
    assert.equal(await page.locator('#oa-input').isDisabled(),false);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id),changedSession);
    // Restore an invalidated turn: do not adopt its old CM/event selection.
    await page.evaluate(session_id=>{
      const key='ttmedia.assistant.'+document.getElementById('operations-assistant').dataset.userId;
      localStorage.setItem(key,JSON.stringify({...JSON.parse(localStorage.getItem(key)),session_id}));
    },changedSession);
    await page.reload(); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-status').textContent.includes('资料已变化'));
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.doesNotMatch(await page.locator('#oa-scope').textContent(),/Remi|旧事项/);
    await page.locator('#oa-input').fill('重新查未回复事项'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(()=>window.sources.length===1); assert.equal(posts.length,8);
    assert.equal(posts[7].body.session_id,changedSession);
    assert.equal(posts[7].body.context.cm_id,ownId());
    assert.deepEqual(posts[7].body.context,{cm_id:ownId()});
    const activeTurn=view.id, activeRequest=JSON.stringify(posts[7].body);
    olderTurns=[{...view,id:require('node:crypto').randomUUID(),status:'failed',
      client_request_id:require('node:crypto').randomUUID(),error_code:'source_changed',
      message:'已失效的旧问题',error:'旧资料已变化，请重新提问。',
      result:{...result,answer:'不应出现的旧正文'},scope:{cm_id:remiId,cm_name:'Remi',event_ref:'旧事项'}}];
    await page.reload(); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>window.sources.length===1);
    assert.equal(posts.length,8); assert.equal(JSON.stringify(posts[7].body),activeRequest);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id),view.session_id);
    assert.match(await page.evaluate(()=>window.sources[0].url),new RegExp(`/turns/${activeTurn}/events$`));
    assert.equal(await page.locator('#oa-stop').isEnabled(),true);
    assert.equal(await page.locator('#oa-stop').isVisible(),true);
    assert.doesNotMatch(await page.locator('#oa-history').textContent(),/不应出现的旧正文|已失效的旧问题/);
    assert.doesNotMatch(await page.locator('#oa-scope').textContent(),/Remi|旧事项/);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('#oa-sources').isHidden(),true);
    olderTurns=[];
    await page.evaluate(value=>window.sources.at(-1).emit('result',value),result);
    holdEvidence=true;
    const protectedRead=new Promise(resolve=>evidenceStarted=resolve);
    await page.locator('.oa-event .oa-evidence').click(); await protectedRead;
    await page.locator('#oa-sources-close').click();
    await page.locator('#oa-sources').waitFor({state:'hidden'});
    denyEvidence=true; await page.locator('.oa-event .oa-evidence').click();
    await page.waitForFunction(()=>document.getElementById('oa-input').disabled);
    const protectedResponse=page.waitForResponse(response=>
      response.url().includes('/evidence/')&&response.status()===200);
    pendingEvidence.shift()(); await (await protectedResponse).finished();
    await page.evaluate(value=>window.sources.at(-1).emit('result',value),result);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data, .oa-summary-section, ' +
      '#oa-sources-body blockquote').count(),0);
    assert.match(await page.locator('#oa-history').textContent(),/连接或访问权限已变化.*检查账号连接/);
    assert.equal(await page.getByRole('link',{name:'检查账号连接',exact:true}).getAttribute('href'),'/dashboard');
    assert.equal(await page.getByRole('link',{name:'检查账号连接',exact:true}).isVisible(),true);
    assert.equal(await page.locator('#oa-input').isDisabled(),true);
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id),undefined);
    holdEvidence=false; denyEvidence=false;
    // An Owner's ordinary entry does not expose cross-CM selection or honor stale cross-CM browser state.
    await page.evaluate(cm_id=>{
      localStorage.setItem('ttmedia.assistant.'+document.getElementById('operations-assistant').dataset.userId,
        JSON.stringify({cm_id,session_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}));
    },remiId);
    const ordinaryReads=[];
    page.on('request',request=>{if(request.url().includes('/sessions/')) ordinaryReads.push(request.url());});
    await page.goto('http://localhost/'); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-cm').options.length===2);
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('#oa-scope-editor, #oa-audience, #oa-period, [data-scope-edit]').count(),0);
    assert(!ordinaryReads.some(url=>url.includes('aaaaaaaa-aaaa')));
    const ownerPosts=posts.length;
    await page.evaluate(cm_id=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context:{cm_id}}})),remiId);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(posts.length,ownerPosts);
    await page.locator('#oa-input').fill('看看今天的重点'); await page.locator('#oa-input').press('Enter');
    await page.waitForFunction(()=>window.sources.length===1);
    await page.evaluate(value=>window.sources.at(-1).emit('progress',value),progress);
    assert.equal(await page.locator('#oa-status').textContent(),'正在整理事项…');
    const ownerResult={answer:'今天的重点已经整理。',scope:{cm_id:ownId(),audience:'all'},events:[],
      sections:result.sections,
      coverage:{messages:20,conversations:5,cutoff:'2026-10-12T11:00:00Z'},data:result.data};
    view={...view,status:'completed',result:ownerResult};
    await page.evaluate(value=>{window.sources.at(-1).emit('result',value);
      window.sources.at(-1).emit('done',{status:'completed'});},ownerResult);
    assert.equal(await page.locator('.oa-data[data-query-ref="q.sync"]').count(),0);
    assert.equal(await page.locator('.oa-data[data-query-ref="q.groups"] small').count(),0);
    assert.equal(await page.locator('.oa-source-entry').count(),0);
    assert.equal(await page.locator('.oa-summary-section').count(),2);
    assert.equal(await page.locator('.oa-section-sources[open]').count(),0);
    assert.equal(await page.locator('.oa-history .oa-gaps summary').textContent(),'有些内容暂时无法查看');
    assert.doesNotMatch(await page.locator('#oa-history').textContent(),
      /首次同步|最近同步时间|测试用同步定义|1272 个会话|读取至/);
    assert.equal(await page.locator('.oa-queried-cms, .oa-cm-source').count(),0);

    // Multi-CM sources retain their own scope while the Owner's selected person and session stay fixed.
    const multiResult={answer:'已核对两位人员的合作事项。',scope:{cm_id:ownId(),cm_name:'CM',audience:'all',
      cm_ids:[ownId(),remiId],cm_names:['CM','Remi']},events:[],
      sections:result.sections.map((section,index)=>({...section,sources:section.sources.map(message=>
        ({...message,cm_id:index?remiId:ownId(),cm_name:index?'Remi':'CM'}))})),
      data:[{kind:'window_messages',query_ref:'q.owner',scope:{cm_id:ownId(),cm_name:'CM'},
        total_messages:4,page_count:4,page:1,has_more:true,source_url:'/assistant/sources/q.owner?page=1',
        messages:[{message_ref:'m.owner',cm_name:'CM',conversation_name:'本人合作群',text:'Own source.'}]},
        {kind:'window_messages',query_ref:'q.remi',scope:{cm_id:remiId,cm_name:'Remi'},
          total_messages:4,page_count:4,page:1,has_more:true,source_url:'/assistant/sources/q.remi?page=1',
          messages:[{message_ref:'m.remi',cm_name:'Remi',conversation_name:'Remi 合作群',text:'Remi source.'}]},
        {kind:'creators',query_ref:'q.creators',scope:{cm_id:remiId,cm_name:'Remi',audience:'creators'},
          creators:[],total:1,next_cursor:'owner-next'}]};
    view={...view,status:'completed',result:multiResult};
    const multiSession=view.session_id, multiPosts=posts.length;
    await page.goto('http://localhost/dashboard');
    await page.locator('#oa-launcher').click();
    await page.locator('.oa-queried-cms').waitFor();
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('.oa-source-entry').count(),2);
    assert.deepEqual(await page.locator('.oa-section-source .oa-cm-source').allTextContents(),['CM','Remi']);
    assert.deepEqual(await page.locator('.oa-source-entry > details > summary').allTextContents(),
      ['查看相关消息CM','查看相关消息Remi']);
    await page.goto('http://localhost/admin/messages'); await page.locator('#oa-launcher').click();
    await page.locator('.oa-queried-cms').waitFor();
    assert.equal(await page.locator('.oa-queried-cms').textContent(),'查询人员：CM、Remi');
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('.oa-source-entry').count(),2);
    assert.deepEqual(await page.locator('.oa-section-source .oa-cm-source').allTextContents(),['CM','Remi']);
    assert.equal(await page.locator('.oa-data[data-query-ref="q.creators"] .oa-cm-source').textContent(),'Remi');
    const remiSources=page.locator('.oa-source-entry[data-query-ref="q.remi"]');
    await remiSources.locator('summary').click();
    await remiSources.getByRole('button',{name:'下一页',exact:true}).click();
    await remiSources.getByText('第 2 / 4 页',{exact:true}).waitFor();
    assert.equal(await remiSources.locator('figcaption .oa-cm-source').textContent(),'Remi');
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(posts.length,multiPosts);
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-queried-cms').waitFor();
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('ttmedia.assistant.'+
      document.getElementById('operations-assistant').dataset.userId)).session_id),multiSession);
    await page.getByRole('button',{name:'继续下一页',exact:true}).click();
    await page.waitForFunction(()=>window.sources.length===1);
    assert.equal(posts.at(-1).body.session_id,multiSession);
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId(),query_ref:'q.creators',cursor:'owner-next'});
    view={...view,status:'completed',result:multiResult};
    await page.evaluate(value=>{window.sources.at(-1).emit('result',value);
      window.sources.at(-1).emit('done',{status:'completed'});},multiResult);
    view={...view,result:{...multiResult,scope:{...multiResult.scope,cm_ids:[remiId],cm_names:['Remi']},
      sections:[multiResult.sections[1]],data:[multiResult.data[1]]}};
    await page.reload(); await page.locator('#oa-launcher').click(); await page.locator('.oa-queried-cms').waitFor();
    assert.equal(await page.locator('.oa-queried-cms').textContent(),'查询人员：Remi');
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('.oa-source-entry').count(),1);
    await page.goto('http://localhost/'); await page.locator('#oa-launcher').click();
    selfReady=false;
    await page.reload(); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-status').textContent.includes('Lark 连接未就绪'));
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(await page.locator('#oa-send').isDisabled(),true);
    assert.equal(await page.locator('.oa-gaps').count(),0);
    assert.equal(await page.getByRole('link',{name:'检查账号连接',exact:true}).getAttribute('href'),'/dashboard');
    assert.equal(await page.getByRole('link',{name:'检查账号连接',exact:true}).isVisible(),true);
    selfReady=true;
    html=html.replace(/data-user-id="[^"]+"/,'data-user-id="33333333-3333-4333-8333-333333333333"');
    const reads=[]; page.on('request',request => {if(request.url().includes('/sessions/')) reads.push(request.url());});
    await page.goto('http://localhost/admin/messages'); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-cm').options.length===1);
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('.oa-event').count(),0); assert(!reads.some(url=>url.includes(previous)));
    const deniedSwitchPosts=posts.length;
    await page.evaluate(cm_id=>{
      document.getElementById('operations-assistant').dataset.canSwitchCm='true';
      const select=document.getElementById('oa-cm'), option=document.createElement('option');
      option.value=cm_id; option.textContent='Injected person'; select.append(option);
      select.value=cm_id; select.dispatchEvent(new Event('change'));
    },remiId);
    assert.equal(await page.locator('#oa-cm').inputValue(),ownId());
    assert.equal(await page.locator('#oa-person').isHidden(),true);
    assert.equal(posts.length,deniedSwitchPosts);
    await page.goto('http://localhost/'); await page.locator('#oa-launcher').click();
    await page.waitForFunction(()=>document.getElementById('oa-cm').options.length===1);
    assert.equal(await page.locator('#oa-person').isHidden(),true);

    // Complete worklist, non-AI source pagination, arbitrary message drafts, and hand-edited translation.
    const postCount=posts.length;
    const worklistSourceReads=sourceReads.length;
    assert.equal(await page.locator('[data-quick]').count(),3);
    await page.locator('[data-quick="today"]').click();
    await page.waitForFunction(()=>window.sources.length===1);
    assert.equal(posts.length,postCount+1); assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    assert.equal(posts.at(-1).body.message,'总结今天聊天里的重点事项。');
    await page.evaluate(value=>window.sources.at(-1).emit('progress',value),progress);
    assert.equal(await page.locator('#oa-status').textContent(),'正在整理事项…');
    await page.evaluate(value=>window.sources.at(-1).emit('progress',{...value,status:'waiting'}),progress);
    assert.equal(await page.locator('#oa-status').textContent(),'正在等待结果…');
    assert.equal(await page.locator('#oa-stop').isVisible(),true);
    const fullResult={answer:'找到 12 项事项，已按重要度排序。',
      scope:{cm_id:ownId(),cm_name:'CM',audience:'all',reply_status:'unreplied',sort:'importance'},
      events:Array.from({length:12},(_,i)=>({event_ref:`event-${i}`,title:`事项 ${i+1}`,creator_name:`达人 ${i+1}`,
        action_group:i===9?'followup':i===10?'waiting':i===11?'closed':'reply',reply_status:i===10?'replied':'unreplied',
        handling_status:i===11?'resolved':'unresolved',summary:'请核对当前情况。',
        importance:{tier:i<3?'focus':'normal'},deadline_day:i===0?'2026-10-12':null,due_today:i===0,
        waiting_seconds:3600,conversations:[{name:'客服群',type:'group'}]})),
      data:[{kind:'window_messages',query_ref:'q.fixture',source_url:'/assistant/sources/q.fixture?page=1',
        page:1,page_count:4,has_more:true,total_messages:353,total_conversations:43,
        window:{start:'2026-10-11T23:00:00Z',end:'2026-10-12T11:00:00Z'},
        gaps:[{chat_id:'oc_restricted',name:'TikTok Shop 受限群',reason:'群开启受限模式，Lark禁止读取消息（231203）'}],
        gap_summary:[{reason:'群开启受限模式，Lark禁止读取消息（231203）',
          conversations:1,names:['TikTok Shop 受限群']}],
        messages:[{conversation_name:'客服群',sender_name:'达人',message_ref:'m.fixture.first',text:'First source.'}]},
        {kind:'messages',query_ref:'q.search',next_source_url:'/assistant/sources/q.search?page=2&cursor=fixture',
          gaps:[{chat_id:'oc_restricted',name:'TikTok Shop 受限群',reason:'群开启受限模式，Lark禁止读取消息（231203）'},
            {chat_id:'oc_unknown',name:'未知不可读群',reason:'Lark暂不允许读取此会话消息（错误码 999999）'},
            {chat_id:'oc_pending',name:'这段时间不全的群',
              reason:'历史回填尚未完成；请求早于已确认历史同步范围；同步水位未覆盖请求截止'},
            {chat_id:'oc_not_ready',name:'暂不可读群',reason:'尚无同步水位；未启用消息同步；同步尚未成功'},
            {reason:'非文字消息未分析'}],
          gap_summary:[{reason:'群开启受限模式，Lark禁止读取消息（231203）',
            conversations:1,names:['TikTok Shop 受限群']}],
          messages:[{conversation_name:'普通消息群',sender_name:'商家',
            message_ref:'m.fixture.search',text:'Matching source.'}]},...diagnosticData],
      coverage:{messages:353,conversations:43,cutoff:'2026-10-12T11:00:00Z'}};
    const deliver=async result=>{view={...view,status:'completed',result}; await page.evaluate(value=>{
      window.sources.at(-1).emit('result',value); window.sources.at(-1).emit('done',{status:'completed'});},result);};
    const nextStream=async action=>{const count=await page.evaluate(()=>window.sources.length);
      await action(); await page.waitForFunction(count=>window.sources.length===count+1,count);};
    await deliver(fullResult);
    assert.equal(await page.locator('.oa-event').count(),12);
    assert.equal(await page.locator('.oa-event[open]').count(),8);
    assert.equal(await page.locator('.oa-event-group').count(),4);
    assert.equal(await page.locator('.oa-deadline').textContent(),'今天截止');
    assert.match(await page.locator('#oa-scope').textContent(),/未回复.*重要度优先/);
    assert.equal(await page.locator('.oa-event-group').last().getAttribute('open'),null);
    assert.match(await page.locator('.oa-event-group').last().textContent(),/已解决／无需处理.*未见回复.*已解决/);
    assert.equal(await page.locator('.oa-source-entry').count(),1);
    assert.equal(await page.locator('.oa-history .oa-gaps').count(),1);
    assert.equal(await page.locator('.oa-history .oa-gaps > div').count(),4);
    assert.equal(await page.locator('.oa-history .oa-gaps summary').textContent(),'有些内容暂时无法查看');
    assert.equal(await page.locator('.oa-source-entry > details > summary').textContent(),'查看相关消息');
    assert.equal(await page.locator('.oa-data[data-query-ref="q.sync"]').count(),0);
    assert.equal(await page.locator('.oa-data[data-query-ref="q.groups"] small').count(),0);
    assert.doesNotMatch(await page.locator('#oa-history').textContent(),
      /首次同步|最近同步时间|测试用同步定义|353 条消息|43 个会话|读取至/);
    assert.match(await page.locator('.oa-history .oa-gap-names').first().textContent(),/TikTok Shop 受限群/);
    assert.match(await page.locator('.oa-history .oa-gaps').textContent(),/群开启受限模式，无法读取消息/);
    assert.match(await page.locator('.oa-history .oa-gaps').textContent(),/图片、附件等消息暂无法解析/);
    assert.match(await page.locator('.oa-history .oa-gaps').textContent(),
      /此会话消息暂时无法查看会话：未知不可读群、暂不可读群/);
    assert.equal((await page.locator('.oa-history .oa-gaps').textContent()).match(/这段时间的消息暂时不全/g).length,1);
    assert.doesNotMatch(await page.locator('.oa-history .oa-gaps').textContent(),
      /同步水位|历史回填|未启用消息同步|同步尚未成功|错误码|999999/);
    assert.match(await page.locator('#oa-scope').textContent(),/10\/12 00:00.*10\/12 12:00/);
    assert.doesNotMatch(await page.locator('.oa-history .oa-gaps').textContent(),/2 个会话|部分授权/);
    await page.reload(); await page.locator('#oa-launcher').click();
    await page.locator('.oa-event').first().waitFor();
    assert.match(await page.locator('#oa-scope').textContent(),/未回复.*重要度优先/);
    assert.equal(posts.length,postCount+1);
    await page.locator('.oa-source-entry > details > summary').click();
    await page.locator('.oa-source-pagination').getByRole('button',{name:'下一页',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.oa-source-pagination').textContent.includes('第 2 / 4 页'));
    assert.deepEqual(sourceReads.slice(worklistSourceReads),['2']); assert.equal(posts.length,postCount+1);
    await page.locator('.oa-data[data-query-ref="q.search"]').getByRole('button',{name:'下一页',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.oa-data[data-query-ref="q.search"]')
      .textContent.includes('Search continuation.'));
    assert.deepEqual(sourceReads.slice(worklistSourceReads),['2','2']); assert.equal(posts.length,postCount+1);
    assert.equal(await page.locator('.oa-data[data-query-ref="q.search"] .oa-reply-action').count(),1);
    await page.locator('#oa-fullscreen').click();
    olderTurns=[view];
    await nextStream(()=>page.locator('.oa-source-page .oa-reply-action').click());
    assert.equal(posts.at(-1).body.context.message_ref,'m.fixture.second');
    assert.equal(posts.at(-1).body.context.reply_action,'generate');
    const draft={draft_ref:'d.fixture',message_ref:'m.fixture.second',source_ref:'m.fixture.second',language:'en',
      draft:'I will check and get back to you.',chinese_translation:'我会核对后回复你。',
      context:{chat_name:'商家客服群',chat_url:'https://applink.larksuite.com/client/chat/open?openChatId=oc_fixture',
        message:{chat_id:'oc_fixture',message_id:'target',chat_name:'商家客服群',sender_name:'未分类商家',
          text:'Can you check the dispatch status?'},
        messages:Array.from({length:8},(_,i)=>({chat_id:'oc_fixture',message_id:i===0?'target':`context-${i}`,
          ref:`R${i+1}`,chat_name:'商家客服群',sender_name:i===1?'CM':'未分类商家',
          text:i===0?'Can you check the dispatch status?':i===1?'Please check the dispatch status.':
            `Other context ${i}. ${'Full original body. '.repeat(30)}`}))},
      evidence:[{chat_id:'oc_fixture',message_id:'target',ref:'R1',chat_name:'商家客服群',
        sender_name:'未分类商家',text:'Can you check the dispatch status?'},
        {chat_id:'oc_fixture',message_id:'context-1',ref:'R2',chat_name:'商家客服群',sender_name:'CM',
          text:'Please check the dispatch status.'},
        {chat_id:'oc_fixture',message_id:'context-1',ref:'R2',chat_name:'商家客服群',sender_name:'CM',
          text:'Please check the dispatch status.'}],
      checks:['请确认能完成这项跟进。']};
    const draftResult={answer:'草稿已准备好。',scope:fullResult.scope,draft};
    await deliver(draftResult);
    assert.equal(await page.locator('.oa-reply-context > figure').count(),2);
    assert.equal(await page.locator('.oa-reply-more').getAttribute('open'),null);
    assert.equal(await page.locator('.oa-reply-more > summary').textContent(),'更多上下文（6）');
    assert.equal(await page.locator('.oa-reply-more figure').count(),6);
    assert.equal(await page.locator('#oa-reply textarea').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.querySelector('#oa-reply textarea').getBoundingClientRect().bottom <=
      document.getElementById('oa-reply-body').getBoundingClientRect().bottom),true);
    await page.locator('.oa-reply-more > summary').click();
    assert.equal(await page.locator('.oa-reply-more blockquote').first().textContent(),
      `Other context 2. ${'Full original body. '.repeat(30)}`);
    await page.locator('.oa-reply-more > summary').click();
    await page.locator('#oa-reply textarea').fill('I will check the status today.');
    olderTurns.push(view);
    await nextStream(()=>page.getByRole('button',{name:'更新中文',exact:true}).click());
    assert.equal(posts.at(-1).body.context.edited_draft,'I will check the status today.');
    assert.equal(posts.at(-1).body.context.reply_action,'translate');
    await deliver({...draftResult,draft:{...draft,draft:'I will check the status today.',
      chinese_translation:'我今天会核对状态。'}});
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'I will check the status today.');
    assert.equal(await page.locator('#oa-reply .oa-translation').textContent(),'我今天会核对状态。');
    await page.locator('#oa-reply textarea').fill('My hand-edited reply.');
    olderTurns.push(view);
    await nextStream(()=>page.getByRole('button',{name:'更新中文',exact:true}).click());
    await page.locator('#oa-reply textarea').fill('Later edit during translation.');
    await deliver({...draftResult,draft:{...draft,draft:'My hand-edited reply.',chinese_translation:'旧快照的译文。'}});
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'Later edit during translation.');
    assert.equal(await page.locator('#oa-reply .oa-translation').textContent(),'我今天会核对状态。');
    assert.match(await page.locator('.oa-translation-label').textContent(),/待更新/);
    olderTurns.push(view);
    await nextStream(()=>page.getByRole('button',{name:'更短',exact:true}).click());
    await page.locator('#oa-reply textarea').fill('My hand-edited reply.');
    await deliver({...draftResult,draft:{...draft,draft:'Short rewritten draft.'}});
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'My hand-edited reply.');
    assert.equal(await page.locator('.oa-draft-update').count(),1);
    newDraftMessages=true;
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('.oa-draft-new-message').waitFor();
    assert.equal(posts.length,postCount+5);
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'My hand-edited reply.');
    assert.match(await page.locator('.oa-translation-label').textContent(),/待更新/);
    olderTurns.push(view);
    await nextStream(()=>page.getByRole('button',{name:'更新草稿',exact:true}).click());
    await deliver({...draftResult,draft:{...draft,draft:'A new incoming suggestion.'}});
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'My hand-edited reply.');
    assert.equal(await page.locator('.oa-draft-update').count(),1);
    await page.getByRole('button',{name:'复制并打开 Lark',exact:true}).click();
    await page.waitForFunction(()=>window.copied==='My hand-edited reply.');
    assert.equal(await page.evaluate(()=>window.opened),draft.context.chat_url);
    assert.equal(await page.locator('.oa-copy-feedback a').getAttribute('href'),draft.context.chat_url);
    assert.equal(await page.locator('.oa-copy-feedback a').getAttribute('rel'),'noopener noreferrer');
    assert.equal(posts.length,postCount+6);
    assert.match(await page.locator('.oa-copy-feedback').textContent(),/未发送/);
    assert.match(await page.locator('.oa-translation-label').textContent(),/待更新/);
    if (input.screenshot) await page.screenshot({path:input.screenshot.replace('.png','-reply.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <=
      document.documentElement.clientWidth),true);
    if (input.screenshot) await page.screenshot({path:input.screenshot.replace('.png','-mobile.png'),fullPage:false});
    await page.setViewportSize({width:1280,height:900});
    olderTurns.push(view);
    await nextStream(()=>page.locator('.oa-hit .oa-reply-action').click());
    assert.equal(await page.locator('#oa-reply-title').textContent(),'商家客服群');
    const secondDraft={...draft,draft_ref:'d.second',message_ref:'u.fixture.message',draft:'Reply to the second chat.',
      context:{...draft.context,chat_name:'普通消息群',chat_url:'https://applink.larksuite.com/client/chat/open?openChatId=oc_second'}};
    await deliver({...draftResult,draft:secondDraft});
    assert.equal(await page.locator('#oa-reply-title').textContent(),'商家客服群');
    assert.match(await page.locator('.oa-draft-update').textContent(),/普通消息群/);
    await page.getByRole('button',{name:'复制并打开 Lark',exact:true}).click();
    await page.waitForFunction(()=>window.opened.includes('oc_fixture'));
    await page.locator('.oa-draft-update').click();
    assert.equal(await page.locator('#oa-reply-title').textContent(),'普通消息群');
    assert.equal(await page.locator('#oa-reply textarea').inputValue(),'Reply to the second chat.');
    await page.locator('#oa-reply-close').click();
    assert.equal(await page.locator('#oa-scope button, #oa-scope-editor, #oa-audience, #oa-period').count(),0);
    await page.locator('#oa-input').fill('查看10月11日到10月12日我负责达人的未回复事项，重要度优先');
    await nextStream(()=>page.locator('#oa-input').press('Enter'));
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    await deliver({...fullResult,scope:{...fullResult.scope,audience:'creators'},window:{
      start:'2026-10-10T23:00:00Z',end:'2026-10-12T11:00:00Z'}});
    assert.match(await page.locator('#oa-scope').textContent(),/我的达人.*10\/11.*10\/12/);
    assert.doesNotMatch(await page.locator('#oa-scope').textContent(),/授权|修改范围/);
    if (input.screenshot) await page.screenshot({path:input.screenshot,fullPage:true});
    const aliceScope={cm_id:ownId(),audience:'creators',creator_ids:['44444444-4444-4444-8444-444444444444'],
      creator_names:['Alice'],chat_ids:['oc_alice'],chat_names:['Alice group'],event_ref:'alice-event'};
    await page.evaluate(context=>document.dispatchEvent(new CustomEvent('ttmedia:assistant',
      {detail:{context}})),aliceScope);
    await page.waitForFunction(()=>document.getElementById('oa-scope').textContent.includes('Alice'));
    await page.locator('#oa-input').fill('总结昨天整天');
    await nextStream(()=>page.locator('#oa-input').press('Enter'));
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    await deliver({answer:'昨天的事项已整理。',scope:{cm_id:ownId(),audience:'all'},events:[],data:[]});
    await nextStream(()=>page.locator('[data-quick="unreplied"]').click());
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    assert.match(posts.at(-1).body.message,/我负责达人的未回复事项/);
    await deliver({answer:'待回复事项已整理。',scope:{cm_id:ownId(),audience:'creators'},events:[],data:[]});
    await nextStream(()=>page.locator('[data-quick="reply"]').click());
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    assert.match(posts.at(-1).body.message,/刚才讨论的消息/);
    const ordinarySession=view.session_id;
    view={...view,status:'failed',error_code:'source_changed',result:null,
      scope:{cm_id:ownId(),event_ref:'old-ref'},error:'旧资料已变化。'};
    await page.evaluate(()=>window.sources.at(-1).emit('error',
      {code:'source_changed',message:'旧资料已变化。',clear:true}));
    assert.equal(await page.locator('#oa-status').textContent(),'相关消息已更新，可以继续提问。');
    assert.equal(await page.locator('.oa-event, .oa-draft, .oa-data, .oa-summary-section').count(),0);
    await page.locator('#oa-input').fill('继续刚才的事项');
    await nextStream(()=>page.locator('#oa-input').press('Enter'));
    assert.equal(posts.at(-1).body.session_id,ordinarySession);
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    await deliver({answer:'事项已更新。',scope:{cm_id:ownId()},events:[],data:[]});
    await page.locator('#oa-new').click();
    await page.locator('#oa-input').fill('一个新的问题');
    await nextStream(()=>page.locator('#oa-input').press('Enter'));
    assert.notEqual(posts.at(-1).body.session_id,ordinarySession);
    assert.deepEqual(posts.at(-1).body.context,{cm_id:ownId()});
    const categorized={answer:'今天有两类事项需要关注。',scope:{cm_id:ownId(),audience:'all'},
      window:{start:'2026-10-12T23:00:00Z',end:'2026-10-13T11:00:00Z'},events:[],
      sections:[{title:'活动报名',summary:'Amy 询问是否还能报名。需要确认活动名额后回复。',
        message_refs:['m.section.group'],sources:[{message_ref:'m.section.group',
          conversation_name:'Gifting Season 活动群',sender_name:'Amy',time:'2026-10-13T07:00:00Z',
          text:'<img src=x onerror=alert(1)> **Hi, is it too late to attend?**'}]},
        {title:'样品寄送',summary:'Sara 询问样品寄送进度。请先核对物流信息。',
          message_refs:['m.section.private'],sources:[{message_ref:'m.section.private',
            conversation_name:'Sara 私聊',sender_name:'Sara',time:'2026-10-13T08:00:00Z',
            text:'Has the sample been shipped?',truncated:true}]}],
      data:[fullResult.data[0],{kind:'conversations',query_ref:'q.conversations',scope:{cm_id:ownId()},
        total:2,next_cursor:'next-directory-page',conversations:[
          {id:'oc_gifting',name:'Gifting Season 活动群',mode:'group',
            matched_people:[{id:'person-amy',name:'Amy'}],readable:true,match_basis:['participant_name']},
          {id:'oc_sara',name:'Sara 私聊',mode:'p2p',matched_people:[{id:'person-sara',name:'Sara'}],
            readable:false,match_basis:['conversation_name']}]},
        {kind:'activity',scope:{audience:'all'},metrics:{messages:12,own_messages:3}}]};
    await deliver(categorized);
    const categories=page.locator('.oa-summary-section');
    assert.deepEqual(await categories.locator('h4').allTextContents(),['活动报名','样品寄送']);
    assert.equal(await page.locator('.oa-section-sources[open]').count(),0);
    assert.equal(await page.locator('.oa-source-entry').count(),0);
    assert.match(await page.locator('.oa-data[data-query-ref="q.conversations"]').textContent(),
      /会话目录.*Gifting Season 活动群.*群聊.*Amy.*Sara 私聊.*私聊.*Sara/);
    assert.doesNotMatch(await page.locator('.oa-data[data-query-ref="q.conversations"]').textContent(),
      /participant_name|conversation_name|同步|发送权限/);
    assert.equal(await page.locator('.oa-data').filter({has:page.getByRole('heading',{name:'活动统计'})}).count(),1);
    if (input.screenshot) await page.screenshot({
      path:input.screenshot.replace('.png','-sections-default.png'),fullPage:true});
    const categoryPostCount=posts.length;
    await categories.first().locator('summary').click();
    assert.equal(await categories.nth(1).locator('details').getAttribute('open'),null);
    assert.match(await categories.first().textContent(),/Gifting Season 活动群.*Amy.*10\/13.*Hi, is it too late/);
    assert.doesNotMatch(await categories.first().textContent(),/Sara|shipped/);
    assert.equal(await categories.first().locator('img, script, blockquote strong').count(),0);
    assert.equal(await categories.first().locator('blockquote').textContent(),
      '<img src=x onerror=alert(1)> **Hi, is it too late to attend?**');
    await categories.nth(1).locator('summary').click();
    assert.doesNotMatch(await categories.nth(1).textContent(),/Amy|Gifting Season/);
    assert.equal(posts.length,categoryPostCount);
    await page.evaluate(()=>{document.getElementById('oa-history').scrollTop=0;});
    if (input.screenshot) await page.screenshot({path:input.screenshot.replace('.png','-sections.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.getElementById('oa-panel').getBoundingClientRect().height===innerHeight);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <=
      document.documentElement.clientWidth),true);
    if (input.screenshot) await page.screenshot({
      path:input.screenshot.replace('.png','-sections-mobile.png'),fullPage:false});
    await page.setViewportSize({width:1280,height:900});
    await categories.nth(1).getByRole('button',{name:'查看原文',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('oa-sources-body').textContent
      .includes('Full private message and question context.'));
    assert.equal(evidenceReads.at(-1),'m.section.private');
    assert.equal(posts.length,categoryPostCount);
    await page.locator('#oa-sources-close').click();
    await nextStream(()=>categories.nth(1).getByRole('button',{name:'帮我回复',exact:true}).click());
    assert.deepEqual(posts.at(-1).body.context,
      {cm_id:ownId(),message_ref:'m.section.private',reply_action:'generate'});
    await deliver({answer:'草稿已准备好。',scope:categorized.scope,draft:{...draft,
      draft_ref:'d.section.private',source_ref:'m.section.private',message_ref:'m.section.private',
      context:{...draft.context,chat_name:'Sara 私聊',message:{chat_name:'Sara 私聊',sender_name:'Sara',
        text:'Has the sample been shipped?'},messages:[],
        chat_url:'https://applink.larksuite.com/client/chat/open?openChatId=oc_sara'}}});
    assert.equal(await page.locator('#oa-reply-title').textContent(),'Sara 私聊');
    await page.locator('#oa-reply-close').click();
    denyEvidence=true;
    await categories.first().getByRole('button',{name:'查看原文',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('oa-input').disabled);
    assert.equal(await page.locator('.oa-summary-section, .oa-data, .oa-draft, #oa-sources-body blockquote').count(),0);
    assert.equal(await page.getByRole('link',{name:'检查账号连接',exact:true}).isVisible(),true);
    assert.deepEqual(errors,[]); console.log('assistant browser workflow passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"html": html, "backend": str(BACKEND),
                            "screenshot": os.environ.get("ASSISTANT_UI_SCREENSHOT")}),
                            text=True, capture_output=True, timeout=60, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
