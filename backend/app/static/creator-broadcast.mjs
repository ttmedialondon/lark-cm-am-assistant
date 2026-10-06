// Internal interaction prototype. All identities/groups are synthetic; no API calls or storage.
export const demoGroups = [
  { id: 'demo-shared', name: 'TT Media × Ava / Eva · Support', purpose: 'support', members: ['ava', 'eva'] },
  { id: 'demo-chloe-main', name: 'TT Media × Chloe · Main account', purpose: 'support', members: ['chloe'] },
  { id: 'demo-chloe-shop', name: 'TT Media × Chloe · Shop account', purpose: 'support', members: ['chloe'] },
  { id: 'demo-ben', name: 'TT Media × Ben · Support', purpose: 'support', members: ['ben'] },
  { id: 'demo-ben-live', name: 'TT Media × Ben Live · Support', purpose: 'support', members: ['ben-live'] },
  { id: 'demo-training', name: 'TT Media · Creator training', purpose: 'training', members: ['daniel'] },
  { id: 'demo-maya', name: 'TT Media × Maya · Support', purpose: 'support', members: ['maya'], blocked: '当前账号无权发送' },
];
export const demoPeople = [
  { id: 'ava', name: 'Ava Collins', handles: ['demo_ava', 'demo_ava_backup'], groups: ['demo-shared'] },
  { id: 'eva', name: 'Eva Morgan', handles: ['demo_eva'], groups: ['demo-shared'] },
  { id: 'chloe', name: 'Chloe Green', handles: ['demo_chloe', 'demo_chloe_shop'], groups: ['demo-chloe-main', 'demo-chloe-shop'] },
  { id: 'ben', name: 'Ben Carter', handles: ['demo_ben'], groups: ['demo-ben'] },
  { id: 'ben-live', name: 'Ben Carter', handles: ['demo_ben_live'], groups: ['demo-ben-live'] },
  { id: 'daniel', name: 'Daniel Hughes', handles: ['demo_daniel'], groups: ['demo-training'] },
  { id: 'maya', name: 'Maya Reed', handles: ['demo_maya'], groups: ['demo-maya'] },
];
export function normalized(value) {
  let text = value.normalize('NFKC').trim();
  if (/^https?:\/\//i.test(text)) {
    try {
      const url = new URL(text);
      if (!['tiktok.com', 'www.tiktok.com', 'm.tiktok.com'].includes(url.hostname)) return text.toLowerCase();
      const match = decodeURIComponent(url.pathname).match(/^\/@([a-z0-9._]+)(?:\/|$)/i);
      if (match) text = match[1];
    } catch { return text.toLowerCase(); }
  }
  return text.replace(/^@/, '').replace(/\s+/g, ' ').toLowerCase();
}
export function matchEntries(raw, people = demoPeople) {
  const values = raw.split(/[\n\r,，;；\t]+/).map(value => value.trim()).filter(Boolean);
  if (!values.length) throw new Error('请先输入 handle 或名字。');
  if (raw.length > 32768 || values.length > 200 || values.some(value => value.length > 256)) throw new Error('原型最多匹配 200 项，每项不超过 256 字符。');
  const unique = new Map();
  for (const input of values) {
    const key = normalized(input);
    if (unique.has(key)) { unique.get(key).inputs.push(input); continue; }
    const exact = people.filter(person => normalized(person.name) === key || person.handles.some(handle => normalized(handle) === key));
    const candidates = exact.length ? exact : key.length < 2 ? [] : people.filter(person => normalized(person.name).includes(key)
      || person.handles.some(handle => normalized(handle).includes(key))).slice(0, 3);
    unique.set(key, { key, inputs: [input], candidates, person: exact.length === 1 ? exact[0] : null, selected: true, approximate: !exact.length });
  }
  return [...unique.values()];
}
export function recipients(entries, routes = new Map(), groups = demoGroups) {
  const rows = new Map();
  for (const entry of entries) {
    const key = entry.person ? entry.person.id : `input:${entry.key}`;
    if (!rows.has(key)) rows.set(key, { key, person: entry.person, entries: [], selected: false });
    const row = rows.get(key); row.entries.push(entry); row.selected ||= entry.selected;
  }
  return [...rows.values()].map(row => {
    const options = row.person ? groups.filter(group => group.purpose === 'support' && row.person.groups.includes(group.id)) : [];
    return { ...row, options, chatId: routes.get(row.key) ?? (options.length === 1 ? options[0].id : '') };
  });
}
export function buildPlan(rows, mention = true) {
  const groups = new Map(), issues = [], people = [];
  for (const row of rows.filter(row => row.selected)) {
    let reason = '';
    const group = row.options.find(option => option.id === row.chatId);
    if (!row.person) reason = row.entries[0].candidates.length ? '需要选择具体达人' : '未找到已入库达人';
    else if (!row.options.length) reason = '没有客服群';
    else if (!group) reason = '需要选择客服群';
    else if (group.blocked) reason = group.blocked;
    else if (!group.members.includes(row.person.id)) reason = '群成员未核验';
    if (row.person) people.push(row.person);
    if (reason) { issues.push({ key: row.key, reason }); continue; }
    if (!groups.has(group.id)) groups.set(group.id, { id: group.id, name: group.name, people: [], mentions: [] });
    const target = groups.get(group.id);
    if (!target.people.some(person => person.id === row.person.id)) {
      target.people.push(row.person);
      if (mention) target.mentions.push(row.person);
    }
  }
  return { groups: [...groups.values()], issues, people };
}
export function retryableTargets(targets) { return targets.filter(target => target.status === 'failed'); }

if (typeof process !== 'undefined' && process.argv.includes('--self-check')) {
  const { default: assert } = await import('node:assert/strict');
  const entries = matchEntries('@DEMO_AVA\nhttps://www.tiktok.com/@demo_ava\n@demo_ava_backup\n@demo_eva\nChloe Green\nBen Carter\nDaniel Hughes\n@demo_missing\n@demo_maya');
  const routes = new Map();
  let current = buildPlan(recipients(entries, routes));
  assert.equal(current.groups.length, 1);
  assert.deepEqual(current.groups[0].mentions.map(person => person.id), ['ava', 'eva']);
  assert.equal(current.issues.length, 5);
  const ben = entries.find(entry => entry.key === 'ben carter');
  assert.equal(ben.person, null); assert.equal(ben.candidates.length, 2);
  ben.person = ben.candidates[0]; routes.set('chloe', 'demo-chloe-shop');
  current = buildPlan(recipients(entries, routes));
  assert.equal(current.groups.length, 3); assert.equal(current.issues.length, 3);
  for (const row of recipients(entries, routes)) {
    if (current.issues.some(issue => issue.key === row.key)) for (const entry of row.entries) entry.selected = false;
  }
  current = buildPlan(recipients(entries, routes));
  assert.equal(current.issues.length, 0); assert.equal(current.people.length, 4);
  assert.ok(buildPlan(recipients(entries, routes), false).groups.every(group => !group.mentions.length));
  assert.equal(matchEntries('Chloe')[0].person, null);
  for (const values of ['@demo_ava\nava', 'ava\n@demo_ava']) {
    const mixed = buildPlan(recipients(matchEntries(values)));
    assert.equal(mixed.groups.length, 1); assert.equal(mixed.issues.length, 1);
  }
  assert.equal(matchEntries('https://tiktok.com.evil.example/@demo_ava')[0].person, null);
  assert.throws(() => matchEntries('name\n'.repeat(201)));
  assert.deepEqual(retryableTargets([{ status: 'succeeded' }, { status: 'unknown' }, { status: 'failed' }]), [{ status: 'failed' }]);
  console.log('Broadcast prototype workflow check passed (synthetic data, no API calls).');
}

if (typeof document !== 'undefined' && document.getElementById('broadcast-app')) {
  const $ = id => document.getElementById(`broadcast-${id}`);
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className; return element;
  };
  let entries = [], routes = new Map(), preview = null, targets = [], running = false, stopped = false, dirty = true;
  $('input').value = '@demo_ava\nhttps://www.tiktok.com/@demo_ava_backup\n@demo_eva\nChloe Green\nBen Carter\n@demo_missing\nDaniel Hughes';
  $('message').value = 'Hi! We’re collecting availability for next week’s creator session.\nPlease reply in this group with a time that works for you.\nThank you!';
  const plan = () => buildPlan(recipients(entries, routes), $('mention').checked);
  const invalidate = () => { preview = null; $('visible-check').checked = false; $('simulate').disabled = true; };
  function render() {
    invalidate();
    const current = plan(), rows = recipients(entries, routes);
    $('matches').hidden = !entries.length;
    $('count').textContent = `· ${rows.filter(row => row.person).length} 人`;
    $('rows').replaceChildren();
    for (const row of rows) {
      const tr = node('tr'), selection = node('td'), identity = node('td', '', 'broadcast-identity'), route = node('td', '', 'broadcast-route');
      const check = node('input'); check.type = 'checkbox'; check.className = 'form-check-input'; check.checked = row.selected;
      check.setAttribute('aria-label', `选择 ${row.person?.name || row.entries[0].inputs[0]}`);
      check.addEventListener('change', () => { for (const entry of row.entries) entry.selected = check.checked; render(); });
      selection.append(check);
      if (row.person) {
        identity.append(node('strong', row.person.name), node('div', row.person.handles.map(handle => `@${handle}`).join(' · '), 'text-secondary small'));
        const sources = row.entries.flatMap(entry => entry.inputs);
        if (sources.length > 1) identity.append(node('div', `${sources.length} 项输入已合并为同一人`, 'text-secondary small mt-1'));
      } else {
        identity.append(node('div', row.entries[0].inputs[0]));
        const entry = row.entries[0];
        if (entry.candidates.length) {
          const select = node('select', '', 'form-select form-select-sm mt-2'); select.setAttribute('aria-label', `为 ${entry.inputs[0]} 选择达人`);
          select.append(new Option(entry.approximate ? '近似候选，需选择' : '同名候选，需选择', ''));
          for (const person of entry.candidates) select.append(new Option(`${person.name} · @${person.handles[0]}`, person.id));
          select.addEventListener('change', () => { entry.person = entry.candidates.find(person => person.id === select.value) || null; render(); });
          identity.append(select);
        }
      }
      if (row.options.length > 1) {
        const select = node('select', '', 'form-select form-select-sm'); select.setAttribute('aria-label', `为 ${row.person.name} 选择客服群`);
        select.append(new Option('选择一个客服群', ''));
        for (const group of row.options) select.append(new Option(group.name, group.id));
        select.value = row.chatId; select.addEventListener('change', () => { routes.set(row.key, select.value); render(); }); route.append(select);
      } else if (row.options.length === 1) route.append(node('div', row.options[0].name));
      const issue = current.issues.find(item => item.key === row.key);
      if (issue) route.append(node('div', issue.reason, 'text-orange small mt-1'));
      else if (!row.selected) route.append(node('div', '已移除', 'text-secondary small mt-1'));
      else route.append(node('div', '就绪', 'text-success small mt-1'));
      tr.append(selection, identity, route); $('rows').append(tr);
    }
    $('exclude').hidden = !current.issues.length;
    $('summary').textContent = entries.length ? `已选 ${current.people.length} 人 · ${current.groups.length} 个群` : '尚未选择对象';
    $('notice').textContent = dirty && entries.length ? '名单已修改，请重新匹配。' : current.issues.length ? `${current.issues.length} 项需要处理或移除。` : entries.length ? '同一人已合并，同一群只发送一次。' : '';
    $('preview').disabled = dirty || !current.groups.length || Boolean(current.issues.length) || !$('message').value.trim() || running;
    $('message-preview').textContent = $('message').value || '输入消息后预览';
    const first = current.groups[0];
    $('mention-preview').textContent = first?.mentions.length ? first.mentions.map(person => `@${person.name}`).join(' ') : '';
  }
  $('match').addEventListener('click', () => {
    try { entries = matchEntries($('input').value); routes = new Map(); dirty = false; render(); }
    catch (error) { $('notice').textContent = error.message; $('notice').setAttribute('role', 'alert'); }
  });
  $('input').addEventListener('input', () => { dirty = true; render(); });
  for (const id of ['message', 'mention']) $(id).addEventListener('input', render);
  $('exclude').addEventListener('click', () => {
    const blocked = new Set(plan().issues.map(item => item.key));
    for (const row of recipients(entries, routes)) if (blocked.has(row.key)) for (const entry of row.entries) entry.selected = false;
    render();
  });
  $('preview').addEventListener('click', () => {
    const current = plan();
    if (dirty || current.issues.length || !current.groups.length || !$('message').value.trim()) return;
    preview = structuredClone({ ...current, message: $('message').value });
    $('confirm-count').textContent = `${current.people.length} 位达人 → ${current.groups.length} 个群 → ${current.groups.length} 条消息`;
    const excluded = recipients(entries, routes).filter(row => !row.selected).length;
    $('confirm-excluded').textContent = excluded ? `已移除 ${excluded} 项，不会发送。` : '';
    $('confirm-message').textContent = preview.message;
    $('confirm-groups').replaceChildren();
    for (const group of preview.groups) {
      const item = node('div', '', 'broadcast-target');
      item.append(node('strong', group.name), node('div', group.mentions.length ? group.mentions.map(person => `@${person.name}`).join(' ') : '不 @ 成员', 'text-primary small mt-1'));
      $('confirm-groups').append(item);
    }
    $('visible-check').checked = false; $('simulate').disabled = true;
    $('confirm').showModal();
  });
  $('back').addEventListener('click', () => $('confirm').close());
  $('confirm').addEventListener('close', () => { $('visible-check').checked = false; $('simulate').disabled = true; });
  $('visible-check').addEventListener('change', () => { $('simulate').disabled = !preview || !$('visible-check').checked; });
  function renderResults() {
    $('results').hidden = !targets.length; $('stop').hidden = !running; $('retry').hidden = running || !retryableTargets(targets).length;
    $('result-rows').replaceChildren();
    const counts = status => targets.filter(target => target.status === status).length;
    $('progress').textContent = `${running ? '模拟进行中' : '模拟结束，未真实发送'} · 成功 ${counts('succeeded')} · 失败 ${counts('failed')} · 送达待核实 ${counts('unknown')} · 已停止 ${counts('cancelled')}`;
    const labels = { queued: '等待模拟', sending: '模拟发送中', succeeded: '模拟成功', failed: '模拟失败 · 权限拒绝', unknown: '模拟超时 · 送达待核实，不自动重发', cancelled: '已停止 · 未执行' };
    for (const target of targets) {
      const tr = node('tr'); tr.append(node('td', target.name), node('td', target.mentions.map(person => `@${person.name}`).join(' ') || '不 @ 成员'), node('td', labels[target.status])); $('result-rows').append(tr);
    }
  }
  function lockControls(locked) {
    for (const selector of ['#broadcast-input', '#broadcast-match', '#broadcast-message', '#broadcast-mention', '#broadcast-preview', '#broadcast-exclude', '#broadcast-rows input', '#broadcast-rows select']) {
      for (const control of document.querySelectorAll(selector)) control.disabled = locked;
    }
    if (!locked) render();
  }
  async function run(retry = false) {
    if (running) return;
    running = true; stopped = false; $('stop').disabled = false; lockControls(true); renderResults();
    for (const target of targets.filter(item => item.status === 'queued')) {
      if (stopped) { target.status = 'cancelled'; continue; }
      target.status = 'sending'; renderResults();
      await new Promise(resolve => setTimeout(resolve, 650));
      target.status = retry ? 'succeeded' : target.demoOutcome;
      renderResults();
    }
    running = false; lockControls(false); renderResults();
  }
  $('simulate').addEventListener('click', () => {
    if (!preview || !$('visible-check').checked || running) return;
    const snapshot = preview; $('confirm').close();
    targets = snapshot.groups.map((group, index) => ({ ...group, status: 'queued', demoOutcome: ['succeeded', 'failed', 'unknown'][index] || 'succeeded' }));
    void run(); $('results').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
  $('stop').addEventListener('click', () => { stopped = true; $('stop').disabled = true; });
  $('retry').addEventListener('click', () => {
    if (running) return;
    for (const target of retryableTargets(targets)) target.status = 'queued';
    $('stop').disabled = false; void run(true);
  });
  render();
}
