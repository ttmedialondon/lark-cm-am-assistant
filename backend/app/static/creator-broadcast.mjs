// Sending uses a frozen, server-rechecked preview and an idempotent request ID.
export function recipients(entries, routes, groups, purpose) {
  const rows = new Map();
  for (const entry of entries) {
    const key = entry.person ? entry.person.id : `input:${entry.key}`;
    if (!rows.has(key)) rows.set(key, { key, person: entry.person, entries: [], selected: false });
    const row = rows.get(key); row.entries.push(entry); row.selected ||= entry.selected;
  }
  return [...rows.values()].map(row => {
    const options = row.person ? groups.filter(group => group.purpose === purpose && row.person.groups.includes(group.id)) : [];
    return { ...row, options, chatId: routes.get(row.key) ?? (options.length === 1 ? options[0].id : '') };
  });
}
export function buildPlan(rows, mention, purposeLabel) {
  const groups = new Map(), issues = [], people = [];
  for (const row of rows.filter(row => row.selected)) {
    const group = row.options.find(option => option.id === row.chatId);
    const reason = row.entries[0].reason || (!row.person ? (row.entries[0].candidates.length ? '请选择具体达人' : '未找到已入库达人')
      : !row.options.length ? `暂无可用${purposeLabel}` : !group ? `请选择${purposeLabel}`
        : !group.members.includes(row.person.id) ? '人员与群的关联已变化' : '');
    if (row.person) people.push(row.person);
    if (reason) { issues.push({ key: row.key, reason }); continue; }
    if (!groups.has(group.id)) groups.set(group.id, { ...group, people: [], mentions: [] });
    const target = groups.get(group.id); target.people.push(row.person);
    if (mention) target.mentions.push(row.person);
  }
  return { groups: [...groups.values()], issues, people };
}
export function groupPlan(groups, selected, mention) {
  const available = new Map(groups.map(group => [group.id, group]));
  const targets = [], issues = [];
  for (const id of new Set(selected)) {
    const group = available.get(id);
    if (group) targets.push({...group, people:[], mentions:[], mention_all:mention});
    else issues.push({key:id,reason:'群已失效，请重新选择'});
  }
  return {groups:targets,issues,people:[]};
}
export function parseSelection(raw, actor, now = Date.now(), canSelectCm = true) {
  const selection = JSON.parse(raw), age = now - selection.created_at;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (selection.actor_id !== actor || typeof selection.created_at !== 'number' || !Number.isFinite(age) || age < 0 || age > 300000
      || (selection.cm_id !== 'all' && !uuid.test(selection.cm_id)) || (!canSelectCm && selection.cm_id !== actor)
      || !Array.isArray(selection.contact_ids)
      || !selection.contact_ids.length || selection.contact_ids.length > 500
      || !selection.contact_ids.every(id => typeof id === 'string' && uuid.test(id))) throw new Error('带入名单已失效，请回到“我的达人”重新选择。');
  return {cm_id:selection.cm_id,contact_ids:[...new Set(selection.contact_ids)]};
}
export function listState(cmId, purpose, mention, targets) {
  return JSON.stringify([cmId, purpose, mention,
    targets.map(target => [target.contact_id || '', target.chat_id]).sort((a,b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))]);
}
export function permissionReady(result, pending, cm, checkedCm, redirecting = false) {
  return !pending && !redirecting && cm === checkedCm && result?.status === 'ready';
}
export function authorizationUrl(value, actor, origin) {
  if (typeof value !== 'string' || value.length > 512) throw new Error('授权地址无效，请重新检查。');
  const url = new URL(value, origin);
  if (url.origin !== origin || url.pathname !== '/auth/lark/start' || url.hash || url.username || url.password
      || url.searchParams.get('send') !== '1' || url.searchParams.get('account_id') !== actor
      || [...url.searchParams.keys()].some(key => !['send','account_id'].includes(key))) {
    throw new Error('授权账号不一致，请重新检查。');
  }
  return `/auth/lark/start?${new URLSearchParams({send:'1',account_id:actor})}`;
}
export function parseDraft(raw, actor, now = Date.now(), canSelectCm = true) {
  if (typeof raw !== 'string' || raw.length > 300000) throw new Error('授权草稿无效。');
  const draft = JSON.parse(raw), uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const text = (value, limit) => typeof value === 'string' && value.length <= limit && !value.includes('\0');
  const id = value => typeof value === 'string' && uuid.test(value);
  const chat = value => text(value,256) && Boolean(value.trim());
  const age = now - draft?.created_at;
  if (!draft || draft.version !== 1 || draft.actor_id !== actor || typeof draft.created_at !== 'number'
      || !Number.isFinite(age) || age < 0 || age > 1800000
      || (!['all',''].includes(draft.cm_id) && !id(draft.cm_id)) || (!canSelectCm && draft.cm_id !== actor)
      || !['support','community','training','category','campaign','custom'].includes(draft.purpose)
      || typeof draft.mention !== 'boolean' || !text(draft.raw,65536) || !text(draft.message,4000)
      || (draft.fixed_ids !== null && (!Array.isArray(draft.fixed_ids) || draft.fixed_ids.length > 500
        || !draft.fixed_ids.every(id)))
      || !Array.isArray(draft.selected_groups) || draft.selected_groups.length > 500 || !draft.selected_groups.every(chat)) throw new Error('授权草稿已失效。');
  const ids = draft.fixed_ids && [...new Set(draft.fixed_ids)];
  if (draft.fixed_targets !== null && (!ids || !Array.isArray(draft.fixed_targets)
      || draft.fixed_targets.length !== ids.length || new Set(draft.fixed_targets.map(target => target?.contact_id)).size !== ids.length
      || !draft.fixed_targets.every(target => target && ids.includes(target.contact_id) && text(target.chat_id,256)))) {
    throw new Error('草稿接收对象无效，请重新选择。');
  }
  return {version:1,actor_id:actor,created_at:draft.created_at,cm_id:draft.cm_id,purpose:draft.purpose,
    mention:draft.mention,raw:draft.raw,message:draft.message,fixed_ids:ids,
    fixed_targets:draft.fixed_targets?.map(target => ({contact_id:target.contact_id,chat_id:target.chat_id})) || null,
    selected_groups:[...new Set(draft.selected_groups)]};
}

if (typeof document !== 'undefined' && document.getElementById('broadcast-app')) {
  const $ = id => document.getElementById(`broadcast-${id}`);
  const config = JSON.parse($('config').textContent), buttons = [...document.querySelectorAll('[data-purpose]')];
  const canSelectCm = config.can_select_cm === true, actor = $('app').dataset.actor;
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className; return element;
  };
  let entries = [], groups = [], routes = new Map(), purpose = 'support', dirty = true, busy = false;
  let lists = config.lists || [], currentList = null, editingList = null, fixedIds = null, fixedTargets = null;
  let savedState = '', editMode = 'create', pendingAction = null, initialMetadata = '';
  let selectedGroups = new Set(), templates = config.templates || [], currentTemplateId = '', editingTemplate = null, initialTemplate = '';
  let permissionCheck = 0, permissionPending = false, permissionCm = null, permissionResult = null;
  let redirecting = false, pendingRestore = false, loadingGroups = false;
  let frozenPreview = null, sendRequestId = null, sending = false, batches = [], activeBatch = null, batchTimer = null, batchBusy = false, batchRequest = 0;
  const draftKey = 'ttmedia.broadcast.authorization-draft';
  const allowed = () => !redirecting;
  const sendReady = () => permissionReady(permissionResult,permissionPending,$('cm').value,permissionCm,redirecting)
    && permissionResult.feature_enabled === true && $('cm').value === actor;
  const groupMode = () => purpose !== 'support';
  function syncAssistantScope() {
    const cmId = $('cm').value === 'all' || !$('cm').value ? actor : $('cm').value;
    const cm = config.cms.find(person => person.id === cmId);
    const context = {cm_id: cmId, cm_name: cm?.name, audience: 'all', creator_ids: null};
    const initial = document.getElementById('oa-page-context');
    if (initial?.textContent.trim()) initial.textContent = JSON.stringify(context);
    document.dispatchEvent(new CustomEvent('ttmedia:assistant', {detail: {
      context,
    }}));
  }
  const rows = () => recipients(entries, routes, groups, purpose);
  const plan = () => groupMode() ? groupPlan(groups,selectedGroups,$('mention').checked) : buildPlan(rows(), $('mention').checked, config.purposes[purpose]);
  function loading(text) {
    const state = node('span','','ui-loading'), spinner = node('span','','ui-spinner');
    spinner.setAttribute('aria-hidden','true'); state.append(spinner,document.createTextNode(text)); return state;
  }
  function notice(text, pending = false) {
    $('notice').replaceChildren(pending ? loading(text) : text);
    $('notice').setAttribute('aria-busy',String(pending));
  }
  const selections = () => groupMode() ? [...selectedGroups].map(chat_id => ({chat_id})) : rows().filter(row => row.selected && row.person).map(row => ({contact_id:row.person.id,chat_id:row.chatId}));
  const canSaveTargets = () => !dirty && plan().groups.length > 0 && plan().issues.length === 0;
  const selectionState = () => listState($('cm').value,purpose,$('mention').checked,selections());
  const unsavedTargets = () => currentList ? dirty || selectionState() !== savedState : groupMode() ? Boolean(selectedGroups.size) : Boolean(entries.length || $('input').value.trim());
  function renderLibrary(current = plan()) {
    const canSave = !dirty && current.groups.length > 0 && current.issues.length === 0, changed = unsavedTargets();
    $('list-library').textContent = lists.length ? `名单库 · ${lists.length}` : '打开名单库';
    $('list-library').disabled = busy;
    $('list-new').textContent = currentList ? '另存为新名单' : '保存当前名单';
    $('list-new').disabled = busy;
    $('list-current-update').hidden = !currentList;
    $('list-current-update').disabled = busy || !canSave || !changed;
    $('list-info').textContent = currentList ? `当前：${currentList.name}${changed ? ' · 有未保存的调整' : ''}`
      : canSave ? groupMode() ? `已选 ${current.groups.length} 个群` : `已选 ${current.people.length} 人` : '';
    $('library-items').replaceChildren();
    if (!lists.length) {
      const empty = node('div','','broadcast-library-empty');
      empty.append(node('h4','还没有保存的名单'),node('p','先添加接收对象，再点击“保存当前名单”。','text-secondary mb-0'));
      $('library-items').append(empty);
    }
    for (const list of lists) {
      const item = node('article','',`broadcast-library-item${currentList?.id === list.id ? ' is-current' : ''}`);
      const heading = node('div','','d-flex align-items-start justify-content-between gap-2');
      heading.append(node('h4',list.name,'mb-0'));
      if (currentList?.id === list.id) heading.append(node('span','当前使用','badge bg-blue-lt'));
      const cm = config.cms.find(cm => cm.id === list.cm_id)?.name || (list.cm_id === 'all' ? '全部达人' : '原负责人已失效');
      item.append(heading,node('div',`${config.purposes[list.purpose]} · ${list.target_count} ${list.purpose === 'support' ? '人' : '个群'} · ${cm}`,'text-secondary small mt-2'));
      if (list.notes) item.append(node('p',list.notes,'small text-secondary'));
      const actions = node('div','','broadcast-list-actions');
      for (const [label,style,action] of [
        ['使用','btn-outline-primary',() => unsavedTargets() ? confirmListAction('load',list) : void loadList(list)],
        ['编辑','btn-outline-secondary',() => editList(list)],
        ['删除','btn-ghost-danger',() => confirmListAction('delete',list)],
      ]) {
        const button = node('button',label,`btn btn-sm ${style}`); button.type = 'button'; button.disabled = busy;
        if (label === '使用') button.dataset.listId = list.id;
        button.setAttribute('aria-label',`${label}名单 ${list.name}`); button.addEventListener('click',action); actions.append(button);
      }
      item.append(actions); $('library-items').append(item);
    }
  }
  function renderGroupPicker() {
    const focused = document.activeElement?.dataset.chatId;
    const query = $('group-search').value.trim().toLocaleLowerCase();
    const visible = groups.filter(group => group.name.toLocaleLowerCase().includes(query));
    $('group-items').replaceChildren();
    for (const group of visible) {
      const label = node('label','','broadcast-group-item');
      const check = node('input'); check.type = 'checkbox'; check.className = 'form-check-input';
      check.dataset.chatId = group.id; check.checked = selectedGroups.has(group.id); check.disabled = busy;
      check.setAttribute('aria-label',`选择群 ${group.name}`);
      check.addEventListener('change',() => {
        if (check.checked && selectedGroups.size >= 500) { check.checked = false; notice('最多选择 500 个群。'); return; }
        if (check.checked) selectedGroups.add(group.id); else selectedGroups.delete(group.id);
        render();
      });
      label.append(check,node('span',group.name)); $('group-items').append(label);
    }
    if (!visible.length) {
      const empty = node('div',!$('cm').value ? '选择 CM/AM 后查看群聊' : query ? '没有匹配的群' : '暂无已分类的群','broadcast-group-empty text-secondary');
      if (loadingGroups && !groups.length) empty.replaceChildren(loading('加载群聊'));
      $('group-items').append(empty);
    }
    $('group-items').setAttribute('aria-busy',String(loadingGroups));
    $('group-count').textContent = `已选 ${selectedGroups.size} / ${groups.length}`;
    $('group-all').checked = visible.length > 0 && visible.every(group => selectedGroups.has(group.id));
    $('group-all').indeterminate = visible.some(group => selectedGroups.has(group.id)) && !$('group-all').checked;
    $('group-all').disabled = busy || !visible.length;
    if (focused) [...$('group-items').querySelectorAll('input')].find(input => input.dataset.chatId === focused)?.focus();
  }
  function renderTemplates() {
    $('template-select').replaceChildren(new Option('选择消息模板',''));
    for (const template of templates) $('template-select').add(new Option(template.name,template.id));
    $('template-select').value = currentTemplateId;
    $('template-edit').disabled = busy || !templates.some(template => template.id === currentTemplateId);
  }
  function render() {
    const current = plan(), all = rows();
    $('people-input').hidden = groupMode(); $('group-picker').hidden = !groupMode();
    $('cm-label').textContent = groupMode() ? 'CM/AM' : '达人范围';
    $('mention-label').textContent = groupMode() ? '@ 全体成员' : '@ 所选达人';
    $('matches').hidden = groupMode() || !entries.length;
    if (groupMode()) renderGroupPicker();
    $('count').textContent = `已选 ${current.people.length} 人 · 待处理 ${current.issues.length} 项`;
    $('route-title').textContent = `目标${config.purposes[purpose]}`;
    $('rows').replaceChildren();
    for (const row of all) {
      const tr = node('tr'), selection = node('td'), identity = node('td', '', 'broadcast-identity'), route = node('td', '', 'broadcast-route');
      const check = node('input'); check.type = 'checkbox'; check.className = 'form-check-input'; check.checked = row.selected;
      check.setAttribute('aria-label', `选择 ${row.person?.name || row.entries[0].inputs[0]}`);
      check.addEventListener('change', () => { for (const entry of row.entries) entry.selected = check.checked; render(); });
      selection.append(check);
      if (row.person) {
        identity.append(node('strong', row.person.name), node('div', row.person.handles.map(handle => `@${handle.replace(/^@/,'')}`).join(' · '), 'text-secondary small'));
        const count = row.entries.flatMap(entry => entry.inputs).length;
        if (count > 1) identity.append(node('div', `${count} 项输入已合并`, 'text-secondary small mt-1'));
        if (row.entries.some(entry => entry.approximate || entry.candidates.length > 1)) {
          const change = node('button', '改选达人', 'btn btn-sm btn-link p-0 mt-1'); change.type = 'button';
          change.setAttribute('aria-label', `为 ${row.person.name} 改选达人`);
          change.addEventListener('click', () => {
            for (const entry of row.entries) if (entry.approximate || entry.candidates.length > 1) entry.person = null;
            routes.delete(row.key); render();
          });
          identity.append(change);
        }
      } else {
        const entry = row.entries[0]; identity.append(node('div', entry.inputs[0]));
        if (entry.candidates.length) {
          const select = node('select', '', 'form-select form-select-sm mt-2'); select.setAttribute('aria-label', `为 ${entry.inputs[0]} 选择达人`);
          select.append(new Option(entry.approximate ? '近似候选，需选择' : '同名或同账号，需选择', ''));
          for (const person of entry.candidates) {
            const group = groups.find(group => person.groups.includes(group.id));
            select.append(new Option([person.name, person.handles[0] ? `@${person.handles[0].replace(/^@/,'')}` : '', group?.name || '暂无可用群'].filter(Boolean).join(' · '), person.id));
          }
          select.addEventListener('change', () => { entry.person = entry.candidates.find(person => person.id === select.value) || null; render(); });
          identity.append(select);
        }
      }
      if (row.options.length > 1 || (row.options.length && row.entries[0].reason)) {
        const select = node('select', '', 'form-select form-select-sm'); select.setAttribute('aria-label', `为 ${row.person.name} 选择${config.purposes[purpose]}`);
        select.append(new Option(`选择一个${config.purposes[purpose]}`, ''));
        for (const group of row.options) select.append(new Option(group.name, group.id));
        select.value = row.chatId; select.addEventListener('change', () => {
          routes.set(row.key, select.value);
          if (select.value && fixedTargets) for (const target of fixedTargets) if (target.contact_id === row.person.id) target.chat_id = select.value;
          if (select.value) for (const entry of row.entries) { entry.reason = ''; entry.selected = true; }
          render();
        }); route.append(select);
      } else if (row.options.length === 1) route.append(node('div', row.options[0].name));
      const issue = current.issues.find(item => item.key === row.key);
      const savedIssue = row.entries[0].reason;
      route.append(node('div', savedIssue ? `已跳过 · ${savedIssue}` : !row.selected ? '已移除' : issue ? issue.reason : '路由已匹配', savedIssue || issue ? 'text-orange small mt-1' : !row.selected ? 'text-secondary small mt-1' : 'text-success small mt-1'));
      tr.append(selection, identity, route); $('rows').append(tr);
    }
    $('exclude').hidden = !current.issues.length;
    $('summary').textContent = groupMode() ? `已选 ${current.groups.length} 个群` : entries.length ? `已选 ${current.people.length} 人 · ${current.groups.length} 个群` : '尚未选择对象';
    $('preview').disabled = busy || dirty || !current.groups.length || Boolean(current.issues.length) || !$('message').value.trim();
    $('download').disabled = busy || dirty || !current.people.length;
    $('message-preview').textContent = $('message').value || '输入消息后预览';
    $('input').readOnly = Boolean(fixedIds);
    $('match').textContent = fixedIds ? '重新核验' : '匹配名单';
    $('match').disabled = busy || Boolean(fixedIds && !fixedIds.length);
    $('mention-preview').textContent = groupMode() ? $('mention').checked ? '@全体成员' : '' : current.groups[0]?.mentions.map(person => `@${person.name}`).join(' ') || '';
    for (const button of buttons) {
      const active = button.dataset.purpose === purpose;
      button.className = `btn ${active ? 'btn-primary' : 'btn-outline-secondary'}`; button.setAttribute('aria-pressed', String(active));
    }
    renderLibrary(current);
    renderTemplates();
    syncGate();
  }
  function syncGate() {
    $('workspace').disabled = busy || redirecting;
    $('editors').disabled = busy || redirecting;
    $('cm').disabled = redirecting || !canSelectCm;
    $('permission-retry').disabled = permissionPending || redirecting;
    $('permission-authorize').disabled = permissionPending || redirecting;
    $('send').disabled = busy || sending || !sendReady();
    $('send').textContent = frozenPreview ? `确认发送 ${frozenPreview.groups.length} 个群` : '准备发送';
    if (!sending) $('confirm-state').textContent = sendReady() ? frozenPreview ? `以 ${frozenPreview.account_name || permissionResult.account_name} 本人身份发送 · 发送前逐群检查权限` : '尚未发送'
      : permissionPending ? '正在核对发送授权，仍可预览' : $('cm').value !== actor ? '其他人员范围仅可预览' : permissionResult?.feature_enabled === false ? '演示模式 · 发送权限未开通' : '预览可用，发送前需补充本人授权';
  }
  function lock(value) { busy = value; render(); }
  function beginAuthorization(value) {
    if (redirecting) return;
    try {
      const url = authorizationUrl(value,$('app').dataset.actor,location.origin);
      const selected = !groupMode() && !dirty && entries.length ? rows().filter(row => row.selected && row.person) : null;
      const raw = JSON.stringify({version:1,actor_id:$('app').dataset.actor,created_at:Date.now(),
        cm_id:$('cm').value,purpose,mention:$('mention').checked,raw:$('input').value,message:$('message').value,
        fixed_ids:selected ? selected.map(row => row.person.id) : fixedIds,
        fixed_targets:selected ? selected.map(row => ({contact_id:row.person.id,chat_id:row.chatId})) : fixedTargets,
        selected_groups:[...selectedGroups]});
      parseDraft(raw,actor,Date.now(),canSelectCm);
      sessionStorage.setItem(draftKey,raw);
      if (sessionStorage.getItem(draftKey) !== raw) throw new Error('无法保存草稿');
      redirecting = true; syncGate(); location.assign(url);
    } catch (error) {
      redirecting = false; syncGate();
      $('permission-message').textContent = `${error.message}。已暂停跳转，请保留当前页面并重新检查。`;
    }
  }
  async function api(action, data) {
    const business = action !== 'permissions', request = permissionCheck, cm = $('cm').value;
    if (business && !allowed()) throw new Error('请先完成当前账号的授权核对。');
    const control = {'permissions':'permission-retry','match':'match','resolve':'match','groups':'group-refresh',
      'preview':'preview','prepare':'send','send':'send','lists/save':'list-save','templates/save':'template-save',
      'lists/delete':'list-action-proceed','templates/delete':'list-action-proceed'}[action];
    const indicator = action === 'lists/load' ? $('list-action-dialog').open ? $('list-action-proceed')
      : [...$('library-items').querySelectorAll('[data-list-id]')].find(button => button.dataset.listId === data.id)
      : control ? $(control) : null;
    indicator?.setAttribute('aria-busy','true');
    try {
      const response = await fetch(`/creators/broadcast/${action}`, {method:'POST', credentials:'same-origin', signal:AbortSignal.timeout(30000), headers:{'Content-Type':'application/json','X-CSRF-Token':$('app').dataset.csrf}, body:JSON.stringify(data)});
      const result = await response.json().catch(() => { throw new Error('服务暂时不可用，请稍后重试。'); });
      if (business && (cm !== $('cm').value || !allowed())) throw new Error('账号已变化，请重新核验。');
      if (!response.ok) {
        if (business && response.status === 403) {
          permissionResult = null; syncGate();
          $('permission-status').textContent = '需要重新核对'; $('permission-status').className = 'badge bg-yellow-lt ms-2';
          $('permission-message').textContent = result.error || '当前操作权限已变化，请重新检查。';
          $('permission-authorize').hidden = true;
          if (result.authorization_url) {
            try { authorizationUrl(result.authorization_url,actor,location.origin); permissionResult = {status:'needs_authorization',is_self:true,authorization_url:result.authorization_url}; $('permission-authorize').hidden = false; }
            catch { /* Never offer OAuth for a different account or an unrelated 403. */ }
          }
        }
        throw new Error(result.error || '请求失败，请稍后重试。');
      }
      const readAction = ['match','resolve','groups','preview','prepare','lists/load'].includes(action);
      const groupRead = action === 'groups' || result?.mode === 'groups';
      if (!result || (readAction && (!Array.isArray(result.groups) || !Array.isArray(result[action === 'preview' || action === 'prepare' || groupRead ? 'issues' : 'entries'])))
          || (action === 'permissions' ? !['ready','needs_authorization','unavailable'].includes(result.status)
            : action === 'send' ? !result.batch?.id : !readAction && !Array.isArray(action.startsWith('templates/') ? result.templates : result.lists))) throw new Error('返回格式异常，请刷新后重试。');
      return result;
    } catch (error) {
      if (['TimeoutError','AbortError'].includes(error.name)) throw new Error('请求超时，请重新尝试。');
      if (error instanceof TypeError) throw new Error('网络连接失败，请稍后重试。');
      throw error;
    } finally {
      if (business || request === permissionCheck) indicator?.setAttribute('aria-busy','false');
    }
  }
  async function checkPermissions(force = false) {
    const cm = $('cm').value;
    if (!force && permissionCm === cm && !permissionPending) return allowed();
    if (permissionPending && permissionCm === cm) return false;
    if (redirecting) return false;
    const request = ++permissionCheck; permissionCm = cm; permissionPending = true; permissionResult = null;
    $('permission-bar').setAttribute('aria-busy','true'); $('permission-retry').disabled = true;
    $('permission-account').textContent = `发送账号：${config.cms.find(item => item.id === actor)?.name || '本人'}`;
    $('permission-status').replaceChildren(loading('正在核对授权')); $('permission-status').className = 'badge bg-secondary-lt ms-2';
    $('permission-status').setAttribute('aria-busy','true');
    $('permission-message').textContent = ''; $('permission-authorize').hidden = true; syncGate();
    try {
      const result = await api('permissions',{cm_id:cm});
      if (request !== permissionCheck || cm !== $('cm').value) return false;
      permissionResult = result;
      $('permission-account').textContent = `发送账号：${result.account_name || '本人'}`;
      $('permission-status').textContent = result.status === 'ready' && result.feature_enabled ? '本人发送已就绪' : result.feature_enabled === false ? '演示模式' : result.status === 'needs_authorization' ? '待补发送授权' : '暂时无法核对';
      $('permission-status').className = `badge ms-2 ${result.status === 'ready' && result.feature_enabled ? 'bg-green-lt' : 'bg-yellow-lt'}`;
      $('permission-message').textContent = result.status === 'ready' && result.feature_enabled ? '' : '可编辑、保存和预览；实际发送需要本人授权及发送权限。';
      if (result.status === 'needs_authorization' && result.authorization_url) {
        if (result.is_self) $('permission-authorize').hidden = false;
      }
    } catch (error) {
      if (request !== permissionCheck) return false;
      $('permission-status').textContent = '暂时无法核对'; $('permission-status').className = 'badge bg-yellow-lt ms-2';
      $('permission-message').textContent = error.message;
    } finally {
      if (request === permissionCheck) {
        permissionPending = false; $('permission-bar').setAttribute('aria-busy','false');
        $('permission-status').setAttribute('aria-busy','false'); syncGate();
      }
    }
    if (request !== permissionCheck || !allowed()) return false;
    if (pendingRestore) {
      pendingRestore = false;
      if (groupMode()) await fetchGroups(true);
      else if (fixedIds?.length || (!fixedIds && $('input').value.trim())) await match(true);
    }
    return allowed();
  }
  $('permission-retry').addEventListener('click',() => void checkPermissions(true));
  $('permission-authorize').addEventListener('click',() => { if (permissionResult?.is_self && permissionResult.authorization_url) beginAuthorization(permissionResult.authorization_url); });
  document.addEventListener('visibilitychange',() => {
    if (!document.hidden) void checkPermissions(true);
  });
  window.addEventListener('pageshow',event => { if (event.persisted) void checkPermissions(true); });
  function applyTargets(result) {
    entries = result.entries || []; groups = result.groups; routes = new Map((result.routes || []).map(route => [route.contact_id,route.chat_id]));
    selectedGroups = new Set(result.selected_chat_ids || []); dirty = false;
  }
  function configureScope(value = $('cm').value) {
    $('cm').replaceChildren();
    if (canSelectCm) $('cm').add(new Option(groupMode() ? '选择 CM/AM' : '全部已入库达人',groupMode() ? '' : 'all'));
    for (const cm of config.cms.filter(cm => canSelectCm || cm.id === actor)) $('cm').add(new Option(groupMode() ? cm.name : `${cm.name} 负责的达人`,cm.id));
    if (!canSelectCm) value = actor;
    if (!groupMode() && !value) value = 'all';
    if (groupMode() && value === 'all') value = config.cms.some(cm => cm.id === $('app').dataset.actor) ? $('app').dataset.actor : '';
    if (canSelectCm && value && ![...$('cm').options].some(option => option.value === value)) $('cm').add(new Option('授权已失效',value));
    $('cm').value = value;
    syncAssistantScope();
  }
  async function fetchGroups(preserve = false) {
    if (busy || !allowed()) return;
    const old = preserve ? new Set(selectedGroups) : new Set();
    if (!preserve) { groups = []; selectedGroups = new Set(); }
    dirty = true;
    if (!$('cm').value || $('cm').value === 'all') { render(); return; }
    loadingGroups = true; lock(true); notice('');
    try {
      const result = await api('groups',{cm_id:$('cm').value,purpose}); applyTargets(result);
      selectedGroups = new Set(groups.filter(group => old.has(group.id)).map(group => group.id));
      if (selectedGroups.size !== old.size) notice('已跳过失效群，请检查当前选择。');
    } catch (error) { notice(error.message); }
    finally { loadingGroups = false; lock(false); }
  }
  async function match(preserve = false) {
    if (busy || !allowed()) return;
    if (fixedIds && !fixedIds.length) return;
    const old = preserve || fixedIds ? new Map(entries.map(entry => [entry.key,{id:entry.person?.id,selected:entry.selected}])) : new Map();
    const incompleteRoutes = fixedTargets?.some(target => !target.chat_id);
    lock(true); notice('正在匹配',true);
    try {
      const result = await api(fixedIds ? 'resolve' : 'match',
        {cm_id:$('cm').value,purpose,raw:$('input').value,contact_ids:fixedIds,...(fixedTargets && !incompleteRoutes ? {selections:fixedTargets} : {})});
      applyTargets(result);
      if (incompleteRoutes) for (const target of fixedTargets) {
        const entry = entries.find(entry => entry.person?.id === target.contact_id);
        const valid = entry && groups.some(group => group.id === target.chat_id && entry.person.groups.includes(group.id));
        routes.set(target.contact_id,valid ? target.chat_id : '');
        if (entry && target.chat_id && !valid) { entry.reason = '保存的目标群已变化，请重新选择'; entry.selected = false; }
      }
      for (const entry of entries) {
        const saved = old.get(entry.key);
        if (saved) { entry.selected = !entry.reason && saved.selected; if (saved.id) entry.person = entry.candidates.find(person => person.id === saved.id) || entry.person; }
      }
      if (fixedIds) $('input').value = entries.map(entry => entry.inputs[0]).join('\n');
      notice(result.duplicate_inputs ? `已合并 ${result.duplicate_inputs} 项重复输入。` : '匹配完成。');
    } catch (error) { dirty = true; notice(error.message || '匹配失败，请稍后重试。'); }
    finally { lock(false); }
  }
  $('match').addEventListener('click', () => void match());
  $('input').addEventListener('input', () => { dirty = true; notice(entries.length ? '名单已修改，请重新匹配。' : ''); render(); });
  for (const id of ['message','mention']) $(id).addEventListener('input', render);
  $('cm').addEventListener('change', async () => {
    syncAssistantScope();
    pendingRestore = false;
    entries = []; groups = []; selectedGroups = new Set(); routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; $('input').value = ''; dirty = true; notice('');
    render();
    if (await checkPermissions()) { if (groupMode()) await fetchGroups(); else render(); }
  });
  for (const button of buttons) button.addEventListener('click', async () => {
    if (purpose === button.dataset.purpose || busy || !allowed()) return;
    pendingRestore = false;
    const wasGroupMode = groupMode();
    purpose = button.dataset.purpose; entries = []; groups = []; selectedGroups = new Set(); routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; dirty = true;
    if (wasGroupMode !== groupMode()) $('mention').checked = !groupMode();
    $('confirm').close(); $('input').value = ''; $('group-search').value = ''; notice(''); configureScope();
    render();
    if (await checkPermissions()) { if (groupMode()) await fetchGroups(); else render(); }
  });
  $('group-refresh').addEventListener('click',() => void fetchGroups(true));
  $('group-search').addEventListener('input',renderGroupPicker);
  $('group-all').addEventListener('change',() => {
    const query = $('group-search').value.trim().toLocaleLowerCase(), visible = groups.filter(group => group.name.toLocaleLowerCase().includes(query));
    if ($('group-all').checked) {
      if (new Set([...selectedGroups,...visible.map(group => group.id)]).size > 500) { notice('最多选择 500 个群，请缩小搜索范围。'); render(); return; }
      for (const group of visible) selectedGroups.add(group.id);
    } else for (const group of visible) selectedGroups.delete(group.id);
    render();
  });
  $('clear').addEventListener('click', () => { $('input').value = ''; entries = []; groups = []; routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; dirty = true; notice(''); render(); });
  $('exclude').addEventListener('click', () => {
    const blocked = new Set(plan().issues.map(item => item.key));
    for (const row of rows()) if (blocked.has(row.key)) for (const entry of row.entries) entry.selected = false;
    notice('待处理项已移除。'); render();
  });
  $('download').addEventListener('click', () => {
    const selected = rows().filter(row => row.selected && row.person);
    const text = selected.map(row => {
      const handles = row.person.handles.map(handle => handle.normalize('NFKC').replace(/^@/,'').toLowerCase());
      const matched = row.entries.map(entry => entry.key.slice(entry.key.indexOf(':') + 1)).find(key => handles.includes(key));
      return matched ? `@${matched}` : row.person.handles[0] ? `@${row.person.handles[0].replace(/^@/,'')}` : row.person.name;
    }).join('\n');
    const url = URL.createObjectURL(new Blob([text + '\n'], {type:'text/plain;charset=utf-8'}));
    const link = node('a'); link.href = url; link.download = `broadcast-${purpose}-list.txt`; link.click(); URL.revokeObjectURL(url);
    notice('已下载文本名单；再次使用时仍需匹配，群选择不会永久保存。');
  });
  $('preview').addEventListener('click', async () => {
    const current = plan();
    if (busy || !allowed() || dirty || current.issues.length || !current.groups.length || !$('message').value.trim()) return;
    lock(true); notice('核对路由',true);
    try {
      const result = await api('preview',{cm_id:$('cm').value,purpose,mention:$('mention').checked,message:$('message').value,selections:selections()});
      if (result.issues.length) { dirty = true; throw new Error(groupMode() ? '群聊已变化，请点击“刷新”重新核验。' : `${result.issues.length} 项路由已变化，请重新匹配。`); }
      frozenPreview = null; sendRequestId = null; $('confirm-error').textContent = '';
      renderPreview(result);
      notice(''); $('confirm').showModal();
    } catch (error) { notice(error.message || '预览失败，请稍后重试。'); }
    finally { lock(false); }
  });
  function renderPreview(result) {
    $('confirm-count').textContent = `${result.groups.length} 个群 · ${result.groups.length} 条消息`;
    $('confirm-scope').textContent = result.account_name ? `发送账号：${result.account_name}` : $('cm').selectedOptions[0].textContent;
    $('confirm-message').textContent = result.message; $('confirm-groups').replaceChildren();
    for (const group of result.groups) {
      const item = node('div','','broadcast-target');
      item.append(node('strong',group.name),node('div',group.mention_all ? '@全体成员'
        : group.mentions?.length ? group.mentions.map(person => `@${person.name}`).join(' ') : '不 @ 成员','text-primary small mt-1'));
      $('confirm-groups').append(item);
    }
    for (const issue of result.issues || []) $('confirm-groups').append(node('div',`已跳过：${issue.name || issue.chat_name || ''} ${issue.reason || '群聊不可发送'}`,'broadcast-target small text-orange'));
    syncGate();
  }
  $('send').addEventListener('click',async () => {
    if (sending || busy || !sendReady()) return;
    sending = true; $('confirm-error').textContent = ''; syncGate();
    try {
      if (!frozenPreview) {
        const result = await api('prepare',{cm_id:$('cm').value,purpose,mention:$('mention').checked,message:$('message').value,selections:selections()});
        if (!result.preview_id || !result.groups.length) throw new Error('没有可发送的群，请返回修改。');
        frozenPreview = result; sendRequestId = crypto.randomUUID(); renderPreview(result);
      } else {
        const result = await api('send',{preview_id:frozenPreview.preview_id,request_id:sendRequestId});
        $('confirm').close(); frozenPreview = null; sendRequestId = null;
        notice(result.deduplicated ? '已显示已有发送任务，未重复创建。' : '已加入发送队列。');
        upsertBatch(result.batch); await openBatch(result.batch.id);
      }
    } catch (error) {
      $('confirm-error').textContent = error.message;
      if (!sendReady()) $('confirm-state').textContent = '发送已暂停，预览和消息会保留';
    } finally { sending = false; syncGate(); }
  });
  const statusLabels = {pending:'排队中',sending:'发送中',succeeded:'已发送',failed:'失败',unknown:'状态未知',cancelled:'已停止',partial:'部分完成'};
  const statusStyle = status => status === 'succeeded' ? 'bg-green-lt' : ['failed','unknown'].includes(status) ? 'bg-yellow-lt' : 'bg-secondary-lt';
  const batchActive = batch => ['pending','sending'].includes(batch.status) || batch.pending > 0 || batch.sending > 0;
  const progress = batch => [`${batch.total} 个群`, batch.succeeded ? `已发 ${batch.succeeded}` : '', batch.failed ? `失败 ${batch.failed}` : '',
    batch.unknown ? `待核对 ${batch.unknown}` : '', batch.cancelled ? `已停 ${batch.cancelled}` : ''].filter(Boolean).join(' · ');
  function upsertBatch(batch) {
    batches = [batch,...batches.filter(item => item.id !== batch.id)].slice(0,20);
    batches.sort((a,b) => String(b.created_at).localeCompare(String(a.created_at))); renderHistory();
  }
  function renderHistory() {
    $('history').replaceChildren();
    if (!batches.length) $('history').append(node('div','暂无发送记录','text-secondary small'));
    for (const batch of batches) {
      const button = node('button','','broadcast-history-row'); button.type = 'button';
      const summary = node('span'), date = new Date(batch.created_at);
      summary.append(node('span',`${batch.account_name || '本人发送'} · ${Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}`),node('span',progress(batch),'small text-secondary mt-1'));
      button.append(summary,node('span',statusLabels[batch.status] || batch.status,`badge ${statusStyle(batch.status)}`));
      button.addEventListener('click',() => void openBatch(batch.id)); $('history').append(button);
    }
  }
  async function batchApi(path = '', action = '', data = {}) {
    let response;
    try {
      response = await fetch(`/creators/broadcast/batches${path}`,{method:action ? 'POST' : 'GET',credentials:'same-origin',
        signal:AbortSignal.timeout(30000),headers:{'Content-Type':'application/json','X-CSRF-Token':$('app').dataset.csrf},...(action ? {body:JSON.stringify(data)} : {})});
    } catch { throw new Error('网络或请求超时，请重新查询结果。'); }
    const result = await response.json().catch(() => { throw new Error('暂时无法读取发送记录。'); });
    if (!response.ok) throw new Error(result.error || '操作失败，请重试。');
    if (!result || (!path ? !Array.isArray(result.batches) : !result.batch?.id)) throw new Error('发送记录格式异常。');
    return result;
  }
  async function refreshHistory() {
    if ($('history-refresh').disabled) return;
    $('history-refresh').disabled = true; $('history-refresh').setAttribute('aria-busy','true'); $('history-error').textContent = '';
    try { batches = (await batchApi()).batches; renderHistory(); }
    catch (error) { $('history-error').textContent = error.message; }
    finally { $('history-refresh').disabled = false; $('history-refresh').setAttribute('aria-busy','false'); }
  }
  function renderBatch(result) {
    const batch = result.batch; activeBatch = batch; upsertBatch(batch);
    $('batch-summary').textContent = `${statusLabels[batch.status] || batch.status} · ${progress(batch)}`;
    $('batch-message-panel').hidden = !result.message; $('batch-message').textContent = result.message || '';
    $('batch-targets').replaceChildren();
    for (const target of result.targets || []) {
      const item = node('div','','broadcast-target broadcast-result'), info = node('div');
      if (typeof target.chat_id === 'string' && /^oc_[A-Za-z0-9_-]{1,128}$/.test(target.chat_id)) {
        const link = node('a',target.chat_name,'fw-semibold');
        link.href = `https://applink.larksuite.com/client/chat/open?openChatId=${encodeURIComponent(target.chat_id)}`;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.title = '在 Lark 中打开此群'; info.append(link);
      } else info.append(node('strong',target.chat_name));
      if (target.error_message) info.append(node('div',target.error_message,'small text-secondary mt-1'));
      item.append(info,node('span',statusLabels[target.status] || target.status,`badge ${statusStyle(target.status)}`)); $('batch-targets').append(item);
    }
    $('batch-cancel').hidden = !(batch.pending > 0 || batch.sending > 0);
    $('batch-retry').hidden = batch.can_retry !== true;
    $('batch-cancel').disabled = $('batch-retry').disabled = batchBusy;
    $('batch-note').textContent = batch.unknown > 0 ? '状态未知项不会重发，请先在 Lark 核对。' : batch.pending > 0 || batch.sending > 0 ? '停止尚未发送的消息，已发出的不撤回。' : batch.can_retry ? '重试只发送失败项，不重发成功项。' : '';
  }
  async function loadBatch(id) {
    const request = ++batchRequest;
    const result = await batchApi(`/${encodeURIComponent(id)}`);
    if (request !== batchRequest || !$('batch-dialog').open || activeBatch?.id !== id) return;
    renderBatch(result); $('batch-error').textContent = '';
    if (batchActive(result.batch)) batchTimer = setTimeout(() => void pollBatch(id),3000);
  }
  async function pollBatch(id) {
    batchTimer = null;
    if (document.hidden || !$('batch-dialog').open || activeBatch?.id !== id || batchBusy) return;
    try { await loadBatch(id); }
    catch (error) { $('batch-error').textContent = `${error.message} 点击“刷新”重查。`; }
  }
  async function openBatch(id) {
    clearTimeout(batchTimer); batchTimer = null; activeBatch = {id};
    $('batch-summary').replaceChildren(loading('读取发送结果')); $('batch-targets').replaceChildren();
    $('batch-message-panel').hidden = true; $('batch-message-panel').open = false; $('batch-error').textContent = ''; $('batch-note').textContent = '';
    $('batch-cancel').hidden = $('batch-retry').hidden = true;
    if (!$('batch-dialog').open) $('batch-dialog').showModal();
    try { await loadBatch(id); } catch (error) { $('batch-error').textContent = error.message; }
  }
  $('history-refresh').addEventListener('click',async () => { await refreshHistory(); if ($('batch-dialog').open && activeBatch?.id) await openBatch(activeBatch.id); });
  $('batch-refresh').addEventListener('click',async () => {
    if (batchBusy || !activeBatch?.id) return;
    $('batch-refresh').disabled = true; $('batch-refresh').setAttribute('aria-busy','true');
    try { await openBatch(activeBatch.id); }
    finally { $('batch-refresh').disabled = false; $('batch-refresh').setAttribute('aria-busy','false'); }
  });
  $('batch-close').addEventListener('click',() => $('batch-dialog').close());
  $('batch-dialog').addEventListener('close',() => { clearTimeout(batchTimer); batchTimer = null; activeBatch = null; ++batchRequest; });
  for (const action of ['cancel','retry']) $(`batch-${action}`).addEventListener('click',async () => {
    if (batchBusy || !activeBatch?.id) return;
    const id = activeBatch.id; batchBusy = true; ++batchRequest; clearTimeout(batchTimer); batchTimer = null;
    $('batch-cancel').disabled = $('batch-retry').disabled = true; $(`batch-${action}`).setAttribute('aria-busy','true'); $('batch-error').textContent = '';
    try {
      const result = await batchApi(`/${encodeURIComponent(id)}/${action}`,action);
      upsertBatch(result.batch);
    } catch (error) { $('batch-error').textContent = error.message; }
    finally {
      batchBusy = false; $(`batch-${action}`).setAttribute('aria-busy','false');
      if ($('batch-dialog').open && activeBatch?.id === id) {
        try { await loadBatch(id); } catch (error) { $('batch-error').textContent = error.message; }
        $('batch-cancel').disabled = $('batch-retry').disabled = false;
      }
    }
  });
  document.addEventListener('visibilitychange',() => {
    clearTimeout(batchTimer); batchTimer = null;
    if (!document.hidden && $('batch-dialog').open && activeBatch?.id) void pollBatch(activeBatch.id);
  });
  async function loadList(list) {
    if (busy || !allowed()) return;
    lock(true); $('library-error').textContent = ''; notice('加载名单',true);
    try {
      const result = await api('lists/load',{id:list.id});
      lists = lists.map(list => list.id === result.list.id ? result.list : list);
      currentList = result.list; purpose = result.purpose;
      fixedIds = groupMode() ? null : result.entries.map(entry => entry.key.replace(/^contact:/,'')); fixedTargets = groupMode() ? null : result.list.targets;
      configureScope(groupMode() && result.selected_cm_id === 'all' ? '' : result.selected_cm_id); $('mention').checked = result.list.mention;
      $('input').value = (result.entries || []).map(entry => entry.inputs[0]).join('\n'); $('group-search').value = ''; applyTargets(result);
      savedState = selectionState(); pendingAction = null; $('library-dialog').close(); $('list-action-dialog').close();
      notice([result.issues.length ? `已跳过 ${result.issues.length} 项：${result.issues[0].reason}` : '',result.notice || '名单已载入。'].filter(Boolean).join(' '));
      await checkPermissions(true);
    } catch (error) {
      $('library-error').textContent = error.message; $('list-action-error').textContent = error.message; notice(error.message);
    }
    finally {
      lock(false);
      if ($('list-action-dialog').open) $('list-action-cancel').focus();
      else if ($('library-dialog').open) $('library-close').focus();
    }
  }
  function editList(list, mode = list ? 'edit' : 'create') {
    if (busy || !allowed()) return;
    if (mode === 'update' && (!currentList || list?.id !== currentList.id)) return;
    editingList = list; editMode = mode; $('library-error').textContent = '';
    $('list-title').textContent = mode === 'edit' ? '编辑名单信息' : mode === 'update' ? '更新当前名单' : '保存为新名单';
    $('list-name').value = list?.name || ''; $('list-notes').value = list?.notes || '';
    initialMetadata = JSON.stringify([$('list-name').value,$('list-notes').value]);
    $('list-save').textContent = mode === 'edit' ? '保存修改' : mode === 'update' ? '确认更新名单' : '创建名单';
    $('list-error').textContent = '';
    $('list-description').textContent = mode === 'edit' ? '修改名称和备注，不改变接收对象。'
      : `${groupMode() ? '' : `${plan().people.length} 人 · `}${plan().groups.length} 个群 · ${config.purposes[purpose]}${$('mention').checked ? groupMode() ? ' · @全体成员' : ' · @所选达人' : ''}`;
    $('list-dialog').showModal(); $('list-name').focus();
  }
  function confirmListAction(kind, list = null) {
    if (busy || !allowed()) return;
    pendingAction = {kind,list}; $('list-action-error').textContent = '';
    if (kind !== 'discard') $('library-error').textContent = '';
    $('list-action-title').textContent = kind === 'delete' ? '删除这份名单？' : kind === 'template-delete' ? '删除这个模板？' : kind === 'load' ? '替换当前接收对象？' : kind === 'template-use' ? '替换当前消息？' : '放弃未保存的修改？';
    $('list-action-name').textContent = list?.name || '';
    $('list-action-description').textContent = kind === 'delete' ? '只删除名单，保留当前选择。' : kind === 'template-delete' ? '删除保存的模板，当前消息会保留。'
      : kind === 'load' ? '替换未保存的接收对象，消息正文会保留。' : kind === 'template-use' ? '用模板正文替换当前消息，接收对象不变。' : '关闭后将放弃未保存的修改。';
    $('list-action-proceed').textContent = kind === 'delete' ? '删除名单' : kind === 'template-delete' ? '删除模板' : kind === 'load' ? '使用名单' : kind === 'template-use' ? '使用模板' : '放弃修改';
    $('list-action-proceed').className = `btn ${kind.endsWith('delete') ? 'btn-danger' : 'btn-primary'}`;
    $('list-action-dialog').showModal(); $('list-action-cancel').focus();
  }
  function closeEditor() {
    if (busy || !allowed()) return;
    if (JSON.stringify([$('list-name').value,$('list-notes').value]) !== initialMetadata) confirmListAction('discard');
    else $('list-dialog').close();
  }
  $('list-library').addEventListener('click', () => {
    $('library-error').textContent = ''; renderLibrary(); $('library-dialog').showModal();
  });
  $('library-close').addEventListener('click', () => $('library-dialog').close());
  $('library-dialog').addEventListener('cancel', event => { if (busy || !allowed()) event.preventDefault(); });
  $('list-new').addEventListener('click', () => {
    if (canSaveTargets()) editList(null);
    else { notice(groupMode() ? '先选择群聊。' : '先匹配达人并选好群。'); const control = $(groupMode() ? 'group-search' : 'input'); control.focus(); control.scrollIntoView({block:'center',behavior:'smooth'}); }
  });
  $('list-current-update').addEventListener('click', () => { if (canSaveTargets()) editList(currentList,'update'); });
  $('list-cancel').addEventListener('click',closeEditor);
  $('list-dialog').addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
  $('list-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const metadataOnly = editMode === 'edit';
    if (editMode === 'update' && editingList?.id !== currentList?.id) { $('list-error').textContent = '请先载入要更新的名单。'; return; }
    if (!metadataOnly && !canSaveTargets()) { $('list-error').textContent = '请先完成名单匹配和选群。'; return; }
    const fields = {name:$('list-name').value,notes:$('list-notes').value,metadata_only:metadataOnly,
      ...(!metadataOnly ? {cm_id:$('cm').value,purpose,mention:$('mention').checked,selections:selections()} : {}),
      ...(editingList ? {id:editingList.id,revision:editingList.revision} : {})};
    lock(true); $('list-error').textContent = '';
    try {
      const result = await api('lists/save',fields); lists = result.lists;
      if (currentList?.id === result.list.id) currentList = {...currentList,...result.list};
      if (!metadataOnly) {
        currentList = {...result.list,targets:fields.selections}; fixedIds = groupMode() ? null : fields.selections.map(target => target.contact_id); fixedTargets = groupMode() ? null : fields.selections;
        savedState = selectionState();
      }
      $('list-dialog').close();
      notice(metadataOnly ? '名单信息已保存。' : editMode === 'update' ? '当前名单已更新。' : '新名单已创建。');
    } catch (error) { $('list-error').textContent = error.message; }
    finally {
      lock(false);
      if (!$('list-dialog').open) ($('library-dialog').open ? $('library-close') : $('list-new')).focus();
    }
  });
  function useTemplate(template) {
    currentTemplateId = template.id; $('message').value = template.body;
    pendingAction = null; $('list-action-dialog').close(); render();
  }
  function editTemplate(template = null) {
    if (busy || !allowed()) return;
    editingTemplate = template;
    $('template-title').textContent = template ? '编辑消息模板' : '新建消息模板';
    $('template-name').value = template?.name || ''; $('template-body').value = template?.body ?? $('message').value;
    initialTemplate = JSON.stringify([$('template-name').value,$('template-body').value]);
    $('template-error').textContent = ''; $('template-delete').hidden = !template;
    $('template-dialog').showModal(); $('template-name').focus();
  }
  function closeTemplate() {
    if (busy || !allowed()) return;
    if (JSON.stringify([$('template-name').value,$('template-body').value]) !== initialTemplate) confirmListAction('template-discard');
    else $('template-dialog').close();
  }
  $('template-new').addEventListener('click',() => editTemplate());
  $('template-edit').addEventListener('click',() => editTemplate(templates.find(template => template.id === currentTemplateId)));
  $('template-select').addEventListener('change',() => {
    const template = templates.find(template => template.id === $('template-select').value);
    if (!template) { currentTemplateId = ''; renderTemplates(); return; }
    $('template-select').value = currentTemplateId;
    if ($('message').value.trim() && $('message').value !== template.body) confirmListAction('template-use',template);
    else useTemplate(template);
  });
  $('template-cancel').addEventListener('click',closeTemplate);
  $('template-dialog').addEventListener('cancel',event => { event.preventDefault(); closeTemplate(); });
  $('template-delete').addEventListener('click',() => { if (editingTemplate) confirmListAction('template-delete',editingTemplate); });
  $('template-form').addEventListener('submit',async event => {
    event.preventDefault(); if (busy) return;
    const fields = {name:$('template-name').value,body:$('template-body').value,
      ...(editingTemplate ? {id:editingTemplate.id,revision:editingTemplate.revision} : {})};
    lock(true); $('template-error').textContent = '';
    try {
      const result = await api('templates/save',fields); templates = result.templates; useTemplate(result.template);
      $('template-dialog').close(); notice('模板已保存。');
    } catch (error) { $('template-error').textContent = error.message; }
    finally { lock(false); if (!$('template-dialog').open) $('template-select').focus(); }
  });
  $('list-action-cancel').addEventListener('click', () => { pendingAction = null; $('list-action-dialog').close(); });
  $('list-action-dialog').addEventListener('cancel', event => { if (busy || !allowed()) event.preventDefault(); else pendingAction = null; });
  $('list-action-proceed').addEventListener('click', async () => {
    if (busy || !pendingAction) return;
    const {kind,list} = pendingAction;
    if (kind === 'discard') { pendingAction = null; $('list-action-dialog').close(); $('list-dialog').close(); return; }
    if (kind === 'template-discard') { pendingAction = null; $('list-action-dialog').close(); $('template-dialog').close(); return; }
    if (kind === 'template-use') { useTemplate(list); return; }
    if (kind === 'load') { await loadList(list); return; }
    lock(true); $('list-action-error').textContent = '';
    try {
      if (kind === 'template-delete') {
        const result = await api('templates/delete',{id:list.id,revision:list.revision}); templates = result.templates;
        if (currentTemplateId === list.id) currentTemplateId = '';
        $('template-dialog').close(); notice('模板已删除。');
      } else {
        const result = await api('lists/delete',{id:list.id,revision:list.revision}); lists = result.lists;
        if (currentList?.id === list.id) currentList = null;
        notice('名单已删除。');
      }
      pendingAction = null; $('list-action-dialog').close();
    } catch (error) { $('list-action-error').textContent = error.message; }
    finally { lock(false); ($('list-action-dialog').open ? $('list-action-cancel') : kind === 'template-delete' ? $('template-select') : $('library-close')).focus(); }
  });
  $('back').addEventListener('click', () => { if (!sending) { frozenPreview = null; sendRequestId = null; $('confirm').close(); } });
  $('confirm').addEventListener('cancel',event => { if (sending) event.preventDefault(); });
  configureScope(config.selected_cm_id);
  try {
    const raw = sessionStorage.getItem(draftKey); sessionStorage.removeItem(draftKey);
    if (raw) {
      const draft = parseDraft(raw,actor,Date.now(),canSelectCm);
      purpose = draft.purpose; fixedIds = draft.fixed_ids; fixedTargets = draft.fixed_targets;
      selectedGroups = new Set(draft.selected_groups); $('input').value = draft.raw; $('message').value = draft.message;
      $('mention').checked = draft.mention; configureScope(draft.cm_id); pendingRestore = true;
      notice('草稿已恢复，正在重新核验接收对象。');
    }
  } catch (error) { notice(error.message || '无法读取授权草稿。'); }
  try {
    const raw = sessionStorage.getItem('ttmedia.broadcast.selection'); sessionStorage.removeItem('ttmedia.broadcast.selection');
    if (raw) {
      const selection = parseSelection(raw,actor,Date.now(),canSelectCm);
      purpose = 'support'; fixedIds = selection.contact_ids; fixedTargets = null; selectedGroups = new Set();
      $('input').value = ''; $('mention').checked = true; configureScope(selection.cm_id); pendingRestore = true;
      notice('已带入选中达人，正在核验目标群。');
    }
  } catch (error) { notice(error.message || '无法读取带入名单，请重新选择。'); }
  render();
  void checkPermissions();
  void refreshHistory();
}
