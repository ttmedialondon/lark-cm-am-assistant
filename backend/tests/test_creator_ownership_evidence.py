"""Critical scoped-input, false-pair and immutable-reference checks; no paid calls."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app import creator_ownership_evidence as evidence
from app.crypto import message_body_cipher


@pytest.fixture
def history(pg, monkeypatch):
    ids = {name: uuid4() for name in ("cm", "owner", "creator", "other")}
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    cipher = message_body_cipher(settings.token_encryption_key)
    for name in ("cm", "owner"):
        pg.execute(
            "INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,body_consent_at) "
            "VALUES(%s,%s,%s,'test',NOW()-INTERVAL '1 day')",
            (ids[name], "ou_" + name, "Remi" if name == "cm" else name),
        )
    for name in ("cm", "creator", "other"):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (ids[name], name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   ("ou_" + name, ids[name], "Alice Creator" if name == "creator" else name))
    for chat, mode in (("private", "p2p"), ("group", "group")):
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'PRIVATE NAME',%s)", (chat, mode))
        for who in ("cm", "owner"):
            pg.execute(
                "INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) VALUES(%s,%s,%s)",
                (ids[who], chat, "ou_creator" if mode == "p2p" else None),
            )
    counter = 0

    def add(sender, body, *, chat="private", observer="cm", when=None, parent=None, mentions=None, kind="text"):
        nonlocal counter
        counter += 1
        message_id = "msg_" + str(counter).zfill(4)
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,body_ciphertext,"
            "message_create_time,parent_id,mentions_json) VALUES(%s,%s,%s,'user',%s,%s,%s,%s,%s)",
            (chat, message_id, "ou_" + sender, kind, cipher.encrypt(json.dumps({"text": body})),
             when or datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=counter), parent, Jsonb(mentions)),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,%s,%s)",
            (ids[observer], chat, message_id),
        )
        return message_id

    monkeypatch.setattr(evidence, "connection", lambda: nullcontext(pg))
    return ids, settings, add


def test_private_pack_is_cm_scoped_redacted_and_all_refs_remain_exact(pg, history):
    ids, settings, add = history
    u = add("creator", "Remi can you arrange this campaign for me? " + "Detailed campaign requirements. " * 35)
    add("cm", "Alice Creator, I am your manager and will arrange your campaign. "
        "Contact: private@example.com; handle: @privatehandle; "
        "https://example.com/products/123?access_token=SECRET", parent=u)
    add("creator", "OTHER_AUTHORIZATION_SECRET campaign message", observer="owner")
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert result["eligible"] and result["stats"]["strong_pair"]
    packed = json.dumps(result["pack"])
    assert not any(value in packed for value in (
        "OTHER_AUTHORIZATION_SECRET", "private@example.com", "privatehandle", "SECRET", "Alice Creator", "PRIVATE NAME",
    ))
    assert len(result["pack"]["messages"][0]["text"]) > 500
    assert evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings)
    assert not evidence.revalidate_refs(pg, result["refs"][:-1], ids["creator"], ids["cm"], settings)
    assert not evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["owner"], settings)
    for sql, params in (
        ("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id=%s",
         (message_body_cipher(settings.token_encryption_key).encrypt('{"text":"changed"}'), u)),
        ("UPDATE chat_messages SET parent_id='changed' WHERE message_id=%s", (u,)),
        ("UPDATE chat_messages SET mentions_json='[{\"key\":\"@_user_1\",\"id\":\"ou_other\"}]' "
         "WHERE message_id=%s", (u,)),
        ("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (ids["cm"],)),
        ("UPDATE authorizations SET body_consent_at=NOW()+INTERVAL '1 day' WHERE id=%s", (ids["cm"],)),
        ("DELETE FROM authorization_message_observations WHERE authorization_id=%s AND message_id=%s", (ids["cm"], u)),
        ("UPDATE lark_identities SET contact_id=%s WHERE open_id='ou_creator'", (ids["other"],)),
    ):
        pg.execute("SAVEPOINT changed_ref")
        pg.execute(sql, params)
        assert not evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings)
        assert not evidence.revalidate_refs(
            pg, result["refs"], ids["creator"], ids["cm"], settings, check_history=False)
        pg.execute("ROLLBACK TO SAVEPOINT changed_ref")
        pg.execute("RELEASE SAVEPOINT changed_ref")
    # New unrelated chats do not invalidate this pair, but a later handover does.
    pg.execute("INSERT INTO chats(chat_id,chat_mode) VALUES('unrelated','p2p')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
               "VALUES(%s,'unrelated','ou_other')", (ids["cm"],))
    add("cm", "I will check your product", chat="unrelated")
    assert evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings)
    add("cm", "A different manager will take over your campaigns", when=datetime(2025, 1, 1, tzinfo=UTC))
    assert not evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings)
    # A list may still display valid old quotes; only the full check proves business freshness.
    assert evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings, check_history=False)


def test_group_co_presence_unknown_mentions_and_broadcast_do_not_create_direct_pair(pg, history):
    ids, settings, add = history
    u = add("creator", "Can someone help me get a product sample?", chat="group")
    add("cm", "The campaign launches on Friday.", chat="group")
    add("cm", "Hello everyone, all creators can request product samples.", chat="group")
    add("creator", "@_user_1 Please consider my account for a voucher", chat="group", mentions=[
        {"key": "@_user_1", "id": "ou_cm", "id_type": "user_id", "name": "Remi"},
    ])
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert not result["eligible"] and result["reason_code"] == "no_direct_exchange"
    pg.execute("SAVEPOINT weak_turns")
    add("creator", "Please arrange the Garnier campaign", chat="group")
    add("other", "OTHERS_BODY_SECRET the Garnier product is ready", chat="group")
    add("cm", "I will arrange the Garnier campaign", chat="group")
    assert not evidence.build_evidence(ids["creator"], ids["cm"], settings)["eligible"]
    add("creator", "Please arrange the Garnier campaign", chat="group")
    add("cm", "I will arrange the Garnier campaign", chat="group")
    weak = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert weak["eligible"] and weak["stats"]["weak_pair"] and not weak["stats"]["strong_pair"]
    assert "OTHERS_BODY_SECRET" not in json.dumps(weak["pack"])
    pg.execute("ROLLBACK TO SAVEPOINT weak_turns")
    pg.execute("RELEASE SAVEPOINT weak_turns")
    pg.execute("SAVEPOINT split_turns")
    pg.execute("INSERT INTO chats(chat_id,chat_mode) VALUES('split','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'split')", (ids["cm"],))
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    add("creator", "Can you help with this request?", chat="split", when=moment)
    anchor = add("cm", "@_user_1", chat="split", when=moment + timedelta(seconds=30), mentions=[
        {"key": "@_user_1", "id": "ou_creator", "id_type": "open_id", "name": "Alice Creator"},
    ])
    tail = add("cm", "I can apply for LE subsidies.", chat="split", when=moment + timedelta(seconds=50))
    split = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert split["eligible"] and split["stats"]["strong_pair"]
    tail_ref = next(r["label"] for r in split["refs"] if r["message_id"] == tail)
    anchor_ref = next(r["label"] for r in split["refs"] if r["message_id"] == anchor)
    assert next(m for m in split["pack"]["messages"] if m["ref"] == tail_ref)["continuation_of_ref"] == anchor_ref
    # No inheriting an address across another participant, beyond two minutes, or to a different target.
    pg.execute("SAVEPOINT interrupted_tail")
    add("other", "A separate discussion", chat="split", when=moment + timedelta(seconds=40))
    assert not evidence.build_evidence(ids["creator"], ids["cm"], settings)["eligible"]
    pg.execute("ROLLBACK TO SAVEPOINT interrupted_tail")
    pg.execute("RELEASE SAVEPOINT interrupted_tail")
    for sql, params in (
        ("UPDATE chat_messages SET message_create_time=%s WHERE message_id=%s",
         (moment + timedelta(minutes=3), tail)),
        ("UPDATE chat_messages SET mentions_json=%s WHERE message_id=%s",
         (Jsonb([{"key": "@_user_1", "id": "ou_other", "id_type": "open_id", "name": "Other"}]), anchor)),
        ("UPDATE chat_messages SET mentions_json=%s WHERE message_id=%s",
         (Jsonb([{"key": "@_user_1", "id": "ou_other", "id_type": "open_id", "name": "Other"}]), tail)),
    ):
        pg.execute("SAVEPOINT changed_tail")
        pg.execute(sql, params)
        assert not evidence.build_evidence(ids["creator"], ids["cm"], settings)["eligible"]
        pg.execute("ROLLBACK TO SAVEPOINT changed_tail")
        pg.execute("RELEASE SAVEPOINT changed_tail")
    assert all(evidence.BUSINESS.search(word) for word in ("subsidy", "subsidies", "LE", "FS", "GMV", "flash sale"))
    pg.execute("ROLLBACK TO SAVEPOINT split_turns")
    pg.execute("RELEASE SAVEPOINT split_turns")
    # Explicit reply to the actual creator changes the evidence, not the display name.
    add("cm", "I will arrange the requested sample for you", chat="group", parent=u)
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert result["eligible"] and result["stats"]["strong_pair"]
    assert "group" in result["stats"]["selection_tags"]
    assert not any(m["speaker"] == "other" for m in result["pack"]["messages"])


def test_single_sided_business_is_skipped_but_explicit_direct_responsibility_is_sent(pg, history):
    ids, settings, add = history
    add("creator", "Please approve a product sample for my next live")
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert not result["eligible"] and result["reason_code"] == "insufficient_two_way_material"
    pg.execute("DELETE FROM authorization_message_observations")
    pg.execute("DELETE FROM chat_messages")
    add("cm", "I am your new manager and will manage your campaigns")
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert result["eligible"] and result["stats"]["explicit_responsibility"]
    assert not result["stats"]["strong_pair"]
    assert result["stats"]["messages"] == 1
    pg.execute("DELETE FROM authorization_message_observations")
    pg.execute("DELETE FROM chat_messages")
    add("cm", "I am your new manager and will manage your campaigns", chat="group")
    assert not evidence.build_evidence(ids["creator"], ids["cm"], settings)["eligible"]
    add("cm", "@_user_1 I am your new manager and will manage your campaigns", chat="group", mentions=[
        {"key": "@_user_1", "id": "ou_creator", "id_type": "open_id", "name": "Alice Creator"},
    ])
    assert evidence.build_evidence(ids["creator"], ids["cm"], settings)["stats"]["explicit_responsibility"]


def test_historical_negative_latest_and_oldest_turns_survive_bounded_selection(pg, history):
    ids, settings, add = history
    start = datetime(2020, 1, 1, tzinfo=UTC)
    first, last = None, None
    for i in range(15):
        moment = start + timedelta(days=31 * i)
        message = add("creator", "Please arrange my product campaign. " + "Relevant business detail. " * 30,
                      when=moment)
        first = first or message
        last = add("cm", "I will arrange your product campaign. " + "Confirmed business detail. " * 30,
                   when=moment + timedelta(minutes=1), parent=message)
    handoff = add("cm", "Your new manager will take over; I am no longer responsible for you.",
                  when=start + timedelta(days=600))
    result = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert result["eligible"] and result["stats"]["negative_signal"]
    assert result["stats"]["selection_limited"]
    assert result["stats"]["messages"] <= 40 and result["stats"]["segments"] <= 6
    assert result["stats"]["body_chars"] <= 16_000
    assert {first, last, handoff} <= {r["message_id"] for r in result["refs"]}
    assert evidence.revalidate_refs(pg, result["refs"], ids["creator"], ids["cm"], settings)
    same = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert same["fingerprint"] == result["fingerprint"]
    assert "history_handoff" in result["stats"]["selection_tags"]

    # An oversized later handover must not leave only the old positive business exchanges eligible.
    oversized_handoff = "I am no longer responsible for you. " + "Complete handover details. " * 900
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id=%s",
               (message_body_cipher(settings.token_encryption_key).encrypt(json.dumps({"text": oversized_handoff})),
                handoff))
    blocked = evidence.build_evidence(ids["creator"], ids["cm"], settings)
    assert not blocked["eligible"] and blocked["reason_code"] == "material_over_limit"
    assert blocked["stats"]["selection_limited"] and blocked["refs"] == []
    assert blocked["pack"]["messages"] == []


def test_model_verdicts_cannot_borrow_uncited_global_flags_or_invent_counter_evidence():
    from app.creator_ownership import _validate_result

    def message(ref, speaker, text, *, day=1, conversation="C1", relation="private"):
        return {"ref": ref, "speaker": speaker, "text": text, "conversation": conversation,
                "time": f"2024-01-{day:02d}T10:{int(ref[1:]):02d}:00+00:00", "relation": relation,
                "mentions": [], "parent_ref": None}

    items = [message("M1", "U1", "Can you approve this sample?"),
             message("M2", "CM1", "I will approve this sample."),
             message("M3", "CM1", "I am your manager", conversation="C2"),
             message("M4", "CM1", "I am no longer responsible for you.", conversation="C3")]
    pack = {"messages": items, "meta": {"strong_pair": True, "explicit_responsibility": True}}
    baseline = {"outcome": "suggested", "confidence": 0.99, "reason": "测试判断",
                "responsibility_basis": "ongoing_management", "evidence_refs": ["M1", "M2"],
                "counter_evidence_refs": [], "conflicts": [], "missing_info": []}

    def outcome(**changes):
        return _validate_result(json.dumps(baseline | changes), pack)["outcome"]

    assert outcome() == "uncertain"  # One request + one favour is not continuing management.
    assert outcome(responsibility_basis="explicit_responsibility", evidence_refs=["M1"]) == "uncertain"
    assert outcome(responsibility_basis="explicit_responsibility", evidence_refs=["M3"]) == "suggested"
    assert outcome(outcome="not_related", responsibility_basis="transferred",
                   counter_evidence_refs=["M1"]) == "uncertain"
    assert outcome(outcome="not_related", responsibility_basis="ended", evidence_refs=["M4"],
                   counter_evidence_refs=["M4"]) == "not_related"
    pack["messages"] = [*items[:2], message("M5", "U1", "Can you arrange my voucher?", day=2, conversation="C4"),
                        message("M6", "CM1", "I will arrange your voucher.", day=2, conversation="C4")]
    # The guard requires complete cited exchanges, not a formal title or reassuring sampling metadata.
    pack["meta"].update(selection_limited=True, negative_signal=True,
                        history_latest_at="2024-02-01T10:00:00+00:00", selected_latest_at=pack["messages"][-1]["time"])
    assert outcome(evidence_refs=["M2", "M6"]) == "uncertain"
    assert outcome(confidence=0.94, evidence_refs=["M1", "M2", "M5", "M6"]) == "uncertain"
    assert outcome(evidence_refs=["M6", "M5", "M2", "M1"]) == "suggested"
    pack["messages"].append(message("M7", "U1", "I left the agency but still want campaign products.", day=3))
    assert outcome(outcome="not_related", responsibility_basis="ended", evidence_refs=["M7"],
                   counter_evidence_refs=["M7"]) == "uncertain"  # CAP departure alone does not end CM responsibility.
    assert outcome(evidence_refs=["M1", "M2", "M5", "M6"], conflicts=["实际负责人不明确"]) == "uncertain"
    for item in pack["messages"]:
        item["relation"] = "group"
    assert outcome(evidence_refs=["M1", "M2", "M5", "M6"]) == "uncertain"  # Weak group adjacency is insufficient.
    pack["messages"] = [message("M1", "U1", "Thanks!"), message("M2", "CM1", "No worries!")]
    assert outcome() == "uncertain"
