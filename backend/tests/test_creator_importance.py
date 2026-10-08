"""Grounded cooperation lifecycle rules; queue and authorization checks extend this file."""

import json
from datetime import UTC, datetime

import pytest

from app import creator_importance_facts as facts


def _pack(*texts):
    return {"creator_id": "creator", "cm_id": "authorization", "cm_open_id": "ou_cm", "messages": [
        {"ref": f"E{index}", "message_id": f"m{index}", "chat_id": "c1", "role": role,
         "author_id": "ou_cm" if role == "cm" else "ou_creator",
         "contact_id": "cm_contact" if role == "cm" else "creator", "text": text,
         "created_at": f"2026-08-{index + 1:02}T10:00:00+00:00"}
        for index, (role, text) in enumerate(texts)
    ]}


def _fact(kind, refs, *, key=None, obj="Acme autumn skincare video"):
    return {"kind": kind, "object": obj, "previous_key": key, "refs": refs}


def test_fact_lifecycle_is_grounded_deduplicated_and_not_a_message_score():
    pack = _pack(("cm", "Let's agree to an Acme autumn skincare video on Friday."),
                 ("creator", "Confirmed, I'll post the Acme video on Friday."),
                 ("creator", "The sample arrived. I will shoot the video tomorrow."),
                 ("creator", "The Acme video is published, here is its link."))
    agreed = facts.validate(json.dumps({"facts": [_fact("agreed", ["E0", "E1"])]}), pack)
    key = agreed[0]["key"]
    assert facts.rank(agreed)["tier"] == "normal"
    assert facts.rank(agreed)["phase"] == "single"
    assert agreed[0]["occurred_at"] == "2026-08-02T10:00:00+00:00"
    sample = facts.validate({"facts": [_fact("completed", ["E2"], key=key)]}, pack, agreed)
    assert sample[0]["kind"] == "intent"
    unchanged = facts.merge_facts(agreed, facts.validate({"facts": []}, pack, agreed))
    assert unchanged == agreed
    assert facts.merge_facts(agreed, agreed) == agreed
    completed = facts.validate({"facts": [_fact("completed", ["E3"], key=key)]}, pack, agreed)
    history = facts.merge_facts(agreed, completed)
    assert facts.rank(history)["phase"] == "history"
    next_pack = _pack(("cm", "Let's do the Acme video again next month."), ("creator", "Yes, confirmed."))
    for message in next_pack["messages"]:
        message["created_at"] = message["created_at"].replace("2026-08", "2026-09")
    repeat = facts.validate({"facts": [_fact("agreed", ["E0", "E1"], key=key)]}, next_pack, history)
    ranked = facts.rank(facts.merge_facts(history, repeat), datetime(2026, 10, 8, tzinfo=UTC))
    assert ranked["tier"] == "focus" and ranked["phase"] == "ongoing" and ranked["needs_update"]
    assert ranked["last_progress_at"] == "2026-09-02T10:00:00+00:00"
    assert len(ranked["grounds"]) <= 2


def test_weak_claims_and_unrelated_cancellations_do_not_overwrite_valid_facts():
    pack = _pack(("cm", "Every week, we will have the agreed Acme live."),
                 ("creator", "Confirmed, I'll host that Acme live every week."),
                 ("creator", "The other Beta campaign has been cancelled."),
                 ("creator", "The Acme live arrangement is cancelled too."))
    current = facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)
    weak = facts.validate({"facts": [_fact("agreed", ["E0"], key=current[0]["key"])]}, pack, current)
    assert weak[0]["kind"] == "intent"
    cancelled = facts.validate({"facts": [_fact("cancelled", ["E2"], obj="Beta summer campaign")]}, pack, current)
    assert facts.rank(facts.merge_facts(current, [*weak, *cancelled]))["tier"] == "focus"
    cancelled_target = facts.validate({"facts": [_fact("cancelled", ["E3"], key=current[0]["key"])]}, pack, current)
    assert facts.rank(facts.merge_facts(current, cancelled_target))["tier"] == "pending"
    with pytest.raises(ValueError, match="previous object"):
        facts.validate({"facts": [_fact("cancelled", ["E3"], key=current[0]["key"], obj="Other object")]},
                       pack, current)
    solo = facts.validate({"facts": [_fact("completed", ["E2"])]}, pack)
    assert solo[0]["kind"] == "intent" and facts.rank(solo)["tier"] == "pending"


def test_fact_protocol_rejects_unknown_and_third_party_evidence():
    pack = _pack(("creator", "I'd like to hear about the Acme product."))
    with pytest.raises(ValueError, match="refs"):
        facts.validate({"facts": [_fact("intent", ["missing"])]}, pack)
    pack["messages"][0]["contact_id"] = "another creator"
    with pytest.raises(ValueError, match="source author"):
        facts.validate({"facts": [_fact("intent", ["E0"])]}, pack)
    pack["messages"][0].update(contact_id="creator", context_complete=False)
    with pytest.raises(ValueError, match="incomplete reply"):
        facts.validate({"facts": [_fact("intent", ["E0"])]}, pack)
    schema = facts.output_format(["E0"])["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts"]["maxItems"] == facts.MAX_FACTS
    assert facts.rank([])["tier"] == "pending"


def test_completion_needs_positive_execution_and_accepts_linked_natural_answers():
    pack = _pack(("cm", "Acme autumn skincare video on Friday is agreed."),
                 ("creator", "Confirmed, I agree."), ("creator", "I'm happy."),
                 ("cm", "Have you published the Acme video?"), ("creator", "Yes, done."),
                 ("creator", "My friend published her video."))
    established = facts.validate({"facts": [_fact("agreed", ["E0", "E1"])]}, pack)
    key = established[0]["key"]
    for ref in ("E2", "E5"):
        weak = facts.validate({"facts": [_fact("completed", [ref], key=key)]}, pack, established)
        assert weak[0]["kind"] == "intent"
        assert facts.rank(facts.merge_facts(established, weak))["last_progress_at"] == established[0]["occurred_at"]
    pack["messages"][4]["reply_to"] = "E3"
    yes = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack, established)
    assert yes[0]["kind"] == "completed"
    pack["messages"][3]["text"] = "Will you publish the Acme video tomorrow?"
    planned = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack, established)
    assert planned[0]["kind"] == "intent"
    pack["messages"][3]["text"] = "Have you published the Acme video?"
    pack["messages"][2]["text"] = "The Acme cooperation is cancelled."
    cancelled = facts.validate({"facts": [_fact("cancelled", ["E2"], key=key)]}, pack, established)
    after_cancel = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack,
                                 facts.merge_facts(established, cancelled))
    assert after_cancel[0]["kind"] == "intent"
    compact = facts.prompt_previous(facts.merge_facts(established, [*cancelled, *yes]))
    assert len(compact) <= 4 and all("refs" not in fact for fact in compact)
    assert {fact["kind"] for fact in compact} == {"agreed", "completed", "cancelled"}
    assert facts.rank(facts.merge_facts(established, [*cancelled, *yes]))["tier"] == "pending"
