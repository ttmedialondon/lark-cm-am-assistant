/* Isolated fictional prototype: only this tab's sessionStorage is used. */
(() => {
  "use strict";
  const payload = document.getElementById("creator-setup-data");
  if (!payload) return;
  const demo = JSON.parse(payload.textContent);
  const accounts = new Map(demo.accounts.map(account => [account.id, account]));
  const conversations = new Map(demo.conversations.map(conversation => [conversation.id, conversation]));
  const decisions = new Set(["pending", "mine", "defer", "not_mine"]);
  const byId = id => document.getElementById(id);
  const rows = [...document.querySelectorAll("tr[data-account-id]")];
  const search = byId("demo-search");
  const filter = byId("demo-filter");
  const status = byId("demo-save-status");
  const dialog = byId("demo-conversation-dialog");
  const storageKey = demo.storage_key;

  function initialState() {
    return Object.fromEntries(demo.accounts.map(account => [account.id, {
      decision: account.decision,
      selected_conversations: [...account.selected_conversations],
    }]));
  }

  // Storage is untrusted: accept only known decisions and per-account demo IDs.
  function cleanState(candidate) {
    const clean = initialState();
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return clean;
    for (const account of demo.accounts) {
      if (!Object.prototype.hasOwnProperty.call(candidate, account.id)) continue;
      const row = candidate[account.id];
      if (!row || typeof row !== "object" || Array.isArray(row)) continue;
      if (decisions.has(row.decision)) clean[account.id].decision = row.decision;
      if (account.owner_conflict && clean[account.id].decision === "mine") clean[account.id].decision = "pending";
      if (Array.isArray(row.selected_conversations)) {
        clean[account.id].selected_conversations = [...new Set(row.selected_conversations.filter(id => (
          typeof id === "string" && account.conversation_ids.includes(id) && conversations.has(id)
        )))];
      }
    }
    return clean;
  }

  let state = initialState();
  let restored = false;
  let storageUnavailable = false;
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (raw) {
      const saved = JSON.parse(raw);
      if (saved && saved.version === demo.version && saved.accounts && typeof saved.accounts === "object"
          && !Array.isArray(saved.accounts)) {
        state = cleanState(saved.accounts);
        restored = true;
      } else {
        status.textContent = "旧版或无效演示存档未使用，已载入初始假数据。";
      }
    }
  } catch (_) {
    storageUnavailable = true;
    status.textContent = "演示存档无法读取，已载入初始假数据；保存时将重试，仅影响本标签演示。";
  }

  function totals(snapshot) {
    const mine = demo.accounts.filter(account => snapshot[account.id].decision === "mine");
    const selected = new Set(mine.flatMap(account => snapshot[account.id].selected_conversations));
    return {mine, selected};
  }

  function applyFilters() {
    const query = search.value.trim().replace(/^@+/, "").toLocaleLowerCase();
    let visible = 0;
    for (const row of rows) {
      const account = accounts.get(row.dataset.accountId);
      const decision = state[account.id].decision;
      const searchable = [account.handle, account.contact_label,
        ...account.conversation_ids.map(id => conversations.get(id).title)].join(" ").toLocaleLowerCase();
      row.hidden = !(searchable.includes(query) && (filter.value === "all"
        || (filter.value === "conflict" ? Boolean(account.owner_conflict) : decision === filter.value)));
      if (!row.hidden) visible += 1;
    }
    byId("demo-visible-count").textContent = `${visible} / ${demo.accounts.length} 个账号`;
    byId("demo-empty").hidden = visible !== 0;
  }

  function renderControls() {
    const count = {pending: 0, mine: 0, defer: 0, not_mine: 0};
    for (const row of rows) {
      const account = accounts.get(row.dataset.accountId);
      const current = state[account.id];
      row.dataset.decision = current.decision;
      row.querySelector(".csp-decision").value = current.decision;
      count[current.decision] += 1;
      for (const input of row.querySelectorAll("input[data-conversation-id]")) {
        input.checked = current.selected_conversations.includes(input.dataset.conversationId);
      }
      const selectedCount = row.querySelector("[data-selected-count]");
      if (selectedCount) selectedCount.textContent = current.selected_conversations.length;
      row.querySelector("[data-scope-note]").textContent = current.decision === "mine"
        ? (current.selected_conversations.length ? "已选会话将进入本账号的演示结果。" : "账号已确认；未选会话不会阻塞，可以之后再补。")
        : "当前会话选择仅作草稿，不计入“由我负责”的结果。";
    }
    byId("demo-mine-count").textContent = `${count.mine} 个`;
    byId("demo-pending-count").textContent = `${count.pending} 个`;
    byId("demo-other-count").textContent = `${count.defer} / ${count.not_mine} 个`;
    const {mine, selected} = totals(state);
    byId("demo-selection-total").textContent = `由我负责 ${mine.length} 个账号 · 已选 ${selected.size} 个去重会话`;
    applyFilters();
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function previewButton(id) {
    const button = element("button", conversations.get(id).title, "csp-preview-link");
    button.type = "button";
    button.dataset.previewConversation = id;
    return button;
  }

  function renderResults(snapshot, description) {
    const {mine, selected} = totals(snapshot);
    const missingContacts = mine.filter(account => !account.contact_label).length;
    byId("demo-results-description").textContent = description;
    byId("demo-results-summary").textContent = `${mine.length} 个账号 · ${selected.size} 个去重会话 · ${missingContacts} 个账号的联系人待补`;
    const list = byId("demo-result-accounts");
    list.replaceChildren();
    for (const account of mine) {
      const card = element("article", undefined, "csp-result-account");
      card.append(element("h3", `@${account.handle}`), element("p", `负责人：${demo.cm_label}`),
        element("p", account.contact_label || "联系人待补（不阻塞账号确认）"));
      const selectedIds = snapshot[account.id].selected_conversations;
      if (!selectedIds.length) card.append(element("p", "未选择会话 · 账号确认有效，会话可后补"));
      else {
        const chats = element("ul");
        for (const id of selectedIds) {
          const item = element("li");
          item.append(previewButton(id));
          chats.append(item);
        }
        card.append(chats);
      }
      list.append(card);
    }
    if (!mine.length) list.append(element("p", "当前没有确认由我负责的演示账号。", "csp-small"));
    const chatList = byId("demo-result-conversations");
    chatList.replaceChildren();
    for (const id of selected) {
      const item = element("li");
      item.append(previewButton(id));
      chatList.append(item);
    }
    if (!selected.size) chatList.append(element("li", "暂无已选会话。可以只确认账号，不选择会话。"));
  }

  function markChanged() {
    status.textContent = "有未保存的演示修改；刷新将恢复上次保存内容。没有数据提交到服务器。";
    byId("demo-results-description").textContent = "下方是上次保存或初始演示结果；上方未保存的修改尚未计入。";
    renderControls();
  }

  document.addEventListener("change", event => {
    const target = event.target;
    const account = accounts.get(target.dataset.accountId);
    if (!account) return;
    if (target.matches(".csp-decision")) {
      if (!decisions.has(target.value)) return;
      state[account.id].decision = account.owner_conflict && target.value === "mine" ? "pending" : target.value;
    } else if (target.matches("input[data-conversation-id]")) {
      const id = target.dataset.conversationId;
      if (!account.conversation_ids.includes(id) || !conversations.has(id)) return;
      const selected = new Set(state[account.id].selected_conversations);
      if (target.checked) selected.add(id); else selected.delete(id);
      state[account.id].selected_conversations = [...selected];
    } else return;
    markChanged();
  });
  search.addEventListener("input", applyFilters);
  filter.addEventListener("change", applyFilters);
  byId("demo-clear-filters").addEventListener("click", () => {
    search.value = "";
    filter.value = "all";
    applyFilters();
    search.focus();
  });

  byId("demo-save").addEventListener("click", () => {
    state = cleanState(state);
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({version: demo.version, accounts: state}));
      storageUnavailable = false;
      status.textContent = "演示结果已保存在当前标签；刷新保留，不跨设备保存。未提交服务器，正式数据未改动。";
      renderResults(state, "上次保存的演示结果；仅在当前浏览器标签内暂存，不是正式名单。");
    } catch (_) {
      storageUnavailable = true;
      status.textContent = "浏览器未允许保存；下方只展示本次演示结果，刷新可能丢失。未提交服务器。";
      renderResults(state, "本次结果预览（未能保存）；刷新可能丢失，不是正式名单。");
    }
    renderControls();
    byId("demo-results-title").focus({preventScroll: true});
    byId("demo-results").scrollIntoView({behavior: "smooth", block: "start"});
  });
  byId("demo-reset").addEventListener("click", () => {
    state = initialState();
    search.value = "";
    filter.value = "all";
    try {
      sessionStorage.removeItem(storageKey);
      storageUnavailable = false;
      status.textContent = "已恢复初始假数据并清除本标签演示存档；正式数据没有变化。";
    } catch (_) {
      storageUnavailable = true;
      status.textContent = "页面已恢复初始假数据，但无法清除浏览器演示存档；刷新可能恢复旧结果。";
    }
    renderControls();
    renderResults(state, "初始演示状态；账号确认与会话范围分别展示。");
  });

  document.addEventListener("click", event => {
    const button = event.target.closest("button[data-preview-conversation]");
    if (!button) return;
    const conversation = conversations.get(button.dataset.previewConversation);
    if (!conversation) return;
    byId("demo-dialog-title").textContent = conversation.title;
    byId("demo-dialog-context").textContent = `${conversation.type === "group" ? "群聊" : "私聊"} · ${conversation.context}`;
    byId("demo-dialog-participants").textContent = `虚构参与者：${conversation.participants.join("、")}`;
    const messages = byId("demo-dialog-messages");
    messages.replaceChildren();
    for (const message of conversation.messages) {
      const article = element("article", undefined, "csp-dialog-message");
      article.append(element("strong", `${message.sender} · ${message.time}`), element("p", message.text));
      messages.append(article);
    }
    dialog.showModal();
  });
  byId("demo-dialog-close").addEventListener("click", () => dialog.close());
  renderControls();
  renderResults(state, restored ? "已恢复本标签上次保存的演示结果；不是正式名单。" : "初始演示状态；账号确认与会话范围分别展示。");
  if (restored && !storageUnavailable) status.textContent = "已恢复本标签的演示存档；刷新保留已保存内容，不跨设备保存。正式数据未改动。";
})();
