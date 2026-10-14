/* Human-reviewed drafts; no message sending. Private messages stay in memory and are rendered as text. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const priorities = ["P0", "P1", "P2", "P3"];
  const labels = {P0: "重大风险", P1: "优先处理", P2: "正常跟进", P3: "日常沟通"};
  let data = JSON.parse($("offhours-summary-data").textContent);
  $("offhours-summary-data").textContent = "";
  let selected = "all", query = "", timer = null, loading = false, denied = false, navigating = false;
  let revision = "";

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text || "—";
    return element;
  }

  function date(value) {
    if (!value || Number.isNaN(new Date(value).getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: data.window?.timezone || "Europe/London", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(new Date(value));
  }

  function priority(event) { return priorities.includes(event.priority) ? event.priority : null; }
  function needsReview(event) { return event.handling_status ? event.handling_status === "uncertain" : event.needs_review || !priority(event); }
  function events() { return Array.isArray(data.events) ? data.events : []; }

  const replyStates = new Map();
  function clearReplies() {
    for (const state of replyStates.values()) {
      state.version++; clearTimeout(state.timer);
      state.text = ""; state.panel.replaceChildren();
    }
    replyStates.clear();
  }
  async function replyRequest(path, body) {
    const response = await fetch(`/dashboard/offhours-summary/replies${path}`, {
      method: body ? "POST" : "GET", cache: "no-store", credentials: "same-origin", redirect: "error",
      headers: {Accept: "application/json", ...(body ? {"Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="summary-csrf-token"]').content} : {})},
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    if ([401, 403].includes(response.status)) {
      denied = true; clearReplies(); data = {events: [], enabled: false, run: null}; render();
      throw new Error("登录或查看权限已失效，请重新登录。");
    }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "生成暂未完成，请重试。");
    return value;
  }
  function replyPanel(event) {
    if (replyStates.has(event.id)) return replyStates.get(event.id).panel;
    const panel = node("section", "oh-reply"), feedback = node("p", "oh-small");
    feedback.setAttribute("aria-live", "polite");
    const generate = node("button", "btn btn-outline-primary btn-sm", "生成回复建议"); generate.type = "button";
    const content = node("div", "oh-reply-content"); content.hidden = true;
    const label = node("label", "", "建议回复（可编辑）");
    const editor = node("textarea", "form-control oh-reply-editor");
    editor.id = `reply-${event.id}`; editor.rows = 7; label.htmlFor = editor.id;
    const translationLabel = node("div", "oh-small", "中文翻译");
    const translation = node("p", "oh-reply-translation");
    const checks = node("p", "oh-small oh-reply-checks");
    const copy = node("button", "btn btn-primary btn-sm", "复制回复"); copy.type = "button";
    const translate = node("button", "btn btn-outline-secondary btn-sm", "更新中文翻译"); translate.type = "button";
    const actions = node("div", "oh-actions"); actions.append(copy, translate);
    content.append(label, editor, translationLabel, translation, checks, actions);
    panel.append(generate, feedback, content);
    const state = {panel, version: 0, text: "", jobId: null, copyId: null, timer: null};
    replyStates.set(event.id, state);
    function stale(message) {
      copy.disabled = true; translate.disabled = true; generate.disabled = false;
      for (const button of [copy, translate, generate]) button.removeAttribute("aria-busy");
      feedback.textContent = message;
    }
    function translationFailed(message) {
      copy.disabled = !state.text.trim(); translate.disabled = false; translate.removeAttribute("aria-busy");
      translation.textContent = "翻译未完成，可重试。"; feedback.textContent = message;
    }
    async function poll(jobId, version, isTranslation = false) {
      if (denied || version !== state.version) return;
      try {
        const value = await replyRequest(`/${encodeURIComponent(jobId)}?fresh=true`);
        if (denied || version !== state.version) return;
        if (["queued", "running"].includes(value.status)) {
          state.timer = setTimeout(() => poll(jobId, version, isTranslation), 2500); return;
        }
        generate.disabled = false;
        (isTranslation ? translate : generate).removeAttribute("aria-busy");
        if (value.status !== "succeeded" && value.reason_code !== "new_messages") {
          if (isTranslation && value.status === "failed") {
            translationFailed(value.error || "翻译未完成，可重试。");
          } else stale(value.error || "资料已变化，请重新生成。");
          return;
        }
        const result = value.result;
        if (isTranslation) {
          if (result.draft !== state.text) return;
          translation.textContent = result.chinese_translation;
          state.copyId = jobId;
          copy.disabled = false; translate.disabled = false;
          feedback.textContent = "翻译已更新。";
        } else {
          feedback.textContent = result.explanation;
          checks.textContent = result.checks?.length ? `发送前需确认：${result.checks.join("；")}` : "";
          content.hidden = result.outcome !== "ready";
          if (result.outcome !== "ready") {
            if (result.checks?.length) feedback.textContent += ` ${result.checks.join("；")}`;
            generate.textContent = "重新核对"; return;
          }
          state.jobId = jobId; state.copyId = jobId;
          editor.value = state.text = result.draft;
          translation.textContent = result.chinese_translation;
          translationLabel.hidden = translation.hidden = result.language === "zh";
          copy.disabled = false; translate.disabled = false; generate.textContent = "重新生成";
        }
        if (value.reason_code === "new_messages") feedback.textContent = "聊天有新消息，请核对后发送。";
      } catch (error) {
        if (!denied && version === state.version) {
          if (isTranslation) translationFailed(error.message);
          else stale(error.message);
        }
      }
    }
    generate.addEventListener("click", async () => {
      const version = ++state.version; clearTimeout(state.timer);
      generate.disabled = true; copy.disabled = true; content.hidden = true;
      for (const button of [copy, translate]) button.removeAttribute("aria-busy");
      generate.setAttribute("aria-busy", "true");
      feedback.textContent = "正在生成回复建议…";
      try {
        const value = await replyRequest("", {event_id: event.id});
        if (!denied && version === state.version) poll(value.id, version);
      } catch (error) { if (!denied && version === state.version) stale(error.message); }
    });
    async function updateTranslation() {
      clearTimeout(state.timer);
      if (!state.text.trim() || !state.jobId || denied) return;
      const version = ++state.version;
      translate.disabled = true;
      translate.setAttribute("aria-busy", "true");
      translationLabel.hidden = translation.hidden = false;
      translation.textContent = "正在更新中文翻译…";
      try {
        const value = await replyRequest("", {event_id: event.id, parent_id: state.jobId, text: state.text});
        if (!denied && version === state.version) poll(value.id, version, true);
      } catch (error) {
        if (!denied && version === state.version) {
          translationFailed(error.message);
        }
      }
    }
    editor.addEventListener("input", () => {
      state.text = editor.value; state.version++;
      clearTimeout(state.timer);
      for (const button of [copy, translate]) button.removeAttribute("aria-busy");
      copy.disabled = !state.text.trim(); translate.disabled = !state.text.trim();
      translation.textContent = "正文已修改，中文翻译待更新。";
      translationLabel.hidden = translation.hidden = false;
    });
    translate.addEventListener("click", updateTranslation);
    copy.addEventListener("click", async () => {
      const version = state.version, text = state.text;
      let fresh = false;
      copy.disabled = true;
      copy.setAttribute("aria-busy", "true");
      try {
        const value = await replyRequest(`/${encodeURIComponent(state.copyId)}?fresh=true`);
        if (denied || version !== state.version || text !== state.text) return;
        if (value.status !== "succeeded" && value.reason_code !== "new_messages") {
          stale(value.error || "资料已变化，请重新生成。"); return;
        }
        fresh = true;
        await navigator.clipboard.writeText(text);
        if (denied || version !== state.version || text !== state.text) return;
        feedback.textContent = value.reason_code === "new_messages" ? "已复制。聊天有新消息，请核对后发送。" : "已复制，未发送。";
        copy.disabled = false;
      } catch (error) {
        if (!denied && version === state.version) {
          feedback.textContent = error.name === "NotAllowedError" ? "浏览器未允许复制，可重试或手动复制。" : error.message;
          if (fresh && error.name === "NotAllowedError") copy.disabled = false;
          // A failed freshness check must not make an old draft copyable.
        }
      } finally { if (version === state.version) copy.removeAttribute("aria-busy"); }
    });
    return panel;
  }

  function eventCard(event) {
    const level = priority(event), card = node("article", "oh-event");
    const heading = node("div", "oh-event-heading");
    heading.append(node("span", `oh-priority ${level ? `oh-${level.toLowerCase()}` : "oh-warning"}`, level || "未分级"),
      node("h3", "oh-event-title", event.title));
    const statuses = node("div", "oh-event-status");
    const names = [...new Set((Array.isArray(event.replied_by) ? event.replied_by : [])
      .filter(name => typeof name === "string" && name.trim()))];
    if (event.handling_status !== "not_required" && Array.isArray(event.replied_by)) {
      const replied = names.length > 0;
      const label = names.length ? `${names.slice(0, 2).join("、")}${names.length > 2 ? ` 等${names.length}人` : ""} 已回复`
        : "未见回复";
      const reply = node("span", `oh-replied${replied ? "" : " oh-replied-empty"}`, label);
      if (names.length > 2) reply.title = `${names.join("、")} 已回复`;
      statuses.append(reply);
    }
    const handlingLabels = {resolved: "已解决", unresolved: "未解决", uncertain: "待判断", not_required: "无需处理"};
    const handling = node("span", `oh-handling oh-handling-${event.handling_status || "pending"}`,
      handlingLabels[event.handling_status] || "待判断");
    statuses.append(handling); heading.append(statuses);
    const identity = node("div", "oh-identity");
    identity.append(node("span", "", `达人 handle：${event.creator_handle || event.creator_handles?.join("、") || "—"}`),
      node("span", "", `Lark 用户：${event.lark_user_name || "—"}`));
    if (event.conversation_type) {
      const conversations = node("span", "oh-conversations",
        `${event.conversation_type === "private" ? "私聊" : event.conversation_type === "combined" ? "相关会话" : "群聊"}：`);
      const chats = new Map((event.evidence || []).map(message => [message.chat_id, message.chat_name]));
      if (!chats.size) conversations.append(document.createTextNode(event.conversation_name || "—"));
      let index = 0;
      for (const [chatId, name] of chats) {
        if (index++) conversations.append(document.createTextNode("、"));
        const label = name || event.conversation_name || "未命名会话";
        if (typeof chatId === "string" && /^oc_[A-Za-z0-9_-]{1,128}$/.test(chatId)) {
          const link = node("a", "", label);
          link.href = `https://applink.larksuite.com/client/chat/open?openChatId=${encodeURIComponent(chatId)}`;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.title = "在 Lark 中打开此会话（需有会话访问权限）";
          conversations.append(link);
        } else conversations.append(document.createTextNode(label));
      }
      identity.append(conversations);
    }
    card.append(heading, identity);
    card.append(node("p", "oh-summary", event.summary));
    const meta = node("div", "oh-event-meta");
    const progress = node("dl");
    progress.append(node("dt", "", "时段内最新进展"), node("dd", "", event.latest_progress));
    meta.append(progress);
    card.append(meta);
    const evidence = Array.isArray(event.evidence) ? event.evidence : [];
    if (evidence.length) {
      const details = node("details", "oh-evidence"), messages = node("div", "oh-evidence-list");
      details.dataset.detailKey = `event:${event.id}`;
      details.append(node("summary", "", `查看相关原文（${evidence.length}）`));
      if (event.handling_reason) details.append(node("p", "oh-small", event.handling_reason));
      for (const message of evidence) {
        const item = node("figure", "oh-message");
        const supportsStatus = (event.handling_evidence || []).some(e => e.chat_id === message.chat_id
          && e.message_id === message.message_id && e.text === message.text);
        if (supportsStatus) item.append(node("span", "oh-small", "处理状态依据"));
        item.append(node("figcaption", "", `${message.chat_name ? message.chat_name + " · " : ""}${message.sender_name || "—"} · ${date(message.created_at)}（英国时间）`),
          node("blockquote", "", message.text || "该消息没有可显示的文字。"));
        messages.append(item);
      }
      details.append(messages); card.append(details);
    } else card.append(node("p", "oh-small", "当前没有可展示的原文，请勿仅凭总结作出结论。"));
    if (event.handling_status === "unresolved") card.append(replyPanel(event));
    return card;
  }

  function renderEvents() {
    if (!denied && document.activeElement?.closest(".oh-reply")) return;
    const openDetails = new Set([...$("oh-events").querySelectorAll("details[open][data-detail-key]")]
      .map(detail => detail.dataset.detailKey));
    const all = events();
    const filtered = all.filter(event => (selected === "all" || selected === "review" && needsReview(event)
      || selected === "unresolved" && event.handling_status === "unresolved" || priority(event) === selected)
      && (!query || [event.creator_handle, ...(event.creator_handles || []), ...(Array.isArray(event.replied_by) ? event.replied_by : []), event.lark_user_name, event.conversation_name, event.title, event.summary, event.latest_progress]
        .some(value => String(value || "").toLocaleLowerCase().includes(query))));
    filtered.sort((a, b) => (priorities.indexOf(priority(a)) < 0 ? 4 : priorities.indexOf(priority(a)))
      - (priorities.indexOf(priority(b)) < 0 ? 4 : priorities.indexOf(priority(b))));
    const cards = [], routine = node("details", "oh-routine");
    routine.dataset.detailKey = "routine";
    const routineEvents = filtered.filter(event => priority(event) === "P3");
    routine.append(node("summary", "", `日常沟通（${routineEvents.length}）· 展开查看`));
    for (const event of filtered) {
      if (selected === "all" && priority(event) === "P3") {
        if (!cards.includes(routine)) cards.push(routine);
        routine.append(eventCard(event));
      } else cards.push(eventCard(event));
    }
    $("oh-events").replaceChildren(...cards);
    for (const detail of $("oh-events").querySelectorAll("details[data-detail-key]")) {
      detail.open = openDetails.has(detail.dataset.detailKey);
    }
    $("oh-empty").hidden = filtered.length > 0;
    $("oh-visible-count").textContent = `显示 ${filtered.length} / ${all.length} 条已归纳事件`;
    if (all.length && !filtered.length) {
      $("oh-empty-title").textContent = "没有符合筛选条件的事件";
      $("oh-empty-description").textContent = "可以切换等级，或换一个关键词。";
    } else if (!data.enabled && !data.run) {
      $("oh-empty-title").textContent = "尚未开启分析";
      $("oh-empty-description").textContent = data.can_manage
        ? "开启消息总结后，系统按上班和下班时段整理已入库达人的消息。"
        : "分析尚未开启。开启后，这里会展示已入库达人的真实消息总结。";
    } else if (data.run?.status === "completed" && !data.coverage?.messages) {
      $("oh-empty-title").textContent = "本时段暂无已同步的相关消息";
      $("oh-empty-description").textContent = "尚未同步的消息不包含在本轮范围内。";
    } else if (data.run?.status === "completed") {
      $("oh-empty-title").textContent = "分析完成，未识别出事项";
      $("oh-empty-description").textContent = "本轮已处理的消息未形成可展示事项，资料缺口另行标明。";
    } else if (data.run?.counts?.failed) {
      $("oh-empty-title").textContent = "部分分析未完成";
      $("oh-empty-description").textContent = "失败部分不能当作没有事项。";
    } else {
      $("oh-empty-title").textContent = data.run ? "暂时没有可展示的事件" : "等待分析任务";
      $("oh-empty-description").textContent = "请结合上方的分析进度与资料缺口查看。没有结果不代表没有风险。";
    }
    for (const button of document.querySelectorAll("[data-priority]")) {
      button.setAttribute("aria-pressed", String(button.dataset.priority === selected));
    }
  }

  function render() {
    const run = data.run, counts = run?.counts || {};
    const settingsLink = document.querySelector('.oh-actions a[href^="/admin/"]');
    if (settingsLink) settingsLink.hidden = !data.can_manage;
    const failed = Number(counts.failed || 0);
    const pending = Number(counts.queued || 0) + Number(counts.running || 0);
    let title = "尚未开启分析", description = "开启后将自动整理达人消息。";
    if (run) {
      const status = {
        queued: "正在整理", running: "正在整理", succeeded: "整理完成",
        completed: "整理完成", partial: "部分结果待更新", failed: "暂时无法更新",
        cancelled: "本轮分析已停止", disabled: "分析已关闭", paused: "分析已暂停",
      }[run.status] || "正在整理";
      title = !data.enabled ? "分析已关闭" : pending ? "正在整理" : data.window?.is_open ? "持续更新中"
        : data.window?.is_partial ? "部分结果待更新" : status;
      description = failed || data.window?.is_partial && !data.window?.is_open
        ? "已显示可用结果；部分消息暂未纳入。" : "";
    } else if (data.window?.is_future) {
      title = "时段尚未开始";
      description = "开始后自动整理消息。";
    } else if (data.enabled) {
      title = "正在整理";
      description = "有结果后将自动更新。";
    }
    $("oh-status-title").textContent = title;
    $("oh-analysis-spinner").hidden = denied || !data.enabled || !(pending || ["queued", "running"].includes(run?.status));
    $("oh-status-description").textContent = description;
    const gaps = [...new Set(run?.gaps || [])];
    $("oh-issues").hidden = !gaps.length;
    $("oh-issue-list").replaceChildren(...gaps.map(gap => node("li", "oh-small", gap)));
    $("oh-updated").textContent = run?.updated_at ? `结果更新 ${date(run.updated_at)}` : "";
    const all = events();
    for (const level of priorities) {
      const count = all.filter(event => priority(event) === level).length;
      $("oh-count-" + level).textContent = run ? String(count) : "—";
      document.querySelector(`.oh-stat[data-priority="${level}"]`).setAttribute("aria-label", `${level} ${labels[level]}，${run ? count : "尚未分析"}`);
    }
    $("oh-review-count").textContent = String(all.filter(needsReview).length);
    $("oh-unresolved-count").textContent = String(all.filter(event => event.handling_status === "unresolved").length);
    renderEvents();
  }

  function schedule() {
    clearTimeout(timer);
    const active = data.enabled && (!data.run || data.window?.is_open
      || ["queued", "running"].includes(data.run.status));
    if (!denied && !navigating && !document.hidden) timer = setTimeout(refresh, active ? 30000 : 120000);
  }

  async function refresh(interactive = false) {
    if (loading || denied || navigating) return;
    loading = true; clearTimeout(timer); $("oh-refresh").disabled = true;
    if (interactive) $("oh-refresh").setAttribute("aria-busy", "true");
    try {
      const params = new URLSearchParams({cm_id: data.responsible.id,
        date: data.selection.date, period: data.selection.period});
      const response = await fetch("/dashboard/offhours-summary/data?" + params, {
        cache: "no-store", credentials: "same-origin", redirect: "error",
        headers: {Accept: "application/json", ...(revision ? {"If-None-Match": revision} : {})},
      });
      if (navigating) return;
      if (response.status === 304) { $("oh-feedback").hidden = true; return; }
      if ([401, 403].includes(response.status)) {
        denied = true; clearReplies();
        data = {events: [], enabled: false, run: null}; render();
        $("oh-empty-title").textContent = "当前无法查看总结";
        $("oh-empty-description").textContent = "登录或查看权限已变化。请重新登录后再打开本页。";
        $("oh-status-title").textContent = "查看权限已失效";
        $("oh-status-description").textContent = "为保护消息内容，已清除本页显示的总结。";
        throw new Error("登录或查看权限已失效，请重新登录。");
      }
      if (!response.ok) throw new Error("暂时无法刷新。仍显示上次已读取的结果，请稍后重试。");
      const next = await response.json();
      if (navigating) return;
      if (!next.window || !Array.isArray(next.events)) throw new Error("返回的数据不完整。仍显示上次已读取的结果，请稍后重试。");
      revision = response.headers.get("ETag") || "";
      $("oh-feedback").hidden = true;
      if (JSON.stringify(data) !== JSON.stringify(next)) { data = next; render(); }
    } catch (error) {
      if (navigating) return;
      $("oh-feedback").textContent = error instanceof TypeError || error instanceof SyntaxError
        ? "连接暂时中断，结果可能已过时。请稍后刷新；这不代表没有新事件。" : error.message;
      $("oh-feedback").hidden = false;
    } finally {
      loading = false; $("oh-refresh").removeAttribute("aria-busy"); $("oh-refresh").disabled = denied || navigating; schedule();
    }
  }

  document.querySelectorAll("[data-priority]").forEach(button => button.addEventListener("click", () => {
    selected = button.dataset.priority; renderEvents();
  }));
  $("oh-selection").addEventListener("change", () => {
    if (navigating) return;
    navigating = true; clearTimeout(timer); clearReplies();
    $("oh-selection").inert = true;
    $("oh-events").inert = true; $("oh-events").setAttribute("aria-busy", "true");
    $("oh-refresh").disabled = true;
    $("oh-status-title").textContent = "正在加载总结…";
    $("oh-status-description").textContent = ""; $("oh-analysis-spinner").hidden = false;
    $("oh-selection").requestSubmit();
  });
  $("oh-search").addEventListener("input", event => { query = event.target.value.trim().toLocaleLowerCase(); renderEvents(); });
  $("oh-refresh").addEventListener("click", () => refresh(true));
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  window.addEventListener("pageshow", event => {
    if (!event.persisted || !navigating) return;
    navigating = false; $("oh-selection").inert = false; $("oh-events").inert = false;
    $("oh-events").removeAttribute("aria-busy"); $("oh-refresh").disabled = denied;
    for (const select of $("oh-selection").querySelectorAll("select")) {
      select.value = select.name === "cm_id" ? data.responsible?.id : data.selection?.[select.name];
    }
    render(); refresh();
  });
  render(); schedule();
})();
