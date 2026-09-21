"use strict";

(() => {
  const app = document.getElementById("ownership-app");
  if (!app) return;
  const owner = app.dataset.owner === "1";
  const base = "/admin/ai/creator-ownership";
  const el = name => document.getElementById(`ownership-${name}`);
  const selected = new Set();
  const rows = new Map();
  let data = JSON.parse(el("initial").textContent);
  let busy = false;
  let reading = false;
  let requestVersion = 0;
  let page = data.page || 1;
  let query = el("query").value.trim();
  let evidenceVersion = 0;
  let revision = "", revisionURL = "", denied = false;
  el("initial").textContent = "";

  function node(tag, text = "", className = "") {
    const value = document.createElement(tag);
    value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function displayText(text, cmName, creatorName) {
    const names = {CM1: cmName, U1: creatorName};
    return String(text || "").replace(/(?<![A-Za-z0-9_])(CM1|U1)(?![A-Za-z0-9_])/g, token => names[token] || token);
  }
  function notice(message, error = false) {
    el("notice").textContent = message;
    el("notice").className = message ? `alert alert-${error ? "danger" : "success"}` : "alert d-none";
  }
  async function readResponse(response) {
    if ([401, 403].includes(response.status) || response.redirected) {
      denied = true; busy = true; evidenceVersion++;
      data = {cms: [], items: [], counts: {}, usage: {}, run: null};
      selected.clear(); el("evidence-body").replaceChildren(); el("evidence").close(); render();
    }
    const payload = response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
    if (!response.ok || response.redirected) {
      throw new Error(payload.error || "访问失败，请检查登录状态或刷新后重试。");
    }
    return payload;
  }
  function selection() {
    if (!owner) return;
    const current = (data.items || []).filter(item => item.can_defer);
    const chosen = current.filter(item => selected.has(item.id));
    el("select-all").checked = current.length > 0 && chosen.length === current.length;
    el("select-all").indeterminate = chosen.length > 0 && chosen.length < current.length;
    el("select-all").disabled = busy || !current.length;
    el("defer").disabled = busy || !chosen.length || chosen.some(item => !item.can_defer);
    el("selected").textContent = chosen.length ? `已选 ${chosen.length} 人` : "";
  }
  function makeRow(item) {
    const tr = node("tr");
    const parts = {};
    if (owner) {
      const td = node("td");
      parts.checkbox = node("input", "", "form-check-input");
      parts.checkbox.type = "checkbox";
      parts.checkbox.addEventListener("change", () => {
        if (parts.checkbox.checked) selected.add(item.id); else selected.delete(item.id);
        selection();
      });
      td.append(parts.checkbox); tr.append(td);
    }
    const person = node("td");
    const personWrap = node("div", "", "d-flex gap-2 align-items-center ownership-name");
    const avatar = node("span", (item.display_name || "?").slice(0, 1).toUpperCase(), "avatar avatar-sm rounded-circle flex-shrink-0 position-relative overflow-hidden");
    avatar.setAttribute("aria-hidden", "true");
    if (item.avatar_url) {
      const url = new URL(item.avatar_url, window.location.origin);
      if (url.protocol === "https:" && !url.username && !url.password) {
        const image = node("img", "", "position-absolute top-0 start-0 w-100 h-100");
        image.alt = ""; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        image.style.objectFit = "cover"; image.style.visibility = "hidden";
        image.addEventListener("load", () => { image.style.visibility = "visible"; }, {once: true});
        image.addEventListener("error", () => image.remove(), {once: true});
        image.src = url.href; avatar.append(image);
      }
    }
    parts.name = node("a", item.display_name || "—", "fw-medium");
    parts.name.target = "_blank"; parts.name.rel = "noopener noreferrer";
    personWrap.append(avatar, parts.name); person.append(personWrap); tr.append(person);
    for (const key of ["handle", "result", "confidence", "reason"]) {
      parts[key] = node("td"); tr.append(parts[key]);
    }
    const actions = node("td", "", "ownership-actions");
    const list = node("div", "", "d-flex flex-wrap gap-1");
    if (owner) {
      parts.candidate = node("a", "查看候选", "btn btn-sm btn-outline-primary");
      list.append(parts.candidate);
      for (const [action, label] of [["defer", "暂缓"], ["evidence", "相关对话"]]) {
        parts[action] = node("button", label, "btn btn-sm btn-outline-secondary");
        parts[action].type = "button";
        parts[action].addEventListener("click", () => action === "evidence" ? showEvidence(item.id) : mutate(action, {task_ids: item.id}));
        list.append(parts[action]);
      }
    } else { list.append(node("span", "只读", "text-secondary")); }
    actions.append(list); tr.append(actions);
    el("rows").append(tr);
    return {tr, parts};
  }
  function render() {
    const run = data.run;
    const counts = data.counts || {};
    const usage = data.usage || {};
    const cmName = data.cms?.find(cm => cm.id === data.selected_cm_id)?.name;
    const preparing = run?.status === "preparing";
    const prepared = Math.max(0, (counts.total || 0) - (counts.pending || 0) - (counts.preparing || 0));
    const exceptions = (counts.failed || 0) + (counts.invalid || 0) + (counts.cancelled || 0);
    el("scope").textContent = run ? `已分类达人 ${run.directory_count || 0} 人 · 已在当前 CM 名下排除 ${run.excluded_owned_count || 0} 人 · 缺少 Lark 用户排除 ${run.excluded_identity_count || 0} 人` : "";
    el("run-status").textContent = run?.status_label || "尚未预筛选";
    el("progress").textContent = preparing ? `已预筛选 ${prepared} / ${counts.total || 0} 人 · 符合分析条件 ${counts.ready || 0} 人` : run ? `范围 ${counts.total || 0} 人 · 待分析 ${counts.ready || 0} · 排队 ${counts.queued || 0} · 分析中 ${counts.running || 0}` : "预筛选只读取现有材料，不调用 OpenAI。";
    el("progress-bar").max = counts.total || 1;
    el("progress-bar").value = preparing ? prepared : (counts.completed || 0) + (counts.skipped || 0) + exceptions;
    el("progress-bar").setAttribute("aria-label", preparing ? "本批次预筛选进度" : "本批次分析进度");
    el("usage").textContent = `有结果 ${counts.completed || 0} · 暂不分析 ${counts.skipped || 0} · 异常 / 失效 ${exceptions} · 复用 ${counts.reused || 0}　|　实际 API 请求 ${usage.requests || 0} 次 · 输入 ${usage.input_tokens || 0} / 输出 ${usage.output_tokens || 0} tokens`;
    el("run-note").textContent = run?.status === "paused" ? "已暂停发起新请求；已经发出的请求会完成收尾。" : "暂不分析不代表不属于该 CM。刷新只读取状态，不发起分析。";
    el("api").textContent = data.api_message || "";
    el("cm").disabled = busy;
    if (owner) {
      el("candidates").href = `/creators?tab=candidates&cm_id=${encodeURIComponent(data.selected_cm_id || "")}`;
      el("preview").disabled = busy || !el("cm").value || ["preparing", "running", "paused"].includes(run?.status);
      for (const id of ["start", "start-all"]) el(id).disabled = busy || !run?.can_start || data.api_ready === false;
      el("pause").hidden = !run?.can_pause; el("pause").disabled = busy;
      el("resume").hidden = !run?.can_resume; el("resume").disabled = busy || data.api_ready === false;
    }
    const ids = new Set((data.items || []).map(item => item.id));
    for (const [id, row] of rows) {
      if (!ids.has(id)) { row.tr.remove(); rows.delete(id); selected.delete(id); }
    }
    for (const item of data.items || []) {
      if (!rows.has(item.id)) rows.set(item.id, makeRow(item));
      const {parts} = rows.get(item.id);
      parts.name.textContent = item.display_name || "—";
      if (item.private_chat_url) parts.name.href = item.private_chat_url;
      else parts.name.removeAttribute("href");
      parts.name.title = item.private_chat_url ? "在 Lark 中打开你与此人的私聊" : "缺少有效 Lark 用户，暂不能跳转";
      parts.handle.textContent = item.handle || "—";
      parts.result.textContent = item.review_status === "confirmed" ? "已关联" : item.review_status === "deferred" ? "已暂缓" : item.result ? item.result_label : item.status_label || "待处理";
      parts.confidence.textContent = Number.isFinite(item.confidence) ? `${Math.round(item.confidence * 100)}%` : "—";
      parts.reason.textContent = displayText(item.reason, cmName, item.display_name) || "—";
      if (owner) {
        if (!item.can_defer) selected.delete(item.id);
        parts.checkbox.checked = selected.has(item.id);
        parts.checkbox.disabled = busy || !item.can_defer;
        parts.checkbox.setAttribute("aria-label", `选择 ${item.display_name || "达人"}`);
        parts.candidate.hidden = item.result !== "suggested";
        parts.candidate.href = `/creators?tab=candidates&cm_id=${encodeURIComponent(data.selected_cm_id || "")}`;
        parts.defer.disabled = busy || !item.can_defer;
        parts.evidence.hidden = !item.has_evidence;
      }
    }
    el("empty").hidden = !!data.items?.length;
    el("empty").textContent = run ? "当前筛选暂无记录。" : "请选择负责人并预筛选。";
    page = data.page || 1;
    el("total").textContent = `${data.total_count || 0} 人`;
    el("page-label").textContent = `第 ${page} / ${Math.max(1, data.total_pages || 0)} 页`;
    el("previous").disabled = busy || page <= 1;
    el("next").disabled = busy || page >= (data.total_pages || 1);
    selection();
  }
  function syncURL() {
    const params = new URLSearchParams({status: el("status").value, page: String(page)});
    if (el("cm").value) params.set("cm_id", el("cm").value);
    if (data.run?.id) params.set("run_id", data.run.id);
    if (query) params.set("q", query);
    history.replaceState(null, "", `${base}?${params}`);
    return params;
  }
  async function refresh(automatic = false) {
    if (denied || (automatic && (reading || busy || document.hidden))) return;
    const version = ++requestVersion;
    reading = true;
    try {
      const url = `${base}/data?${syncURL()}`;
      const response = await fetch(url, {cache: "no-store",
        headers: revisionURL === url && revision ? {"If-None-Match": revision} : {}});
      if (response.status === 304) return;
      const next = await readResponse(response);
      if (version !== requestVersion) return;
      revision = response.headers.get("ETag") || ""; revisionURL = url;
      if (JSON.stringify(data) !== JSON.stringify(next)) { data = next; render(); }
    } catch (error) { if (version === requestVersion) notice(error.message, true); }
    finally { if (version === requestVersion) reading = false; }
  }
  async function mutate(action, fields = {}) {
    if (denied || !owner || busy) return;
    busy = true; render(); notice("");
    try {
      const path = action === "preview" ? "/preview" : `/${data.run.id}/${action}`;
      const result = await readResponse(await fetch(base + path, {
        method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({csrf_token: app.dataset.csrf, ...fields}),
      }));
      if (action === "preview") { data.run = {id: result.run_id}; page = 1; }
      selected.clear();
      notice(action === "defer" ? `已暂缓 ${result.count || 0} 人。` : "已提交，正在更新状态。");
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { busy = false; render(); }
  }
  async function showEvidence(taskId) {
    const version = ++evidenceVersion;
    el("evidence-title").textContent = "相关对话";
    el("evidence-body").textContent = "正在读取…";
    if (!el("evidence").open) el("evidence").showModal();
    try {
      const evidence = await readResponse(await fetch(`${base}/tasks/${taskId}/evidence`, {cache: "no-store"}));
      if (version !== evidenceVersion) return;
      el("evidence-title").textContent = `${evidence.display_name || "达人"} · 相关对话`;
      el("evidence-body").replaceChildren();
      const explain = text => displayText(text, evidence.cm_name, evidence.display_name);
      if (evidence.summary) el("evidence-body").append(node("p", explain(evidence.summary)));
      if (evidence.conflicts?.length) el("evidence-body").append(node("p", `冲突线索：${explain(evidence.conflicts.join("；"))}`, "text-warning"));
      if (evidence.missing_info?.length) el("evidence-body").append(node("p", `还缺：${explain(evidence.missing_info.join("；"))}`, "text-secondary"));
      for (const message of evidence.messages || []) {
        const section = node("section", "", "border-top pt-3 mt-3");
        const ref = /^M[1-9][0-9]*$/.test(message.ref) ? `[${message.ref}]` : "";
        section.append(node("div", [ref, message.sender_name, message.time_label, message.chat_name].filter(Boolean).join(" · "), "small text-secondary mb-2"));
        section.append(node("blockquote", message.available === false ? "当前无法读取这条消息。" : message.text || "—"));
        el("evidence-body").append(section);
      }
      if (!evidence.messages?.length) el("evidence-body").append(node("p", "当前没有可读取的相关对话。", "text-secondary"));
    } catch (error) { if (version === evidenceVersion) el("evidence-body").textContent = error.message; }
  }

  if (data.cms?.length) {
    el("cm").replaceChildren(...data.cms.map(cm => {
      const option = node("option", cm.name); option.value = cm.id; return option;
    }));
    el("cm").value = data.selected_cm_id;
  }
  el("cm").addEventListener("change", () => {
    data = {...data, run: null, items: [], counts: {}, usage: {}, page: 1, total_count: 0, total_pages: 0};
    selected.clear(); page = 1; render(); refresh();
  });
  el("status").addEventListener("change", () => { selected.clear(); page = 1; refresh(); });
  el("search").addEventListener("submit", event => { event.preventDefault(); query = el("query").value.trim(); selected.clear(); page = 1; refresh(); });
  el("refresh").addEventListener("click", () => refresh());
  el("previous").addEventListener("click", () => { page--; selected.clear(); refresh(); });
  el("next").addEventListener("click", () => { page++; selected.clear(); refresh(); });
  el("evidence-close").addEventListener("click", () => el("evidence").close());
  el("evidence").addEventListener("close", () => { evidenceVersion++; el("evidence-body").replaceChildren(); });
  if (owner) {
    el("preview").addEventListener("click", () => mutate("preview", {cm_id: el("cm").value}));
    el("start").addEventListener("click", () => mutate("start", {limit: "20"}));
    el("start-all").addEventListener("click", () => mutate("start", {limit: "all"}));
    el("pause").addEventListener("click", () => mutate("pause"));
    el("resume").addEventListener("click", () => mutate("resume"));
    el("defer").addEventListener("click", () => mutate("defer", {task_ids: [...selected].join(",")}));
    el("select-all").addEventListener("change", () => {
      for (const item of data.items || []) {
        if (el("select-all").checked && item.can_defer) selected.add(item.id); else selected.delete(item.id);
      }
      render();
    });
  }
  render();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  // Settled results still recheck live access/material, without an 8-second full refresh loop.
  let lastCheck = Date.now();
  setInterval(() => {
    const active = ["preparing", "running"].includes(data.run?.status);
    if (Date.now() - lastCheck >= (active ? 8000 : 60000)) {
      lastCheck = Date.now(); refresh(true);
    }
  }, 8000);
})();
