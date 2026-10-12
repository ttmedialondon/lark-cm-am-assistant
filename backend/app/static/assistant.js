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
  let cmScopes = [], scopesReady = false, scopeRequest = null;
  let pendingPageContext = null;
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
    for (const button of app.querySelectorAll("[data-prompt], [data-ask]")) button.disabled = denied || busy || Boolean(saved.client_request_id);
    $("oa-form").setAttribute("aria-busy", String(busy));
  }
  function clearProtected() {
    denied = true; revision++; source?.close(); source = null; active = null; pending = null;
    activeContext = {}; scopeVersion++; currentScopeTurn = null;
    closeSources(false);
    delete saved.client_request_id; persist();
    history.replaceChildren(node("p", "oa-answer", "登录或查看权限已变化，请重新登录后打开助手。"));
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
        const selected = (saved.client_request_id && cmScopes.find(person => person.cm_id === saved.cm_id)) ||
          cmScopes.find(person => person.cm_id === pageContext.cm_id) ||
          cmScopes.find(person => person.cm_id === value.selected_cm_id);
        if (!selected) { status("暂无可查看的已授权人员。", true); return false; }
        $("oa-cm").replaceChildren(...cmScopes.map(person => {
          const option = node("option", "", person.cm_name || "未命名人员"); option.value = person.cm_id; return option;
        }));
        $("oa-person").hidden = value.can_switch_cm !== true;
        if (activeContext.cm_id !== selected.cm_id) activeContext = {audience: "all", creator_ids: null};
        activeContext.cm_id = selected.cm_id; activeContext.cm_name = selected.cm_name;
        activeContext.audience ||= "all";
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
    activeContext = {}; selectContext({...person, audience: "all", creator_ids: null}); currentScopeTurn = null;
    $("oa-cm").value = person.cm_id;
    closeSources(false); $("oa-new-result").hidden = true;
    history.replaceChildren(node("p", "oa-answer", "开始一个新问题。"));
    scope({}); status(""); if (!keepInput) input.value = ""; controls();
  }
  function switchCM(cmId) {
    if (denied || !scopesReady || active || submitting || restoring || saved.client_request_id) return false;
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
    if (["cm_id", "creator_ids", "chat_ids", "event_ref"].some(key => Object.hasOwn(value, key))) {
      const cmId = value.cm_id || activeContext.cm_id;
      activeContext = {cm_id: cmId, cm_name: cmId === activeContext.cm_id ? activeContext.cm_name : undefined,
        audience: value.audience || (value.creator_ids != null || value.event_ref ? "creators" : "all")};
    }
    for (const key of ["cm_id", "cm_name", "audience", "creator_ids", "creator_name", "creator_names", "chat_ids", "chat_names", "event_ref", "event_title"]) {
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
    const matching = (!value.cm_id || !activeContext.cm_id || value.cm_id === activeContext.cm_id) &&
      ["creator_ids", "chat_ids"].every(key => !Object.hasOwn(value, key) ||
        JSON.stringify([...(value[key] || [])].sort()) === JSON.stringify([...(activeContext[key] || [])].sort())) &&
      ((value.event_ref || null) === (activeContext.event_ref || null) ||
        (activeContext.event_ref && value.event_ref?.startsWith(`${activeContext.event_ref}:`)));
    if (matching) {
      if (!activeContext.cm_id && value.cm_id) activeContext.cm_id = value.cm_id;
      if (activeContext.event_ref && value.event_ref) activeContext.event_ref = value.event_ref;
      for (const key of ["cm_name", "creator_names", "chat_names"]) if (value[key]) activeContext[key] = value[key];
    }
    const current = [...new Set([activeContext.cm_name, activeContext.creator_name, ...(activeContext.creator_names || []),
      ...(activeContext.chat_names || []), activeContext.event_title || (activeContext.event_ref ? "当前事项" : null),
      activeContext.audience === "all" ? "全部授权消息" : activeContext.audience === "creators" ? "达人范围" : null,
      activeContext.page_label].filter(Boolean))];
    const values = matching && value.start && value.end ? [`${date(value.start)} – ${date(value.end)} · 英国时间`] : [];
    if (matching && !values.length && value.label) values.push(value.label);
    $("oa-scope").replaceChildren(...current.map(text => node("span", "oa-chip oa-chip--page", text)),
      ...[...new Set(values)].filter(text => !current.includes(text)).map(text => node("span", "oa-chip", text)));
    $("oa-scope").hidden = !current.length && !values.length;
  }
  function atBottom() { return panel.hidden ? followBottom : history.scrollHeight - history.scrollTop - history.clientHeight < 64; }
  function scrollToBottom() { followBottom = true; history.scrollTop = history.scrollHeight; $("oa-new-result").hidden = true; }
  function updatedScroll(follow) { followBottom = follow; if (follow) scrollToBottom(); else $("oa-new-result").hidden = false; }
  function userMessage(requestId, text) {
    $("oa-welcome")?.remove();
    for (const placeholder of history.querySelectorAll(":scope > p.oa-answer")) placeholder.remove();
    let message = [...history.children].find(child => child.dataset.requestId === requestId);
    if (!message) {
      message = node("article", "oa-message oa-message--user"); message.dataset.requestId = requestId;
      message.append(node("p", "", text)); history.append(message);
    }
  }
  function gapList(values, summary, conversationCount) {
    let groups = summary;
    if (!Array.isArray(groups) || !groups.length) {
      const reasons = new Map(), conversations = new Set();
      for (const gap of Array.isArray(values) ? values : []) {
        const reason = typeof gap === "string" ? gap : gap.message || gap.reason || "资料暂不可读取";
        if (!reasons.has(reason)) reasons.set(reason, {reason, chats: new Set(), names: new Set(), items: 0});
        const group = reasons.get(reason);
        if (gap.chat_id) {
          group.chats.add(gap.chat_id); conversations.add(gap.chat_id);
          if (gap.name || gap.conversation_name) group.names.add(gap.name || gap.conversation_name);
        }
        else group.items += Number.isFinite(gap.count) && gap.count > 0 ? gap.count : 1;
      }
      groups = [...reasons.values()].map(group => ({reason: group.reason, conversations: group.chats.size,
        names: [...group.names].slice(0, 5), items: group.items}));
      conversationCount = conversations.size;
    }
    if (!groups.length) return null;
    const counts = (conversations, items) => [conversations > 0 ? `${conversations} 个会话` : "",
      items > 0 ? `${items} 项记录` : ""].filter(Boolean).join(" · ");
    const details = node("details", "oa-gaps"), items = groups.reduce((total, group) => total + (group.items || 0), 0);
    const total = counts(conversationCount, items);
    details.append(node("summary", "", `部分资料尚不完整${total ? ` · ${total}` : ""}`));
    for (const group of groups) {
      const row = node("div", "", [group.reason || "资料暂不可读取", counts(group.conversations, group.items)].filter(Boolean).join(" · "));
      const names = (group.names || []).slice(0, 5), remaining = (group.conversations || 0) - names.length;
      if (names.length) row.append(node("p", "oa-gap-names", `会话：${names.join("、")}${remaining > 0 ? `；另有 ${remaining} 个会话` : ""}`));
      details.append(row);
    }
    return details;
  }
  function sourceLayout() {
    const overlay = !$("oa-sources").hidden && !(panel.classList.contains("is-fullscreen") && matchMedia("(min-width: 861px)").matches);
    $("oa-chat").inert = overlay;
    if (overlay) $("oa-chat").setAttribute("aria-hidden", "true");
    else $("oa-chat").removeAttribute("aria-hidden");
  }
  function closeSources(focus = true) {
    sourceVersion++; $("oa-sources").hidden = true; panel.classList.remove("has-sources");
    $("oa-sources-body").replaceChildren(); sourceLayout();
    if (focus && sourceFocus?.isConnected) sourceFocus.focus();
    sourceFocus = null;
  }
  function evidenceCard(reference, title = "消息原文", count = null) {
    const button = node("button", "oa-evidence", Number.isFinite(count) ? `查看原文 · ${count} 条` : "查看原文");
    button.type = "button"; button.setAttribute("aria-controls", "oa-sources");
    button.addEventListener("click", async () => {
      if (denied) return;
      const version = ++sourceVersion, content = $("oa-sources-body");
      sourceFocus = button; $("oa-sources").hidden = false; panel.classList.add("has-sources");
      $("oa-source-title").textContent = title; $("oa-sources-close").focus(); sourceLayout();
      content.replaceChildren(node("p", "oa-coverage", "正在读取原文…"));
      try {
        const value = await request(`/evidence/${encodeURIComponent(reference)}`);
        if (denied || version !== sourceVersion) return;
        content.replaceChildren();
        for (const message of value.messages || []) {
          const item = node("figure");
          item.append(node("figcaption", "", [message.conversation_name || message.chat_name, message.sender_name,
            date(message.time || message.sent_at || message.created_at),
            {matched: "引用消息", parent: "回复对象", root: "话题起点", nearby_context: "相邻消息"}[message.relation]].filter(Boolean).join(" · ")),
            node("blockquote", "", message.text || "这条消息没有可显示的文字。"));
          content.append(item);
        }
        if (!value.messages?.length) content.append(node("p", "oa-coverage", "当前没有可读取的原文。"));
        const gaps = gapList(value.gaps, value.gap_summary, value.gap_conversation_count); if (gaps) content.append(gaps);
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
    const copy = node("button", "", "复制回复"), feedback = node("span", "oa-copy-feedback");
    copy.type = "button"; feedback.setAttribute("role", "status");
    card.append(label, editor);
    if (draft.chinese_translation && draft.language !== "zh") card.append(node("p", "oa-translation", draft.chinese_translation));
    editor.addEventListener("input", () => {
      card.dataset.edited = String(editor.value !== draft.draft);
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
  function dataCard(data) {
    const titles = {creators: "达人资料", groups: "群聊目录", sync: "同步状态", activity: "活动统计", messages: "消息命中",
      window_messages: "总结来源", message_context: "原文与问答"};
    if (!data || !titles[data.kind]) return null;
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
          askButton("查看情况", `查看 ${person.name || "这位达人"} 的最新情况`, {creator_ids: [person.id], creator_names: [person.name || "未命名达人"]})];
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
        backfill_complete_count: "历史回填完成", syncing_conversation_count: "正在同步", not_readable_conversation_count: "无法读取"})) {
        const count = (data.conversations?.counts || data.conversations)?.[key]; if (Number.isFinite(count)) rows.push([label, count]);
      }
      if (rows.length) card.append(table(["范围", "会话数"], rows));
      if (data.watermarks?.latest) card.append(node("p", "oa-coverage", `最新同步水位 ${date(data.watermarks.latest)}`));
      if (data.watermarks?.oldest) card.append(node("p", "oa-coverage", `最早同步水位 ${date(data.watermarks.oldest)}`));
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
      card.append(node("p", "oa-coverage", "包含引用消息与有限相邻问答，不是完整会话。"));
      const ref = data.message_ref || data.evidence_ref || message.message_ref;
      if (ref) card.append(evidenceCard(ref, message.conversation_name || message.chat_name || "原文与问答"));
    } else if (["messages", "window_messages"].includes(data.kind)) {
      const window = data.kind === "window_messages";
      const messageList = window ? node("details", "oa-coverage") : card;
      if (window) {
        messageList.append(node("summary", "", `查看本页原消息（${data.messages?.length || 0}）`));
        card.append(messageList);
      }
      for (const message of (data.messages || []).slice(0, window ? 100 : 25)) {
        const hit = node("div", "oa-hit");
        hit.append(node("div", "oa-card-meta", [message.conversation_name || message.chat_name, message.sender_name,
          date(message.time || message.sent_at)].filter(Boolean).join(" · ")));
        const text = message.text || (message.unreadable ? "正文暂不可读取" : "这条消息没有可显示的文字。");
        hit.append(node("p", "", text.length > 420 ? `${text.slice(0, 420)}…` : text));
        if (message.unavailable_or_truncated) hit.append(node("p", "oa-coverage", "正文不完整，请查看来源范围。"));
        const ref = message.evidence_ref || message.message_ref;
        if (ref) hit.append(evidenceCard(ref, message.conversation_name || message.chat_name || "消息原文"));
        messageList.append(hit);
      }
      if (!data.messages?.length) card.append(node("p", "oa-coverage", window ? "该时段尚无已同步消息。" : "这次扫描未找到可读的匹配消息。"));
      const coverage = data.coverage || {}, counts = [];
      for (const [key, label] of [["scanned", "扫描"], ["readable", "可读"], ["unreadable", "不可读"], ["matched", "命中"]]) {
        if (Number.isFinite(coverage[key])) counts.push(`${label} ${coverage[key]}`);
      }
      if (counts.length && !window) card.append(node("p", "oa-coverage", `本页：${counts.join(" · ")}，不是全库总量。`));
      if (window) {
        const total = coverage.total_messages ?? data.total_messages;
        card.append(node("p", "oa-coverage", `${data.scope?.audience === "creators" ? "达人消息" : "全部授权消息"} · 本页 ${data.messages?.length || 0} 条${Number.isFinite(total) ? ` / 时段 ${total} 条` : ""}`));
      }
      if (coverage.scan_complete === false) card.append(node("p", "oa-coverage", "扫描尚未结束，可继续下一页。"));
      if (coverage.source_complete === false) card.append(node("p", "oa-coverage", "来源覆盖不完整，未命中不代表没有发生。"));
    }
    const gaps = gapList(data.gaps, data.gap_summary, data.gap_conversation_count); if (gaps) card.append(gaps);
    if (data.checked_at) card.append(node("p", "oa-coverage", `查询于 ${date(data.checked_at)}`));
    if (data.next_cursor && data.query_ref) {
      const actions = node("div", "oa-card-actions");
      actions.append(askButton("继续下一页", "继续查看这次查询的下一页", {...data.scope, query_ref: data.query_ref, cursor: data.next_cursor})); card.append(actions);
    }
    return card;
  }
  function renderResult(container, result) {
    const previousDraft = container.querySelector(".oa-draft");
    const keepDraft = typeof result.draft?.draft === "string" && result.draft.draft.trim() && previousDraft &&
      (previousDraft.dataset.edited === "true" || previousDraft.contains(document.activeElement));
    const focusedEditor = keepDraft && previousDraft.querySelector("textarea") === document.activeElement ? document.activeElement : null;
    const selection = focusedEditor ? [focusedEditor.selectionStart, focusedEditor.selectionEnd] : null;
    container.replaceChildren(answer(result.answer));
    scope({...result.scope, ...result.window});
    (result.events || []).forEach((event, index) => {
      const card = node("section", "oa-event"), meta = node("div", "oa-event-meta");
      for (const text of [event.creator_handle || event.lark_user_name || event.creator_name, event.priority,
        {unresolved: "待跟进", resolved: "已解决", uncertain: "待核对"}[event.handling_status]]) {
        if (text) meta.append(node("span", "", text));
      }
      card.append(meta, node("h4", "", `${index + 1}. ${event.title || "聊天事项"}`));
      if (event.summary) card.append(node("p", "", event.summary));
      if (event.latest_progress) card.append(node("p", "", event.latest_progress));
      const actions = node("div", "oa-card-actions");
      if (event.evidence_ref || event.event_ref) actions.append(evidenceCard(event.evidence_ref || event.event_ref, event.title || "聊天事项原文", event.evidence_count));
      if (event.event_ref) actions.append(askButton("最新进展", "查看这个事项的最新进展", {event_ref: event.event_ref, event_title: event.title || "聊天事项"}),
        askButton("起草回复", "针对这个事项起草一条回复", {event_ref: event.event_ref, event_title: event.title || "聊天事项"}));
      if (actions.children.length) card.append(actions);
      container.append(card);
    });
    for (const data of result.data || []) { const card = dataCard(data); if (card) container.append(card); }
    const draft = keepDraft ? previousDraft : draftCard(result.draft, container.dataset.turnId);
    if (draft) {
      container.append(draft);
      if (focusedEditor) { focusedEditor.focus({preventScroll: true}); focusedEditor.setSelectionRange(...selection); }
      if (keepDraft && result.draft?.draft && previousDraft.dataset.original !== result.draft.draft && !draft.querySelector(".oa-draft-update")) {
        const replace = node("button", "oa-draft-update", "使用新生成的草稿"); replace.type = "button";
        replace.addEventListener("click", () => { const value = draftCard(result.draft, container.dataset.turnId); if (value) draft.replaceWith(value); });
        draft.append(replace);
      }
    }
    const gaps = gapList(result.gaps, result.gap_summary, result.gap_conversation_count); if (gaps) container.append(gaps);
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
        followup.append(askButton(label, prompt));
      }
      container.append(followup);
    }
  }
  function turn(view) {
    if (denied) return;
    if (view.error_code === "permission") { clearProtected(); return; }
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
      status(progress.message || labels[progress.status] || "正在整理");
    });
    source.addEventListener("result", event => {
      if (active !== watching || denied) return;
      const follow = atBottom();
      const completed = {...view, status: "completed", result: JSON.parse(event.data)};
      adoptTurnScope(completed); turn(completed); controls(); updatedScroll(follow);
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
    const readScopeVersion = scopeVersion;
    const follow = !history.querySelector(".oa-message") || atBottom();
    try {
      const value = await request(`/sessions/${encodeURIComponent(saved.session_id)}`);
      if (denied || version !== revision) return;
      const turns = value.turns || [];
      const latest = turns.at(-1);
      const latestScope = latest?.result?.scope || latest?.scope;
      const sessionCM = latestScope?.cm_id || saved.cm_id;
      const running = turns.find(view => !terminal.has(view.status));
      const unconfirmed = saved.client_request_id && !turns.some(view => view.client_request_id === saved.client_request_id);
      if (turns.length && sessionCM && sessionCM !== activeContext.cm_id) {
        if (running || unconfirmed) {
          const person = cmScopes.find(person => person.cm_id === sessionCM);
          if (!person) { clearProtected(); return; }
          selectContext({...person, audience: "all", creator_ids: null}); $("oa-cm").value = person.cm_id;
        } else { resetSession(cmScopes.find(person => person.cm_id === activeContext.cm_id), true); return; }
      }
      if (!turns.length) {
        closeSources(false); currentScopeTurn = null;
        history.replaceChildren(node("p", "oa-answer", "开始一个新问题。"));
        selectContext({cm_id: activeContext.cm_id, cm_name: activeContext.cm_name, audience: "all", creator_ids: null}); scope({});
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
      if (turns.some(view => view.client_request_id === saved.client_request_id)) {
        delete saved.client_request_id; pending = null; persist();
      }
      if (running) watch(running, restoreScope ? scopeVersion : null);
      else { source?.close(); source = null; active = null; controls(); }
      if (saved.client_request_id) status("上次提交尚未确认，重新输入后重试会复用原请求编号。", true);
      else if (turns.length && !running) status("");
      updatedScroll(follow);
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
      selectContext(selectedContext); scope({});
      pendingScopeVersion = scopeVersion;
      pending = {session_id: saved.session_id, client_request_id: saved.client_request_id, message: text,
        context: {...activeContext, ...(selectedContext.query_ref ? {query_ref: selectedContext.query_ref, cursor: selectedContext.cursor} : {})}};
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
      adoptTurnScope(value); turn(value); watch(value);
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
    if ($("oa-sources").hidden) input.focus(); else $("oa-sources-close").focus();
    void loadScopes();
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
    sourceLayout();
  }
  $("oa-launcher").addEventListener("click", open);
  $("oa-collapse").addEventListener("click", collapse);
  $("oa-fullscreen").addEventListener("click", () => fullscreen(!panel.classList.contains("is-fullscreen")));
  $("oa-cm").addEventListener("change", () => {
    if (!switchCM($("oa-cm").value)) $("oa-cm").value = activeContext.cm_id || "";
    else input.focus();
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
  for (const button of app.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    input.value = button.dataset.prompt; controls(); void ask(input.value);
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
      if (!$("oa-sources").hidden) closeSources();
      else if (panel.classList.contains("is-fullscreen") && !matchMedia("(max-width: 600px)").matches) fullscreen(false);
      else collapse();
    }
    if (event.key !== "Tab" || !panel.classList.contains("is-fullscreen")) return;
    const elements = [...panel.querySelectorAll("button:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex='0']")]
      .filter(element => element.getClientRects().length && !element.closest("[inert]"));
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  $("oa-sources-close").addEventListener("click", () => closeSources());
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
  window.addEventListener("pagehide", () => { source?.close(); clearTimeout(restoreTimer); });
  if (app.dataset.configured !== "true") status("助手尚未配置，请联系管理员。", true);
  scope({}); controls();
})();
