/* Browser checks use isolated fixture data; no production requests or new dependency.
   NODE_PATH=<existing playwright runtime>/node_modules node --test backend/tests/test_creator_workflow_browser.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {chromium} = require("playwright");
const backend = path.resolve(__dirname, "..");
const candidate = () => ({id: "candidate-1", version: 1, platform: "tiktok", handle: null, status: "pending",
  sources: ["excel", "ai"], contact: null, relationship: null, reasons: [
    {code: "missing_handle", label: "请补充达人 handle", resolvable: false},
    {code: "missing_user", label: "请选择对应 Lark 用户", resolvable: false},
    {code: "ai_review", label: "AI 推荐待核对", resolvable: true}],
  evidence: [{source_type: "excel", source_data: {filename: "private-source.xlsx", reason: "Private source detail"}}]});
const contacts = [1, 2].map(id => ({contact_id: `user-${id}`, open_id: `open-${id}`, display_name: "Same Name", identity_hint: `稳定身份 ${id}`}));
const rows = items => ({items, summary: {total: items.length, pending: items.filter(i => i.status !== "confirmed").length,
  confirmed: items.filter(i => i.status === "confirmed").length}, total: items.length, page: 1, pages: 1});
async function fixture(t, {readonly = false, owner = false, testEditor = false, item = candidate()} = {}) {
  const scope = {responsible_id: "cm-remi", responsible_name: "Remi", readonly, can_write: !readonly,
    can_admin: owner, can_import: (!readonly && !testEditor) || owner,
    can_scan: (!readonly && !testEditor) || owner, test_editor: testEditor};
  const state = {items: [item], calls: [], holdSave: null, holdRecheck: null, holdConfirm: null,
    recheckItems: [{...contacts[0], match_type: "handle", match_label: "账号相似"}]};
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
    "import json,sys; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); print(Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('creator_workflow.html').render(**data))"],
    {cwd: backend, input: JSON.stringify({scope, authorization: {display_name: "Tester"}, csrf_token: "test-csrf", creator_data: rows(state.items), responsibles: [{id: "cm-other", display_name: "Other CM"}]}), encoding: "utf8"});
  assert.equal(rendered.status, 0, rendered.stderr);
  const browser = await chromium.launch({headless: true, channel: "chrome"}); t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});
  page.setDefaultTimeout(5000);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => dialog.accept());
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://localhost") {
      if (process.env.CW_SCREENSHOT_DIR && url.href === "https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/css/tabler.min.css") return route.continue();
      return route.abort();
    }
    if (url.pathname === "/dashboard/creators") return route.fulfill({contentType: "text/html", body: rendered.stdout});
    if (url.pathname.startsWith("/static/")) return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css", body: fs.readFileSync(path.join(backend, "app", url.pathname))});
    state.calls.push({url, method: request.method(), body: request.postData(), headers: request.headers()});
    assert.equal(url.searchParams.get("responsible_id"), "cm-remi");
    const json = body => route.fulfill({json: body});
    if (url.pathname.endsWith("/data")) return json(rows(state.items));
    if (url.pathname.endsWith("/users")) {
      if (url.searchParams.get("q") === "fail") return route.fulfill({status: 500, json: {detail: "搜索暂不可用"}});
      if (url.searchParams.get("q") === "slow") await new Promise(resolve => { state.releaseSearch = resolve; });
      return json({items: url.searchParams.get("q") === "none" ? [] : contacts});
    }
    if (url.pathname.endsWith("/draft")) {
      const body = request.postDataJSON(); assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      if (state.holdSave) await state.holdSave();
      const identityChanged = body.handle !== state.items[0].handle || body.contact_id !== (state.items[0].contact?.contact_id || null)
        || body.relationship !== state.items[0].relationship;
      state.items[0] = {...state.items[0], handle: body.handle, contact: contacts.find(c => c.contact_id === body.contact_id) || null,
        relationship: body.relationship, version: state.items[0].version + 1,
        reasons: state.items[0].reasons.filter(r => !body.resolved_reasons.includes(r.code) && !(r.code === "missing_handle" && body.handle) && !(r.code === "missing_user" && body.contact_id))};
      if (identityChanged) state.items[0].reasons = [...state.items[0].reasons.filter(r => r.code !== "details_changed"),
        {code: "details_changed", label: "资料已修改，请重新核对账号、用户及关系", resolvable: true}];
      return json(state.items[0]);
    }
    if (url.pathname.endsWith("/recheck")) {
      const snapshot = {...state.items[0]};
      assert.equal(url.searchParams.get("version"), String(snapshot.version));
      if (state.holdRecheck) await state.holdRecheck();
      if (state.recheckError) return route.fulfill({status: 500, json: {detail: "重新检查暂不可用"}});
      return json({candidate_id: snapshot.id, version: snapshot.version, handle: snapshot.handle,
        items: state.recheckItems, reasons: snapshot.reasons, notice: "检索结果仅为建议，请核对后选择；不会自动关联或入库。"});
    }
    if (url.pathname.endsWith("/confirm")) {
      if (state.holdConfirm) await state.holdConfirm();
      state.items[0] = {...state.items[0], status: "confirmed", reasons: [], version: state.items[0].version + 1};
      return json(state.items[0]);
    }
    if (url.pathname.endsWith("/assign")) { state.items[0] = {...state.items[0], responsible_id: request.postDataJSON().responsible_id, status: "pending", version: state.items[0].version + 1}; return json(state.items[0]); }
    if (url.pathname.endsWith("/import/preview")) return json({batch_id: "batch-1", rows: [{sheet: "Sheet1", row: 2, handle: null, status: "pending", reasons: ["缺用户"]}], summary: {total: 1, ready: 0, pending: 1, invalid: 0}});
    if (url.pathname.endsWith("/import/batch-1/commit")) return json({imported: 1, formal: 0, pending: 1, skipped: 0, items: [{handle: null, status: "pending", reasons: ["缺用户"]}]});
    if (url.pathname.endsWith("/ai/scan")) return json({id: "run-1", status: "queued"});
    if (url.pathname.endsWith("/ai/status")) return json({run: {id: "run-1", status: "succeeded", message: "候选分析已完成"}});
    throw new Error("Unexpected request " + request.url());
  });
  await page.goto("http://localhost/dashboard/creators");
  await page.locator("#cw-rows tr").waitFor();
  if (process.env.CW_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.CW_SCREENSHOT_DIR, `creator-workflow-${readonly ? owner ? "owner" : "review" : "cm"}.png`), fullPage: true});
  return {page, state, errors};
}

test("real-result selection, missing values and draft/confirm remain distinct", async t => {
  const {page, state, errors} = await fixture(t);
  const row = page.locator("#cw-rows tr");
  assert.equal(await row.locator(".cw-handle").textContent(), "—");
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  const search = row.getByPlaceholder("搜索我的 Lark 用户");
  assert(!(await search.isVisible()));
  assert(!(await row.getByPlaceholder("填写 handle").isVisible()));
  await row.getByRole("button", {name: "选择用户", exact: true}).click();
  await search.fill("none"); await row.getByText("未找到可选用户，仍保留为待确认。").waitFor();
  await search.fill("fail"); await row.getByText("搜索失败：搜索暂不可用").waitFor();
  await search.fill("Same"); await row.locator(".cw-search-results button").nth(1).waitFor();
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  await row.locator(".cw-search-results button").nth(1).click();
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 2"));
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("correct.handle");
  await row.getByLabel("这位用户与账号的关系").selectOption("representative");
  await row.getByLabel("已核对：AI 推荐待核对").check();
  await row.getByLabel("我确认由我负责，且所选用户及关系正确").check();
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/recheck")),
    row.getByRole("button", {name: "保存并重新检查", exact: true}).click(),
  ]);
  await row.getByLabel("已核对：资料已修改，请重新核对账号、用户及关系").waitFor();
  assert.equal(state.items[0].status, "pending");
  assert.equal(state.items[0].contact.contact_id, "user-2");
  assert(!(await row.getByLabel("我确认由我负责，且所选用户及关系正确").isChecked()));
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  for (const checkbox of await row.getByLabel(/^已核对：/).all()) await checkbox.check();
  await row.getByLabel("我确认由我负责，且所选用户及关系正确").check();
  await row.getByRole("button", {name: "确认并入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("a late draft response preserves edits typed while save is in flight", async t => {
  const {page, state, errors} = await fixture(t);
  let release; state.holdSave = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("first.handle");
  await Promise.all([page.waitForRequest(request => request.url().includes("/draft")), row.getByRole("button", {name: "保存并重新检查", exact: true}).click()]);
  await row.getByPlaceholder("填写 handle").fill("newer.handle");
  release();
  await row.getByText("资料已修改，请重新核对账号、用户及关系", {exact: true}).waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "newer.handle");
  assert.equal(state.items[0].handle, "first.handle");
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  assert.equal(await row.getByRole("button", {name: "选择此用户", exact: true}).count(), 0);
  assert.deepEqual(errors, []);
});

test("recheck proposes users without selecting one and supports an explicit choice", async t => {
  const {page, state, errors} = await fixture(t);
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("suggested.handle");
  await row.getByRole("button", {name: "保存并重新检查", exact: true}).click();
  const choose = row.getByRole("button", {name: "选择此用户", exact: true});
  await choose.waitFor();
  if (process.env.CW_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.CW_SCREENSHOT_DIR, "creator-workflow-recheck.png"), fullPage: true});
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  assert.equal(state.items[0].contact, null);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/draft")).length, 1);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  await choose.click();
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 1"));
  assert.equal(await row.getByLabel("这位用户与账号的关系").inputValue(), "");
  assert.equal(state.items[0].contact, null);
  assert.equal(state.calls.filter(c => c.method === "POST").length, 1);
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  assert.deepEqual(errors, []);
});

test("editing a handle or choosing a different user invalidates previous verification", async t => {
  const item = {...candidate(), handle: "old.handle", contact: contacts[0], relationship: "owner",
    reasons: [{code: "ai_review", label: "AI 推荐待核对", resolvable: true}]};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr"), ack = row.getByLabel("我确认由我负责，且所选用户及关系正确");
  const reviewed = row.getByLabel("已核对：AI 推荐待核对");
  await reviewed.check(); await ack.check();
  assert(!(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled()));
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("corrected.handle");
  assert(!(await reviewed.isChecked())); assert(!(await ack.isChecked()));
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  await reviewed.check(); await ack.check();
  await row.getByRole("button", {name: "更换用户", exact: true}).click();
  await row.getByPlaceholder("搜索我的 Lark 用户").fill("Same");
  await row.locator(".cw-search-results button").nth(1).click();
  assert(!(await reviewed.isChecked())); assert(!(await ack.isChecked()));
  assert.equal(await row.getByLabel("这位用户与账号的关系").inputValue(), "");
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 2"));
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  assert.deepEqual(errors, []);
});

test("late recheck suggestions are discarded if the handle changes during retrieval", async t => {
  const {page, state, errors} = await fixture(t);
  let release; state.holdRecheck = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("first.handle");
  await Promise.all([
    page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/recheck")),
    row.getByRole("button", {name: "保存并重新检查", exact: true}).click(),
  ]);
  await row.getByPlaceholder("填写 handle").fill("newer.handle");
  release();
  await page.getByText(/你刚修改的内容仍未保存/).first().waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "newer.handle");
  assert.equal(state.items[0].handle, "first.handle");
  assert.equal(await row.getByRole("button", {name: "选择此用户", exact: true}).count(), 0);
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  assert.deepEqual(errors, []);
});

test("a failed recheck keeps saved details pending and can be retried without automatic association", async t => {
  const {page, state, errors} = await fixture(t);
  state.recheckError = true;
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("saved.handle");
  await row.getByRole("button", {name: "保存并重新检查", exact: true}).click();
  await row.getByText(/重新检查暂不可用/).waitFor();
  assert.equal(state.items[0].handle, "saved.handle");
  assert.equal(state.items[0].status, "pending");
  assert.equal(state.items[0].contact, null);
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  state.recheckError = false; state.recheckItems = [];
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/recheck") && response.status() === 200),
    row.getByRole("button", {name: "保存并重新检查", exact: true}).click(),
  ]);
  await row.getByRole("button", {name: "选择用户", exact: true}).click();
  await row.getByPlaceholder("搜索我的 Lark 用户").fill("Same");
  await row.locator(".cw-search-results button").first().waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});

test("confirm locks identity editing and suggestions even after the list refreshes", async t => {
  const item = {...candidate(), handle: "checked.handle", contact: contacts[0], relationship: "owner", reasons: []};
  const {page, state, errors} = await fixture(t, {item});
  state.recheckItems = [{...contacts[1], match_type: "handle", match_label: "账号相似"}];
  let release; state.holdConfirm = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "保存并重新检查", exact: true}).click();
  await row.getByRole("button", {name: "选择此用户", exact: true}).waitFor();
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByRole("button", {name: "更换用户", exact: true}).click();
  await Promise.all([
    page.waitForRequest(request => request.url().includes("q=slow")),
    row.getByPlaceholder("搜索我的 Lark 用户").fill("slow"),
  ]);
  await row.getByLabel("我确认由我负责，且所选用户及关系正确").check();
  await Promise.all([
    page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/confirm")),
    row.getByRole("button", {name: "确认并入库", exact: true}).click(),
  ]);
  async function assertLocked() {
    for (const name of ["选择此用户", "更换用户", "清除", "修改 handle", "重新载入此行"]) {
      assert(await row.getByRole("button", {name, exact: true}).isDisabled(), `${name} must stay disabled during confirmation`);
    }
    for (const control of await row.locator("input,select,button").all()) assert(await control.isDisabled());
    await row.getByRole("button", {name: "选择此用户", exact: true}).evaluate(button => button.click());
    assert((await row.locator(".cw-user").textContent()).includes("稳定身份 1"));
  }
  state.releaseSearch();
  await row.locator(".cw-search-results button").first().waitFor();
  assert.equal(await row.locator(".cw-search-results button").count(), 2);
  await row.locator(".cw-search-results button").nth(1).evaluate(button => button.click());
  await assertLocked();
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/data")),
    page.getByRole("button", {name: "刷新列表", exact: true}).click(),
  ]);
  await row.getByRole("button", {name: "选择此用户", exact: true}).waitFor();
  await assertLocked();
  release();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].contact.contact_id, "user-1");
  assert.equal(state.items[0].handle, "checked.handle");
  assert.equal(state.items[0].relationship, "owner");
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/draft")).length, 1);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("reviewer sees no writing, search or evidence-view controls", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true});
  assert.equal(await page.getByPlaceholder("搜索我的 Lark 用户").count(), 0);
  for (const name of ["选择用户", "更换用户", "填写 handle", "修改 handle", "保存并重新检查", "选择此用户"]) {
    assert.equal(await page.getByRole("button", {name, exact: true}).count(), 0);
  }
  assert.equal(await page.locator("#cw-import-open, #cw-ai-scan, #cw-confirm-selected").count(), 0);
  assert.equal(await page.locator("#cw-responsible").inputValue(), "cm-remi");
  assert.equal(state.calls.length, 0);
  assert.equal(await page.getByText("查看依据", {exact: true}).count(), 0);
  assert.equal(await page.getByRole("button", {name: "查看相关会话记录"}).count(), 0);
  assert.equal(await page.locator("#cw-evidence-dialog, #cw-conversations, #cw-messages, .cw-source-details").count(), 0);
  assert.equal(await page.getByText("private-source.xlsx", {exact: false}).count(), 0);
  assert.equal(await page.getByText("Private source detail", {exact: false}).count(), 0);
  assert.deepEqual(errors, []);
});

test("scoped tester can correct and confirm Remi records without AI or Excel controls", async t => {
  const item = {...candidate(), responsible_id: "cm-remi", handle: "wrong.handle", contact: contacts[0],
    relationship: "owner", reasons: []};
  const {page, state, errors} = await fixture(t, {testEditor: true, item});
  const row = page.locator("#cw-rows tr");
  await page.getByText("测试编辑：你正在操作 Remi 的真实名单。修改会保存，确认后仍归 Remi，系统记录实际操作人。", {exact: true}).waitFor();
  assert.equal(await page.locator("#cw-import-open, #cw-import-dialog, #cw-ai-scan, #cw-ai-retry").count(), 0);
  assert.equal(await row.getByLabel("我确认由我负责，且所选用户及关系正确").count(), 0);
  assert.equal(await row.getByRole("button", {name: "不是我负责", exact: true}).count(), 0);
  assert.equal(await row.getByRole("button", {name: "不由 Remi 负责", exact: true}).count(), 1);
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("corrected.handle");
  await row.getByRole("button", {name: "更换用户", exact: true}).click();
  await row.getByPlaceholder("搜索 Remi 的 Lark 用户").fill("Same");
  await row.locator(".cw-search-results button").nth(1).click();
  await row.getByLabel("这位用户与账号的关系").selectOption("owner");
  await row.getByRole("button", {name: "保存并重新检查", exact: true}).click();
  await row.getByLabel("已核对：资料已修改，请重新核对账号、用户及关系").check();
  await row.getByLabel("我已核对：仍由 Remi 负责，所选用户及关系正确").check();
  await row.getByRole("button", {name: "确认并入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].handle, "corrected.handle");
  assert.equal(state.items[0].contact.contact_id, "user-2");
  assert.equal(state.items[0].responsible_id, "cm-remi");
  assert.equal(state.calls.filter(call => /\/ai\/|\/import\//.test(call.url.pathname)).length, 0);
  assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("source filter offers AI and Excel and a shared record retains both labels without evidence", async t => {
  const {page, state, errors} = await fixture(t);
  const source = page.locator('select[name="source"]');
  assert.deepEqual(await source.locator("option").evaluateAll(options => options.map(option => [option.value, option.textContent])),
    [["", "全部来源"], ["ai", "AI"], ["excel", "Excel"]]);
  const row = page.locator("#cw-rows tr");
  assert.equal(await row.locator(".cw-pill--ai").textContent(), "AI");
  assert.equal(await row.locator(".cw-pill--excel").textContent(), "Excel");
  assert.equal(await row.locator("details").count(), 0);
  assert.equal(await page.locator("#cw-evidence-dialog, #cw-conversations, #cw-messages").count(), 0);
  for (const value of ["ai", "excel"]) {
    await source.selectOption(value);
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/data") && new URL(response.url()).searchParams.get("source") === value),
      page.locator("#cw-filters").getByRole("button", {name: "查找", exact: true}).click(),
    ]);
  }
  assert.equal(state.calls.filter(c => c.method === "POST" || c.url.pathname.endsWith("/evidence")).length, 0);
  assert.deepEqual(errors, []);
});

test("late search results cannot replace a newer query and clearing requires selection again", async t => {
  const {page, state, errors} = await fixture(t);
  const row = page.locator("#cw-rows tr"), search = row.getByPlaceholder("搜索我的 Lark 用户");
  await row.getByRole("button", {name: "选择用户", exact: true}).click();
  await Promise.all([page.waitForRequest(request => request.url().includes("q=slow")), search.fill("slow")]);
  await search.fill("none"); await row.getByText("未找到可选用户，仍保留为待确认。").waitFor();
  state.releaseSearch();
  await page.waitForTimeout(50);
  assert.equal(await row.locator(".cw-search-results button").count(), 0);
  await search.fill("Same"); await row.locator(".cw-search-results button").first().click();
  await row.getByRole("button", {name: "清除", exact: true}).click();
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  assert(await search.isVisible());
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  assert.deepEqual(errors, []);
});

test("administrator can route an unassigned record but cannot confirm it for that CM", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true, owner: true});
  const row = page.locator("#cw-rows tr");
  await row.getByLabel("选择接收待确认记录的 CM").selectOption("cm-other");
  await row.getByRole("button", {name: "交给 CM 核对"}).click();
  await page.getByText("已交给所选 CM 核对，仍为待确认，没有正式入库。").waitFor();
  assert.equal(state.items[0].responsible_id, "cm-other");
  assert.equal(state.items[0].status, "pending");
  assert.equal(await row.getByRole("button", {name: "确认并入库", exact: true}).count(), 0);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});

test("Excel preflight must precede commit and AI completion never confirms a candidate", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true, owner: true});
  await page.getByRole("button", {name: "导入 Excel", exact: true}).click();
  await page.locator("#cw-import-file").setInputFiles({name: "example.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture only")});
  await page.getByRole("button", {name: "预检文件"}).click();
  await page.getByText("共 1 行 · 可直接入库 0 行 · 待确认 1 行 · 无效 0 行").waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/commit")).length, 0);
  await page.getByRole("button", {name: "确认提交这份预检结果"}).click();
  await page.getByText("已导入 1 行 · 正式入库 0 行 · 待确认 1 行 · 跳过 0 行").waitFor();
  await page.locator("#cw-import-close").click();
  await page.getByRole("button", {name: "查找 AI 候选"}).click();
  await page.getByText("候选分析已完成", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});
