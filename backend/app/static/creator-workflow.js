/* Real data stays in this page's memory. The server rechecks scope, identity and version. */
(() => {
  "use strict";
  const pending = item => ["pending", "deferred"].includes(item.status);
  function newDraft(item) {
    return {version: item.version, handle: item.handle || "", contact: item.contact || null,
      creator_type: ["cap", "non_cap"].includes(item.creator_type) ? item.creator_type : null,
      dirty: false, revision: 0, request: null, confirmRequest: null, searchOpen: false, handleOpen: false};
  }
  function missingFields(draft) {
    const missing = [];
    if (!draft.contact?.contact_id) missing.push("对应用户（请从搜索结果中选中）");
    return missing;
  }
  function draftBody(draft, requestId) {
    return {version: draft.version, handle: draft.handle.trim() || null,
      contact_id: draft.contact?.contact_id || null, creator_type: draft.creator_type || null, request_id: requestId};
  }
  // A late save updates the revision token, never overwrites edits made during that save.
  function savedDraft(draft, item, sentRevision) {
    if (draft.revision === sentRevision) {
      const fresh = newDraft(item);
      return {...fresh, searchOpen: draft.searchOpen, handleOpen: draft.handleOpen};
    }
    return {...draft, version: item.version, dirty: true, request: null};
  }
  const exported = {pending, newDraft, missingFields, draftBody, savedDraft};
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (typeof document === "undefined") return;
  const bootstrap = document.getElementById("creator-workflow-data");
  if (!bootstrap) return;
  const {scope, csrf_token: csrfToken, data, responsibles = []} = JSON.parse(bootstrap.textContent);
  const writable = scope.can_write === true && !scope.readonly;
  const canScan = scope.can_scan === true;
  const root = document.getElementById("creator-workflow");
  root.classList.toggle("cw-readonly", !writable);
  const $ = id => document.getElementById(id);
  const drafts = new Map(), records = new Map(), selected = new Set(), busy = new Set(), locked = new Set();
  let listing = data, page = data.page || 1, loading = 0, aiTimer = null, importPreview = null;
  let aiLoading = 0, lastAIProgress = null, showAIProgress = false, aiReading = false, aiPollAgain = false;
  const form = $("cw-filters");
  form.elements.type_filter.value = data.type_filter || new URLSearchParams(location.search).get("type_filter") || "all";
  const requestId = () => crypto.randomUUID();
  const element = (tag, text = "", className = "") => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action, style = "btn btn-outline-secondary") => {
    const node = element("button", text, style); node.type = "button";
    node.addEventListener("click", action); return node;
  };
  const feedback = (text, error = false, target = $("cw-feedback")) => {
    target.textContent = text; target.hidden = !text; target.classList.toggle("cw-error", error);
  };
  function endpoint(path, params = {}) {
    const url = new URL(`/dashboard/creators${path}`, location.origin);
    if (scope.responsible_id) url.searchParams.set("responsible_id", scope.responsible_id);
    else if (scope.can_admin) url.searchParams.set("responsible_id", "unassigned");
    for (const [key, value] of Object.entries(params)) if (value !== "" && value != null) url.searchParams.set(key, value);
    return url;
  }
  async function api(path, {body, params, method, signal} = {}) {
    const headers = {"Accept": "application/json"};
    if (body != null) headers["X-CSRF-Token"] = csrfToken;
    const isFile = typeof Blob !== "undefined" && body instanceof Blob;
    if (body != null) headers["Content-Type"] = isFile ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "application/json";
    let response;
    try { response = await fetch(endpoint(path, params), {method: method || (body == null ? "GET" : "POST"),
      body: body == null ? undefined : isFile ? body : JSON.stringify(body), headers,
      credentials: "same-origin", cache: "no-store", redirect: "error",
      signal: signal || (body == null ? AbortSignal.timeout(15000) : undefined)}); }
    catch (error) { if (error.name === "AbortError") throw error; throw new Error("请求未完成，请检查网络后重试。你的本页草稿仍保留。"); }
    let result;
    try { result = await response.json(); } catch (_) { throw new Error("服务器未返回有效结果，请稍后重试。你的本页草稿仍保留。"); }
    if (!response.ok) {
      const detail = typeof result.detail === "string" ? result.detail : result.detail?.message;
      const error = new Error(detail || result.error || result.message || `请求失败（${response.status}）`);
      error.status = response.status; throw error;
    }
    return result;
  }
  const getDraft = item => {
    const current = records.get(item.id);
    if (current && current.version > item.version) item = current;
    const existing = drafts.get(item.id);
    if (!existing || (existing.version !== item.version && !existing.dirty && !selected.has(item.id))) drafts.set(item.id, newDraft(item));
    else if (!existing.dirty && !busy.has(item.id) && !locked.has(item.id) && !selected.has(item.id)
      && existing.version === item.version && existing.contact?.contact_id === item.contact?.contact_id
      && existing.contact?.open_id === item.contact?.open_id) existing.contact = item.contact || null;
    return drafts.get(item.id);
  };
  function changed(draft) { draft.dirty = true; draft.revision++; draft.request = null; draft.confirmRequest = null; }
  function identityChanged(draft, row) {
    changed(draft);
    row.dispatchEvent(new Event("workflow-draft-change"));
  }
  function updateSelected() {
    if (!writable) return;
    const control = $("cw-confirm-selected");
    control.textContent = `确认所选（${selected.size}）`; control.disabled = selected.size === 0 || busy.size > 0;
  }
  function labelInput(label, input) {
    const wrapper = element("label", "", "cw-inline-label"); wrapper.append(element("span", label), input); return wrapper;
  }
  function renderUser(parent, draft, editable) {
    const block = element("div"), selectedUser = element("div"), search = element("div", "", "cw-search");
    parent.append(block); block.append(selectedUser, search);
    let controller = null, generation = 0, timer = null;
    function openSearch() { draft.searchOpen = true; search.hidden = false; showUser(); input.focus(); }
    function chooseUser(contact) {
      if (locked.has(parent.closest("tr").dataset.candidateId)) return;
      generation++; clearTimeout(timer); controller?.abort();
      draft.contact = contact; draft.searchOpen = !contact;
      input.value = ""; results.replaceChildren(); status.textContent = "";
      const row = parent.closest("tr");
      identityChanged(draft, row); showUser(); search.hidden = !draft.searchOpen;
      if (!contact) input.focus();
    }
    function showUser() {
      selectedUser.replaceChildren();
      const contact = draft.contact;
      if (contact) {
        const info = element("div", "", "cw-user");
        if (contact.avatar_url) {
          try {
            const url = new URL(contact.avatar_url, location.origin);
            if (url.origin === location.origin || url.protocol === "https:") {
              const avatar = element("img", "", "cw-avatar"); avatar.src = url.href; avatar.alt = "";
              avatar.loading = "lazy"; avatar.referrerPolicy = "no-referrer"; avatar.addEventListener("error", () => avatar.remove()); info.append(avatar);
            }
          } catch (_) { /* A missing or malformed avatar is not missing user identity. */ }
        }
        const text = element("div");
        text.append(element("span", contact.display_name || "—", "cw-user-name"),
          element("span", contact.identity_hint || contact.open_id || contact.contact_id, "cw-small"));
        info.append(text); selectedUser.append(info);
      } else selectedUser.append(element("span", "—", "cw-user-name"));
      if (editable) {
        const actions = element("div", "", "cw-user-tools");
        const open = button(contact ? "更换用户" : "选择用户", openSearch, "cw-link");
        open.setAttribute("aria-expanded", String(draft.searchOpen)); actions.append(open);
        if (contact) actions.append(button("清除", () => chooseUser(null), "cw-link"));
        selectedUser.append(actions);
      }
    }
    const input = element("input", "", "form-control"), results = element("ul", "", "cw-search-results");
    const status = element("div", "", "cw-search-status");
    input.type = "search"; input.autocomplete = "off"; input.maxLength = 80;
    input.placeholder = scope.test_editor ? `搜索 ${scope.responsible_name} 的 Lark 用户` : "搜索我的 Lark 用户";
    input.setAttribute("aria-label", input.placeholder);
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    if (editable) {
      search.append(input, status, results, button("收起搜索", () => {
        generation++; clearTimeout(timer); controller?.abort(); draft.searchOpen = false;
        search.hidden = true; results.replaceChildren(); status.textContent = ""; showUser();
        selectedUser.querySelector("button")?.focus();
      }, "cw-link")); search.hidden = !draft.searchOpen;
      input.addEventListener("input", () => {
        clearTimeout(timer); controller?.abort(); const stamp = ++generation;
        results.replaceChildren(); status.textContent = ""; status.classList.remove("cw-error");
        const query = input.value.trim(); if (!query) return;
        timer = setTimeout(async () => {
          controller = new AbortController(); status.textContent = "正在搜索…";
          try {
            const response = await api("/users", {params: {q: query}, signal: controller.signal});
            if (generation !== stamp || !block.isConnected) return;
            status.textContent = response.items.length ? "请选中具体用户；只输入姓名不会关联。" : "未找到可选用户，仍保留为待确认。";
            for (const contact of response.items) {
              const choice = button("", () => chooseUser(contact), "");
              choice.disabled = locked.has(parent.closest("tr").dataset.candidateId);
              choice.append(element("strong", contact.display_name || "—"), element("div", contact.identity_hint || contact.open_id || contact.contact_id, "cw-small"));
              const item = element("li"); item.append(choice); results.append(item);
            }
          } catch (error) {
            if (error.name !== "AbortError" && generation === stamp && block.isConnected) {
              status.textContent = `搜索失败：${error.message}`; status.classList.add("cw-error");
            }
          }
        }, 250);
      });
    } else search.hidden = true;
    showUser();
  }
  function renderRow(item) {
    const row = element("tr"); row.dataset.candidateId = item.id;
    const editable = writable && pending(item), draft = getDraft(item);
    const supplement = writable && item.status === "confirmed" && (!item.handle || !item.creator_type);
    row.setAttribute("aria-busy", String(busy.has(item.id)));
    if (writable) {
      const cell = element("td"), input = element("input", "", "form-check-input");
      input.type = "checkbox"; input.checked = selected.has(item.id); input.disabled = !editable;
      input.setAttribute("aria-label", `选择 ${draft.handle || "待补 handle 的记录"}，用于批量确认`);
      input.addEventListener("change", () => { input.checked ? selected.add(item.id) : selected.delete(item.id); updateSelected(); }); cell.append(input); row.append(cell);
    }
    const handleCell = element("td"), typeCell = element("td", "", "cw-type"), sourceCell = element("td"), userCell = element("td"), statusCell = element("td"), actionCell = element("td");
    row.append(handleCell, typeCell, sourceCell, userCell, statusCell, actionCell);
    handleCell.append(element("div", draft.handle ? `@${draft.handle.replace(/^@/, "")}` : "—", "cw-handle"), element("span", item.platform || "—", "cw-small"));
    if (editable || (supplement && !item.handle)) {
      const input = element("input", "", "form-control"); input.value = draft.handle; input.maxLength = 80;
      input.placeholder = "填写 handle"; input.autocomplete = "off";
      const editor = labelInput("达人 handle", input); editor.hidden = !draft.handleOpen;
      const open = button(draft.handle ? "修改 handle" : "填写 handle", () => {
        draft.handleOpen = !draft.handleOpen; editor.hidden = !draft.handleOpen;
        open.setAttribute("aria-expanded", String(draft.handleOpen)); if (draft.handleOpen) input.focus();
      }, "cw-link");
      open.setAttribute("aria-expanded", String(draft.handleOpen));
      input.addEventListener("input", () => {
        draft.handle = input.value; identityChanged(draft, row);
        handleCell.querySelector(".cw-handle").textContent = input.value.trim() ? `@${input.value.replace(/^@/, "")}` : "—";
      });
      handleCell.append(open, editor);
    }
    if (editable || (supplement && !item.creator_type)) {
      const select = element("select", "", "form-select");
      select.setAttribute("aria-label", "达人类型");
      for (const [value, label] of [["", "—（未确定）"], ["cap", "CAP"], ["non_cap", "非 CAP"]]) {
        const option = element("option", label); option.value = value; select.append(option);
      }
      select.value = draft.creator_type || "";
      select.addEventListener("change", () => {
        if (locked.has(item.id)) { select.value = draft.creator_type || ""; return; }
        draft.creator_type = select.value || null; identityChanged(draft, row);
      });
      typeCell.append(select);
    } else typeCell.append(element("span", {cap: "CAP", non_cap: "非 CAP"}[draft.creator_type] || "—"));
    if (!(item.sources || []).length) sourceCell.append(element("span", "—"));
    for (const source of item.sources || []) sourceCell.append(element("span", source === "excel" ? "Excel" : source === "ai" ? "AI" : source, `cw-pill cw-pill--${source === "excel" ? "excel" : "ai"}`));
    renderUser(userCell, draft, editable);
    statusCell.append(element("span", item.status_label || (item.status === "confirmed" ? "已确认入库" : item.status === "deferred" ? "待确认 · 暂缓" : "待确认"), `cw-pill cw-pill--${item.status === "confirmed" ? "confirmed" : "pending"}`));
    const editStatus = element("div", "", "cw-small"); statusCell.append(editStatus);
    const updateEditStatus = () => { editStatus.textContent = draft.dirty ? item.status === "confirmed" ? "有未保存的补充资料。" : "有未保存的修改，确认时会一并保存。" : ""; };
    row.addEventListener("workflow-draft-change", updateEditStatus); updateEditStatus();
    if ((item.blocking_reasons || []).length) {
      const reasons = element("ul", "", "cw-reasons");
      for (const reason of item.blocking_reasons) reasons.append(element("li", reason.label || reason.code));
      const updateReasons = () => { reasons.hidden = draft.dirty; };
      row.addEventListener("workflow-draft-change", updateReasons); updateReasons();
      statusCell.append(reasons);
    }
    if (editable) {
      const actions = element("div", "", "cw-row-actions");
      const confirm = button("确认入库", () => rowAction(item, "confirm"), "btn btn-primary");
      const checkReady = () => { confirm.disabled = busy.has(item.id) || !scope.responsible_id || missingFields(draft).length > 0; };
      for (const event of ["input", "change", "workflow-draft-change"]) row.addEventListener(event, checkReady);
      checkReady();
      actions.append(confirm, button("保存草稿", () => rowAction(item, "draft")),
        button(scope.test_editor ? `不由 ${scope.responsible_name} 负责` : "不是我负责", () => { if (window.confirm(scope.test_editor ? `这条记录将交回管理员核对，不会自动转给其他 CM。确定不由 ${scope.responsible_name} 负责？` : "这条记录将交回管理员核对，不会自动转给其他 CM。确定不是你负责？")) rowAction(item, "reject"); }),
        button("暂缓", () => rowAction(item, "defer")),
        button("重新载入此行", async () => { if (!draft.dirty || window.confirm("重新载入会放弃此行尚未保存的补充资料，继续？")) { drafts.delete(item.id); selected.delete(item.id); await load(); } }, "cw-link"));
      actionCell.append(actions);
      if (draft.version !== item.version) actionCell.append(element("p", "服务器资料已变化。草稿仍保留，请重新载入此行后核对。", "cw-row-feedback cw-error"));
    } else if (supplement) {
      const actions = element("div", "", "cw-row-actions");
      const saveExtra = button("保存补充", () => rowAction(item, "draft"), "btn btn-outline-primary");
      const checkExtra = () => { saveExtra.disabled = busy.has(item.id) || !draft.dirty; };
      row.addEventListener("workflow-draft-change", checkExtra); checkExtra();
      actions.append(saveExtra,
        button("重新载入此行", async () => { if (!draft.dirty || window.confirm("重新载入会放弃此行尚未保存的补充资料，继续？")) { drafts.delete(item.id); await load(); } }, "cw-link"),
        element("span", "仅补缺失资料，不变更已确认的用户和负责人。", "cw-small"));
      actionCell.append(actions);
    } else if (scope.can_admin && !item.responsible_id && ["pending", "deferred", "rejected"].includes(item.status)) {
      const target = element("select", "", "form-select"), placeholder = element("option", "—"); placeholder.value = ""; target.append(placeholder);
      for (const responsible of responsibles) { const option = element("option", responsible.display_name); option.value = responsible.id; target.append(option); }
      target.setAttribute("aria-label", "选择接收待确认记录的 CM");
      const assign = button("交给 CM 核对", () => assignCandidate(item, target.value), "btn btn-outline-primary"); assign.disabled = true;
      target.addEventListener("change", () => { assign.disabled = !target.value; draft.assignRequest = null; });
      const controls = element("div", "", "cw-row-actions"); controls.append(target, assign, element("span", "只转交待确认记录，不代替 CM 确认入库。", "cw-small")); actionCell.append(controls);
    } else actionCell.append(element("span", item.status === "confirmed" ? "已完成" : "仅核对", "cw-small"));
    const message = element("div", "", "cw-row-feedback"); message.dataset.rowFeedback = "true"; message.setAttribute("role", "status"); actionCell.append(message);
    if (busy.has(item.id)) for (const node of row.querySelectorAll("button")) node.disabled = true;
    if (locked.has(item.id)) setRowLocked(row, true);
    return row;
  }
  function render() {
    const summary = listing.summary || {};
    for (const key of ["total", "pending", "confirmed"]) $("cw-" + key).textContent = summary[key] ?? "—";
    $("cw-visible-count").textContent = `共 ${listing.total ?? 0} 条`;
    const rows = $("cw-rows"); rows.replaceChildren();
    for (const item of listing.items || []) { records.set(item.id, item); rows.append(renderRow(item)); }
    $("cw-empty").hidden = (listing.items || []).length !== 0;
    $("cw-prev").disabled = page <= 1; $("cw-next").disabled = page >= (listing.pages || 1);
    $("cw-page-count").textContent = `第 ${page} / ${listing.pages || 1} 页`;
    updateSelected();
  }
  async function load() {
    const sequence = ++loading; $("cw-refresh").disabled = true;
    try {
      const params = Object.fromEntries(new FormData(form)); params.page = page;
      const result = await api("/data", {params});
      if (sequence !== loading) return;
      listing = result; page = result.page || 1; render(); $("cw-list-stale").hidden = true;
    } catch (error) { if (sequence === loading) feedback(error.message, true); }
    finally { if (sequence === loading) $("cw-refresh").disabled = false; }
  }
  function rowMessage(id, text, error = false) {
    const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === id);
    if (row) feedback(text, error, row.querySelector("[data-row-feedback]"));
  }
  function setRowLocked(row, value) {
    for (const input of row.querySelectorAll("input,select,button")) {
      if (value && input.dataset.cwLocked === undefined) { input.dataset.cwLocked = String(input.disabled); input.disabled = true; }
      else if (!value && input.dataset.cwLocked !== undefined) { input.disabled = input.dataset.cwLocked === "true"; delete input.dataset.cwLocked; }
    }
  }
  function lockRowInputs(id, value) {
    value ? locked.add(id) : locked.delete(id);
    const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === id);
    if (row) setRowLocked(row, value);
  }
  async function assignCandidate(item, responsibleId) {
    if (!scope.can_admin || !responsibleId || busy.has(item.id)) return;
    busy.add(item.id); lockRowInputs(item.id, true); rowMessage(item.id, "正在转交待确认记录…");
    const draft = getDraft(item);
    if (!draft.assignRequest) draft.assignRequest = requestId();
    try {
      await api(`/${encodeURIComponent(item.id)}/assign`, {body: {version: item.version, responsible_id: responsibleId, request_id: draft.assignRequest}});
      drafts.delete(item.id); await load(); feedback("已交给所选 CM 核对，仍为待确认，没有正式入库。");
    } catch (error) { rowMessage(item.id, error.message, true); }
    finally { busy.delete(item.id); lockRowInputs(item.id, false); }
  }
  async function save(item) {
    const draft = getDraft(item), revision = draft.revision;
    if (!draft.request) draft.request = requestId();
    const result = await api(`/${encodeURIComponent(item.id)}/draft`, {body: draftBody(draft, draft.request)});
    const updated = result.item || result;
    if (updated.id !== item.id) {
      drafts.delete(item.id); selected.delete(item.id); records.set(updated.id, updated);
      drafts.set(updated.id, newDraft(updated));
      await load();
      throw new Error("相同 handle 的来源已合并到已有记录。请在合并后的记录上确认入库。");
    }
    // Keep the same object so late saves do not detach active input listeners.
    Object.assign(draft, savedDraft(draft, updated, revision));
    drafts.set(item.id, draft); records.set(item.id, updated);
    return updated;
  }
  async function perform(item, action) {
    let draft = getDraft(item);
    if (action === "draft") {
      if (item.status === "confirmed") lockRowInputs(item.id, true);
      await save(item);
      return getDraft(item).dirty ? "已保存提交时的资料；你刚修改的内容仍未保存。" : item.status === "confirmed" ? "补充资料已保存，正式归属未变。" : "草稿已保存，尚未入库。";
    }
    if (action === "confirm") {
      if (!scope.responsible_id) throw new Error("请先明确这位达人的负责 CM。");
      const missing = missingFields(draft);
      if (missing.length) throw new Error(`还不能入库，请补齐：${missing.join("、")}。`);
      lockRowInputs(item.id, true);
      if (draft.dirty) { item = await save(item); draft = getDraft(item); }
      // New edits during a save are intentionally not included in this confirmation.
      if (draft.dirty) throw new Error("保存期间资料又有变化，已保留，请再次确认入库。");
      const missingAfterSave = missingFields(draft);
      if (missingAfterSave.length) throw new Error(`还需补齐：${missingAfterSave.join("、")}。`);
      if (!draft.confirmRequest) draft.confirmRequest = requestId();
      await api(`/${encodeURIComponent(item.id)}/confirm`, {body: {version: draft.version, request_id: draft.confirmRequest}});
    } else {
      lockRowInputs(item.id, true);
      if (!draft.decisionRequest || draft.decisionAction !== action) { draft.decisionRequest = requestId(); draft.decisionAction = action; }
      await api(`/${encodeURIComponent(item.id)}/decision`, {body: {version: draft.version, action, request_id: draft.decisionRequest}});
    }
    drafts.delete(item.id); selected.delete(item.id);
    return action === "confirm" ? "已确认入库。" : action === "reject" ? "已交回管理员核对。" : "已暂缓，仍在待确认。";
  }
  async function rowAction(item, action) {
    if (!writable || busy.has(item.id)) return;
    busy.add(item.id); updateSelected(); rowMessage(item.id, action === "confirm" ? "正在确认入库…" : "正在保存…");
    try { const message = await perform(item, action); await load(); rowMessage(item.id, message); feedback(message); }
    catch (error) { rowMessage(item.id, error.message, true); }
    finally { busy.delete(item.id); lockRowInputs(item.id, false); updateSelected(); const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === item.id); if (row) { row.setAttribute("aria-busy", "false"); for (const node of row.querySelectorAll("button")) node.disabled = false; row.dispatchEvent(new Event("workflow-draft-change")); } }
  }
  $("cw-refresh").addEventListener("click", load);
  form.addEventListener("submit", event => { event.preventDefault(); page = 1; load(); });
  for (const select of form.querySelectorAll("select")) select.addEventListener("change", () => { page = 1; load(); });
  $("cw-prev").addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
  $("cw-next").addEventListener("click", () => { page++; load(); });
  if (scope.responsible_id || writable || canScan || scope.can_import) {
    if (writable) $("cw-confirm-selected").addEventListener("click", async () => {
      if (!selected.size || !window.confirm(`确认将所选 ${selected.size} 条记录入库？用户或负责人不明确、存在冲突的记录会保留待确认；handle 和类型可稍后补充。`)) return;
      const ids = [...selected], results = $("cw-batch-results"); results.replaceChildren(); results.hidden = false;
      for (const id of ids) busy.add(id); updateSelected();
      for (const id of ids) {
        const item = records.get(id), line = element("div"); results.append(line);
        try { const message = await perform(item, "confirm"); line.textContent = `${item.handle ? "@" + item.handle : "—"}：${message}`; }
        catch (error) { line.textContent = `${item?.handle ? "@" + item.handle : "—"}：${error.message}`; line.classList.add("cw-error"); }
        busy.delete(id); lockRowInputs(id, false);
      }
      await load(); updateSelected();
    });
    const retryAI = canScan ? button("重试未完成", () => startAI(true), "btn btn-outline-primary") : null;
    if (retryAI) { retryAI.hidden = true; retryAI.id = "cw-ai-retry"; $("cw-ai-scan").after(retryAI); }
    const initialRetry = $("cw-initial-retry");
    function showInitial(initial, result, running) {
      const panel = $("cw-initial-scan");
      if (!panel) return;
      panel.hidden = !result;
      if (!result) return;
      const stopped = [initial?.status, result.status].some(status => ["failed", "partial", "cancelled"].includes(status));
      const status = stopped ? "failed" : initial?.status || result.status;
      const labels = {waiting_sync: "等待准备", queued: "扫描中", running: "扫描中", scanning: "扫描中", succeeded: initial ? "全部已完成" : "本批已完成", failed: "整体未完成"};
      const messages = {
        waiting_sync: "等待同步与 AI 配置就绪后再开始；不会把没有数据当成没有达人。",
        scanning: "正在分批检查当前授权下已同步的历史业务对话，不限最近消息。扫描完成前，名单只是部分结果。",
        succeeded: "历史扫描已完成，请核对名单。无候选不代表不存在达人；未确定的 handle 和类型显示“—”。",
        failed: "整体扫描未完成，后续会话未继续；已有结果已保留。刷新只读取状态，不会重试失败任务。",
      };
      $("cw-initial-status").textContent = labels[status] || "待核对";
      $("cw-initial-message").textContent = stopped ? messages.failed : initial?.error_message || messages[status] || messages.scanning;
      $("cw-initial-message").classList.toggle("cw-error", stopped);
      $("cw-initial-counts").textContent = initial ? `会话检查与分批安排：${initial.checked_chats ?? 0} / ${initial.total_chats ?? 0} · 累计排队 ${initial.queued_chats ?? 0} 个消息段。这不是 AI 完成比例；本批处理期间，会话数可能保持不变。` : "下方为当前批次的消息段处理情况，不代表全部历史已处理。";
      const countLabels = {succeeded: "成功", queued: "排队", running: "进行中", invalid: "校验未通过", failed: "执行异常", cancelled: "已取消"};
      const counts = Object.fromEntries(Object.keys(countLabels).map(key => [key, Math.max(0, Number(result.counts?.[key]) || 0)]));
      const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
      const processed = counts.succeeded + counts.invalid + counts.failed + counts.cancelled;
      for (const key of Object.keys(countLabels)) $("cw-scan-" + key).textContent = counts[key];
      $("cw-batch-summary").hidden = total === 0;
      $("cw-batch-label").textContent = `本批成功 ${counts.succeeded} / ${total} 段（${total ? Math.floor(counts.succeeded / total * 100) : 0}%）`;
      const progress = $("cw-batch-progress"); progress.max = total || 1; progress.value = counts.succeeded;
      progress.classList.toggle("cw-progress-incomplete", stopped || counts.invalid + counts.failed + counts.cancelled > 0);
      $("cw-batch-processed").textContent = `本批已处理 ${processed} / ${total} 段 · 待处理 ${counts.queued + counts.running} 段。已处理包括未成功的结果，不等于全部历史扫描完成。`;
      const dispatch = result.dispatch;
      $("cw-scan-dispatch").textContent = dispatch ? `本批正在执行 ${dispatch.running_count ?? "—"} 段 · 全系统 AI 队列共享最多 ${dispatch.max_concurrency ?? "—"} 个并行任务。${{ready: "队列正常。", paused: "队列已暂停，请管理员检查。", throttled: "队列限速等待中。"}[dispatch.status] || "队列状态待核对。"}这是应用并发上限，不是 OpenAI 账户配额。` : "并发状态暂未返回；无法据此判断 OpenAI 是否异常。";
      const details = result.error_summary?.length ? result.error_summary : (result.errors || []).map(error => ({message: typeof error === "string" ? error : error.message || error.error}));
      const errorList = $("cw-scan-error-list"); errorList.replaceChildren();
      for (const group of details) {
        let message = group.message || "没有保留具体错误信息。";
        if (message.includes("AI 扫描未完成；未生成候选，请检查服务状态。")) message += " 历史记录未保留具体异常类型。";
        errorList.append(element("li", `${countLabels[group.status] || "历史提示"}${group.count == null ? "（未提供次数）" : ` · ${group.count} 段`}：${message}`));
      }
      if (!details.length && counts.invalid + counts.failed + counts.cancelled > 0) errorList.append(element("li", "本批存在未成功的消息段，接口尚未提供具体错误分类。"));
      $("cw-scan-errors").hidden = errorList.children.length === 0;
      $("cw-initial-limit").textContent = `扫描本轮开始前已同步、当前授权可读的历史；每段 ${initial?.message_limit ?? 80} 条，相邻重叠 ${initial?.message_overlap ?? 20} 条。每条最多 ${initial?.message_char_limit ?? 1500} 字符；未同步消息、图片和附件内容不在本次范围内。`;
      if (initialRetry) { initialRetry.hidden = !["failed", "cancelled"].includes(initial?.status); initialRetry.disabled = running; }
    }
    async function startAI(retryFailed = false) {
      if (!canScan) return;
      ++aiLoading; clearTimeout(aiTimer); showAIProgress = true;
      const control = $("cw-ai-scan"); control.disabled = true;
      if (retryAI) retryAI.hidden = true;
      try {
        const result = await api("/ai/scan", {body: {request_id: requestId(), retry_failed: retryFailed}});
        feedback(result.message || "历史业务对话扫描已提交。明确是达人且由当前 CM 负责的记录可入库；不确定的留待确认，handle 可后补。");
        pollAI();
      } catch (error) { feedback(error.message, true); control.disabled = false; }
    }
    if (canScan) $("cw-ai-scan").addEventListener("click", () => startAI());
    async function pollAI() {
      if (!scope.responsible_id) return;
      if (aiReading) { aiPollAgain = true; return; }
      aiReading = true;
      clearTimeout(aiTimer);
      const sequence = ++aiLoading;
      const refresh = $("cw-initial-refresh");
      if (refresh) { refresh.disabled = true; refresh.textContent = "读取中…"; refresh.setAttribute("aria-busy", "true"); }
      try {
        const response = await api("/ai/status");
        if (sequence !== aiLoading) return;
        const result = response.run;
        const initial = result?.onboarding;
        const running = result && ["queued", "running"].includes(result.status);
        const initialActive = initial && ["waiting_sync", "scanning"].includes(initial.status);
        showInitial(initial, result, running);
        $("cw-initial-updated").textContent = `上次更新：${new Date().toLocaleTimeString("en-GB", {timeZone: "Europe/London"})}（英国时间）`;
        if (canScan) $("cw-ai-scan").disabled = Boolean(running || initialActive);
        if (!result) { if (showAIProgress) feedback("当前没有正在运行的候选分析。"); if (retryAI) retryAI.hidden = true; return; }
        const errors = (result.errors || []).map(error => typeof error === "string" ? error : error.message || error.error || "部分候选分析失败");
        const active = running || initialActive;
        if (showAIProgress) feedback([result.message || (active ? "AI 正在核对历史业务对话与 CM 关系；不明确的记录保留待确认。" : "本轮候选分析已结束，请核对名单和待确认项。"), ...errors].join("；"), errors.length > 0 || ["failed", "partial"].includes(result.status));
        if (retryAI) retryAI.hidden = Boolean(initialActive || ["failed", "cancelled"].includes(initial?.status)) || !(errors.length || ["failed", "partial"].includes(result.status));
        const terminal = status => ["succeeded", "failed", "partial", "cancelled"].includes(status) ? status : "";
        const progress = JSON.stringify([result.candidate_count, terminal(result.status), terminal(initial?.status)]);
        const changed = lastAIProgress !== null && lastAIProgress !== progress;
        lastAIProgress = progress;
        if (changed || (showAIProgress && !active)) {
          if ([...drafts.values()].some(draft => draft.dirty)) $("cw-list-stale").hidden = false;
          else await load();
        }
        if (sequence !== aiLoading) return;
        if (active) aiTimer = setTimeout(pollAI, 5000);
        else showAIProgress = false;
      } catch (error) {
        if (sequence !== aiLoading) return;
        const panel = $("cw-initial-scan");
        if (panel) {
          panel.hidden = false; $("cw-initial-status").textContent = "进度读取失败";
          $("cw-initial-message").textContent = `暂时未能读取分析进度：${error.message} 请点击“刷新进度”，不会重新启动分析。`;
          $("cw-initial-message").classList.add("cw-error");
        }
        if (canScan) $("cw-ai-scan").disabled = false;
      } finally {
        aiReading = false;
        if (refresh) { refresh.disabled = false; refresh.textContent = "刷新进度"; refresh.setAttribute("aria-busy", "false"); }
        if (aiPollAgain) { aiPollAgain = false; pollAI(); }
      }
    }
    $("cw-initial-refresh")?.addEventListener("click", pollAI);
    if (initialRetry) initialRetry.addEventListener("click", async () => {
      if (!canScan || initialRetry.disabled) return;
      ++aiLoading; clearTimeout(aiTimer); initialRetry.disabled = true;
      try {
        await api("/ai/initial-retry", {body: {request_id: requestId()}});
        await pollAI();
      } catch (error) {
        $("cw-initial-message").textContent = error.message; $("cw-initial-message").classList.add("cw-error");
        initialRetry.disabled = false;
      }
    });
    if (scope.responsible_id) pollAI();
    if (scope.can_import) {
      const dialog = $("cw-import-dialog"), fileInput = $("cw-import-file"), resultBox = $("cw-import-results");
      $("cw-import-open").addEventListener("click", () => dialog.showModal());
      $("cw-import-close").addEventListener("click", () => dialog.close());
      fileInput.addEventListener("change", () => { importPreview = null; resultBox.replaceChildren(); $("cw-import-commit").hidden = true; });
      function showImport(result) {
        resultBox.replaceChildren();
        if (result.summary) resultBox.append(element("p", `共 ${result.summary.total} 行 · 可提交 ${result.summary.ready + result.summary.pending} 行 · 无效 ${result.summary.invalid} 行；提交时重新核验 AI 匹配，确定是否自动入库。`, "cw-note"));
        else if (result.imported != null) resultBox.append(element("p", `已导入 ${result.imported} 行 · 正式入库 ${result.formal} 行 · 待确认 ${result.pending} 行 · 跳过 ${result.skipped} 行`, "cw-note"));
        const table = element("table", "", "cw-import-table"), head = element("tr"), body = element("tbody");
        for (const text of ["行", "handle", "结果", "说明"]) head.append(element("th", text));
        const thead = element("thead"); thead.append(head); table.append(thead, body);
        for (const row of result.items || result.rows || []) {
          const line = element("tr"), reasons = (row.reasons || []).map(reason => reason.label || reason).join("；");
          const statusLabels = {ready: "可提交", pending: result.summary ? "提交后匹配或确认" : "待确认", invalid: "无效行", confirmed: "已确认入库", skipped: "已跳过", error: "失败"};
          for (const value of [[row.sheet, row.row_number || row.row].filter(Boolean).join(" · ") || "—", row.handle || "—", row.status_label || statusLabels[row.status] || row.status || "—", row.message || reasons || "—"]) line.append(element("td", String(value)));
          body.append(line);
        }
        resultBox.append(table);
      }
      $("cw-import-form").addEventListener("submit", async event => {
        event.preventDefault(); if (!fileInput.files[0]) return;
        const control = $("cw-import-check"); control.disabled = true; $("cw-import-commit").hidden = true; importPreview = null;
        const file = fileInput.files[0];
        try {
          const result = await api("/import/preview", {body: file, params: {filename: file.name}});
          if (fileInput.files[0] !== file) return;
          importPreview = result; showImport(result); $("cw-import-commit").hidden = !result.batch_id;
          feedback(result.message || "预检完成。请检查逐项结果，再确认提交。", false, $("cw-import-feedback"));
        } catch (error) { feedback(error.message, true, $("cw-import-feedback")); }
        finally { control.disabled = false; }
      });
      $("cw-import-commit").addEventListener("click", async () => {
        if (!importPreview) return;
        const control = $("cw-import-commit"); control.disabled = true; fileInput.disabled = true; $("cw-import-check").disabled = true;
        if (!importPreview.request_id) importPreview.request_id = requestId();
        try {
          const result = await api(`/import/${encodeURIComponent(importPreview.batch_id)}/commit`, {body: {request_id: importPreview.request_id}});
          showImport(result); feedback(result.message || "导入完成，逐项结果如下。待确认项没有写入正式名单。", false, $("cw-import-feedback"));
          importPreview = null; control.hidden = true; await load();
        } catch (error) { feedback(error.message, true, $("cw-import-feedback")); }
        finally { control.disabled = false; fileInput.disabled = false; $("cw-import-check").disabled = false; }
      });
    }
  }
  window.addEventListener("beforeunload", event => {
    if ([...drafts.values()].some(draft => draft.dirty)) { event.preventDefault(); event.returnValue = ""; }
  });
  render();
})();
