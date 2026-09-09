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
async function fixture(t, {readonly = false, owner = false, testEditor = false, actorName = "Tester", item = candidate()} = {}) {
  const scope = {responsible_id: "cm-remi", responsible_name: "Remi", readonly, can_write: !readonly,
    can_admin: owner, can_import: (!readonly && !testEditor) || owner,
    can_scan: (!readonly && !testEditor) || owner, test_editor: testEditor};
  const state = {items: [item], calls: [], holdSave: null, holdConfirm: null};
  const rendered = spawnSync(path.join(backend, ".venv/bin/python"), ["-c",
    "import json,sys; from jinja2 import Environment,FileSystemLoader,select_autoescape; data=json.load(sys.stdin); print(Environment(loader=FileSystemLoader('app/templates'),autoescape=select_autoescape()).get_template('creator_workflow.html').render(**data))"],
    {cwd: backend, input: JSON.stringify({scope, authorization: {display_name: actorName}, csrf_token: "test-csrf", creator_data: rows(state.items), responsibles: [{id: "cm-other", display_name: "Other CM"}]}), encoding: "utf8"});
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
      assert.deepEqual(Object.keys(body).sort(), ["contact_id", "handle", "request_id", "version"]);
      if (state.holdSave) await state.holdSave();
      if (state.saveError) return route.fulfill({status: state.saveError.status, json: {detail: state.saveError.message}});
      assert.equal(body.version, state.items[0].version);
      const identityChanged = body.handle !== state.items[0].handle || body.contact_id !== (state.items[0].contact?.contact_id || null);
      state.items[0] = {...state.items[0], handle: body.handle, contact: contacts.find(c => c.contact_id === body.contact_id) || null,
        version: state.items[0].version + 1,
        reasons: state.items[0].reasons.filter(r => !(r.code === "missing_handle" && body.handle) && !(r.code === "missing_user" && body.contact_id))};
      if (identityChanged) state.items[0].reasons = [...state.items[0].reasons.filter(r => r.code !== "details_changed"),
        {code: "details_changed", label: "资料已修改，请重新核对账号、用户及关系", resolvable: true}];
      return json(state.items[0]);
    }
    if (url.pathname.endsWith("/confirm")) {
      const body = request.postDataJSON(); assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      assert.equal(body.version, state.items[0].version);
      if (state.holdConfirm) await state.holdConfirm();
      if (state.confirmError) return route.fulfill({status: state.confirmError.status, json: {detail: state.confirmError.message}});
      assert(state.items[0].handle && state.items[0].contact?.contact_id);
      state.items[0] = {...state.items[0], status: "confirmed", reasons: [], version: state.items[0].version + 1};
      return json(state.items[0]);
    }
    if (url.pathname.endsWith("/decision")) {
      const body = request.postDataJSON(); assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      state.items[0] = {...state.items[0], status: body.action === "reject" ? "rejected" : "deferred", version: state.items[0].version + 1};
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

test("missing values require a real user selection and complete edits confirm in one action", async t => {
  const {page, state, errors} = await fixture(t);
  const row = page.locator("#cw-rows tr");
  assert.equal(await row.locator(".cw-handle").textContent(), "—");
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  const confirm = row.getByRole("button", {name: "确认入库", exact: true});
  assert(await confirm.isDisabled());
  const search = row.getByPlaceholder("搜索我的 Lark 用户");
  assert(!(await search.isVisible()));
  assert(!(await row.getByPlaceholder("填写 handle").isVisible()));
  await row.getByRole("button", {name: "选择用户", exact: true}).click();
  await search.fill("none"); await row.getByText("未找到可选用户，仍保留为待确认。").waitFor();
  await search.fill("fail"); await row.getByText("搜索失败：搜索暂不可用").waitFor();
  await search.fill("Same"); await row.locator(".cw-search-results button").nth(1).waitFor();
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  assert(await confirm.isDisabled());
  await row.locator(".cw-search-results button").nth(1).click();
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 2"));
  assert(await confirm.isDisabled());
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("correct.handle");
  assert(!(await confirm.isDisabled()));
  assert.equal(await row.getByLabel("这位用户与账号的关系").count(), 0);
  assert.equal(await row.locator("[data-review], [data-acknowledgement]").count(), 0);
  await confirm.click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].contact.contact_id, "user-2");
  assert.equal(state.items[0].handle, "correct.handle");
  assert.deepEqual(state.calls.filter(c => c.method === "POST").map(c => c.url.pathname),
    ["/dashboard/creators/candidate-1/draft", "/dashboard/creators/candidate-1/confirm"]);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/recheck")).length, 0);
  assert.deepEqual(errors, []);
});

test("a late draft response preserves edits typed while save is in flight", async t => {
  const {page, state, errors} = await fixture(t);
  let release; state.holdSave = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("first.handle");
  await Promise.all([page.waitForRequest(request => request.url().includes("/draft")), row.getByRole("button", {name: "保存草稿", exact: true}).click()]);
  await row.getByPlaceholder("填写 handle").fill("newer.handle");
  release();
  await page.getByText(/你刚修改的内容仍未保存/).first().waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "newer.handle");
  assert.equal(state.items[0].handle, "first.handle");
  assert(await row.getByRole("button", {name: "确认入库", exact: true}).isDisabled());
  assert.equal(state.calls.filter(c => /\/(recheck|confirm)$/.test(c.url.pathname)).length, 0);
  assert.deepEqual(errors, []);
});

test("complete records with historical doubts confirm without review fields or a recheck", async t => {
  const item = {...candidate(), handle: "ready.handle", contact: contacts[0], relationship: null,
    reasons: [{code: "ai_review", label: "AI 推荐待核对", resolvable: true},
      {code: "details_changed", label: "资料已修改，请重新核对账号、用户及关系", resolvable: true},
      {code: "missing_relationship", label: "用户关系待核对", resolvable: false}]};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr");
  for (const control of ["[data-relationship]", "[data-review]", "[data-acknowledgement]", ".cw-recheck", "#cw-reason"]) {
    assert.equal(await page.locator(control).count(), 0);
  }
  assert.equal(await row.getByText("AI 推荐待核对", {exact: true}).count(), 0);
  assert.equal(await row.getByRole("button", {name: "保存并重新检查", exact: true}).count(), 0);
  if (process.env.CW_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.CW_SCREENSHOT_DIR, "creator-workflow-simple-confirm.png"), fullPage: true});
  await row.getByRole("button", {name: "确认入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.deepEqual(state.calls.filter(c => c.method === "POST").map(c => c.url.pathname),
    ["/dashboard/creators/candidate-1/confirm"]);
  assert.deepEqual(errors, []);
});

test("correcting a handle and user saves the chosen identity and confirms without extra steps", async t => {
  const item = {...candidate(), handle: "old.handle", contact: contacts[0], relationship: "owner",
    reasons: [{code: "ai_review", label: "AI 推荐待核对", resolvable: true}],
    blocking_reasons: [{code: "ownership_conflict", label: "旧账号已有其他归属"}]};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr");
  assert(await row.getByText("旧账号已有其他归属", {exact: true}).isVisible());
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("corrected.handle");
  assert(!(await row.getByText("旧账号已有其他归属", {exact: true}).isVisible()));
  assert(!(await row.getByRole("button", {name: "确认入库", exact: true}).isDisabled()));
  await row.getByRole("button", {name: "更换用户", exact: true}).click();
  await row.getByPlaceholder("搜索我的 Lark 用户").fill("Same");
  await row.locator(".cw-search-results button").nth(1).click();
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 2"));
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  await row.getByRole("button", {name: "确认入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].handle, "corrected.handle");
  assert.equal(state.items[0].contact.contact_id, "user-2");
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/draft")).length, 1);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("live conflict, version and authorization errors leave complete records pending", async t => {
  for (const [status, message] of [[409, "账号已有其他归属"], [409, "记录版本已变化"], [403, "你已无权修改此名单"]]) {
    await t.test(message, async sub => {
      const item = {...candidate(), handle: "complete.handle", contact: contacts[0]};
      const {page, state, errors} = await fixture(sub, {item});
      state.confirmError = {status, message};
      const row = page.locator("#cw-rows tr");
      await row.getByRole("button", {name: "确认入库", exact: true}).click();
      await row.getByText(message, {exact: true}).waitFor();
      assert.equal(state.items[0].status, "pending");
      assert.equal(state.items[0].contact.contact_id, "user-1");
      assert.equal(await row.getByText("已确认入库", {exact: true}).count(), 0);
      assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
      assert.deepEqual(errors, []);
    });
  }
});

test("a failed save prevents confirmation and retry preserves the same request identity", async t => {
  const item = {...candidate(), handle: "old.handle", contact: contacts[0]};
  const {page, state, errors} = await fixture(t, {item});
  state.saveError = {status: 500, message: "保存暂不可用"};
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("saved.handle");
  await row.getByRole("button", {name: "确认入库", exact: true}).click();
  await row.getByText("保存暂不可用", {exact: true}).waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "saved.handle");
  assert.equal(state.items[0].handle, "old.handle");
  assert.equal(state.items[0].status, "pending");
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  state.saveError = null;
  await row.getByRole("button", {name: "确认入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  const saves = state.calls.filter(c => c.url.pathname.endsWith("/draft"));
  assert.equal(saves.length, 2);
  assert.equal(JSON.parse(saves[0].body).request_id, JSON.parse(saves[1].body).request_id);
  assert.equal(state.items[0].handle, "saved.handle");
  assert.deepEqual(errors, []);
});

test("confirm locks identity editing and late search results even after the list refreshes", async t => {
  const item = {...candidate(), handle: "checked.handle", contact: contacts[0], relationship: "owner", reasons: []};
  const {page, state, errors} = await fixture(t, {item});
  let release; state.holdConfirm = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByRole("button", {name: "更换用户", exact: true}).click();
  await Promise.all([
    page.waitForRequest(request => request.url().includes("q=slow")),
    row.getByPlaceholder("搜索我的 Lark 用户").fill("slow"),
  ]);
  await Promise.all([
    page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/confirm")),
    row.getByRole("button", {name: "确认入库", exact: true}).click(),
  ]);
  async function assertLocked() {
    for (const name of ["更换用户", "清除", "修改 handle", "重新载入此行"]) {
      assert(await row.getByRole("button", {name, exact: true}).isDisabled(), `${name} must stay disabled during confirmation`);
    }
    for (const control of await row.locator("input,select,button").all()) assert(await control.isDisabled());
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
  await assertLocked();
  release();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].contact.contact_id, "user-1");
  assert.equal(state.items[0].handle, "checked.handle");
  assert.equal(state.items[0].relationship, "owner");
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/draft")).length, 0);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("reviewer sees no writing, search or evidence-view controls", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true});
  assert.equal(await page.getByPlaceholder("搜索我的 Lark 用户").count(), 0);
  for (const name of ["选择用户", "更换用户", "填写 handle", "修改 handle", "保存草稿", "确认入库"]) {
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
  for (const actorName of ["Shuxuan Li", "Shang Ma"]) {
    await t.test(actorName, async sub => {
      const item = {...candidate(), responsible_id: "cm-remi", handle: "wrong.handle", contact: contacts[0],
        relationship: "owner", reasons: []};
      const {page, state, errors} = await fixture(sub, {testEditor: true, actorName, item});
      const row = page.locator("#cw-rows tr");
      await page.getByText("测试编辑：你正在操作 Remi 的真实名单。修改会保存，确认后仍归 Remi，系统记录实际操作人。", {exact: true}).waitFor();
      assert.equal(await page.locator("#cw-import-open, #cw-import-dialog, #cw-ai-scan, #cw-ai-retry").count(), 0);
      assert.equal(await row.locator("[data-review], [data-acknowledgement], [data-relationship]").count(), 0);
      assert.equal(await row.getByRole("button", {name: "不是我负责", exact: true}).count(), 0);
      assert.equal(await row.getByRole("button", {name: "不由 Remi 负责", exact: true}).count(), 1);
      await row.getByRole("button", {name: "修改 handle", exact: true}).click();
      await row.getByPlaceholder("填写 handle").fill("corrected.handle");
      await row.getByRole("button", {name: "更换用户", exact: true}).click();
      await row.getByPlaceholder("搜索 Remi 的 Lark 用户").fill("Same");
      await row.locator(".cw-search-results button").nth(1).click();
      await row.getByRole("button", {name: "确认入库", exact: true}).click();
      await row.getByText("已确认入库", {exact: true}).waitFor();
      assert.equal(state.items[0].handle, "corrected.handle");
      assert.equal(state.items[0].contact.contact_id, "user-2");
      assert.equal(state.items[0].responsible_id, "cm-remi");
      assert.equal(state.calls.filter(call => /\/ai\/|\/import\//.test(call.url.pathname)).length, 0);
      assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/confirm")).length, 1);
      assert.deepEqual(errors, []);
    });
  }
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
  assert(await row.getByRole("button", {name: "确认入库", exact: true}).isDisabled());
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
  assert.equal(await row.getByRole("button", {name: "确认入库", exact: true}).count(), 0);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});

test("saving a complete draft remains optional and never enters the formal list", async t => {
  const item = {...candidate(), handle: "old.handle", contact: contacts[0]};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("draft.handle");
  await row.getByRole("button", {name: "保存草稿", exact: true}).click();
  await page.getByText("草稿已保存，尚未入库。", {exact: true}).first().waitFor();
  assert.equal(state.items[0].handle, "draft.handle");
  assert.equal(state.items[0].status, "pending");
  assert(!(await row.getByRole("button", {name: "确认入库", exact: true}).isDisabled()));
  assert.equal(state.calls.filter(c => /\/(recheck|confirm)$/.test(c.url.pathname)).length, 0);
  assert.deepEqual(errors, []);
});

test("bulk confirmation uses the same two-field requirement", async t => {
  for (const complete of [true, false]) {
    await t.test(complete ? "complete record confirms" : "missing identity stays pending", async sub => {
      const item = {...candidate(), handle: "bulk.handle", contact: complete ? contacts[0] : null};
      const {page, state, errors} = await fixture(sub, {item});
      const row = page.locator("#cw-rows tr");
      await row.getByRole("checkbox", {name: /用于批量确认/}).check();
      await page.locator("#cw-confirm-selected").click();
      await page.locator("#cw-batch-results div").waitFor();
      if (complete) await row.getByText("已确认入库", {exact: true}).waitFor();
      else await page.locator("#cw-batch-results").getByText(/对应用户/).waitFor();
      assert.equal(state.items[0].status, complete ? "confirmed" : "pending");
      assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, complete ? 1 : 0);
      assert.deepEqual(errors, []);
    });
  }
});

test("deferring or returning an incomplete record does not require identity completion", async t => {
  for (const [action, name, status] of [["defer", "暂缓", "deferred"], ["reject", "不是我负责", "rejected"]]) {
    await t.test(action, async sub => {
      const {page, state, errors} = await fixture(sub);
      const row = page.locator("#cw-rows tr");
      await row.getByRole("button", {name, exact: true}).click();
      await page.getByText(action === "defer" ? "已暂缓，仍在待确认。" : "已交回管理员核对。", {exact: true}).first().waitFor();
      assert.equal(state.items[0].status, status);
      assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
      assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/decision")).length, 1);
      assert.deepEqual(errors, []);
    });
  }
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
