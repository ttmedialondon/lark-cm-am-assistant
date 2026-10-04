"use strict";

(() => {
  const app = document.getElementById("creators-app");
  if (!app) return;
  const el = key => document.getElementById(`creators-${key}`);
  let data = JSON.parse(el("initial").textContent);
  let tab = app.dataset.tab;
  let page = data.page || 1;
  let query = el("query").value.trim();
  let busy = false, reading = false, refreshQueued = false, version = 0, evidenceVersion = 0;
  const rows = new Map();
  let importPreview = null;
  const rowKey = item => `${item.kind || "person"}:${item.id}`;
  const node = (tag, text = "", classes = "") => {
    const item = document.createElement(tag); item.textContent = text; item.className = classes; return item;
  };
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
  function selectedCMName() { return data.cms?.find(cm => cm.id === selectedCM())?.name || "我"; }
  function clearRows() { for (const row of rows.values()) row.searchVersion++; rows.clear(); el("rows").replaceChildren(); }
  function importMessage(text, error = false) {
    el("import-message").textContent = text;
    el("import-message").className = `small mt-3 ${error ? "text-danger" : "text-secondary"}`;
  }
  function makeImportRow(item) {
    const row = node("tr"), handleCell = node("td"), userCell = node("td");
    handleCell.style.minWidth = "200px"; userCell.style.minWidth = "240px";
    const handle = node("input", "", "form-control"); handle.maxLength = 160;
    handle.setAttribute("aria-label", "达人 handle"); handle.placeholder = "—";
    const groups = node("div", "", "small text-secondary mt-1");
    handleCell.append(handle, groups);
    const searchLine = node("div", "", "d-flex gap-1");
    const user = node("input", "", "form-control"); user.maxLength = 120; user.placeholder = "— 搜索对应用户";
    user.setAttribute("aria-label", "搜索当前负责人可访问的用户");
    const search = node("button", "搜索", "btn btn-sm btn-outline-secondary"); search.type = "button";
    const choices = node("div", "", "d-flex flex-column gap-1 mt-1"); choices.setAttribute("aria-live", "polite");
    searchLine.append(user, search); userCell.append(searchLine, choices);
    const editNote = node("div", "账号或用户已修改，原 AI 判断不适用于本次修改；确认时重新核验。", "small text-warning mt-1");
    editNote.hidden = true;
    const actions = node("td"), actionsList = node("div", "", "d-flex gap-1 flex-wrap");
    const confirm = node("button", "确认入库", "btn btn-sm btn-primary"); confirm.type = "button";
    const defer = node("button", "暂缓", "btn btn-sm btn-outline-secondary"); defer.type = "button";
    const evidence = node("button", "查看证据链", "btn btn-sm btn-outline-secondary"); evidence.type = "button";
    evidence.addEventListener("click", () => showEvidence(item.id, parts.dirty));
    actionsList.append(confirm, defer, evidence); actions.append(actionsList, editNote);
    row.append(handleCell, userCell, actions);
    const parts = {row, handle, groups, user, search, choices, editNote, confirm, defer, evidence, contactId: "", dirty: false, searchVersion: 0};
    const check = () => {
      confirm.disabled = busy || !parts.canConfirm || !handle.value.trim() || !parts.contactId;
      editNote.hidden = !parts.dirty;
    };
    handle.addEventListener("input", () => { parts.dirty = true; check(); });
    user.addEventListener("input", () => { parts.dirty = true; parts.contactId = ""; parts.searchVersion++; choices.replaceChildren(); check(); });
    search.addEventListener("click", async () => {
      const token = ++parts.searchVersion, scope = selectedCM(); search.disabled = true;
      choices.textContent = "正在查找…";
      try {
        const matches = await read(await fetch(`/creators/imports/users?${new URLSearchParams({cm_id: scope, q: user.value.trim()})}`, {cache: "no-store"}));
        if (token !== parts.searchVersion || scope !== selectedCM()) return;
        choices.replaceChildren();
        for (const match of matches) {
          const choice = node("button", [match.display_name || "未命名用户", match.context_label].filter(Boolean).join(" · "), "btn btn-sm btn-outline-secondary text-start");
          choice.type = "button";
          choice.addEventListener("click", () => {
            if (busy) return;
            parts.contactId = match.id; user.value = match.display_name || "未命名用户";
            parts.dirty = true; choices.replaceChildren(); check();
          });
          choices.append(choice);
        }
        if (!matches.length) choices.textContent = "没有找到可访问的用户，请换个姓名试试。";
      } catch (error) { if (token === parts.searchVersion) choices.textContent = error.message; }
      finally { search.disabled = busy || !parts.canConfirm; }
    });
    user.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); search.click(); } });
    confirm.addEventListener("click", () => mutate(`/imports/rows/${item.id}/confirm`, {handle: handle.value.trim(), contact_id: parts.contactId}));
    defer.addEventListener("click", () => mutate(`/imports/rows/${item.id}/defer`));
    return parts;
  }
  function renderImports() {
    el("import-open").disabled = busy || !selectedCM();
    for (const key of ["file", "check", "confirm", "close"]) el(`import-${key}`).disabled = busy;
    el("import-confirm").hidden = !importPreview || importPreview.status !== "preview" || !(importPreview.counts?.valid > 0);
    const batches = data.import_batches || [];
    el("import-batches").hidden = !batches.length;
    el("import-batch-list").replaceChildren();
    for (const batch of batches) {
      const line = node("div", "", "d-flex flex-wrap align-items-center gap-2 mb-2");
      const label = node("div", "", "me-auto");
      label.append(node("strong", batch.filename || "Excel 名单"), node("span", ` · ${batch.status_label || batch.status}`, "text-secondary"));
      const counts = batch.counts || {};
      label.append(node("div", batch.status === "preview" ? `有效 handle ${counts.valid || 0}` : `已入库 ${counts.confirmed || 0} · 待核对 ${counts.review || 0} · 待处理 ${(counts.pending || 0) + (counts.queued || 0)} · 分析中 ${(counts.preparing || 0) + (counts.running || 0)}${counts.failed ? ` · 异常 ${counts.failed}` : ""}`, "small text-secondary"));
      if (batch.error) label.append(node("div", batch.error, "small text-danger"));
      line.append(label);
      for (const [action, text] of [["start", "确认导入"], ["pause", "暂停"], ["resume", "继续"], ["recheck", "重新检查未确认"]]) {
        if (!batch[`can_${action}`]) continue;
        const button = node("button", text, "btn btn-sm btn-outline-primary");
        button.type = "button"; button.disabled = busy;
        button.addEventListener("click", () => mutate(`/imports/${batch.id}/${action}`)); line.append(button);
      }
      el("import-batch-list").append(line);
    }
  }
  function renderImportRow(item, parts) {
    if (!parts.dirty) {
      parts.handle.value = item.handle || ""; parts.user.value = item.contact_id ? item.display_name || "" : "";
      parts.contactId = item.contact_id || "";
    }
    parts.canConfirm = !!item.can_confirm;
    parts.groups.textContent = item.group_names?.length ? `群：${item.group_names.join("、")}` : "群：—";
    parts.editNote.hidden = !parts.dirty;
    parts.handle.disabled = busy || !parts.canConfirm; parts.user.disabled = busy || !parts.canConfirm;
    parts.search.disabled = busy || !parts.canConfirm;
    parts.confirm.hidden = !parts.canConfirm;
    parts.confirm.disabled = busy || !parts.canConfirm || !parts.handle.value.trim() || !parts.contactId;
    parts.defer.hidden = !item.can_defer; parts.defer.disabled = busy;
    parts.evidence.hidden = !item.has_evidence; parts.evidence.disabled = busy;
    for (const choice of parts.choices.querySelectorAll("button")) choice.disabled = busy;
  }
  function makeRow(item) {
    const row = node("tr");
    const person = node("td");
    const personWrap = node("div", "", "d-flex align-items-center gap-2");
    const avatar = node("span", "", "avatar avatar-sm bg-blue-lt rounded-circle flex-shrink-0 position-relative overflow-hidden");
    avatar.setAttribute("aria-hidden", "true");
    const initial = node("span"); avatar.append(initial);
    const name = node("a", "", "fw-bold");
    const lark = node("a", "", "creator-lark-link text-secondary");
    const icon = node("img"); icon.src = "/static/lark-share.svg"; icon.alt = "";
    icon.width = 16; icon.height = 16; icon.setAttribute("aria-hidden", "true"); lark.append(icon);
    lark.target = "_blank"; lark.rel = "noopener noreferrer";
    lark.title = "在 Lark 中打开私聊";
    const label = node("div", "", "d-flex align-items-center"); label.append(name, lark);
    personWrap.append(avatar, label); person.append(personWrap);
    const handleCell = node("td"); handleCell.style.minWidth = "250px";
    const saved = node("div", "", "mb-2"); handleCell.append(saved);
    const actions = node("td");
    const parts = {row, name, lark, avatar, initial, saved, actions};
    row.append(handleCell, person, actions);
    return parts;
  }
  function render() {
    el("unassigned").hidden = !data.can_view_unassigned;
    el("import-open").hidden = Boolean(data.read_only);
    el("candidates").hidden = Boolean(data.read_only);
    if (data.can_select_cm) el("cm").disabled = busy;
    for (const key of ["mine", "candidates", "unassigned"]) {
      el(key).className = `btn ${key === tab ? "btn-primary" : "btn-outline-secondary"}`;
      el(key).setAttribute("aria-pressed", String(key === tab));
      el(key).disabled = busy;
    }
    const ids = new Set((data.items || []).map(rowKey));
    for (const [id, parts] of rows) if (!ids.has(id)) { parts.searchVersion++; parts.row.remove(); rows.delete(id); }
    for (const [index, item] of (data.items || []).entries()) {
      const key = rowKey(item);
      if (!rows.has(key)) rows.set(key, item.kind === "import" ? makeImportRow(item) : makeRow(item));
      const parts = rows.get(key);
      if (el("rows").children[index] !== parts.row) el("rows").insertBefore(parts.row, el("rows").children[index] || null);
      if (item.kind === "import") { renderImportRow(item, parts); continue; }
      parts.name.textContent = item.display_name || "—";
      if (item.profile_url) parts.name.href = item.profile_url;
      else parts.name.removeAttribute("href");
      parts.name.title = item.profile_url ? "查看个人资料" : "";
      parts.initial.textContent = Array.from((item.display_name || "").trim())[0]?.toUpperCase() || "?";
      if (parts.avatarUrl !== item.avatar_url) {
        parts.avatarUrl = item.avatar_url;
        parts.avatar.querySelector("img")?.remove();
        if (item.avatar_url) {
          const image = node("img", "", "position-absolute top-0 start-0 w-100 h-100");
          image.alt = ""; image.width = 32; image.height = 32;
          image.loading = "lazy"; image.referrerPolicy = "no-referrer";
          image.style.objectFit = "cover"; image.style.visibility = "hidden";
          image.addEventListener("load", () => { image.style.visibility = "visible"; }, {once: true});
          image.addEventListener("error", () => image.remove(), {once: true});
          image.src = item.avatar_url; parts.avatar.append(image);
        }
      }
      parts.lark.hidden = !item.private_chat_url;
      if (item.private_chat_url) parts.lark.href = item.private_chat_url;
      else parts.lark.removeAttribute("href");
      parts.lark.setAttribute("aria-label", `在 Lark 中与 ${item.display_name || "此人"} 私聊（新窗口）`);
      parts.saved.replaceChildren();
      for (const [index, handle] of (item.handles || []).entries()) {
        if (index) parts.saved.append(document.createTextNode(" · "));
        parts.saved.append(node("span", `@${handle.replace(/^@/, "")}`));
      }
      if (!item.handles?.length) parts.saved.textContent = "—";
      parts.actions.replaceChildren();
      for (const evidenceRow of item.evidence_rows || []) {
        const button = node("button", `@${evidenceRow.handle} · 查看证据链`, "btn btn-sm btn-outline-secondary d-block mb-1");
        button.type = "button"; button.disabled = busy;
        button.addEventListener("click", () => showEvidence(evidenceRow.id));
        parts.actions.append(button);
      }
      if (!parts.actions.childElementCount) parts.actions.textContent = "—";
    }
    page = data.page || 1;
    el("empty").hidden = !!data.items?.length;
    el("empty").textContent = tab === "candidates" ? "暂无候选达人。" : "暂无达人。";
    el("page-label").textContent = `共 ${data.total_count || 0} ${tab === "candidates" ? "条" : "人"} · 第 ${page} / ${Math.max(1, data.total_pages || 0)} 页`;
    el("previous").disabled = busy || page <= 1; el("next").disabled = busy || page >= (data.total_pages || 1);
    renderImports();
  }
  async function refresh(automatic = false) {
    if (automatic && (reading || busy || document.hidden || [...rows.values()].some(row => row.dirty) || el("import-dialog").open)) return;
    if (reading) {
      refreshQueued = true; version++;
      return;
    }
    const token = ++version; reading = true;
    const params = new URLSearchParams({tab, q: query, page: String(page)});
    if (selectedCM()) params.set("cm_id", selectedCM());
    history.replaceState(null, "", `/creators?${params}`);
    try {
      const next = await read(await fetch(`/creators/data?${params}`, {cache: "no-store"}));
      if (token === version && !(automatic && [...rows.values()].some(row => row.dirty))) { data = next; render(); }
    } catch (error) { if (token === version) notice(error.message, true); }
    finally {
      reading = false;
      if (refreshQueued) { refreshQueued = false; void refresh(); }
    }
  }
  async function mutate(path, fields = {}) {
    if (busy) return;
    busy = true; render(); notice("");
    try {
      await read(await fetch(`/creators${path}`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({csrf_token: app.dataset.csrf, ...fields})}));
      if (tab === "candidates" && (path.endsWith("/confirm") || path.endsWith("/defer"))) {
        const key = `import:${path.split("/")[3]}`;
        data.items = data.items.filter(item => rowKey(item) !== key);
      }
      if (path.endsWith("/confirm")) {
        notice("已确认入库，可在“我的达人”查看。同一人的多个 handle 会合并展示。");
      }
      else if (path.endsWith("/defer")) {
        notice("已暂缓，尚未确认的修改不会入库。");
      }
      else if (path.endsWith("/start")) {
        page = 1; clearRows();
        notice("名单已导入，系统正在找群、找人。结果保留待核对，确认前请核验 Curva 收录及本人账号证据。");
      }
      else if (path.endsWith("/recheck")) notice("未确认的账号已重新排队，可在候选行查看进度和证据链。");
      else if (path.endsWith("/pause")) notice("已暂停，已发出的请求完成后不再开始下一步。");
      else if (path.endsWith("/resume")) notice("已继续匹配，可在候选行查看进度。");
      else notice("已保存。");
      void refresh();
    } catch (error) { notice(error.message, true); }
    finally { busy = false; render(); }
  }
  async function checkImport() {
    if (busy) return;
    const file = el("import-file").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".xlsx")) return importMessage("请选择 .xlsx 文件。", true);
    if (file.size > 2 * 1024 * 1024) return importMessage("文件过大，请上传不超过 2 MB 的表格。", true);
    busy = true; importPreview = null; render(); importMessage("正在检查表格…");
    el("import-preview").hidden = true;
    try {
      importPreview = await read(await fetch(`/creators/imports/preview?${new URLSearchParams({cm_id: selectedCM()})}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream", "X-CSRF-Token": app.dataset.csrf, "X-Filename": encodeURIComponent(file.name)}, body: file,
      }));
      const counts = importPreview.counts || {};
      const nextStep = importPreview.status !== "preview" ? "这份表已导入，请查看导入记录及候选达人。" : counts.valid ? "请核对后确认导入。" : "请修改表格后重新上传。";
      importMessage(`有效 handle ${counts.valid || 0} · 重复 ${counts.duplicates || 0} · 无效 ${counts.invalid || 0}。${nextStep}`);
      const preview = el("import-preview"); preview.replaceChildren();
      preview.append(node("p", `负责人：${selectedCMName()}。确认导入后，系统自动找群、找人并检查冲突；结果保留待核对。`, "small"));
      const list = node("div", "", "border rounded p-2"); list.style.maxHeight = "240px"; list.style.overflowY = "auto";
      for (const item of (importPreview.rows || []).slice(0, 100)) {
        list.append(node("div", `第 ${item.row_number} 行 · ${item.handle || "—"}${item.error ? ` · ${item.error}` : ""}`, `small py-1 ${item.error ? "text-danger" : ""}`));
      }
      preview.append(list);
      if ((importPreview.rows || []).length > 100) preview.append(node("p", "这里只展示前 100 行，确认后会处理全部有效 handle。", "small text-secondary mt-2"));
      preview.hidden = false;
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { busy = false; render(); }
  }
  async function startImport() {
    if (busy || !importPreview) return;
    busy = true; render(); importMessage("正在导入名单…");
    try {
      await read(await fetch(`/creators/imports/${importPreview.id}/start`, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams({csrf_token: app.dataset.csrf})}));
      importPreview = null; el("import-dialog").close();
      page = 1; clearRows();
      notice("名单已导入，系统正在找群、找人。结果保留待核对，确认前请核验 Curva 收录及本人账号证据。");
      void refresh();
    } catch (error) { importMessage(error.message, true); }
    finally { busy = false; render(); }
  }
  async function showEvidence(rowId, edited = false) {
    const token = ++evidenceVersion, scope = selectedCM(), body = el("evidence-body");
    el("evidence-title").textContent = "证据链"; body.textContent = "正在读取…";
    if (!el("evidence").open) el("evidence").showModal();
    try {
      const evidence = await read(await fetch(`/creators/imports/rows/${rowId}/evidence`, {cache: "no-store"}));
      if (token !== evidenceVersion || scope !== selectedCM()) return;
      el("evidence-title").textContent = `${evidence.handle || "账号"} · 证据链`;
      body.replaceChildren();
      const confidence = (value, label = "置信度") => Number.isFinite(value) ? `${label} ${Math.round(value * 100)}%` : "";
      const section = title => {
        const part = node("section", "", "border-top pt-3 mt-3");
        part.append(node("h4", title)); body.append(part); return part;
      };
      const list = (parent, values, classes = "") => {
        if (!values?.length) return;
        const ul = node("ul", "", classes);
        for (const value of values) ul.append(node("li", value));
        parent.append(ul);
      };
      body.append(node("p", [evidence.phase_label, evidence.status_label].filter(Boolean).join(" · "), "text-secondary"));
      if (edited) body.append(node("p", "以下是已保存的证据。当前账号或用户的修改尚未提交，原 AI 判断不适用于修改后的内容。", "alert alert-warning"));
      list(body, evidence.warnings, "text-warning");
      const groups = section("1. 对应群与相关材料");
      if (evidence.group_decision) {
        const decision = evidence.group_decision;
        groups.append(node("p", ["AI 找群判断", decision.status_label, confidence(decision.confidence, "对此结论的置信度")].filter(Boolean).join(" · "), "fw-semibold"));
        groups.append(node("p", decision.reason || "等待 AI 判断对应群。"));
        if (decision.search_summary) groups.append(node("p", decision.search_summary, "small text-secondary"));
        if (decision.evidence_refs?.length) groups.append(node("p", `依据：${decision.evidence_refs.join("、")}`, "small text-secondary"));
      }
      for (const group of evidence.groups || []) {
        const detail = node("details", "", "border rounded p-3 mb-2"); detail.open = !!group.selected;
        const selection = group.selected === true ? "已选中" : group.selected === false ? "未选中" : "待判断";
        detail.append(node("summary", [group.ref, group.name || "未命名群", selection, confidence(group.confidence)].filter(Boolean).join(" · "), "fw-semibold"));
        detail.append(node("p", group.reason || "尚无选群判断。", "mt-2 mb-2"));
        const members = node("details", "", "mt-3"); members.open = (group.members || []).length <= 8;
        members.append(node("summary", `群成员 · ${(group.members || []).length} 人`, "fw-semibold"));
        if (group.members?.length) {
          const wrap = node("div", "", "table-responsive mt-2"); wrap.style.maxHeight = "320px"; wrap.style.overflowY = "auto";
          const table = node("table", "", "table table-sm table-vcenter");
          const head = node("thead"), header = node("tr"), rows = node("tbody");
          for (const text of ["用户", "已知分类", "本次处理", "依据"]) {
            const th = node("th", text); th.scope = "col"; header.append(th);
          }
          head.append(header); table.append(head, rows);
          for (const member of group.members) {
            const row = node("tr");
            for (const text of [[member.ref, member.display_name || "未命名用户"].filter(Boolean).join(" · "), member.classification || "待定", member.status_label || "—", member.reason || "—"]) row.append(node("td", text));
            rows.append(row);
          }
          wrap.append(table); members.append(wrap);
        } else members.append(node("p", "本群成员信息尚不可用。", "text-secondary mt-2"));
        detail.append(members);
        const messages = node("details", "", "mt-3"); messages.open = (group.messages || []).length <= 3;
        messages.append(node("summary", `相关发言 · ${(group.messages || []).length} 条`, "fw-semibold"));
        for (const message of group.messages || []) {
          const quote = node("div", "", "border-top mt-2 pt-2");
          quote.append(node("p", [message.ref, message.sender_name, message.time_label].filter(Boolean).join(" · "), "small text-secondary mb-1"));
          quote.append(node("blockquote", message.available === false ? "这条材料当前不可读取。" : message.text || "—"));
          messages.append(quote);
        }
        if (!group.messages?.length) messages.append(node("p", "本次没有可展示的相关发言。", "text-secondary mt-2"));
        detail.append(messages); groups.append(detail);
      }
      if (!evidence.groups?.length) groups.append(node("p", "暂无可展示的候选群，具体原因见下方结论。", "text-secondary"));
      const person = evidence.person || {}, personSection = section("2. 用户判断");
      personSection.append(node("p", ["AI 找人判断", person.status_label, confidence(person.confidence, "对此结论的置信度")].filter(Boolean).join(" · "), "fw-semibold"));
      personSection.append(node("p", `用户：${person.display_name || "—"}`));
      personSection.append(node("p", person.reason || "尚未确定具体用户。"));
      if (person.evidence_refs?.length) personSection.append(node("p", `依据：${person.evidence_refs.join("、")}`, "small text-secondary"));
      const conclusion = section("3. 当前结论");
      conclusion.append(node("p", evidence.reason || "尚未完成匹配。"));
      list(conclusion, evidence.conflicts, "text-danger");
    } catch (error) { if (token === evidenceVersion) body.textContent = error.message; }
  }
  for (const key of ["mine", "candidates", "unassigned"]) el(key).addEventListener("click", () => { tab = key; page = 1; clearRows(); refresh(); });
  el("search").addEventListener("submit", event => { event.preventDefault(); query = el("query").value.trim(); page = 1; clearRows(); refresh(); });
  el("refresh").addEventListener("click", () => refresh());
  el("previous").addEventListener("click", () => { page--; clearRows(); refresh(); });
  el("next").addEventListener("click", () => { page++; clearRows(); refresh(); });
  el("evidence-close").addEventListener("click", () => el("evidence").close());
  el("evidence").addEventListener("close", () => { evidenceVersion++; el("evidence-body").replaceChildren(); });
  el("import-open").addEventListener("click", () => {
    importPreview = null; el("import-file").value = ""; el("import-preview").hidden = true;
    el("import-cm").textContent = selectedCMName(); importMessage(""); render(); el("import-dialog").showModal();
  });
  el("import-close").addEventListener("click", () => el("import-dialog").close());
  el("import-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  el("import-file").addEventListener("change", () => {
    importPreview = null; el("import-preview").hidden = true; importMessage(""); render();
  });
  el("import-check").addEventListener("click", checkImport);
  el("import-confirm").addEventListener("click", startImport);
  if (data.can_select_cm) {
    if (data.cms?.length) {
      el("cm").replaceChildren(...data.cms.map(cm => { const option = node("option", cm.name); option.value = cm.id; return option; }));
      el("cm").value = data.selected_cm_id;
    }
    el("cm").addEventListener("change", () => {
      tab = "mine";
      data = {...data, items: [], import_batches: [], page: 1, total_count: 0, total_pages: 0};
      page = 1; clearRows(); render(); refresh();
    });
  }
  render();
  setInterval(() => {
    if (data.import_batches?.some(batch => batch.status === "running")) void refresh(true);
  }, 5000);
})();
