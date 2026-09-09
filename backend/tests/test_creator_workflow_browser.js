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
    {code: "ai_review", label: "AI 推荐待核对", resolvable: true}], evidence: []});
const contacts = [1, 2].map(id => ({contact_id: `user-${id}`, open_id: `open-${id}`, display_name: "Same Name", identity_hint: `稳定身份 ${id}`}));
const rows = items => ({items, summary: {total: items.length, pending: items.filter(i => i.status !== "confirmed").length,
  confirmed: items.filter(i => i.status === "confirmed").length}, total: items.length, page: 1, pages: 1});
async function fixture(t, {readonly = false, owner = false, item = candidate()} = {}) {
  const scope = {responsible_id: "cm-remi", responsible_name: "Remi", readonly, can_write: !readonly,
    can_admin: owner, can_import: !readonly || owner};
  const state = {items: [item], calls: [], holdSave: null};
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
    if (url.pathname.endsWith("/evidence")) return json({conversations: [{chat_id: "chat-1", name: "相关群聊", chat_mode: "group"}],
      messages: url.searchParams.get("chat_id") ? [{message_id: "message-1", sender_name: "Creator", time: "2026-09-09", text: "<script>non executable evidence</script>"}] : [], notice: "仅在当前授权范围内核对。", page: 1, pages: 1});
    if (url.pathname.endsWith("/draft")) {
      const body = request.postDataJSON(); assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      if (state.holdSave) await state.holdSave();
      state.items[0] = {...state.items[0], handle: body.handle, contact: contacts.find(c => c.contact_id === body.contact_id) || null,
        relationship: body.relationship, version: state.items[0].version + 1,
        reasons: state.items[0].reasons.filter(r => !body.resolved_reasons.includes(r.code) && !(r.code === "missing_handle" && body.handle) && !(r.code === "missing_user" && body.contact_id))};
      return json(state.items[0]);
    }
    if (url.pathname.endsWith("/confirm")) { state.items[0] = {...state.items[0], status: "confirmed", reasons: [], version: state.items[0].version + 1}; return json(state.items[0]); }
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
  assert(await row.getByRole("button", {name: "确认并入库", exact: true}).isDisabled());
  const search = row.getByPlaceholder("搜索我的 Lark 用户");
  await search.fill("none"); await row.getByText("未找到可选用户，仍保留为待确认。").waitFor();
  await search.fill("fail"); await row.getByText("搜索失败：搜索暂不可用").waitFor();
  await search.fill("Same"); await row.locator(".cw-search-results button").nth(1).waitFor();
  assert.equal(await row.locator(".cw-user-name").textContent(), "—");
  await row.locator(".cw-search-results button").nth(1).click();
  assert((await row.locator(".cw-user").textContent()).includes("稳定身份 2"));
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  await row.getByPlaceholder("填写 handle").fill("correct.handle");
  await row.getByLabel("这位用户与账号的关系").selectOption("representative");
  await row.getByLabel("已核对：AI 推荐待核对").check();
  await row.getByLabel("我确认由我负责，且所选用户及关系正确").check();
  await row.getByRole("button", {name: "保存草稿", exact: true}).click();
  await page.getByText("草稿已保存，仍在待确认。").first().waitFor();
  assert.equal(state.items[0].status, "pending");
  assert.equal(state.items[0].contact.contact_id, "user-2");
  await row.getByRole("button", {name: "确认并入库", exact: true}).click();
  await row.getByText("已确认入库", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.url.pathname.endsWith("/confirm")).length, 1);
  assert.deepEqual(errors, []);
});

test("a late draft response preserves edits typed while save is in flight", async t => {
  const {page, state, errors} = await fixture(t);
  let release; state.holdSave = () => new Promise(resolve => { release = resolve; });
  const row = page.locator("#cw-rows tr");
  await row.getByPlaceholder("填写 handle").fill("first.handle");
  await Promise.all([page.waitForRequest(request => request.url().includes("/draft")), row.getByRole("button", {name: "保存草稿", exact: true}).click()]);
  await row.getByPlaceholder("填写 handle").fill("newer.handle");
  release();
  await page.getByText("草稿已保存，仍在待确认。").first().waitFor();
  assert.equal(await row.getByPlaceholder("填写 handle").inputValue(), "newer.handle");
  assert.equal(state.items[0].handle, "first.handle");
  assert.deepEqual(errors, []);
});

test("reviewer sees no writing/search and evidence remains lazy and text-only", async t => {
  const {page, state, errors} = await fixture(t, {readonly: true});
  assert.equal(await page.getByPlaceholder("搜索我的 Lark 用户").count(), 0);
  assert.equal(await page.locator("#cw-import-open, #cw-ai-scan, #cw-confirm-selected").count(), 0);
  assert.equal(await page.locator("#cw-responsible").inputValue(), "cm-remi");
  assert.equal(state.calls.length, 0);
  await page.getByText("查看依据", {exact: true}).click();
  await page.getByRole("button", {name: "查看相关会话记录"}).click();
  await page.getByRole("button", {name: "相关群聊"}).click();
  await page.getByText("<script>non executable evidence</script>", {exact: true}).waitFor();
  assert.equal(state.calls.filter(c => c.method === "POST").length, 0);
  assert.equal(await page.locator("#cw-messages script").count(), 0);
  assert.deepEqual(errors, []);
});

test("late search results cannot replace a newer query and clearing requires selection again", async t => {
  const {page, state, errors} = await fixture(t);
  const row = page.locator("#cw-rows tr"), search = row.getByPlaceholder("搜索我的 Lark 用户");
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
