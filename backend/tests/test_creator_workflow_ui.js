/* Run with: node --test backend/tests/test_creator_workflow_ui.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const {newDraft, missingFields, draftBody, savedDraft, pending} = require("../app/static/creator-workflow.js");
const item = {id: "candidate", version: 4, status: "pending", handle: "example", relationship: "owner",
  contact: {contact_id: "actual-id", display_name: "Same name"}, reasons: []};

test("missing fields stay empty and free text is not a selected identity", () => {
  const draft = newDraft({...item, handle: null, contact: null, relationship: null});
  assert.equal(draft.handle, "");
  assert.equal(draft.searchOpen, false);
  assert.equal(draft.handleOpen, false);
  assert.equal(draftBody(draft, "retry-id").handle, null);
  assert.equal(draftBody(draft, "retry-id").contact_id, null);
  draft.contact = {display_name: "Same name"};
  assert(missingFields(draft, item).some(reason => reason.includes("对应用户")));
  for (const handle of ["—", "–", "-", "   "]) {
    draft.handle = handle;
    assert(missingFields(draft, item).includes("handle"));
  }
});

test("handle plus a selected identity suffice without review acknowledgements or relationship", () => {
  const risk = {...item, relationship: null, reasons: [
    {code: "ai_review", label: "AI 推荐待核对", resolvable: true},
    {code: "details_changed", label: "资料已修改", resolvable: true},
    {code: "missing_relationship", label: "用户关系待核对", resolvable: false}]};
  const draft = newDraft(risk);
  assert.deepEqual(missingFields(draft, risk), []);
  assert.deepEqual(draftBody(draft, "request"), {
    version: 4, handle: "example", contact_id: "actual-id", request_id: "request",
  });
  for (const field of ["relationship", "resolved_reasons", "acknowledged", "identityDirty", "recheckRequired", "recheck"]) {
    assert.equal(field in draft, false, `${field} is no longer a confirmation step`);
  }
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

test("complete edits can be confirmed and emptying either field makes the draft incomplete", () => {
  const draft = {...newDraft(item), dirty: true, handle: "corrected.handle", contact: {contact_id: "another-id"}};
  assert.deepEqual(missingFields(draft, item), []);
  draft.handle = "";
  assert(missingFields(draft, item).includes("handle"));
  draft.handle = "corrected.handle"; draft.contact = null;
  assert(missingFields(draft, item).some(reason => reason.includes("对应用户")));
});

test("saving corrected identity preserves editor visibility and does not add another verification gate", () => {
  const draft = {...newDraft(item), dirty: true, revision: 2, handleOpen: true, searchOpen: true};
  const updated = {...item, version: 5, reasons: [{code: "details_changed", label: "资料已修改", resolvable: true}]};
  const result = savedDraft(draft, updated, 2);
  assert.equal(result.version, 5);
  assert.equal(result.dirty, false);
  assert.equal(result.handleOpen, true);
  assert.equal(result.searchOpen, true);
  assert.deepEqual(missingFields(result, updated), []);
});

test("deferred stays pending, formal and rejected are not editable pending records", () => {
  assert(pending({status: "deferred"}));
  assert(!pending({status: "confirmed"}));
  assert(!pending({status: "rejected"}));
});
