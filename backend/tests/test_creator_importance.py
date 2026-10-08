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
    assert facts.rank(history)["priority"]["primary_at"] is None
    assert facts.rank(history)["grounds"] == ["近期无重点事项", "已有历史合作记录"]
    next_pack = _pack(("cm", "Let's do the Acme video again next month."), ("creator", "Yes, confirmed."))
    for message in next_pack["messages"]:
        message["created_at"] = message["created_at"].replace("2026-08", "2026-09")
    repeat = facts.validate({"facts": [_fact("agreed", ["E0", "E1"], key=key)]}, next_pack, history)
    ranked = facts.rank(facts.merge_facts(history, repeat), datetime(2026, 10, 8, tzinfo=UTC))
    assert ranked["tier"] == "normal" and ranked["phase"] == "ongoing" and ranked["needs_update"]
    assert ranked["last_progress_at"] == "2026-09-02T10:00:00+00:00"
    assert ranked["priority"]["primary_at"] == "2026-09-02T10:00:00+00:00"
    assert ranked["priority"]["historical"]
    assert ranked["grounds"] == ["近期无重点事项", "已有历史合作记录"]


def test_weak_claims_and_unrelated_cancellations_do_not_overwrite_valid_facts():
    pack = _pack(("cm", "Every week, we will have the agreed Acme live."),
                 ("creator", "Confirmed, I'll host that Acme live every week."),
                 ("creator", "The other Beta campaign has been cancelled."),
                 ("creator", "The Acme live arrangement is cancelled too."))
    current = facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)
    weak = facts.validate({"facts": [_fact("agreed", ["E0"], key=current[0]["key"])]}, pack, current)
    assert weak[0]["kind"] == "intent"
    cancelled = facts.validate({"facts": [_fact("cancelled", ["E2"], obj="Beta summer campaign")]}, pack, current)
    assert facts.rank(facts.merge_facts(current, [*weak, *cancelled]),
                      datetime(2026, 8, 5, tzinfo=UTC))["tier"] == "focus"
    cancelled_target = facts.validate({"facts": [_fact("cancelled", ["E3"], key=current[0]["key"],
                                                     obj="Acme weekly live arrangement")]}, pack, current)
    assert cancelled_target[0]["object"] == current[0]["object"]
    assert cancelled_target[0]["key"] == current[0]["key"]
    assert facts.rank(facts.merge_facts(current, cancelled_target))["tier"] == "normal"
    with pytest.raises(ValueError, match="previous object"):
        facts.validate({"facts": [_fact("cancelled", ["E3"], key="unknown", obj=current[0]["object"])]},
                       pack, current)
    for invalid in (" ", "x" * 161):
        with pytest.raises(ValueError, match="business object"):
            facts.validate({"facts": [_fact("cancelled", ["E3"], key=current[0]["key"], obj=invalid)]},
                           pack, current)
    solo = facts.validate({"facts": [_fact("completed", ["E2"])]}, pack)
    assert solo[0]["kind"] == "intent" and facts.rank(solo)["tier"] == "normal"


def test_fact_protocol_rejects_unknown_and_third_party_evidence():
    assert facts.PROMPT_VERSION == "creator-importance-facts-v2.0.2"
    assert facts.RULE_VERSION == "creator-importance-v2.0"
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
    assert facts.rank([], readable=True)["tier"] == "normal"


def test_current_opportunity_and_bilateral_business_days_drive_maintenance_not_volume():
    pack = _pack(("cm", "The Acme autumn campaign has a concrete £500 video offer."),
                 ("creator", "Please send the Acme brief; I can discuss dates."),
                 ("cm", "The sample tracking number is ready."),
                 ("creator", "Thanks, I received the sample tracking update."))
    opportunity = facts.validate({"facts": [_fact("opportunity", ["E0", "E1"])]}, pack)
    ranked = facts.rank(opportunity, datetime(2026, 8, 5, tzinfo=UTC))
    assert ranked["tier"] == "focus" and ranked["primary_at"] == "2026-08-02T10:00:00+00:00"
    assert ranked["priority"] == {
        "primary_at": "2026-08-02T10:00:00+00:00",
        "business_at": "2026-08-02T10:00:00+00:00",
        "business_days": ["2026-08-01", "2026-08-02"],
        "historical": False,
    }
    assert ranked["grounds"] == [
        "Acme autumn skincare video：具体机会",
        "近30天业务交流2天 · 最近推进08-02",
    ]

    later = datetime(2026, 9, 10, 12, tzinfo=UTC)
    assert facts.rank(opportunity, later)["tier"] == "normal"
    progress_pack = _pack(("cm", "Let's settle the Acme quotation and filming date."),
                          ("creator", "The quote works; I can film Acme next Tuesday."))
    progress_pack["messages"][0]["created_at"] = "2026-09-09T10:00:00+00:00"
    progress_pack["messages"][1]["created_at"] = "2026-09-10T10:00:00+00:00"
    recent_interaction = facts.validate({"facts": [
        _fact("interaction", ["E0", "E1"], key=opportunity[0]["key"])
    ]}, progress_pack, opportunity)
    unchanged = facts.rank(facts.merge_facts(opportunity, recent_interaction), later)
    assert unchanged["tier"] == "normal"
    assert unchanged["priority"]["primary_at"] == "2026-08-02T10:00:00+00:00"
    progress = facts.validate({"facts": [
        _fact("progress", ["E0", "E1"], key=opportunity[0]["key"])
    ]}, progress_pack, opportunity)
    refreshed = facts.rank(facts.merge_facts(opportunity, progress), later)
    assert refreshed["tier"] == "focus" and refreshed["phase"] == "intent"
    assert refreshed["priority"]["primary_at"] == "2026-09-10T10:00:00+00:00"

    future = facts.rank(opportunity, datetime(2026, 7, 31, tzinfo=UTC))
    assert future["tier"] == "normal" and future["recent_business_dates"] == []
    assert future["priority"]["primary_at"] is None

    future_pack = _pack(("cm", "The Beta winter campaign has a concrete video offer."),
                        ("creator", "Please send the Beta brief; I can discuss dates."))
    for message in future_pack["messages"]:
        message["created_at"] = message["created_at"].replace("2026-08", "2026-09")
    future_opportunity = facts.validate({"facts": [
        _fact("opportunity", ["E0", "E1"], obj="Beta winter video")
    ]}, future_pack)
    mixed = facts.rank(facts.merge_facts(opportunity, future_opportunity),
                       datetime(2026, 8, 5, tzinfo=UTC))
    assert mixed["tier"] == "focus"
    assert mixed["priority"]["primary_at"] == "2026-08-02T10:00:00+00:00"

    interaction = facts.validate({"facts": [_fact("interaction", ["E2", "E3"], obj="Acme sample tracking")]},
                                 pack)
    assert facts.prompt_previous(interaction) == []
    support = facts.rank(interaction, datetime(2026, 8, 5, tzinfo=UTC))
    assert support["tier"] == "normal" and support["recent_business_dates"] == ["2026-08-03", "2026-08-04"]
    assert support["priority"]["primary_at"] is None  # Frequent support is not an opportunity score.
    assert support["grounds"] == ["近期无重点事项"]

    one_sided = _pack(("cm", "A one-way Acme campaign announcement."))
    downgraded = facts.validate({"facts": [_fact("opportunity", ["E0"])]}, one_sided)
    assert downgraded[0]["kind"] == "intent" and not downgraded[0]["business_dates"]
    assert facts.validate({"facts": [_fact("interaction", ["E0"])]}, one_sided) == []

    cancelled = facts.validate({"facts": [_fact("cancelled", ["E1"], key=opportunity[0]["key"])]},
                               pack, opportunity)
    closed = facts.rank(facts.merge_facts(opportunity, cancelled), datetime(2026, 8, 5, tzinfo=UTC))
    assert closed["tier"] == "normal" and closed["priority"]["primary_at"] is None


def test_ongoing_needs_this_creators_recurring_delivery_not_recurring_subsidies():
    cases = [
        ("I've activated DJI subsidy and links.",
         "Did we find out if we could get live exclusives on all DJI products?", "intent"),
        ("I've activated DJI subsidy and links.",
         "Would I be able to get live exclusive on all DJI products?", "intent"),
        ("We can arrange flash sales everyday for Roborock.",
         "I’ll be live with it tonight see how we go", "agreed"),
        ("We can arrange flash sales everyday.",
         "I'll be live tonight; flash sales everyday would help.", "agreed"),
        ("每天安排闪购价。", "我会直播今晚这一场，每天有闪购价。", "agreed"),
        ("每天安排优惠券。", "我会直播一场，每天有优惠券。", "agreed"),
        ("每天安排优惠券。", "每天有优惠券我直播一场。", "agreed"),
        ("Acme weekly lives are agreed.", "I will do our weekly lives", "ongoing"),
        ("Acme weekly lives are agreed.", "I'll host the Acme live every week.", "ongoing"),
        ("Acme weekly lives are agreed.", "I host the Acme live every week.", "ongoing"),
        ("Acme weekly lives are agreed.", "I go live every Friday.", "ongoing"),
        ("Acme weekly lives are agreed.", "我每周直播两场。", "ongoing"),
        ("Acme weekly lives are agreed.", "我会直播每周两场。", "ongoing"),
        ("Acme weekly lives are agreed.", "I will do weekly lives. Is that okay?", "ongoing"),
        ("Acme weekly lives are agreed.", "My assistant will host the Acme live every week.", "intent"),
        ("Acme weekly lives are agreed.", "I will ask my assistant to host the Acme live every week.", "intent"),
        ("Acme weekly lives are agreed.", "I will not host the Acme live every week.", "intent"),
        ("Acme weekly lives are agreed.", "Will I host the Acme live every week?", "intent"),
        ("Acme weekly lives are agreed.", "Would I host the Acme live every week", "intent"),
    ]
    for cm, creator, expected in cases:
        pack = _pack(("cm", cm), ("creator", creator))
        result = facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)
        assert result[0]["kind"] == expected, creator
        assert facts.rank(result, datetime(2026, 8, 5, tzinfo=UTC))["tier"] == {
            "ongoing": "focus", "agreed": "focus", "intent": "normal",
        }[expected]

    pack = _pack(("cm", "Please host the Acme live every week."), ("creator", "Yes, agreed."))
    pack["chats"] = {"c1": {"private": True}}
    pack["messages"][1]["created_at"] = "2026-08-01T10:01:00+00:00"
    assert facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)[0]["kind"] == "ongoing"
    assert facts.validate({"facts": [_fact("ongoing", ["E1"])]}, pack)[0]["kind"] == "intent"
    pack["chats"]["c1"]["private"] = False
    assert facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)[0]["kind"] == "intent"
    pack["messages"][1]["reply_to"] = "E0"
    assert facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)[0]["kind"] == "ongoing"
    pack["messages"][0]["text"] = "We'll arrange flash sales everyday for Acme."
    assert facts.validate({"facts": [_fact("ongoing", ["E0", "E1"])]}, pack)[0]["kind"] == "intent"


def test_completion_needs_positive_execution_and_accepts_linked_natural_answers():
    pack = _pack(("cm", "Acme autumn skincare video on Friday is agreed."),
                 ("creator", "Confirmed, I agree."), ("creator", "I'm happy."),
                 ("cm", "Have you published the Acme video?"), ("creator", "Yes, done."),
                 ("creator", "My friend published her video."),
                 ("creator", "3 Acme videos completed."),
                 ("creator", "https://www.tiktok.com/@creator/video/123"))
    established = facts.validate({"facts": [_fact("agreed", ["E0", "E1"])]}, pack)
    key = established[0]["key"]
    for ref in ("E2", "E5"):
        weak = facts.validate({"facts": [_fact("completed", [ref], key=key)]}, pack, established)
        assert weak[0]["kind"] == "intent"
        assert facts.rank(facts.merge_facts(established, weak))["last_progress_at"] == established[0]["occurred_at"]
    plural = facts.validate({"facts": [_fact("completed", ["E6"], key=key)]}, pack, established)
    assert plural[0]["kind"] == "completed"
    link_only = facts.validate({"facts": [_fact("completed", ["E7"], key=key)]}, pack, established)
    assert link_only[0]["kind"] == "intent"
    same_response = facts.validate({"facts": [
        _fact("agreed", ["E0", "E1"]), _fact("completed", ["E6"]),
    ]}, pack)
    same_agreed = next(fact for fact in same_response if fact["kind"] == "agreed")
    same_completed = next(fact for fact in same_response if fact["refs"] == ["E6"])
    assert same_completed["kind"] == "completed" and same_completed["key"] == same_agreed["key"]
    pack["chats"] = {"c1": {"private": True}}
    pack["messages"][4]["created_at"] = "2026-08-04T10:01:00+00:00"
    natural = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack, established)
    assert natural[0]["kind"] == "completed"
    pack["chats"]["c1"]["private"] = False
    group = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack, established)
    assert group[0]["kind"] == "intent"
    pack["chats"]["c1"]["private"] = True
    pack["messages"][4]["chat_id"] = "other"
    unrelated = facts.validate({"facts": [_fact("completed", ["E3", "E4"], key=key)]}, pack, established)
    assert unrelated[0]["kind"] == "intent"
    pack["messages"][4]["chat_id"] = "c1"
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
    assert facts.rank(facts.merge_facts(established, [*cancelled, *yes]))["tier"] == "normal"
