import test from 'node:test';
import assert from 'node:assert/strict';
import {recipients, buildPlan, groupPlan, parseSelection, listState, permissionReady,
  authorizationUrl, parseDraft} from '../app/static/creator-broadcast.mjs';

test('broadcast routing, draft isolation and explicit authorization; no API calls', () => {
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
  const actor = '00000000-0000-0000-0000-000000000001', other = '00000000-0000-0000-0000-000000000002';
  assert.equal(parseSelection(JSON.stringify({...handoff,actor_id:actor,cm_id:actor}),actor,1001,false).cm_id,actor);
  for (const cm_id of ['all',other]) {
    assert.throws(() => parseSelection(JSON.stringify({...handoff,actor_id:actor,cm_id}),actor,1001,false));
  }
  const draft = {version:1,actor_id:actor,created_at:1000,cm_id:'all',purpose:'support',mention:true,
    message:'Keep my text',raw:'@example',fixed_ids:[other],fixed_targets:[{contact_id:other,chat_id:'oc_example'}],
    selected_groups:[]};
  assert.equal(parseDraft(JSON.stringify(draft),actor,1001).message,draft.message);
  assert.equal(parseDraft(JSON.stringify({...draft,cm_id:actor}),actor,1001,false).message,draft.message);
  for (const cm_id of ['all','',other]) assert.throws(() => parseDraft(JSON.stringify({...draft,cm_id}),actor,1001,false));
  assert.equal(parseDraft(JSON.stringify({...draft,fixed_ids:[],fixed_targets:[]}),actor,1001).fixed_ids.length,0);
  assert.equal(parseDraft(JSON.stringify({...draft,fixed_targets:[{contact_id:other,chat_id:''}]}),actor,1001).fixed_targets[0].chat_id,'');
  for (const fields of [{actor_id:other},{created_at:-1800001},{message:'x'.repeat(4001)},
    {fixed_ids:['bad']},{fixed_targets:[{contact_id:actor,chat_id:'oc_example'}]},{selected_groups:['']},
    {purpose:'unknown'}]) assert.throws(() => parseDraft(JSON.stringify({...draft,...fields}),actor,1001));
  assert.equal(authorizationUrl(`/auth/lark/start?send=1&account_id=${actor}`,actor,'https://testserver'),
    `/auth/lark/start?send=1&account_id=${actor}`);
  for (const url of [`https://other.test/auth/lark/start?send=1&account_id=${actor}`,
    `/auth/lark/start?send=1&account_id=${other}`,`/auth/lark/start?send=1&account_id=${actor}&redirect=evil`,
    `/auth/lark/start?send=1&account_id=${actor}&auto=1`]) {
    assert.throws(() => authorizationUrl(url,actor,'https://testserver'));
  }
  assert.ok(permissionReady({status:'ready'},false,'all','all'));
  assert.ok(!permissionReady({status:'ready'},true,'all','all'));
  assert.ok(!permissionReady({status:'ready'},false,actor,'all'));
  assert.ok(!permissionReady({status:'ready'},false,'all','all',true));
  assert.ok(!permissionReady({status:'needs_authorization'},false,'all','all'));
});
