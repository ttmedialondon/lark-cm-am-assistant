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
  let pending = null, pendingScopeVersion = null, restoreTimer = null, revision = 0, sourceVersion = 0, sourceFocus = null, followBottom = true;
  let cmScopes = [], scopesReady = false, scopeRequest = null, canManageCM = false;
  let pendingPageContext = null, currentDraft = null, activeReplyAction = null, activeReplyText = null;
  let draftTimer = null, draftStatusVersion = 0;
  try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch { /* IDs can remain in memory. */ }
  if (!saved || typeof saved !== "object" || !uuid.test(saved.session_id || "")) saved = {};
  if (saved.client_request_id && !uuid.test(saved.client_request_id)) delete saved.client_request_id;
  if (saved.cm_id && !uuid.test(saved.cm_id)) delete saved.cm_id;
  const pageContext = JSON.parse($("oa-page-context").textContent);
  let activeContext = {...pageContext}, contextChosen = false, scopeVersion = 0, currentScopeTurn = null;
  $("oa-page-context").textContent = "";

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }
  function answer(text) {
    const value = node("div", "oa-answer");
    function inline(parent, text) {
      const tokens = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\[([^\]\n]+)\]\(([^)\s]+)\)/g;
      let start = 0;
      for (const token of text.matchAll(tokens)) {
        parent.append(document.createTextNode(text.slice(start, token.index)));
        if (token[1]) parent.append(node("code", "", token[1]));
        else if (token[2] || token[3]) parent.append(node("strong", "", token[2] || token[3]));
        else {
          let url;
          try { url = new URL(token[5]); } catch { /* Invalid links stay readable. */ }
          if (url && ["https:", "http:"].includes(url.protocol)) {
            const link = node("a", "", token[4]); link.href = url.href;
            link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link);
          } else parent.append(document.createTextNode(token[0]));
        }
        start = token.index + token[0].length;
      }
      parent.append(document.createTextNode(text.slice(start)));
    }
    // shortcut: render the answer's text formatting only; use a maintained parser if full CommonMark is needed.
    let paragraph = null, list = null, code = null;
    for (const line of String(text || "").replace(/\r\n?/g, "\n").split("\n")) {
      if (/^\s*```/.test(line)) {
        paragraph = list = null;
        if (code) code = null;
        else { const pre = node("pre"); code = node("code"); pre.append(code); value.append(pre); }
      } else if (code) code.append(document.createTextNode(`${line}\n`));
      else if (!line.trim()) paragraph = list = null;
      else {
        const item = line.match(/^\s*(?:(\d+)[.)]|[-+*])\s+(.+)$/), heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (item) {
          const tag = item[1] ? "ol" : "ul";
          if (list?.tagName.toLowerCase() !== tag) {
            list = node(tag); if (item[1]) list.start = Number(item[1]); value.append(list);
          }
          const entry = node("li"); inline(entry, item[2]); list.append(entry); paragraph = null;
        } else if (heading) {
          const title = node(`h${heading[1].length}`); inline(title, heading[2]); value.append(title); paragraph = list = null;
        } else {
          list = null;
          if (!paragraph) { paragraph = node("p"); value.append(paragraph); }
          else paragraph.append(node("br"));
          inline(paragraph, line);
        }
      }
    }
    return value;
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
    const busy = !scopesReady || submitting || restoring || Boolean(active);
    $("oa-send").disabled = denied || busy || !input.value.trim();
    $("oa-send").textContent = saved.client_request_id && !busy ? "重试提交" : "发送 ↑";
    $("oa-stop").hidden = !active;
    $("oa-stop").disabled = false;
    $("oa-new").disabled = denied || busy || Boolean(saved.client_request_id);
    $("oa-cm").disabled = denied || busy || Boolean(saved.client_request_id);
    input.disabled = denied || submitting;
    $("oa-composer-note").textContent = active ? "可以继续输入，完成后发送" : "Enter 发送 · Shift + Enter 换行";
    for (const button of app.querySelectorAll("[data-ask], [data-quick]")) button.disabled = denied || busy || Boolean(saved.client_request_id);
    $("oa-form").setAttribute("aria-busy", String(busy));
  }
  function clearProtected() {
    denied = true; revision++; source?.close(); source = null; active = null; pending = null;
    activeContext = {}; scopeVersion++; currentScopeTurn = null;
    closeSources(false);
    closeReply(); currentDraft = null; $("oa-reply-body").replaceChildren();
    delete saved.client_request_id; persist();
    const reconnect = node("a", "oa-evidence", "检查账号连接"); reconnect.href = "/dashboard";
    history.replaceChildren(node("p", "oa-answer", "连接或访问权限已变化。"), reconnect);
    cmScopes = []; scopesReady = false; $("oa-cm").replaceChildren(); $("oa-person").hidden = true;
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
  async function loadScopes() {
    if (denied) return false;
    if (scopesReady) return true;
    if (scopeRequest) return scopeRequest;
    scopeRequest = (async () => {
      try {
        const value = await request("/scopes");
        if (denied) return false;
        cmScopes = value.scopes || [];
        canManageCM = location.pathname.startsWith("/admin/") && value.can_switch_cm === true;
        const selected = (canManageCM && saved.client_request_id && cmScopes.find(person => person.cm_id === saved.cm_id)) ||
          (canManageCM && cmScopes.find(person => person.cm_id === pageContext.cm_id)) ||
          cmScopes.find(person => person.cm_id === value.selected_cm_id) || (canManageCM && cmScopes[0]);
        if (!selected) {
          status("Lark 连接未就绪。", true);
          const reconnect = node("a", "oa-evidence", "检查账号连接"); reconnect.href = "/dashboard";
          $("oa-status").append(document.createTextNode(" "), reconnect); return false;
        }
        if (!canManageCM && saved.cm_id && saved.cm_id !== selected.cm_id) { saved = {}; pending = null; persist(); }
        $("oa-cm").replaceChildren(...cmScopes.map(person => {
          const option = node("option", "", person.cm_name || "未命名人员"); option.value = person.cm_id; return option;
        }));
        $("oa-person").hidden = !canManageCM;
        if (activeContext.cm_id !== selected.cm_id) activeContext = {};
        activeContext.cm_id = selected.cm_id; activeContext.cm_name = selected.cm_name;
        $("oa-cm").value = selected.cm_id; scopesReady = true;
        scope({}); await restore(); return scopesReady && !denied;
      } catch (error) { if (!denied) status(error.message, true); return false; }
      finally { scopeRequest = null; controls(); }
    })();
    return scopeRequest;
  }
  function resetSession(person, keepInput = false) {
    revision++; source?.close(); source = null; clearTimeout(restoreTimer);
    saved = {cm_id: person.cm_id}; pending = null; pendingScopeVersion = null; persist();
    activeContext = {}; selectContext(person); currentScopeTurn = null;
    $("oa-cm").value = person.cm_id;
    closeSources(false); $("oa-new-result").hidden = true;
    closeReply(); currentDraft = null; $("oa-reply-body").replaceChildren();
    history.replaceChildren(node("p", "oa-answer", "开始一个新问题。"));
    scope({}); status(""); if (!keepInput) input.value = ""; controls();
  }
  function sourceChanged(message) {
    const person = cmScopes.find(person => person.cm_id === activeContext.cm_id) ||
      {cm_id: activeContext.cm_id, cm_name: activeContext.cm_name};
    active = null; submitting = false;
    resetSession(person, true);
    status(typeof message === "string" ? message : "查询资料已变化，请重新提问。", true);
  }
  function switchCM(cmId) {
    if (denied || !scopesReady || active || submitting || restoring || saved.client_request_id) return false;
    if (!canManageCM && cmId !== activeContext.cm_id) return false;
    const person = cmScopes.find(person => person.cm_id === cmId);
    if (!person) { status("没有该人员的查看权限。", true); return false; }
    if (person.cm_id !== activeContext.cm_id) resetSession(person, true);
    return true;
  }
  function syncPageContext() {
    if (!pendingPageContext || denied || !scopesReady || active || submitting || restoring || saved.client_request_id) return;
    const context = pendingPageContext; pendingPageContext = null;
    if (context.cm_id && !switchCM(context.cm_id)) return;
    selectContext(context); scope({});
  }
  function date(value) {
    if (!value || Number.isNaN(new Date(value).getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {timeZone: "Europe/London", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(value));
  }
  function selectContext(value, explicit = true) {
    if (!value || !Object.keys(value).length) return;
    if (explicit) scopeVersion++;
    if (["cm_id", "event_ref", "message_ref"].some(key => Object.hasOwn(value, key))) {
      const cmId = value.cm_id || activeContext.cm_id;
      activeContext = {cm_id: cmId, cm_name: cmId === activeContext.cm_id ? activeContext.cm_name : undefined};
    }
    for (const key of ["cm_id", "cm_name", "audience", "creator_name", "creator_names", "chat_names", "event_ref", "event_title", "message_ref", "start", "end", "reply_status", "sort"]) {
      if (Object.hasOwn(value, key)) activeContext[key] = value[key];
    }
    contextChosen = true;
  }
  function adoptTurnScope(view) {
    const value = view.result?.scope || view.scope;
    if (view.status !== "completed" || !value || currentScopeTurn?.id !== view.id ||
        currentScopeTurn.version !== scopeVersion || (value.cm_id && activeContext.cm_id && value.cm_id !== activeContext.cm_id)) return;
    selectContext(value, false); scope(value);
  }
  function scope(value) {
    if (!value || typeof value !== "object") return;
    const matching = !value.cm_id || !activeContext.cm_id || value.cm_id === activeContext.cm_id;
    if (matching) {
      if (!activeContext.cm_id && value.cm_id) activeContext.cm_id = value.cm_id;
      if (activeContext.event_ref && value.event_ref) activeContext.event_ref = value.event_ref;
      for (const key of ["cm_name", "creator_names", "chat_names"]) if (value[key]) activeContext[key] = value[key];
      for (const key of ["start", "end"]) if (value[key]) activeContext[key] = value[key];
      for (const key of ["reply_status", "sort"]) if (Object.hasOwn(value, key)) activeContext[key] = value[key];
    }
    const current = [...new Set([canManageCM ? activeContext.cm_name : null, activeContext.creator_name, ...(activeContext.creator_names || []),
      ...(activeContext.chat_names || []), activeContext.event_title || (activeContext.event_ref ? "当前事项" : null),
      activeContext.audience === "creators" ? "我的达人" : null,
      activeContext.page_label].filter(Boolean))];
    const values = activeContext.start && activeContext.end ? [`${date(activeContext.start)} – ${date(activeContext.end)} · 英国时间`] : [];
    if (matching && !values.length && value.label) values.push(value.label);
    if (activeContext.reply_status === "unreplied") values.push("未回复");
    if (activeContext.sort === "importance") values.push("重要度优先");
    $("oa-scope").replaceChildren(...current.map(text => node("span", "oa-chip oa-chip--page", text)),
      ...[...new Set(values)].filter(text => !current.includes(text)).map(text => node("span", "oa-chip", text)));
    $("oa-scope").hidden = !current.length && !values.length;
  }
  function atBottom() { return panel.hidden ? followBottom : history.scrollHeight - history.scrollTop - history.clientHeight < 64; }
  function scrollToBottom() { followBottom = true; history.scrollTop = history.scrollHeight; $("oa-new-result").hidden = true; }
  function updatedScroll(follow, turnId = null) {
    const result = turnId && [...history.children].find(child => child.dataset.turnId === turnId);
    if (follow && result) {
      history.scrollTop += result.getBoundingClientRect().top - history.getBoundingClientRect().top;
      followBottom = atBottom(); $("oa-new-result").hidden = true;
    } else { followBottom = follow; if (follow) scrollToBottom(); else $("oa-new-result").hidden = false; }
  }
  function userMessage(requestId, text) {
    $("oa-welcome")?.remove();
    for (const placeholder of history.querySelectorAll(":scope > p.oa-answer")) placeholder.remove();
    let message = [...history.children].find(child => child.dataset.requestId === requestId);
    if (!message) {
      message = node("article", "oa-message oa-message--user"); message.dataset.requestId = requestId;
      message.append(node("p", "", text)); history.append(message);
    }
  }
  function gapList(values, summary, precise = false) {
    const groups = new Map(), counts = new Map();
    const labels = {"历史同步未覆盖检索起点": "该时段消息尚未同步完成", "历史回填尚未完成": "该时段消息尚未同步完成",
      "请求早于已确认历史同步范围": "该时段消息尚未同步完成", "非文字消息未分析": "图片、附件等消息暂无法解析",
      "群开启受限模式，Lark禁止读取消息（231203）": "群开启受限模式，无法读取消息"};
    const reasonFor = gap => {
      const raw = typeof gap === "string" ? gap : gap.message || gap.reason || "消息暂不可读取";
      return Number(gap.code || gap.error_code) === 231203 ? "群开启受限模式，无法读取消息" : labels[raw] || raw;
    };
    if (precise) for (const gap of summary || []) {
      const label = [[gap.conversations, "个会话"], [gap.items, "项记录"]]
        .filter(([number]) => Number.isFinite(number) && number > 0).map(([number, unit]) => `${number} ${unit}`).join(" · ");
      if (label) counts.set(reasonFor(gap), label);
    }
    for (const gap of [...(Array.isArray(summary) ? summary : []), ...(Array.isArray(values) ? values : [])]) {
      const reason = reasonFor(gap);
      if (!groups.has(reason)) groups.set(reason, new Set());
      for (const name of [...(gap.names || []), gap.name, gap.conversation_name].filter(Boolean)) groups.get(reason).add(name);
    }
    if (!groups.size) return null;
    const details = node("details", "oa-gaps"); details.append(node("summary", "", "读取情况"));
    for (const [reason, names] of groups) {
      const row = node("div", "", [reason, counts.get(reason)].filter(Boolean).join(" · "));
      if (names.size) row.append(node("p", "oa-gap-names", `会话：${[...names].join("、")}`));
      details.append(row);
    }
    return details;
  }
  function sourceLayout() {
    const overlay = (!$("oa-sources").hidden || !$("oa-reply").hidden) && !(panel.classList.contains("is-fullscreen") && matchMedia("(min-width: 861px)").matches);
    $("oa-chat").inert = overlay;
    if (overlay) $("oa-chat").setAttribute("aria-hidden", "true");
    else $("oa-chat").removeAttribute("aria-hidden");
  }
  function closeSources(focus = true) {
    sourceVersion++; $("oa-sources").hidden = true;
    if ($("oa-reply").hidden) panel.classList.remove("has-sources");
    $("oa-sources-body").replaceChildren(); sourceLayout();
    if (focus && sourceFocus?.isConnected) sourceFocus.focus();
    sourceFocus = null;
  }
  function closeReply() {
    clearTimeout(draftTimer); draftStatusVersion++;
    $("oa-reply").hidden = true;
    if ($("oa-sources").hidden) panel.classList.remove("has-sources");
    sourceLayout();
  }
  function openReply() {
    closeSources(false); $("oa-reply").hidden = false; panel.classList.add("has-sources"); sourceLayout();
    $("oa-reply-close").focus();
    void checkDraftMessages();
  }
  async function checkDraftMessages() {
    clearTimeout(draftTimer);
    if (denied || panel.hidden || $("oa-reply").hidden || document.hidden || !currentDraft?.data.draft_ref) return;
    const draft = currentDraft, version = ++draftStatusVersion;
    try {
      const value = await request(`/drafts/${encodeURIComponent(draft.data.draft_ref)}/status`);
      if (denied || version !== draftStatusVersion || draft !== currentDraft) return;
      let notice = $("oa-reply-body").querySelector(".oa-draft-new-message");
      if (value.new_messages && !notice) {
        notice = node("div", "oa-draft-new-message"); notice.setAttribute("role", "status");
        const refresh = node("button", "", "更新草稿"); refresh.type = "button"; refresh.dataset.ask = "true";
        refresh.addEventListener("click", () => void ask("读取这个会话的最新问答，重新起草英文回复。", {
          event_ref: draft.data.event_ref || null, message_ref: draft.data.message_ref || null, reply_action: "generate"}));
        notice.append(node("span", "", "有新消息，建议核对。"), refresh); $("oa-reply-body").prepend(notice); controls();
      } else if (!value.new_messages) notice?.remove();
    } catch (error) {
      if (!denied && version === draftStatusVersion) status(error.message, true);
    } finally {
      if (!denied && version === draftStatusVersion && draft === currentDraft && !panel.hidden && !$("oa-reply").hidden && !document.hidden) draftTimer = setTimeout(checkDraftMessages, 30000);
    }
  }
  function replyButton(reference, title, message = false) {
    const context = message ? {message_ref: reference} : {event_ref: reference, event_title: title};
    const button = node("button", "oa-reply-action", "帮我回复"); button.type = "button"; button.dataset.ask = "true";
    button.addEventListener("click", () => {
      if (!currentDraft) $("oa-reply-title").textContent = title || "帮我回复";
      openReply();
      if (!currentDraft) $("oa-reply-body").replaceChildren(node("p", "oa-coverage", "正在读取问答并起草…"));
      void ask("针对选中的消息起草一条英文回复，并给出中文参考。", {...context, reply_action: "generate"});
    });
    return button;
  }
  function rawMessages(content, value, replyable = true, reading = true) {
    for (const message of value.messages || []) {
      const item = node("figure");
      item.append(node("figcaption", "", [message.conversation_name || message.chat_name, message.sender_name || (message.is_staff ? "内部同事" : message.speaker === "other" ? "对方" : null),
        date(message.time || message.sent_at || message.created_at),
        {matched: "引用消息", parent: "回复对象", root: "话题起点", nearby_context: "相邻消息"}[message.relation]].filter(Boolean).join(" · ")),
        node("blockquote", "", message.text || "这条消息没有可显示的文字。"));
      const ref = message.message_ref || message.evidence_ref;
      if (replyable && ref && message.text && !message.unreadable) item.append(replyButton(ref, message.conversation_name || message.chat_name || "当前消息", true));
      content.append(item);
    }
    if (!value.messages?.length) content.append(node("p", "oa-coverage", "当前没有可读取的原文。"));
    if (reading) { const gaps = gapList(value.gaps, value.gap_summary); if (gaps) content.append(gaps); }
  }
  function evidenceCard(reference, title = "消息原文", count = null) {
    const button = node("button", "oa-evidence", Number.isFinite(count) ? `查看原文 · ${count} 条` : "查看原文");
    button.type = "button"; button.setAttribute("aria-controls", "oa-sources");
    button.addEventListener("click", async () => {
      if (denied) return;
      const version = ++sourceVersion, content = $("oa-sources-body");
      closeReply();
      sourceFocus = button; $("oa-sources").hidden = false; panel.classList.add("has-sources");
      $("oa-source-title").textContent = title; $("oa-sources-close").focus(); sourceLayout();
      content.replaceChildren(node("p", "oa-coverage", "正在读取原文…"));
      try {
        const value = await request(`/evidence/${encodeURIComponent(reference)}`);
        if (denied || version !== sourceVersion) return;
        content.replaceChildren();
        rawMessages(content, value);
        content.scrollTop = 0;
      } catch (error) { if (!denied && version === sourceVersion) content.replaceChildren(node("p", "oa-gaps", error.message)); }
    });
    return button;
  }
  function draftCard(draft, turnId) {
    if (!draft || typeof draft.draft !== "string" || !draft.draft.trim()) return null;
    const card = node("section", "oa-draft"), editor = node("textarea"), label = node("label", "", "回复草稿 · 可编辑");
    editor.value = draft.draft; editor.rows = 5; editor.setAttribute("aria-label", "回复草稿");
    card.dataset.original = draft.draft;
    const copy = node("button", "", draft.context?.chat_url ? "复制并打开 Lark" : "复制回复"), feedback = node("span", "oa-copy-feedback");
    copy.type = "button"; feedback.setAttribute("role", "status");
    if (draft.context?.message) {
      const context = node("details", "oa-reply-context"); context.open = true; context.tabIndex = 0;
      context.append(node("summary", "", draft.context.chat_name || "原文与最新进展"));
      const messageKey = message => message.chat_id && message.message_id ? `${message.chat_id}:${message.message_id}` :
        message.ref ? `ref:${message.ref}` : JSON.stringify([message.conversation_name || message.chat_name,
          message.sender_name, message.time || message.sent_at, message.text]);
      const necessary = new Map([draft.context.message, ...(draft.evidence || [])].map(message => [messageKey(message), message]));
      rawMessages(context, {messages: [...necessary.values()]}, false);
      const remaining = [...new Map((draft.context.messages || []).map(message => [messageKey(message), message])).values()]
        .filter(message => !necessary.has(messageKey(message)));
      if (remaining.length) {
        const more = node("details", "oa-reply-more");
        more.append(node("summary", "", `更多上下文（${remaining.length}）`));
        rawMessages(more, {messages: remaining}, false); context.append(more);
      }
      card.append(context);
    }
    card.append(label, editor);
    const translation = node("p", "oa-translation", draft.chinese_translation || "");
    const translationLabel = node("div", "oa-translation-label", "中文参考");
    translationLabel.hidden = translation.hidden = !draft.chinese_translation || draft.language === "zh"; card.append(translationLabel, translation);
    editor.addEventListener("input", () => {
      card.dataset.edited = String(editor.value !== draft.draft);
      copy.disabled = !editor.value.trim();
      translationLabel.textContent = editor.value !== draft.draft && draft.chinese_translation ? "中文参考 · 待更新" : "中文参考";
      feedback.textContent = "";
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
        if (draft.context?.chat_url) {
          let url;
          try { url = new URL(draft.context.chat_url); } catch { /* Invalid conversation links are not opened. */ }
          if (url?.protocol === "https:" && ["applink.larksuite.com", "applink.feishu.cn"].includes(url.hostname)) {
            if (!window.open(url.href, "_blank", "noopener,noreferrer")) {
              const open = node("a", "", "打开 Lark"); open.href = url.href; open.target = "_blank"; open.rel = "noopener noreferrer";
              feedback.replaceChildren(document.createTextNode("已复制，未发送 · "), open);
            }
          }
        }
      } catch (error) {
        if (!denied) feedback.textContent = error.name === "NotAllowedError" ? "浏览器未允许复制，可选中文字手动复制" : error.message;
      } finally { if (!denied) copy.disabled = !editor.value.trim(); }
    });
    const actions = node("div", "oa-draft-tools");
    for (const [text, action] of [["更短", "shorter"], ["更自然", "natural"], ["礼貌催办", "follow_up"], ["换成英文", "english"], ["更新中文", "translate"]]) {
      const button = node("button", "", text); button.type = "button"; button.dataset.ask = "true";
      button.addEventListener("click", () => {
        if (!draft.draft_ref || !editor.value.trim()) { feedback.textContent = "请先重新生成这条草稿。"; return; }
        void ask(`${text}当前草稿，保留已确认事实。`, {event_ref: draft.event_ref || null, message_ref: draft.message_ref || null,
          draft_ref: draft.draft_ref, edited_draft: editor.value, reply_action: action});
      });
      actions.append(button);
    }
    card.append(actions);
    if (draft.checks?.length || draft.explanation) card.append(node("p", "oa-draft-checks", [draft.explanation, ...(draft.checks || [])].filter(Boolean).join(" · ")));
    card.append(copy, feedback);
    return card;
  }
  function showDraft(draft, turnId, requested = false) {
    const body = $("oa-reply-body"), previous = currentDraft?.card;
    const edited = previous?.dataset.edited === "true" || previous?.contains(document.activeElement);
    const changedDuringAction = previous && activeReplyText != null && previous.querySelector("textarea").value !== activeReplyText;
    if (previous && activeReplyAction === "translate") {
      const translation = previous.querySelector(".oa-translation");
      if (changedDuringAction) {
        previous.querySelector(".oa-translation-label").textContent = "中文参考 · 待更新";
        previous.querySelector(".oa-copy-feedback").textContent = "正文又有修改，请更新中文。";
      } else {
        translation.textContent = draft.chinese_translation || ""; translation.hidden = !draft.chinese_translation;
        previous.querySelector(".oa-translation-label").textContent = "中文参考";
        previous.querySelector(".oa-copy-feedback").textContent = "中文参考已更新";
        Object.assign(currentDraft.data, draft, {draft: previous.querySelector("textarea").value});
      }
    } else if (previous && (edited || changedDuringAction) && (!activeReplyAction || activeReplyAction === "generate" || changedDuringAction)) {
      currentDraft.next = {draft, turnId};
      if (!body.querySelector(".oa-draft-update")) {
        const replace = node("button", "oa-draft-update"); replace.type = "button";
        replace.addEventListener("click", () => {
          const next = currentDraft.next, card = draftCard(next.draft, next.turnId);
          if (card) { body.replaceChildren(card); currentDraft = {card, data: next.draft, turnId: next.turnId}; $("oa-reply-title").textContent = next.draft.context?.chat_name || "回复草稿"; controls(); void checkDraftMessages(); }
        }); body.prepend(replace);
      }
      body.querySelector(".oa-draft-update").textContent = `新草稿${draft.context?.chat_name ? ` · ${draft.context.chat_name}` : ""} · 点击更新`;
    } else {
      const card = draftCard(draft, turnId);
      if (card) { body.replaceChildren(card); currentDraft = {card, data: draft, turnId}; $("oa-reply-title").textContent = draft.context?.chat_name || "回复草稿"; }
    }
    if (requested) openReply();
    else if (!$("oa-reply").hidden) void checkDraftMessages();
  }
  function askButton(label, prompt, context = {}) {
    const button = node("button", "", label); button.type = "button"; button.dataset.ask = "true";
    button.addEventListener("click", () => void ask(prompt, context)); return button;
  }
  function table(headers, rows) {
    const wrap = node("div", "oa-table-wrap"), value = node("table", "oa-table"), head = node("thead"), body = node("tbody");
    const title = node("tr"); headers.forEach(text => title.append(node("th", "", text))); head.append(title);
    for (const cells of rows.slice(0, 25)) {
      const row = node("tr");
      for (const cell of cells) { const item = node("td"); item.append(cell instanceof Node ? cell : node("span", "", cell ?? "—")); row.append(item); }
      body.append(row);
    }
    value.append(head, body); wrap.append(value); return wrap;
  }
  function metrics(values, previous = null, all = false) {
    const list = node("div", "oa-metrics");
    const labels = all ? {messages: "消息", own_messages: "本人消息", active_people: "活跃人员",
      contacted_people: "联系人数", conversations: "会话"} : {messages: "关联消息", own_messages: "本人消息",
      active_creators: "活跃达人", contacted_creators: "已联系达人", conversations: "会话"};
    for (const [key, label] of Object.entries(labels)) {
      if (!Number.isFinite(values?.[key])) continue;
      const item = node("div", "oa-metric"); item.append(node("span", "", label), node("strong", "", values[key]));
      if (Number.isFinite(previous?.[key])) item.append(node("small", "", `前一时段 ${previous[key]}`));
      list.append(item);
    }
    return list;
  }
  function windowSources(data) {
    const card = node("section", "oa-data oa-source-entry"), details = node("details", "oa-coverage");
    let pageNumber = data.page || 1, currentPage = data;
    const count = data.total_messages ?? data.coverage?.total_messages ?? data.messages?.length ?? 0;
    details.append(node("summary", "", `来源 · ${count} 条消息${data.total_conversations ? ` · ${data.total_conversations} 个会话` : ""}`));
    const body = node("div", "oa-source-page"), footer = node("div", "oa-source-pagination");
    const pageLabel = node("span", "oa-coverage"), previous = node("button", "", "上一页"), next = node("button", "", "下一页");
    previous.type = next.type = "button";
    const draw = value => {
      currentPage = value;
      body.replaceChildren();
      rawMessages(body, value, true, false);
      const pageCount = value.page_count || data.page_count || 1;
      pageLabel.textContent = `第 ${pageNumber} / ${pageCount} 页`;
      previous.disabled = pageNumber <= 1; next.disabled = !(value.has_more ?? pageNumber < pageCount);
      footer.hidden = pageCount <= 1 || !data.source_url;
    };
    const load = async step => {
      if (denied) return;
      const requestedPage = pageNumber + step, version = revision;
      previous.disabled = next.disabled = true;
      try {
        const url = new URL(data.source_url, location.origin);
        if (url.origin !== location.origin || !url.pathname.startsWith("/assistant/sources/")) throw new Error("来源地址无效。");
        url.searchParams.set("page", requestedPage);
        const value = await request(url.pathname.slice("/assistant".length) + url.search);
        if (denied || version !== revision || !card.isConnected) return;
        pageNumber = requestedPage; draw(value); controls();
      } catch (error) { if (!denied && version === revision) { draw(currentPage); body.append(node("p", "oa-gaps", error.message)); } }
    };
    previous.addEventListener("click", () => void load(-1)); next.addEventListener("click", () => void load(1));
    footer.append(previous, pageLabel, next); details.append(body, footer);
    if (data.window?.start && data.window?.end) details.append(node("p", "oa-card-meta", `${date(data.window.start)} – ${date(data.window.end)} · 英国时间`));
    card.append(details); draw(data); return card;
  }
  function dataCard(data) {
    const titles = {creators: "达人资料", groups: "群聊目录", sync: "连接与消息读取", activity: "活动统计", messages: "消息命中",
      window_messages: "总结来源", message_context: "原文与问答"};
    if (!data || !titles[data.kind]) return null;
    if (data.kind === "window_messages") return windowSources(data);
    const card = node("section", "oa-data"); card.dataset.queryRef = data.query_ref || "";
    card.append(node("h4", "", titles[data.kind]));
    if (data.window?.start && data.window?.end) card.append(node("p", "oa-card-meta",
      `${date(data.window.start)} – ${date(data.window.end)} · 英国时间`));
    if (data.kind === "creators") {
      const rows = (data.creators || []).map(person => {
        const name = node("div", "", person.name || "未命名达人");
        if (person.handles?.length) name.append(node("small", "", person.handles.join(" · ")));
        if (person.last_contact_at) name.append(node("small", "", `最近联系 ${date(person.last_contact_at)}`));
        return [name, [person.importance?.label || person.importance?.tier_label,
          ...(person.responsible || []).map(owner => owner.name)].filter(Boolean).join(" · ") || "—",
          askButton("查看情况", `查看 ${person.name || "这位达人"}${person.handles?.length ? `（${person.handles.join("、")}）` : ""} 的最新情况`)];
      });
      if (rows.length) card.append(table(["达人", "分层 · 负责人", ""], rows));
      else card.append(node("p", "oa-coverage", "当前范围没有匹配的达人。"));
      if (Number.isFinite(data.total)) card.append(node("p", "oa-coverage", `共 ${data.total} 位 · 本页 ${rows.length} 位`));
    } else if (data.kind === "groups") {
      const rows = (data.groups || []).map(group => {
        const name = node("div", "", group.name || "未命名群聊");
        if (typeof group.sync_enabled === "boolean") name.append(node("small", "", group.sync_enabled ? "已启用同步" : "未启用同步"));
        return [name, group.purpose_label || group.purpose || "—", Number.isFinite(group.member_count) ? group.member_count : "—"];
      });
      if (rows.length) card.append(table(["群聊", "用途", "成员"], rows));
      else card.append(node("p", "oa-coverage", "当前范围没有匹配的群聊。"));
      if (Number.isFinite(data.total)) card.append(node("p", "oa-coverage", `共 ${data.total} 个 · 本页 ${rows.length} 个`));
    } else if (data.kind === "sync") {
      if (data.summary?.label) card.append(node("p", "", data.summary.label));
      if (data.summary?.detail) card.append(node("p", "oa-coverage", data.summary.detail));
      const rows = [];
      for (const [key, label] of Object.entries({discovered_conversation_count: "已发现会话", syncable_conversation_count: "可同步会话",
        backfill_complete_count: "历史消息已读取", syncing_conversation_count: "正在同步", not_readable_conversation_count: "无法读取"})) {
        const count = (data.conversations?.counts || data.conversations)?.[key]; if (Number.isFinite(count)) rows.push([label, count]);
      }
      if (rows.length) card.append(table(["范围", "会话数"], rows));
      if (data.watermarks?.latest) card.append(node("p", "oa-coverage", `最近同步时间 ${date(data.watermarks.latest)}`));
      if (typeof data.definition === "string") card.append(node("p", "oa-coverage", data.definition));
    } else if (data.kind === "activity") {
      const all = data.audience === "all" || data.scope?.audience === "all";
      card.append(metrics(data.metrics, data.comparison?.metrics, all));
      const groupLabels = {creator: "达人", person: "人员", conversation: "会话", day: "日期"};
      const rows = (data.rows || []).map(row => [row.creator_name || row.person_name || row.conversation_name || row.day || "未命名",
        Number.isFinite(row.messages) ? row.messages : "—", Number.isFinite(row.own_messages) ? row.own_messages : "—"]);
      if (rows.length) card.append(table([groupLabels[data.group_by] || "分组", all ? "消息" : "关联消息", "本人消息"], rows));
      if (data.rows?.length > 25) card.append(node("p", "oa-coverage", `共 ${data.rows.length} 行，显示前 25 行。`));
      if (data.definition && typeof data.definition === "object") {
        const details = node("details", "oa-coverage"); details.append(node("summary", "", "统计口径"));
        for (const key of ["messages", "active_creators", "contacted_creators", "active_people", "contacted_people", "rows"]) if (data.definition[key]) details.append(node("p", "", data.definition[key]));
        card.append(details);
      }
    } else if (data.kind === "message_context") {
      const message = data.message || {};
      card.append(node("div", "oa-card-meta", [message.conversation_name || message.chat_name, message.sender_name,
        date(message.time || message.sent_at)].filter(Boolean).join(" · ")));
      const text = message.text || "这条消息没有可显示的文字。";
      card.append(node("p", "", text.length > 420 ? `${text.slice(0, 420)}…` : text));
      const ref = data.message_ref || data.evidence_ref || message.message_ref;
      if (ref) card.append(evidenceCard(ref, message.conversation_name || message.chat_name || "原文与问答"), replyButton(ref, message.conversation_name || message.chat_name || "当前消息", true));
    } else if (data.kind === "messages") {
      for (const message of (data.messages || []).slice(0, 25)) {
        const hit = node("div", "oa-hit");
        hit.append(node("div", "oa-card-meta", [message.conversation_name || message.chat_name, message.sender_name,
          date(message.time || message.sent_at)].filter(Boolean).join(" · ")));
        const text = message.text || (message.unreadable ? "正文暂不可读取" : "这条消息没有可显示的文字。");
        hit.append(node("p", "", text.length > 420 ? `${text.slice(0, 420)}…` : text));
        const ref = message.evidence_ref || message.message_ref;
        if (ref) {
          hit.append(evidenceCard(ref, message.conversation_name || message.chat_name || "消息原文"));
          if (message.text && !message.unreadable) hit.append(replyButton(ref, message.conversation_name || message.chat_name || "当前消息", true));
        }
        card.append(hit);
      }
      if (!data.messages?.length) card.append(node("p", "oa-coverage", "这次扫描未找到可读的匹配消息。"));
      const coverage = data.coverage || {}, counts = [];
      for (const [key, label] of [["scanned", "扫描"], ["readable", "可读"], ["unreadable", "不可读"], ["matched", "命中"]]) {
        if (Number.isFinite(coverage[key])) counts.push(`${label} ${coverage[key]}`);
      }
      if (counts.length) card.append(node("p", "oa-coverage", `本页：${counts.join(" · ")}，不是全库总量。`));
      if (coverage.scan_complete === false) card.append(node("p", "oa-coverage", "扫描尚未结束，可继续下一页。"));
    }
    if (data.checked_at) card.append(node("p", "oa-coverage", `查询于 ${date(data.checked_at)}`));
    if (data.kind === "messages" && data.next_source_url) {
      const actions = node("div", "oa-card-actions"), next = node("button", "", "下一页"); next.type = "button";
      next.addEventListener("click", async () => {
        const version = revision; next.disabled = true;
        try {
          const url = new URL(data.next_source_url, location.origin);
          if (url.origin !== location.origin || !url.pathname.startsWith("/assistant/sources/")) throw new Error("来源地址无效。");
          const value = await request(url.pathname.slice("/assistant".length) + url.search);
          if (denied || version !== revision || !card.isConnected) return;
          const page = dataCard(value); if (page) card.replaceWith(page); controls();
        } catch (error) { if (!denied && version === revision) { next.disabled = false; actions.append(node("span", "oa-gaps", error.message)); } }
      });
      actions.append(next); card.append(actions);
    } else if (data.kind !== "messages" && data.next_cursor && data.query_ref) {
      const actions = node("div", "oa-card-actions");
      actions.append(askButton("继续下一页", "继续查看这次查询的下一页", {...data.scope, query_ref: data.query_ref, cursor: data.next_cursor})); card.append(actions);
    }
    return card;
  }
  function renderResult(container, result) {
    container.replaceChildren(answer(result.answer));
    const queryWindow = result.window || [...(result.data || [])].reverse().find(data => data.actual?.window || data.window);
    scope({...result.scope, ...(queryWindow?.actual?.window || queryWindow?.window || queryWindow)});
    const groups = new Map([["reply", {label: "需要我回复", element: node("details", "oa-event-group")}],
      ["followup", {label: "需要内部跟进", element: node("details", "oa-event-group")}],
      ["waiting", {label: "等待对方", element: node("details", "oa-event-group")}],
      ["closed", {label: "已解决／无需处理", element: node("details", "oa-event-group")}]]);
    let expanded = 0;
    (result.events || []).forEach((event, index) => {
      const action = event.action_group || (["resolved", "not_required", "no_action"].includes(event.handling_status) ? "closed" : event.reply_status === "replied" ? "followup" : "reply");
      const group = groups.get(action) || groups.get("followup");
      const card = node("details", "oa-event"), header = node("summary", "oa-event-title"), meta = node("div", "oa-event-meta");
      card.open = action !== "closed" && expanded++ < 8;
      const importance = event.importance?.tier_label || {focus: "重点", priority: "优先", normal: "常规",
        dormant: "久未联系", pending: "待评估"}[event.importance?.tier];
      for (const text of [event.creator_handle || event.lark_user_name || event.creator_name,
        importance && `重要度：${importance}`, event.priority]) {
        if (text) meta.append(node("span", "", text));
      }
      header.append(meta, node("h4", "", `${index + 1}. ${event.title || "聊天事项"}`)); card.append(header);
      const states = node("details", "oa-event-states");
      states.append(node("summary", "", "状态详情"), node("div", "", `回复状态：${{unreplied: "未见回复", replied: "已回复", uncertain: "待核对"}[event.reply_status] || "待核对"} · 处理状态：${{unresolved: "未解决", resolved: "已解决", not_required: "无需处理", no_action: "无需处理", uncertain: "待核对"}[event.handling_status] || "待核对"}`));
      if (event.conversations?.length) {
        const conversations = node("div", "oa-card-meta");
        for (const chat of event.conversations) conversations.append(node("span", "",
          `${{private: "私聊", group: "群聊"}[chat.type] || "会话"}：${chat.name || "未命名会话"}`));
        card.append(conversations);
      }
      if (event.summary) card.append(node("p", "", event.summary));
      if (event.latest_progress) card.append(node("p", "", event.latest_progress));
      if (event.deadline_day) card.append(node("div", "oa-deadline", event.due_today ? "今天截止" : `截止 ${event.deadline_day}`));
      if (action === "reply" && Number.isFinite(event.waiting_seconds) && event.waiting_seconds > 0) card.append(node("div", "oa-card-meta", `原文中已等待 ${event.waiting_seconds >= 86400 ? `${Math.floor(event.waiting_seconds / 86400)} 天` : event.waiting_seconds >= 3600 ? `${Math.floor(event.waiting_seconds / 3600)} 小时` : `${Math.max(1, Math.floor(event.waiting_seconds / 60))} 分钟`}`));
      card.append(states);
      const actions = node("div", "oa-card-actions");
      if (event.evidence_ref || event.event_ref) actions.append(evidenceCard(event.evidence_ref || event.event_ref, event.title || "聊天事项原文", event.evidence_count));
      if (event.event_ref) actions.append(askButton("最新进展", "查看这个事项的最新进展", {event_ref: event.event_ref, event_title: event.title || "聊天事项"}),
        replyButton(event.event_ref, event.title || "聊天事项"));
      if (actions.children.length) card.append(actions);
      group.element.append(card);
    });
    for (const [key, group] of groups) if (group.element.children.length) {
      group.element.open = key !== "closed";
      group.element.prepend(node("summary", "", `${group.label} · ${group.element.children.length} 项`)); container.append(group.element);
    }
    const sourcePages = (result.data || []).filter(data => data.kind === "window_messages");
    let sourcesAdded = false;
    for (const data of result.data || []) {
      if (data.kind === "window_messages" && sourcesAdded) continue;
      const value = data.kind === "window_messages" && sourcePages.length > 1 ? {...data,
        messages: [...new Map(sourcePages.flatMap(page => page.messages || []).map(message => [message.message_ref || message.evidence_ref || JSON.stringify(message), message])).values()]} : data;
      const card = dataCard(value); if (card) container.append(card);
      if (data.kind === "window_messages") sourcesAdded = true;
    }
    if (result.draft?.draft?.trim()) {
      const draft = {...result.draft, draft_ref: result.draft.draft_ref || result.draft_ref};
      showDraft(draft, container.dataset.turnId, Boolean(activeReplyAction));
      const openDraft = node("button", "oa-evidence", "查看回复草稿"); openDraft.type = "button";
      openDraft.addEventListener("click", () => { showDraft(draft, container.dataset.turnId); openReply(); }); container.append(openDraft);
    }
    const readingSources = (result.data || []).filter(data => data.gaps?.length || data.gap_summary?.length);
    const reading = gapList([...(result.gaps || []), ...readingSources.flatMap(data => data.gaps || []), ...(result.draft?.context?.gaps || [])],
      result.gap_summary || readingSources.flatMap(data => data.gap_summary || []),
      readingSources.length === 1 && !result.gaps?.length && !result.draft?.context?.gaps?.length);
    if (reading) container.append(reading);
    const coverage = result.coverage;
    if (coverage && typeof coverage === "object") {
      const counts = [];
      if (Number.isFinite(coverage.responsible_creators)) counts.push(`负责达人 ${coverage.responsible_creators} 位`);
      if (Number.isFinite(coverage.matched_events)) counts.push(`匹配事项 ${coverage.matched_events} 项`);
      if (coverage.analysis_complete === false) counts.push("部分结果");
      if (Number.isFinite(coverage.conversations)) counts.push(`${coverage.conversations} 个会话`);
      if (Number.isFinite(coverage.messages)) counts.push(`${coverage.messages} 条消息`);
      const cutoff = coverage.cutoff || result.window?.cutoff;
      if (cutoff) counts.push(`读取至 ${date(cutoff)}`);
      if (counts.length) container.append(node("p", "oa-coverage", counts.join(" · ")));
    }
    if (result.events?.length) {
      const followup = node("div", "oa-followup");
      for (const [label, prompt] of [["只看未解决", "只看刚才结果中还没解决的事项"], ["查看最新进展", "刚才这些事项后来有什么进展？"]]) {
        followup.append(askButton(label, prompt));
      }
      container.append(followup);
    }
  }
  function turn(view) {
    if (denied) return;
    if (view.error_code === "permission") { clearProtected(); return; }
    if (view.error_code === "source_changed") { sourceChanged(view.error); return; }
    if (view.scope) scope(view.scope);
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
  }
  function finish(state) {
    source?.close(); source = null; active = null; controls();
    activeReplyAction = null; activeReplyText = null;
    if (state === "completed") status("");
    else status(labels[state] || "这轮处理已结束", state !== "cancelled");
    syncPageContext();
  }
  function watch(view, restoredVersion = scopeVersion) {
    if (denied) return;
    source?.close();
    if (currentScopeTurn?.id !== view.id) currentScopeTurn = {id: view.id, version: restoredVersion};
    if (terminal.has(view.status)) { finish(view.status); return; }
    active = view.id; controls();
    const watching = view.id;
    status(labels[view.status] || "正在读取进度");
    source = new EventSource(`/assistant/turns/${encodeURIComponent(watching)}/events`);
    source.addEventListener("scope", event => { if (active === watching) scope(JSON.parse(event.data)); });
    source.addEventListener("progress", event => {
      if (active !== watching) return;
      const progress = JSON.parse(event.data);
      const counts = [];
      if (Number.isFinite(progress.messages)) counts.push(`已读取 ${progress.messages}${Number.isFinite(progress.total_messages) ? ` / ${progress.total_messages}` : ""} 条`);
      if (Number.isFinite(progress.conversations)) counts.push(`${progress.conversations} 个会话`);
      if (Number.isFinite(progress.segments_completed) && Number.isFinite(progress.segments_total)) counts.push(`已整理 ${progress.segments_completed} / ${progress.segments_total} 段`);
      status([progress.message || labels[progress.status] || "正在整理", ...counts].join(" · "));
    });
    source.addEventListener("result", event => {
      if (active !== watching || denied) return;
      const follow = atBottom();
      const completed = {...view, status: "completed", result: JSON.parse(event.data)};
      adoptTurnScope(completed); turn(completed); controls(); updatedScroll(follow, completed.id);
    });
    source.addEventListener("error", event => {
      if (active !== watching) return;
      if (typeof event.data === "string") {
        const value = JSON.parse(event.data);
        if (value.code === "source_changed") { sourceChanged(value.message); return; }
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
    const readScopeVersion = scopeVersion;
    const follow = !history.querySelector(".oa-message") || atBottom();
    try {
      const value = await request(`/sessions/${encodeURIComponent(saved.session_id)}`);
      if (denied || version !== revision) return;
      const received = value.turns || [];
      const turns = received.filter(view => view.error_code !== "source_changed");
      const running = turns.find(view => !terminal.has(view.status));
      if (!running && received.at(-1)?.error_code === "source_changed") {
        sourceChanged(received.at(-1).error); return;
      }
      const changed = received.filter(view => view.error_code === "source_changed");
      if (changed.length) {
        closeSources(false);
        if (changed.some(view => view.id === currentDraft?.turnId)) {
          closeReply(); currentDraft = null; $("oa-reply-body").replaceChildren();
        }
        for (const child of [...history.children]) {
          if (changed.some(view => child.dataset.turnId === view.id || child.dataset.requestId === view.client_request_id)) child.remove();
        }
      }
      const latest = turns.at(-1);
      const latestScope = latest?.result?.scope || latest?.scope;
      const sessionCM = latestScope?.cm_id || saved.cm_id;
      const unconfirmed = saved.client_request_id && !received.some(view => view.client_request_id === saved.client_request_id);
      if (turns.length && sessionCM && sessionCM !== activeContext.cm_id) {
        if (canManageCM && (running || unconfirmed)) {
          const person = cmScopes.find(person => person.cm_id === sessionCM);
          if (!person) { clearProtected(); return; }
          selectContext(person); $("oa-cm").value = person.cm_id;
        } else { resetSession(cmScopes.find(person => person.cm_id === activeContext.cm_id), true); return; }
      }
      if (!turns.length) {
        closeSources(false); currentScopeTurn = null;
        closeReply(); currentDraft = null; $("oa-reply-body").replaceChildren();
        history.replaceChildren(node("p", "oa-answer", "开始一个新问题。"));
        selectContext({cm_id: activeContext.cm_id, cm_name: activeContext.cm_name}); scope({});
        if (!saved.client_request_id) status("");
      }
      saved.cm_id = activeContext.cm_id; persist();
      const restoreScope = !contextChosen && readScopeVersion === scopeVersion &&
        !["creator_ids", "chat_ids", "event_ref"].some(key => pageContext[key]);
      if (restoreScope && latestScope?.cm_id === activeContext.cm_id) selectContext(latestScope, false);
      if (latest) adoptTurnScope(latest);
      for (const view of turns) {
        const cmId = view.result?.scope?.cm_id || view.scope?.cm_id || sessionCM;
        if (!cmId || cmId === activeContext.cm_id || view.error_code === "permission") turn(view);
      }
      if (denied) return;
      if (received.some(view => view.client_request_id === saved.client_request_id)) {
        delete saved.client_request_id; pending = null; persist();
      }
      if (running) watch(running, restoreScope ? scopeVersion : null);
      else { source?.close(); source = null; active = null; controls(); }
      if (saved.client_request_id) status("上次提交尚未确认，重新输入后重试会复用原请求编号。", true);
      else if (turns.length && !running) status("");
      updatedScroll(follow, latest?.id);
    } catch (error) {
      if (denied || version !== revision) return;
      if (error.status === 404 && !saved.client_request_id) { saved = {}; persist(); }
      if (error.status !== 404) scopesReady = false;
      status(error.status === 404 ? (saved.client_request_id ? "上次请求尚未保存，可重新输入并提交。" :
        "上次会话已不可读取，可开始新对话。") : error.message, true);
    } finally { restoring = false; controls(); syncPageContext(); }
  }
  async function ask(text, selectedContext = {}) {
    if (denied || active || submitting || restoring) return;
    if (!await loadScopes() || denied || active || submitting || restoring) return;
    if (selectedContext.cm_id && !switchCM(selectedContext.cm_id)) return;
    text = text.trim(); if (!text) return;
    if (!saved.session_id) saved.session_id = crypto.randomUUID();
    if (!saved.client_request_id) saved.client_request_id = crypto.randomUUID();
    saved.cm_id = activeContext.cm_id;
    if (!pending) {
      activeReplyAction = selectedContext.reply_action || null;
      activeReplyText = selectedContext.edited_draft ?? null;
      const context = {cm_id: activeContext.cm_id, ...Object.fromEntries(
        ["event_ref", "message_ref", "reply_action", "draft_ref", "edited_draft", "query_ref", "cursor"]
          .filter(key => Object.hasOwn(selectedContext, key)).map(key => [key, selectedContext[key]]))};
      selectContext({...context, cm_name: activeContext.cm_name, ...(selectedContext.event_title ? {event_title: selectedContext.event_title} : {})}); scope({});
      pendingScopeVersion = scopeVersion;
      pending = {session_id: saved.session_id, client_request_id: saved.client_request_id, message: text,
        context};
    }
    persist(); submitting = true; controls(); userMessage(pending.client_request_id, pending.message); scrollToBottom();
    status("正在保存请求…");
    const version = revision;
    const submittedScopeVersion = pendingScopeVersion;
    try {
      const value = await request("/turns", pending);
      if (denied || version !== revision) return;
      delete saved.client_request_id; pending = null; persist(); input.value = "";
      currentScopeTurn = {id: value.id, version: submittedScopeVersion};
      adoptTurnScope(value); turn(value);
      if (!denied && version === revision) watch(value);
    } catch (error) {
      if (denied || version !== revision) return;
      if (error.status === 400 || error.status === 413 || error.status === 415 || error.status === 422) {
        delete saved.client_request_id; pending = null; persist();
      }
      status(error.message + (saved.client_request_id ? " 重试将复用这次请求。" : ""), true);
    } finally { if (version === revision) { submitting = false; controls(); syncPageContext(); } }
  }
  function open() {
    panel.hidden = false; $("oa-launcher").hidden = true; $("oa-launcher").setAttribute("aria-expanded", "true");
    if (matchMedia("(max-width: 600px)").matches) fullscreen(true);
    if (followBottom) scrollToBottom();
    if (!$("oa-reply").hidden) $("oa-reply-close").focus();
    else if ($("oa-sources").hidden) input.focus(); else $("oa-sources-close").focus();
    void loadScopes();
    if (!$("oa-reply").hidden) void checkDraftMessages();
  }
  function collapse() {
    clearTimeout(draftTimer); draftStatusVersion++;
    panel.hidden = true; $("oa-launcher").hidden = false; $("oa-launcher").setAttribute("aria-expanded", "false");
    $("oa-launcher").focus();
  }
  function fullscreen(enabled) {
    enabled ||= matchMedia("(max-width: 600px)").matches;
    panel.classList.toggle("is-fullscreen", enabled); panel.setAttribute("aria-modal", String(enabled));
    $("oa-fullscreen").setAttribute("aria-label", enabled ? "退出全屏" : "全屏");
    $("oa-fullscreen").title = enabled ? "退出全屏" : "全屏";
    sourceLayout();
  }
  $("oa-launcher").addEventListener("click", open);
  $("oa-collapse").addEventListener("click", collapse);
  $("oa-fullscreen").addEventListener("click", () => fullscreen(!panel.classList.contains("is-fullscreen")));
  $("oa-cm").addEventListener("change", () => {
    if (!switchCM($("oa-cm").value)) $("oa-cm").value = activeContext.cm_id || "";
    else input.focus();
  });
  for (const button of app.querySelectorAll("[data-quick]")) button.addEventListener("click", () => {
    const prompt = {unreplied: "查看昨天至现在我负责达人的未回复事项，按达人重要度优先展示。",
      today: "总结今天聊天里的重点事项。", reply: "帮我给刚才讨论的消息写一条英文回复，并给出中文参考。"};
    void ask(prompt[button.dataset.quick]);
  });
  $("oa-new").addEventListener("click", () => {
    if (denied || !scopesReady || active || submitting || restoring || saved.client_request_id) return;
    resetSession(cmScopes.find(person => person.cm_id === activeContext.cm_id)); input.focus();
  });
  $("oa-form").addEventListener("submit", event => { event.preventDefault(); void ask(input.value); });
  input.addEventListener("input", controls);
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void ask(input.value); }
  });
  $("oa-stop").addEventListener("click", async () => {
    if (!active) return;
    const stopping = active; $("oa-stop").disabled = true;
    try { const value = await request(`/turns/${encodeURIComponent(stopping)}/cancel`, {}); if (!denied && active === stopping) { adoptTurnScope(value); turn(value); watch(value); } }
    catch (error) { if (!denied) { status(error.message, true); $("oa-stop").disabled = false; } }
  });
  panel.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!$("oa-reply").hidden) closeReply();
      else if (!$("oa-sources").hidden) closeSources();
      else if (panel.classList.contains("is-fullscreen") && !matchMedia("(max-width: 600px)").matches) fullscreen(false);
      else collapse();
    }
    if (event.key !== "Tab" || !panel.classList.contains("is-fullscreen")) return;
    const elements = [...panel.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex='0']")]
      .filter(element => element.getClientRects().length && !element.closest("[inert]"));
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  $("oa-sources-close").addEventListener("click", () => closeSources());
  $("oa-reply-close").addEventListener("click", () => { closeReply(); input.focus(); });
  $("oa-new-result").addEventListener("click", scrollToBottom);
  history.addEventListener("scroll", () => {
    if (panel.hidden) return;
    followBottom = atBottom(); if (followBottom) $("oa-new-result").hidden = true;
  });
  window.addEventListener("resize", () => {
    if (matchMedia("(max-width: 600px)").matches) fullscreen(true); else sourceLayout();
  });
  document.addEventListener("ttmedia:assistant", async event => {
    const detail = event.detail;
    if (!detail || denied) return;
    const context = detail.context && typeof detail.context === "object" && !Array.isArray(detail.context) ? detail.context : {};
    const message = typeof detail.message === "string" ? detail.message.trim() : "";
    if (!message) {
      pendingPageContext = context;
      if (await loadScopes()) syncPageContext();
      return;
    }
    pendingPageContext = null; open();
    if (!await loadScopes()) return;
    if (active || submitting || restoring) { status("当前问题还在处理，完成后请再查看。"); return; }
    if (saved.client_request_id) { status("上次提交尚未确认，请先恢复或重试原请求。", true); return; }
    if (context.cm_id && !switchCM(context.cm_id)) return;
    input.value = message; controls();
    void ask(message, context);
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(draftTimer); draftStatusVersion++;
    if (!document.hidden) void checkDraftMessages();
  });
  window.addEventListener("pagehide", () => { source?.close(); clearTimeout(restoreTimer); clearTimeout(draftTimer); draftStatusVersion++; });
  if (app.dataset.configured !== "true") status("助手尚未配置，请联系管理员。", true);
  scope({}); controls();
})();
