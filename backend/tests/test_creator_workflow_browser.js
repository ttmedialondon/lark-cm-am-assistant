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
async function fixture(t, {readonly = false, owner = false, testEditor = false, actorName = "Tester", item = candidate(), onboarding = null, run = null} = {}) {
  const scope = {responsible_id: "cm-remi", responsible_name: "Remi", readonly, can_write: !readonly,
    can_admin: owner, can_import: (!readonly && !testEditor) || owner,
    can_scan: (!readonly && !testEditor) || owner, test_editor: testEditor};
  const state = {items: [item], calls: [], holdSave: null, holdConfirm: null, onboarding, run};
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
      assert(state.items[0].contact?.contact_id);
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
    if (url.pathname.endsWith("/ai/preflight")) {
      assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      assert.equal(typeof request.postDataJSON().request_id, "string");
      state.run = {id: "run-1", preflight_id: "run-1", targeted: true, preview_only: true, status: "ready",
        total_people: 3, message_count: 20, char_count: 2000, request_count: 0, request_limit: 6,
        counts: {queued: 3}, outcomes: {}, dispatch: {max_concurrency: 3, running_count: 0, status: "ready"}};
      return json(state.run);
    }
    if (url.pathname.endsWith("/ai/preflight/run-1")) {
      return json({...state.run, local_pending: [{display_name: "Local only user", reason: "只有同群发言，尚不能确定业务关系"}], people: [{name: "Preview user", message_count: 2, chat_count: 1,
        messages: [{ref: "M1", user: "U1", text: "Please send a sample. [email hidden]"},
          {ref: "M2", user: "CM1", text: "<script>not executable</script> I can arrange the sample."}]}]});
    }
    if (url.pathname.endsWith("/ai/scan")) {
      assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      assert.equal(request.postDataJSON().preflight_id, "run-1");
      state.run = {...state.run, status: "running", counts: {running: 3}, request_count: 3,
        dispatch: {...state.run.dispatch, running_count: 3}};
      return json(state.run);
    }
    if (url.pathname.endsWith("/ai/pause")) {
      assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      state.run = {...state.run, status: "cancelled", retryable_people: 3, counts: {cancelled: 3}, dispatch: {...state.run.dispatch, running_count: 0}};
      return json(state.run);
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
  assert(!(await confirm.isDisabled()));
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

test("known user confirms without handle or type and only missing details can be added later", async t => {
  const item = {...candidate(), sources: ["ai"], handle: null, contact: contacts[0], creator_type: null};
  const {page, state, errors} = await fixture(t, {item});
  const row = page.locator("#cw-rows tr"), type = row.getByLabel("达人类型", {exact: true});
  const confirm = row.getByRole("button", {name: "确认入库", exact: true});
  assert.equal(await type.inputValue(), "");
  assert.equal(await type.locator("option:checked").textContent(), "—（未确定）");
  assert(!(await confirm.isDisabled()));
  await confirm.click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.items[0].creator_type, null);
  assert.equal(state.items[0].handle, null);
  assert.equal(await row.locator(".cw-handle").textContent(), "—");
  for (const name of ["更换用户", "清除", "确认入库"]) assert.equal(await row.getByRole("button", {name, exact: true}).count(), 0);
  assert(await row.getByRole("checkbox").isDisabled());
  await row.getByRole("button", {name: "填写 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("later.handle");
  await type.selectOption("non_cap");
  await row.getByRole("button", {name: "保存补充", exact: true}).click();
  await page.getByText("补充资料已保存，正式归属未变。", {exact: true}).first().waitFor();
  assert.equal(state.items[0].handle, "later.handle");
  assert.equal(state.items[0].contact.contact_id, "user-1");
  assert.equal(state.items[0].status, "confirmed");
  assert.equal(state.items[0].creator_type, "non_cap");
  assert.equal(await row.locator(".cw-type").textContent(), "非 CAP");
  assert.equal(await row.getByRole("button", {name: "修改 handle", exact: true}).count(), 0);
  assert.equal(await row.getByLabel("达人类型", {exact: true}).count(), 0);
  assert.deepEqual(state.calls.filter(c => c.method === "POST").map(c => c.url.pathname),
    ["/dashboard/creators/candidate-1/confirm", "/dashboard/creators/candidate-1/draft"]);
  assert.deepEqual(errors, []);
});

test("bulk confirmation requires a selected user, not handle or creator type", async t => {
  for (const missing of [null, "user"]) {
    const complete = missing === null;
    await t.test(complete ? "complete record confirms" : `missing ${missing} stays pending`, async sub => {
      const item = {...candidate(), handle: null, contact: missing === "user" ? null : contacts[0], creator_type: null};
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
  await page.getByRole("button", {name: "准备核对", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("准备完成 · 尚未发送", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/ai/scan")).length, 0);
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 0);
  assert.deepEqual(errors, []);
});

test("targeted preflight shows bounded excerpts before starting and pause never resumes legacy history", async t => {
  const {page, state, errors} = await fixture(t, {run: {status: "cancelled", targeted: false}});
  await page.locator("#cw-initial-status").getByText("旧扫描已停用", {exact: true}).waitFor();
  assert.equal(await page.locator("#cw-ai-retry, #cw-initial-retry").count(), 0);
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  await page.getByRole("button", {name: "准备核对", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("准备完成 · 尚未发送", {exact: true}).waitFor();
  assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/ai/scan")).length, 0);
  assert(await page.getByRole("button", {name: "取消本批准备", exact: true}).isVisible());
  assert((await page.locator("#cw-initial-counts").textContent()).includes("本批 3 人"));
  assert((await page.locator("#cw-initial-counts").textContent()).includes("0 / 6 次"));
  await page.getByRole("button", {name: "查看本批送审内容", exact: true}).click();
  const preview = page.locator(".cw-person-preview").filter({has: page.locator("summary", {hasText: "Preview user"})});
  await preview.locator("summary").getByText(/Preview user/).waitFor();
  await preview.locator("summary").click();
  assert(await preview.getByText("Please send a sample. [email hidden]", {exact: true}).isVisible());
  assert.equal(await preview.locator("script").count(), 0);
  assert((await preview.textContent()).includes("<script>not executable</script>"));
  await page.getByText("仅有本地线索（未发送 AI） · 1 人", {exact: true}).click();
  assert(await page.getByText("Local only user：只有同群发言，尚不能确定业务关系", {exact: true}).isVisible());
  await page.getByRole("button", {name: "开始 AI 核对（最多 30 人）", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("核对中", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "准备核对", exact: true}).isDisabled());
  assert((await page.locator("#cw-scan-dispatch").textContent()).includes("本批正在核对 3 人"));
  assert((await page.locator("#cw-initial-limit").textContent()).includes("本批不自动入库"));
  assert.equal(await page.locator("#cw-batch-progress").getAttribute("max"), "3");
  state.run = {...state.run, counts: {succeeded: 1, invalid: 1, running: 1}, outcomes: {uncertain: 1},
    error_summary: [{status: "invalid", count: 1, message: "候选未通过证据校验。"}]};
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  await page.getByText("证据未通过 · 1 人：候选未通过证据校验。", {exact: true}).waitFor();
  assert((await page.locator("#cw-scan-outcomes").textContent()).includes("信息不足 1 人"));
  await page.getByRole("button", {name: "暂停", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("已停止", {exact: true}).waitFor();
  assert(!(await page.getByRole("button", {name: "暂停", exact: true}).isVisible()));
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  assert.equal(await page.locator("#cw-initial-status").textContent(), "已停止");
  assert.deepEqual(state.calls.filter(call => call.method !== "GET").map(call => call.url.pathname),
    ["/dashboard/creators/ai/preflight", "/dashboard/creators/ai/scan", "/dashboard/creators/ai/pause"]);
  await page.getByRole("button", {name: "继续未完成（限本批预算）", exact: true}).click();
  await page.locator("#cw-initial-status").getByText("核对中", {exact: true}).waitFor();
  const resumed = state.calls.filter(call => call.url.pathname.endsWith("/ai/scan"));
  assert.equal(resumed.length, 2);
  assert.equal(JSON.parse(resumed[1].body).retry_failed, true);
  assert.equal(JSON.parse(resumed[1].body).preflight_id, "run-1");
  state.run = {...state.run, status: "failed", retryable_people: 0};
  await page.getByRole("button", {name: "刷新进度", exact: true}).click();
  assert(!(await page.locator("#cw-ai-start").isVisible()));
  assert.equal(state.items[0].status, "pending");
  assert.deepEqual(errors, []);
});

test("progress refresh is single-flight and only refreshes roster changes without discarding drafts", async t => {
  const onboarding = {status: "scanning", total_chats: 1193, checked_chats: 100, queued_chats: 4};
  const run = {id: "run-1", targeted: true, status: "running", total_people: 4, candidate_count: 1, counts: {succeeded: 1, queued: 2, running: 1}};
  const {page, state, errors} = await fixture(t, {onboarding, run, item: {...candidate(), handle: "existing", contact: contacts[0]}});
  const refresh = page.getByRole("button", {name: "刷新进度", exact: true});
  await page.locator("#cw-initial-updated").getByText(/上次更新/).waitFor();
  let release; state.holdStatus = () => new Promise(resolve => { release = resolve; });
  state.run = {...run, counts: {succeeded: 2, queued: 1, running: 1}};
  const before = state.calls.filter(call => call.url.pathname.endsWith("/ai/status")).length;
  await Promise.all([page.waitForRequest(request => request.url().includes("/ai/status")), refresh.click()]);
  assert(await page.getByRole("button", {name: "读取中…", exact: true}).isDisabled());
  await page.locator("#cw-initial-refresh").evaluate(button => { button.click(); button.click(); });
  assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/ai/status")).length, before + 1);
  state.holdStatus = null; release();
  await page.locator("#cw-scan-succeeded").getByText("2", {exact: true}).waitFor();
  assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/data")).length, 0);
  state.run.candidate_count = 2;
  await Promise.all([page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/data")), refresh.click()]);
  const row = page.locator("#cw-rows tr");
  await row.getByRole("button", {name: "修改 handle", exact: true}).click();
  await row.getByPlaceholder("填写 handle").fill("unsaved.handle");
  state.run.candidate_count = 3;
  await refresh.click();
  await page.locator("#cw-list-stale").waitFor({state: "visible"});
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "unsaved.handle");
  assert.equal(state.calls.filter(call => call.url.pathname.endsWith("/data")).length, 1);
  state.statusError = "暂时无法读取进度";
  await refresh.click();
  await page.locator("#cw-initial-status").getByText("进度读取失败", {exact: true}).waitFor();
  assert(!(await refresh.isDisabled()));
  assert.equal(state.calls.filter(call => call.method !== "GET").length, 0);
  assert.deepEqual(errors, []);
});
