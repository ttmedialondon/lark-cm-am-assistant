/* Isolated browser fixtures only: these tests never call OpenAI or production. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
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
  events: [event("P2", {reply_evidence_refs: [], replied_by: []}),
    event("P0", {reply_evidence_refs: ["M2"], replied_by: ["Remi"]}),
    event("P3", {reply_evidence_refs: [], replied_by: []}),
    event("P1", {reply_evidence_refs: ["M2"], replied_by: ["Shang Ma"], needs_review: true,
      latest_progress: "Shang Ma 已回复会催付款，尚未到账。"}),
    event(null, {creator_handle: null, title: "资料尚不足以判断", needs_review: true})], ...extra});

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
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://localhost") {
      if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (url.pathname === "/dashboard/offhours-summary") {
      if (state.navigationGate) await state.navigationGate;
      return route.fulfill({contentType: "text/html", body: rendered.stdout});
    }
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

test("creator roster edits preserve sibling handles and permission-scoped profile links", async t => {
  const attack = '<img src=x onerror="window.compromised=true">';
  const contactId = "11111111-1111-4111-8111-111111111111";
  const data = {tab: "mine", cms: [{id: "cm-1", name: "CM"}], selected_cm_id: "cm-1", can_select_cm: true,
    can_view_unassigned: true, can_view_importance: true, can_adjust_importance: true, can_edit_handles: true, can_link_people: true,
    read_only: false, import_batches: [{id: "batch-1", filename: "fixture.xlsx", status: "completed",
      status_label: "识别完成", counts: {confirmed: 2, review: 1}, can_recheck: true}],
    sort: "importance", tier: "", importance_running: true, page: 1, total_count: 3, total_pages: 1,
    items: [
      {id: contactId, contact_id: contactId, kind: "confirmed", display_name: "Creator One", open_id: "ou_creator_one", profile_url: "/admin/contacts/ou_creator_one", handles: ["one", "one.alt"]},
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
    "import json,sys; from app.lark_api import private_chat_url; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); [item.update(private_chat_url=private_chat_url(item['open_id'])) for item in data['items']]; html=Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('creator_handles.html').render(authorization={'id':'cm-1','display_name':'CM'},creator_data=data,creator_tab='mine',creator_query='',csrf_token='fixture-csrf'); print(json.dumps({'data':data,'html':html}))"],
    {cwd: backend, input: JSON.stringify(data), encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const fixture = JSON.parse(rendered.stdout), state = {data: fixture.data, calls: [], larkRequests: [], status: 200};
  fixture.html = fixture.html.replace("<body>", '<body><div id="operations-assistant" hidden></div>');
  const reviewCandidates = [
    {id: "review-one", handle: "review.one", display_name: "Creator One", contact_id: contactId,
      status_label: "待核对", reason: "账号有候选，需核对原文。", can_defer: true},
    {id: "review-two", handle: "review.two", display_name: "—", contact_id: null,
      status_label: "待核对", reason: "请选择对应用户。", can_defer: true},
  ];
  state.candidates = reviewCandidates.map(row => ({...row}));
  const url = "https://applink.larksuite.com/client/chat/open?openId=ou_creator_one";
  assert.equal(state.data.items[0].private_chat_url, url);
  assert.deepEqual(state.data.items.slice(1).map(item => item.private_chat_url), [null, null]);
  const loginServer = http.createServer((request, response) => {
    response.writeHead(200, {"Content-Type": "text/html", Connection: "close"});
    response.end(new URL(request.url, "http://localhost").pathname === "/creators" ? fixture.html : "<p>Log in</p>");
  });
  await new Promise(resolve => loginServer.listen(0, "127.0.0.1", resolve));
  t.after(() => { loginServer.close(); });
  const loginOrigin = `http://127.0.0.1:${loginServer.address().port}`;
  const profileUrl = loginOrigin + "/admin/contacts/ou_creator_one";
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  const context = await browser.newContext(), page = await context.newPage();
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const request = route.request(), target = new URL(request.url());
    if (target.origin !== loginOrigin) {
      if (target.origin === "https://applink.larksuite.com") state.larkRequests.push(request.url());
      if (process.env.OH_SCREENSHOT_DIR && target.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (["/creators", "/fixture-login"].includes(target.pathname)) return route.continue();
    if (target.pathname.startsWith("/static/")) return route.fulfill({contentType: target.pathname.endsWith(".js") ? "text/javascript" : target.pathname.endsWith(".svg") ? "image/svg+xml" : "text/css", body: fs.readFileSync(path.join(backend, "app", target.pathname))});
    state.calls.push({method: request.method(), url: target.pathname, search: target.search, body: request.postData()});
    if (target.pathname === "/creators/imports/data") {
      assert.equal(target.searchParams.get("batch_id"), "batch-1");
      assert.equal(target.searchParams.get("cm_id"), "cm-1");
      if (state.reviewReadStatus) return route.fulfill({status: state.reviewReadStatus, json: {error: "Fixture access revoked"}});
      return route.fulfill({json: {candidates: state.candidates, total: state.candidates.length, page: 1, total_pages: 1}});
    }
    if (target.pathname === "/creators/imports/users") {
      if (state.userGate) await state.userGate;
      if (state.userRedirect) return route.fulfill({status: 302, headers: {Location: loginOrigin + "/fixture-login"}});
      return route.fulfill({json: [{id: contactId, display_name: "Creator One", context_label: "Fixture group"}]});
    }
    if (target.pathname === "/creators/imports/rows/review-two/evidence") {
      if (state.evidenceGate) await state.evidenceGate;
      if (state.evidenceStatus) return route.fulfill({status: state.evidenceStatus, json: {error: "Fixture access revoked"}});
    }
    if (target.pathname.endsWith("/evidence")) return route.fulfill({json: {
      person: {status_label: "对应关系待定", display_name: "Creator One", confidence: .93,
        reason: "请结合本人账号发言核对。", evidence_refs: ["找人 M1"]}, warnings: [], conflicts: [],
      groups: [{name: "Fixture group", selected: true, members: [], messages: [
        {ref: "找人 M1", sender_name: "Creator One", time_label: "2026-10-12", text: "My handle is review.one"},
        {ref: "找人 M2", sender_name: "CM", time_label: "2026-10-12", text: "Older fixture context"},
      ]}],
    }});
    if (request.method() === "POST" && target.pathname.startsWith("/creators/imports/rows/")) {
      const fields = new URLSearchParams(request.postData());
      assert.equal(fields.get("csrf_token"), "fixture-csrf");
      if (state.reviewSaveStatus) return route.fulfill({status: state.reviewSaveStatus, json: {error: "Fixture validation rejected"}});
      if (target.pathname.endsWith("/confirm")) {
        assert.equal(fields.get("contact_id"), contactId); assert.equal(fields.get("handle"), "review.corrected");
        state.candidates = state.candidates.filter(row => row.id !== "review-one");
      } else Object.assign(state.candidates[0], {status_label: "暂缓", can_defer: false});
      return route.fulfill({json: {ok: true}});
    }
    if (target.pathname === "/admin/contacts/ou_creator_one") return route.fulfill({contentType: "text/html", body: "<p>Creator One profile</p>"});
    if (request.method() === "POST" && target.pathname === `/creators/people/${contactId}/importance`) {
      const fields = new URLSearchParams(request.postData());
      Object.assign(state.data.items[0].importance, {tier: fields.get("tier"), tier_label: "优先",
        manual_tier: fields.get("tier"), manual_reason: fields.get("reason")});
      return route.fulfill({json: {saved: true, importance: state.data.items[0].importance}});
    }
    if (request.method() === "POST" && target.pathname === `/creators/people/${contactId}/handle`) {
      const fields = new URLSearchParams(request.postData());
      const handles = state.data.items[0].handles;
      state.data.items[0].handles = fields.get("operation") === "add" ? [...handles, fields.get("handle")]
        : fields.get("operation") === "remove" ? handles.filter(handle => handle !== fields.get("old_handle"))
        : handles.map(handle => handle === fields.get("old_handle") ? fields.get("handle") : handle);
      if (state.handleSaveGate) await state.handleSaveGate;
      return route.fulfill({json: {saved: true, handles: state.data.items[0].handles}});
    }
    if (target.pathname === "/creators/people/search") {
      if (state.linkGate) await state.linkGate;
      if (state.linkStatus) return route.fulfill({status: state.linkStatus, json: {error: "Fixture access revoked"}});
      return route.fulfill({json: {cm_id: "cm-1", items: [
        {contact_id: contactId, display_name: "Same Name", avatar_url: null, context_label: "Creator customer group", handles: []},
        {contact_id: "22222222-2222-4222-8222-222222222222", display_name: "Same Name", avatar_url: null, context_label: "Private with Same Name", handles: []},
      ]}});
    }
    if (request.method() === "POST" && target.pathname === "/creators/people/link") {
      const fields = new URLSearchParams(request.postData());
      if (!fields.get("old_contact_id")) state.data.items[0].handles.push(fields.get("handle"));
      return route.fulfill({json: {saved: true, correction: Boolean(fields.get("old_contact_id"))}});
    }
    if (target.pathname === "/creators/data") {
      if (state.dataGate) await state.dataGate;
      state.data.tab = target.searchParams.get("tab") || "mine";
      state.data.sort = target.searchParams.get("sort") || "importance";
      state.data.tier = target.searchParams.get("tier") || "";
      return route.fulfill({status: state.status,
        json: state.status === 200 ? state.data : {error: "Fixture access no longer available"}});
    }
    return route.abort();
  });
  await page.goto(loginOrigin + "/creators");
  const rows = page.locator("#creators-rows tr");
  await rows.nth(2).waitFor();
  assert.equal(await page.getByRole("button", {name: "候选达人", exact: true}).count(), 0);
  assert.deepEqual(await page.getByRole("group", {name: "达人列表"}).getByRole("button").allTextContents(), ["我的达人", "未分配达人"]);
  assert.equal(await page.getByRole("combobox", {name: "负责人", exact: true}).inputValue(), "cm-1");
  const history = page.locator("#creators-import-batches");
  assert(!(await history.evaluate(element => element.open)));
  assert(!(await history.getByRole("button", {name: "重新检查未确认"}).isVisible()));
  await history.locator("summary").press("Enter");
  assert(await history.getByRole("button", {name: "重新检查未确认"}).isVisible());
  await history.locator("summary").click();
  assert.equal(await page.locator("#creators-evidence, .creator-account-detail, .creator-importance-detail").count(), 0);
  assert.equal(await page.getByText("Acme：需要跟进直播安排。", {exact: true}).count(), 0);
  const names = rows.locator("td:nth-child(2) .fw-bold");
  assert.deepEqual(await names.allTextContents(), data.items.map(item => item.display_name));
  assert.equal(await page.locator("#creators-sort").inputValue(), "importance");
  assert.deepEqual(await page.locator("#creators-tier option").allTextContents(), ["全部优先级", "重点", "优先", "常规", "久未联系", "待评估"]);
  assert.deepEqual(await rows.locator(".creator-importance-badge").allTextContents(), ["重点", "常规", "待评估"]);
  const manual = rows.first().locator(".creator-importance-editor");
  assert(!(await manual.evaluate(element => element.open)));
  await manual.locator("summary").press("Enter");
  assert.equal(await manual.getByRole("combobox", {name: "重要度", exact: true}).count(), 1);
  assert.deepEqual(await manual.locator("option").allTextContents(), ["使用自动评估", "重点", "优先", "常规", "久未联系", "待评估"]);
  await manual.getByRole("combobox", {name: "重要度", exact: true}).selectOption("priority");
  await manual.getByRole("textbox", {name: "调整说明"}).fill("Owner reviewed fixture");
  await manual.getByRole("button", {name: "保存", exact: true}).click();
  await page.waitForFunction(() => document.querySelector('.creator-importance-badge').textContent === "优先 · 人工");
  assert(!(await manual.evaluate(element => element.open)));
  const handleEditor = rows.first().locator(".creator-handle-editor");
  await handleEditor.locator("summary").press("Enter");
  assert.deepEqual(await handleEditor.getByRole("combobox", {name: "选择要修改的 handle", exact: true}).locator("option").allTextContents(), ["@one", "@one.alt"]);
  await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).fill("cancelled.edit");
  await handleEditor.getByRole("button", {name: "取消", exact: true}).click();
  assert(!(await handleEditor.evaluate(element => element.open)));
  await handleEditor.locator("summary").press("Enter");
  assert.equal(await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).inputValue(), "one");
  await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).fill("one.updated");
  state.data.items[0].handles.push("worker.added");
  state.status = 503;
  await handleEditor.getByRole("button", {name: "保存", exact: true}).click();
  await page.getByText("Fixture access no longer available", {exact: true}).waitFor();
  await page.waitForFunction(() => document.querySelector('#creators-rows td').textContent.includes("@one.updated"));
  assert.deepEqual(await rows.first().locator("td:first-child span").allTextContents(), ["@one.updated", "@one.alt", "@worker.added"]);
  assert(!(await handleEditor.evaluate(element => element.open)));
  const posts = state.calls.filter(call => call.method === "POST");
  assert.deepEqual(posts.map(call => call.url), [`/creators/people/${contactId}/importance`, `/creators/people/${contactId}/handle`]);
  for (const call of posts) {
    const fields = new URLSearchParams(call.body);
    assert.equal(fields.get("csrf_token"), "fixture-csrf"); assert.equal(fields.get("cm_id"), "cm-1");
  }
  assert.equal(new URLSearchParams(posts[1].body).get("old_handle"), "one");
  assert.equal(new URLSearchParams(posts[1].body).get("handle"), "one.updated");
  state.status = 200;
  let releaseData;
  state.dataGate = new Promise(resolve => { releaseData = resolve; });
  const refreshing = page.waitForRequest(request => request.url().includes("/creators/data?"));
  await page.locator("#creators-refresh").click();
  await refreshing;
  assert.equal(await page.locator("#creators-refresh").getAttribute("aria-busy"), "true");
  assert.equal(await rows.count(), 3);
  assert(await page.locator("#creators-table").evaluate(element => element.inert));
  assert.equal(await page.locator("#creators-table").getAttribute("data-ui-updating"), "true");
  assert(!(await page.locator("#creators-loading").isVisible()));
  releaseData(); state.dataGate = null;
  await page.waitForFunction(() => document.getElementById("creators-refresh").getAttribute("aria-busy") !== "true");
  const ask = rows.first().getByRole("button", {name: "问助手", exact: true});
  assert(await ask.isEnabled());
  await page.evaluate(() => { window.assistantEvents = []; document.addEventListener("ttmedia:assistant", event => window.assistantEvents.push(event.detail)); });
  await ask.click();
  assert.equal(await page.evaluate(() => window.assistantEvents[0].context.creator_ids[0]), contactId);
  await history.locator("summary").click();
  await history.getByRole("button", {name: "核对账号", exact: true}).click();
  const review = page.locator("#creators-review-dialog");
  await review.locator("[data-row='review-two']").waitFor();
  assert.equal(await review.getByText("My handle is review.one", {exact: true}).count(), 0);
  const firstReview = review.locator("[data-row='review-one']"), secondReview = review.locator("[data-row='review-two']");
  await secondReview.locator("summary").first().click();
  await secondReview.getByRole("textbox", {name: "核对 handle", exact: true}).fill("keep.unsaved");
  await firstReview.locator("summary").first().click();
  await firstReview.getByText("查看证据", {exact: true}).click();
  await firstReview.getByText("My handle is review.one", {exact: true}).waitFor();
  assert(!(await firstReview.getByText("Older fixture context", {exact: true}).isVisible()));
  await firstReview.getByRole("textbox", {name: "核对 handle", exact: true}).fill("review.corrected");
  await firstReview.getByRole("searchbox", {name: "搜索 Lark 用户姓名", exact: true}).fill("Creator");
  await firstReview.getByRole("button", {name: "搜索用户", exact: true}).click();
  await firstReview.getByText("请选择对应用户。", {exact: true}).waitFor();
  await firstReview.getByRole("combobox", {name: "对应 Lark 用户", exact: true}).selectOption(contactId);
  if (process.env.OH_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-import-review.png")});
  await firstReview.getByRole("button", {name: "确认入库", exact: true}).click();
  await page.waitForFunction(() => !document.querySelector("[data-row='review-one']"));
  await secondReview.locator("summary").first().click();
  assert.equal(await secondReview.getByRole("textbox", {name: "核对 handle", exact: true}).inputValue(), "keep.unsaved");
  await secondReview.getByRole("button", {name: "暂缓", exact: true}).click();
  await review.getByText("@review.two · — · 暂缓", {exact: true}).waitFor();
  assert(!(await review.getByRole("button", {name: "暂缓", exact: true}).isVisible()));
  await review.getByRole("button", {name: "关闭", exact: true}).click();
  await history.locator("summary").click();
  assert(!(await page.locator("#creators-table").evaluate(element => element.inert)));
  assert.equal(await page.locator("#creators-table").getAttribute("data-ui-updating"), null);
  assert.equal(await page.getByText("这一条不应展示。", {exact: true}).count(), 0);
  assert.equal(await page.getByText("资料截至 10-08 13:00", {exact: true}).count(), 0);
  const polling = page.waitForResponse(response => response.url().includes("/creators/data?"));
  state.data.importance_running = false;
  await polling;
  assert(!(await page.locator("#creators-loading").isVisible()));
  assert.equal(await page.locator("#creators-refresh").getAttribute("aria-busy"), null);
  assert(!(await history.evaluate(element => element.open)));
  Object.assign(state.data.items[0].importance, {tier: "priority", tier_label: "优先", manual_tier: ""});
  Object.assign(state.data.items[1].importance, {tier: "dormant", tier_label: "久未联系",
    grounds: ["已同步记录已42天无相关消息", "最近消息：2026-08-27"], gap_label: "历史同步范围不足"});
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => document.querySelectorAll('.creator-importance-badge[data-tier="dormant"]').length === 1);
  assert.deepEqual(await rows.locator(".creator-importance-badge").allTextContents(), ["优先", "久未联系", "待评估"]);
  assert.equal(await rows.first().locator(".creator-importance-badge").getAttribute("data-tier"), "priority");
  assert.equal(await rows.nth(1).getByText("历史同步范围不足", {exact: true}).count(), 0);
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
  assert.deepEqual(await rows.locator("td:first-child").allTextContents(), ["@one.updated · @one.alt · @worker.added", "@two", "@three"]);
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
  state.dataGate = new Promise(resolve => { releaseData = resolve; });
  const searching = page.waitForRequest(request => request.url().includes("/creators/data?") && new URL(request.url()).searchParams.get("q") === "Creator");
  await page.locator("#creators-search button").click(); await searching;
  assert.equal(await rows.count(), 0);
  assert(await page.locator("#creators-loading").isVisible());
  assert.equal(await page.locator("#creators-loading .ui-skeleton-row").count(), 3);
  assert(!(await page.locator("#creators-empty").isVisible()));
  assert.equal(await page.locator("#creators-table").getAttribute("aria-busy"), "true");
  const spinner = page.locator("#creators-loading .ui-spinner");
  const transform = await spinner.evaluate(element => getComputedStyle(element).transform);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.notEqual(await spinner.evaluate(element => getComputedStyle(element).transform), transform);
  if (process.env.OH_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-list-loading.png")});
  releaseData(); state.dataGate = null;
  await rows.nth(2).waitFor();
  assert(!(await page.locator("#creators-loading").isVisible()));
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
  const mineView = {read_only: state.data.read_only, can_view_importance: state.data.can_view_importance,
    can_adjust_importance: state.data.can_adjust_importance, can_edit_handles: state.data.can_edit_handles,
    can_link_people: state.data.can_link_people,
    import_batches: state.data.import_batches};
  Object.assign(state.data, {read_only: true, can_view_importance: false,
    can_adjust_importance: false, can_edit_handles: false, can_link_people: false, import_batches: []});
  await Promise.all([
    page.waitForResponse(response => response.url().includes("/creators/data?")
      && new URL(response.url()).searchParams.get("tab") === "unassigned"),
    page.getByRole("button", {name: "未分配达人", exact: true}).click(),
  ]);
  await page.waitForFunction(() => document.querySelector('#creators-unassigned').getAttribute('aria-pressed') === 'true'
    && document.querySelector('#creators-cm-filter').hidden);
  assert.equal(await page.getByRole("button", {name: "未分配达人", exact: true}).getAttribute("aria-pressed"), "true");
  assert(!(await page.getByRole("combobox", {name: "负责人", exact: true}).isVisible()));
  assert(!(await page.locator('#creators-importance-toolbar').isVisible()));
  assert.equal(await page.locator('.creator-inline-editor:visible').count(), 0);
  assert(!(await page.getByRole("button", {name: "手动关联", exact: true}).isVisible()));
  Object.assign(state.data, mineView);
  await page.getByRole("button", {name: "我的达人", exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#creators-mine').getAttribute('aria-pressed') === 'true'
    && document.querySelectorAll('.creator-handle-editor').length === 3);
  assert(await page.getByRole("combobox", {name: "负责人", exact: true}).isVisible());
  let releaseSave;
  state.handleSaveGate = new Promise(resolve => { releaseSave = resolve; });
  await handleEditor.locator("summary").click();
  await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).fill("one.final");
  const pendingSave = page.waitForRequest(request => request.method() === "POST" && request.url().endsWith("/handle"));
  await handleEditor.getByRole("button", {name: "保存", exact: true}).click();
  await pendingSave;
  assert.equal(await handleEditor.getByRole("button", {name: "保存", exact: true}).getAttribute("aria-busy"), "true");
  assert.equal(await rows.nth(1).locator('[aria-busy="true"]').count(), 0);
  state.data.can_adjust_importance = false;
  state.data.can_edit_handles = false;
  await page.locator("#creators-search button").click();
  await page.waitForFunction(() => document.querySelectorAll('#creators-rows tr').length === 3
    && !document.querySelector('.creator-handle-editor'));
  releaseSave();
  await page.getByText("handle 已更新。", {exact: true}).waitFor();
  assert.equal(await page.locator('.creator-importance-editor:visible').count(), 0);
  assert.equal(await page.locator('.creator-handle-editor:visible').count(), 0);
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
  assert.equal(await page.locator('#creators-evidence').count(), 0);
  assert.equal(await page.locator('#creators-initial').textContent(), "");
  assert(!(await history.isVisible()));
  assert(!(await page.locator('#creators-importance-toolbar').isVisible()));
  assert.equal(state.calls.filter(call => call.method === "POST").length, 5);
  assert.equal(state.calls.filter(call => call.url.endsWith("/evidence")).length, 1);
  for (const failure of ["save", "evidence", "redirect", "review"]) {
    state.status = 200; state.candidates = reviewCandidates.map(row => ({...row}));
    state.reviewReadStatus = state.reviewSaveStatus = state.evidenceStatus = null; state.userRedirect = false;
    await page.reload(); await rows.first().waitFor();
    let releaseRoster, releaseUsers, releaseEvidence;
    if (failure === "save") {
      state.dataGate = new Promise(resolve => { releaseRoster = resolve; });
      const pending = page.waitForRequest(request => new URL(request.url()).pathname === "/creators/data");
      await page.locator("#creators-refresh").click(); await pending;
    }
    await history.locator("summary").click();
    if (failure === "review") state.reviewReadStatus = 403;
    await history.getByRole("button", {name: "核对账号", exact: true}).click();
    if (failure !== "review") {
      await firstReview.waitFor();
      await firstReview.locator("summary").first().click(); await secondReview.locator("summary").first().click();
      await firstReview.getByText("查看证据", {exact: true}).click();
      await firstReview.getByText("My handle is review.one", {exact: true}).waitFor();
      if (failure === "save") {
        await firstReview.getByRole("textbox", {name: "核对 handle", exact: true}).fill("review.corrected");
        await secondReview.getByRole("textbox", {name: "核对 handle", exact: true}).fill("keep.unsaved");
        state.reviewSaveStatus = 400;
        await firstReview.getByRole("button", {name: "确认入库", exact: true}).click();
        await review.locator("#creators-review-message").getByText("Fixture validation rejected", {exact: true}).waitFor();
        assert.equal(await firstReview.getByRole("textbox", {name: "核对 handle", exact: true}).inputValue(), "review.corrected");
        assert.equal(await secondReview.getByRole("textbox", {name: "核对 handle", exact: true}).inputValue(), "keep.unsaved");
        assert(await firstReview.getByText("My handle is review.one", {exact: true}).isVisible());
        state.userGate = new Promise(resolve => { releaseUsers = resolve; });
        state.evidenceGate = new Promise(resolve => { releaseEvidence = resolve; });
        const userRequest = page.waitForRequest(request => new URL(request.url()).pathname === "/creators/imports/users");
        await firstReview.getByRole("searchbox", {name: "搜索 Lark 用户姓名", exact: true}).fill("Creator");
        await firstReview.getByRole("button", {name: "搜索用户", exact: true}).click(); await userRequest;
        const evidenceRequest = page.waitForRequest(request => request.url().endsWith("/review-two/evidence"));
        await secondReview.getByText("查看证据", {exact: true}).click(); await evidenceRequest;
        state.reviewSaveStatus = 401;
        await firstReview.getByRole("button", {name: "确认入库", exact: true}).click();
      } else if (failure === "evidence") {
        state.evidenceStatus = 403; await secondReview.getByText("查看证据", {exact: true}).click();
      } else {
        state.userRedirect = true;
        await firstReview.getByRole("searchbox", {name: "搜索 Lark 用户姓名", exact: true}).fill("Creator");
        await firstReview.getByRole("button", {name: "搜索用户", exact: true}).click();
      }
    }
    await page.waitForFunction(() => !document.querySelectorAll("#creators-rows tr").length
      && !document.getElementById("creators-review-dialog").open);
    if (failure === "save") {
      const late = ["/creators/data", "/creators/imports/users", "/creators/imports/rows/review-two/evidence"].map(pathname =>
        page.waitForResponse(response => new URL(response.url()).pathname === pathname));
      releaseRoster(); releaseUsers(); releaseEvidence();
      await Promise.all((await Promise.all(late)).map(response => response.finished()));
      state.dataGate = state.userGate = state.evidenceGate = null;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
      await page.waitForFunction(() => document.getElementById("creators-refresh").getAttribute("aria-busy") !== "true");
    }
    assert.equal(await rows.count(), 0);
    assert.equal(await review.locator("#creators-review-rows").textContent(), "");
    assert.equal(await page.getByText("My handle is review.one", {exact: true}).count(), 0);
    assert.equal(await page.locator("#creators-cm option").count(), 0);
    assert(await page.locator("#creators-app").evaluate(app => [...app.querySelectorAll("button, input, select")].every(control => control.disabled)));
  }
  state.status = 200; state.handleSaveGate = null;
  Object.assign(state.data, {can_view_unassigned: false, can_select_cm: false, can_adjust_importance: false,
    can_edit_handles: true, can_link_people: true, read_only: false, import_batches: []});
  await page.reload(); await rows.first().waitFor();
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => document.querySelector('#creators-unassigned').hidden);
  assert.equal(await page.getByRole("button", {name: "更正 Lark 用户", exact: true}).count(), 0);
  await page.getByRole("button", {name: "手动关联", exact: true}).click();
  const manualLink = page.locator("#creators-link-dialog");
  assert(!(await manualLink.getByRole("combobox", {name: "查找范围", exact: true}).isVisible()));
  await manualLink.getByRole("textbox", {name: "TikTok handle", exact: true}).fill("manual.linked");
  await manualLink.getByRole("searchbox", {name: "对应 Lark 用户", exact: true}).fill("Same");
  await manualLink.getByRole("button", {name: "搜索", exact: true}).click();
  const personChoice = manualLink.getByRole("button", {name: "Same Name · Creator customer group", exact: true});
  await personChoice.click();
  await manualLink.getByRole("searchbox", {name: "对应 Lark 用户", exact: true}).fill("Same Name");
  assert(await manualLink.getByRole("button", {name: "关联并加入我的达人", exact: true}).isDisabled());
  await manualLink.getByRole("button", {name: "搜索", exact: true}).click(); await personChoice.click();
  if (process.env.OH_SCREENSHOT_DIR) {
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-manual-link.png")});
    await page.setViewportSize({width: 390, height: 844});
    assert(await manualLink.evaluate(element => {
      const bounds = element.getBoundingClientRect(); return bounds.left >= 0 && bounds.right <= innerWidth;
    }));
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "creator-manual-link-mobile.png")});
    await page.setViewportSize({width: 1280, height: 720});
  }
  await manualLink.getByRole("button", {name: "关联并加入我的达人", exact: true}).click();
  await page.getByText("已关联并加入达人名单。", {exact: true}).waitFor();
  const linked = new URLSearchParams(state.calls.findLast(call => call.url === "/creators/people/link").body);
  assert.equal(linked.get("csrf_token"), "fixture-csrf"); assert.equal(linked.get("cm_id"), "cm-1");
  assert.equal(linked.get("contact_id"), contactId); assert.equal(linked.get("handle"), "manual.linked");
  assert.equal(linked.get("old_contact_id"), null);
  await page.waitForFunction(() => document.querySelector('#creators-rows td').textContent.includes("@manual.linked"));
  await handleEditor.locator("summary").click();
  await handleEditor.getByRole("combobox", {name: "账号操作", exact: true}).selectOption("add");
  await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).fill("manual.extra");
  await handleEditor.getByRole("button", {name: "保存", exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#creators-rows td').textContent.includes("@manual.extra"));
  await handleEditor.locator("summary").click();
  await handleEditor.getByRole("combobox", {name: "账号操作", exact: true}).selectOption("remove");
  await handleEditor.getByRole("combobox", {name: "选择要修改的 handle", exact: true}).selectOption("manual.extra");
  assert(!(await handleEditor.getByRole("textbox", {name: "新 handle", exact: true}).isVisible()));
  await handleEditor.getByRole("button", {name: "解除关联", exact: true}).click();
  await page.waitForFunction(() => !document.querySelector('#creators-rows td').textContent.includes("@manual.extra"));
  assert(state.data.items[0].handles.includes("one.alt") && state.data.items[0].handles.includes("worker.added"));
  Object.assign(state.data, {can_view_unassigned: true, can_select_cm: true});
  await page.reload(); await rows.first().waitFor();
  await page.locator("#creators-refresh").click();
  await page.waitForFunction(() => document.querySelector('#creators-rows td').textContent.includes("@manual.linked"));
  await handleEditor.locator("summary").click();
  await handleEditor.getByRole("button", {name: "更正 Lark 用户", exact: true}).click();
  assert(await manualLink.getByRole("textbox", {name: "TikTok handle", exact: true}).evaluate(element => element.readOnly));
  await manualLink.getByRole("searchbox", {name: "对应 Lark 用户", exact: true}).fill("Same");
  await manualLink.getByRole("button", {name: "搜索", exact: true}).click();
  await manualLink.getByRole("button", {name: "Same Name · Private with Same Name", exact: true}).click();
  await manualLink.getByRole("button", {name: "保存更正", exact: true}).click();
  await page.getByText("账号关联已更正。", {exact: true}).waitFor();
  const corrected = new URLSearchParams(state.calls.findLast(call => call.url === "/creators/people/link").body);
  assert.equal(corrected.get("old_contact_id"), contactId);
  assert.equal(corrected.get("contact_id"), "22222222-2222-4222-8222-222222222222");
  await page.getByRole("button", {name: "手动关联", exact: true}).click();
  await manualLink.getByRole("searchbox", {name: "对应 Lark 用户", exact: true}).fill("Same");
  state.linkStatus = 403;
  await manualLink.getByRole("button", {name: "搜索", exact: true}).click();
  await page.waitForFunction(() => !document.querySelectorAll('#creators-rows tr').length);
  assert(!(await manualLink.evaluate(element => element.open)));
  assert.equal(await manualLink.locator('#creators-link-results').textContent(), "");
  assert.equal(await manualLink.locator('#creators-link-handle').inputValue(), "");
  assert.deepEqual(errors, []);
});

test("grades are sorted and filters/search/evidence work without writes", async t => {
  const {page, state, errors} = await fixture(t);
  assert.deepEqual(await page.locator(".oh-handling").allTextContents(), ["已解决", "未解决", "待判断", "无需处理", "待判断"]);
  assert.deepEqual(await page.locator(".oh-replied").allTextContents(), ["Remi 已回复", "Shang Ma 已回复", "未见回复"]);
  assert.deepEqual(await page.locator(".oh-event .oh-priority").allTextContents(), ["P0", "P1", "P2", "P3", "未分级"]);
  assert.equal(await page.locator("#oh-unresolved-count").textContent(), "1");
  assert.equal(await page.locator("#oh-review-count").textContent(), "2");
  assert(!(await page.getByText("截至所选时段判断。", {exact: true}).first().isVisible()));
  assert(!(await page.locator(".oh-routine .oh-event").isVisible()));
  assert.equal(await page.locator("#oh-count-P3").textContent(), "1");
  assert(await page.locator("#oh-analysis-spinner").isVisible());
  assert.equal(await page.locator("#oh-analysis-spinner").evaluate(element => getComputedStyle(element).animationName), "ui-spin");
  assert.equal(await page.locator('a[href^="/admin/"]').count(), 0);
  assert.equal(await page.locator("#oh-status-title").textContent(), "正在整理");
  assert.equal(await page.locator('#oh-selection select[name="cm_id"]').count(), 0);
  assert.equal(await page.locator('#oh-selection input[name="cm_id"]').inputValue(), "remi-id");
  assert(!(await page.locator("#oh-issues").evaluate(element => element.open)));
  assert(!(await page.locator("#oh-status-description").textContent()).includes("分析任务"));
  assert.equal(await page.locator("#oh-gaps").count(), 0);
  await page.locator('.oh-filter[data-priority="P0"]').click();
  assert.equal(await page.locator(".oh-event").count(), 1);
  await page.getByText("查看相关原文（1）").click();
  assert(await page.getByText("Please check this issue.").isVisible());
  assert(await page.getByText("截至所选时段判断。", {exact: true}).isVisible());
  await page.locator('.oh-filter[data-priority="unresolved"]').click();
  assert.equal(await page.locator(".oh-event").count(), 1);
  assert.equal(await page.locator(".oh-replied").textContent(), "Shang Ma 已回复");
  assert.equal(await page.locator(".oh-handling").textContent(), "未解决");
  assert(await page.getByRole("button", {name: "生成回复建议", exact: true}).isVisible());
  await page.locator('.oh-filter[data-priority="review"]').click();
  assert.equal(await page.locator(".oh-event").count(), 2);
  assert((await page.locator(".oh-identity").allTextContents()).some(text => text.includes("handle：—")));
  assert.equal(await page.locator(".oh-event").last().locator(".oh-replied").count(), 0);
  await page.locator('.oh-filter[data-priority="all"]').click();
  await page.locator("#oh-search").fill("Shang Ma");
  assert.equal(await page.locator(".oh-event").count(), 1);
  assert.equal(await page.locator(".oh-handling").textContent(), "未解决");
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
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "offhours-summary-mobile.png"), fullPage: true});
  }
  const resolved = state.data.events.find(event => event.handling_status === "resolved");
  resolved.priority = null; resolved.needs_review = true;
  await page.locator("#oh-refresh").click();
  await page.waitForFunction(() => document.getElementById("oh-count-P0").textContent === "0");
  assert.equal(await page.locator("#oh-review-count").textContent(), "2");
  await page.locator('.oh-filter[data-priority="review"]').click();
  assert.deepEqual(await page.locator(".oh-event-title").allTextContents(), ["P2 fixture event", "资料尚不足以判断"]);
  await page.locator('.oh-filter[data-priority="unresolved"]').click();
  assert.deepEqual(await page.locator(".oh-event-title").allTextContents(), ["P1 fixture event"]);
  assert(state.calls.every(call => call.method === "GET"));
  assert.deepEqual(errors, []);
});

test("disabled state contains no pretend counts or seeded results", async t => {
  const {page, state, errors} = await fixture(t, report({enabled: false, can_manage: true, run: null, events: []}));
  assert.equal(await page.locator("#oh-count-P0").textContent(), "—");
  assert.equal(await page.locator(".oh-event").count(), 0);
  assert(!(await page.locator("#oh-analysis-spinner").isVisible()));
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
  assert.equal(await page.locator("#oh-status-title").textContent(), "部分结果待更新");
  assert(!(await page.locator("#oh-analysis-spinner").isVisible()));
  const description = await page.locator("#oh-status-description").textContent();
  assert(description.includes("部分消息暂未纳入"));
  assert(!description.includes("尚未到时段结束时间"));
  assert.deepEqual(errors, []);
});

test("source content is literal, network failures show stale results, and revoked access clears them", async t => {
  const attack = '<img src=x onerror="window.compromised=true">';
  const {page, state, errors} = await fixture(t, report({events: [event("P0", {
    title: attack, replied_by: [attack], reply_evidence_refs: ["M1"], evidence: [{text: attack}],
  })]}));
  assert.equal(await page.locator(".oh-event-title").textContent(), attack);
  assert.equal(await page.locator(".oh-replied").textContent(), `${attack} 已回复`);
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

test("admin role-scoped controls keep native save values and loading without production writes", async t => {
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  for (const role of ["owner", "reviewer"]) {
    const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
      "import sys; sys.path.insert(0,'tests'); from test_offhours_summary_ui import render_admin; print(render_admin(sys.argv[1]))", role],
      {cwd: backend, encoding: "utf8"});
    assert.equal(rendered.status, 0, rendered.stderr);
    const page = await browser.newPage({viewport: {width: 1360, height: 1100}});
    const writes = [], state = {};
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") {
        writes.push(route.request().postData());
        assert(state.postGate, "Only the explicitly simulated form save may POST");
        await state.postGate;
        return route.fulfill({contentType: "text/html", body: rendered.stdout});
      }
      if (url.origin !== "http://localhost") {
        if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
        return route.abort();
      }
      if (url.pathname === "/admin/ai") {
        if (url.search && state.navigationGate) await state.navigationGate;
        return route.fulfill({contentType: "text/html", body: rendered.stdout});
      }
      if (url.pathname === "/fixture-export") return route.fulfill({contentType: "application/zip", headers: {"Content-Disposition": 'attachment; filename="fixture.zip"'}, body: "fixture-only"});
      if (url.pathname.startsWith("/static/")) return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css", body: fs.readFileSync(path.join(backend, "app", url.pathname))});
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
    if (role === "owner") {
      await page.evaluate(() => {
        const link = document.createElement("a"); link.id = "fixture-export";
        link.href = "/fixture-export"; link.dataset.uiDownload = ""; link.textContent = "下载"; document.body.append(link);
      });
      const download = page.waitForEvent("download");
      await page.locator("#fixture-export").click(); await download;
      assert.equal(await page.locator("html").getAttribute("data-ui-navigating"), null);
      const save = card.getByRole("button", {name: "保存配置（开启则立即执行）", exact: true});
      await card.locator('input[name="project_id"]').fill("fixture-project");
      await save.evaluate(button => { button.name = "operation"; button.value = "save"; });
      let saving;
      page.on("console", message => {
        if (message.text().startsWith("fixture-native-save:")) saving = JSON.parse(message.text().slice("fixture-native-save:".length));
      });
      await page.emulateMedia({reducedMotion: "reduce"});
      assert.equal(await page.evaluate(() => {
        document.documentElement.dataset.uiNavigating = "true";
        const animation = getComputedStyle(document.documentElement, "::before").animationName;
        delete document.documentElement.dataset.uiNavigating; return animation;
      }), "none");
      await page.emulateMedia({reducedMotion: "no-preference"});
      await page.evaluate(() => {
        let checked = false;
        document.addEventListener("submit", event => {
          if (checked || !event.target.closest("#offhours-summary")) return;
          checked = true;
          const button = event.submitter;
          const duplicate = new SubmitEvent("submit", {bubbles: true, cancelable: true, submitter: button});
          event.target.dispatchEvent(duplicate);
          console.log("fixture-native-save:" + JSON.stringify({busy: button.getAttribute("aria-busy"), disabled: button.disabled,
            blocked: button.getAttribute("aria-disabled"), animation: getComputedStyle(button, "::before").animationName,
            duplicatePrevented: duplicate.defaultPrevented}));
        });
      });
      let releasePost;
      state.postGate = new Promise(resolve => { releasePost = resolve; });
      const posting = page.waitForRequest(request => request.method() === "POST");
      const saveAction = save.click(); const posted = await posting;
      assert.equal(saving.busy, "true"); assert.equal(saving.blocked, "true");
      assert(!saving.disabled, "Do not disable native submitter before serialization");
      const fields = new URLSearchParams(posted.postData());
      assert.equal(fields.get("operation"), "save"); assert.equal(fields.get("csrf_token"), "fixture-csrf");
      assert.equal(fields.get("feature"), "offhours_summary"); assert.equal(fields.get("window_end"), "2026-09-10T12:00");
      assert.equal(saving.animation, "ui-spin");
      assert(saving.duplicatePrevented);
      const loaded = page.waitForEvent("load"); releasePost(); await loaded; await saveAction;
      assert.equal(page.url(), "http://localhost/admin/ai"); assert(await card.isVisible());
      assert.equal(writes.length, 1);
      assert.equal(await page.locator("html").getAttribute("data-ui-navigating"), null);
      assert.equal(await card.locator('[aria-busy="true"]').count(), 0);
      await page.evaluate(() => {
        const link = document.createElement("a"); link.id = "fixture-page";
        link.href = "/admin/ai?page=8"; link.textContent = "第八页"; document.body.append(link);
        document.addEventListener("click", event => {
          if (event.target.id === "fixture-page") event.preventDefault();
        });
      });
      await page.evaluate(() => document.getElementById("fixture-page").click());
      assert.equal(await page.locator("html").getAttribute("data-ui-navigating"), "true");
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", {persisted: true})));
      assert.equal(await page.locator("html").getAttribute("data-ui-navigating"), null);
      assert.equal(await card.locator('[aria-busy="true"]').count(), 0);
    }
    await page.close();
  }
});

test("each summary selector navigates immediately without a submit button", async t => {
  const data = report({selection: {
    cms: [{id: "remi-id", name: "Remi"}, {id: "nero-id", name: "Nero"}],
    dates: ["2026-09-09", "2026-09-10"], date: "2026-09-09", period: "off",
  }});
  const {page, state, errors} = await fixture(t, data);
  assert.equal(await page.locator('#oh-selection button, [aria-label="总结范围"]').count(), 0);
  for (const [name, value] of [["cm_id", "nero-id"], ["date", "2026-09-10"], ["period", "work"]]) {
    let loading, restored;
    page.on("console", message => {
      if (message.text().startsWith("fixture-summary-navigation:")) {
        ({loading, restored} = JSON.parse(message.text().slice("fixture-summary-navigation:".length)));
      }
    });
    await page.evaluate(name => {
      document.addEventListener("change", event => {
        if (!event.target.closest("#oh-selection")) return;
        const loading = {title: document.getElementById("oh-status-title").textContent,
          events: document.getElementById("oh-events").inert, selection: document.getElementById("oh-selection").inert,
          navigating: document.documentElement.dataset.uiNavigating};
        let restored;
        if (name === "cm_id") {
          window.dispatchEvent(new PageTransitionEvent("pageshow", {persisted: true}));
          restored = {cm: document.querySelector('#oh-selection select[name="cm_id"]').value,
            inert: document.getElementById("oh-events").inert};
        }
        console.log("fixture-summary-navigation:" + JSON.stringify({loading, restored}));
      }, {once: true});
    }, name);
    let releaseNavigation;
    state.navigationGate = new Promise(resolve => { releaseNavigation = resolve; });
    const request = page.waitForRequest(request => new URL(request.url()).searchParams.get(name) === value);
    const selecting = page.locator(`#oh-selection select[name="${name}"]`).selectOption(value); await request;
    assert.equal(loading.title, "正在加载总结…"); assert(loading.events); assert(loading.selection);
    assert.equal(loading.navigating, "true");
    if (name === "cm_id") {
      assert.equal(restored.cm, "remi-id"); assert(!restored.inert);
    }
    const loaded = page.waitForEvent("load"); releaseNavigation(); await loaded; await selecting;
    state.navigationGate = null;
    await page.waitForFunction(() => document.getElementById("oh-status-title").textContent !== "正在读取总结");
    const url = new URL(page.url());
    assert.equal(url.pathname, "/dashboard/offhours-summary");
    assert.equal(url.searchParams.size, 3);
  }
  assert.deepEqual(errors, []);
});


test("reply drafts copy edits without auto translation, warn on new messages and block changed or revoked sources", async t => {
  const {page, errors} = await fixture(t, report({events: [event("P1", {
    replied_by: ["Shang Ma"], reply_evidence_refs: ["M2"],
  })]}));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  let edited = "", stale = false, revoked = false, translationFailed = false, releaseGenerate;
  const generateGate = new Promise(resolve => { releaseGenerate = resolve; });
  const posted = [];
  await page.route("**/dashboard/offhours-summary/replies**", async route => {
    if (revoked) return route.fulfill({status: 403, json: {error: "权限已失效"}});
    const req = route.request();
    if (req.method() === "POST") {
      const body = req.postDataJSON(); posted.push(body);
      assert.equal(req.headers()["x-csrf-token"], "fixture-csrf");
      if (body.parent_id && translationFailed) return route.fulfill({status: 503, json: {error: "翻译暂时不可用。"}});
      if (body.parent_id) edited = body.text;
      else await generateGate;
      return route.fulfill({status: 202, json: {id: body.parent_id ? "translation" : "draft", status: "queued"}});
    }
    if (stale) return route.fulfill({json: {status: "stale", error: "资料已变化，请重新生成建议。",
      ...(stale === "new_messages" ? {reason_code: "new_messages"} : {})}});
    const translation = req.url().includes("/translation?");
    return route.fulfill({json: {status: "succeeded", result: translation
      ? {draft: edited, chinese_translation: "修改后的中文翻译，金额仍为 £200。"}
      : {outcome: "ready", language: "en", draft: "Hi lovely, I can check that £200 payment with you. Could you confirm which order this concerns?",
        chinese_translation: "你好，我可以帮你核对这笔 £200 的付款。请确认对应哪笔订单。", explanation: "尚未看到完成结果。",
        checks: ["确认可以核对。"], checked_at: "2026-09-16T01:00:00Z"}}});
  });
  await page.getByRole("button", {name: "生成回复建议", exact: true}).click();
  assert.equal(await page.getByRole("button", {name: "生成回复建议", exact: true}).getAttribute("aria-busy"), "true");
  releaseGenerate();
  const editor = page.getByLabel("建议回复（可编辑）");
  await editor.waitFor({state: "visible"});
  const newText = "Hi lovely, I can check that £200 payment with you. I have the order number already, so you do not need to resend it.";
  await editor.fill(newText);
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isEnabled());
  assert(!(await page.locator(".oh-reply-translation").textContent()).includes("请确认对应哪笔订单"));
  await page.waitForTimeout(1700);
  assert.equal(posted.length, 1);
  await page.getByRole("button", {name: "更新中文翻译", exact: true}).click();
  await page.getByText("修改后的中文翻译，金额仍为 £200。", {exact: true}).waitFor();
  await page.getByRole("button", {name: "刷新", exact: true}).click();
  assert.equal(await editor.inputValue(), newText);
  await page.evaluate(() => {
    const write = navigator.clipboard.writeText.bind(navigator.clipboard);
    let first = true;
    navigator.clipboard.writeText = text => {
      if (first) { first = false; return Promise.reject(new DOMException("Fixture permission rejected", "NotAllowedError")); }
      return write(text);
    };
  });
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("浏览器未允许复制，可重试或手动复制。", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isEnabled());
  assert.equal(await page.getByRole("button", {name: "复制回复", exact: true}).getAttribute("aria-busy"), null);
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("已复制，未发送。", {exact: true}).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), newText);
  assert.equal(await page.locator(".oh-handling").textContent(), "未解决");
  assert.equal(await page.locator(".oh-replied").textContent(), "Shang Ma 已回复");
  translationFailed = true;
  await page.getByRole("button", {name: "更新中文翻译", exact: true}).click();
  await page.getByText("翻译暂时不可用。", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isEnabled());
  assert.equal(await page.getByRole("button", {name: "更新中文翻译", exact: true}).getAttribute("aria-busy"), null);
  assert.equal(await editor.inputValue(), newText);
  stale = "new_messages";
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("已复制。聊天有新消息，请核对后发送。", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isEnabled());
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), newText);
  stale = "changed";
  await page.getByRole("button", {name: "复制回复", exact: true}).click();
  await page.getByText("资料已变化，请重新生成建议。", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "复制回复", exact: true}).isDisabled());
  revoked = true;
  await page.getByRole("button", {name: "重新生成", exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll(".oh-event").length === 0);
  assert.equal(posted.length, 3);
  assert.deepEqual(errors, []);
});

test("broadcast waiting states preserve selections and drafts after request failures", async t => {
  const actor = "11111111-1111-4111-8111-111111111111";
  const other = "22222222-2222-4222-8222-222222222222";
  const personalList = {id: "list-existing", name: "Fixture saved groups", purpose: "category", cm_id: actor,
    mention: true, revision: 1, target_count: 1};
  const data = {can_select_cm: true, selected_cm_id: actor, cms: [{id: actor, name: "CM"}, {id: other, name: "Other CM"}],
    purposes: {support: "客服群", category: "类目群"}, lists: [personalList], templates: []};
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
    "import json,sys; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); print(Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('creator_broadcast.html').render(authorization={'id':data['selected_cm_id'],'display_name':'CM'},broadcast_data=data,csrf_token='fixture-csrf'))"],
    {cwd: backend, input: JSON.stringify(data), encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1360, height: 1000}});
  const errors = [], calls = [], holds = new Map();
  let currentBatch = null, batchTargets = [];
  const secondImageId = '55555555-5555-4555-8555-555555555555';
  const imageId = '33333333-3333-4333-8333-333333333333';
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1sAAAAASUVORK5CYII=', 'base64');
  const gifId = '66666666-6666-4666-8666-666666666666';
  const gif = Buffer.from('R0lGODlhAQABAIAAAP///////yH5BAEAAAEALAAAAAABAAEAAAICRAEAOw==','base64');
  page.on("pageerror", error => errors.push(error.message));
  function hold(action) {
    let started, release;
    const arrived = new Promise(resolve => { started = resolve; });
    const response = new Promise(resolve => { release = resolve; });
    holds.set(action, {started, response}); return {arrived, release};
  }
  const permissions = hold("permissions");
  await page.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.origin !== "http://localhost") {
      if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (url.pathname === "/creators/broadcast") return route.fulfill({contentType: "text/html", body: rendered.stdout});
    if (url.pathname === '/auth/lark/start') return route.fulfill({contentType:'text/html',body:'<p>Fixture authorization only</p>'});
    if (url.pathname.startsWith("/static/")) return route.fulfill({
      contentType: /\.(m?js)$/.test(url.pathname) ? "text/javascript" : "text/css",
      body: fs.readFileSync(path.join(backend, "app", url.pathname))});
    const action = url.pathname.replace("/creators/broadcast/", "");
    if (req.method() === 'GET' && action.startsWith('images/')) return route.fulfill({contentType:action.endsWith(gifId) ? 'image/gif' : 'image/png',body:action.endsWith(gifId) ? gif : png});
    if (req.method() === "GET" && action === "batches") return route.fulfill({json: {batches: currentBatch ? [currentBatch] : []}});
    if (req.method() === "GET" && action.startsWith("batches/")) return route.fulfill({json: {batch: currentBatch, targets: batchTargets, message: currentBatch.message_body}});
    assert.equal(req.method(), "POST"); assert.equal(req.headers()["x-csrf-token"], "fixture-csrf");
    calls.push({action, body: action === 'images' ? {size:req.postDataBuffer().length,type:req.headers()['content-type']} : req.postDataJSON()});
    const held = holds.get(action);
    assert(held, `Unexpected fixture request: ${url.pathname}`);
    held.started(); const response = await held.response; holds.delete(action);
    return route.fulfill(response);
  });
  await page.goto("http://localhost/creators/broadcast");
  await permissions.arrived;
  assert(!(await page.locator("#broadcast-workspace").evaluate(el => el.disabled)));
  assert.equal(await page.locator('#broadcast-message .ql-editor').getAttribute('contenteditable'),'true');
  assert.equal(await page.locator("#broadcast-permission-status").getAttribute("aria-busy"), "true");
  assert.equal(await page.locator("#broadcast-permission-status .ui-spinner").count(), 1);
  assert.equal(await page.locator("#broadcast-permission-status .ui-spinner").evaluate(el => getComputedStyle(el).animationName), "ui-spin");
  permissions.release({json: {status: "needs_authorization", feature_enabled: false, account_name: "CM", account_id: actor,
    is_self: true, is_sender: true, authorization_url: `/auth/lark/start?send=1&account_id=${actor}`}});
  await page.waitForFunction(() => document.getElementById("broadcast-permission-status").getAttribute("aria-busy") === "false");
  assert.equal(new URL(page.url()).pathname, "/creators/broadcast");
  assert(await page.locator("#broadcast-permission-authorize").isVisible());
  assert.equal(await page.locator("#broadcast-permission-status").getAttribute("aria-busy"), "false");
  assert.equal(await page.locator("#broadcast-permission-status .ui-spinner").count(), 0);

  const firstGroups = hold("groups");
  await page.getByRole("button", {name: "类目群", exact: true}).click();
  await firstGroups.arrived;
  assert.equal(await page.locator("#broadcast-group-items").getAttribute("aria-busy"), "true");
  assert.equal(await page.locator("#broadcast-group-items .ui-spinner").count(), 1);
  assert((await page.locator("#broadcast-group-items").textContent()).includes("加载群聊"));
  const groupResult = {groups: [{id: "oc_category", name: "女装类目", purpose: "category", members: []}], issues: []};
  firstGroups.release({json: groupResult});
  const selected = page.getByRole("checkbox", {name: "选择群 女装类目", exact: true});
  await selected.check();
  const refreshGroups = hold("groups");
  await page.locator("#broadcast-group-refresh").click();
  await refreshGroups.arrived;
  assert.equal(await page.locator("#broadcast-group-refresh").getAttribute("aria-busy"), "true");
  assert.equal(await page.locator("#broadcast-group-items").getAttribute("aria-busy"), "true");
  assert(await selected.isChecked());
  assert.equal(await page.locator("#broadcast-group-items .ui-spinner").count(), 0);
  refreshGroups.release({json: groupResult});
  await page.waitForFunction(() => document.getElementById("broadcast-group-items").getAttribute("aria-busy") === "false");
  assert(await selected.isChecked());
  assert.equal(await page.locator("#broadcast-group-refresh").getAttribute("aria-busy"), "false");

  await page.getByRole("button", {name: "客服群", exact: true}).click();
  const raw = "@sample.handle\nSample Creator";
  await page.locator("#broadcast-input").fill(raw);
  const matching = hold("match");
  await page.locator("#broadcast-match").click();
  await matching.arrived;
  assert.equal(await page.locator("#broadcast-match").getAttribute("aria-busy"), "true");
  assert.equal(await page.locator("#broadcast-match").textContent(), "匹配名单");
  assert.equal(await page.locator("#broadcast-notice .ui-spinner").count(), 1);
  matching.release({status: 503, json: {error: "名单暂时无法读取，请重试。"}});
  await page.getByText("名单暂时无法读取，请重试。", {exact: true}).waitFor();
  assert.equal(await page.locator("#broadcast-match").getAttribute("aria-busy"), "false");
  assert.equal(await page.locator("#broadcast-notice .ui-spinner").count(), 0);
  assert.equal(await page.locator("#broadcast-input").inputValue(), raw);
  assert(!(await page.locator("#broadcast-workspace").evaluate(el => el.disabled)));

  const message = "Hello, please check this week's offer.";
  await page.locator('#broadcast-message .ql-editor').fill(message);
  await page.locator("#broadcast-template-new").click();
  await page.locator("#broadcast-template-name").fill("本周活动");
  const saving = hold("templates/save");
  await page.locator("#broadcast-template-save").click();
  await saving.arrived;
  assert.equal(await page.locator("#broadcast-template-save").getAttribute("aria-busy"), "true");
  assert.equal(await page.locator("#broadcast-template-save").textContent(), "保存并使用");
  assert.equal(await page.locator("#broadcast-template-dialog").getAttribute("open"), "");
  saving.release({status: 503, json: {error: "模板未保存，请重试。"}});
  await page.getByText("模板未保存，请重试。", {exact: true}).waitFor();
  assert.equal(await page.locator("#broadcast-template-save").getAttribute("aria-busy"), "false");
  assert(!(await page.locator("#broadcast-template-save").isDisabled()));
  assert(await page.locator("#broadcast-template-dialog").isVisible());
  assert.equal(await page.locator("#broadcast-template-name").inputValue(), "本周活动");
  assert.equal((await page.locator('#broadcast-template-body .ql-editor').innerText()).trim(), message);
  assert.equal((await page.locator('#broadcast-message .ql-editor').innerText()).trim(), message);
  assert.equal(await page.locator("[aria-busy='true']").count(), 0);
  assert.equal(await page.locator("html").getAttribute("data-ui-navigating"), null);
  assert.deepEqual(calls.map(call => call.action), ["permissions", "groups", "groups", "match", "templates/save"]);

  await page.locator("#broadcast-template-cancel").click();
  await page.locator("#broadcast-list-action-proceed").click();
  const demoGroups = hold("groups");
  await page.getByRole("button", {name: "类目群", exact: true}).click();
  await demoGroups.arrived; demoGroups.release({json: groupResult});
  await selected.check(); await page.locator("#broadcast-mention").check();
  const previewResult = {message, groups: [{...groupResult.groups[0], people: [], mentions: [], mention_all: true}], issues: [], people: []};
  const demoPreview = hold("preview");
  await page.locator("#broadcast-preview").click(); await demoPreview.arrived; demoPreview.release({json: previewResult});
  await page.locator("#broadcast-confirm").waitFor({state: "visible"});
  assert(await page.locator("#broadcast-send").isDisabled());
  assert.equal(calls.filter(call => ["prepare", "send"].includes(call.action)).length, 0);
  await page.locator("#broadcast-back").click();

  const ready = hold("permissions");
  await page.locator("#broadcast-permission-retry").click(); await ready.arrived;
  ready.release({json: {status: "ready", feature_enabled: true, account_name: "CM", is_self: true, is_sender: true}});
  await page.waitForFunction(() => document.getElementById("broadcast-permission-status").textContent === "本人发送已就绪");
  const preparing = hold("prepare");
  await page.locator("#broadcast-preview").click(); await preparing.arrived;
  assert.equal(await page.locator("#broadcast-preview").getAttribute("aria-busy"), "true");
  assert.equal(calls.filter(call => call.action === "send").length, 0);
  preparing.release({json: {...previewResult, preview_id: "preview-1", account_name: "CM", expires_at: "2030-01-01T00:00:00Z"}});
  await page.waitForFunction(() => document.getElementById("broadcast-send").textContent === "确认发送 1 个群");
  assert.equal(await page.locator("#broadcast-confirm-state").textContent(), "以 CM 本人身份发送");
  assert.equal(calls.filter(call => call.action === "send").length, 0);
  if (process.env.OH_SCREENSHOT_DIR) {
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "broadcast-single-confirm.png")});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "broadcast-single-confirm-mobile.png")});
    await page.setViewportSize({width: 1360, height: 1000});
  }

  const failedSend = hold("send");
  await page.locator("#broadcast-send").click(); await failedSend.arrived;
  assert(await page.locator("#broadcast-send").isDisabled());
  failedSend.release({status: 503, json: {error: "发送任务响应中断，请重试确认。"}});
  await page.getByText("发送任务响应中断，请重试确认。", {exact: true}).waitFor();
  assert.equal((await page.locator('#broadcast-message .ql-editor').innerText()).trim(), message);
  const send = hold("send");
  await page.locator("#broadcast-send").click(); await send.arrived;
  const submissions = calls.filter(call => call.action === "send");
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0].body.request_id, submissions[1].body.request_id);
  assert.equal(submissions[0].body.preview_id, "preview-1");
  currentBatch = {id: "batch-1", account_name: "CM", status: "partial", created_at: "2026-10-09T12:00:00Z",
    total: 3, pending: 0, sending: 0, succeeded: 1, failed: 1, unknown: 1, cancelled: 0, can_retry: false, message_body: message};
  batchTargets = [{chat_id: "oc_success", chat_name: "成功群", status: "succeeded"}, {chat_id: "oc_failed", chat_name: "失败群", status: "failed", error_message: "发送权限不足"},
    {chat_id: "javascript:alert(1)", chat_name: "不确定群", status: "unknown", error_message: "结果未知，请在 Lark 核对"}];
  send.release({json: {batch: currentBatch}});
  await page.locator("#broadcast-batch-dialog").waitFor({state: "visible"});
  await page.getByText("发送权限不足", {exact: true}).waitFor();
  assert.equal(await page.locator("#broadcast-batch-targets .badge").allTextContents().then(items => items.join(",")), "已发送,失败,状态未知");
  assert(!(await page.locator("#broadcast-batch-retry").isVisible()));
  assert(!(await page.locator("#broadcast-batch-cancel").isVisible()));
  assert((await page.locator("#broadcast-batch-note").textContent()).includes("状态未知项不会重发"));
  const groupLinks = page.locator("#broadcast-batch-targets a");
  assert.equal(await groupLinks.count(), 2);
  assert.equal(await groupLinks.first().getAttribute("href"), "https://applink.larksuite.com/client/chat/open?openChatId=oc_success");
  assert.equal(await groupLinks.first().getAttribute("rel"), "noopener noreferrer");
  const bodyPanel = page.locator("#broadcast-batch-message-panel");
  assert(!(await bodyPanel.evaluate(element => element.open)));
  await bodyPanel.locator("summary").click();
  assert.equal((await page.locator('#broadcast-batch-message').textContent()).trim(), message);
  currentBatch.can_retry = true;
  await page.locator("#broadcast-batch-refresh").click();
  await page.locator("#broadcast-batch-retry").waitFor({state: "visible"});
  const retrying = hold("batches/batch-1/retry");
  await page.locator("#broadcast-batch-retry").click(); await retrying.arrived;
  currentBatch = {...currentBatch, status: "sending", pending: 0, sending: 1, failed: 0, can_retry: false};
  retrying.release({json: {batch: currentBatch}});
  await page.locator("#broadcast-batch-cancel").waitFor({state: "visible"});
  const stopping = hold("batches/batch-1/cancel");
  await page.locator("#broadcast-batch-cancel").click(); await stopping.arrived;
  currentBatch = {...currentBatch, status: "partial", pending: 0, sending: 0, cancelled: 1};
  stopping.release({json: {batch: currentBatch}});
  await page.waitForFunction(() => document.getElementById("broadcast-batch-cancel").hidden);
  assert.equal(calls.filter(call => call.action === "send").length, 2);
  assert.equal(calls.filter(call => call.action.endsWith("/retry")).length, 1);
  assert.equal(calls.filter(call => call.action.endsWith("/cancel")).length, 1);
  await page.locator("#broadcast-batch-close").click();
  const permissionCalls = calls.filter(call => call.action === "permissions").length;
  await page.locator("#broadcast-list-library").click();
  await page.locator("#broadcast-library-items").getByRole("button", {name: "使用名单 Fixture saved groups", exact: true}).click();
  const loadingList = hold("lists/load");
  await page.locator("#broadcast-list-action-proceed").click(); await loadingList.arrived;
  loadingList.release({json: {...groupResult, entries: [], mode: "groups", purpose: "category", selected_cm_id: actor,
    selected_chat_ids: ["oc_category"], list: {...personalList, targets: [{chat_id: "oc_category"}], snapshot: {mode: "groups"}}}});
  await page.waitForFunction(() => !document.getElementById("broadcast-list-action-dialog").open);
  assert.equal(calls.filter(call => call.action === "permissions").length, permissionCalls);
  assert(await selected.isChecked());
  assert.equal((await page.locator('#broadcast-message .ql-editor').innerText()).trim(), message);
  const otherGroups = hold("groups");
  await page.locator("#broadcast-cm").selectOption(other);
  assert.equal(await page.locator("#broadcast-permission-account").textContent(), "发送账号：CM");
  await otherGroups.arrived; otherGroups.release({json: groupResult});
  await page.waitForFunction(() => document.getElementById("broadcast-group-items").getAttribute("aria-busy") === "false");
  assert.equal(await page.locator("#broadcast-permission-account").textContent(), "发送账号：CM");
  assert(await page.locator("#broadcast-send").isDisabled());
  assert.equal(calls.filter(call => call.action === "permissions").length, permissionCalls);
  await selected.check();
  const otherPreview = hold("preview");
  await page.locator("#broadcast-preview").click(); await otherPreview.arrived;
  otherPreview.release({json: previewResult});
  await page.locator("#broadcast-confirm").waitFor({state: "visible"});
  assert(await page.locator("#broadcast-send").isDisabled());
  assert.equal(calls.filter(call => call.action === "prepare").length, 1);
  await page.locator('#broadcast-back').click();
  const ownGroups = hold('groups'); await page.locator('#broadcast-cm').selectOption(actor);
  await ownGroups.arrived; ownGroups.release({json:groupResult}); await selected.check();
  await page.locator('#broadcast-message .ql-editor').fill('New offer\nPlease check');
  await page.evaluate(() => window.Quill.find(document.querySelector('#broadcast-message .ql-container')).setSelection(0,9));
  await page.locator('#broadcast-message').getByRole('button',{name:'加粗',exact:true}).click();
  await page.locator('#broadcast-message .broadcast-emoji').selectOption('🎉');
  const failedUploadPermission = hold('permissions');
  await page.locator('#broadcast-message input[type=file]').setInputFiles({name:'fixture.png',mimeType:'image/png',buffer:png});
  await failedUploadPermission.arrived;
  assert.equal(calls.at(-1).body.media,true);
  const failedImage = hold('images');
  failedUploadPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await failedImage.arrived;
  assert(await page.locator('#broadcast-preview').isDisabled());
  failedImage.release({status:503,json:{error:'图片未上传，请重试。'}});
  await page.getByText('图片未上传，请重试。',{exact:false}).waitFor();
  assert(await page.locator('#broadcast-preview').isDisabled());
  const retryUploadPermission = hold('permissions');
  await page.locator('#broadcast-message').getByRole('button',{name:'重试',exact:true}).click();
  await retryUploadPermission.arrived;
  const uploadedImage = hold('images');
  retryUploadPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await uploadedImage.arrived;
  uploadedImage.release({json:{image:{id:imageId,url:`/creators/broadcast/images/${imageId}`,mime_type:'image/png'}}});
  await page.locator(`#broadcast-message img[data-asset-id="${imageId}"]`).waitFor();
  assert.equal(calls.filter(call => call.action === 'images').length,2);
  assert(calls.filter(call => call.action === 'images').every(call => call.body.size === png.length && call.body.type === 'image/png'));
  const readDocument = () => page.evaluate(() => ({version:2,ops:window.Quill.find(document.querySelector('#broadcast-message .ql-container')).getContents().ops}));
  const beforeMove = await readDocument();
  await page.locator(`#broadcast-message img[data-asset-id="${imageId}"]`).click();
  await page.locator('#broadcast-message').getByRole('button',{name:'下移',exact:true}).click();
  assert.notDeepEqual((await readDocument()).ops,beforeMove.ops);
  const pastedPermission = hold('permissions');
  await page.evaluate(bytes => {
    const editor = window.Quill.find(document.querySelector('#broadcast-message .ql-container'));
    editor.setSelection(editor.getLength()-1,0);
    const data = new DataTransfer(); data.items.add(new File([new Uint8Array(bytes)],'pasted.png',{type:'image/png'}));
    editor.root.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,clipboardData:data}));
  },[...png]);
  await pastedPermission.arrived;
  const pastedImage = hold('images'); pastedPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await pastedImage.arrived; pastedImage.release({json:{image:{id:secondImageId,mime_type:'image/png'}}});
  await page.locator(`#broadcast-message img[data-asset-id="${secondImageId}"]`).click();
  await page.locator('#broadcast-message').getByRole('button',{name:'移除',exact:true}).click();
  assert.equal(await page.locator('#broadcast-message img').count(),1);
  await page.locator('#broadcast-template-new').click();
  const templateImagePermission = hold('permissions');
  await page.locator('#broadcast-template-body input[type=file]').setInputFiles({name:'failed-template.png',mimeType:'image/png',buffer:png});
  await templateImagePermission.arrived;
  const templateImage = hold('images'); templateImagePermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await templateImage.arrived; templateImage.release({status:503,json:{error:'Template image upload failed'}});
  await page.getByText('Template image upload failed',{exact:false}).waitFor();
  assert(await page.locator('#broadcast-preview').isDisabled());
  await page.locator('#broadcast-template-cancel').click(); await page.locator('#broadcast-list-action-proceed').click();
  assert(!(await page.locator('#broadcast-preview').isDisabled()));
  await page.locator('#broadcast-template-new').click();
  await page.locator('#broadcast-template-name').fill('Rich fixture');
  const savingRich = hold('templates/save'); await page.locator('#broadcast-template-save').click(); await savingRich.arrived;
  const richTemplate = {id:'44444444-4444-4444-8444-444444444444',name:'Rich fixture',body:calls.at(-1).body.body,revision:1};
  assert.equal(richTemplate.body.version,2); assert(richTemplate.body.ops.some(op => op.insert?.image === imageId));
  assert(richTemplate.body.ops.some(op => op.attributes?.bold)); assert(richTemplate.body.ops.some(op => typeof op.insert === 'string' && op.insert.includes('🎉')));
  savingRich.release({json:{template:richTemplate,templates:[richTemplate]}});
  await page.waitForFunction(() => !document.getElementById('broadcast-template-dialog').open);
  const richPermission = hold('permissions'), richPrepare = hold('prepare');
  await page.locator('#broadcast-preview').click(); await richPermission.arrived;
  richPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await richPrepare.arrived;
  const richMessage = calls.at(-1).body.message;
  assert.deepEqual(richMessage,richTemplate.body);
  const richPreview = {...previewResult,message:richMessage,preview_id:'rich-preview',account_name:'CM',expires_at:'2030-01-01T00:00:00Z'};
  richPrepare.release({json:richPreview}); await page.locator('#broadcast-confirm').waitFor({state:'visible'});
  assert.equal(await page.locator('#broadcast-confirm-message img').getAttribute('src'),`/creators/broadcast/images/${imageId}`);
  assert(await page.locator('#broadcast-confirm-message strong').count());
  const richSend = hold('send'); await page.locator('#broadcast-send').click(); await richSend.arrived;
  currentBatch = {...currentBatch,id:'rich-batch',status:'succeeded',total:1,succeeded:1,failed:0,unknown:0,pending:0,sending:0,cancelled:0,can_retry:false,message_body:richMessage};
  batchTargets = [{chat_id:'oc_category',chat_name:'女装类目',status:'succeeded'}];
  richSend.release({json:{batch:currentBatch}}); await page.locator('#broadcast-batch-message-panel').waitFor({state:'visible'});
  await page.locator('#broadcast-batch-message-panel summary').click();
  assert.equal(await page.locator('#broadcast-batch-message img').getAttribute('src'),`/creators/broadcast/images/${imageId}`);
  assert(await page.locator('#broadcast-batch-message strong').count());
  const pasted = await page.evaluate(() => window.Quill.find(document.querySelector('#broadcast-message .ql-container')).clipboard.convert({
    html:'<p>Safe <img src="https://untrusted.invalid/picture.png"><a href="javascript:alert(1)">label</a><script>alert(1)</script></p>',text:''}).ops);
  assert(pasted.every(op => typeof op.insert === 'string' && (!op.attributes?.link || /^https?:/.test(op.attributes.link))));
  if (process.env.OH_SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.OH_SCREENSHOT_DIR,'broadcast-rich-history.png')});
  await page.locator('#broadcast-batch-close').click();
  const missingMedia = hold('permissions'); await page.locator('#broadcast-message').getByRole('button',{name:'添加图片',exact:true}).click();
  await missingMedia.arrived;
  missingMedia.release({json:{status:'needs_authorization',resource_authorized:false,is_self:true,
    authorization_url:`/auth/lark/start?send=1&media=1&account_id=${actor}`}});
  await page.waitForURL('**/auth/lark/start?**');
  const savedDraft = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ttmedia.broadcast.authorization-draft')));
  assert.equal(savedDraft.version,2); assert.equal(savedDraft.actor_id,actor); assert.deepEqual(savedDraft.message,richMessage);
  const sendCount = calls.filter(call => call.action === 'send').length;
  const restoredPermission = hold('permissions'), restoredGroups = hold('groups');
  await page.goto('http://localhost/creators/broadcast'); await restoredPermission.arrived;
  restoredPermission.release({json:{status:'ready',feature_enabled:true,resource_authorized:true,account_name:'CM',is_self:true,is_sender:true}});
  await restoredGroups.arrived; restoredGroups.release({json:groupResult});
  await page.waitForFunction(() => document.getElementById('broadcast-group-items').getAttribute('aria-busy') === 'false');
  assert.deepEqual(await readDocument(),richMessage); assert(await selected.isChecked());
  assert.equal(calls.filter(call => call.action === 'send').length,sendCount);
  const gifPermission = hold('permissions');
  await page.evaluate(bytes => {
    const editor = document.querySelector('#broadcast-message .ql-editor'), bounds = editor.getBoundingClientRect();
    const data = new DataTransfer(); data.items.add(new File([new Uint8Array(bytes)],'fixture.gif',{type:'image/gif'}));
    editor.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:data,clientX:bounds.left+20,clientY:bounds.top+20}));
  },[...gif]);
  await gifPermission.arrived;
  const gifUpload = hold('images'); gifPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}});
  await gifUpload.arrived; assert.equal(calls.at(-1).body.type,'image/gif');
  gifUpload.release({json:{image:{id:gifId,mime_type:'image/gif'}}});
  await page.locator(`#broadcast-message img[data-asset-id="${gifId}"]`).waitFor();
  await page.evaluate(id => window.Quill.find(document.querySelector('#broadcast-message .ql-container')).setContents([{insert:{image:id}},{insert:'\n'}]),gifId);
  assert(!(await page.locator('#broadcast-preview').isDisabled()));
  const imageOnlyPermission = hold('permissions'), imageOnlyPrepare = hold('prepare');
  await page.locator('#broadcast-preview').click(); await imageOnlyPermission.arrived;
  imageOnlyPermission.release({json:{status:'ready',resource_authorized:true,is_self:true}}); await imageOnlyPrepare.arrived;
  const imageOnly = calls.at(-1).body.message;
  assert.equal(imageOnly.ops.filter(op => typeof op.insert !== 'string').length,1);
  assert(!imageOnly.ops.some(op => typeof op.insert === 'string' && op.insert.trim()));
  imageOnlyPrepare.release({json:{...richPreview,message:imageOnly,preview_id:'image-only-preview'}});
  await page.locator('#broadcast-confirm').waitFor({state:'visible'});
  assert.equal(await page.locator('#broadcast-confirm-message img').getAttribute('src'),`/creators/broadcast/images/${gifId}`);
  assert.equal(calls.filter(call => call.action === 'send').length,sendCount);
  await page.locator('#broadcast-back').click();
  assert.deepEqual(errors, []);
});

test("admin source selection resets filters and contact names open the profile with a separate Lark link", async t => {
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c", `
import json
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app import admin_permissions, admin_reporting
env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
common = dict(authorization={'display_name':'Fixture owner'}, admin_role='owner',
              admin_permissions=['messages','contacts'], permission_pages=admin_permissions.PAGES,
              admin_landing_path='/admin', csrf_token='fixture-csrf')
sources = [dict(id='cm-one',display_name='CM One',content_access_label='可读取'),
           dict(id='cm-two',display_name='CM Two',content_access_label='可读取')]
messages = env.get_template('admin/messages.html').render(**common,active_page='messages',
    users=sources,selected_user=sources[0],selected_user_in_page=True,selected_chat=None,
    user_page={'page':1},conversation_page={'total_count':0},conversations=[],
    conversation_query='previous filter',conversation_type='group',content_page=None,
    clear_conversation_filters_url='/admin/messages?user_id=cm-one')
person = admin_reporting._decorate_contact(dict(open_id='ou_fixture',display_name='Fixture <Creator>',
    category='creator',handles=['fixture.creator'],conversation_count=2,message_count=5,lark_identity_count=1))
contacts = env.get_template('admin/contacts.html').render(**common,active_page='contacts',
    contacts=[person],contact_page={'page':2},category='creator',q='Fixture',
    name_query_param='q=Fixture',category_filters=[],refresh_url='/admin/contacts')
print(json.dumps(dict(messages=messages,contacts=contacts)))
`], {cwd: backend, encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const html = JSON.parse(rendered.stdout);
  const browser = await chromium.launch({headless: true, channel: "chrome"});
  t.after(() => browser.close());
  const errors = [], calls = [];
  const route = async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://localhost") {
      if (process.env.OH_SCREENSHOT_DIR && url.href.endsWith("/tabler.min.css")) return route.continue();
      return route.abort();
    }
    if (url.pathname.startsWith("/static/")) return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : url.pathname.endsWith(".svg") ? "image/svg+xml" : "text/css",
      body: fs.readFileSync(path.join(backend, "app", url.pathname))});
    calls.push({method: route.request().method(), url});
    return route.fulfill({contentType: "text/html", body: url.pathname === "/admin/contacts" ? html.contacts : html.messages});
  };
  const context = await browser.newContext({viewport: {width: 1360, height: 1000}});
  await context.route("**/*", route);
  const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
  await page.goto("http://localhost/admin/messages?chat_id=old&page=3&conversation_query=old");
  assert(await page.getByRole("button", {name: "切换人员", exact: true}).isHidden());
  await page.locator("#message-user").selectOption("cm-two");
  await page.waitForURL(url => url.searchParams.get("user_id") === "cm-two");
  assert.deepEqual([...new URL(page.url()).searchParams.entries()], [["user_page", "1"], ["user_id", "cm-two"]]);
  assert.equal(calls.at(-1).method, "GET");
  if (process.env.OH_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "admin-messages.png"), fullPage: true});
  await page.goto("http://localhost/admin/contacts");
  const person = page.locator("tbody tr").first();
  assert.equal(await person.getByRole("link", {name: "Fixture <Creator>", exact: true}).getAttribute("href"),
    "/admin/contacts/ou_fixture?page=2&category=creator&q=Fixture");
  const external = person.getByRole("link", {name: "在 Lark 中打开与 Fixture <Creator> 的私聊", exact: true});
  assert.equal(await external.getAttribute("target"), "_blank");
  assert.equal(await external.getAttribute("rel"), "noopener noreferrer");
  assert((await external.getAttribute("href")).startsWith("https://applink.larksuite.com/"));
  assert.equal(await person.locator("script").count(), 0);
  if (process.env.OH_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.OH_SCREENSHOT_DIR, "admin-contacts.png"), fullPage: true});
  const noJs = await browser.newContext({javaScriptEnabled: false}); await noJs.route("**/*", route);
  const fallback = await noJs.newPage(); await fallback.goto("http://localhost/admin/messages");
  assert(await fallback.getByRole("button", {name: "切换人员", exact: true}).isVisible());
  await fallback.locator("#message-user").selectOption("cm-two");
  await fallback.getByRole("button", {name: "切换人员", exact: true}).click();
  await fallback.waitForURL(url => url.searchParams.get("user_id") === "cm-two");
  assert.deepEqual(errors, []);
});
