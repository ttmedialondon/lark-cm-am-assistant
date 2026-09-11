"use strict";

(() => {
  const app = document.getElementById("creators-app");
  if (!app) return;
  const el = key => document.getElementById(`creators-${key}`);
  let data = JSON.parse(el("initial").textContent);
  let tab = app.dataset.tab;
  let page = data.page || 1;
  let query = el("query").value.trim();
  let busy = false, reading = false, version = 0, evidenceVersion = 0, autoDirty = false;
  const rows = new Map();
  const node = (tag, text = "", classes = "") => {
    const item = document.createElement(tag); item.textContent = text; item.className = classes; return item;
  };
  function displayText(text, cmName, creatorName) {
    const names = {CM1: cmName, U1: creatorName};
    return String(text || "").replace(/(?<![A-Za-z0-9_])(CM1|U1)(?![A-Za-z0-9_])/g, token => names[token] || token);
  }
  function notice(text, error = false) {
    el("notice").textContent = text;
    el("notice").className = text ? `alert alert-${error ? "danger" : "success"}` : "alert d-none";
  }
  async function read(response) {
    const body = response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
    if (!response.ok || response.redirected) throw new Error(body.error || "访问失败，请刷新或重新登录。");
    return body;
  }
  function selectedCM() { return el("cm")?.value || data.selected_cm_id || ""; }
  function clearRows() { rows.clear(); el("rows").replaceChildren(); }
  function makeRow(item) {
    const row = node("tr");
    const person = node("td");
    person.append(node("strong", item.display_name || "—"), node("div", item.open_id || "—", "small text-secondary"));
    const handleCell = node("td"); handleCell.style.minWidth = "250px";
    const saved = node("div", "", "mb-2"); handleCell.append(saved);
    const editor = node("div");
    const options = node("div", "", "mb-2"); editor.append(options);
    const input = node("textarea", "", "form-control");
    input.rows = 2; input.maxLength = 3200; input.placeholder = "填写 handle；多个账号每行一个";
    input.setAttribute("aria-label", `${item.display_name || "达人"} 的 handle`);
    input.value = (item.handles || []).join("\n");
    const parts = {row, saved, editor, options, input, dirty: false, optionsKey: ""};
    input.addEventListener("input", () => { parts.dirty = true; });
    editor.append(input); handleCell.append(editor);
    const status = node("td"); status.style.minWidth = "180px";
    parts.status = node("div"); parts.reason = node("div", "", "small text-secondary mt-1");
    status.append(parts.status, parts.reason);
    const actions = node("td");
    const actionsList = node("div", "", "d-flex gap-1 flex-wrap");
    for (const [action, text] of [["confirm", "确认入库"], ["defer", "暂缓"], ["evidence", "相关材料"]]) {
      const button = node("button", text, `btn btn-sm ${action === "confirm" ? "btn-primary" : "btn-outline-secondary"}`);
      button.type = "button"; parts[action] = button;
      button.addEventListener("click", () => {
        if (action === "evidence") return showEvidence(item.id);
        const handles = [...options.querySelectorAll("input:checked")].map(box => box.value)
          .concat(input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean));
        return mutate(`/tasks/${item.id}/${action}`, action === "confirm" ? {handles: JSON.stringify([...new Set(handles)])} : {});
      });
      actionsList.append(button);
    }
    actions.append(actionsList); row.append(person, handleCell, status, actions); el("rows").append(row);
    return parts;
  }
  function render() {
    const run = data.run, counts = data.counts || {}, usage = data.usage || {};
    el("run-status").textContent = run?.status_label || "暂无分析批次";
    const prepared = Math.max(0, (counts.total || 0) - (counts.pending || 0) - (counts.preparing || 0));
    el("progress").textContent = run?.status === "preparing" ? `已整理 ${prepared} / ${counts.total || 0} 人` : run ? `待分析 ${counts.ready || 0} · 排队 ${counts.queued || 0} · 分析中 ${counts.running || 0}` : "";
    el("usage").textContent = run ? `实际 API 请求 ${usage.requests ?? usage.api_requests ?? 0} 次 · 输入 ${usage.input_tokens || 0} / 输出 ${usage.output_tokens || 0} tokens · 自动入库${run.auto_admit ? "已开启" : "未开启"}` : "只有第二步建议归属的达人会进入候选。";
    if (data.can_manage) {
      el("cm").disabled = busy;
      el("preview").disabled = busy || !selectedCM() || ["preparing", "running", "paused"].includes(run?.status);
      el("start").disabled = busy || !run?.can_start;
      el("pause").hidden = !run?.can_pause; el("pause").disabled = busy;
      el("resume").hidden = !run?.can_resume; el("resume").disabled = busy;
      el("save-auto").disabled = busy || !run;
      el("auto").disabled = busy || !run;
      if (!autoDirty) el("auto").checked = !!run?.auto_admit;
    }
    for (const key of ["candidates", "mine"]) {
      el(key).className = `btn ${key === tab ? "btn-primary" : "btn-outline-secondary"}`;
      el(key).setAttribute("aria-pressed", String(key === tab));
      el(key).disabled = busy;
    }
    const ids = new Set((data.items || []).map(item => item.id));
    for (const [id, parts] of rows) if (!ids.has(id)) { parts.row.remove(); rows.delete(id); }
    for (const item of data.items || []) {
      if (!rows.has(item.id)) rows.set(item.id, makeRow(item));
      const parts = rows.get(item.id);
      const options = item.handle_options || [];
      parts.saved.textContent = item.handles?.length ? item.handles.map(handle => `@${handle.replace(/^@/, "")}`).join(" · ") : "—";
      parts.editor.hidden = !item.can_confirm;
      parts.input.disabled = busy || !item.can_confirm;
      if (!parts.dirty && document.activeElement !== parts.input) {
        parts.input.value = item.handles?.length ? item.handles.join("\n") : options.length === 1 ? options[0].handle : "";
      }
      const optionsKey = JSON.stringify(options);
      if (parts.optionsKey !== optionsKey && !parts.dirty) {
        parts.options.replaceChildren(); parts.optionsKey = optionsKey;
        for (const option of options.length > 1 ? options : []) {
          const label = node("label", "", "form-check mb-1");
          const checkbox = node("input", "", "form-check-input"); checkbox.type = "checkbox"; checkbox.value = option.handle;
          checkbox.addEventListener("change", () => { parts.dirty = true; });
          label.append(checkbox, node("span", `@${option.handle.replace(/^@/, "")}`, "form-check-label")); parts.options.append(label);
        }
      }
      for (const checkbox of parts.options.querySelectorAll("input")) checkbox.disabled = busy || !item.can_confirm;
      parts.status.textContent = item.status_label || "待确认";
      parts.reason.textContent = displayText(item.reason, data.cms?.find(cm => cm.id === data.selected_cm_id)?.name, item.display_name);
      parts.confirm.hidden = !item.can_confirm; parts.confirm.disabled = busy;
      parts.confirm.textContent = tab === "mine" ? "保存修改" : "确认入库";
      parts.defer.hidden = !item.can_defer; parts.defer.disabled = busy;
      parts.evidence.hidden = !item.has_evidence;
    }
    page = data.page || 1;
    el("empty").hidden = !!data.items?.length;
    el("empty").textContent = tab === "mine" ? "还没有已入库的达人。" : "暂无候选达人。";
    el("page-label").textContent = `共 ${data.total_count || 0} 人 · 第 ${page} / ${Math.max(1, data.total_pages || 0)} 页`;
    el("previous").disabled = busy || page <= 1; el("next").disabled = busy || page >= (data.total_pages || 1);
  }
  async function refresh(automatic = false) {
    if (automatic && (reading || busy || document.hidden || [...rows.values()].some(row => row.dirty))) return;
    const token = ++version; reading = true;
    const params = new URLSearchParams({tab, q: query, page: String(page)});
    if (selectedCM()) params.set("cm_id", selectedCM());
    history.replaceState(null, "", `/creators?${params}`);
    try {
      const next = await read(await fetch(`/creators/data?${params}`, {cache: "no-store"}));
      if (token === version) { data = next; render(); }
    } catch (error) { if (token === version) notice(error.message, true); }
    finally { if (token === version) reading = false; }
  }
  async function mutate(path, fields = {}) {
    if (busy) return;
    busy = true; render(); notice("");
    try {
      await read(await fetch(`/creators${path}`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({csrf_token: app.dataset.csrf, ...fields})}));
      if (path.endsWith("auto-admit")) autoDirty = false;
      if (path.endsWith("/confirm")) {
        const taskId = path.split("/")[2];
        rows.get(taskId)?.row.remove(); rows.delete(taskId);
        notice(tab === "mine" ? "handle 已保存。" : "已确认入库，可在“我的达人”查看。");
      }
      else notice("已保存。");
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { busy = false; render(); }
  }
  async function showEvidence(taskId) {
    const token = ++evidenceVersion;
    el("evidence-title").textContent = "相关材料"; el("evidence-body").textContent = "正在读取…";
    if (!el("evidence").open) el("evidence").showModal();
    try {
      const evidence = await read(await fetch(`/creators/tasks/${taskId}/evidence`, {cache: "no-store"}));
      if (token !== evidenceVersion) return;
      el("evidence-title").textContent = `${evidence.display_name || "达人"} · 相关材料`;
      el("evidence-body").replaceChildren();
      if (evidence.summary) el("evidence-body").append(node("p", displayText(evidence.summary, evidence.cm_name, evidence.display_name)));
      for (const message of evidence.messages || []) {
        const section = node("section", "", "border-top pt-3 mt-3");
        const ref = /^[MG][1-9][0-9]*$/.test(message.ref) ? `[${message.ref}]` : "";
        section.append(node("div", [ref, message.sender_name, message.time_label, message.chat_name].filter(Boolean).join(" · "), "small text-secondary mb-2"));
        section.append(node("blockquote", message.available === false ? "当前无法读取这条材料。" : message.text || "—"));
        el("evidence-body").append(section);
      }
      if (!evidence.messages?.length) el("evidence-body").append(node("p", "当前没有可读取的材料。", "text-secondary"));
    } catch (error) { if (token === evidenceVersion) el("evidence-body").textContent = error.message; }
  }
  for (const key of ["candidates", "mine"]) el(key).addEventListener("click", () => { tab = key; page = 1; clearRows(); refresh(); });
  el("search").addEventListener("submit", event => { event.preventDefault(); query = el("query").value.trim(); page = 1; clearRows(); refresh(); });
  el("refresh").addEventListener("click", () => refresh());
  el("previous").addEventListener("click", () => { page--; clearRows(); refresh(); });
  el("next").addEventListener("click", () => { page++; clearRows(); refresh(); });
  el("evidence-close").addEventListener("click", () => el("evidence").close());
  el("evidence").addEventListener("close", () => { evidenceVersion++; el("evidence-body").replaceChildren(); });
  if (data.can_manage) {
    if (data.cms?.length) {
      el("cm").replaceChildren(...data.cms.map(cm => { const option = node("option", cm.name); option.value = cm.id; return option; }));
      el("cm").value = data.selected_cm_id;
    }
    el("cm").addEventListener("change", () => {
      data = {...data, run: null, items: [], page: 1, total_count: 0, total_pages: 0};
      page = 1; autoDirty = false; clearRows(); render(); refresh();
    });
    el("preview").addEventListener("click", () => mutate("/preview", {cm_id: selectedCM()}));
    for (const action of ["start", "pause", "resume"]) el(action).addEventListener("click", () => mutate(`/runs/${data.run.id}/${action}`));
    el("auto").addEventListener("change", () => { autoDirty = true; });
    el("save-auto").addEventListener("click", () => mutate(`/runs/${data.run.id}/auto-admit`, {enabled: el("auto").checked ? "1" : "0"}));
  }
  render();
  setInterval(() => refresh(true), 8000);
})();
