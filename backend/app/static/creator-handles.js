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
  let busy = false, reading = false, listLoading = false, refreshQueued = false, version = 0, denied = false;
  const rows = new Map();
  const selectedContacts = new Map();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let importPreview = null;
  let reviewBatch = null, reviewPage = 1, reviewPages = 1, reviewLoading = false, reviewVersion = 0;
  let linkSelection = null, linkOriginal = null, linkVersion = 0, linkSearching = false;
  const reviewDrafts = new Map();
  const rowKey = item => `${item.kind || "person"}:${item.id}`;
  const node = (tag, text = "", classes = "") => {
    const item = document.createElement(tag); item.textContent = text; item.className = classes; return item;
  };
  function notice(text, error = false) {
    el("notice").textContent = text;
    el("notice").className = text ? `alert alert-${error ? "danger" : "success"}` : "alert d-none";
  }
  async function read(response) {
    if (response.redirected || [401, 403].includes(response.status)) clearProtected();
    const body = !denied && response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
    if (denied || !response.ok) {
      const error = new Error(denied ? "登录或查看权限已变化，请刷新或重新登录。" : body.error || "访问失败，请刷新或重新登录。");
      error.status = denied ? 403 : response.status; throw error;
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
  function clearReview() {
    reviewVersion++; reviewLoading = false; reviewBatch = null; reviewDrafts.clear(); el("review-rows").replaceChildren(); reviewMessage("");
  }
  function clearProtected() {
    denied = true; version++; refreshQueued = false; listLoading = false;
    selectedContacts.clear(); importPreview = null; clearRows(); clearReview();
    el("initial").textContent = ""; el("query").value = "";
    el("import-file").value = ""; el("import-preview").replaceChildren(); el("import-preview").hidden = true;
    el("import-cm").textContent = ""; importMessage("");
    el("review-title").textContent = "核对导入账号"; el("review-page-label").textContent = "";
    el("import-dialog").close(); el("review-dialog").close(); clearLink(); el("link-dialog")?.close();
    data = {...data, items: [], import_batches: [], cms: [], selected_cm_id: "", read_only: true,
      can_view_unassigned: false, can_select_cm: false, can_broadcast: false,
      can_view_importance: false, can_adjust_importance: false, can_edit_handles: false, can_link_people: false, importance_running: false,
      total_count: 0, total_pages: 1, page: 1};
    if (el("cm")) el("cm").replaceChildren();
    notice("登录或查看权限已变化，请刷新或重新登录。", true); render();
  }
  function importMessage(text, error = false) {
    el("import-message").textContent = text;
    el("import-message").className = `small mt-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function linkMessage(text, error = false) {
    if (!el("link-message")) return;
    el("link-message").textContent = text;
    el("link-message").className = `small mt-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function clearLink() {
    linkVersion++; linkSelection = null; linkOriginal = null; linkSearching = false;
    el("link-results")?.replaceChildren();
    if (el("link-save")) el("link-save").disabled = true;
    if (el("link-handle")) el("link-handle").value = "";
    if (el("link-query")) el("link-query").value = "";
    if (el("link-original")) el("link-original").textContent = "";
    if (el("link-search")) el("link-search").disabled = busy;
    el("link-cm")?.replaceChildren();
    linkMessage("");
  }
  function openLink(original = null, handle = "") {
    if (denied || busy || !data.can_link_people || !el("link-dialog")) return;
    clearLink(); linkOriginal = original;
    el("link-title").textContent = original ? "更正对应 Lark 用户" : "手动关联达人";
    el("link-effect").textContent = original ? "只更正账号归属，原有负责人和聊天记录不变。" : "确认达人身份和账号，并加入当前负责人的名单。";
    el("link-original").hidden = !original;
    el("link-original").textContent = original ? `原 Lark 用户：${original.display_name}` : "";
    el("link-handle").value = handle; el("link-handle").readOnly = Boolean(original);
    el("link-query").value = "";
    el("link-cm-field").hidden = !data.can_view_unassigned;
    el("link-cm").replaceChildren(...(data.cms || []).map(cm => { const option = node("option", cm.name); option.value = cm.id; return option; }));
    el("link-cm").value = selectedCM();
    el("link-save").textContent = original ? "保存更正" : "关联并加入我的达人";
    if (!original && selectedCM() !== app.dataset.actor) el("link-save").textContent = `关联并加入 ${selectedCMName()} 的达人`;
    el("link-dialog").showModal();
    (original ? el("link-query") : el("link-handle")).focus();
  }
  async function searchLinkPeople() {
    if (denied || busy || linkSearching || !el("link-dialog")?.open || !el("link-query").value.trim()) return;
    const token = ++linkVersion, cm = el("link-cm").value || selectedCM();
    linkSelection = null; linkSearching = true; el("link-save").disabled = true; el("link-search").disabled = true;
    el("link-results").replaceChildren(); linkMessage("正在搜索…");
    try {
      const result = await read(await fetch(`/creators/people/search?${new URLSearchParams({cm_id: cm, q: el("link-query").value.trim()})}`, {cache: "no-store"}));
      if (token !== linkVersion || !el("link-dialog").open) return;
      for (const person of result.items || []) {
        const button = node("button", "", "list-group-item list-group-item-action d-flex align-items-center gap-3");
        button.type = "button"; button.setAttribute("aria-pressed", "false");
        button.setAttribute("aria-label", `${person.display_name} · ${person.context_label || "有历史交流"}`);
        const initial = node("span", Array.from(person.display_name || "?")[0], "avatar avatar-sm");
        if (typeof person.avatar_url === "string" && person.avatar_url.startsWith("https://")) {
          const avatar = node("img", "", "avatar avatar-sm"); avatar.src = person.avatar_url; avatar.alt = ""; avatar.referrerPolicy = "no-referrer";
          avatar.addEventListener("error", () => avatar.replaceWith(initial)); button.append(avatar);
        } else button.append(initial);
        const label = node("span", "", "text-start"); label.append(node("strong", person.display_name), node("span", person.context_label || "有历史交流", "d-block small text-secondary"));
        button.append(label);
        button.addEventListener("click", () => {
          if (busy || denied) return;
          linkSelection = person;
          for (const sibling of el("link-results").children) { sibling.classList.remove("active"); sibling.setAttribute("aria-pressed", "false"); }
          button.classList.add("active"); button.setAttribute("aria-pressed", "true");
          el("link-save").disabled = false; linkMessage("");
        });
        el("link-results").append(button);
      }
      linkMessage(result.items?.length ? "" : "未找到当前负责人可见的用户。");
    } catch (error) { if (token === linkVersion) linkMessage(error.message, true); }
    finally { if (token === linkVersion) { linkSearching = false; el("link-search").disabled = busy; } }
  }
  async function saveLink() {
    if (denied || busy || !linkSelection) return;
    const fields = {csrf_token: app.dataset.csrf, cm_id: el("link-cm").value || selectedCM(),
      handle: el("link-handle").value.trim(), contact_id: linkSelection.contact_id};
    if (linkOriginal) fields.old_contact_id = linkOriginal.contact_id;
    busy = true; render(); el("link-save").disabled = true; el("link-close").disabled = true;
    for (const key of ["handle", "cm", "query", "search"]) el(`link-${key}`).disabled = true;
    el("link-save").setAttribute("aria-busy", "true"); linkMessage("");
    try {
      await read(await fetch("/creators/people/link", {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams(fields)}));
      const correction = Boolean(linkOriginal); el("link-dialog").close(); clearLink();
      notice(correction ? "账号关联已更正。" : "已关联并加入达人名单。");
      page = 1; clearRows();
    } catch (error) { linkMessage(error.message, true); }
    finally {
      busy = false; el("link-save").removeAttribute("aria-busy"); el("link-close").disabled = false;
      for (const key of ["handle", "cm", "query", "search"]) el(`link-${key}`).disabled = false;
      el("link-save").disabled = !linkSelection; render(); if (!denied) void refresh();
    }
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
      if (batch.status !== "preview" && !data.read_only) {
        const review = node("button", "核对账号", "btn btn-sm btn-outline-primary");
        review.type = "button"; review.disabled = busy;
        review.addEventListener("click", () => {
          reviewBatch = {id: batch.id, cm_id: selectedCM()}; reviewPage = 1;
          el("review-title").textContent = `核对账号 · ${batch.filename || "Excel 名单"}`;
          el("review-dialog").showModal(); void loadReview();
        });
        line.append(review);
      }
      for (const [action, text] of [["start", "确认导入"], ["pause", "暂停"], ["resume", "继续"], ["recheck", "重新检查未确认"]]) {
        if (!batch[`can_${action}`]) continue;
        const button = node("button", text, "btn btn-sm btn-outline-primary");
        button.type = "button"; button.disabled = busy;
        button.addEventListener("click", () => mutate(`/imports/${batch.id}/${action}`)); line.append(button);
      }
      el("import-batch-list").append(line);
    }
    for (const control of el("review-dialog").querySelectorAll("button, input, select")) control.disabled = busy || reviewLoading;
    el("review-previous").disabled ||= reviewPage <= 1;
    el("review-next").disabled ||= reviewPage >= reviewPages;
  }
  function reviewMessage(text, error = false) {
    el("review-message").textContent = text;
    el("review-message").className = `small mb-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function renderEvidence(target, material) {
    target.replaceChildren();
    for (const warning of [...(material.warnings || []), ...(material.conflicts || [])]) target.append(node("p", warning, "small text-danger"));
    const cited = new Set();
    for (const [label, decision] of [["找群", material.group_decision], ["找人", material.person]]) {
      if (!decision?.status_label) continue;
      target.append(node("p", `${label}：${decision.status_label}${decision.display_name ? ` · ${decision.display_name}` : ""}${decision.confidence != null ? ` · 自评分 ${Math.round(decision.confidence * 100)}%` : ""}`, "small fw-semibold mb-1"));
      target.append(node("p", decision.reason || "", "small text-secondary"));
      for (const ref of decision.evidence_refs || []) cited.add(ref);
    }
    const messageNode = message => {
      const item = node("div", "", "border-start ps-2 my-2");
      item.append(node("div", `${message.ref} · ${message.sender_name} · ${message.time_label}`, "small text-secondary"));
      const body = node("div", message.text, "small"); body.style.whiteSpace = "pre-wrap"; item.append(body); return item;
    };
    for (const group of material.groups || []) {
      target.append(node("div", `${group.name}${group.selected ? " · 已选群" : ""}`, "small fw-semibold mt-3"));
      const messages = group.messages || [];
      for (const message of messages.filter(message => cited.has(message.ref))) target.append(messageNode(message));
      const more = node("details", "", "small mt-2"); more.append(node("summary", "更多上下文与群成员"));
      for (const member of group.members || []) more.append(node("p", `${member.display_name} · ${member.status_label} · ${member.reason || ""}`, "small text-secondary my-2"));
      for (const message of messages.filter(message => !cited.has(message.ref))) more.append(messageNode(message));
      target.append(more);
    }
    if (!target.childElementCount) target.append(node("p", material.reason || "暂无可展示的证据。", "small text-secondary"));
  }
  async function loadReview() {
    if (denied || !reviewBatch || reviewLoading) return;
    const token = ++reviewVersion, scope = reviewBatch;
    for (const card of el("review-rows").children) {
      const {handle, userQuery, users} = card.reviewFields;
      reviewDrafts.set(card.dataset.row, {handle: handle.value, query: userQuery.value, selected: users.value,
        users: [...users.options].map(option => ({value: option.value, text: option.textContent}))});
    }
    reviewLoading = true; reviewMessage("正在加载待核对账号…"); renderImports();
    el("review-rows").replaceChildren();
    try {
      const result = await read(await fetch(`/creators/imports/data?${new URLSearchParams({cm_id: scope.cm_id, batch_id: scope.id, page: String(reviewPage)})}`, {cache: "no-store"}));
      if (token !== reviewVersion || !el("review-dialog").open) return;
      reviewPage = result.page; reviewPages = result.total_pages;
      el("review-page-label").textContent = `待核对 ${result.total} 条 · 第 ${reviewPage} / ${reviewPages} 页`;
      reviewMessage(result.total ? "" : "本批账号已全部入库。");
      for (const candidate of result.candidates || []) {
        const card = node("details", "", "border rounded p-3 mb-2");
        card.dataset.row = candidate.id;
        card.append(node("summary", `@${candidate.handle} · ${candidate.display_name} · ${candidate.status_label}`, "fw-semibold"));
        card.append(node("p", candidate.reason, "small text-secondary mt-2"));
        const evidence = node("details", "", "my-2"), material = node("div");
        evidence.append(node("summary", "查看证据", "small"), material);
        evidence.addEventListener("toggle", async () => {
          if (!evidence.open || evidence.dataset.loaded) return;
          evidence.dataset.loaded = "true"; material.textContent = "正在读取证据…";
          try {
            const result = await read(await fetch(`/creators/imports/rows/${candidate.id}/evidence`, {cache: "no-store"}));
            if (token === reviewVersion) renderEvidence(material, result);
          } catch (error) { material.textContent = error.message; delete evidence.dataset.loaded; }
        });
        const form = node("form", "", "mt-3"), handle = node("input", "", "form-control form-control-sm");
        handle.value = candidate.handle; handle.maxLength = 160; handle.required = true; handle.setAttribute("aria-label", "核对 handle");
        const userQuery = node("input", "", "form-control form-control-sm"), search = node("button", "搜索用户", "btn btn-sm btn-outline-secondary");
        userQuery.type = "search"; userQuery.maxLength = 120; userQuery.placeholder = "搜索 Lark 用户姓名"; userQuery.setAttribute("aria-label", "搜索 Lark 用户姓名"); search.type = "button";
        const users = node("select", "", "form-select form-select-sm mt-2"); users.required = true; users.setAttribute("aria-label", "对应 Lark 用户");
        const placeholder = node("option", "请搜索并选择对应用户"); placeholder.value = ""; users.append(placeholder);
        if (candidate.contact_id) { const user = node("option", candidate.display_name); user.value = candidate.contact_id; users.append(user); users.value = candidate.contact_id; }
        const draft = reviewDrafts.get(candidate.id);
        if (draft) {
          handle.value = draft.handle; userQuery.value = draft.query;
          users.replaceChildren(...draft.users.map(item => { const option = node("option", item.text); option.value = item.value; return option; }));
          users.value = draft.selected;
        }
        card.reviewFields = {handle, userQuery, users};
        const status = node("div", "", "small mt-2"); status.setAttribute("role", "status");
        const searchUsers = async () => {
          if (busy || search.disabled || !userQuery.value.trim()) return;
          search.disabled = true; status.textContent = "正在搜索…";
          try {
            const found = await read(await fetch(`/creators/imports/users?${new URLSearchParams({cm_id: scope.cm_id, q: userQuery.value.trim()})}`, {cache: "no-store"}));
            if (token !== reviewVersion) return;
            users.replaceChildren(placeholder);
            for (const item of found) { const option = node("option", `${item.display_name} · ${item.context_label}`); option.value = item.id; users.append(option); }
            status.textContent = found.length ? "请选择对应用户。" : "未找到当前负责人可访问的用户。";
          } catch (error) { status.textContent = error.message; }
          finally { search.disabled = busy; }
        };
        search.addEventListener("click", searchUsers);
        userQuery.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); void searchUsers(); } });
        const searchLine = node("div", "", "d-flex gap-2 mt-2"); searchLine.append(userQuery, search);
        const confirm = node("button", "确认入库", "btn btn-sm btn-primary"), defer = node("button", "暂缓", "btn btn-sm btn-outline-secondary"); confirm.type = "submit"; defer.type = "button";
        defer.hidden = !candidate.can_defer;
        form.addEventListener("submit", event => { event.preventDefault(); void mutate(`/imports/rows/${candidate.id}/confirm`, {handle: handle.value.trim(), contact_id: users.value}, confirm); });
        defer.addEventListener("click", () => void mutate(`/imports/rows/${candidate.id}/defer`, {}, defer));
        const actions = node("div", "", "d-flex gap-2 mt-3"); actions.append(confirm, defer);
        form.append(node("label", "达人 handle", "small mb-1"), handle, searchLine, users, status, actions); card.append(evidence, form);
        el("review-rows").append(card);
      }
    } catch (error) {
      if (token === reviewVersion) reviewMessage(error.message, true);
    } finally { if (token === reviewVersion) { reviewLoading = false; renderImports(); } }
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
    if (document.getElementById("operations-assistant")) {
      parts.ask = node("button", "问助手", "btn btn-sm btn-outline-secondary mb-2"); parts.ask.type = "button";
      parts.ask.addEventListener("click", () => document.dispatchEvent(new CustomEvent("ttmedia:assistant", {detail: {
        message: "查看这位达人的账号、负责人和维护情况", context: {
          cm_id: selectedCM() || app.dataset.actor, creator_ids: [item.contact_id], creator_name: item.display_name,
        },
      }})));
      actions.append(parts.ask);
    }
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
      const editor = node("details", "", "creator-inline-editor creator-handle-editor"); editor.append(node("summary", "管理账号"));
      const form = node("form", "", "mt-2"), oldHandle = node("select", "", "form-select form-select-sm"), handle = node("input", "", "form-control form-control-sm mt-2");
      const operation = node("select", "", "form-select form-select-sm mb-2"); operation.setAttribute("aria-label", "账号操作");
      for (const [value, label] of [["replace", "修改账号"], ["add", "添加账号"], ["remove", "解除关联"]]) { const option = node("option", label); option.value = value; operation.append(option); }
      oldHandle.setAttribute("aria-label", "选择要修改的 handle");
      handle.maxLength = 160; handle.required = true; handle.placeholder = "新 handle"; handle.setAttribute("aria-label", "新 handle");
      const save = node("button", "保存", "btn btn-sm btn-outline-primary mt-2"); save.type = "submit";
      const cancel = node("button", "取消", "btn btn-sm btn-link mt-2"); cancel.type = "button";
      cancel.addEventListener("click", () => { if (!busy) { parts.handleDirty = false; editor.open = false; render(); } });
      form.append(operation, oldHandle, handle, save, cancel); editor.append(form); actions.append(editor);
      Object.assign(parts, {handleEditor: editor, oldHandle, handleOperation: operation, handleInput: handle, handleSave: save, handleCancel: cancel});
      const updateOperation = () => {
        const remove = operation.value === "remove", add = operation.value === "add";
        parts.handleDirty = true; oldHandle.hidden = add; handle.hidden = remove; handle.required = !remove;
        handle.value = add ? "" : oldHandle.value; save.textContent = remove ? "解除关联" : "保存";
        if (parts.handleCorrection) parts.handleCorrection.hidden = add || !oldHandle.value;
      };
      operation.addEventListener("change", updateOperation);
      oldHandle.addEventListener("change", () => { parts.handleDirty = true; handle.value = oldHandle.value; });
      handle.addEventListener("input", () => { parts.handleDirty = true; });
      form.addEventListener("submit", event => {
        event.preventDefault();
        void mutate(`/people/${item.contact_id}/handle`, {cm_id: selectedCM(), operation: operation.value,
          old_handle: operation.value === "add" ? "" : oldHandle.value, handle: operation.value === "remove" ? "" : handle.value.trim()}, save);
      });
      if (data.can_view_unassigned) {
        const correction = node("button", "更正 Lark 用户", "btn btn-sm btn-link mt-2"); correction.type = "button";
        correction.addEventListener("click", () => openLink(item, oldHandle.value)); form.append(correction); parts.handleCorrection = correction;
      }
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
    if (el("link-open")) { el("link-open").hidden = !data.can_link_people; el("link-open").disabled = busy || reading; }
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
      if (parts.ask) {
        parts.ask.hidden = tab !== "mine" || item.kind !== "confirmed" || !uuid.test(item.contact_id || "");
        parts.ask.disabled = busy || reading;
      }
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
          parts.handleOperation.value = item.handles?.length ? "replace" : "add";
          parts.oldHandle.hidden = !item.handles?.length; parts.handleInput.hidden = false; parts.handleInput.required = true;
          parts.handleSave.textContent = "保存"; parts.handleInput.value = parts.oldHandle.value;
        }
        parts.handleOperation.disabled = busy;
        parts.oldHandle.disabled = busy; parts.handleInput.disabled = busy; parts.handleSave.disabled = busy; parts.handleCancel.disabled = busy;
        if (parts.handleCorrection) {
          parts.handleCorrection.hidden = !data.can_view_unassigned || parts.handleOperation.value === "add" || !parts.oldHandle.value;
          parts.handleCorrection.disabled = busy || !parts.oldHandle.value;
        }
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
    if (denied) for (const control of app.querySelectorAll("button, input, select")) control.disabled = true;
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
    if (denied) return;
    if (automatic && (reading || busy || document.hidden || [...rows.values()].some(row => row.manualDirty || row.handleDirty) || el("import-dialog").open || el("review-dialog").open || el("link-dialog")?.open)) return;
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
        notice(error.message, true);
      }
    }
    finally {
      reading = false;
      renderSelection();
      if (refreshQueued) { refreshQueued = false; void refresh(); }
      else { listLoading = false; render(); }
    }
  }
  async function mutate(path, fields = {}, button = null) {
    if (denied || busy) return;
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
      else if (path.endsWith("/confirm")) notice("账号已入库。");
      else if (path.endsWith("/defer")) notice("账号已暂缓。");
      else notice("已保存。");
      await refresh();
      if (path.startsWith("/imports/rows/")) await loadReview();
    } catch (error) {
      notice(error.message, true);
      if (el("review-dialog").open) reviewMessage(error.message, true);
    }
    finally { button?.removeAttribute("aria-busy"); busy = false; render(); }
  }
  async function checkImport() {
    if (denied || busy) return;
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
    if (denied || busy || !importPreview) return;
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
  el("review-close").addEventListener("click", () => el("review-dialog").close());
  el("review-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  el("review-dialog").addEventListener("close", clearReview);
  el("link-open")?.addEventListener("click", () => openLink());
  el("link-close")?.addEventListener("click", () => { if (!busy) el("link-dialog").close(); });
  el("link-dialog")?.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  el("link-dialog")?.addEventListener("close", clearLink);
  el("link-search")?.addEventListener("click", searchLinkPeople);
  el("link-query")?.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); void searchLinkPeople(); } });
  el("link-query")?.addEventListener("input", () => { linkVersion++; linkSelection = null; linkSearching = false; el("link-search").disabled = false; el("link-save").disabled = true; el("link-results").replaceChildren(); });
  el("link-cm")?.addEventListener("change", () => {
    linkVersion++; linkSelection = null; linkSearching = false; el("link-search").disabled = false; el("link-save").disabled = true; el("link-results").replaceChildren(); linkMessage("");
    if (!linkOriginal) el("link-save").textContent = el("link-cm").value === app.dataset.actor ? "关联并加入我的达人" : `关联并加入 ${el("link-cm").selectedOptions[0]?.textContent || "该负责人"} 的达人`;
  });
  el("link-form")?.addEventListener("submit", event => { event.preventDefault(); void saveLink(); });
  for (const [key, delta] of [["previous", -1], ["next", 1]]) el(`review-${key}`).addEventListener("click", () => { reviewPage += delta; void loadReview(); });
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
      document.dispatchEvent(new CustomEvent("ttmedia:assistant", {detail: {
        context: {cm_id: selectedCM(), cm_name: selectedCMName()},
      }}));
      selectedContacts.clear();
      tab = "mine";
      data = {...data, tab, items: [], import_batches: [], page: 1, total_count: 0, total_pages: 0};
      page = 1; clearRows(); render(); refresh();
    });
  }
  render();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(true); });
  window.addEventListener("pageshow", event => { if (event.persisted) void refresh(true); });
  setInterval(() => {
    if (data.importance_running || data.import_batches?.some(batch => batch.status === "running")) void refresh(true);
  }, 30000);
})();
