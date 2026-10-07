// Routing is rechecked server-side. Personal lists/templates only; never sends.
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
export function parseSelection(raw, actor, now = Date.now()) {
  const selection = JSON.parse(raw), age = now - selection.created_at;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (selection.actor_id !== actor || typeof selection.created_at !== 'number' || !Number.isFinite(age) || age < 0 || age > 300000
      || (selection.cm_id !== 'all' && !uuid.test(selection.cm_id)) || !Array.isArray(selection.contact_ids)
      || !selection.contact_ids.length || selection.contact_ids.length > 500
      || !selection.contact_ids.every(id => typeof id === 'string' && uuid.test(id))) throw new Error('带入名单已失效，请回到“我的达人”重新选择。');
  return {cm_id:selection.cm_id,contact_ids:[...new Set(selection.contact_ids)]};
}
export function listState(cmId, purpose, mention, targets) {
  return JSON.stringify([cmId, purpose, mention,
    targets.map(target => [target.contact_id || '', target.chat_id]).sort((a,b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))]);
}

if (typeof process !== 'undefined' && process.argv.includes('--self-check')) {
  const { default: assert } = await import('node:assert/strict');
  const people = [{id:'p1', name:'Example One', handles:['one','backup'], groups:['shared','training']},
    {id:'p2', name:'Example Two', handles:['two'], groups:['shared','training','training2']}];
  const groups = [{id:'shared', purpose:'support', members:['p1','p2']},
    {id:'training', purpose:'training', members:['p1','p2']}, {id:'training2', purpose:'training', members:['p2']}];
  const entries = [people[0], people[0], people[1]].map((person,index) => ({key:`h${index}`, inputs:['input'],person,candidates:[person],selected:true}));
  let rows = recipients(entries,new Map(),groups,'support');
  assert.equal(rows.length,2); assert.equal(rows[0].entries.length,2);
  assert.equal(buildPlan(rows,true,'客服群').groups.length,1);
  assert.equal(buildPlan(rows,true,'客服群').groups[0].mentions.length,2);
  rows = recipients(entries,new Map(),groups,'training');
  assert.equal(buildPlan(rows,true,'培训群').issues.length,1);
  rows = recipients(entries,new Map([['p2','training2']]),groups,'training');
  assert.equal(buildPlan(rows,false,'培训群').groups.length,2);
  assert.ok(buildPlan(rows,false,'培训群').groups.every(group => !group.mentions.length));
  const mixed = [...entries,{key:'p1',inputs:['candidate'],person:null,candidates:[people[0]],selected:true}];
  assert.equal(buildPlan(recipients(mixed,new Map(),groups,'support'),true,'客服群').issues.length,1);
  assert.equal(buildPlan(recipients(entries,new Map([['p1','shared']]),groups,'training'),true,'培训群').issues.length,2);
  assert.equal(buildPlan(recipients(entries,new Map([['p1','']]),groups,'support'),true,'客服群').issues.length,1);
  const handoff = {actor_id:'actor',cm_id:'all',created_at:1000,contact_ids:['00000000-0000-0000-0000-000000000001']};
  assert.equal(parseSelection(JSON.stringify(handoff),'actor',1001).contact_ids.length,1);
  assert.throws(() => parseSelection(JSON.stringify(handoff),'other',1001));
  assert.throws(() => parseSelection(JSON.stringify(handoff),'actor',301001));
  assert.throws(() => parseSelection(JSON.stringify({...handoff,contact_ids:['bad']}),'actor',1001));
  const targets = [{contact_id:'p1',chat_id:'shared'},{contact_id:'p2',chat_id:'shared'}];
  const saved = listState('all','support',true,targets);
  assert.equal(saved,listState('all','support',true,[...targets].reverse()));
  assert.notEqual(saved,listState('all','support',false,targets));
  assert.notEqual(saved,listState('all','training',true,targets));
  assert.notEqual(saved,listState('other','support',true,targets));
  assert.notEqual(saved,listState('all','support',true,targets.slice(0,1)));
  for (const purpose of ['community','category','campaign']) {
    assert.equal(buildPlan(recipients(entries,new Map(),groups,purpose),true,'群').groups.length,0);
  }
  const direct = groupPlan(groups, ['training','training','training2'], true);
  assert.equal(direct.groups.length,2); assert.ok(direct.groups.every(group => group.mention_all && !group.people.length));
  assert.equal(groupPlan(groups, ['missing'], true).issues.length,1);
  assert.equal(groupPlan(groups, [], true).groups.length,0);
  assert.notEqual(listState('cm','category',true,[{chat_id:'training'}]),listState('cm','category',true,[{chat_id:'training2'}]));
  console.log('Multi-purpose broadcast routing check passed; no API calls.');
}

if (typeof document !== 'undefined' && document.getElementById('broadcast-app')) {
  const $ = id => document.getElementById(`broadcast-${id}`);
  const config = JSON.parse($('config').textContent), buttons = [...document.querySelectorAll('[data-purpose]')];
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className; return element;
  };
  let entries = [], groups = [], routes = new Map(), purpose = 'support', dirty = true, busy = false;
  let lists = config.lists || [], currentList = null, editingList = null, fixedIds = null, fixedTargets = null;
  let savedState = '', editMode = 'create', pendingAction = null, initialMetadata = '';
  let selectedGroups = new Set(), templates = config.templates || [], currentTemplateId = '', editingTemplate = null, initialTemplate = '';
  let permissionCheck = 0, permissionPending = false, permissionCm = null, permissionResult = null;
  const groupMode = () => purpose !== 'support';
  const rows = () => recipients(entries, routes, groups, purpose);
  const plan = () => groupMode() ? groupPlan(groups,selectedGroups,$('mention').checked) : buildPlan(rows(), $('mention').checked, config.purposes[purpose]);
  function notice(text) { $('notice').textContent = text; }
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
    if (!visible.length) $('group-items').append(node('div',busy ? '正在读取群聊…' : !$('cm').value ? '选择 CM/AM 后查看群聊' : query ? '没有匹配的群' : '暂无已分类的群','broadcast-group-empty text-secondary'));
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
    $('mention-preview').textContent = groupMode() ? $('mention').checked ? '@全体成员' : '' : current.groups[0]?.mentions.map(person => `@${person.name}`).join(' ') || '';
    for (const button of buttons) {
      const active = button.dataset.purpose === purpose;
      button.className = `btn ${active ? 'btn-primary' : 'btn-outline-secondary'}`; button.setAttribute('aria-pressed', String(active));
    }
    renderLibrary(current);
    renderTemplates();
  }
  function lock(value) {
    busy = value;
    for (const control of document.querySelectorAll('#broadcast-app main button, #broadcast-app main input, #broadcast-app main select, #broadcast-app main textarea')) control.disabled = value;
    render();
    $('permission-retry').disabled = permissionPending;
    for (const control of $('rows').querySelectorAll('input,select,button')) control.disabled = value;
    for (const dialog of ['list-dialog','library-dialog','list-action-dialog','template-dialog']) {
      for (const control of $(dialog).querySelectorAll('input,textarea,button')) control.disabled = value;
    }
  }
  async function api(action, data) {
    try {
      const response = await fetch(`/creators/broadcast/${action}`, {method:'POST', credentials:'same-origin', signal:AbortSignal.timeout(30000), headers:{'Content-Type':'application/json','X-CSRF-Token':$('app').dataset.csrf}, body:JSON.stringify(data)});
      const result = await response.json().catch(() => { throw new Error('服务暂时不可用，请稍后重试。'); });
      if (!response.ok) throw new Error(result.error || '请求失败，请稍后重试。');
      const readAction = ['match','resolve','groups','preview','lists/load'].includes(action);
      const groupRead = action === 'groups' || result?.mode === 'groups';
      if (!result || (readAction && (!Array.isArray(result.groups) || !Array.isArray(result[action === 'preview' || groupRead ? 'issues' : 'entries'])))
          || (action === 'permissions' ? !['ready','needs_authorization','unavailable'].includes(result.status)
            : !readAction && !Array.isArray(action.startsWith('templates/') ? result.templates : result.lists))) throw new Error('返回格式异常，请刷新后重试。');
      return result;
    } catch (error) {
      if (['TimeoutError','AbortError'].includes(error.name)) throw new Error('请求超时，请重新尝试。');
      if (error instanceof TypeError) throw new Error('网络连接失败，请稍后重试。');
      throw error;
    }
  }
  async function checkPermissions(force = false) {
    const cm = $('cm').value;
    if (!force && permissionCm === cm) return;
    if (force && permissionPending && permissionCm === cm) return;
    const request = ++permissionCheck; permissionCm = cm; permissionPending = true; permissionResult = null;
    $('permission-bar').setAttribute('aria-busy','true'); $('permission-retry').disabled = true;
    $('permission-account').textContent = cm === 'all' ? '当前账号' : config.cms.find(item => item.id === cm)?.name || 'CM/AM';
    $('permission-status').textContent = '正在核对授权'; $('permission-status').className = 'badge bg-secondary-lt ms-2';
    $('permission-message').textContent = ''; $('permission-authorize').hidden = true; $('permission-copy').hidden = true;
    try {
      const result = await api('permissions',{cm_id:cm});
      if (request !== permissionCheck) return;
      permissionResult = result;
      $('permission-account').textContent = `${result.is_sender ? 'CM/AM' : '当前账号'}：${result.account_name || '未选择'}`;
      $('permission-status').textContent = result.status === 'ready' ? '授权已核对' : result.status === 'needs_authorization' ? '需要补授权' : '暂时无法核对';
      $('permission-status').className = `badge ms-2 ${result.status === 'ready' ? 'bg-green-lt' : 'bg-yellow-lt'}`;
      $('permission-message').textContent = result.message || '';
      if (result.status === 'needs_authorization' && result.authorization_url) {
        if (result.is_self) { $('permission-authorize').href = result.authorization_url; $('permission-authorize').hidden = false; }
        else $('permission-copy').hidden = false;
      }
    } catch (error) {
      if (request !== permissionCheck) return;
      $('permission-status').textContent = '暂时无法核对'; $('permission-status').className = 'badge bg-yellow-lt ms-2';
      $('permission-message').textContent = error.message;
    } finally {
      if (request === permissionCheck) { permissionPending = false; $('permission-bar').setAttribute('aria-busy','false'); $('permission-retry').disabled = false; }
    }
  }
  $('permission-retry').addEventListener('click',() => void checkPermissions(true));
  $('permission-copy').addEventListener('click',async () => {
    const result = permissionResult, request = permissionCheck;
    if (!result?.authorization_url) return;
    try {
      await navigator.clipboard.writeText(new URL(result.authorization_url,location.origin).href);
      if (request === permissionCheck) $('permission-message').textContent = `已复制，请 ${result.account_name} 用本人账号打开。`;
    } catch { if (request === permissionCheck) $('permission-message').textContent = '无法复制，请允许浏览器访问剪贴板后重试。'; }
  });
  document.addEventListener('visibilitychange',() => {
    if (!document.hidden) void checkPermissions(true);
  });
  window.addEventListener('pageshow',event => { if (event.persisted) void checkPermissions(true); });
  function applyTargets(result) {
    entries = result.entries || []; groups = result.groups; routes = new Map((result.routes || []).map(route => [route.contact_id,route.chat_id]));
    selectedGroups = new Set(result.selected_chat_ids || []); dirty = false;
  }
  function configureScope(value = $('cm').value) {
    $('cm').replaceChildren(new Option(groupMode() ? '选择 CM/AM' : '全部已入库达人',groupMode() ? '' : 'all'));
    for (const cm of config.cms) $('cm').add(new Option(groupMode() ? cm.name : `${cm.name} 负责的达人`,cm.id));
    if (!groupMode() && !value) value = 'all';
    if (groupMode() && value === 'all') value = config.cms.some(cm => cm.id === $('app').dataset.actor) ? $('app').dataset.actor : '';
    if (value && ![...$('cm').options].some(option => option.value === value)) $('cm').add(new Option('授权已失效',value));
    $('cm').value = value;
    void checkPermissions();
  }
  async function fetchGroups(preserve = false) {
    if (busy) return;
    const old = preserve ? new Set(selectedGroups) : new Set();
    if (!preserve) { groups = []; selectedGroups = new Set(); }
    dirty = true;
    if (!$('cm').value || $('cm').value === 'all') { render(); return; }
    lock(true); notice('');
    try {
      const result = await api('groups',{cm_id:$('cm').value,purpose}); applyTargets(result);
      selectedGroups = new Set(groups.filter(group => old.has(group.id)).map(group => group.id));
      if (selectedGroups.size !== old.size) notice('已跳过失效群，请检查当前选择。');
    } catch (error) { notice(error.message); }
    finally { lock(false); }
  }
  async function match(preserve = false) {
    if (busy) return;
    const old = preserve || fixedIds ? new Map(entries.map(entry => [entry.key,{id:entry.person?.id,selected:entry.selected}])) : new Map();
    lock(true); notice('正在匹配…');
    try {
      const result = await api(fixedIds ? 'resolve' : 'match',
        {cm_id:$('cm').value,purpose,raw:$('input').value,contact_ids:fixedIds,...(fixedTargets ? {selections:fixedTargets} : {})});
      applyTargets(result);
      for (const entry of entries) {
        const saved = old.get(entry.key);
        if (saved) { entry.selected = !entry.reason && saved.selected; if (saved.id) entry.person = entry.candidates.find(person => person.id === saved.id) || entry.person; }
      }
      notice(result.duplicate_inputs ? `已合并 ${result.duplicate_inputs} 项重复输入。` : '匹配完成。');
    } catch (error) { dirty = true; notice(error.message || '匹配失败，请稍后重试。'); }
    finally { lock(false); }
  }
  $('match').addEventListener('click', () => void match());
  $('input').addEventListener('input', () => { dirty = true; notice(entries.length ? '名单已修改，请重新匹配。' : ''); render(); });
  for (const id of ['message','mention']) $(id).addEventListener('input', render);
  $('cm').addEventListener('change', () => {
    entries = []; groups = []; selectedGroups = new Set(); routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; $('input').value = ''; dirty = true; notice('');
    if (groupMode()) void fetchGroups(); else render();
    void checkPermissions();
  });
  for (const button of buttons) button.addEventListener('click', () => {
    if (purpose === button.dataset.purpose || busy) return;
    const wasGroupMode = groupMode();
    purpose = button.dataset.purpose; entries = []; groups = []; selectedGroups = new Set(); routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; dirty = true;
    if (wasGroupMode !== groupMode()) $('mention').checked = !groupMode();
    $('confirm').close(); $('input').value = ''; $('group-search').value = ''; notice(''); configureScope();
    if (groupMode()) void fetchGroups(); else render();
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
    if (busy || dirty || current.issues.length || !current.groups.length || !$('message').value.trim()) return;
    lock(true); notice('正在重新核对路由…');
    try {
      const result = await api('preview',{cm_id:$('cm').value,purpose,mention:$('mention').checked,message:$('message').value,selections:selections()});
      if (result.issues.length) { dirty = true; throw new Error(groupMode() ? '群聊已变化，请点击“刷新”重新核验。' : `${result.issues.length} 项路由已变化，请重新匹配。`); }
      $('confirm-count').textContent = `${result.groups.length} 个群 · ${result.groups.length} 条消息${groupMode() ? '' : ` · ${result.people.length} 位达人`}`;
      $('confirm-scope').textContent = $('cm').selectedOptions[0].textContent;
      $('confirm-message').textContent = result.message; $('confirm-groups').replaceChildren();
      for (const group of result.groups) {
        const item = node('div', '', 'broadcast-target');
        item.append(node('strong',group.name),node('div',group.mention_all ? '@全体成员 · 权限待核验' : group.mentions.length ? group.mentions.map(person => `@${person.name}`).join(' ') : '不 @ 成员','text-primary small mt-1'));
        $('confirm-groups').append(item);
      }
      notice(''); $('confirm').showModal();
    } catch (error) { notice(error.message || '预览失败，请稍后重试。'); }
    finally { lock(false); }
  });
  async function loadList(list) {
    if (busy) return;
    lock(true); $('library-error').textContent = ''; notice('正在载入并核验名单…');
    try {
      const result = await api('lists/load',{id:list.id});
      lists = lists.map(list => list.id === result.list.id ? result.list : list);
      currentList = result.list; purpose = result.purpose;
      fixedIds = groupMode() ? null : result.entries.map(entry => entry.key.replace(/^contact:/,'')); fixedTargets = groupMode() ? null : result.list.targets;
      configureScope(groupMode() && result.selected_cm_id === 'all' ? '' : result.selected_cm_id); $('mention').checked = result.list.mention;
      void checkPermissions(true);
      $('input').value = (result.entries || []).map(entry => entry.inputs[0]).join('\n'); $('group-search').value = ''; applyTargets(result);
      savedState = selectionState(); pendingAction = null; $('library-dialog').close(); $('list-action-dialog').close();
      notice([result.issues.length ? `已跳过 ${result.issues.length} 项：${result.issues[0].reason}` : '',result.notice || '名单已载入。'].filter(Boolean).join(' '));
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
    if (busy) return;
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
    if (busy) return;
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
    if (busy) return;
    if (JSON.stringify([$('list-name').value,$('list-notes').value]) !== initialMetadata) confirmListAction('discard');
    else $('list-dialog').close();
  }
  $('list-library').addEventListener('click', () => {
    $('library-error').textContent = ''; renderLibrary(); $('library-dialog').showModal();
  });
  $('library-close').addEventListener('click', () => $('library-dialog').close());
  $('library-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
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
    if (busy) return;
    editingTemplate = template;
    $('template-title').textContent = template ? '编辑消息模板' : '新建消息模板';
    $('template-name').value = template?.name || ''; $('template-body').value = template?.body ?? $('message').value;
    initialTemplate = JSON.stringify([$('template-name').value,$('template-body').value]);
    $('template-error').textContent = ''; $('template-delete').hidden = !template;
    $('template-dialog').showModal(); $('template-name').focus();
  }
  function closeTemplate() {
    if (busy) return;
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
  $('list-action-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); else pendingAction = null; });
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
  $('back').addEventListener('click', () => $('confirm').close());
  render();
  try {
    const raw = sessionStorage.getItem('ttmedia.broadcast.selection'); sessionStorage.removeItem('ttmedia.broadcast.selection');
    if (raw) {
      const selection = parseSelection(raw,$('app').dataset.actor);
      fixedIds = selection.contact_ids; $('cm').value = selection.cm_id;
      if (!$('cm').value) throw new Error('名单负责人已失效，请重新选择。');
      lock(true); notice('正在核验选中达人…');
      api('resolve',{cm_id:selection.cm_id,purpose,contact_ids:fixedIds}).then(result => {
        applyTargets(result); $('input').value = entries.map(entry => entry.inputs[0]).join('\n');
        notice(`已带入 ${entries.length} 位达人，请检查目标群。`);
      }).catch(error => { fixedIds = null; notice(error.message); }).finally(() => lock(false));
    }
  } catch (error) { fixedIds = null; notice(error.message || '无法读取带入名单，请重新选择。'); render(); }
  void checkPermissions();
}
