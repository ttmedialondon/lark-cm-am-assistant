/* Browser checks use isolated fixture data; no production requests or new dependency.
   NODE_PATH=<existing playwright runtime>/node_modules node --test backend/tests/test_creator_workflow_browser.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {chromium} = require("playwright");
const backend = path.resolve(__dirname, "..");
const candidate = () => ({id: "candidate-1", version: 1, platform: "tiktok", handle: null, status: "pending", creator_type: "cap",
  sources: ["excel", "ai"], contact: null, relationship: null, reasons: [
    {code: "missing_handle", label: "请补充达人 handle", resolvable: false},
    {code: "missing_user", label: "请选择对应 Lark 用户", resolvable: false},
    {code: "ai_review", label: "AI 推荐待核对", resolvable: true}],
  evidence: [{source_type: "excel", source_data: {filename: "private-source.xlsx", reason: "Private source detail"}}]});
const contacts = [1, 2].map(id => ({contact_id: `user-${id}`, open_id: `open-${id}`, display_name: "Same Name", identity_hint: `稳定身份 ${id}`}));
const rows = items => ({items, summary: {total: items.length, pending: items.filter(i => i.status !== "confirmed").length,
  confirmed: items.filter(i => i.status === "confirmed").length}, total: items.length, page: 1, pages: 1});
async function fixture(t, {readonly = false, owner = false, testEditor = false, actorName = "Tester", item = candidate(), onboarding = null} = {}) {
  const scope = {responsible_id: "cm-remi", responsible_name: "Remi", readonly, can_write: !readonly,
    can_admin: owner, can_import: (!readonly && !testEditor) || owner,
    can_scan: (!readonly && !testEditor) || owner, test_editor: testEditor};
  const state = {items: [item], calls: [], holdSave: null, holdConfirm: null, onboarding, run: null};
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
      assert.deepEqual(Object.keys(body).sort(), ["contact_id", "creator_type", "handle", "request_id", "version"]);
      if (state.holdSave) await state.holdSave();
      if (state.saveError) return route.fulfill({status: state.saveError.status, json: {detail: state.saveError.message}});
      assert.equal(body.version, state.items[0].version);
      const identityChanged = body.handle !== state.items[0].handle || body.contact_id !== (state.items[0].contact?.contact_id || null);
      state.items[0] = {...state.items[0], handle: body.handle, contact: contacts.find(c => c.contact_id === body.contact_id) || null,
        creator_type: body.creator_type,
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
      assert(state.items[0].handle && state.items[0].contact?.contact_id && ["cap", "non_cap"].includes(state.items[0].creator_type));
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
    if (url.pathname.endsWith("/ai/initial-retry")) {
      assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      assert.equal(typeof request.postDataJSON().request_id, "string");
      if (state.initialRetryError) return route.fulfill({status: 409, json: {error: state.initialRetryError}});
      state.onboarding = {...state.onboarding, status: "scanning", error_message: null};
      return json({onboarding: state.onboarding});
    }
    if (url.pathname.endsWith("/ai/scan")) {
      state.run = {id: "run-1", status: "succeeded", message: "候选分析已完成"};
      return json({id: "run-1", status: "queued"});
    }
    if (url.pathname.endsWith("/ai/status")) {
      if (state.statusError) return route.fulfill({status: 500, json: {error: state.statusError}});
      const run = state.run || (state.onboarding ? {status: state.onboarding.status, counts: {}, candidate_count: 0, errors: []} : null);
      const result = run && {...run, onboarding: state.onboarding};
      if (state.holdStatus) await state.holdStatus();
      return json({run: result});
    }
    throw new Error("Unexpected request " + request.url());
  });
  const initialStatus = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/ai/status"));
  await page.goto("http://localhost/dashboard/creators");
  await initialStatus;
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
  await row.getByLabel("达人类型", {exact: true}).selectOption("non_cap");
  release();
  await page.getByText(/你刚修改的内容仍未保存/).first().waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "newer.handle");
  assert.equal(state.items[0].handle, "first.handle");
  assert.equal(await row.getByLabel("达人类型", {exact: true}).inputValue(), "non_cap");
  assert.equal(state.items[0].creator_type, "cap");
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
  await row.getByLabel("达人类型", {exact: true}).evaluate(select => {
    select.value = "non_cap"; select.dispatchEvent(new Event("change", {bubbles: true}));
  });
  assert.equal(await row.getByLabel("达人类型", {exact: true}).inputValue(), "cap");
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

test("same-version refresh updates clean user labels without replacing dirty or selected rows", async t => {
  const item = {...candidate(), handle: "checked.handle", contact: {...contacts[0]}, reasons: []};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr");
  async function refresh(name) {
    state.items[0] = {...state.items[0], contact: {...state.items[0].contact, display_name: name}};
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/data")),
      page.getByRole("button", {name: "刷新列表", exact: true}).click(),
    ]);
  }
  await refresh("Remi（Quanzhen Liang）");
  assert.equal(await row.locator(".cw-user-name").textContent(), "Remi（Quanzhen Liang）");
  await row.locator('input[type="checkbox"]').check();
  await refresh("New Label While Selected");
  assert.equal(await row.locator(".cw-user-name").textContent(), "Remi（Quanzhen Liang）");
  assert(await row.locator('input[type="checkbox"]').isChecked());
  await row.locator('input[type="checkbox"]').uncheck();
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("unsaved.handle");
  await refresh("New Label While Dirty");
  assert.equal(await row.locator(".cw-user-name").textContent(), "Remi（Quanzhen Liang）");
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "unsaved.handle");
  assert.equal(state.items[0].version, 1);
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
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
  assert.deepEqual(state.calls.map(call => [call.method, call.url.pathname]), [["GET", "/dashboard/creators/ai/status"]]);
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
      assert.equal(state.calls.filter(call => /\/ai\/|\/import\//.test(call.url.pathname) && call.method !== "GET").length, 0);
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

test("AI-only unknown type is not guessed as non-CAP and must be chosen before confirmation", async t => {
  const item = {...candidate(), sources: ["ai"], handle: "known.handle", contact: contacts[0], creator_type: null};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr"), type = row.getByLabel("达人类型", {exact: true});
  const confirm = row.getByRole("button", {name: "确认入库", exact: true});
  assert.equal(await type.inputValue(), "");
  assert.equal(await type.locator("option:checked").textContent(), "—（未确定）");
  assert(await confirm.isDisabled());
  await type.selectOption("cap");
  assert(!(await confirm.isDisabled()));
  await type.selectOption("");
  assert(await confirm.isDisabled());
  await row.getByRole("button", {name: "保存草稿", exact: true}).click();
  await page.getByText("草稿已保存，尚未入库。", {exact: true}).first().waitFor();
  assert.equal(state.items[0].creator_type, null);
  assert.equal(state.items[0].status, "pending");
  assert(await confirm.isDisabled());
  await type.selectOption("non_cap");
  await confirm.click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].creator_type, "non_cap");
  assert.equal(await row.locator(".cw-type").textContent(), "非 CAP");
  assert.deepEqual(state.calls.filter(c => c.method === "POST").map(c => c.url.pathname),
    ["/dashboard/creators/candidate-1/draft", "/dashboard/creators/candidate-1/draft", "/dashboard/creators/candidate-1/confirm"]);
  assert.deepEqual(errors, []);
});

test("read-only unknown type renders a dash and all type filters remain read-only scoped requests", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true, item: {...candidate(), creator_type: null, sources: ["ai"]}});
  assert.equal(await page.locator("#cw-rows .cw-type").textContent(), "—");
  assert.equal(await page.locator("#cw-rows .cw-type select").count(), 0);
  const filter = page.locator('select[name="type_filter"]');
  assert.deepEqual(await filter.locator("option").evaluateAll(options => options.map(o => [o.value, o.textContent])),
    [["all", "全部类型"], ["cap", "CAP"], ["non_cap", "非 CAP"], ["unknown", "未确定"]]);
  for (const value of ["cap", "non_cap", "unknown", "all"]) {
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/data") && new URL(response.url()).searchParams.get("type_filter") === value),
      filter.selectOption(value),
    ]);
  }
  assert.equal(state.calls.filter(c => c.method !== "GET").length, 0);
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

test("bulk confirmation requires handle, selected user and known creator type", async t => {
  for (const missing of [null, "user", "type"]) {
    const complete = missing === null;
    await t.test(complete ? "complete record confirms" : `missing ${missing} stays pending`, async sub => {
      const item = {...candidate(), handle: "bulk.handle", contact: missing === "user" ? null : contacts[0],
        creator_type: missing === "type" ? null : "cap"};
      const {page, state, errors} = await fixture(sub, {item});
      const row = page.locator("#cw-rows tr");
      await row.getByRole("checkbox", {name: /用于批量确认/}).check();
      await page.locator("#cw-confirm-selected").click();
      await page.locator("#cw-batch-results div").waitFor();
      if (complete) await row.getByText("已确认入库", {exact: true}).waitFor();
      else await page.locator("#cw-batch-results").getByText(missing === "user" ? /对应用户/ : /达人类型/).waitFor();
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

test("CAP Excel preflight precedes commit and the browser does not confirm AI candidates itself", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true, owner: true});
  await page.getByRole("button", {name: "导入 CAP Excel", exact: true}).click();
  assert(await page.getByText("Remi 的 CAP 名单", {exact: true}).isVisible());
  assert(await page.getByText(/Excel 只需要 handle 列，不用填写 CM/).isVisible());
  await page.locator("#cw-import-file").setInputFiles({name: "example.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture only")});
  await page.getByRole("button", {name: "预检文件"}).click();
  await page.getByText("共 1 行 · 可提交 1 行 · 无效 0 行；提交时重新核验 AI 匹配，确定是否自动入库。").waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/commit")).length, 0);
  await page.getByRole("button", {name: "确认提交这份预检结果"}).click();
  await page.getByText("已导入 1 行 · 正式入库 0 行 · 待确认 1 行 · 跳过 0 行").waitFor();
  await page.locator("#cw-import-close").click();
  await page.getByRole("button", {name: "查找 AI 候选"}).click();
  await page.getByText("候选分析已完成", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});

test("first scan shows waiting reasons, checked-not-completed counts and input limits without starting AI", async t => {
  const onboarding = {status: "waiting_sync", total_chats: 0, checked_chats: 0, queued_chats: 0, truncated_chats: 0,
    error_message: "等待首次会话发现与历史消息同步完成；尚未调用 AI。", history_days: 90, message_limit: 30, message_char_limit: 1500};
  const {page, state, errors} = await fixture(t, {onboarding});
  await page.locator("#cw-initial-message").getByText(onboarding.error_message, {exact: true}).waitFor();
  assert.equal(await page.locator("#cw-initial-status").textContent(), "等待准备");
  assert(await page.locator("#cw-ai-scan").isDisabled());
  assert(!(await page.locator("#cw-initial-retry").isVisible()));
  state.onboarding = {...onboarding, status: "scanning", total_chats: 120, checked_chats: 100, queued_chats: 3, truncated_chats: 2, error_message: null};
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.locator("#cw-initial-counts").getByText("已检查会话 100 / 120 · 累计排队 3 个会话", {exact: true}).waitFor();
  assert((await page.locator("#cw-initial-message").textContent()).includes("会话检查完不等于 AI 已分析完"));
  assert((await page.locator("#cw-initial-limit").textContent()).includes("每个会话最多 30 条消息、每条 1500 字符"));
  assert((await page.locator("#cw-initial-limit").textContent()).includes("2 个会话截断"));
  state.onboarding = {...state.onboarding, status: "succeeded", checked_chats: 120};
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("已完成", {exact: true}).waitFor();
  assert(!(await page.locator("#cw-ai-scan").isDisabled()));
  assert.equal(state.items[0].status, "pending");
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  assert.deepEqual(errors, []);
});

test("failed initial scan retries only on explicit authorized click and rejected consent changes remain stopped", async t => {
  const onboarding = {status: "failed", total_chats: 120, checked_chats: 100, queued_chats: 3, truncated_chats: 1,
    error_message: "本批有失败，请手动重试。"};
  const {page, state, errors} = await fixture(t, {onboarding});
  const retry = page.getByRole("button", {name: "重试首次筛选", exact: true});
  await retry.waitFor();
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  state.initialRetryError = "正文授权已变化，旧首次筛选不可重放；请使用当前范围的手动扫描。";
  await retry.click();
  await page.locator("#cw-initial-message").getByText(state.initialRetryError, {exact: true}).waitFor();
  assert.equal(state.onboarding.status, "failed");
  assert(!(await retry.isDisabled()));
  state.initialRetryError = null;
  await retry.click();
  await page.locator("#cw-initial-status").getByText("筛选中", {exact: true}).waitFor();
  assert(!(await retry.isVisible()));
  assert.deepEqual(state.calls.filter(call => call.method !== "GET").map(call => call.url.pathname),
    ["/dashboard/creators/ai/initial-retry", "/dashboard/creators/ai/initial-retry"]);
  assert.equal(state.items[0].status, "pending");
  assert.deepEqual(errors, []);
});

test("test editor can read initial failure but cannot trigger retry or scan", async t => {
  const onboarding = {status: "cancelled", total_chats: 10, checked_chats: 5, queued_chats: 2, error_message: "Lark 授权已失效；首次筛选已停止。"};
  const {page, state, errors} = await fixture(t, {testEditor: true, onboarding});
  await page.locator("#cw-initial-status").getByText("已停止", {exact: true}).waitFor();
  assert.equal(await page.locator("#cw-initial-retry, #cw-ai-retry, #cw-ai-scan").count(), 0);
  state.statusError = "状态服务暂不可用";
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("进度读取失败", {exact: true}).waitFor();
  assert((await page.locator("#cw-initial-message").textContent()).includes("不会重新启动分析"));
  state.statusError = null;
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("已停止", {exact: true}).waitFor();
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  assert.deepEqual(errors, []);
});

test("late initial progress cannot overwrite a newer successful refresh", async t => {
  const onboarding = {status: "waiting_sync", total_chats: 1, checked_chats: 0, queued_chats: 0};
  const {page, state, errors} = await fixture(t, {onboarding});
  await page.locator("#cw-initial-status").getByText("等待准备", {exact: true}).waitFor();
  let release;
  state.holdStatus = () => new Promise(resolve => { release = resolve; });
  await Promise.all([
    page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/ai/status")),
    page.getByRole("button", {name: "刷新进度", exact: true}).click(),
  ]);
  state.holdStatus = null;
  state.onboarding = {...onboarding, status: "succeeded", checked_chats: 1};
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("已完成", {exact: true}).waitFor();
  release();
  await page.waitForTimeout(75);
  assert.equal(await page.locator("#cw-initial-status").textContent(), "已完成");
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  assert.deepEqual(errors, []);
});
