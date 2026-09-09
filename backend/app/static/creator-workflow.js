/* Real data stays in this page's memory. The server rechecks scope, identity and version. */
(() => {
  "use strict";
  const pending = item => ["pending", "deferred"].includes(item.status);
  const reasonCode = reason => typeof reason === "string" ? reason : reason.code;
  function newDraft(item) {
    return {version: item.version, handle: item.handle || "", contact: item.contact || null,
      relationship: item.relationship || "", resolved_reasons: [], acknowledged: false,
      dirty: false, revision: 0, request: null, searchOpen: !item.contact};
  }
  function missingFields(draft, item) {
    const missing = [];
    if (!draft.handle.trim() || /^[—–-]+$/.test(draft.handle.trim())) missing.push("handle");
    if (!draft.contact?.contact_id) missing.push("对应用户（请从搜索结果中选中）");
    if (!["owner", "representative"].includes(draft.relationship)) missing.push("用户与账号的关系");
    for (const reason of item.reasons || []) {
      if (reason.resolvable && !draft.resolved_reasons.includes(reasonCode(reason))) missing.push(reason.label);
      if (["missing_cm", "ownership_conflict", "identity_conflict"].includes(reasonCode(reason))) missing.push(reason.label);
    }
    if (!draft.acknowledged) missing.push("本人负责及用户关系确认");
    return missing;
  }
  function draftBody(draft, requestId) {
    return {version: draft.version, handle: draft.handle.trim() || null,
      contact_id: draft.contact?.contact_id || null, relationship: draft.relationship || null,
      resolved_reasons: [...draft.resolved_reasons], request_id: requestId};
  }
  // A late save updates the revision token, never overwrites edits made during that save.
  function savedDraft(draft, item, sentRevision) {
    if (draft.revision === sentRevision) {
      const fresh = newDraft(item);
      return {...fresh, acknowledged: draft.acknowledged, searchOpen: draft.searchOpen};
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
  const root = document.getElementById("creator-workflow");
  root.classList.toggle("cw-readonly", !writable);
  const $ = id => document.getElementById(id);
  const drafts = new Map(), records = new Map(), selected = new Set(), busy = new Set();
  let listing = data, page = data.page || 1, loading = 0, aiTimer = null, importPreview = null;
  const form = $("cw-filters");
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
      credentials: "same-origin", cache: "no-store", redirect: "error", signal}); }
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
    if (!existing || (existing.version !== item.version && !existing.dirty && !existing.acknowledged && !selected.has(item.id))) drafts.set(item.id, newDraft(item));
    return drafts.get(item.id);
  };
  function changed(draft) { draft.dirty = true; draft.revision++; draft.request = null; draft.confirmRequest = null; }
  function updateSelected() {
    if (!writable) return;
    const control = $("cw-confirm-selected");
    control.textContent = `确认所选（${selected.size}）`; control.disabled = selected.size === 0 || busy.size > 0;
  }
  function labelInput(label, input) {
    const wrapper = element("label", "", "cw-inline-label"); wrapper.append(element("span", label), input); return wrapper;
  }
  function check(label, checked, onChange) {
    const wrapper = element("label", "", "cw-check"), input = element("input");
    input.type = "checkbox"; input.checked = checked;
    input.addEventListener("change", () => onChange(input.checked));
    wrapper.append(input, element("span", label)); return wrapper;
  }
  function renderUser(parent, draft, editable) {
    const block = element("div"), selectedUser = element("div"), search = element("div", "", "cw-search");
    parent.append(block); block.append(selectedUser, search);
    let controller = null, generation = 0, timer = null;
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
        if (contact) actions.append(button("更换用户", () => { draft.searchOpen = true; search.hidden = false; input.focus(); }, "cw-link"),
          button("清除", () => {
            generation++; controller?.abort(); input.value = ""; results.replaceChildren();
            draft.contact = null; draft.searchOpen = true; draft.acknowledged = false; changed(draft);
            const ack = parent.closest("tr").querySelector("[data-acknowledgement]"); if (ack) ack.checked = false;
            showUser(); search.hidden = false; parent.closest("tr").dispatchEvent(new Event("workflow-draft-change"));
          }, "cw-link"));
        selectedUser.append(actions);
      }
    }
    const input = element("input", "", "form-control"), results = element("ul", "", "cw-search-results");
    const status = element("div", "", "cw-search-status");
    input.type = "search"; input.autocomplete = "off"; input.maxLength = 80;
    input.placeholder = "搜索我的 Lark 用户"; input.setAttribute("aria-label", "搜索我的 Lark 用户");
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    if (editable) {
      search.append(input, status, results); search.hidden = !draft.searchOpen;
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
              const choice = button("", () => {
                generation++; controller?.abort(); draft.contact = contact; draft.searchOpen = false;
                draft.acknowledged = false; changed(draft); input.value = ""; results.replaceChildren();
                const ack = parent.closest("tr").querySelector("[data-acknowledgement]"); if (ack) ack.checked = false;
                showUser(); search.hidden = true; parent.closest("tr").dispatchEvent(new Event("workflow-draft-change"));
              }, "");
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
  function renderEvidence(cell, item) {
    const details = element("details", "", "cw-source-details");
    details.append(element("summary", "查看依据"));
    for (const evidence of item.evidence || []) {
      const source = evidence.source_data || {};
      const sourceRows = source.row ? [source.row] : source.rows || source.source_rows || [];
      const sourceCM = source.cm_name || [source.cm, source.tap_cm].flat().filter(Boolean).join(" / ");
      const lines = [source.filename || source.source_filename, source.sheet || source.sheet_name,
        sourceRows.length ? `第 ${sourceRows.join("、")} 行` : null, source.reason || source.rationale || source.summary,
        sourceCM ? `来源负责人：${sourceCM}` : null,
        source.original_handle || source.handle_raw ? `原始账号：${source.original_handle || source.handle_raw}` : null].filter(Boolean);
      if (!lines.length) lines.push(evidence.label || (evidence.source_type === "excel" ? "Excel 导入来源" : "AI 推荐依据"));
      details.append(element("div", lines.join(" · "), "cw-source-detail"));
    }
    details.append(button("查看相关会话记录", () => openEvidence(item), "cw-link"));
    cell.append(details);
  }
  let evidenceSequence = 0;
  async function openEvidence(item, chatId = "", messagePage = 1) {
    const dialog = $("cw-evidence-dialog"), sequence = ++evidenceSequence;
    $("cw-evidence-title").textContent = `${item.handle ? "@" + item.handle : "—"} · 核对依据`;
    if (!dialog.open) dialog.showModal();
    $("cw-conversations").replaceChildren(); $("cw-messages").replaceChildren(); $("cw-message-pages").replaceChildren();
    feedback("正在读取当前授权范围内的记录…", false, $("cw-evidence-feedback"));
    try {
      const result = await api(`/${encodeURIComponent(item.id)}/evidence`, {params: {chat_id: chatId, page: messagePage}});
      if (sequence !== evidenceSequence || !dialog.open) return;
      feedback(result.notice || "", false, $("cw-evidence-feedback"));
      for (const chat of result.conversations || []) {
        const control = button("", () => openEvidence(item, chat.chat_id), "cw-conversation");
        control.append(element("strong", chat.name || "—"), element("span", chat.chat_mode === "p2p" ? "私聊" : "群聊", "cw-small"));
        if (chat.chat_id === chatId) control.setAttribute("aria-current", "true");
        $("cw-conversations").append(control);
      }
      if (!(result.conversations || []).length) $("cw-conversations").append(element("p", "当前没有可查看的相关会话。", "cw-small"));
      for (const message of result.messages || []) {
        const article = element("article", "", "cw-message"), meta = element("div", "", "cw-small");
        meta.append(element("strong", message.sender_name || "—"), element("span", message.time || "—"));
        article.append(meta, element("p", message.text || "—")); $("cw-messages").append(article);
      }
      if (!(result.messages || []).length) $("cw-messages").append(element("p", chatId ? "当前授权范围内没有可展示的消息。" : "选择左侧会话，再查看记录。", "cw-empty"));
      if (chatId && result.pages > 1) {
        const prev = button("上一页", () => openEvidence(item, chatId, result.page - 1)); prev.disabled = result.page <= 1;
        const next = button("下一页", () => openEvidence(item, chatId, result.page + 1)); next.disabled = result.page >= result.pages;
        $("cw-message-pages").append(prev, element("span", `第 ${result.page} / ${result.pages} 页`), next);
      }
    } catch (error) { if (sequence === evidenceSequence && dialog.open) feedback(error.message, true, $("cw-evidence-feedback")); }
  }
  $("cw-evidence-close").addEventListener("click", () => $("cw-evidence-dialog").close());
  $("cw-evidence-dialog").addEventListener("close", () => { evidenceSequence++; $("cw-conversations").replaceChildren(); $("cw-messages").replaceChildren(); });
  function renderRow(item) {
    const row = element("tr"); row.dataset.candidateId = item.id;
    const editable = writable && pending(item), draft = getDraft(item);
    row.setAttribute("aria-busy", String(busy.has(item.id)));
    if (writable) {
      const cell = element("td"), input = element("input", "", "form-check-input");
      input.type = "checkbox"; input.checked = selected.has(item.id); input.disabled = !editable;
      input.setAttribute("aria-label", `选择 ${draft.handle || "待补 handle 的记录"}，用于批量确认`);
      input.addEventListener("change", () => { input.checked ? selected.add(item.id) : selected.delete(item.id); updateSelected(); }); cell.append(input); row.append(cell);
    }
    const handleCell = element("td"), sourceCell = element("td"), userCell = element("td"), statusCell = element("td"), actionCell = element("td");
    row.append(handleCell, sourceCell, userCell, statusCell, actionCell);
    handleCell.append(element("div", draft.handle ? `@${draft.handle.replace(/^@/, "")}` : "—", "cw-handle"), element("span", item.platform || "—", "cw-small"));
    if (editable) {
      const input = element("input", "", "form-control"); input.value = draft.handle; input.maxLength = 80;
      input.placeholder = "填写 handle"; input.autocomplete = "off";
      input.addEventListener("input", () => { draft.handle = input.value; draft.acknowledged = false; changed(draft); const ack = row.querySelector("[data-acknowledgement]"); if (ack) ack.checked = false; handleCell.querySelector(".cw-handle").textContent = input.value.trim() ? `@${input.value.replace(/^@/, "")}` : "—"; });
      handleCell.append(labelInput("补充 / 修改 handle", input));
    }
    if (!(item.sources || []).length) sourceCell.append(element("span", "—"));
    for (const source of item.sources || []) sourceCell.append(element("span", source === "excel" ? "Excel" : source === "ai" ? "AI" : source, `cw-pill cw-pill--${source === "excel" ? "excel" : "ai"}`));
    renderEvidence(sourceCell, item); renderUser(userCell, draft, editable);
    if (editable) {
      const relation = element("select", "", "form-select");
      for (const [value, label] of [["", "—"], ["owner", "达人本人"], ["representative", "达人代表 / 代运营"]]) {
        const option = element("option", label); option.value = value; relation.append(option);
      }
      relation.value = draft.relationship;
      relation.addEventListener("change", () => { draft.relationship = relation.value; draft.acknowledged = false; changed(draft); const ack = row.querySelector("[data-acknowledgement]"); if (ack) ack.checked = false; });
      userCell.append(labelInput("这位用户与账号的关系", relation));
    } else if (draft.relationship) userCell.append(element("p", draft.relationship === "owner" ? "达人本人" : "达人代表 / 代运营", "cw-small"));
    statusCell.append(element("span", item.status_label || (item.status === "confirmed" ? "已确认入库" : item.status === "deferred" ? "待确认 · 暂缓" : "待确认"), `cw-pill cw-pill--${item.status === "confirmed" ? "confirmed" : "pending"}`));
    if (draft.dirty) statusCell.append(element("div", "有未保存的补充资料", "cw-small"));
    if ((item.reasons || []).length) {
      const reasons = element("ul", "", "cw-reasons");
      for (const reason of item.reasons) reasons.append(element("li", reason.label || reasonCode(reason)));
      statusCell.append(reasons);
    }
    if (editable) {
      for (const reason of item.reasons || []) if (reason.resolvable) statusCell.append(check(`已核对：${reason.label}`, draft.resolved_reasons.includes(reason.code), value => {
        draft.resolved_reasons = value ? [...new Set([...draft.resolved_reasons, reason.code])] : draft.resolved_reasons.filter(code => code !== reason.code); changed(draft);
      }));
      const ack = check("我确认由我负责，且所选用户及关系正确", draft.acknowledged, value => { draft.acknowledged = value; });
      ack.querySelector("input").dataset.acknowledgement = "true"; statusCell.append(ack);
      const actions = element("div", "", "cw-row-actions");
      const confirm = button("确认并入库", () => rowAction(item, "confirm"), "btn btn-primary");
      const checkReady = () => { confirm.disabled = busy.has(item.id) || missingFields(draft, item).length > 0; };
      for (const event of ["input", "change", "workflow-draft-change"]) row.addEventListener(event, checkReady);
      checkReady();
      actions.append(button("保存草稿", () => rowAction(item, "draft")), confirm,
        button("不是我负责", () => { if (window.confirm("这条记录将交回管理员核对，不会自动转给其他 CM。确定不是你负责？")) rowAction(item, "reject"); }),
        button("暂缓", () => rowAction(item, "defer")),
        button("重新载入此行", async () => { if (!draft.dirty || window.confirm("重新载入会放弃此行尚未保存的补充资料，继续？")) { drafts.delete(item.id); selected.delete(item.id); await load(); } }, "cw-link"));
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
    const reasonSelect = $("cw-reason"), previous = reasonSelect.value;
    const labels = new Map([...reasonSelect.options].filter(option => option.value).map(option => [option.value, option.textContent]));
    for (const item of listing.items || []) for (const reason of item.reasons || []) labels.set(reasonCode(reason), reason.label || reasonCode(reason));
    reasonSelect.replaceChildren(element("option", "全部原因")); reasonSelect.options[0].value = "";
    for (const [code, label] of labels) { const option = element("option", label); option.value = code; reasonSelect.append(option); } reasonSelect.value = previous;
    updateSelected();
  }
  async function load() {
    const sequence = ++loading; $("cw-refresh").disabled = true;
    try {
      const params = Object.fromEntries(new FormData(form)); params.page = page;
      const result = await api("/data", {params});
      if (sequence !== loading) return;
      listing = result; page = result.page || 1; render();
    } catch (error) { if (sequence === loading) feedback(error.message, true); }
    finally { if (sequence === loading) $("cw-refresh").disabled = false; }
  }
  function rowMessage(id, text, error = false) {
    const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === id);
    if (row) feedback(text, error, row.querySelector("[data-row-feedback]"));
  }
  function lockRowInputs(id, locked) {
    const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === id);
    if (!row) return;
    for (const input of row.querySelectorAll("input,select")) {
      if (locked && input.dataset.cwLocked === undefined) { input.dataset.cwLocked = String(input.disabled); input.disabled = true; }
      else if (!locked && input.dataset.cwLocked !== undefined) { input.disabled = input.dataset.cwLocked === "true"; delete input.dataset.cwLocked; }
    }
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
      throw new Error("相同 handle 的来源已合并到已有记录。请重新核对合并后的依据，再确认入库。");
    }
    drafts.set(item.id, savedDraft(draft, updated, revision)); records.set(item.id, updated);
    return updated;
  }
  async function perform(item, action) {
    let draft = getDraft(item);
    if (action === "draft") { await save(item); return "草稿已保存，仍在待确认。"; }
    if (action === "confirm") {
      const missing = missingFields(draft, item);
      if (missing.length) throw new Error(`还不能入库，请补齐 / 核对：${missing.join("、")}。`);
      lockRowInputs(item.id, true);
      if (draft.dirty) { item = await save(item); draft = getDraft(item); }
      // New edits during a save are intentionally not included in this confirmation.
      if (draft.dirty) throw new Error("保存期间资料又有变化，已保留，请重新核对后确认。");
      const missingAfterSave = missingFields(draft, item);
      if (missingAfterSave.length) throw new Error(`还需核对：${missingAfterSave.join("、")}。`);
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
    busy.add(item.id); updateSelected(); rowMessage(item.id, "正在保存…");
    try { const message = await perform(item, action); await load(); rowMessage(item.id, message); feedback(message); }
    catch (error) { rowMessage(item.id, error.message, true); }
    finally { busy.delete(item.id); lockRowInputs(item.id, false); updateSelected(); const row = [...$("cw-rows").children].find(node => node.dataset.candidateId === item.id); if (row) { row.setAttribute("aria-busy", "false"); for (const node of row.querySelectorAll("button")) node.disabled = false; row.dispatchEvent(new Event("workflow-draft-change")); } }
  }
  $("cw-refresh").addEventListener("click", load);
  form.addEventListener("submit", event => { event.preventDefault(); page = 1; load(); });
  for (const select of form.querySelectorAll("select")) select.addEventListener("change", () => { page = 1; load(); });
  $("cw-prev").addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
  $("cw-next").addEventListener("click", () => { page++; load(); });
  if (writable || scope.can_admin || scope.can_import) {
    if (writable) $("cw-confirm-selected").addEventListener("click", async () => {
      if (!selected.size || !window.confirm(`确认逐项核对过的 ${selected.size} 条记录？缺资料或存在冲突的会保留待确认，并逐项显示结果。`)) return;
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
    const retryAI = (writable || scope.can_admin) ? button("重试未完成", () => startAI(true), "btn btn-outline-primary") : null;
    if (retryAI) { retryAI.hidden = true; retryAI.id = "cw-ai-retry"; $("cw-ai-scan").after(retryAI); }
    async function startAI(retryFailed = false) {
      const control = $("cw-ai-scan"); control.disabled = true;
      if (retryAI) retryAI.hidden = true;
      try {
        const result = await api("/ai/scan", {body: {request_id: requestId(), retry_failed: retryFailed}});
        feedback(result.message || "候选分析已提交。AI 结果只进入待确认，不会自动入库。");
        pollAI();
      } catch (error) { feedback(error.message, true); control.disabled = false; }
    }
    if (writable || scope.can_admin) $("cw-ai-scan").addEventListener("click", () => startAI());
    async function pollAI() {
      clearTimeout(aiTimer);
      try {
        const response = await api("/ai/status");
        const result = response.run;
        if (!result) { feedback("当前没有正在运行的候选分析。"); $("cw-ai-scan").disabled = false; return; }
        const terminal = ["completed", "succeeded", "failed", "cancelled", "partial"].includes(result.status);
        const errors = (result.errors || []).map(error => typeof error === "string" ? error : error.message || error.error || "部分候选分析失败");
        feedback([result.message || (terminal ? "本轮候选分析已结束，请核对待确认名单。" : "AI 正在查找候选；结果仍需你确认。"), ...errors].join("；"), errors.length > 0 || ["failed", "partial"].includes(result.status));
        if (terminal) { $("cw-ai-scan").disabled = false; if (retryAI) retryAI.hidden = !(errors.length || ["failed", "partial"].includes(result.status)); await load(); }
        else aiTimer = setTimeout(pollAI, 5000);
      } catch (error) { feedback(`暂时未能读取分析进度：${error.message}`, true); $("cw-ai-scan").disabled = false; }
    }
    if (scope.can_import) {
      const dialog = $("cw-import-dialog"), fileInput = $("cw-import-file"), resultBox = $("cw-import-results");
      $("cw-import-open").addEventListener("click", () => dialog.showModal());
      $("cw-import-close").addEventListener("click", () => dialog.close());
      fileInput.addEventListener("change", () => { importPreview = null; resultBox.replaceChildren(); $("cw-import-commit").hidden = true; });
      function showImport(result) {
        resultBox.replaceChildren();
        if (result.summary) resultBox.append(element("p", `共 ${result.summary.total} 行 · 可直接入库 ${result.summary.ready} 行 · 待确认 ${result.summary.pending} 行 · 无效 ${result.summary.invalid} 行`, "cw-note"));
        else if (result.imported != null) resultBox.append(element("p", `已导入 ${result.imported} 行 · 正式入库 ${result.formal} 行 · 待确认 ${result.pending} 行 · 跳过 ${result.skipped} 行`, "cw-note"));
        const table = element("table", "", "cw-import-table"), head = element("tr"), body = element("tbody");
        for (const text of ["行", "handle", "结果", "说明"]) head.append(element("th", text));
        const thead = element("thead"); thead.append(head); table.append(thead, body);
        for (const row of result.items || result.rows || []) {
          const line = element("tr"), reasons = (row.reasons || []).map(reason => reason.label || reason).join("；");
          const statusLabels = {ready: "可直接入库", pending: "待确认", invalid: "无效行", confirmed: "已确认入库", skipped: "已跳过", error: "失败"};
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
