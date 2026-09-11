/* Real data stays in this page's memory. The server rechecks scope, identity and version. */
(() => {
  "use strict";
  const pending = item => ["pending", "deferred"].includes(item.status);
  function newDraft(item) {
    return {version: item.version, handle: item.handle || "", contact: item.contact || null,
      handle_verified: item.handle_verified === true,
      creator_type: ["cap", "non_cap"].includes(item.creator_type) ? item.creator_type : null,
      dirty: false, revision: 0, request: null, confirmRequest: null, searchOpen: false, handleOpen: false};
  }
  function missingFields(draft) {
    const missing = [];
    if (!draft.contact?.contact_id) missing.push("对应用户（请从搜索结果中选中）");
    if (!draft.handle.trim()) missing.push("达人 handle");
    else if (!draft.handle_verified) missing.push("核实 handle 属于当前用户");
    return missing;
  }
  function aiReady(item, draft) {
    return item.stages?.identity?.verdict === "pass" && item.stages?.ownership?.verdict === "pass"
      && Boolean(draft.contact?.contact_id) && draft.contact.contact_id === item.contact?.contact_id;
  }
  function readyToConfirm(item, draft) {
    return pending(item) && aiReady(item, draft) && missingFields(draft).length === 0
      && (item.can_confirm === true || (draft.dirty && (item.blocking_reasons || []).every(reason =>
        ["missing_handle", "handle_verification_required"].includes(reason.code))));
  }
  function draftBody(draft, requestId) {
    return {version: draft.version, handle: draft.handle.trim() || null,
      contact_id: draft.contact?.contact_id || null, handle_verified: draft.handle_verified === true,
      creator_type: draft.creator_type || null, request_id: requestId};
  }
  // A late save updates the revision token, never overwrites edits made during that save.
  function savedDraft(draft, item, sentRevision) {
    if (draft.revision === sentRevision) {
      const fresh = newDraft(item);
      return {...fresh, searchOpen: draft.searchOpen, handleOpen: draft.handleOpen};
    }
    return {...draft, version: item.version, dirty: true, request: null};
  }
  const exported = {pending, newDraft, missingFields, draftBody, savedDraft, readyToConfirm};
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
  let aiRun = null, aiSubmitting = false, previewKey = null;
  const form = $("cw-filters");
  const urlParams = new URLSearchParams(location.search);
  form.elements.status.value = data.status || urlParams.get("status") || (scope.responsible_id ? "confirmed" : "pending");
  if (!form.elements.status.value) form.elements.status.value = "confirmed";
  form.elements.reason.value = data.reason || urlParams.get("reason") || "";
  form.elements.source.value = data.source || urlParams.get("source") || "";
  form.elements.query.value = data.query || urlParams.get("q") || urlParams.get("query") || "";
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
      if (draft.contact?.contact_id !== contact?.contact_id) draft.handle_verified = false;
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
    const editable = writable && (pending(item) || item.status === "confirmed"), draft = getDraft(item);
    row.setAttribute("aria-busy", String(busy.has(item.id)));
    if (writable) {
      const cell = element("td"), input = element("input", "", "form-check-input");
      input.type = "checkbox"; input.checked = selected.has(item.id); input.disabled = !editable || !pending(item);
      input.setAttribute("aria-label", `选择 ${draft.handle || "待补 handle 的记录"}，用于批量确认`);
      input.addEventListener("change", () => { input.checked ? selected.add(item.id) : selected.delete(item.id); updateSelected(); }); cell.append(input); row.append(cell);
    }
    const handleCell = element("td"), typeCell = element("td", "", "cw-type"), sourceCell = element("td"), userCell = element("td"), statusCell = element("td"), actionCell = element("td");
    row.append(handleCell, typeCell, sourceCell, userCell, statusCell, actionCell);
    handleCell.append(element("div", draft.handle ? `@${draft.handle.replace(/^@/, "")}` : "—", "cw-handle"), element("span", item.platform || "—", "cw-small"));
    if (editable) {
      const input = element("input", "", "form-control"); input.value = draft.handle; input.maxLength = 80;
      input.placeholder = "填写 handle"; input.autocomplete = "off";
      const editor = labelInput("达人 handle", input); editor.hidden = !draft.handleOpen;
      const open = button(draft.handle ? "修改 handle" : "填写 handle", () => {
        draft.handleOpen = !draft.handleOpen; editor.hidden = !draft.handleOpen;
        open.setAttribute("aria-expanded", String(draft.handleOpen)); if (draft.handleOpen) input.focus();
      }, "cw-link");
      open.setAttribute("aria-expanded", String(draft.handleOpen));
      input.addEventListener("input", () => {
        draft.handle = input.value; draft.handle_verified = false; identityChanged(draft, row);
        handleCell.querySelector(".cw-handle").textContent = input.value.trim() ? `@${input.value.replace(/^@/, "")}` : "—";
      });
      handleCell.append(open, editor);
      const verified = element("input", "", "form-check-input"); verified.type = "checkbox";
      verified.setAttribute("aria-label", "我已核实 handle 属于当前 Lark 用户");
      const verification = labelInput("我已核实 handle 属于当前 Lark 用户", verified);
      verification.classList.add("cw-handle-verification");
      const syncVerification = () => {
        verified.checked = draft.handle_verified;
        verified.disabled = locked.has(item.id) || !draft.handle.trim() || !draft.contact?.contact_id;
      };
      verified.addEventListener("change", () => {
        if (locked.has(item.id)) return syncVerification();
        draft.handle_verified = verified.checked; identityChanged(draft, row);
      });
      row.addEventListener("workflow-draft-change", syncVerification); syncVerification();
      handleCell.append(verification);
    }
    if (editable) {
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
    const stageLabels = {identity: "身份待判断", ownership: "归属待判断", handle: "待补或核实 handle", ready: "资料齐全 · 待确认"};
    const statusLabel = item.status === "confirmed" ? "已确认入库" : item.status === "rejected" ? "CM 已退回，待分配"
      : item.stages?.identity?.verdict === "fail" ? "不是达人 · 不纳入"
      : item.stages?.ownership?.verdict === "fail" ? "不由当前 CM 负责 · 不纳入"
      : `${stageLabels[item.processing_stage] || item.status_label || "待处理"}${item.status === "deferred" ? " · 暂缓" : ""}`;
    statusCell.append(element("span", statusLabel, `cw-pill cw-pill--${item.status === "confirmed" ? "confirmed" : "pending"}`));
    if (item.status !== "confirmed") statusCell.append(element("div", [
      ["identity", "身份"], ["ownership", "归属"], ["handle", "账号"]
    ].map(([stage, label]) => `${label}${{pass: "已通过", fail: "不符合"}[item.stages?.[stage]?.verdict] || "待判断"}`).join(" · "), "cw-small"));
    const editStatus = element("div", "", "cw-small"); statusCell.append(editStatus);
    const updateEditStatus = () => { editStatus.textContent = draft.dirty ? item.status === "confirmed" ? "有未保存的修改；更换账号或用户后会退出正式名单。保存或刷新不会自动重跑 AI。" : "有未保存的修改。更换用户后，原身份和归属判断失效；保存或刷新不会自动复核。" : ""; };
    row.addEventListener("workflow-draft-change", updateEditStatus); updateEditStatus();
    if ((item.blocking_reasons || []).length) {
      const reasons = element("ul", "", "cw-reasons");
      for (const reason of item.blocking_reasons) reasons.append(element("li", reason.label || reason.code));
      statusCell.append(reasons);
    }
    if (editable) {
      const actions = element("div", "", "cw-row-actions");
      if (pending(item)) {
        const confirm = button("确认入库", () => rowAction(item, "confirm"), "btn btn-primary");
        const checkReady = () => { confirm.disabled = busy.has(item.id) || !scope.responsible_id || !readyToConfirm(item, draft); };
        for (const event of ["input", "change", "workflow-draft-change"]) row.addEventListener(event, checkReady);
        checkReady(); actions.append(confirm);
      }
      actions.append(button(item.status === "confirmed" ? "保存修改" : "保存草稿", () => rowAction(item, "draft")));
      if (pending(item)) actions.append(
        button(scope.test_editor ? `不由 ${scope.responsible_name} 负责` : "不是我负责", () => { if (window.confirm(scope.test_editor ? `这条记录将交回管理员核对，不会自动转给其他 CM。确定不由 ${scope.responsible_name} 负责？` : "这条记录将交回管理员核对，不会自动转给其他 CM。确定不是你负责？")) rowAction(item, "reject"); }),
        button("暂缓", () => rowAction(item, "defer")));
      actions.append(button("重新载入此行", async () => { if (!draft.dirty || window.confirm("重新载入会放弃此行尚未保存的补充资料，继续？")) { drafts.delete(item.id); selected.delete(item.id); await load(); } }, "cw-link"));
      if (!aiReady(item, draft) && pending(item)) actions.append(element("span", "先保存资料，再准备核对并明确启动 AI；保存或刷新不会自动复核，不能直接确认入库。", "cw-small"));
      actionCell.append(actions);
      if (draft.version !== item.version) actionCell.append(element("p", "服务器资料已变化。草稿仍保留，请重新载入此行后核对。", "cw-row-feedback cw-error"));
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
    for (const key of ["pending", "confirmed"]) $("cw-" + key).textContent = summary[key] ?? "—";
    const official = form.elements.status.value === "confirmed";
    $("cw-list-title").textContent = official ? "我的达人" : form.elements.reason.value === "excluded" ? "不纳入 · 不计入达人或待处理人数" : "待处理";
    $("cw-show-confirmed").setAttribute("aria-pressed", String(official));
    $("cw-show-pending").setAttribute("aria-pressed", String(!official));
    $("cw-stage-filter").hidden = official;
    if ($("cw-batch-actions")) $("cw-batch-actions").hidden = official;
    $("cw-empty").textContent = official ? "暂无已入库达人。待判断或缺资料的人请到“待处理”查看。" : "没有符合条件的待处理记录。";
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
      const updated = await save(item);
      return getDraft(item).dirty ? "已保存提交时的资料；你刚修改的内容仍未保存。" : updated.status === "confirmed" ? "资料已保存，已通过入库校验。" : "草稿已保存，仍在待处理；没有启动 AI。需要复核时，请先准备核对，再明确启动；不能跳过 AI 判断。";
    }
    if (action === "confirm") {
      if (!scope.responsible_id) throw new Error("请先明确这位达人的负责 CM。");
      const missing = missingFields(draft);
      if (missing.length) throw new Error(`还不能入库，请补齐：${missing.join("、")}。`);
      if (!aiReady(item, draft)) throw new Error("AI 的达人身份和当前 CM 归属判断尚未通过，请先保存资料并完成分析。");
      lockRowInputs(item.id, true);
      if (draft.dirty) { item = await save(item); draft = getDraft(item); }
      // New edits during a save are intentionally not included in this confirmation.
      if (draft.dirty) throw new Error("保存期间资料又有变化，已保留，请再次确认入库。");
      const missingAfterSave = missingFields(draft);
      if (missingAfterSave.length) throw new Error(`还需补齐：${missingAfterSave.join("、")}。`);
      if (item.status === "confirmed") { drafts.delete(item.id); selected.delete(item.id); return "资料已保存并通过校验，已入库。"; }
      if (item.can_confirm !== true) throw new Error("后台入库校验尚未通过，记录仍在待处理。请检查身份、归属和账号是否完整。");
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
  for (const status of ["confirmed", "pending"]) $("cw-show-" + status).addEventListener("click", () => {
    form.elements.status.value = status; form.elements.reason.value = ""; selected.clear(); page = 1; load();
  });
  form.addEventListener("submit", event => { event.preventDefault(); page = 1; load(); });
  for (const select of form.querySelectorAll("select")) select.addEventListener("change", () => {
    if (select.name === "status") { form.elements.reason.value = ""; selected.clear(); }
    page = 1; load();
  });
  $("cw-prev").addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
  $("cw-next").addEventListener("click", () => { page++; load(); });
  if (scope.responsible_id || writable || canScan || scope.can_import) {
    if (writable) $("cw-confirm-selected").addEventListener("click", async () => {
      if (!selected.size || !window.confirm(`确认检查所选 ${selected.size} 条记录并入库？只有身份、归属两项 AI 已通过，且用户和已核实 handle 都齐全的记录才能入库。CAP 类型可以为空。`)) return;
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
    const activeRun = result => result?.targeted === true && ["queued", "running"].includes(result.status);
    const retryableRun = result => result?.targeted === true && ["failed", "cancelled"].includes(result.status) && result.retryable_people > 0;
    const previewVersion = result => JSON.stringify([result?.id, result?.request_count, result?.stages, result?.preflight_json?.preflight_revision]);
    function scanControls() {
      if (!canScan) return;
      $("cw-ai-scan").disabled = !scope.responsible_id || aiSubmitting || activeRun(aiRun);
      const start = $("cw-ai-start"), pause = $("cw-ai-pause");
      if (start) {
        start.hidden = aiRun?.targeted !== true || !(aiRun.status === "ready" || retryableRun(aiRun));
        start.textContent = retryableRun(aiRun) ? "继续未完成（限本批预算）" : aiRun?.resumed ? "开始本次续核（沿用预算）" : "开始 AI 核对（最多 30 人）";
        start.disabled = aiSubmitting || !aiRun?.total_people;
      }
      if (pause) {
        pause.hidden = aiRun?.targeted !== true || !["ready", "queued", "running"].includes(aiRun.status);
        pause.textContent = aiRun?.status === "ready" ? "取消本批准备" : "暂停";
        pause.disabled = aiSubmitting;
      }
    }
    function showInitial(result) {
      const panel = $("cw-initial-scan");
      if (!panel) return;
      panel.hidden = !result;
      if (!result) return;
      const targeted = result.targeted === true, status = result.status;
      const stopped = ["failed", "cancelled"].includes(status);
      const labels = {ready: "准备完成 · 尚未发送", queued: "等待 AI 核对", running: "核对中", succeeded: "本批核对完成", failed: "本批有未完成项", cancelled: "已停止"};
      const messages = {
        ready: "已在服务器准备好当前待判断阶段的片段，这次准备没有调用 AI。新用户从本人发言开始；有新资料的旧任务可能从未通过的阶段继续，已用预算不清零。请查看内容后再明确启动。",
        queued: "本批已排队，仅核对下方人数范围；不会继续扩大到整群或全部聊天。",
        running: "同一人按顺序进行：本人发言判断身份 → 双方沟通判断归属 → 核实 handle。前一步未通过就不继续；三步都通过、信息齐全且无冲突时自动入库。",
        succeeded: "本批分析已结束，不代表全部联系人已检查。符合条件的进入“我的达人”，缺项或不明确的留在“待处理”；有新资料时可重新准备核对，预算不清零，刷新不会自动继续。",
        failed: "部分用户尚未完成核对；已有结果保留。刷新只读取进度，不会重新调用 AI。",
        cancelled: "已停止安排新请求。已发出的请求无法撤回，其迟到结果不会入库；刷新不会恢复执行。",
      };
      $("cw-initial-status").textContent = targeted ? labels[status] || "待核对" : "旧扫描已停用";
      $("cw-initial-message").textContent = targeted ? messages[status] || "按身份、归属、账号顺序判断；缺项留在待处理。" : "原来的整段历史扫描不会恢复。请点击“准备核对”，先在服务器筛选用户本人历史发言。";
      $("cw-initial-message").classList.toggle("cw-error", targeted && stopped);
      $("cw-initial-counts").textContent = targeted ? `本批检查 ${result.total_people ?? 0} 人${result.resumed ? ` · 本次续核准备 ${result.prepared_people ?? 0} 人（沿用原批次）` : ""} · 本次预检片段 ${result.message_count ?? 0} 条消息、${result.char_count ?? 0} 字符（不是三阶段累计发送量） · 已用尝试 ${result.request_count ?? 0} / 上限 ${result.request_limit ?? 0} 次（含调用前校验失败与重试，不等于付费请求数）。` : "";
      const countLabels = {succeeded: "核对完成", queued: "等待核对", running: "正在核对", invalid: "证据未通过", failed: "执行异常", cancelled: "已停止"};
      const counts = Object.fromEntries(Object.keys(countLabels).map(key => [key, Math.max(0, Number(result.counts?.[key]) || 0)]));
      const total = result.total_people || Object.values(counts).reduce((sum, value) => sum + value, 0);
      const processed = counts.succeeded + counts.invalid + counts.failed + counts.cancelled;
      for (const key of Object.keys(countLabels)) $("cw-scan-" + key).textContent = counts[key];
      $("cw-batch-summary").hidden = !targeted || total === 0;
      $("cw-batch-label").textContent = `本批核对完成 ${counts.succeeded} / ${total} 人（${total ? Math.floor(counts.succeeded / total * 100) : 0}%）`;
      const progress = $("cw-batch-progress"); progress.max = total || 1; progress.value = counts.succeeded;
      progress.classList.toggle("cw-progress-incomplete", stopped || counts.invalid + counts.failed + counts.cancelled > 0);
      $("cw-batch-processed").textContent = `已结束 ${processed} / ${total} 人 · 等待或正在核对 ${counts.queued + counts.running} 人。已结束包含异常与停止项，不代表全部核对成功。`;
      const outcomes = result.outcomes || {};
      const waiting = ["identity_unknown", "ownership_unknown", "handle_missing", "handle_conflict", "review"].reduce((sum, key) => sum + (Number(outcomes[key]) || 0), 0);
      $("cw-scan-outcomes").textContent = targeted ? `本批入库 ${outcomes.confirmed ?? 0} 人 · 待处理 ${waiting} 人 · 不是达人 ${outcomes.not_creator ?? 0} 人 · 不由当前 CM 负责 ${outcomes.not_mine ?? 0} 人。名单上方显示的是累计入库与待处理人数，不是本批总数。` : "";
      const stages = result.stages || {};
      const stageCount = key => Object.values(stages[key] || {}).reduce((sum, value) => sum + (Number(value) || 0), 0);
      $("cw-scan-stages").textContent = targeted ? `当前停留阶段（含已结束）：身份 ${stageCount("identity")} 人 → 归属 ${stageCount("ownership")} 人 → handle ${stageCount("handle")} 人。每个人只进入前一步已通过的下一步，不把三步人数相加当作达人数量。` : "";
      const dispatch = result.dispatch;
      $("cw-scan-dispatch").textContent = !targeted ? "" : dispatch ? `本批正在核对 ${dispatch.running_count ?? 0} 人 · 不同人最多 ${dispatch.max_concurrency ?? 3} 路并行，同一个人的三步依次执行。${{ready: "队列正常。", paused: "队列已暂停，请管理员检查。", throttled: "队列限速等待中。"}[dispatch.status] || "队列状态待核对。"}` : "不同人最多 3 路并行，同一个人的三步依次执行；与系统其他 AI 任务共享名额。";
      const details = result.error_summary?.length ? result.error_summary : (result.errors || []).map(error => ({message: typeof error === "string" ? error : error.message || error.error}));
      const errorList = $("cw-scan-error-list"); errorList.replaceChildren();
      if (targeted) for (const group of details) {
        errorList.append(element("li", `${countLabels[group.status] || "提示"}${group.count == null ? "" : ` · ${group.count} 人`}：${group.message || "没有保留具体错误信息。"}`));
      }
      $("cw-scan-errors").hidden = errorList.children.length === 0;
      const limits = result.limits || {};
      $("cw-initial-limit").textContent = targeted ? `本批最多 ${limits.people ?? 30} 人；每人每阶段最多 ${limits.messages_per_stage ?? limits.messages_per_person ?? 30} 条消息、${limits.chats_per_stage ?? limits.chats_per_person ?? 3} 个会话、${limits.chars_per_stage ?? limits.chars_per_person ?? 16000} 字符。每人三阶段累计最多 ${limits.attempts_per_person ?? 4} 次处理尝试（3 个阶段 + 1 次共享重试，调用前校验失败也占次数）；有新资料、暂停或继续都不重置。只取当前有权读取的已同步片段，不上传整个群聊。信息不足停在当前阶段；额度用完仍缺项的保留待处理，刷新不会自动补跑。` : "";
      $("cw-preflight").hidden = !targeted || !result.id;
      if (previewKey !== previewVersion(result)) { $("cw-preflight-content").replaceChildren(); $("cw-preflight-content").hidden = true; previewKey = null; }
    }
    async function changeScan(action) {
      if (!canScan || aiSubmitting || !scope.responsible_id) return;
      if (action === "scan" && (!aiRun?.preflight_id || !(aiRun.status === "ready" || retryableRun(aiRun)))) return;
      ++aiLoading; clearTimeout(aiTimer); showAIProgress = true;
      aiSubmitting = true; scanControls();
      feedback(action === "preflight" ? "正在服务器准备本批业务片段，尚未发送给 AI…" : action === "pause" ? "正在停止后续请求…" : "正在提交本批核对…");
      try {
        const result = await api(`/ai/${action}`, {body: {request_id: requestId(), ...(action === "scan" ? {preflight_id: aiRun.preflight_id, retry_failed: retryableRun(aiRun)} : {})}});
        if (action === "preflight") previewKey = null;
        aiRun = result; showInitial(result);
        feedback(result.message || (action === "preflight" ? "准备完成；现在可查看当前阶段的送审内容。明确启动后才会调用 AI，沿用已有尝试额度。" : action === "pause" ? "已停止后续请求。" : "本批已开始分阶段核对，符合全部条件的自动入库。"));
        await pollAI();
      } catch (error) { feedback(error.message, true); }
      finally { aiSubmitting = false; scanControls(); }
    }
    if (canScan) $("cw-ai-scan").addEventListener("click", () => changeScan("preflight"));
    $("cw-ai-start")?.addEventListener("click", () => changeScan("scan"));
    $("cw-ai-pause")?.addEventListener("click", () => changeScan("pause"));
    $("cw-preflight-open")?.addEventListener("click", async () => {
      if (!aiRun?.targeted || !aiRun.id) return;
      const control = $("cw-preflight-open"), box = $("cw-preflight-content"), runId = aiRun.id, version = previewVersion(aiRun);
      control.disabled = true; box.hidden = false; box.replaceChildren(element("p", "正在读取当前授权范围内的送审片段…"));
      try {
        const result = await api(`/ai/preflight/${encodeURIComponent(runId)}`);
        if (previewVersion(aiRun) !== version) return;
        previewKey = version; box.replaceChildren();
        box.append(element("p", "下方显示本批所有人的当前阶段；若为续核，本次准备人数见上方。不一定都是第一步，只有前一步已通过才继续归属或 handle。查看不会调用 AI；明确启动后沿用原任务，每人累计最多 4 次处理尝试。", "cw-small"));
        for (const person of result.people || []) {
          const details = element("details", "", "cw-person-preview");
          const outcome = {confirmed: "已入库", review: "待处理", not_creator: "不是达人", not_mine: "不由当前 CM 负责", identity_unknown: "身份不确定", ownership_unknown: "归属不确定", handle_missing: "待补 handle", handle_conflict: "handle 待核实", failed: "分析未完成"}[person.outcome];
          const stage = {identity: "判断身份", ownership: "判断归属", handle: "核实 handle", complete: "已结束", done: "已结束"}[person.current_stage];
          const messages = person.messages || [], chatCount = new Set(messages.map(message => message.chat || message.chat_id).filter(Boolean)).size;
          details.append(element("summary", `${person.name || person.display_name || "用户姓名暂缺"}${stage ? ` · ${stage}` : ""} · ${messages.length} 条消息 · ${chatCount || (messages.length ? person.chat_count || 0 : 0)} 个会话${outcome ? ` · ${outcome}` : ""}`));
          if (person.request_count != null) details.append(element("p", `累计尝试 ${person.request_count} / ${person.request_limit ?? 4} 次，含调用前校验和重试，不等于付费请求数。${person.budget_exhausted ? "额度已用完，不再自动补跑；缺项记录仍保留待处理。" : "续核不会重置已用次数。"}`, "cw-small"));
          if (person.unavailable) details.append(element("p", "当前权限或原文已变化，不能展示这位用户的送审片段。", "cw-small"));
          for (const message of person.messages || []) {
            const item = element("div", "", "cw-preview-message");
            item.append(element("p", `${message.ref || ""} · ${message.user || message.speaker || "用户"} · ${message.time || "时间暂缺"}`, "cw-small"), element("pre", message.text || "—"));
            details.append(item);
          }
          box.append(details);
        }
        if (!result.people?.length) box.append(element("p", "本批没有可送审用户；这不代表没有达人。"));
        if (result.local_pending?.length) {
          const pending = element("details", "", "cw-person-preview"), list = element("ul");
          pending.append(element("summary", `仅有本地线索（未发送 AI） · ${result.local_pending.length} 人`));
          pending.append(element("p", "这些线索不足以证明达人或负责人关系，等待 CM 核对；没有创建达人记录，也不会自动入库。", "cw-small"));
          for (const person of result.local_pending) list.append(element("li", `${person.display_name || "用户姓名暂缺"}：${person.reason || "业务关系尚不明确"}`));
          pending.append(list); box.append(pending);
        }
        if (result.local_pending_hidden_count) box.append(element("p", "另有部分本地线索当前已无权查看，未在本页展示。", "cw-small"));
      } catch (error) { box.replaceChildren(element("p", error.message, "cw-error")); }
      finally { control.disabled = false; }
    });
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
        aiRun = result;
        const active = activeRun(result);
        showInitial(result); scanControls();
        $("cw-initial-updated").textContent = `上次更新：${new Date().toLocaleTimeString("en-GB", {timeZone: "Europe/London"})}（英国时间）`;
        if (!result) { if (showAIProgress) feedback("当前没有核对任务；点击“准备核对”先在服务器整理少量业务片段。"); return; }
        const progress = JSON.stringify([result.id, result.candidate_count, result.outcomes, ["succeeded", "failed", "cancelled"].includes(result.status) ? result.status : ""]);
        const changed = lastAIProgress !== null && lastAIProgress !== progress;
        lastAIProgress = progress;
        if (changed && result.targeted) {
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
        scanControls();
      } finally {
        aiReading = false;
        if (refresh) { refresh.disabled = false; refresh.textContent = "刷新进度"; refresh.setAttribute("aria-busy", "false"); }
        if (aiPollAgain) { aiPollAgain = false; pollAI(); }
      }
    }
    $("cw-initial-refresh")?.addEventListener("click", pollAI);
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
