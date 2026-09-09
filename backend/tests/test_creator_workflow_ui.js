/* Run with: node --test backend/tests/test_creator_workflow_ui.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const {newDraft, missingFields, draftBody, savedDraft, pending} = require("../app/static/creator-workflow.js");
const item = {id: "candidate", version: 4, status: "pending", handle: "example", relationship: "owner",
  contact: {contact_id: "actual-id", display_name: "Same name"}, reasons: []};

test("missing fields stay empty and free text is not a selected identity", () => {
  const draft = newDraft({...item, handle: null, contact: null, relationship: null});
  assert.equal(draft.handle, "");
  assert.equal(draftBody(draft, "retry-id").handle, null);
  assert.equal(draftBody(draft, "retry-id").contact_id, null);
  draft.contact = {display_name: "Same name"};
  assert(missingFields(draft, item).some(reason => reason.includes("对应用户")));
  for (const handle of ["—", "–", "-", "   "]) {
    draft.handle = handle;
    assert(missingFields(draft, item).includes("handle"));
  }
});

test("selection requires explicit acknowledgement and unresolved doubts are not auto-selected", () => {
  const risk = {...item, reasons: [{code: "ai_review", label: "AI 推荐待核对", resolvable: true}]};
  const draft = newDraft(risk);
  assert.deepEqual(draft.resolved_reasons, []);
  assert.equal(draft.acknowledged, false);
  assert(missingFields(draft, risk).includes("AI 推荐待核对"));
  draft.acknowledged = true;
  assert(missingFields(draft, risk).length > 0);
  draft.resolved_reasons = ["ai_review"];
  assert.deepEqual(missingFields(draft, risk), []);
  assert.equal(draftBody(draft, "request").contact_id, "actual-id");
  assert.equal(draftBody(draft, "request").version, 4);
});

test("a conflict cannot be bypassed with a local resolution checkbox", () => {
  const risk = {...item, reasons: [{code: "ownership_conflict", label: "归属冲突", resolvable: false}]};
  const draft = {...newDraft(risk), acknowledged: true, resolved_reasons: ["ownership_conflict"]};
  assert(missingFields(draft, risk).includes("归属冲突"));
});

test("late save keeps new local edits but advances the optimistic version", () => {
  const draft = {...newDraft(item), dirty: true, revision: 3, handle: "newer-local", request: "old-request"};
  const result = savedDraft(draft, {...item, version: 5, handle: "older-saved"}, 2);
  assert.equal(result.handle, "newer-local");
  assert.equal(result.version, 5);
  assert.equal(result.dirty, true);
  assert.equal(result.request, null);
  const noRace = savedDraft(draft, {...item, version: 5, handle: "saved-value"}, 3);
  assert.equal(noRace.handle, "saved-value");
  assert.equal(noRace.dirty, false);
});

test("deferred stays pending, formal and rejected are not editable pending records", () => {
  assert(pending({status: "deferred"}));
  assert(!pending({status: "confirmed"}));
  assert(!pending({status: "rejected"}));
});
