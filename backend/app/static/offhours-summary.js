/* Read-only report. Private messages stay in memory and are rendered as text. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const priorities = ["P0", "P1", "P2", "P3"];
  const labels = {P0: "重大风险", P1: "优先处理", P2: "正常跟进", P3: "日常沟通"};
  let data = JSON.parse($("offhours-summary-data").textContent);
  $("offhours-summary-data").textContent = "";
  let selected = "all", query = "", timer = null, loading = false, denied = false;

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
  function needsReview(event) { return event.needs_review || !priority(event); }
  function events() { return Array.isArray(data.events) ? data.events : []; }

  function eventCard(event) {
    const level = priority(event), card = node("article", "oh-event");
    const heading = node("div", "oh-event-heading");
    heading.append(node("span", `oh-priority ${level ? `oh-${level.toLowerCase()}` : "oh-warning"}`, level || "待核实"),
      node("h3", "oh-event-title", event.title));
    const identity = node("div", "oh-identity");
    identity.append(node("span", "", `达人 handle：${event.creator_handle || event.creator_handles?.join("、") || "—"}`),
      node("span", "", `Lark 用户：${event.lark_user_name || "—"}`));
    if (event.conversation_type) identity.append(node("span", "",
      `${event.conversation_type === "private" ? "私聊" : "群聊"}：${event.conversation_name || "—"}`));
    card.append(heading, identity);
    if (needsReview(event)) card.append(node("span", "oh-review-tag", "信息不足，需结合原文核实"));
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
      for (const message of evidence) {
        const item = node("figure", "oh-message");
        item.append(node("figcaption", "", `${message.sender_name || "—"} · ${date(message.created_at)}（英国时间）`),
          node("blockquote", "", message.text || "该消息没有可显示的文字。"));
        messages.append(item);
      }
      details.append(messages); card.append(details);
    } else card.append(node("p", "oh-small", "当前没有可展示的原文，请勿仅凭总结作出结论。"));
    return card;
  }

  function renderEvents() {
    const openDetails = new Set([...$("oh-events").querySelectorAll("details[open][data-detail-key]")]
      .map(detail => detail.dataset.detailKey));
    const all = events();
    const filtered = all.filter(event => (selected === "all" || selected === "review" && needsReview(event) || priority(event) === selected)
      && (!query || [event.creator_handle, ...(event.creator_handles || []), event.lark_user_name, event.conversation_name, event.title, event.summary, event.latest_progress]
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
    const complete = Number(counts.completed || 0), failed = Number(counts.failed || 0);
    const pending = Number(counts.queued || 0) + Number(counts.running || 0);
    const total = complete + failed + pending;
    let title = "尚未开启分析", description = "保存并开启后，系统会自动开始归纳这段时间的真实消息。";
    if (run) {
      const status = {
        queued: "已排队，准备分析", running: "正在并行分析", succeeded: "本轮分析已结束",
        completed: "本轮分析已结束", partial: "已有部分分析结果", failed: "本轮分析遇到问题",
        cancelled: "本轮分析已停止", disabled: "分析已关闭", paused: "分析已暂停",
      }[run.status] || "正在整理分析结果";
      title = !data.enabled ? "分析已关闭" : data.window?.is_open ? "时段未结束 · 当前为部分结果"
        : data.window?.is_partial ? "当前为部分结果 · 尚有资料缺口" : status;
      description = `已完成 ${complete} / ${total} 个分析任务`;
      if (pending) description += `，${pending} 个等待或处理中`;
      if (failed) description += `，${failed} 个未能完成`;
      description += "。";
      if (data.coverage) description += ` 已检查 ${data.coverage.related_groups || 0} 个相关群聊、${data.coverage.related_private_chats || 0} 个私聊；本时段纳入 ${data.coverage.group_conversations || 0} 个群聊、${data.coverage.private_conversations || 0} 个私聊，共 ${data.coverage.messages || 0} 条消息。`;
      if (run.cutoff) description += ` 本轮读取到 ${date(run.cutoff)}（英国时间）。`;
      if (data.window?.is_open) description += " 尚未到时段结束时间，请稍后查看更新。";
    } else if (data.window?.is_future) {
      title = "时段尚未开始";
      description = "到达开始时间后，系统会自动收集并总结该时段的消息。";
    } else if (data.enabled) {
      title = "已开启 · 等待分析任务";
      description = "后台正在准备当前时段的数据。尚未有结果不代表没有消息或没有风险。";
    }
    $("oh-status-title").textContent = title;
    $("oh-status-description").textContent = description;
    $("oh-updated").textContent = run?.updated_at ? `结果更新 ${date(run.updated_at)}` : "";
    const all = events();
    for (const level of priorities) {
      const count = all.filter(event => priority(event) === level).length;
      $("oh-count-" + level).textContent = run ? String(count) : "—";
      document.querySelector(`.oh-stat[data-priority="${level}"]`).setAttribute("aria-label", `${level} ${labels[level]}，${run ? count : "尚未分析"}`);
    }
    $("oh-review-count").textContent = String(all.filter(needsReview).length);
    renderEvents();
  }

  function schedule() {
    clearTimeout(timer);
    const active = data.enabled && (!data.run || data.window?.is_open
      || ["queued", "running"].includes(data.run.status));
    if (!denied && !document.hidden) timer = setTimeout(refresh, active ? 10000 : 30000);
  }

  async function refresh() {
    if (loading || denied) return;
    loading = true; clearTimeout(timer); $("oh-refresh").disabled = true;
    try {
      const params = new URLSearchParams({cm_id: data.responsible.id,
        date: data.selection.date, period: data.selection.period});
      const response = await fetch("/dashboard/offhours-summary/data?" + params, {
        cache: "no-store", credentials: "same-origin", redirect: "error", headers: {Accept: "application/json"},
      });
      if ([401, 403].includes(response.status)) {
        denied = true;
        data = {events: [], enabled: false, run: null}; render();
        $("oh-empty-title").textContent = "当前无法查看总结";
        $("oh-empty-description").textContent = "登录或查看权限已变化。请重新登录后再打开本页。";
        $("oh-status-title").textContent = "查看权限已失效";
        $("oh-status-description").textContent = "为保护消息内容，已清除本页显示的总结。";
        throw new Error("登录或查看权限已失效，请重新登录。");
      }
      if (!response.ok) throw new Error("暂时无法刷新。仍显示上次已读取的结果，请稍后重试。");
      const next = await response.json();
      if (!next.window || !Array.isArray(next.events)) throw new Error("返回的数据不完整。仍显示上次已读取的结果，请稍后重试。");
      data = next; $("oh-feedback").hidden = true; render();
    } catch (error) {
      $("oh-feedback").textContent = error instanceof TypeError || error instanceof SyntaxError
        ? "连接暂时中断，结果可能已过时。请稍后刷新；这不代表没有新事件。" : error.message;
      $("oh-feedback").hidden = false;
    } finally {
      loading = false; $("oh-refresh").disabled = denied; schedule();
    }
  }

  document.querySelectorAll("[data-priority]").forEach(button => button.addEventListener("click", () => {
    selected = button.dataset.priority; renderEvents();
  }));
  $("oh-selection").addEventListener("change", () => {
    clearTimeout(timer);
    $("oh-selection").requestSubmit();
  });
  $("oh-search").addEventListener("input", event => { query = event.target.value.trim().toLocaleLowerCase(); renderEvents(); });
  $("oh-refresh").addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  render(); schedule();
})();
