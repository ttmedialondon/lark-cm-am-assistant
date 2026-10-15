"""Read-only HTTP and template boundaries; backend scope checks have their own SQL tests."""

import json
import os
import re
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin
from app import offhours_summary_routes as routes

APP = Path(__file__).resolve().parents[1] / "app"


def render(data):
    data = {"responsible": {"id": "fixture-cm", "name": "Tester"},
            "selection": {"cms": [], "dates": [], "date": "2026-09-15", "period": "work"}, **data}
    env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    return env.get_template("offhours_summary.html").render(
        authorization={"display_name": "Tester"}, summary_data=data,
    )


def test_reply_knowledge_requires_explicit_single_opt_in():
    app = FastAPI()

    @app.post("/form")
    async def parse_reply_form(fields: Annotated[dict[str, str], Depends(admin.assistant_rule_fields)]):
        return fields

    client = TestClient(app)
    form = {"csrf_token": "csrf", "document": "doc-fixture", "revision": "0", "title": "Reply policy",
            "description": "Approved facts", "kind": "knowledge", "status": "published",
            "visibility": "all", "content": "Approved customer-facing facts."}
    assert "reply_usable" not in client.post("/form", data=form).json()
    assert client.post("/form", data={**form, "reply_usable": "1"}).json()["reply_usable"] == "1"
    for body in (urlencode({**form, "reply_usable": "true"}),
                 urlencode({**form, "reply_usable": "1"}) + "&reply_usable=1"):
        assert client.post("/form", content=body,
                           headers={"content-type": "application/x-www-form-urlencoded"}).json() == {}


def test_template_never_exposes_admin_to_a_viewer_and_escapes_private_json():
    hostile = "</script><script>alert(1)</script>"
    html = render({"can_manage": False, "events": [{"title": hostile}]})
    assert 'href="/admin/' not in html
    assert hostile not in html
    raw = re.search(r'<script id="offhours-summary-data" type="application/json">(.*?)</script>', html).group(1)
    assert json.loads(raw)["events"][0]["title"] == hostile
    assert 'href="/admin/ai#offhours-summary"' in render({"can_manage": True})


def test_reply_browser_polls_only_generation_and_rechecks_before_copy():
    html = render({"enabled": True, "preparing": True, "window": {}, "run": {"status": "completed"}, "events": [{
        "id": "event:0", "title": "<img src=x onerror=alert(1)> 样品尚未收到",
        "priority": "P1", "handling_status": "unresolved",
        "summary": "请跟进样品。", "latest_progress": "等待回复。", "evidence": [],
    }]})
    preparing_html = render({"enabled": True, "preparing": True, "window": {}, "events": [],
        "run": {"status": "queued", "counts": {"failed": 0}, "gaps": ["模型暂不可用，请联系管理员。"]}})
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require('playwright');
(async () => {
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  try {
    const page=await browser.newPage(), errors=[], reads=[];
    let deny=false, html=input.preparing_html;
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{
      value:{writeText:async text=>{window.copied=text;}}
    }));
    await page.clock.install();
    await page.route('**/*',async route=>{
      const request=route.request(), url=new URL(request.url());
      if(url.origin!=='http://localhost') return route.abort();
      if(url.pathname==='/') return route.fulfill({contentType:'text/html',body:html});
      if(url.pathname.startsWith('/static/')) return route.fulfill({
        contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css',
        body:url.pathname.endsWith('.js')?fs.readFileSync(path.join(input.app,url.pathname.slice(1)),'utf8'):''
      });
      if(url.pathname==='/dashboard/offhours-summary/data') return route.fulfill({status:304});
      if(url.pathname==='/dashboard/offhours-summary/replies') {
        assert.equal(request.method(),'POST');
        return route.fulfill({status:202,json:{id:'job-1'}});
      }
      if(url.pathname==='/dashboard/offhours-summary/replies/job-1') {
        assert.equal(url.searchParams.get('fresh'),'true'); reads.push(url.href);
        if(deny) return route.fulfill({status:403,json:{error:'revoked'}});
        return route.fulfill({json:reads.length===1?{status:'running'}:{
          status:'succeeded',result:{outcome:'ready',draft:'I will check your sample.',
            chinese_translation:'我会核查样品。',language:'en',explanation:'请核对后发送。',checks:[]}
        }});
      }
      throw new Error('Unexpected request: '+url.href);
    });
    await page.goto('http://localhost/');
    assert.equal(await page.locator('#oh-status-title').textContent(),'正在准备近期聊天');
    assert.equal(await page.locator('#oh-empty').isVisible(),false);
    assert.equal(await page.locator('#oh-analysis-spinner').isVisible(),false);
    assert.equal(await page.locator('#oh-refresh').isEnabled(),true);
    assert.equal(await page.locator('#oh-issues').isVisible(),true);
    assert.equal(await page.locator('#oh-issues').getAttribute('open'),null);
    html=input.html;
    await page.reload();
    assert.notEqual(await page.locator('#oh-status-title').textContent(),'正在准备近期聊天');
    assert.equal(await page.locator('.oh-event img, .oh-event script').count(),0);
    await page.getByRole('button',{name:'生成回复建议',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.oh-reply button').getAttribute('aria-busy')==='true');
    await page.clock.runFor(2600);
    const copy=page.getByRole('button',{name:'复制回复',exact:true});
    await copy.waitFor({state:'visible'});
    await page.locator('.oh-reply textarea').fill('My edited sample reply.');
    assert.equal(reads.length,2); // Pending work continues polling until completion.
    assert.equal(await copy.isEnabled(),true);
    await page.getByRole('button',{name:'P2',exact:true}).click();
    assert.equal(await page.locator('#oh-events .oh-reply').count(),0);
    await page.clock.runFor(65000);
    assert.equal(reads.length,2); // Neither completed nor detached drafts perform idle reads.
    await page.getByRole('button',{name:'全部事件',exact:true}).click();
    await copy.click();
    await page.waitForFunction(()=>window.copied==='My edited sample reply.');
    assert.equal(reads.length,3);
    deny=true;
    await copy.click();
    await page.waitForFunction(()=>document.querySelectorAll('.oh-reply').length===0);
    assert.equal(reads.length,4);
    assert.equal(await page.locator('body').textContent().then(text=>text.includes('I will check your sample.')),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({
        "html": html, "preparing_html": preparing_html, "app": str(APP)}),
                            text=True, capture_output=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def client(monkeypatch):
    auth = {"id": uuid4(), "status": "active"}
    calls = []
    conn = SimpleNamespace(transaction=nullcontext)
    monkeypatch.setattr(routes, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(routes.user_preparation, "status_for", lambda *_: {"preparing": False})
    monkeypatch.setattr(
        routes.offhours_summary, "get_summary_view",
        lambda connection, actor, **kwargs: calls.append((connection, actor)) or {"events": []},
    )
    monkeypatch.setattr(routes.summary_periods, "selection",
                        lambda conn, actor, *args: (actor, None, None, {"date": "2026-09-15", "period": "work"}))
    app = FastAPI()
    def authorization(_request):
        import asyncio

        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        auth["session_reads"] = auth.get("session_reads", 0) + 1
        return auth

    def render_page(_request, name, context, status_code=200):
        env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
        body = env.get_template(name).render(**context) if name == "error.html" else "private-summary-fixture"
        return HTMLResponse(body, status_code=status_code)

    app.include_router(routes.create_router(
        current_authorization=authorization,
        render_template=render_page, csrf_token=lambda _: "test-csrf",
    ))
    return TestClient(app), auth, calls, conn


def test_route_uses_session_identity_not_query_scope_and_is_uncacheable(client):
    api, auth, calls, conn = client
    result = api.get(f"/dashboard/offhours-summary/data?responsible_id={uuid4()}&actor_id={uuid4()}")
    assert result.status_code == 200
    assert auth["session_reads"] == 1
    assert calls == [(conn, auth["id"])]
    assert result.headers["cache-control"].startswith("no-store")
    assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
    assert result.headers["x-content-type-options"] == "nosniff"
    headers = {"If-None-Match": result.headers["etag"]}
    assert api.get("/dashboard/offhours-summary/data", headers=headers).status_code == 304
    assert len(calls) == 2  # Conditional reads still check live scope and message material.
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/data", headers=headers).status_code == 403
    assert api.post("/dashboard/offhours-summary/data", json={}).status_code == 405


def test_revoked_session_and_backend_scope_denial_never_render_data(client, monkeypatch):
    api, auth, calls, _ = client
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/data").status_code == 403
    assert not calls
    auth["status"] = "active"

    def deny(*_, **kwargs):
        raise PermissionError("当前账号不能查看此总结。")

    monkeypatch.setattr(routes.offhours_summary, "get_summary_view", deny)
    result = api.get("/dashboard/offhours-summary")
    assert result.status_code == 403
    assert auth["session_reads"] == 2
    assert "private-summary-fixture" not in result.text
    assert "返回工作台" in result.text
    assert result.headers["cache-control"].startswith("no-store")


def test_source_cm_authorization_loss_returns_forbidden_so_browser_clears_results(client, monkeypatch):
    api, _, _, _ = client

    def revoked_source(*_, **kwargs):
        raise routes.offhours_summary.SummaryInputError("Remi 的消息读取授权已失效。")

    monkeypatch.setattr(routes.offhours_summary, "get_summary_view", revoked_source)
    for path in ("/dashboard/offhours-summary", "/dashboard/offhours-summary/data"):
        result = api.get(path)
        assert result.status_code == 403
        assert result.headers["cache-control"].startswith("no-store")
        assert "private-summary-fixture" not in result.text


def test_reply_creation_requires_session_csrf_and_server_scope(client, monkeypatch):
    api, auth, _, _ = client
    captured = []
    monkeypatch.setattr(routes.offhours_summary, "get_settings", lambda: object())
    monkeypatch.setattr(routes.summary_replies, "enqueue", lambda conn, actor, event, settings, **kwargs:
                        captured.append((actor, event)) or "reply-job")
    assert api.post("/dashboard/offhours-summary/replies", json={"event_id": "test:0"}).status_code == 403
    result = api.post("/dashboard/offhours-summary/replies", headers={"X-CSRF-Token": "test-csrf"},
                      json={"event_id": "test:0", "actor_id": str(uuid4())})
    assert result.status_code == 202 and captured == [(auth["id"], "test:0")]
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/replies/reply-job").status_code == 403
