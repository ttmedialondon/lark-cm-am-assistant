"use strict";

(() => {
  const app = document.getElementById("creators-app");
  if (!app) return;
  const el = key => document.getElementById(`creators-${key}`);
  let data = JSON.parse(el("initial").textContent);
  el("initial").textContent = "";
  let tab = data.tab || app.dataset.tab;
  let page = data.page || 1;
  let query = el("query").value.trim();
  let sort = data.sort || "importance", tier = data.tier || "";
  let busy = false, reading = false, listLoading = false, refreshQueued = false, version = 0;
  const rows = new Map();
  const selectedContacts = new Map();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
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
    if (!response.ok || response.redirected) {
      const error = new Error(body.error || "访问失败，请刷新或重新登录。");
      error.status = response.redirected ? 401 : response.status; throw error;
    }
    return body;
  }
  function selectedCM() { return el("cm")?.value || data.selected_cm_id || ""; }
  function selectedCMName() { return data.cms?.find(cm => cm.id === selectedCM())?.name || "我"; }
  const canSelectContacts = () => Boolean(data.can_broadcast && (data.can_view_unassigned
    || tab === "mine" && selectedCM() === app.dataset.actor));
  function selectableContact(item) {
    return canSelectContacts() && item.kind === "confirmed"
      && typeof item.contact_id === "string" && uuid.test(item.contact_id) ? item.contact_id : "";
  }
  function renderSelection() {
    if (!el("selection-bar")) return;
    const available = canSelectContacts();
    if (!available) selectedContacts.clear();
    el("selection-bar").hidden = !available; el("selection-head").hidden = !available;
    el("selection-count").textContent = `已选 ${selectedContacts.size} / 500 位`;
    const ids = (data.items || []).map(selectableContact).filter(Boolean);
    const count = ids.filter(id => selectedContacts.has(id)).length;
    el("select-page").checked = Boolean(ids.length && count === ids.length);
    el("select-page").indeterminate = count > 0 && count < ids.length;
    el("select-page").disabled = busy || reading || !ids.length;
    el("selection-clear").disabled = busy || reading || !selectedContacts.size;
    el("selection-broadcast").disabled = busy || reading || !selectedContacts.size;
    for (const parts of rows.values()) if (parts.selection) {
      parts.selectionCell.hidden = !available;
      parts.selection.checked = selectedContacts.has(parts.selectionId);
      parts.selection.disabled = busy || reading || !parts.selectionId;
    }
  }
  function clearRows() { data = {...data, items: [], page}; rows.clear(); el("rows").replaceChildren(); }
  function importMessage(text, error = false) {
    el("import-message").textContent = text;
    el("import-message").className = `small mt-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function renderImports() {
    for (const key of ["file", "check", "confirm", "close"]) el(`import-${key}`).disabled = busy;
    el("import-confirm").hidden = !importPreview || importPreview.status !== "preview" || !(importPreview.counts?.valid > 0);
    const batches = data.import_batches || [];
    el("import-batches").hidden = !batches.length;
    el("import-batch-list").replaceChildren();
    for (const batch of batches) {
      const line = node("div", "", "d-flex flex-wrap align-items-center gap-2 mb-2");
      const label = node("div", "", "me-auto");
      label.append(node("strong", batch.filename || "Excel 名单"), node("span", ` · ${batch.status_label || batch.status}`, "text-secondary"));
      const counts = batch.counts || {};
      label.append(node("div", batch.status === "preview" ? `有效 handle ${counts.valid || 0}` : `已入库 ${counts.confirmed || 0} · 待核对 ${counts.review || 0} · 待处理 ${(counts.pending || 0) + (counts.queued || 0)} · 分析中 ${(counts.preparing || 0) + (counts.running || 0)}${counts.failed ? ` · 异常 ${counts.failed}` : ""}`, "small text-secondary"));
      if (batch.error) label.append(node("div", batch.error, "small text-danger"));
      line.append(label);
      for (const [action, text] of [["start", "确认导入"], ["pause", "暂停"], ["resume", "继续"], ["recheck", "重新检查未确认"]]) {
        if (!batch[`can_${action}`]) continue;
        const button = node("button", text, "btn btn-sm btn-outline-primary");
        button.type = "button"; button.disabled = busy;
        button.addEventListener("click", () => mutate(`/imports/${batch.id}/${action}`)); line.append(button);
      }
      el("import-batch-list").append(line);
    }
  }
  function makeRow(item) {
    const row = node("tr");
    const person = node("td");
    const personWrap = node("div", "", "d-flex align-items-center gap-2");
    const avatar = node("span", "", "avatar avatar-sm bg-blue-lt rounded-circle flex-shrink-0 position-relative overflow-hidden");
    avatar.setAttribute("aria-hidden", "true");
    const initial = node("span"); avatar.append(initial);
    const name = node("a", "", "fw-bold");
    const lark = node("a", "", "creator-lark-link text-secondary");
    const icon = node("img"); icon.src = "/static/lark-share.svg"; icon.alt = "";
    icon.width = 16; icon.height = 16; icon.setAttribute("aria-hidden", "true"); lark.append(icon);
    lark.target = "_blank"; lark.rel = "noopener noreferrer";
    lark.title = "在 Lark 中打开私聊";
    const label = node("div", "", "d-flex align-items-center"); label.append(name, lark);
    const personLabel = node("div"), importanceBadge = node("span", "", "creator-importance-badge");
    personLabel.append(label, importanceBadge); personWrap.append(avatar, personLabel); person.append(personWrap);
    const handleCell = node("td"); handleCell.style.minWidth = "250px";
    const saved = node("div", "", "mb-2"); handleCell.append(saved);
    const actions = node("td");
    const parts = {row, name, lark, avatar, initial, saved, actions, importanceBadge, manualDirty: false, handleDirty: false};
    if (data.can_adjust_importance) {
      const editor = node("details", "", "creator-inline-editor creator-importance-editor"); editor.append(node("summary", "调整重要度"));
      const form = node("form", "", "mt-2"), manualTier = node("select", "", "form-select form-select-sm"), manualReason = node("input", "", "form-control form-control-sm mt-2");
      manualTier.setAttribute("aria-label", "重要度");
      for (const [value, label] of [["", "使用自动评估"], ["focus", "重点"], ["priority", "优先"], ["normal", "常规"], ["dormant", "久未联系"], ["pending", "待评估"]]) {
        const option = node("option", label); option.value = value; manualTier.append(option);
      }
      manualReason.maxLength = 240; manualReason.placeholder = "调整说明"; manualReason.setAttribute("aria-label", "调整说明");
      const save = node("button", "保存", "btn btn-sm btn-outline-primary mt-2"); save.type = "submit";
      const cancel = node("button", "取消", "btn btn-sm btn-link mt-2"); cancel.type = "button";
      cancel.addEventListener("click", () => { if (!busy) { parts.manualDirty = false; editor.open = false; render(); } });
      form.append(manualTier, manualReason, save, cancel); editor.append(form); actions.append(editor);
      Object.assign(parts, {manualEditor: editor, manualTier, manualReason, manualSave: save, manualCancel: cancel});
      manualTier.addEventListener("change", () => { parts.manualDirty = true; manualReason.required = Boolean(manualTier.value); });
      manualReason.addEventListener("input", () => { parts.manualDirty = true; });
      form.addEventListener("submit", event => {
        event.preventDefault();
        void mutate(`/people/${item.contact_id}/importance`, {cm_id: selectedCM(), tier: manualTier.value, reason: manualReason.value.trim()}, save);
      });
    }
    if (data.can_edit_handles) {
      const editor = node("details", "", "creator-inline-editor creator-handle-editor"); editor.append(node("summary", "修改 handle"));
      const form = node("form", "", "mt-2"), oldHandle = node("select", "", "form-select form-select-sm"), handle = node("input", "", "form-control form-control-sm mt-2");
      oldHandle.setAttribute("aria-label", "选择要修改的 handle");
      handle.maxLength = 160; handle.required = true; handle.placeholder = "新 handle"; handle.setAttribute("aria-label", "新 handle");
      const save = node("button", "保存", "btn btn-sm btn-outline-primary mt-2"); save.type = "submit";
      const cancel = node("button", "取消", "btn btn-sm btn-link mt-2"); cancel.type = "button";
      cancel.addEventListener("click", () => { if (!busy) { parts.handleDirty = false; editor.open = false; render(); } });
      form.append(oldHandle, handle, save, cancel); editor.append(form); actions.append(editor);
      Object.assign(parts, {handleEditor: editor, oldHandle, handleInput: handle, handleSave: save, handleCancel: cancel});
      oldHandle.addEventListener("change", () => { parts.handleDirty = true; handle.value = oldHandle.value; });
      handle.addEventListener("input", () => { parts.handleDirty = true; });
      form.addEventListener("submit", event => {
        event.preventDefault();
        void mutate(`/people/${item.contact_id}/handle`, {cm_id: selectedCM(), old_handle: oldHandle.value, handle: handle.value.trim()}, save);
      });
    }
    if (el("selection-bar")) {
      parts.selectionCell = node("td", "", "creator-selection-cell");
      parts.selection = node("input", "", "form-check-input"); parts.selection.type = "checkbox";
      parts.selection.addEventListener("change", () => {
        if (busy || reading || !canSelectContacts() || !parts.selectionId) return;
        if (parts.selection.checked && !selectedContacts.has(parts.selectionId) && selectedContacts.size >= 500) {
          notice("最多选择 500 位达人。", true); renderSelection(); return;
        }
        if (parts.selection.checked) selectedContacts.set(parts.selectionId, true);
        else selectedContacts.delete(parts.selectionId);
        renderSelection();
      });
      parts.selectionCell.append(parts.selection); row.append(parts.selectionCell);
    }
    row.append(handleCell, person, actions);
    return parts;
  }
  function render() {
    tab = data.tab || tab;
    el("importance-toolbar").hidden = tab !== "mine" || !data.can_view_importance;
    el("sort").value = sort; el("tier").value = tier;
    el("sort").disabled = busy; el("tier").disabled = busy;
    el("unassigned").hidden = !data.can_view_unassigned;
    el("import-open").hidden = Boolean(data.read_only);
    if (el("cm-filter")) el("cm-filter").hidden = tab !== "mine" || !data.can_select_cm;
    if (data.can_select_cm) el("cm").disabled = busy;
    for (const key of ["unassigned", "mine"]) {
      el(key).className = `btn ${key === tab ? "btn-primary" : "btn-outline-secondary"}`;
      el(key).setAttribute("aria-pressed", String(key === tab));
      el(key).disabled = busy;
    }
    const ids = new Set((data.items || []).map(rowKey));
    for (const [id, parts] of rows) if (!ids.has(id)) { parts.row.remove(); rows.delete(id); }
    for (const [index, item] of (data.items || []).entries()) {
      const key = rowKey(item);
      if (!rows.has(key)) rows.set(key, makeRow(item));
      const parts = rows.get(key);
      if (el("rows").children[index] !== parts.row) el("rows").insertBefore(parts.row, el("rows").children[index] || null);
      if (parts.selection) {
        parts.selectionId = selectableContact(item);
        parts.selection.setAttribute("aria-label", `选择 ${item.display_name || "此达人"}`);
      }
      parts.name.textContent = item.display_name || "—";
      if (item.profile_url) parts.name.href = item.profile_url;
      else parts.name.removeAttribute("href");
      parts.name.title = item.profile_url ? "查看个人资料" : "";
      const importance = item.importance;
      parts.importanceBadge.hidden = !importance;
      if (parts.manualEditor) {
        parts.manualEditor.hidden = !data.can_adjust_importance || !importance;
        if (parts.manualEditor.hidden) { parts.manualDirty = false; parts.manualEditor.open = false; }
      }
      if (importance) {
        parts.importanceBadge.dataset.tier = ["focus", "priority", "normal", "dormant"].includes(importance.tier) ? importance.tier : "pending";
        parts.importanceBadge.textContent = [importance.tier_label || "待评估", importance.manual_tier ? "人工" : ""].filter(Boolean).join(" · ");
        if (parts.manualTier) {
          if (!parts.manualDirty) { parts.manualTier.value = importance.manual_tier || ""; parts.manualReason.value = importance.manual_reason || ""; }
          parts.manualReason.required = Boolean(parts.manualTier.value);
          parts.manualTier.disabled = busy; parts.manualReason.disabled = busy; parts.manualSave.disabled = busy; parts.manualCancel.disabled = busy;
        }
      }
      if (parts.handleEditor) {
        parts.handleEditor.hidden = !data.can_edit_handles;
        if (parts.handleEditor.hidden) { parts.handleDirty = false; parts.handleEditor.open = false; }
        if (!parts.handleDirty) {
          parts.oldHandle.replaceChildren(...(item.handles?.length ? item.handles : [""]).map(handle => {
            const option = node("option", handle ? `@${handle}` : "尚无 handle"); option.value = handle; return option;
          }));
          parts.handleInput.value = parts.oldHandle.value;
        }
        parts.oldHandle.disabled = busy; parts.handleInput.disabled = busy; parts.handleSave.disabled = busy; parts.handleCancel.disabled = busy;
      }
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
      parts.lark.hidden = !item.private_chat_url;
      if (item.private_chat_url) parts.lark.href = item.private_chat_url;
      else parts.lark.removeAttribute("href");
      parts.lark.setAttribute("aria-label", `在 Lark 中与 ${item.display_name || "此人"} 私聊（新窗口）`);
      parts.saved.replaceChildren();
      for (const [index, handle] of (item.handles || []).entries()) {
        if (index) parts.saved.append(document.createTextNode(" · "));
        parts.saved.append(node("span", `@${handle.replace(/^@/, "")}`));
      }
      if (!item.handles?.length) parts.saved.textContent = "—";
      if (!parts.actions.childElementCount) parts.actions.textContent = "—";
    }
    page = data.page || 1;
    el("empty").textContent = tab === "unassigned" ? "暂无未分配达人。" : "暂无达人。";
    renderImports();
    renderSelection();
    renderLoading();
  }
  function renderLoading() {
    el("loading").hidden = !listLoading || Boolean(data.items?.length);
    el("loading-rows").hidden = Boolean(data.items?.length);
    el("table").hidden = listLoading && !data.items?.length;
    el("table").inert = listLoading;
    el("table").setAttribute("aria-busy", String(listLoading));
    if (listLoading && data.items?.length) el("table").dataset.uiUpdating = "true";
    else delete el("table").dataset.uiUpdating;
    el("empty").hidden = listLoading || Boolean(data.items?.length);
    el("import-open").disabled = busy || listLoading || !selectedCM();
    el("refresh").disabled = busy || listLoading;
    if (listLoading) el("refresh").setAttribute("aria-busy", "true");
    else el("refresh").removeAttribute("aria-busy");
    el("page-label").textContent = listLoading && !data.items?.length ? "" : `共 ${data.total_count || 0} 人 · 第 ${page} / ${Math.max(1, data.total_pages || 0)} 页`;
    el("previous").disabled = busy || listLoading || page <= 1;
    el("next").disabled = busy || listLoading || page >= (data.total_pages || 1);
  }
  async function refresh(automatic = false) {
    if (automatic && (reading || busy || document.hidden || [...rows.values()].some(row => row.manualDirty || row.handleDirty) || el("import-dialog").open)) return;
    if (!automatic) { listLoading = true; render(); }
    if (reading) {
      refreshQueued = true; version++;
      return;
    }
    const token = ++version; reading = true;
    renderSelection();
    const params = new URLSearchParams({tab, q: query, page: String(page)});
    if (tab === "mine") { params.set("sort", sort); if (tier) params.set("tier", tier); }
    if (selectedCM()) params.set("cm_id", selectedCM());
    history.replaceState(null, "", `/creators?${params}`);
    try {
      const next = await read(await fetch(`/creators/data?${params}`, {cache: "no-store"}));
      if (token === version && !(automatic && [...rows.values()].some(row => row.manualDirty || row.handleDirty))) {
        data = next; sort = data.sort || "importance"; tier = data.tier || ""; render();
      }
    } catch (error) {
      if (token === version) {
        if ([401, 403].includes(error.status)) {
          selectedContacts.clear(); importPreview = null; clearRows();
          el("initial").textContent = "";
          el("import-dialog").close();
          data = {...data, items: [], import_batches: [], cms: [], read_only: true,
            can_view_unassigned: false, can_select_cm: false, can_broadcast: false,
            can_view_importance: false, can_adjust_importance: false, can_edit_handles: false, importance_running: false,
            total_count: 0, total_pages: 1, page: 1};
          if (el("cm")) { el("cm").replaceChildren(); el("cm").disabled = true; }
          render();
        }
        notice(error.message, true);
      }
    }
    finally {
      reading = false;
      renderSelection();
      if (refreshQueued) { refreshQueued = false; void refresh(); }
      else { listLoading = false; renderLoading(); }
    }
  }
  async function mutate(path, fields = {}, button = null) {
    if (busy) return;
    busy = true; render(); notice("");
    button?.setAttribute("aria-busy", "true");
    try {
      const result = await read(await fetch(`/creators${path}`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({csrf_token: app.dataset.csrf, ...fields})}));
      if (path.endsWith("/importance")) {
        const item = data.items.find(item => item.contact_id === path.split("/")[2]);
        if (item && selectedCM() === fields.cm_id) item.importance = result.importance;
        const parts = rows.get(`confirmed:${path.split("/")[2]}`);
        if (parts?.manualEditor) { parts.manualDirty = false; parts.manualEditor.open = false; }
        notice("重要度已更新。");
      }
      else if (path.endsWith("/handle")) {
        const item = data.items.find(item => item.contact_id === path.split("/")[2]);
        if (item && selectedCM() === fields.cm_id) item.handles = result.handles;
        const parts = rows.get(`confirmed:${path.split("/")[2]}`);
        if (parts?.handleEditor) { parts.handleDirty = false; parts.handleEditor.open = false; }
        notice("handle 已更新。");
      }
      else if (path.endsWith("/start")) {
        page = 1; clearRows();
        notice("已导入，正在匹配。");
      }
      else if (path.endsWith("/recheck")) notice("未确认账号已重新排队。");
      else if (path.endsWith("/pause")) notice("已暂停，进行中的请求仍会完成。");
      else if (path.endsWith("/resume")) notice("已继续匹配。");
      else notice("已保存。");
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button?.removeAttribute("aria-busy"); busy = false; render(); }
  }
  async function checkImport() {
    if (busy) return;
    const file = el("import-file").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".xlsx")) return importMessage("请选择 .xlsx 文件。", true);
    if (file.size > 2 * 1024 * 1024) return importMessage("文件过大，请上传不超过 2 MB 的表格。", true);
    busy = true; importPreview = null; render(); importMessage("正在检查表格…");
    el("import-check").setAttribute("aria-busy", "true");
    el("import-preview").hidden = true;
    try {
      importPreview = await read(await fetch(`/creators/imports/preview?${new URLSearchParams({cm_id: selectedCM()})}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream", "X-CSRF-Token": app.dataset.csrf, "X-Filename": encodeURIComponent(file.name)}, body: file,
      }));
      const counts = importPreview.counts || {};
      const nextStep = importPreview.status !== "preview" ? "这份表已导入，请查看导入记录。" : counts.valid ? "请核对后确认导入。" : "请修改表格后重新上传。";
      importMessage(`有效 handle ${counts.valid || 0} · 重复 ${counts.duplicates || 0} · 无效 ${counts.invalid || 0}。${nextStep}`);
      const preview = el("import-preview"); preview.replaceChildren();
      const list = node("div", "", "border rounded p-2"); list.style.maxHeight = "240px"; list.style.overflowY = "auto";
      for (const item of (importPreview.rows || []).slice(0, 100)) {
        list.append(node("div", `第 ${item.row_number} 行 · ${item.handle || "—"}${item.error ? ` · ${item.error}` : ""}`, `small py-1 ${item.error ? "text-danger" : ""}`));
      }
      preview.append(list);
      if ((importPreview.rows || []).length > 100) preview.append(node("p", "预览前 100 行，导入全部有效 handle。", "small text-secondary mt-2"));
      preview.hidden = false;
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { el("import-check").removeAttribute("aria-busy"); busy = false; render(); }
  }
  async function startImport() {
    if (busy || !importPreview) return;
    busy = true; render(); importMessage("正在导入名单…");
    el("import-confirm").setAttribute("aria-busy", "true");
    try {
      await read(await fetch(`/creators/imports/${importPreview.id}/start`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams({csrf_token: app.dataset.csrf})}));
      importPreview = null; el("import-dialog").close();
      page = 1; clearRows();
      notice("已导入，正在匹配。");
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { el("import-confirm").removeAttribute("aria-busy"); busy = false; render(); }
  }
  for (const key of ["unassigned", "mine"]) el(key).addEventListener("click", () => {
    if (tab === key) return;
    selectedContacts.clear(); tab = key; data.tab = key; page = 1; clearRows(); renderSelection(); refresh();
  });
  el("search").addEventListener("submit", event => { event.preventDefault(); query = el("query").value.trim(); page = 1; clearRows(); refresh(); });
  for (const key of ["sort", "tier"]) el(key).addEventListener("change", () => {
    sort = el("sort").value; tier = el("tier").value; page = 1; clearRows(); refresh();
  });
  el("refresh").addEventListener("click", () => { notice(""); void refresh(); });
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
  el("select-page")?.addEventListener("change", () => {
    if (busy || reading || !canSelectContacts()) return;
    const ids = (data.items || []).map(selectableContact).filter(Boolean);
    if (el("select-page").checked) {
      if (selectedContacts.size + ids.filter(id => !selectedContacts.has(id)).length > 500) {
        notice("最多选择 500 位达人。", true); renderSelection(); return;
      }
      for (const id of ids) selectedContacts.set(id, true);
    } else for (const id of ids) selectedContacts.delete(id);
    renderSelection();
  });
  el("selection-clear")?.addEventListener("click", () => { selectedContacts.clear(); notice(""); renderSelection(); });
  el("selection-broadcast")?.addEventListener("click", () => {
    if (busy || reading || !canSelectContacts() || !selectedContacts.size) return;
    const cm = tab === "unassigned" ? "all" : selectedCM();
    if (!uuid.test(app.dataset.actor || "") || (cm !== "all" && !uuid.test(cm))) return notice("当前账号或负责人不可用，请刷新。", true);
    try {
      sessionStorage.setItem("ttmedia.broadcast.selection", JSON.stringify({
        actor_id: app.dataset.actor, cm_id: cm, contact_ids: [...selectedContacts.keys()], created_at: Date.now(),
      }));
      location.assign("/creators/broadcast");
    } catch { notice("无法带入名单，请允许浏览器存储后重试。", true); }
  });
  if (data.can_select_cm) {
    if (data.cms?.length) {
      el("cm").replaceChildren(...data.cms.map(cm => { const option = node("option", cm.name); option.value = cm.id; return option; }));
      el("cm").value = data.selected_cm_id;
    }
    el("cm").addEventListener("change", () => {
      selectedContacts.clear();
      tab = "mine";
      data = {...data, tab, items: [], import_batches: [], page: 1, total_count: 0, total_pages: 0};
      page = 1; clearRows(); render(); refresh();
    });
  }
  render();
  setInterval(() => {
    if (data.importance_running || data.import_batches?.some(batch => batch.status === "running")) void refresh(true);
  }, 5000);
})();
