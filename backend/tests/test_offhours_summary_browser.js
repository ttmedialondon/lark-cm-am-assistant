/* Isolated browser fixtures only: these tests never call OpenAI or production. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {chromium} = require("playwright");
const backend = path.resolve(__dirname, "..");
const event = (priority, extra = {}) => ({id: priority, creator_handle: "sample.handle", lark_user_name: "Sample Creator",
  priority, title: `${priority} fixture event`, summary: "这是用于浏览器测试的假数据，不会写入数据库。", latest_progress: "在所选时段内尚未看到进一步回复。",
  handling_status: ({P0: "resolved", P1: "unresolved", P2: "uncertain", P3: "not_required"})[priority],
  handling_reason: "截至所选时段判断。",
  reason: "用于验证界面布局与排序。", needs_review: false,
  evidence: [{message_id: "message-1", chat_id: "chat-1", text: "Please check this issue.", created_at: "2026-09-09T20:00:00Z", sender_name: "Sample Creator"}], ...extra});
const report = (extra = {}) => ({responsible: {id: "remi-id", name: "Remi"}, enabled: true, can_manage: false,
  selection: {cms: [{id: "remi-id", name: "Remi"}], dates: ["2026-09-09"], date: "2026-09-09", period: "off"},
  window: {start: "2026-09-09T17:00:00Z", end: "2026-09-10T11:00:00Z", timezone: "Europe/London", is_partial: true, is_open: true},
  run: {id: "run-1", status: "running", updated_at: "2026-09-10T01:00:00Z", cutoff: "2026-09-10T01:00:00Z",
    counts: {queued: 1, running: 2, completed: 4, failed: 1}, gaps: ["有一段会话同步不完整，尚不能下结论。"]},
  events: [event("P2"), event("P0"), event("P3"), event("P1"), event(null, {creator_handle: null, title: "资料尚不足以判断", needs_review: true})], ...extra});

async function fixture(t, data = report()) {
  const state = {data, calls: [], status: 200};
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
    "import json,sys; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); print(Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('offhours_summary.html').render(authorization={'display_name':'Reviewer'},summary_data=data,csrf_token='fixture-csrf'))"],
    {cwd: backend, input: JSON.stringify(data), encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1360, height: 1000}});
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://localhost") {
      if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (url.pathname === "/dashboard/offhours-summary") return route.fulfill({contentType: "text/html", body: rendered.stdout});
    if (url.pathname.startsWith("/static/")) return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css", body: fs.readFileSync(path.join(backend, "app", url.pathname))});
    state.calls.push({method: route.request().method(), url: url.pathname, search: url.search});
    if (url.pathname.endsWith("/data")) return route.fulfill({status: state.status, json: state.data});
    return route.abort();
  });
  await page.goto("http://localhost/dashboard/offhours-summary");
  await page.waitForFunction(() => document.getElementById("oh-status-title").textContent !== "正在读取总结");
  return {page, state, errors};
}

test("bilingual user labels are displayed literally in evidence", async t => {
  const label = "Remi（Quanzhen Liang）";
  const data = report({responsible: {id: "remi-id", name: label}, events: [event("P2", {
    lark_user_name: "Sample Creator", conversation_type: "combined",
    evidence: ["oc_first", "oc_first", "oc_second", "javascript:alert(1)"].map((chat_id, i) => ({
      message_id: `message-${i}`, chat_id, chat_name: "同名 <b>会话</b>", text: "Checked.",
      created_at: "2026-09-09T20:00:00Z", sender_name: label})),
  })]});
  const {page, errors} = await fixture(t, data);
  await page.getByText("查看相关原文（4）").click();
  assert((await page.locator(".oh-evidence").textContent()).includes(label));
  const links = page.locator(".oh-conversations a");
  assert.equal(await links.count(), 2);
  assert.deepEqual(await links.allTextContents(), ["同名 <b>会话</b>", "同名 <b>会话</b>"]);
  assert.equal(await page.locator(".oh-conversations b").count(), 0);
  for (const [i, chat] of ["oc_first", "oc_second"].entries()) {
    assert.equal(await links.nth(i).getAttribute("href"), `https://applink.larksuite.com/client/chat/open?openChatId=${chat}`);
    assert.equal(await links.nth(i).getAttribute("target"), "_blank");
    assert.equal(await links.nth(i).getAttribute("rel"), "noopener noreferrer");
  }
  assert.deepEqual(errors, []);
});

test("creator names open permitted profiles while a separate accessible icon opens Lark", async t => {
  const attack = '<img src=x onerror="window.compromised=true">';
  const data = {cms: [{id: "cm-1", name: "CM"}], selected_cm_id: "cm-1", can_select_cm: false,
    can_view_unassigned: false, can_view_importance: true, can_adjust_importance: true,
    read_only: false, import_batches: [],
    sort: "importance", tier: "", importance_running: true, page: 1, total_count: 3, total_pages: 1,
    items: [
      {id: "person-1", kind: "confirmed", display_name: "Creator One", open_id: "ou_creator_one", profile_url: "/admin/contacts/ou_creator_one", handles: ["one"]},
      {id: "person-2", kind: "confirmed", display_name: "No identity", open_id: null, profile_url: null, handles: ["two"]},
      {id: "person-3", kind: "confirmed", display_name: attack, open_id: "javascript:alert(1)", profile_url: null, handles: ["three"]},
    ]};
  const tierLabels = {focus: "重点", normal: "常规", pending: "待评估"};
  for (const [i, tier] of ["focus", "normal", "pending"].entries()) data.items[i].importance = {
    tier, tier_label: tierLabels[tier], phase_label: i === 0 ? "持续合作" : i === 1 ? "单次合作" : "",
    grounds: i === 0 ? ["Acme：需要跟进直播安排。", "Beta：事项仍在推进。", "这一条不应展示。"] : [],
    cutoff_label: i === 2 ? "" : "10-08 13:00", gap_label: i === 2 ? "尚未评估" : "",
    manual_tier: "", manual_reason: "",
  };
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
    "import json,sys; from app.lark_api import private_chat_url; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); [item.update(private_chat_url=private_chat_url(item['open_id'])) for item in data['items']]; html=Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('creator_handles.html').render(authorization={'display_name':'CM'},creator_data=data,creator_tab='mine',creator_query='',csrf_token='fixture-csrf'); print(json.dumps({'data':data,'html':html}))"],
    {cwd: backend, input: JSON.stringify(data), encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const fixture = JSON.parse(rendered.stdout), state = {data: fixture.data, calls: [], larkRequests: [], status: 200};
  const url = "https://applink.larksuite.com/client/chat/open?openId=ou_creator_one";
  const profileUrl = "http://localhost/admin/contacts/ou_creator_one";
  assert.equal(state.data.items[0].private_chat_url, url);
  assert.deepEqual(state.data.items.slice(1).map(item => item.private_chat_url), [null, null]);
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  const context = await browser.newContext(), page = await context.newPage();
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", route => {
    const request = route.request(), target = new URL(request.url());
    if (target.origin !== "http://localhost") {
      if (target.origin === "https://applink.larksuite.com") state.larkRequests.push(request.url());
      if (process.env.OH_SCREENSHOT_DIR && target.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (target.pathname === "/creators") return route.fulfill({contentType: "text/html", body: fixture.html});
    if (target.pathname.startsWith("/static/")) return route.fulfill({contentType: target.pathname.endsWith(".js") ? "text/javascript" : target.pathname.endsWith(".svg") ? "image/svg+xml" : "text/css", body: fs.readFileSync(path.join(backend, "app", target.pathname))});
    state.calls.push({method: request.method(), url: target.pathname, search: target.search});
    if (target.pathname === "/admin/contacts/ou_creator_one") return route.fulfill({contentType: "text/html", body: "<p>Creator One profile</p>"});
    if (target.pathname === "/creators/data") {
      state.data.sort = target.searchParams.get("sort") || "importance";
      state.data.tier = target.searchParams.get("tier") || "";
      return route.fulfill({status: state.status,
        json: state.status === 200 ? state.data : {error: "Fixture access no longer available"}});
    }
    return route.abort();
  });
  await page.goto("http://localhost/creators");
  const rows = page.locator("#creators-rows tr");
  await rows.nth(2).waitFor();
  const names = rows.locator("td:nth-child(2) .fw-bold");
  assert.deepEqual(await names.allTextContents(), data.items.map(item => item.display_name));
  assert.equal(await page.locator("#creators-sort").inputValue(), "importance");
  assert.deepEqual(await page.locator("#creators-tier option").allTextContents(), ["全部优先级", "重点", "优先", "常规", "久未联系", "待评估"]);
  assert.deepEqual(await rows.locator(".creator-importance-badge").allTextContents(), ["重点", "常规", "待评估"]);
  assert.equal(await rows.first().locator(".creator-importance-detail > summary").textContent(), "维护依据");
  await rows.first().locator(".creator-importance-detail > summary").click();
  assert.equal(await rows.first().locator(".creator-importance-grounds li").count(), 2);
  assert.equal(await page.getByText("Acme：需要跟进直播安排。", {exact: true}).count(), 1);
  const manual = rows.first().locator(".creator-importance-editor");
  assert(!(await manual.evaluate(element => element.open)));
  await manual.locator("summary").click();
  assert.equal(await manual.getByRole("combobox", {name: "人工维护优先级"}).count(), 1);
  assert.deepEqual(await manual.locator("option").allTextContents(), ["使用自动评估", "重点", "优先", "常规", "久未联系", "待评估"]);
  await manual.locator("summary").click();
  await rows.nth(2).locator(".creator-importance-detail > summary").click();
  assert.equal(await rows.nth(2).getByText("尚未评估", {exact: true}).count(), 1);
  assert.equal(await rows.nth(2).locator(".creator-importance-grounds li").count(), 0);
  await rows.nth(2).locator(".creator-importance-detail > summary").click();
  assert.equal(await page.getByText("这一条不应展示。", {exact: true}).count(), 0);
  assert.equal(await page.getByText("资料截至 10-08 13:00", {exact: true}).count(), 2);
  const polling = page.waitForResponse(response => response.url().includes("/creators/data?"));
  state.data.importance_running = false;
  await polling;
  Object.assign(state.data.items[0].importance, {tier: "priority", tier_label: "优先"});
  Object.assign(state.data.items[1].importance, {tier: "dormant", tier_label: "久未联系",
    grounds: ["已同步记录已42天无相关消息", "最近消息：2026-08-27"], gap_label: "历史同步范围不足"});
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => document.querySelectorAll('.creator-importance-badge[data-tier="dormant"]').length === 1);
  assert.deepEqual(await rows.locator(".creator-importance-badge").allTextContents(), ["优先", "久未联系", "待评估"]);
  assert.equal(await rows.first().locator(".creator-importance-badge").getAttribute("data-tier"), "priority");
  await rows.nth(1).locator(".creator-importance-detail > summary").click();
  assert(await rows.nth(1).getByText("已同步记录已42天无相关消息", {exact: true}).isVisible());
  assert(await rows.nth(1).getByText("历史同步范围不足", {exact: true}).isVisible());
  assert(state.calls.every(call => call.method === "GET"));
  if (process.env.OH_SCREENSHOT_DIR) {
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-importance-desktop.png"), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    assert.equal(await page.locator(".business-heading").count(), 1);
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-importance-mobile.png")});
    await page.setViewportSize({width: 1280, height: 720});
  }
  assert(await names.evaluateAll(elements => elements.every(element => element.tagName === "A")));
  assert.equal(await names.locator("img").count(), 0);
  assert.equal(await page.evaluate(() => window.compromised), undefined);
  assert.deepEqual(await rows.locator("td:first-child").allTextContents(), ["@one", "@two", "@three"]);
  assert.equal(await names.first().getAttribute("href"), "/admin/contacts/ou_creator_one");
  assert.equal(await names.first().getAttribute("title"), "查看个人资料");
  assert.equal(await names.first().getAttribute("target"), null);
  assert.equal(await rows.first().getByRole("link", {name: "Creator One", exact: true}).count(), 1);
  await names.first().focus();
  assert(await names.first().evaluate(element => element === document.activeElement));
  await Promise.all([page.waitForURL(profileUrl), names.first().press("Enter")]);
  assert.equal(context.pages().length, 1);
  assert.equal(page.url(), profileUrl);
  await page.goBack();
  await rows.nth(2).waitFor();
  assert.equal(new URL(page.url()).pathname, "/creators");
  const link = rows.first().getByRole("link", {name: "在 Lark 中与 Creator One 私聊（新窗口）", exact: true});
  assert.equal(await link.getAttribute("href"), url);
  assert.equal(await link.getAttribute("title"), "在 Lark 中打开私聊");
  assert.equal(await link.getAttribute("target"), "_blank");
  assert.equal(await link.getAttribute("rel"), "noopener noreferrer");
  const icon = link.locator("img");
  assert.equal(new URL(await icon.getAttribute("src"), page.url()).pathname, "/static/lark-share.svg");
  assert.equal(await icon.getAttribute("alt"), "");
  assert.equal(await icon.getAttribute("aria-hidden"), "true");
  assert.equal(await icon.getAttribute("width"), "16");
  assert.equal(await icon.getAttribute("height"), "16");
  await page.waitForFunction(() => {
    const image = document.querySelector("#creators-rows .creator-lark-link img");
    return image.complete && image.naturalWidth > 0;
  });
  for (const index of [1, 2]) {
    assert.equal(await rows.nth(index).getByRole("link").count(), 0);
    assert.equal(await rows.nth(index).locator("a[href]").count(), 0);
    assert.equal(await names.nth(index).getAttribute("href"), null);
    assert.equal(await names.nth(index).getAttribute("title"), "");
    assert(await names.nth(index).evaluate(element => { element.focus(); return element !== document.activeElement; }));
  }
  await link.focus();
  assert(await link.evaluate(element => element === document.activeElement));
  const [popup, request] = await Promise.all([
    page.waitForEvent("popup"), context.waitForEvent("request", {predicate: request => request.url() === url}), link.press("Enter"),
  ]);
  assert.equal(request.url(), url);
  await popup.close();
  assert.deepEqual(state.larkRequests, [url]);
  state.data.items[0].profile_url = null;
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => !document.querySelector("#creators-rows .fw-bold[href]"));
  assert.equal(await names.first().getAttribute("title"), "");
  assert.equal(await link.getAttribute("href"), url);
  assert.equal(await rows.first().getByRole("link").count(), 1);
  assert(await names.first().evaluate(element => { element.focus(); return element !== document.activeElement; }));
  for (const item of state.data.items) item.private_chat_url = null;
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => !document.querySelector("#creators-rows a[href]"));
  assert.equal(await rows.getByRole("link").count(), 0);
  assert.deepEqual(await names.allTextContents(), data.items.map(item => item.display_name));
  await page.locator("#creators-query").fill("Creator");
  await Promise.all([
    page.waitForResponse(response => response.url().includes("/creators/data?") && new URL(response.url()).searchParams.get("q") === "Creator"),
    page.locator("#creators-search button").click(),
  ]);
  const search = new URLSearchParams(state.calls.at(-1).search);
  assert.equal(search.get("tab"), "mine");
  assert.equal(search.get("q"), "Creator");
  for (const [key, value] of [["sort", "name"], ["sort", "importance"], ["tier", "priority"], ["tier", "dormant"], ["tier", "focus"]]) {
    await Promise.all([
      page.waitForResponse(response => response.url().includes("/creators/data?") && new URL(response.url()).searchParams.get(key) === value),
      page.locator(`#creators-${key}`).selectOption(value),
    ]);
  }
  const ranked = new URLSearchParams(state.calls.at(-1).search);
  assert.equal(ranked.get("q"), "Creator"); assert.equal(ranked.get("sort"), "importance"); assert.equal(ranked.get("tier"), "focus");
  state.data.can_adjust_importance = false;
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => [...document.querySelectorAll('.creator-importance-editor')].every(node => node.hidden));
  for (const item of state.data.items) item.importance = null;
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => [...document.querySelectorAll('.creator-importance-badge')].every(node => node.hidden));
  assert.equal(await page.locator('.creator-importance-badge:visible').count(), 0);
  state.status = 503;
  await page.locator("#creators-refresh").click();
  await page.getByText("Fixture access no longer available", {exact: true}).waitFor();
  assert.equal(await rows.count(), 3);
  state.status = 403;
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => !document.querySelectorAll('#creators-rows tr').length);
  assert.equal(await page.locator('#creators-evidence-body').textContent(), "");
  assert.equal(await page.locator('#creators-initial').textContent(), "");
  assert(!(await page.locator('#creators-importance-toolbar').isVisible()));
  assert(state.calls.every(call => call.method === "GET"));
  assert.deepEqual(errors, []);
});

test("grades are sorted and filters/search/evidence work without writes", async t => {
  const {page, state, errors} = await fixture(t);
  assert.deepEqual(await page.locator(".oh-handling").allTextContents(), ["已处理", "没有处理", "待核实", "无需处理", "待分析"]);
  assert.deepEqual(await page.locator(".oh-event .oh-priority").allTextContents(), ["P0", "P1", "P2", "P3", "待核实"]);
  assert(!(await page.locator(".oh-routine .oh-event").isVisible()));
  assert.equal(await page.locator("#oh-count-P3").textContent(), "1");
  assert.equal(await page.locator('a[href^="/admin/"]').count(), 0);
  assert((await page.locator("#oh-status-title").textContent()).includes("部分结果"));
  assert.equal(await page.locator("#oh-gaps").count(), 0);
  await page.locator('.oh-filter[data-priority="P0"]').click();
  assert.equal(await page.locator(".oh-event").count(), 1);
  await page.getByText("查看相关原文（1）").click();
  assert(await page.getByText("Please check this issue.").isVisible());
  await page.locator('.oh-filter[data-priority="review"]').click();
  assert.equal(await page.locator(".oh-event").count(), 1);
  assert((await page.locator(".oh-identity").textContent()).includes("handle：—"));
  await page.locator('.oh-filter[data-priority="all"]').click();
  await page.locator("#oh-search").fill("nothing-matches");
  assert.equal(await page.locator(".oh-event").count(), 0);
  assert((await page.locator("#oh-empty-title").textContent()).includes("筛选"));
  await page.locator("#oh-search").fill("");
  await page.locator(".oh-evidence summary").first().click();
  await page.locator(".oh-routine > summary").click();
  await page.locator("#oh-refresh").click();
  await page.waitForFunction(() => !document.getElementById("oh-refresh").disabled);
  assert(await page.locator(".oh-evidence").first().evaluate(element => element.open));
  assert(await page.locator(".oh-routine").evaluate(element => element.open));
  assert(state.calls.every(call => call.method === "GET"));
  assert.deepEqual(errors, []);
  if (process.env.OH_SCREENSHOT_DIR) {
    await page.locator(".oh-evidence summary").first().click();
    await page.locator(".oh-routine > summary").click();
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "offhours-summary-desktop.png"), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "offhours-summary-mobile.png"), fullPage: true});
  }
});

test("disabled state contains no pretend counts or seeded results", async t => {
  const {page, state, errors} = await fixture(t, report({enabled: false, can_manage: true, run: null, events: []}));
  assert.equal(await page.locator("#oh-count-P0").textContent(), "—");
  assert.equal(await page.locator(".oh-event").count(), 0);
  assert.equal(await page.locator('a[href="/admin/ai#offhours-summary"]').count(), 1);
  assert((await page.locator("#oh-empty-description").textContent()).includes("已入库达人"));
  assert.equal(state.calls.length, 0);
  assert.deepEqual(errors, []);
});

test("an ended window with sync gaps is incomplete, not described as still in the future", async t => {
  const data = report();
  data.window.is_open = false;
  data.run.status = "completed";
  data.run.counts = {queued: 0, running: 0, completed: 5, failed: 0};
  const {page, errors} = await fixture(t, data);
  assert.equal(await page.locator("#oh-status-title").textContent(), "当前为部分结果 · 尚有资料缺口");
  const description = await page.locator("#oh-status-description").textContent();
  assert(description.includes("已完成 5 / 5"));
  assert(!description.includes("尚未到时段结束时间"));
  assert.deepEqual(errors, []);
});

test("source content is literal, network failures show stale results, and revoked access clears them", async t => {
  const attack = '<img src=x onerror="window.compromised=true">';
  const {page, state, errors} = await fixture(t, report({events: [event("P0", {title: attack, evidence: [{text: attack}]})]}));
  assert.equal(await page.locator(".oh-event-title").textContent(), attack);
  assert.equal(await page.locator(".oh-event img").count(), 0);
  assert.equal(await page.evaluate(() => window.compromised), undefined);
  state.status = 503;
  await page.locator("#oh-refresh").click(); await page.locator("#oh-feedback").waitFor();
  assert((await page.locator("#oh-feedback").textContent()).includes("上次"));
  assert.equal(await page.locator(".oh-event").count(), 1);
  state.status = 403;
  await page.locator("#oh-refresh").click();
  await page.getByText("查看权限已失效", {exact: true}).waitFor();
  assert.equal(await page.locator(".oh-event").count(), 0);
  assert.equal(await page.locator("#offhours-summary-data").textContent(), "");
  assert(await page.locator("#oh-refresh").isDisabled());
  assert.deepEqual(errors, []);
});

test("admin owner and read-only fixtures expose the right controls without making any write", async t => {
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  for (const role of ["owner", "reviewer"]) {
    const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
      "import sys; sys.path.insert(0,'tests'); from test_offhours_summary_ui import render_admin; print(render_admin(sys.argv[1]))", role],
      {cwd: backend, encoding: "utf8"});
    assert.equal(rendered.status, 0, rendered.stderr);
    const page = await browser.newPage({viewport: {width: 1360, height: 1100}});
    const writes = [];
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") { writes.push(url.href); return route.abort(); }
      if (url.origin !== "http://localhost") {
        if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
        return route.abort();
      }
      if (url.pathname === "/admin/ai") return route.fulfill({contentType: "text/html", body: rendered.stdout});
      if (url.pathname.startsWith("/static/")) return route.fulfill({contentType: "text/css", body: fs.readFileSync(path.join(backend, "app", url.pathname))});
      return route.abort();
    });
    await page.goto("http://localhost/admin/ai");
    const card = page.locator("#offhours-summary");
    assert(await card.isVisible());
    if (role === "owner") {
      assert.equal(await card.locator('input[name="window_start"]').inputValue(), "2026-09-09T18:00");
      assert.equal(await card.locator('input[name="window_end"]').inputValue(), "2026-09-10T12:00");
      assert.equal(await card.locator('input[name="api_key"]').inputValue(), "");
      assert(!(await card.locator('input[name="enabled"]').isChecked()));
      assert.equal(await card.locator('input[name="auto_confirm_threshold"]').count(), 0);
      assert.equal(await card.locator("form").getAttribute("action"), "/admin/ai");
      assert.equal(await card.locator("form").getAttribute("method"), "post");
    } else {
      assert.equal(await page.locator("form,input").count(), 0);
      assert((await card.textContent()).includes("只读配置"));
    }
    assert.deepEqual(writes, []);
    if (process.env.OH_SCREENSHOT_DIR) await card.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, `offhours-admin-${role}.png`)});
    await page.close();
  }
});

test("each summary selector navigates immediately without a submit button", async t => {
  const data = report({selection: {
    cms: [{id: "remi-id", name: "Remi"}, {id: "nero-id", name: "Nero"}],
    dates: ["2026-09-09", "2026-09-10"], date: "2026-09-09", period: "off",
  }});
  const {page, errors} = await fixture(t, data);
  assert.equal(await page.locator('#oh-selection button, [aria-label="总结范围"]').count(), 0);
  for (const [name, value] of [["cm_id", "nero-id"], ["date", "2026-09-10"], ["period", "work"]]) {
    await Promise.all([
      page.waitForURL(url => url.searchParams.get(name) === value),
      page.locator(`#oh-selection select[name="${name}"]`).selectOption(value),
    ]);
    const url = new URL(page.url());
    assert.equal(url.pathname, "/dashboard/offhours-summary");
    assert.equal(url.searchParams.size, 3);
  }
  assert.deepEqual(errors, []);
});


test("reply drafts preserve edits, translate before copying, and block stale or revoked suggestions", async t => {
  const {page, errors} = await fixture(t, report({events: [event("P1")]}));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  let edited = "", stale = false, revoked = false;
  const posted = [];
  await page.route("**/dashboard/offhours-summary/replies**", route => {
    if (revoked) return route.fulfill({status: 403, json: {error: "权限已失效"}});
    const req = route.request();
    if (req.method() === "POST") {
      const body = req.postDataJSON(); posted.push(body);
      assert.equal(req.headers()["x-csrf-token"], "fixture-csrf");
      if (body.parent_id) edited = body.text;
      return route.fulfill({status: 202, json: {id: body.parent_id ? "translation" : "draft", status: "queued"}});
    }
    if (stale) return route.fulfill({json: {status: "stale", error: "收到新消息，请重新生成建议。"}});
    const translation = req.url().includes("/translation?");
    return route.fulfill({json: {status: "succeeded", result: translation
      ? {draft: edited, chinese_translation: "修改后的中文翻译，金额仍为 £200。"}
      : {outcome: "ready", language: "en", draft: "Hi lovely, I can check that £200 payment with you. Could you confirm which order this concerns?",
        chinese_translation: "你好，我可以帮你核对这笔 £200 的付款。请确认对应哪笔订单。", explanation: "尚未看到完成结果。",
        checks: ["确认可以核对。"], checked_at: "2026-09-16T01:00:00Z"}}});
  });
  await page.getByRole("button", {name: "生成回复建议", exact: true}).click();
  const editor = page.getByLabel("建议回复（可编辑）");
  await editor.waitFor({state: "visible"});
  const newText = "Hi lovely, I can check that £200 payment with you. I have the order number already, so you do not need to resend it.";
  await editor.fill(newText);
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isDisabled());
  assert(!(await page.locator(".oh-reply-translation").textContent()).includes("请确认对应哪笔订单"));
  await page.getByText("修改后的中文翻译，金额仍为 £200。", {exact: true}).waitFor();
  await page.getByRole("button", {name: "刷新", exact: true}).click();
  assert.equal(await editor.inputValue(), newText);
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("已复制回复正文；未发送，也未改变事件处理状态。", {exact: true}).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), newText);
  assert.equal(await page.locator(".oh-handling").textContent(), "没有处理");
  stale = true;
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("收到新消息，请重新生成建议。", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isDisabled());
  revoked = true;
  await page.getByRole("button", {name: "重新生成", exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll(".oh-event").length === 0);
  assert.equal(posted.length, 2);
  assert.deepEqual(errors, []);
});
