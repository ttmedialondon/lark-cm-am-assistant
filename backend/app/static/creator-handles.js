"use strict";

(() => {
  const app = document.getElementById("creators-app");
  if (!app) return;
  const el = key => document.getElementById(`creators-${key}`);
  let data = JSON.parse(el("initial").textContent);
  let tab = app.dataset.tab;
  let page = data.page || 1;
  let query = el("query").value.trim();
  let busy = false, reading = false, refreshQueued = false, version = 0;
  const rows = new Map();
  let importPreview = null;
  const rowKey = item => `${item.kind || "person"}:${item.id}`;
  const node = (tag, text = "", classes = "") => {
    const item = document.createElement(tag); item.textContent = text; item.className = classes; return item;
  };
  function notice(text, error = false) {
    el("notice").textContent = text;
    el("notice").className = text ? `alert alert-${error ? "danger" : "success"}` : "alert d-none";
  }
  async function read(response) {
    const body = response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
    if (!response.ok || response.redirected) throw new Error(body.error || "访问失败，请刷新或重新登录。");
    return body;
  }
  function selectedCM() { return el("cm")?.value || data.selected_cm_id || ""; }
  function selectedCMName() { return data.cms?.find(cm => cm.id === selectedCM())?.name || "我"; }
  function clearRows() { for (const row of rows.values()) row.searchVersion++; rows.clear(); el("rows").replaceChildren(); }
  function importMessage(text, error = false) {
    el("import-message").textContent = text;
    el("import-message").className = `small mt-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function makeImportRow(item) {
    const row = node("tr"), handleCell = node("td"), userCell = node("td"), statusCell = node("td");
    handleCell.style.minWidth = "200px"; userCell.style.minWidth = "240px";
    const handle = node("input", "", "form-control"); handle.maxLength = 160;
    handle.setAttribute("aria-label", "达人 handle"); handle.placeholder = "—";
    const creatorType = node("div", "", "small text-secondary mt-1");
    handleCell.append(handle, creatorType);
    const searchLine = node("div", "", "d-flex gap-1");
    const user = node("input", "", "form-control"); user.maxLength = 120; user.placeholder = "— 搜索对应用户";
    user.setAttribute("aria-label", "搜索当前负责人可访问的用户");
    const search = node("button", "搜索", "btn btn-sm btn-outline-secondary"); search.type = "button";
    const choices = node("div", "", "d-flex flex-column gap-1 mt-1"); choices.setAttribute("aria-live", "polite");
    searchLine.append(user, search); userCell.append(searchLine, choices);
    const status = node("div"), reason = node("div", "", "small text-secondary mt-1"); statusCell.append(status, reason);
    const actions = node("td"), actionsList = node("div", "", "d-flex gap-1 flex-wrap");
    const confirm = node("button", "确认入库", "btn btn-sm btn-primary"); confirm.type = "button";
    const defer = node("button", "暂缓", "btn btn-sm btn-outline-secondary"); defer.type = "button";
    actionsList.append(confirm, defer); actions.append(actionsList);
    const source = node("td", "", "text-nowrap"), typeCell = node("td"); typeCell.hidden = true;
    row.append(handleCell, source, userCell, typeCell, statusCell, actions);
    const parts = {row, source, handle, creatorType, user, search, choices, status, reason, confirm, defer, contactId: "", dirty: false, searchVersion: 0};
    const check = () => { confirm.disabled = busy || !parts.canConfirm || !handle.value.trim() || !parts.contactId; };
    handle.addEventListener("input", () => { parts.dirty = true; check(); });
    user.addEventListener("input", () => { parts.dirty = true; parts.contactId = ""; parts.searchVersion++; choices.replaceChildren(); check(); });
    search.addEventListener("click", async () => {
      const token = ++parts.searchVersion, scope = selectedCM(); search.disabled = true;
      choices.textContent = "正在查找…";
      try {
        const matches = await read(await fetch(`/creators/imports/users?${new URLSearchParams({cm_id: scope, q: user.value.trim()})}`, {cache: "no-store"}));
        if (token !== parts.searchVersion || scope !== selectedCM()) return;
        choices.replaceChildren();
        for (const match of matches) {
          const choice = node("button", [match.display_name || "未命名用户", match.context_label].filter(Boolean).join(" · "), "btn btn-sm btn-outline-secondary text-start");
          choice.type = "button";
          choice.addEventListener("click", () => {
            if (busy) return;
            parts.contactId = match.id; user.value = match.display_name || "未命名用户";
            parts.dirty = true; choices.replaceChildren(); check();
          });
          choices.append(choice);
        }
        if (!matches.length) choices.textContent = "没有找到可访问的用户，请换个姓名试试。";
      } catch (error) { if (token === parts.searchVersion) choices.textContent = error.message; }
      finally { search.disabled = busy || !parts.canConfirm; }
    });
    user.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); search.click(); } });
    confirm.addEventListener("click", () => mutate(`/imports/rows/${item.id}/confirm`, {handle: handle.value.trim(), contact_id: parts.contactId}));
    defer.addEventListener("click", () => mutate(`/imports/rows/${item.id}/defer`));
    return parts;
  }
  function renderImports() {
    el("import-open").disabled = busy || !selectedCM();
    for (const key of ["file", "check", "confirm", "close"]) el(`import-${key}`).disabled = busy;
    el("import-confirm").hidden = !importPreview || importPreview.status !== "preview" || !(importPreview.counts?.valid > 0);
    const batches = data.import_batches || [];
    el("import-batches").hidden = !batches.length;
    el("import-batch-list").replaceChildren();
    for (const batch of batches) {
      const line = node("div", "", "d-flex flex-wrap align-items-center gap-2 mb-2");
      const label = node("div", "", "me-auto");
      label.append(node("strong", batch.filename || "Excel 名单"), node("span", ` · CAP · ${batch.status === "preview" ? "待导入" : "已导入"}`, "text-secondary"));
      const counts = batch.counts || {};
      label.append(node("div", batch.status === "preview" ? `有效 handle ${counts.valid || 0}` : `已入库 ${counts.confirmed || 0} · 待确认 ${counts.review || 0}`, "small text-secondary"));
      if (batch.error) label.append(node("div", batch.error, "small text-danger"));
      line.append(label);
      if (batch.can_start) {
        const button = node("button", "确认导入", "btn btn-sm btn-outline-primary");
        button.type = "button"; button.disabled = busy;
        button.addEventListener("click", () => mutate(`/imports/${batch.id}/start`)); line.append(button);
      }
      el("import-batch-list").append(line);
    }
  }
  function renderImportRow(item, parts) {
    if (!parts.dirty) {
      parts.handle.value = item.handle || ""; parts.user.value = item.contact_id ? item.display_name || "" : "";
      parts.contactId = item.contact_id || "";
    }
    parts.canConfirm = !!item.can_confirm;
    parts.creatorType.textContent = "CAP 属性：CAP";
    parts.status.textContent = item.status_label || "待确认"; parts.reason.textContent = item.reason || "";
    parts.handle.disabled = busy || !parts.canConfirm; parts.user.disabled = busy || !parts.canConfirm;
    parts.search.disabled = busy || !parts.canConfirm;
    parts.confirm.hidden = !parts.canConfirm;
    parts.confirm.disabled = busy || !parts.canConfirm || !parts.handle.value.trim() || !parts.contactId;
    parts.defer.hidden = !item.can_defer; parts.defer.disabled = busy;
    for (const choice of parts.choices.querySelectorAll("button")) choice.disabled = busy;
  }
  function makeRow(item) {
    const row = node("tr");
    const person = node("td");
    const personWrap = node("div", "", "d-flex align-items-center gap-2");
    const avatar = node("span", "", "avatar avatar-sm bg-blue-lt rounded-circle flex-shrink-0 position-relative overflow-hidden");
    avatar.setAttribute("aria-hidden", "true");
    const initial = node("span"); avatar.append(initial);
    const name = node("a", "", "fw-bold");
    name.target = "_blank"; name.rel = "noopener noreferrer";
    personWrap.append(avatar, name); person.append(personWrap);
    const handleCell = node("td"); handleCell.style.minWidth = "250px";
    const saved = node("div", "", "mb-2"); handleCell.append(saved);
    const source = node("td", "", "text-nowrap");
    const typeCell = node("td", "CAP"), statusCell = node("td");
    const parts = {row, source, name, avatar, initial, saved, typeCell, statusCell};
    row.append(handleCell, source, person, typeCell, statusCell, node("td", "—", "text-secondary"));
    return parts;
  }
  function render() {
    el("status-column").hidden = tab !== "candidates";
    el("type-column").hidden = tab !== "mine";
    if (data.can_manage) el("cm").disabled = busy;
    for (const key of ["candidates", "mine"]) {
      el(key).className = `btn ${key === tab ? "btn-primary" : "btn-outline-secondary"}`;
      el(key).setAttribute("aria-pressed", String(key === tab));
      el(key).disabled = busy;
    }
    const ids = new Set((data.items || []).map(rowKey));
    for (const [id, parts] of rows) if (!ids.has(id)) { parts.searchVersion++; parts.row.remove(); rows.delete(id); }
    for (const [index, item] of (data.items || []).entries()) {
      const key = rowKey(item);
      if (!rows.has(key)) rows.set(key, item.kind === "import" ? makeImportRow(item) : makeRow(item));
      const parts = rows.get(key);
      if (el("rows").children[index] !== parts.row) el("rows").insertBefore(parts.row, el("rows").children[index] || null);
      parts.source.textContent = item.source || (item.sources || []).join(" + ") || "—";
      if (item.kind === "import") { renderImportRow(item, parts); continue; }
      parts.name.textContent = item.display_name || "—";
      parts.initial.textContent = Array.from((item.display_name || "").trim())[0]?.toUpperCase() || "?";
      if (parts.avatarUrl !== item.avatar_url) {
        parts.avatarUrl = item.avatar_url;
        parts.avatar.querySelector("img")?.remove();
        if (item.avatar_url) {
          const image = node("img", "", "position-absolute top-0 start-0 w-100 h-100");
          image.alt = ""; image.width = 32; image.height = 32;
          image.loading = "lazy"; image.referrerPolicy = "no-referrer";
          image.style.objectFit = "cover"; image.style.visibility = "hidden";
          image.addEventListener("load", () => { image.style.visibility = "visible"; }, {once: true});
          image.addEventListener("error", () => image.remove(), {once: true});
          image.src = item.avatar_url; parts.avatar.append(image);
        }
      }
      if (item.private_chat_url) parts.name.href = item.private_chat_url;
      else parts.name.removeAttribute("href");
      parts.name.title = item.private_chat_url ? "在 Lark 中打开你与此人的私聊" : "缺少有效 Lark 用户，暂不能跳转";
      parts.saved.replaceChildren();
      for (const [index, handle] of (item.handles || []).entries()) {
        if (index) parts.saved.append(document.createTextNode(" · "));
        parts.saved.append(node("span", `@${handle.replace(/^@/, "")}`));
      }
      if (!item.handles?.length) parts.saved.textContent = "—";
      parts.typeCell.hidden = tab !== "mine";
      parts.statusCell.hidden = tab !== "candidates";
    }
    page = data.page || 1;
    el("empty").hidden = !!data.items?.length;
    el("empty").textContent = tab === "mine" ? "还没有已入库的达人。" : "暂无候选达人。";
    el("page-label").textContent = `共 ${data.total_count || 0} ${tab === "mine" ? "人" : "条"} · 第 ${page} / ${Math.max(1, data.total_pages || 0)} 页`;
    el("previous").disabled = busy || page <= 1; el("next").disabled = busy || page >= (data.total_pages || 1);
    renderImports();
  }
  async function refresh() {
    if (reading) {
      refreshQueued = true; version++;
      return;
    }
    const token = ++version; reading = true;
    const params = new URLSearchParams({tab, q: query, page: String(page)});
    if (selectedCM()) params.set("cm_id", selectedCM());
    history.replaceState(null, "", `/creators?${params}`);
    try {
      const next = await read(await fetch(`/creators/data?${params}`, {cache: "no-store"}));
      if (token === version) { data = next; render(); }
    } catch (error) { if (token === version) notice(error.message, true); }
    finally {
      reading = false;
      if (refreshQueued) { refreshQueued = false; void refresh(); }
    }
  }
  async function mutate(path, fields = {}) {
    if (busy) return;
    busy = true; render(); notice("");
    try {
      await read(await fetch(`/creators${path}`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({csrf_token: app.dataset.csrf, ...fields})}));
      if (tab === "candidates" && (path.endsWith("/confirm") || path.endsWith("/defer"))) {
        const key = `import:${path.split("/")[3]}`;
        data.items = data.items.filter(item => rowKey(item) !== key);
      }
      if (path.endsWith("/confirm")) {
        notice("已确认入库，可在“我的达人”查看。同一人的多个 handle 会合并展示。");
      }
      else if (path.endsWith("/defer")) {
        notice("已暂缓，尚未确认的修改不会入库。");
      }
      else if (path.endsWith("/start")) {
        tab = "candidates"; page = 1; clearRows();
        notice("名单已导入。请选择对应的 Lark 用户后确认入库；暂不清楚的账号可以暂缓。");
      }
      else notice("已保存。");
      void refresh();
    } catch (error) { notice(error.message, true); }
    finally { busy = false; render(); }
  }
  async function checkImport() {
    if (busy) return;
    const file = el("import-file").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".xlsx")) return importMessage("请选择 .xlsx 文件。", true);
    if (file.size > 2 * 1024 * 1024) return importMessage("文件过大，请上传不超过 2 MB 的表格。", true);
    busy = true; importPreview = null; render(); importMessage("正在检查表格…");
    el("import-preview").hidden = true;
    try {
      importPreview = await read(await fetch(`/creators/imports/preview?${new URLSearchParams({cm_id: selectedCM(), creator_type: "cap"})}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream", "X-CSRF-Token": app.dataset.csrf, "X-Filename": encodeURIComponent(file.name)}, body: file,
      }));
      const counts = importPreview.counts || {};
      const nextStep = importPreview.status !== "preview" ? "这份表已导入，请查看导入记录及候选达人。" : counts.valid ? "请核对后确认导入。" : "请修改表格后重新上传。";
      importMessage(`有效 handle ${counts.valid || 0} · 重复 ${counts.duplicates || 0} · 无效 ${counts.invalid || 0}。${nextStep}`);
      const preview = el("import-preview"); preview.replaceChildren();
      preview.append(node("p", `负责人：${selectedCMName()} · CAP 属性：CAP · 来源：Excel。确认导入后生成待确认账号，由负责人选择用户、确认入库。`, "small"));
      const list = node("div", "", "border rounded p-2"); list.style.maxHeight = "240px"; list.style.overflowY = "auto";
      for (const item of (importPreview.rows || []).slice(0, 100)) {
        list.append(node("div", `第 ${item.row_number} 行 · ${item.handle || "—"}${item.error ? ` · ${item.error}` : ""}`, `small py-1 ${item.error ? "text-danger" : ""}`));
      }
      preview.append(list);
      if ((importPreview.rows || []).length > 100) preview.append(node("p", "这里只展示前 100 行，确认后会处理全部有效 handle。", "small text-secondary mt-2"));
      preview.hidden = false;
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { busy = false; render(); }
  }
  async function startImport() {
    if (busy || !importPreview) return;
    busy = true; render(); importMessage("正在导入名单…");
    try {
      await read(await fetch(`/creators/imports/${importPreview.id}/start`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams({csrf_token: app.dataset.csrf})}));
      importPreview = null; el("import-dialog").close();
      tab = "candidates"; page = 1; clearRows();
      notice("名单已导入。请选择对应的 Lark 用户后确认入库；暂不清楚的账号可以暂缓。");
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { busy = false; render(); }
  }
  for (const key of ["candidates", "mine"]) el(key).addEventListener("click", () => { tab = key; page = 1; clearRows(); refresh(); });
  el("search").addEventListener("submit", event => { event.preventDefault(); query = el("query").value.trim(); page = 1; clearRows(); refresh(); });
  el("refresh").addEventListener("click", () => refresh());
  el("previous").addEventListener("click", () => { page--; clearRows(); refresh(); });
  el("next").addEventListener("click", () => { page++; clearRows(); refresh(); });
  el("import-open").addEventListener("click", () => {
    importPreview = null; el("import-file").value = ""; el("import-preview").hidden = true;
    el("import-cm").textContent = selectedCMName(); importMessage(""); render(); el("import-dialog").showModal();
  });
  el("import-close").addEventListener("click", () => el("import-dialog").close());
  el("import-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  el("import-file").addEventListener("change", () => {
    importPreview = null; el("import-preview").hidden = true; importMessage(""); render();
  });
  el("import-check").addEventListener("click", checkImport);
  el("import-confirm").addEventListener("click", startImport);
  if (data.can_manage) {
    if (data.cms?.length) {
      el("cm").replaceChildren(...data.cms.map(cm => { const option = node("option", cm.name); option.value = cm.id; return option; }));
      el("cm").value = data.selected_cm_id;
    }
    el("cm").addEventListener("change", () => {
      data = {...data, items: [], import_batches: [], page: 1, total_count: 0, total_pages: 0};
      page = 1; clearRows(); render(); refresh();
    });
  }
  render();
})();
