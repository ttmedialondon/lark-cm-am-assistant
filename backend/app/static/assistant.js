/* Persistent IDs resume server work; chat content stays out of browser storage. */
(() => {
  "use strict";
  const app = document.getElementById("operations-assistant");
  if (!app) return;
  const $ = id => document.getElementById(id);
  const panel = $("oa-panel"), input = $("oa-input"), history = $("oa-history");
  const storageKey = `ttmedia.assistant.${app.dataset.userId}`;
  const terminal = new Set(["completed", "cancelled", "failed", "unknown"]);
  const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  const labels = {queued: "正在等待处理", running: "正在整理", waiting: "等待分析完成",
    completed: "已完成", cancelled: "已停止", failed: "这次未能完成", unknown: "请求结果尚未确认，请勿重复提交"};
  let saved = {}, active = null, source = null, submitting = false, restoring = false, denied = false;
  let pending = null, restoreTimer = null, revision = 0;
  try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch { /* IDs can remain in memory. */ }
  if (!saved || typeof saved !== "object" || !uuid.test(saved.session_id || "")) saved = {};
  if (saved.client_request_id && !uuid.test(saved.client_request_id)) delete saved.client_request_id;
  const pageContext = JSON.parse($("oa-page-context").textContent);
  $("oa-page-context").textContent = "";

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify(saved)); }
    catch { status("浏览器未允许保存会话编号，刷新后可能无法自动恢复。", true); }
  }
  function status(message, error = false) {
    $("oa-status").textContent = message || "";
    $("oa-status").hidden = !message;
    $("oa-status").classList.toggle("is-error", error);
  }
  function controls() {
    const busy = submitting || restoring || Boolean(active);
    $("oa-send").disabled = denied || busy || !input.value.trim();
    $("oa-send").textContent = saved.client_request_id && !busy ? "重试提交" : "发送 ↑";
    $("oa-stop").hidden = !active;
    $("oa-stop").disabled = false;
    $("oa-new").disabled = busy;
    input.disabled = denied || submitting;
    $("oa-composer-note").textContent = active ? "可以继续输入，完成后发送" : "Enter 发送 · Shift + Enter 换行";
    for (const button of app.querySelectorAll("[data-prompt], .oa-followup button")) button.disabled = denied || busy;
    $("oa-form").setAttribute("aria-busy", String(busy));
  }
  function clearProtected() {
    denied = true; revision++; source?.close(); source = null; active = null; pending = null;
    delete saved.client_request_id; persist();
    history.replaceChildren(node("p", "oa-answer", "登录或查看权限已变化，请重新登录后打开助手。"));
    $("oa-scope").replaceChildren(); $("oa-scope").hidden = true;
    input.value = ""; controls();
  }
  async function request(path, body) {
    const response = await fetch(`/assistant${path}`, {
      method: body == null ? "GET" : "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
      headers: {Accept: "application/json", ...(body == null ? {} : {"Content-Type": "application/json", "X-CSRF-Token": app.dataset.csrf})},
      ...(body == null ? {} : {body: JSON.stringify(body)}),
    });
    if ([401, 403].includes(response.status)) clearProtected();
    const value = await response.json();
    if (!response.ok) {
      const error = new Error(typeof value.error === "string" ? value.error : value.error?.message ||
        (typeof value.detail === "string" ? value.detail : "请求未完成，请稍后重试。"));
      error.status = response.status;
      throw error;
    }
    return value;
  }
  function date(value) {
    if (!value || Number.isNaN(new Date(value).getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {timeZone: "Europe/London", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(value));
  }
  function scope(value) {
    if (!value || typeof value !== "object") return;
    const values = [value.cm_name || value.responsible?.name,
      value.start && value.end ? `${date(value.start)} – ${date(value.end)} · 英国时间` : null,
      ...(Array.isArray(value.creator_names) ? value.creator_names : [])].filter(Boolean);
    if (!values.length && value.label) values.push(value.label);
    $("oa-scope").replaceChildren(...values.map(text => node("span", "oa-chip", text)));
    $("oa-scope").hidden = !values.length;
  }
  function scrollToBottom() { history.scrollTop = history.scrollHeight; }
  function userMessage(requestId, text) {
    $("oa-welcome")?.remove();
    let message = [...history.children].find(child => child.dataset.requestId === requestId);
    if (!message) {
      message = node("article", "oa-message oa-message--user"); message.dataset.requestId = requestId;
      message.append(node("p", "", text)); history.append(message);
    }
  }
  function gapList(values) {
    if (!Array.isArray(values) || !values.length) return null;
    const details = node("details", "oa-gaps");
    details.append(node("summary", "", `部分资料尚不完整 · ${values.length} 项`));
    for (const gap of values) details.append(node("div", "", typeof gap === "string" ? gap : gap.message || gap.reason || "资料暂不可读取"));
    return details;
  }
  function evidenceCard(reference) {
    const details = node("details", "oa-evidence"), content = node("div");
    details.append(node("summary", "", "查看原文"), content);
    let reading = false;
    details.addEventListener("toggle", async () => {
      if (!details.open || reading || denied) return;
      reading = true; content.replaceChildren(node("p", "oa-coverage", "正在读取原文…"));
      try {
        const value = await request(`/evidence/${encodeURIComponent(reference)}`);
        if (denied) return;
        content.replaceChildren();
        for (const message of value.messages || []) {
          const item = node("figure");
          item.append(node("figcaption", "", [message.chat_name, message.sender_name, date(message.time || message.created_at)].filter(Boolean).join(" · ")),
            node("blockquote", "", message.text || "这条消息没有可显示的文字。"));
          content.append(item);
        }
        if (!value.messages?.length) content.append(node("p", "oa-coverage", "当前没有可读取的原文。"));
        const gaps = gapList(value.gaps); if (gaps) content.append(gaps);
      } catch (error) { if (!denied) content.replaceChildren(node("p", "oa-gaps", error.message)); }
      finally { reading = false; }
    });
    return details;
  }
  function draftCard(draft, turnId) {
    if (!draft || typeof draft.draft !== "string" || !draft.draft.trim()) return null;
    const card = node("section", "oa-draft"), editor = node("textarea"), label = node("label", "", "回复草稿 · 可编辑");
    editor.value = draft.draft; editor.rows = 5; editor.setAttribute("aria-label", "回复草稿");
    const copy = node("button", "", "复制回复"), feedback = node("span", "oa-copy-feedback");
    copy.type = "button"; feedback.setAttribute("role", "status");
    card.append(label, editor);
    if (draft.chinese_translation && draft.language !== "zh") card.append(node("p", "oa-translation", draft.chinese_translation));
    editor.addEventListener("input", () => {
      copy.disabled = !editor.value.trim();
      feedback.textContent = editor.value !== draft.draft && draft.chinese_translation ? "译文对应生成时原稿" : "";
    });
    copy.addEventListener("click", async () => {
      if (denied || !editor.value.trim()) return;
      copy.disabled = true;
      try {
        const session = await request(`/sessions/${encodeURIComponent(saved.session_id)}`);
        if (denied) return;
        if (!session.turns?.find(item => item.id === turnId)?.result?.draft) {
          card.replaceChildren(node("p", "oa-gaps", "草稿已过时或不可读取，请重新生成。")); return;
        }
        await navigator.clipboard.writeText(editor.value); feedback.textContent = "已复制，未发送";
      } catch (error) {
        if (!denied) feedback.textContent = error.name === "NotAllowedError" ? "浏览器未允许复制，可选中文字手动复制" : error.message;
      } finally { if (!denied) copy.disabled = !editor.value.trim(); }
    });
    card.append(copy, feedback);
    return card;
  }
  function renderResult(container, result) {
    container.replaceChildren(node("p", "oa-answer", result.answer || ""));
    scope({...result.scope, ...result.window, creator_names: [...new Set((result.events || [])
      .map(event => event.creator_handle || event.creator_name || event.lark_user_name).filter(Boolean))].slice(0, 3)});
    (result.events || []).forEach((event, index) => {
      const card = node("section", "oa-event"), meta = node("div", "oa-event-meta");
      for (const text of [event.creator_handle || event.lark_user_name || event.creator_name, event.priority,
        {unresolved: "待跟进", resolved: "已解决", uncertain: "待核对"}[event.handling_status]]) {
        if (text) meta.append(node("span", "", text));
      }
      card.append(meta, node("h4", "", `${index + 1}. ${event.title || "聊天事项"}`));
      if (event.summary) card.append(node("p", "", event.summary));
      if (event.latest_progress) card.append(node("p", "", event.latest_progress));
      if (event.evidence_ref || event.event_ref) card.append(evidenceCard(event.evidence_ref || event.event_ref));
      container.append(card);
    });
    const draft = draftCard(result.draft, container.dataset.turnId); if (draft) container.append(draft);
    const gaps = gapList(result.gaps); if (gaps) container.append(gaps);
    const coverage = result.coverage;
    if (coverage && typeof coverage === "object") {
      const counts = [];
      if (Number.isFinite(coverage.conversations)) counts.push(`${coverage.conversations} 个会话`);
      if (Number.isFinite(coverage.messages)) counts.push(`${coverage.messages} 条消息`);
      const cutoff = coverage.cutoff || result.window?.cutoff;
      if (cutoff) counts.push(`读取至 ${date(cutoff)}`);
      if (counts.length) container.append(node("p", "oa-coverage", counts.join(" · ")));
    }
    if (result.events?.length) {
      const followup = node("div", "oa-followup");
      for (const [label, prompt] of [["只看未解决", "只看刚才结果中还没解决的事项"], ["查看最新进展", "刚才这些事项后来有什么进展？"]]) {
        const button = node("button", "", label); button.type = "button";
        button.addEventListener("click", () => ask(prompt)); followup.append(button);
      }
      container.append(followup);
    }
  }
  function turn(view) {
    if (denied) return;
    if (view.error_code === "permission") { clearProtected(); return; }
    if (view.message) userMessage(view.client_request_id, view.message);
    let container = [...history.children].find(child => child.dataset.turnId === view.id);
    if (!container) {
      container = node("article", "oa-message oa-message--assistant"); container.dataset.turnId = view.id;
      history.append(container);
    }
    if (view.result) {
      const signature = JSON.stringify(view.result);
      if (container.dataset.signature !== signature) { renderResult(container, view.result); container.dataset.signature = signature; }
    } else if (terminal.has(view.status)) {
      container.replaceChildren(node("p", "oa-answer", typeof view.error === "string" ? view.error : view.error?.message || labels[view.status]));
    }
    if (view.scope && !view.result) scope(view.scope);
  }
  function finish(state) {
    source?.close(); source = null; active = null; controls();
    if (state === "completed") status("");
    else status(labels[state] || "这轮处理已结束", state !== "cancelled");
  }
  function watch(view) {
    if (denied) return;
    source?.close();
    if (terminal.has(view.status)) { finish(view.status); return; }
    active = view.id; controls();
    const watching = view.id;
    status(labels[view.status] || "正在读取进度");
    source = new EventSource(`/assistant/turns/${encodeURIComponent(watching)}/events`);
    source.addEventListener("scope", event => { if (active === watching) scope(JSON.parse(event.data)); });
    source.addEventListener("progress", event => {
      if (active !== watching) return;
      const progress = JSON.parse(event.data);
      status(progress.message || labels[progress.status] || "正在整理");
    });
    source.addEventListener("result", event => {
      if (active !== watching || denied) return;
      turn({...view, status: "completed", result: JSON.parse(event.data)}); controls(); scrollToBottom();
    });
    source.addEventListener("error", event => {
      if (active !== watching) return;
      if (typeof event.data === "string") {
        const value = JSON.parse(event.data);
        if (value.clear) clearProtected();
        else turn({...view, status: "failed", error: typeof value === "string" ? value : value.message});
        status(typeof value === "string" ? value : value.message || "这轮处理未能完成。", true);
      } else {
        status("连接中断，正在恢复已有请求…");
        clearTimeout(restoreTimer); restoreTimer = setTimeout(() => void restore(), 3000);
      }
    });
    source.addEventListener("done", event => {
      if (active !== watching) return;
      finish(JSON.parse(event.data).status);
    });
  }
  async function restore() {
    if (!saved.session_id || denied || restoring) return;
    restoring = true; controls();
    const version = revision;
    try {
      const value = await request(`/sessions/${encodeURIComponent(saved.session_id)}`);
      if (denied || version !== revision) return;
      const turns = value.turns || [];
      for (const view of turns) turn(view);
      if (denied) return;
      if (turns.some(view => view.client_request_id === saved.client_request_id)) {
        delete saved.client_request_id; pending = null; persist();
      }
      const running = turns.find(view => !terminal.has(view.status));
      if (running) watch(running);
      else { source?.close(); source = null; active = null; controls(); }
      if (saved.client_request_id) status("上次提交尚未确认，重新输入后重试会复用原请求编号。", true);
      else if (turns.length && !running) status("");
      scrollToBottom();
    } catch (error) {
      if (denied || version !== revision) return;
      if (error.status === 404 && !saved.client_request_id) { saved = {}; persist(); }
      status(error.status === 404 ? "上次请求尚未保存，可重新输入并提交。" : error.message, true);
    } finally { restoring = false; controls(); }
  }
  async function ask(text) {
    if (denied || active || submitting || restoring) return;
    text = text.trim(); if (!text) return;
    if (!saved.session_id) saved.session_id = crypto.randomUUID();
    if (!saved.client_request_id) saved.client_request_id = crypto.randomUUID();
    pending ||= {session_id: saved.session_id, client_request_id: saved.client_request_id,
      message: text, context: pageContext};
    persist(); submitting = true; controls(); userMessage(pending.client_request_id, pending.message); scrollToBottom();
    status("正在保存请求…");
    const version = revision;
    try {
      const value = await request("/turns", pending);
      if (denied || version !== revision) return;
      delete saved.client_request_id; pending = null; persist(); input.value = "";
      turn(value); watch(value);
    } catch (error) {
      if (denied || version !== revision) return;
      if (error.status === 400 || error.status === 413 || error.status === 415 || error.status === 422) {
        delete saved.client_request_id; pending = null; persist();
      }
      status(error.message + (saved.client_request_id ? " 重试将复用这次请求。" : ""), true);
    } finally { if (version === revision) { submitting = false; controls(); } }
  }
  function open() {
    panel.hidden = false; $("oa-launcher").hidden = true; $("oa-launcher").setAttribute("aria-expanded", "true");
    if (matchMedia("(max-width: 600px)").matches) fullscreen(true);
    input.focus();
  }
  function collapse() {
    panel.hidden = true; $("oa-launcher").hidden = false; $("oa-launcher").setAttribute("aria-expanded", "false");
    $("oa-launcher").focus();
  }
  function fullscreen(enabled) {
    enabled ||= matchMedia("(max-width: 600px)").matches;
    panel.classList.toggle("is-fullscreen", enabled); panel.setAttribute("aria-modal", String(enabled));
    $("oa-fullscreen").setAttribute("aria-label", enabled ? "退出全屏" : "全屏");
    $("oa-fullscreen").title = enabled ? "退出全屏" : "全屏";
  }
  $("oa-launcher").addEventListener("click", open);
  $("oa-collapse").addEventListener("click", collapse);
  $("oa-fullscreen").addEventListener("click", () => fullscreen(!panel.classList.contains("is-fullscreen")));
  $("oa-new").addEventListener("click", () => {
    if (active || submitting) return;
    revision++; source?.close(); source = null; saved = {}; pending = null; persist();
    history.replaceChildren(node("p", "oa-answer", "开始一个新问题。"));
    $("oa-scope").replaceChildren(); $("oa-scope").hidden = true; status(""); input.value = ""; controls(); input.focus();
  });
  $("oa-form").addEventListener("submit", event => { event.preventDefault(); void ask(input.value); });
  input.addEventListener("input", controls);
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void ask(input.value); }
  });
  for (const button of app.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    input.value = button.dataset.prompt; controls(); void ask(input.value);
  });
  $("oa-stop").addEventListener("click", async () => {
    if (!active) return;
    const stopping = active; $("oa-stop").disabled = true;
    try { const value = await request(`/turns/${encodeURIComponent(stopping)}/cancel`, {}); if (!denied && active === stopping) { turn(value); watch(value); } }
    catch (error) { if (!denied) { status(error.message, true); $("oa-stop").disabled = false; } }
  });
  panel.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); if (panel.classList.contains("is-fullscreen") && !matchMedia("(max-width: 600px)").matches) fullscreen(false); else collapse(); }
    if (event.key !== "Tab" || !panel.classList.contains("is-fullscreen")) return;
    const elements = [...panel.querySelectorAll("button:not(:disabled), textarea:not(:disabled), summary")].filter(element => element.getClientRects().length);
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  window.addEventListener("pagehide", () => { source?.close(); clearTimeout(restoreTimer); });
  if (app.dataset.configured !== "true") status("助手尚未配置，请联系管理员。", true);
  controls(); void restore();
})();
