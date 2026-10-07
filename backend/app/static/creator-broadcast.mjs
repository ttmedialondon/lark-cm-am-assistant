// Routing is rechecked server-side. Only personal static lists are written; never sends.
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
export function parseSelection(raw, actor, now = Date.now()) {
  const selection = JSON.parse(raw), age = now - selection.created_at;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (selection.actor_id !== actor || typeof selection.created_at !== 'number' || !Number.isFinite(age) || age < 0 || age > 300000
      || (selection.cm_id !== 'all' && !uuid.test(selection.cm_id)) || !Array.isArray(selection.contact_ids)
      || !selection.contact_ids.length || selection.contact_ids.length > 500
      || !selection.contact_ids.every(id => typeof id === 'string' && uuid.test(id))) throw new Error('带入名单已失效，请回到“我的达人”重新选择。');
  return {cm_id:selection.cm_id,contact_ids:[...new Set(selection.contact_ids)]};
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
  for (const purpose of ['community','category','campaign']) {
    assert.equal(buildPlan(recipients(entries,new Map(),groups,purpose),true,'群').groups.length,0);
  }
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
  const rows = () => recipients(entries, routes, groups, purpose);
  const plan = () => buildPlan(rows(), $('mention').checked, config.purposes[purpose]);
  function notice(text) { $('notice').textContent = text; }
  const selections = () => rows().filter(row => row.selected && row.person).map(row => ({contact_id:row.person.id,chat_id:row.chatId}));
  const canSaveTargets = () => !dirty && plan().groups.length > 0 && plan().issues.length === 0;
  function renderLibrary() {
    const selected = $('list-select').value;
    $('list-select').replaceChildren(new Option(lists.length ? '选择已保存的名单' : '还没有保存的名单', ''),
      ...lists.map(list => new Option(`${list.name} · ${list.target_count} 人`,list.id)));
    $('list-select').value = selected;
    $('list-load').disabled = busy || !$('list-select').value;
    $('list-manage').disabled = busy || !$('list-select').value;
    $('list-new').disabled = busy || !canSaveTargets();
    $('list-info').textContent = currentList ? `已载入「${currentList.name}」 · 固定 ${currentList.target_count} 人，失效项不会替换成其他群。` : '保存固定人员和群，下次直接载入。';
  }
  function render() {
    const current = plan(), all = rows();
    $('matches').hidden = !entries.length;
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
    $('summary').textContent = entries.length ? `已选 ${current.people.length} 人 · ${current.groups.length} 个群` : '尚未选择对象';
    $('preview').disabled = busy || dirty || !current.groups.length || Boolean(current.issues.length) || !$('message').value.trim();
    $('download').disabled = busy || dirty || !current.people.length;
    $('message-preview').textContent = $('message').value || '输入消息后预览';
    $('input').readOnly = Boolean(fixedIds);
    $('match').textContent = fixedIds ? '重新核验' : '匹配名单';
    $('mention-preview').textContent = current.groups[0]?.mentions.map(person => `@${person.name}`).join(' ') || '';
    for (const button of buttons) {
      const active = button.dataset.purpose === purpose;
      button.className = `btn ${active ? 'btn-primary' : 'btn-outline-secondary'}`; button.setAttribute('aria-pressed', String(active));
    }
    renderLibrary();
  }
  function lock(value) {
    busy = value;
    for (const control of document.querySelectorAll('#broadcast-app main button, #broadcast-app main input, #broadcast-app main select, #broadcast-app main textarea')) control.disabled = value;
    render();
    for (const control of $('rows').querySelectorAll('input,select,button')) control.disabled = value;
    for (const control of $('list-form').querySelectorAll('input,textarea,button')) control.disabled = value;
    $('list-update').disabled = value || !canSaveTargets();
  }
  async function api(action, data) {
    try {
      const response = await fetch(`/creators/broadcast/${action}`, {method:'POST', credentials:'same-origin', signal:AbortSignal.timeout(30000), headers:{'Content-Type':'application/json','X-CSRF-Token':$('app').dataset.csrf}, body:JSON.stringify(data)});
      const result = await response.json().catch(() => { throw new Error('服务暂时不可用，请稍后重试。'); });
      if (!response.ok) throw new Error(result.error || '请求失败，请稍后重试。');
      const readAction = ['match','resolve','preview','lists/load'].includes(action);
      if (!result || (readAction && (!Array.isArray(result.groups) || !Array.isArray(result[action === 'preview' ? 'issues' : 'entries'])))
          || (!readAction && !Array.isArray(result.lists))) throw new Error('返回格式异常，请刷新后重试。');
      return result;
    } catch (error) {
      if (['TimeoutError','AbortError'].includes(error.name)) throw new Error('请求超时，请重新尝试。');
      if (error instanceof TypeError) throw new Error('网络连接失败，请稍后重试。');
      throw error;
    }
  }
  function applyTargets(result) {
    entries = result.entries; groups = result.groups; routes = new Map((result.routes || []).map(route => [route.contact_id,route.chat_id])); dirty = false;
  }
  async function match(preserve = false) {
    if (busy) return;
    const old = preserve ? new Map(entries.map(entry => [entry.key,{id:entry.person?.id,selected:entry.selected}])) : new Map();
    lock(true); notice('正在匹配…');
    try {
      const result = await api(fixedIds ? 'resolve' : 'match',
        {cm_id:$('cm').value,purpose,raw:$('input').value,contact_ids:fixedIds,...(fixedTargets ? {selections:fixedTargets} : {})});
      applyTargets(result);
      for (const entry of entries) {
        const saved = old.get(entry.key);
        if (saved) { entry.selected = saved.selected; if (saved.id) entry.person = entry.candidates.find(person => person.id === saved.id) || entry.person; }
      }
      notice(result.duplicate_inputs ? `已合并 ${result.duplicate_inputs} 项重复输入。` : '匹配完成。');
    } catch (error) { dirty = true; notice(error.message || '匹配失败，请稍后重试。'); }
    finally { lock(false); }
  }
  $('match').addEventListener('click', () => void match());
  $('input').addEventListener('input', () => { dirty = true; notice(entries.length ? '名单已修改，请重新匹配。' : ''); render(); });
  for (const id of ['message','mention']) $(id).addEventListener('input', render);
  $('cm').addEventListener('change', () => { entries = []; groups = []; routes = new Map(); fixedIds = null; fixedTargets = null; currentList = null; $('input').value = ''; dirty = true; notice('范围已切换，请重新匹配。'); render(); });
  for (const button of buttons) button.addEventListener('click', () => {
    if (purpose === button.dataset.purpose || busy) return;
    purpose = button.dataset.purpose; routes = new Map(); fixedTargets = null; $('confirm').close();
    if (entries.length && !dirty) void match(true); else { entries = []; groups = []; render(); }
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
      if (result.issues.length) { dirty = true; throw new Error(`${result.issues.length} 项路由已变化，请重新匹配。`); }
      $('confirm-count').textContent = `${config.purposes[purpose]} · ${result.people.length} 位达人 → ${result.groups.length} 个群 → ${result.groups.length} 条消息`;
      $('confirm-scope').textContent = `资料范围：${$('cm').selectedOptions[0].textContent}。查看权限不等于发送权限。`;
      $('confirm-message').textContent = result.message; $('confirm-groups').replaceChildren();
      for (const group of result.groups) {
        const item = node('div', '', 'broadcast-target');
        item.append(node('strong',group.name),node('div',group.mentions.length ? group.mentions.map(person => `@${person.name}`).join(' ') : '不 @ 成员','text-primary small mt-1'),node('div',`群内全体可见 · 库内成员数：${group.member_count ?? '未知'}`,'text-secondary small mt-1'));
        $('confirm-groups').append(item);
      }
      notice('路由预览已生成，不会发送。'); $('confirm').showModal();
    } catch (error) { notice(error.message || '预览失败，请稍后重试。'); }
    finally { lock(false); }
  });
  $('list-select').addEventListener('change', renderLibrary);
  $('list-load').addEventListener('click', async () => {
    if (busy || !$('list-select').value) return;
    lock(true); notice('正在载入并核验名单…');
    try {
      const result = await api('lists/load',{id:$('list-select').value});
      lists = lists.map(list => list.id === result.list.id ? result.list : list);
      currentList = result.list; fixedIds = result.entries.map(entry => entry.key.replace(/^contact:/,'')); fixedTargets = result.list.targets;
      purpose = result.purpose;
      if (![...$('cm').options].some(option => option.value === result.selected_cm_id)) $('cm').add(new Option('原负责人已失效',result.selected_cm_id));
      $('cm').value = result.selected_cm_id; $('mention').checked = result.list.mention;
      $('input').value = result.entries.map(entry => entry.inputs[0]).join('\n'); applyTargets(result);
      notice(result.issues.length ? `名单已载入，${result.issues.length} 项已跳过；可查看原因或重新选群。` : '名单已载入，人员与群已重新核验。');
    } catch (error) { notice(error.message); }
    finally { lock(false); }
  });
  function editList(list) {
    editingList = list; $('list-title').textContent = list ? '管理名单' : '保存名单';
    $('list-name').value = list?.name || ''; $('list-notes').value = list?.notes || '';
    $('list-update').checked = false; $('list-update-wrap').hidden = !list;
    $('list-update').disabled = !canSaveTargets(); $('list-delete').hidden = !list;
    $('list-save').textContent = list ? '保存修改' : '保存名单'; $('list-error').textContent = '';
    $('list-description').textContent = list ? '默认只修改名称和备注；勾选上方选项才会替换名单目标。' : `保存当前 ${plan().people.length} 位达人和已选群，不保存消息正文，不会发送。`;
    $('list-dialog').showModal();
  }
  $('list-new').addEventListener('click', () => editList(null));
  $('list-manage').addEventListener('click', () => editList(lists.find(list => list.id === $('list-select').value)));
  $('list-cancel').addEventListener('click', () => $('list-dialog').close());
  $('list-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  $('list-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const metadataOnly = Boolean(editingList) && !$('list-update').checked;
    if (!metadataOnly && !canSaveTargets()) { $('list-error').textContent = '请先完成名单匹配和选群。'; return; }
    const fields = {name:$('list-name').value,notes:$('list-notes').value,metadata_only:metadataOnly,
      cm_id:$('cm').value,purpose,mention:$('mention').checked,selections:selections(),
      ...(editingList ? {id:editingList.id,revision:editingList.revision} : {})};
    lock(true); $('list-error').textContent = '';
    try {
      const result = await api('lists/save',fields); lists = result.lists;
      if (currentList?.id === result.list.id) currentList = result.list;
      if (!metadataOnly) { currentList = result.list; fixedIds = fields.selections.map(target => target.contact_id); fixedTargets = fields.selections; }
      renderLibrary(); $('list-select').value = result.list.id; $('list-dialog').close();
      notice('名单已保存，不会发送消息。');
    } catch (error) { $('list-error').textContent = error.message; }
    finally { lock(false); }
  });
  $('list-delete').addEventListener('click', async () => {
    if (busy || !editingList || !window.confirm(`删除个人名单「${editingList.name}」？不会删除达人或群。`)) return;
    lock(true);
    try {
      const result = await api('lists/delete',{id:editingList.id,revision:editingList.revision}); lists = result.lists;
      if (currentList?.id === editingList.id) currentList = null;
      $('list-dialog').close(); notice('个人名单已删除；达人、群和当前选择未改动。');
    } catch (error) { $('list-error').textContent = error.message; }
    finally { lock(false); }
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
}
