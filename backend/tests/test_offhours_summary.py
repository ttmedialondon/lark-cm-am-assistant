from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

from app import (
    assistant_rules,
    creator_import_evidence,
    creator_imports,
    openai_client,
    summary_dialogues,
    summary_periods,
)
from app import offhours_summary as ai
from app.ai_feature_config import api_key_cipher, save_feature_config
from app.crypto import message_body_cipher


def test_windows_reject_invalid_or_nonexistent_london_times():
    start, end = ai._parse_window(ai.DEFAULT_START, ai.DEFAULT_END)
    assert start == datetime(2026, 9, 9, 17, tzinfo=UTC)
    assert end == datetime(2026, 9, 10, 11, tzinfo=UTC)
    for values in (
        (ai.DEFAULT_END, ai.DEFAULT_START),
        ("bad", ai.DEFAULT_END),
        ("2026-09-01T10:00", ai.DEFAULT_END),
        ("2026-03-29T01:30", "2026-03-29T03:00"),
    ):
        with pytest.raises(ai.SummaryInputError):
            ai._parse_window(*values)


def test_shift_windows_are_adjacent_and_follow_london_dst():
    for day, night_hours in [('2026-09-14', 16), ('2026-03-28', 15), ('2026-10-24', 17)]:
        work = summary_periods.window(day, 'work')
        off = summary_periods.window(day, 'off')
        assert work[1] == off[0]
        assert work[1] - work[0] == timedelta(hours=8)
        assert off[1] - off[0] == timedelta(hours=night_hours)
        next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date()
        assert off[1] == summary_periods.window(next_day, 'work')[0]


def sample_event(**overrides):
    return {
        "user": "U1",
        "handle": "@alice",
        "priority": "P2",
        "title": "申请样品",
        "summary": "达人申请新的样品。",
        "latest_progress": "尚未看到后续确认。",
        "handling_status": "unresolved",
        "handling_reason": "尚未看到完成结果。",
        "handling_evidence_refs": overrides.get("evidence_refs", ["M1"]),
        "reply_evidence_refs": [],
        "needs_review": False,
        "evidence_refs": ["M1"],
        "priority_basis": "content",
        **overrides,
    }


def pack():
    return {
        "users": {"U1": {"handles": ["@alice"], "name": "Alice"}},
        "refs": {"M1": {}},
        "history_complete": False,
        "pack": {
            "is_private": True,
            "messages": [
                {
                    "ref": "M1",
                    "speaker": "U1",
                    "text": "Sample please",
                    "in_window": True,
                    "unavailable_or_truncated": False,
                    "three_workday_deadline_reached": True,
                }
            ],
        },
    }


def test_missing_media_and_multi_handle_do_not_invent_risk_or_duplicate_account():
    data = pack()
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"events": [sample_event(evidence_refs=["unknown"])]}), data)
    data["users"]["U1"]["handles"].append("@alice_shop")
    result = ai._validate(json.dumps({"events": [sample_event()]}), data)
    assert result[0]["handle"] is None and result[0]["needs_review"]
    data["pack"]["messages"][0].update(unreadable=True, unavailable_or_truncated=True)
    result = ai._validate(json.dumps({"events": [sample_event(priority="P0")]}), data)
    assert result[0]["priority"] is None
    result = ai._validate(json.dumps({"events": [sample_event(handling_status="resolved")]}), data)
    assert result[0]["handling_status"] == "uncertain"
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"events": [sample_event(), sample_event()]}), data)
    old_output = ai._validate(json.dumps({"events": [sample_event(reason="旧版等级理由")]}), pack())[0]
    assert "reason" not in old_output and old_output["handling_reason"]


def test_reply_and_resolution_remain_independent_through_context_and_merge(monkeypatch):
    from app import assistant_worklist

    no_staff = ai._format(pack())["schema"]["properties"]["events"]["items"]["properties"]["reply_evidence_refs"]
    assert no_staff["maxItems"] == 0
    unreadable_only = pack()
    unreadable_only["pack"]["messages"][0]["unreadable"] = True
    assert ai._format(unreadable_only)["schema"]["properties"]["events"]["items"]["properties"][
        "waiting_since_ref"]["enum"] == [None]
    first = {"chat_id": "one", "message_id": "first", "created_at": "2026-10-12T08:00:00+00:00",
             "speaker": "U1", "is_staff": False, "text": "Please send the link by today"}
    assert summary_dialogues.merge_format({"E1": first})["schema"]["properties"]["reply_evidence_refs"]["maxItems"] == 0
    reminder = {**first, "message_id": "reminder", "created_at": "2026-10-12T09:00:00+00:00",
                "text": "Any update?"}
    metadata = assistant_worklist.decorate({"handling_status": "unresolved", "evidence": [first, reminder]},
                                          "2026-10-12T10:00:00+00:00", today=datetime(2026, 10, 12).date())
    assert metadata["waiting_since"] == first["created_at"] and metadata["waiting_seconds"] == 7200
    assert metadata["due_today"] and metadata["action_group"] == "reply"
    assert not assistant_worklist.decorate({"handling_status": "unresolved", "evidence": [first]},
        "2026-10-12T10:00:00+00:00", today=datetime(2026, 10, 13).date())["due_today"]
    origin = assistant_worklist.decorate({"handling_status": "unresolved", "evidence": [reminder],
        "waiting_evidence": [first]}, "2026-10-12T10:00:00+00:00")
    assert origin["waiting_since"] == first["created_at"]
    answered = {**first, "message_id": "answered", "created_at": "2026-10-12T08:30:00+00:00",
                "speaker": "CM", "is_staff": True, "text": "Here is the link"}
    metadata = assistant_worklist.decorate({"handling_status": "unresolved", "replied_by": ["CM"],
        "evidence": [first, reminder], "reply_evidence": [answered]}, "2026-10-12T10:00:00+00:00")
    assert metadata["waiting_since"] == reminder["created_at"] and metadata["action_group"] == "followup"
    assert not assistant_worklist.decorate({"handling_status": "resolved", "evidence": [first]},
                                          "2026-10-12T10:00:00+00:00")["due_today"]
    data = pack()
    data["pack"].update(is_private=False, unique_group_user="U1",
                        staff={"CM": {"name": "Remi"}, "CM2": {"name": "Shang"}})
    data["pack"]["messages"][0].update(message_id="request", text="Sample order ABC-123 please",
        mentions=[{"target": "unknown", "ambiguous": True}])
    for ref, speaker, text in (
        ("M2", "CM", "I'll look into sample order ABC-123 tomorrow"),
        ("M3", "CM2", "I can help check sample order ABC-123"),
        ("M4", "U1", "[附件或正文不可读取]"),
        ("M0", "CM", "[附件或正文不可读取]"),
    ):
        data["pack"]["messages"].append({"ref": ref, "message_id": ref, "speaker": speaker,
            "text": text, "in_window": True, "unreadable": ref in {"M4", "M0"},
            "unavailable_or_truncated": ref in {"M4", "M0"}})
    data["refs"] = {m["ref"]: {**m, "sender_name": {"CM": "Remi", "CM2": "Shang"}.get(m["speaker"], "Alice"),
        "is_staff": m["speaker"] in {"CM", "CM2"}, "chat_id": "group", "created_at": m["ref"]}
        for m in data["pack"]["messages"]}
    reply_schema = ai._format(data)["schema"]["properties"]["events"]["items"]["properties"]["reply_evidence_refs"]
    assert set(reply_schema["items"]["enum"]) == {"M2", "M3"}
    assert ai._format(data)["schema"]["properties"]["events"]["items"]["properties"][
        "waiting_since_ref"]["enum"] == ["M1", None]
    with pytest.raises(ai.InvalidModelOutput, match="reply evidence is not staff"):
        ai._validate(json.dumps({"events": [sample_event(reply_evidence_refs=[ref])
                                            for ref in ("M2", "M3", "M1")]}), data)
    pending = ai._validate(json.dumps({"events": [sample_event(
        handling_evidence_refs=["M1", "M4"], reply_evidence_refs=["M2", "M3"])]}), data)[0]
    assert pending["handling_status"] == "unresolved" and pending["replied_by"] == ["Remi", "Shang"]
    assert pending["action_group"] == "followup"
    origin = ai._validate(json.dumps({"events": [sample_event(waiting_since_ref="M1", evidence_refs=["M4"])]}),
                          data)[0]
    assert origin["waiting_since_ref"] == "M1" and "M1" in origin["evidence_refs"]
    for ref in ("M2", "M4", "M0", {"invented": "ref"}):
        with pytest.raises(ai.InvalidModelOutput, match="waiting"):
            ai._validate(json.dumps({"events": [sample_event(waiting_since_ref=ref)]}), data)
    waiting = ai._validate(json.dumps({"events": [sample_event(action_group="waiting",
        handling_evidence_refs=["M1"], reply_evidence_refs=["M2"])]}), data)[0]
    assert waiting["action_group"] == "waiting"
    unsupported_wait = ai._validate(json.dumps({"events": [sample_event(action_group="waiting")]}), data)[0]
    assert unsupported_wait["action_group"] == "followup"
    assert not pending["needs_review"]  # An unknown @ or optional attachment does not erase a readable request.
    pending["evidence"] = [data["refs"][r] for r in pending["evidence_refs"]]
    done_message = {"text": "Sample order ABC-123 delivered today", "speaker": "CM2", "is_staff": True,
                    "sender_name": "Shang", "chat_id": "group", "message_id": "done", "created_at": "M5"}
    done = {**pending, "evidence": [done_message]}
    refs = summary_dialogues.merged_evidence(pending, done)
    request_ref = next(r for r, m in refs.items() if m["message_id"] == "request")
    done_ref = next(r for r, m in refs.items() if m["message_id"] == "done")
    merge_reply_refs = summary_dialogues.merge_format(refs, pending, done)["schema"]["properties"][
        "reply_evidence_refs"]["items"]["enum"]
    assert {refs[ref]["message_id"] for ref in merge_reply_refs} == {"M2", "M3", "done"}
    assert set(summary_dialogues.merge_format(refs, pending, done)["schema"]["properties"][
        "waiting_since_ref"]["enum"]) == {request_ref, None}
    value = {"same": True, "basis_kind": "identifier", "basis_value": "ABC-123", "quote_a_ref": request_ref,
        "quote_b_ref": done_ref, "title": "样品订单", "summary": "样品已送达", "latest_progress": "同事已确认送达",
        "handling_status": "resolved", "handling_reason": "已确认对应样品送达", "handling_evidence_refs": [done_ref],
        "reply_evidence_refs": [r for r, m in refs.items() if m.get("is_staff")]}
    # Paid returns can survive only this exact schema correction, not new material or instructions.
    def unexpected_provider_call(*_args, **_kwargs):
        pytest.fail("saved provider response must not cause another API call")
    monkeypatch.setattr(openai_client, "call_openai", unexpected_provider_call)
    for chat_id, query, schema, valid, forged, unreadable_wait, all_refs in (
        ("chat", data["pack"], ai._format(data), {"events": [sample_event(reply_evidence_refs=["M2"])]},
         {"events": [sample_event(reply_evidence_refs=["M1"])]},
         {"events": [sample_event(evidence_refs=["M4", "M1"], waiting_since_ref="M4")]}, list(data["refs"])),
        (None, {"a": pending, "b": done, "evidence_messages": refs},
         summary_dialogues.merge_format(refs, pending, done),
         value, {**value, "reply_evidence_refs": [request_ref]},
         {**value, "waiting_since_ref": next(r for r, m in refs.items() if m["message_id"] == "M4")}, list(refs)),
    ):
        before = json.loads(json.dumps(schema))
        for old_reply_schema in (False, True):
            legacy = json.loads(json.dumps(schema))
            fields = legacy["schema"]["properties"]
            if chat_id:
                fields = fields["events"]["items"]["properties"]
            fields["waiting_since_ref"] = {"type": ["string", "null"], "enum": [*all_refs, None]}
            if old_reply_schema:
                fields["reply_evidence_refs"] = {
                    "type": "array", "items": {"type": "string", "enum": all_refs}, "maxItems": 20}
            instructions = "fixed paid event manuals"
            response = {"text": json.dumps(valid), "usage": {"input_tokens": 7}, "response_id": "paid-old-response"}
            checkpoint = {"response_received": {
                "signature": ai._hash([query, legacy, instructions]), "response": response}}
            job = {"chat_id": chat_id}
            assert summary_dialogues.request(job, None, None, query, schema, instructions,
                                             checkpoint=checkpoint) is response
            assert schema == before
            for changed_query, changed_rules, reply in (
                ({**query, "changed_material": True}, instructions, False),
                (query, instructions + " changed", False), (query, instructions, True),
            ):
                with pytest.raises(ai.SummaryInputError, match="调用材料或处理阶段已变化"):
                    summary_dialogues.request(job, None, None, changed_query, schema, changed_rules,
                                              checkpoint=checkpoint, reply=reply)
            with pytest.raises(openai_client.OpenAIRequestError, match="请求结果未知"):
                summary_dialogues.request(job, None, None, query, schema, instructions,
                                          checkpoint={"provider_call": {"id": "paid-without-received-return"}})
            for bad, error in ((forged, "reply evidence is not staff"), (unreadable_wait, "invalid waiting evidence")):
                bad_checkpoint = {"response_received": {
                    **checkpoint["response_received"], "response": {"text": json.dumps(bad)}}}
                restored = summary_dialogues.request(job, None, None, query, schema, instructions,
                                                    checkpoint=bad_checkpoint)
                with pytest.raises(ai.InvalidModelOutput, match=error):
                    if chat_id:
                        ai._validate(restored["text"], data)
                    else:
                        summary_dialogues.combine(restored["text"], pending, done)
            assert schema == before
    with pytest.raises(ai.InvalidModelOutput, match="reply evidence is not staff"):
        summary_dialogues.combine(json.dumps({**value, "reply_evidence_refs": [request_ref]}), pending, done)
    with pytest.raises(ai.InvalidModelOutput, match="outside selected events"):
        summary_dialogues.combine(json.dumps({**value, "handling_evidence_refs": ["other-candidate"]}), pending, done,
            messages={**refs, "other-candidate": {**done_message, "message_id": "unselected"}})
    merged = summary_dialogues.combine(json.dumps(value), pending, done)
    assert merged["handling_status"] == "resolved" and merged["replied_by"] == ["Remi", "Shang"]
    assert merged["action_group"] == "closed"
    outsider = {**done_message, "speaker": "U2", "is_staff": False, "message_id": "other-customer",
                "text": "My sample order ABC-123 too", "created_at": "M6"}
    mixed = {**pending, "evidence": [*pending["evidence"], outsider]}
    mixed_refs = summary_dialogues.merged_evidence(mixed, done)
    assert set(summary_dialogues.merge_format(mixed_refs, mixed, done)["schema"]["properties"][
        "waiting_since_ref"]["enum"]) == {
            next(r for r, m in mixed_refs.items() if m["message_id"] == "request"), None}
    wrong_origin = {**value, "handling_status": "unresolved",
        "quote_a_ref": next(r for r, m in mixed_refs.items() if m["message_id"] == "request"),
        "quote_b_ref": next(r for r, m in mixed_refs.items() if m["message_id"] == "done"),
        "handling_evidence_refs": [next(r for r, m in mixed_refs.items() if m["message_id"] == "request")],
        "reply_evidence_refs": [r for r, m in mixed_refs.items() if m.get("is_staff")],
        "waiting_since_ref": next(r for r, m in mixed_refs.items() if m["message_id"] == "other-customer")}
    with pytest.raises(ai.InvalidModelOutput, match="waiting"):
        summary_dialogues.combine(json.dumps(wrong_origin), mixed, done)
    # A later concrete counterexample reopens the issue without forgetting who replied.
    later = {**done, "evidence": [{**done_message, "text": "Sample order ABC-123 still not here",
             "speaker": "U1", "is_staff": False, "sender_name": "Alice", "message_id": "later", "created_at": "M6"}]}
    refs = summary_dialogues.merged_evidence(merged, later)
    later_ref = next(r for r, m in refs.items() if m["message_id"] == "later")
    value.update(quote_a_ref=next(r for r, m in refs.items() if m["message_id"] == "done"), quote_b_ref=later_ref,
        handling_status="unresolved", handling_reason="达人随后明确反馈尚未送达", handling_evidence_refs=[later_ref],
        reply_evidence_refs=[r for r, m in refs.items() if m.get("is_staff")])
    assert summary_dialogues.combine(json.dumps(value), merged, later)["handling_status"] == "unresolved"
    # Multi-person conversation boundaries prevent a colleague's unrelated broadcast being attributed to Alice.
    data["pack"].update(unique_group_user=None)
    data["users"]["U2"] = {"name": "Bob", "handles": []}
    data["pack"]["messages"] += [
        {"ref": "M5", "message_id": "bob", "speaker": "U2", "text": "My different order", "in_window": True},
        {"ref": "M6", "message_id": "broadcast", "speaker": "CM2", "text": "Latest campaign news", "in_window": True},
    ]
    with pytest.raises(ai.InvalidModelOutput, match="waiting"):
        ai._validate(json.dumps({"events": [sample_event(waiting_since_ref="M5")]}), data)
    data["refs"].update(M5={}, M6={})
    only_alice = {**data, "users": {"U1": data["users"]["U1"]}}
    assert ai._format(only_alice)["schema"]["properties"]["events"]["items"]["properties"][
        "waiting_since_ref"]["enum"] == ["M1", None]
    selected, _ = summary_dialogues.parts(data, "U1")
    assert "broadcast" not in {m["message_id"] for m in selected}
    # A request in a neighboring chunk still anchors a natural colleague response.
    data["pack"]["messages"] = data["pack"]["messages"][:3]
    selected, _ = summary_dialogues.parts(data, "U1")
    colleague = next(m for m in selected if m["speaker"] == "CM2")
    assert colleague["addressed_user"] == "U1"
    for m in selected:
        m["in_window"] = m["speaker"] == "CM2"
    scoped = {**data, "pack": {**data["pack"], "messages": selected}, "refs": {m["ref"]: {} for m in selected}}
    event = sample_event(evidence_refs=[colleague["ref"]], handling_evidence_refs=[colleague["ref"]],
                         reply_evidence_refs=[colleague["ref"]])
    assert ai._validate(json.dumps({"events": [event]}), scoped)[0]["replied_by"] == ["Shang"]
    data["pack"].update(users=data["users"], policy="shift-handling-v4")
    previous_chunks = {c["fingerprint"] for c in summary_dialogues.segments(data)}
    data["pack"]["policy"] = ai.POLICY_VERSION
    assert not previous_chunks & {c["fingerprint"] for c in summary_dialogues.segments(data)}
    # Untargeted multi-creator broadcasts stay as context, not one paid analysis per member.
    people = {f"U{i}": {"name": f"Creator {i}", "handles": []} for i in range(1, 128)}
    broadcast = {"ref": "B0", "message_id": "broadcast-0", "speaker": "CM", "is_staff": True,
                 "text": "New campaign is available", "in_window": True}
    raw = [{**broadcast, "ref": f"B{i}", "message_id": f"broadcast-{i}"} for i in range(8)]
    material = {"users": people, "refs": {m["ref"]: {} for m in raw}, "pack": {
        "policy": ai.POLICY_VERSION, "users": people, "staff": {"CM": {"name": "Remi"}},
        "is_private": False, "unique_group_user": None, "messages": raw}}
    assert summary_dialogues.parts(material, "U1")[0][0]["message_id"] == "broadcast-0"
    assert summary_dialogues.segments(material) == []
    reply = {"ref": "R", "message_id": "creator-reply", "speaker": "U1", "text": "Can I join this campaign?",
             "in_window": True, "parent_message_id": "broadcast-0"}
    material["pack"]["messages"] = [*raw, reply]
    material["refs"]["R"] = {}
    chunks = summary_dialogues.segments(material)
    assert len(chunks) == 1 and chunks[0]["pack"]["core_refs"] == ["R.0"]
    assert "broadcast-0" in {m["message_id"] for m in chunks[0]["pack"]["messages"]}
    # Actual targets, private chats, confirmed single-creator groups and natural answers remain analyzable.
    material["pack"]["messages"] = [{**broadcast, "mentions": [{"target": "U2", "ambiguous": False}]}]
    targeted = summary_dialogues.segments(material)
    assert len(targeted) == 1 and set(targeted[0]["users"]) == {"U2"}
    material["pack"].update(messages=[broadcast], is_private=True, users={"U1": people["U1"]})
    material["users"] = {"U1": people["U1"]}
    assert summary_dialogues.segments(material)[0]["pack"]["core_refs"] == ["B0.0"]
    material["pack"].update(is_private=False, unique_group_user="U1")
    assert summary_dialogues.segments(material)[0]["pack"]["core_refs"] == ["B0.0"]
    material["refs"]["S"] = {}
    material["pack"].update(unique_group_user=None,
        messages=[broadcast, reply, {**broadcast, "ref": "S", "message_id": "staff-answer",
                                     "text": "I'll check that campaign for you"}])
    assert summary_dialogues.segments(material)[0]["pack"]["core_refs"] == ["R.0", "S.0"]


@pytest.fixture
def seeded(pg, monkeypatch):
    monkeypatch.setattr(ai, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(ai, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"x" * 32))

    def fail_on_error(*_a, **_k):
        raise sys.exception()

    monkeypatch.setattr(ai.LOGGER, "warning", fail_on_error)
    ids = {
        "owner": uuid4(),
        "cm": ai.REMI_ID,
        "person": uuid4(),
        "other": uuid4(),
        "reviewer": uuid4(),
        "tester": next(who for who in ai.TEST_VIEWERS if str(who).startswith("1225")),
    }
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    start = datetime.now(UTC) - timedelta(hours=18)
    end = datetime.now(UTC) + timedelta(hours=1)
    for name in ("cm", "owner", "reviewer", "tester"):
        pg.execute(
            """INSERT INTO authorizations
                   (id,open_id,display_name,access_token_ciphertext,assignment_eligible,body_consent_at)
                   VALUES(%s,%s,%s,'test',%s,%s)""",
            (ids[name], "ou_" + name, name, name == "cm", start - timedelta(days=10)),
        )
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (ids["owner"],))
    for name in ("cm", "person", "other"):
        pg.execute("INSERT INTO contacts(id,display_name,creator_type) VALUES(%s,%s,'non_cap')", (ids[name], name))
        if name != "cm":
            pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                       "VALUES(%s,'creator','confirmed','manual')",
                       (ids[name],))
        pg.execute(
            """INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification)
                   VALUES(%s,%s,%s,%s)""",
            ("ou_" + name, ids[name], name, "internal" if name == "cm" else "known_external"),
        )
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('chat','Alice X TT','p2p')")
    pg.execute(
        """INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
               p2p_counterpart_type,backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
               VALUES(%s,'chat','ou_person','user',TRUE,%s,%s,'succeeded')""",
        (ids["cm"], start - timedelta(days=9), end),
    )
    account = uuid4()
    pg.execute(
        """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
           VALUES(%s,'tiktok','alice','@alice')""",
        (account,),
    )
    candidate_id = uuid4()
    pg.execute(
        """INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,
               contact_id,relationship,status,social_account_id)
               VALUES(%s,'alice',%s,%s,'associated','confirmed',%s)""",
        (candidate_id, ids["cm"], ids["person"], account),
    )
    pg.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
               VALUES(%s,%s,%s,'manual')""",
        (uuid4(), ids["person"], ids["cm"]),
    )
    pg.execute(
        """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
               VALUES(%s,%s,'owner','confirmed','cm_confirmation')""",
        (ids["person"], account),
    )
    cipher = message_body_cipher(settings.token_encryption_key)

    def add(message_id, who="person", text="Sample please", at=None, kind="text", **extra):
        pg.execute(
            """INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,
                   message_create_time,parent_id) VALUES('chat',%s,%s,%s,%s,%s,%s)""",
            (
                message_id,
                "ou_" + who,
                kind,
                cipher.encrypt(json.dumps({"text": text})),
                at or start + timedelta(hours=1),
                extra.get("parent_id"),
            ),
        )
        pg.execute(
            """INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
                   VALUES(%s,'chat',%s)""",
            (ids["cm"], message_id),
        )

    add("m1")
    add("outside", who="other", text="unrelated outsider secret")
    add("after", who="cm", text="future CM reply", at=end)
    fields = {
        "window_start": start.astimezone(ai.LONDON).isoformat(),
        "window_end": end.astimezone(ai.LONDON).isoformat(),
    }

    def save(enabled=True, key="test-key-not-real"):
        save_feature_config(
            pg,
                api_key_cipher(settings.token_encryption_key),
            ai.FEATURE,
            provider="openai",
            project_id="test-project",
            model="test-model",
            enabled=enabled,
            api_key=key,
            updated_by_authorization_id=ids["owner"],
        )
        return ai.save_settings_and_enqueue(pg, ids["owner"], fields)

    calls = []

    def model(_cfg, _key, input_pack, _cm, **kwargs):
        calls.append(input_pack)
        assert kwargs["output_format"]["strict"]
        fields = kwargs["output_format"]["schema"]["properties"]["events"]["items"]["properties"]
        assert "reason" not in fields and "handling_reason" in fields and "priority_basis" in fields
        message = next(m for m in input_pack["messages"] if m["in_window"] and m["speaker"] == "U1")
        return {
            "text": json.dumps({"processed_refs": input_pack.get("core_refs", []), "events": [sample_event(
                evidence_refs=[message["ref"]], handle=next(iter(input_pack["users"]["U1"]["handles"]), None)
            )]}),
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(openai_client, "call_openai", model)
    return SimpleNamespace(
        ids=ids, settings=settings, save=save, calls=calls, add=add, start=start, end=end, fields=fields, cipher=cipher,
        account=account, candidate_id=candidate_id,
    )


@pytest.mark.parametrize("roster_kind", ["current", "missing_handle", "multi_handle"])
def test_save_enabled_immediately_queues_claims_and_stores_encrypted_summary(pg, seeded, roster_kind, monkeypatch):
    s = seeded
    if roster_kind == "missing_handle":
        pg.execute("DELETE FROM contact_social_accounts")
    for who, relationship, status in (
        ("person", "associated", "confirmed"), ("person", "owner", "rejected"),
        ("other", "owner", "confirmed"),
    ):
        account = uuid4()
        pg.execute(
            """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
                   VALUES(%s,'tiktok',%s,%s)""", (account, str(account), str(account)),
        )
        pg.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
                   VALUES(%s,%s,%s,%s,'cm_confirmation')""", (s.ids[who], account, relationship, status),
        )
    roster = ai._roster(pg)
    assert len(roster) == 1 and roster[0]["contact_id"] == s.ids["person"]
    assert roster[0]["display_handle"] == (None if roster_kind == "missing_handle" else "@alice")
    if roster_kind == "multi_handle":
        account = uuid4()
        pg.execute(
            """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
                   VALUES(%s,'tiktok','alice_shop','@alice_shop')""", (account,),
        )
        pg.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source)
                   VALUES(%s,%s,'owner','confirmed','cm_confirmation')""", (s.ids["person"], account),
        )
    assert s.save(False)["enabled"] is False
    assert ai.claim_next_summary() is None
    queued = s.save()
    assert queued["queued"] == 1
    job = ai.claim_next_summary()
    assert job and job["attempts"] == 1
    ai.run_claimed_summary(job, s.settings)
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert len(s.calls) == 1 and len(view["events"]) == 1
    assert view["window"]["is_open"]
    assert view["events"][0]["priority"] == "P2"
    if roster_kind == "missing_handle":
        assert s.calls[0]["users"]["U1"]["handles"] == []
        assert view["events"][0]["creator_handle"] is None
        assert view["events"][0]["creator_handles"] == []
    elif roster_kind == "multi_handle":
        assert view["events"][0]["creator_handle"] is None
        assert set(view["events"][0]["creator_handles"]) == {"@alice", "@alice_shop"}
    else:
        assert view["events"][0]["creator_handles"] == ["@alice"]
    assert "unrelated outsider secret" not in json.dumps(s.calls)
    assert "future CM reply" not in json.dumps(s.calls)
    record = pg.execute("SELECT * FROM offhours_summary_tasks").fetchone()
    assert record["status"] == "succeeded" and "申请样品" not in record["result_ciphertext"]
    assert pg.execute("SELECT count(*) n FROM contact_roles").fetchone()["n"] == 2
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_candidates").fetchone()["n"] == 1
    if roster_kind == "current":
        from app import creator_importance
        with monkeypatch.context() as patch:
            patch.setattr(creator_importance, "tier_order", lambda *_: creator_importance.TIER_ORDER)
            patch.setattr(summary_dialogues, "view_events", lambda *_a, **_k: [
                {"id": "p2_focus", "priority": "P2", "contact_id": "focus"},
                {"id": "p0_normal", "priority": "P0", "contact_id": "normal"},
                {"id": "p0_focus", "priority": "P0", "contact_id": "focus"},
                {"id": "p0_priority", "priority": "P0", "contact_id": "priority"},
                {"id": "p0_dormant", "priority": "P0", "contact_id": "dormant"},
                {"id": "p0_unknown", "priority": "P0", "contact_id": "unknown"},
            ])
            assert [event["id"] for event in ai.get_summary_view(pg, s.ids["owner"])["events"]] == [
                "p0_focus", "p0_priority", "p0_normal", "p0_dormant", "p0_unknown", "p2_focus",
            ]
    s.save(key="")
    ai.schedule_if_due(pg, force=True)
    assert ai.claim_next_summary() is None and len(s.calls) == 1


def test_acl_owner_remi_and_explicit_test_grant_not_arbitrary_roster_reviewer(pg, seeded):
    s = seeded
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    for name in ("owner", "cm", "tester"):
        assert len(ai.get_summary_view(pg, s.ids[name])["events"]) == 1
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["reviewer"])
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (s.ids["tester"],))
    assert len(ai.get_summary_view(pg, s.ids["tester"])["events"]) == 1
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (s.ids["tester"],))
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["tester"])


@pytest.mark.parametrize("change", ["reassign", "consent"])
def test_revocation_or_edit_before_provider_exports_nothing(pg, seeded, change):
    s = seeded
    s.save()
    job = ai.claim_next_summary()
    if change == "reassign":
        pg.execute("UPDATE contact_assignments SET ended_at=NOW()")
    else:
        pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (s.ids["cm"],))
    ai.run_claimed_summary(job, s.settings)
    assert not s.calls
    assert (
        pg.execute("SELECT status FROM offhours_summary_tasks WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "cancelled"
    )




def test_disable_preserves_unknown_and_old_call_cannot_commit(pg, seeded):
    s = seeded
    s.save()
    stale = ai.claim_next_summary()
    s.save(False, key="")
    with pytest.raises(ai.SummaryInputError, match="结果未知"), pg.transaction():
        s.save(True, key="")
    assert ai.claim_next_summary() is None
    ai.run_claimed_summary(stale, s.settings)
    assert not s.calls
    current = pg.execute("SELECT status,error_message,attempts FROM offhours_summary_tasks WHERE id=%s",
                         (stale["id"],)).fetchone()
    assert current["status"] == "failed" and "请求结果未知" in current["error_message"]
    assert current["attempts"] == stale["attempts"]


def test_all_creator_conversations_include_cm_only_groups_and_recheck_access(pg, seeded, monkeypatch):
    s = seeded
    pg.execute(
        """INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification)
           VALUES('ou_person_alias',%s,'Alice second identity','known_external')""", (s.ids["person"],),
    )
    groups = {
        "group_named": ("❤️ alice X TT Media", "ou_person", True, True),
        "group_renamed": ("September collaborations", "ou_person_alias", True, True),
        "group_ai": ("Her weekly releases", None, True, True),
        "group_unrelated": ("Another creator X TT Media", "ou_other", True, True),
        "group_hidden": ("alice samples", "ou_person", False, True),
        "group_quiet": ("alice archive", "ou_person", True, False),
        "group_restricted": ("Lark restricted creator group", "ou_person", True, True),
    }
    for chat, (name, member, visible, in_window) in groups.items():
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'group')", (chat, name))
        pg.execute(
            """INSERT INTO authorization_chats(authorization_id,chat_id,is_visible,
                   backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
                   VALUES(%s,%s,%s,TRUE,%s,%s,'succeeded')""",
            (s.ids["cm"], chat, visible, s.start - timedelta(days=9), s.end),
        )
        if member:
            pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,%s)", (chat, member))
        pg.execute(
            """INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
                   body_ciphertext,message_create_time) VALUES(%s,%s,'ou_cm','text',%s,%s)""",
            (chat, chat + "_message", s.cipher.encrypt(json.dumps({"text": f"Sample update in {chat}"})),
             s.start + timedelta(hours=1) if in_window else s.start - timedelta(hours=1)),
        )
        pg.execute(
            """INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
                   VALUES(%s,%s,%s)""", (s.ids["cm"], chat, chat + "_message"),
        )
    pg.execute("UPDATE authorization_chats SET sync_status='not_readable' WHERE chat_id='group_restricted'")
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('group_named','ou_cm')")
    pg.execute(
        """UPDATE authorization_chats SET member_snapshot_status='succeeded',member_count=2,
               member_count_status='succeeded' WHERE chat_id='group_named'""",
    )

    # This admitted AI chain connects a second group without requiring a cached
    # creator member or creator-authored message in that particular group.
    hashes = creator_import_evidence.ownership
    cm = pg.execute("SELECT * FROM authorizations WHERE id=%s", (s.ids["cm"],)).fetchone()
    scope = {"scope_fingerprint": hashes._hash([str(s.ids["cm"]), cm["open_id"], cm["body_consent_at"]])}
    group_refs = [
        {"kind": "group", "label": f"G{i + 1}", "chat_id": chat, "name_hash": hashes._hash(groups[chat][0])}
        for i, chat in enumerate(("group_named", "group_ai"))
    ]
    identities = pg.execute(
        """SELECT open_id,contact_id,display_name,name,en_name,identity_type,tenant_classification
           FROM lark_identities WHERE open_id='ou_person' ORDER BY open_id""",
    ).fetchall()
    person_ref = {"kind": "person", "label": "U1", "contact_id": str(s.ids["person"]),
                  "open_ids": ["ou_person"], "identity_hash": hashes._hash(identities)}
    chain = {"group": {"refs": group_refs, "stats": scope},
             "person": {"refs": [*group_refs, person_ref], "stats": scope}}
    decisions = {
        "group": {"outcome": "matched", "confidence": .99, "group_refs": ["G1", "G2"]},
        "person": {"outcome": "matched", "confidence": .99, "user_ref": "U1", "conflicts": []},
    }
    batch_id, row_id = uuid4(), uuid4()
    pg.execute(
        """INSERT INTO creator_import_batches(id,cm_authorization_id,uploaded_by,filename,
               file_fingerprint,policy_version,status,creator_type)
               VALUES(%s,%s,%s,'scope.xlsx','scope',%s,'completed','cap')""",
        (batch_id, s.ids["cm"], s.ids["owner"], creator_imports.POLICY_VERSION),
    )
    from app import creator_data

    cipher = creator_data.cipher(s.settings)
    pg.execute(
        """INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,row_number,original_handle,
               handle,contact_id,status,review_status,outcome,pack_ciphertext,result_ciphertext)
               VALUES(%s,%s,%s,1,'alice','alice',%s,'succeeded','confirmed','matched',%s,%s)""",
        (row_id, batch_id, s.ids["cm"], s.ids["person"],
         cipher.encrypt(json.dumps(chain)), cipher.encrypt(json.dumps(decisions))),
    )
    pg.execute("UPDATE contact_social_accounts SET import_row_id=%s WHERE social_account_id=%s", (row_id, s.account))

    def model(_cfg, _key, input_pack, _cm, **_kwargs):
        if 'candidates' in input_pack:
            return {'text': json.dumps({'match_id': None, 'same': False, 'basis_kind': 'none',
                'handling_status': 'uncertain',
                'handling_reason': '不合并', 'handling_evidence_refs': ['E1'], 'reply_evidence_refs': [],
                'quote_a_ref': 'E1', 'quote_b_ref': 'E1',
                **{k: '' for k in ('basis_value', 'title', 'summary', 'latest_progress')}})}
        s.calls.append(input_pack)
        assert len(input_pack["users"]) == 1
        assert input_pack["users"]["U1"]["handles"] == ["@alice"]
        if input_pack["chat_name"] == groups["group_ai"][0]:
            assert input_pack["unique_group_user"] is None
            return {
                "text": json.dumps({"events": [], "processed_refs": input_pack["core_refs"]}),
                "usage": {"input_tokens": 1},
            }
        if not input_pack["is_private"]:
            assert input_pack["unique_group_user"] == "U1"
        message = next(m for m in input_pack["messages"] if m["in_window"])
        return {
            "text": json.dumps(
                {
                    "processed_refs": input_pack.get("core_refs", []),
                    "events": [sample_event(evidence_refs=[message["ref"]])],
                }
            ),
            "response_id": "resp-new-summary",
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(openai_client, "call_openai", model)
    assert s.save()["queued"] == 4
    tasks = pg.execute("SELECT chat_id FROM offhours_summary_tasks WHERE is_current").fetchall()
    assert {r["chat_id"] for r in tasks} == {"chat", "group_named", "group_renamed", "group_ai"}
    while job := ai.claim_next_summary():
        if job["chat_id"] == "group_renamed":
            before = len(s.calls)
            pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='group_renamed'")
            ai.run_claimed_summary(job, s.settings)
            assert len(s.calls) == before
        else:
            evidence = ai._load_chat(pg, job, job["chat_id"], job["cutoff"], settings=s.settings)
            assert len(evidence["users"]) == 1
            assert set(evidence["users"]["U1"]["open_ids"]) == {"ou_person", "ou_person_alias"}
            if job["chat_id"] == "group_ai":
                checkpoint = {"chunks": {"previous-paid-empty-chunk": []}}
                paid_usage = {"input_tokens": 1234, "output_tokens": 56}
                pg.execute("""UPDATE offhours_summary_tasks SET checkpoint_ciphertext=%s,
                    usage=%s::jsonb,response_id='resp-paid-broadcast' WHERE id=%s""",
                    (summary_dialogues.encode(checkpoint, s.settings), json.dumps(paid_usage), job["id"]))
                job.update(pg.execute("SELECT checkpoint_ciphertext,usage,response_id FROM offhours_summary_tasks "
                                      "WHERE id=%s", (job["id"],)).fetchone())
                before = len(s.calls)
            ai.run_claimed_summary(job, s.settings)
            if job["chat_id"] == "group_ai":
                saved = pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=%s", (job["id"],)).fetchone()
                assert len(s.calls) == before and saved["status"] == "succeeded"
                assert saved["response_id"] == "resp-paid-broadcast" and saved["usage"] == paid_usage
                assert summary_dialogues.decode(saved["checkpoint_ciphertext"], s.settings) == checkpoint
            else:
                assert pg.execute("SELECT response_id FROM offhours_summary_tasks WHERE id=%s",
                                  (job["id"],)).fetchone()["response_id"] == "resp-new-summary"
    assert len(s.calls) == 2
    assert any(p["is_private"] for p in s.calls)
    assert {p["chat_name"] for p in s.calls if not p["is_private"]} == {
        groups["group_named"][0],
    }
    ai.schedule_if_due(pg, force=True)
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert len(view["events"]) == 2 and view["run"]["counts"]["completed"] == 4
    assert any("Lark 读取限制" in gap for gap in view["run"]["gaps"])
    assert not any("group_restricted" in json.dumps(p) for p in s.calls)
    run = pg.execute(
        "SELECT r.* FROM offhours_summary_runs r JOIN offhours_summary_settings s ON s.run_id=r.id"
    ).fetchone()
    with pytest.raises(ai.SummaryInputError, match="不允许读取"):
        ai._load_chat(pg, run, 'group_restricted', s.end, settings=s.settings)
    assert not any("group_hidden" in json.dumps(p) or "group_unrelated" in json.dumps(p) for p in s.calls)

    # A second, unassigned creator still prevents a uniqueness inference. The
    # conversation remains relevant, but CM-only text needs an explicit target.
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('group_named','ou_other')")
    pg.execute("UPDATE authorization_chats SET member_count=3 WHERE chat_id='group_named'")
    related = ai._related_chats(pg, ai._roster(pg), settings=s.settings)
    assert "group_named" in {r["chat_id"] for r in related}
    evidence = ai._load_chat(pg, run, "group_named", s.end, settings=s.settings)
    assert evidence["pack"]["unique_group_user"] is None
    event = sample_event(evidence_refs=[next(m["ref"] for m in evidence["pack"]["messages"] if m["in_window"])])
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"events": [event]}), evidence)
    pg.execute(
        """UPDATE chat_messages SET body_ciphertext=%s,mentions_json=%s::jsonb
               WHERE message_id='group_named_message'""",
        (s.cipher.encrypt(json.dumps({"text": "@_user_1, your sample has shipped"})),
         json.dumps([{"key": "@_user_1", "id": "ou_person", "id_type": "open_id", "name": "Alice"}])),
    )
    targeted = ai._load_chat(pg, run, "group_named", s.end, settings=s.settings)
    assert ai._validate(json.dumps({"events": [event]}), targeted)[0]["priority"] == "P2"
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='group_ai'")
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert not any(ref["chat_id"] == "group_ai" for event in view["events"] for ref in event["evidence"])


def test_current_acl_hides_already_generated_results(pg, seeded, monkeypatch):
    s = seeded
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    run = pg.execute("SELECT r.* FROM offhours_summary_runs r JOIN offhours_summary_settings s "
                     "ON s.run_id=r.id").fetchone()
    task = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id IS NULL",
                      (run["id"],)).fetchone()
    roster = ai._roster(pg)
    chats = ai._related_chats(pg, roster, settings=s.settings)
    live, load = ai._authorize_live, ai._load_chat
    calls = []

    def authorized(*args, **kwargs):
        calls.append("authorize")
        return live(*args, **kwargs)

    def loaded(*args, **kwargs):
        calls.append("load")
        return load(*args, **kwargs)

    monkeypatch.setattr(ai, "_authorize_live", authorized)
    monkeypatch.setattr(ai, "_load_chat", loaded)
    cache = {}
    for _ in range(2):
        assert summary_dialogues.view_events(pg, run, task, s.settings, roster, chats, read_cache=cache)
    assert calls.count("authorize") == calls.count("load") == 1

    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='chat'")
    calls.clear()
    cache = {}
    for _ in range(2):
        with pytest.raises(ai.SummaryInputError):
            summary_dialogues.view_events(pg, run, task, s.settings, roster, chats, read_cache=cache)
    assert calls.count("authorize") == calls.count("load") == 1
    assert not ai.get_summary_view(pg, s.ids["cm"])["events"]
    pg.execute("UPDATE authorization_chats SET is_visible=TRUE WHERE chat_id='chat'")
    assert ai.get_summary_view(pg, s.ids["cm"])["events"]

    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],))
    assert not ai.get_summary_view(pg, s.ids["cm"])["events"]
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (s.ids["owner"],))
    assert ai.get_summary_view(pg, s.ids["cm"])["events"]
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'")
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert not view["events"] and view["run"]["counts"]["failed"] == 1


def test_relevant_chats_are_filtered_before_limit(pg, seeded):
    s = seeded
    for i in range(301):
        chat = f"aaa-unrelated-{i}"
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (s.ids["cm"], chat))
    assert s.save()["queued"] == 1
    assert ai.claim_next_summary()["chat_id"] == "chat"


def test_open_window_watermark_refresh_does_not_invalidate_identical_pack(pg, seeded):
    s = seeded
    pg.execute("UPDATE authorization_chats SET last_incremental_at=%s", (s.start,))
    s.save()
    job = ai.claim_next_summary()
    before = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    pg.execute("UPDATE authorization_chats SET last_incremental_at=%s", (s.end,))
    after = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    assert before["fingerprint"] == after["fingerprint"]
    assert not after["history_complete"]




@pytest.mark.parametrize("http_status", [None, 503])
def test_network_failure_retries_are_bounded_not_cancelled_refresh_loop(
    pg, seeded, monkeypatch, http_status,
):
    s = seeded
    s.save()

    calls = []
    def fail(*_a, **_k):
        calls.append(True)
        raise openai_client.OpenAIRequestError("simulated failure", retryable=True,
                                              telemetry={"http_status": http_status})

    monkeypatch.setattr(openai_client, "call_openai", fail)
    for _ in range(ai.MAX_ATTEMPTS if http_status else 1):
        job = ai.claim_next_summary()
        assert job
        ai.run_claimed_summary(job, s.settings)
        ai.ai_dispatch.resume(pg)
        pg.execute("UPDATE offhours_summary_tasks SET scheduled_at=NOW()")
    ai.schedule_if_due(pg, force=True)
    assert ai.claim_next_summary() is None
    assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE is_current").fetchone()["status"] == "failed"
    assert len(calls) == (ai.MAX_ATTEMPTS if http_status else 1)
    if http_status is None:
        assert "请求结果未知" in pg.execute(
            "SELECT error_message FROM offhours_summary_tasks WHERE is_current").fetchone()["error_message"]
        for key in ("", "replacement-fixture-key"):
            with pytest.raises(ai.SummaryInputError, match="结果未知"), pg.transaction():
                s.save(key=key)
            assert ai.claim_next_summary() is None
        assert not s.save(False)["enabled"]
        assert len(calls) == 1


def _set_mentions(pg, seeded, mentions, text="Hi @_user_1, I haven’t heard from you"):
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s,mentions_json=%s::jsonb WHERE message_id='m1'",
        (seeded.cipher.encrypt(json.dumps({"text": text})), json.dumps(mentions)),
    )


def test_mentions_ui_and_ai_share_resolved_text_and_id_based_targets_without_mutating_source(pg, seeded):
    s = seeded
    mentions = [
        {"key": f"@_user_{n}", "id": "ou_" + who, "id_type": kind, "name": "Remi"}
        for n, who, kind in (
            (1, "cm", "open_id"), (2, "person", "open_id"), (3, "other", "open_id"), (4, "cm", "user_id")
        )
    ]
    text = "Hi @_user_1, @_user_2, @_user_3, @_user_4: I haven’t heard from you"
    _set_mentions(pg, s, mentions, text)
    pg.execute("UPDATE lark_identities SET display_name='Remi（Quanzhen Liang）' WHERE open_id='ou_cm'")
    pg.execute("UPDATE chats SET chat_mode='group' WHERE chat_id='chat'")
    for who in ("reviewer", "tester"):
        pg.execute("INSERT INTO lark_identities(open_id,display_name,tenant_classification) "
                   "VALUES(%s,%s,'internal')", ("ou_" + who, who))
    s.add("colleague", who="reviewer", text="I'll check the sample", parent_id="m1",
          at=s.start + timedelta(hours=2))
    s.add("not_observed", who="tester", text="Only another CM can read this", parent_id="m1",
          at=s.start + timedelta(hours=3))
    pg.execute("DELETE FROM authorization_message_observations WHERE message_id='not_observed'")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat')",
               (s.ids["reviewer"],))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat','not_observed')", (s.ids["reviewer"],))
    original = pg.execute("SELECT body_ciphertext,mentions_json FROM chat_messages WHERE message_id='m1'").fetchone()
    s.save()
    job = ai.claim_next_summary()
    data = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    message = data["pack"]["messages"][0]
    assert message["text"] == "Hi @Remi（Quanzhen Liang）, @person, @other, @Remi: I haven’t heard from you"
    assert [m["target"] for m in message["mentions"]] == ["CM", "U1", "other", "unknown"]
    assert data["refs"][message["ref"]]["text"] == message["text"]
    assert "ou_" not in json.dumps(data["pack"])
    assert not message["unreadable"]  # An unresolved @ does not obscure the readable request.
    colleague = next(m for m in data["pack"]["messages"] if m["message_id"] == "colleague")
    assert colleague["is_staff"] and data["refs"][colleague["ref"]]["sender_name"] == "reviewer"
    assert data["pack"]["staff"][colleague["speaker"]]["name"] == "reviewer"
    assert "Only another CM can read this" not in json.dumps(data)
    pg.execute("UPDATE authorizations SET status='attention_required' WHERE id=%s", (s.ids["tester"],))
    assert ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)["fingerprint"] == data["fingerprint"]
    ai.run_claimed_summary(job, s.settings)
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert view["events"][0]["evidence"][0]["text"] == s.calls[0]["messages"][0]["text"]
    assert original == pg.execute(
        "SELECT body_ciphertext,mentions_json FROM chat_messages WHERE message_id='m1'"
    ).fetchone()


def test_same_display_different_mention_identity_invalidates_only_affected_input(pg, seeded):
    s = seeded
    mentions = [{"key": "@_user_1", "id": "ou_other", "id_type": "open_id", "name": "Same"}]
    pg.execute("UPDATE lark_identities SET display_name='Same' WHERE open_id='ou_other'")
    pg.execute("INSERT INTO lark_identities(open_id,display_name) VALUES('ou_other2','Same')")
    _set_mentions(pg, s, mentions)
    s.save()
    first = ai.claim_next_summary()
    before = ai._load_chat(pg, first, "chat", first["cutoff"], settings=s.settings)
    ai.run_claimed_summary(first, s.settings)
    mentions[0]["id"] = "ou_other2"
    _set_mentions(pg, s, mentions)
    after = ai._load_chat(pg, first, "chat", first["cutoff"], settings=s.settings)
    assert before["pack"] == after["pack"] and before["fingerprint"] != after["fingerprint"]
    assert not ai.get_summary_view(pg, s.ids["owner"])["events"]
    assert ai.schedule_if_due(pg, force=True) == 1
    assert ai.schedule_if_due(pg, force=True) == 0
    assert ai.claim_next_summary()["input_fingerprint"] == after["fingerprint"]


def test_unmentioned_name_changes_and_omitted_mentions_keep_previous_input_fingerprint(pg, seeded):
    s = seeded
    s.save()
    job = ai.claim_next_summary()
    before = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    assert before["fingerprint"] == ai._hash([
        ai._hash([before["pack"], before["users"]]), job["event_rules_digest"]])
    assert "mentions" not in before["pack"]["messages"][0]
    pg.execute("UPDATE lark_identities SET display_name='unrelated changed' WHERE open_id='ou_other'")
    pg.execute(
        "UPDATE chat_messages SET mentions_json=%s::jsonb WHERE message_id='outside'",
        (json.dumps([{"key": "@_user_1", "id": "ou_other", "id_type": "open_id", "name": "Other"}]),),
    )
    after = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    assert before["fingerprint"] == after["fingerprint"]


def test_mention_display_update_invalidates_relevant_message_summary(pg, seeded):
    s = seeded
    _set_mentions(pg, s, [{"key": "@_user_1", "id": "ou_other", "id_type": "open_id", "name": "Old"}])
    s.save()
    job = ai.claim_next_summary()
    before = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    pg.execute("UPDATE lark_identities SET display_name='New（原名）' WHERE open_id='ou_other'")
    after = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    assert before["fingerprint"] != after["fingerprint"]
    assert "@New（原名）" in after["pack"]["messages"][0]["text"]


def test_rich_mentions_keep_full_text_and_credentials_redacted():
    cipher = message_body_cipher(b"x" * 32)
    mentions = [{"key": "@_user_1", "id": "ou_cm", "id_type": "open_id", "name": "Original"}]
    row = {
        "message_type": "post", "mentions_json": mentions,
        "body_ciphertext": cipher.encrypt(json.dumps({"content": [[
            {"tag": "text", "text": "Hello"}, {"tag": "at", "user_id": "ou_cm"},
            {"tag": "a", "text": "link", "href": "https://example.test/?token=secret"},
        ]]})),
    }
    text, incomplete = ai._body(row, cipher, {"ou_cm": "Remi（Original）"})
    assert text == "Hello @Remi（Original） link https://example.test/?token=[redacted]" and not incomplete
    row["body_ciphertext"] = cipher.encrypt(json.dumps({"content": [[{"tag": "at", "user_id": "ou_unknown"}]]}))
    assert ai._body(row, cipher) == ("[未解析的@用户]", True)
    row["body_ciphertext"] = cipher.encrypt(json.dumps({"content": [[{"tag": "at", "user_id": "ou_unknown"},
        {"tag": "text", "text": "Please check my missing payment"}]]}))
    assert not ai._body(row, cipher)[1]
    row.update(message_type="text", body_ciphertext=cipher.encrypt(json.dumps({"text": "@_user_1" + "x" * 2000})))
    # An identifier-like suffix is not a Lark placeholder boundary.
    assert ai._body(row, cipher)[0].startswith("@_user_1x")
    row["body_ciphertext"] = cipher.encrypt(json.dumps({"text": "@_user_1 " + "x" * 2000}))
    text, incomplete = ai._body(row, cipher, {"ou_cm": "Remi（Original）"})
    assert text.startswith("@Remi（Original） ") and len(text) > 2000 and not incomplete


def test_recurring_periods_keep_history_and_cm_scope_separate(pg, seeded):
    s = seeded
    s.save()
    pg.execute('UPDATE offhours_summary_settings SET recurring_enabled=TRUE,next_period_check_at=NULL')
    now = datetime.now(UTC)
    summary_periods.seed_due(pg, now)
    first = pg.execute('SELECT id FROM offhours_summary_runs WHERE period IS NOT NULL').fetchall()
    assert 12 <= len(first) <= 14
    pg.execute('UPDATE offhours_summary_settings SET next_period_check_at=NULL')
    summary_periods.seed_due(pg, now)
    assert pg.execute('SELECT id FROM offhours_summary_runs WHERE period IS NOT NULL').fetchall() == first
    target = pg.execute("""SELECT r.* FROM offhours_summary_runs r JOIN chat_messages m
        ON m.message_create_time>=r.window_start AND m.message_create_time<r.window_end
        WHERE r.period IS NOT NULL AND m.message_id='m1'""").fetchone()
    assert target
    pg.execute("UPDATE offhours_summary_tasks SET status='cancelled'")
    ai._schedule_run(pg, {**target, 'run_id': target['id']}, now)
    pg.execute('UPDATE offhours_summary_runs SET next_check_at=NULL')
    job = ai.claim_next_summary()
    assert job['run_id'] == target['id']
    ai.run_claimed_summary(job, s.settings)
    finish_merges(pg, s)
    historical = ai.get_summary_view(pg, s.ids['cm'], day=target['report_date'], period=target['period'])
    assert historical['events'] and len(s.calls) == 1
    ai._schedule_run(pg, {**target, 'run_id': target['id']}, now)
    assert pg.execute('SELECT count(*) n FROM offhours_summary_tasks WHERE run_id=%s',
                      (target['id'],)).fetchone()['n'] == 2
    with pytest.raises(PermissionError):
        summary_periods.selection(pg, s.ids['reviewer'], s.ids['cm'])
    cm_id, start, end, selected = summary_periods.selection(pg, s.ids['cm'])
    assert cm_id == s.ids['cm'] and len(selected['dates']) == 7 and end > start
    with pytest.raises(ai.SummaryInputError):
        summary_periods.selection(pg, s.ids['cm'], day='2020-01-01', period='work')
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (s.ids['tester'],))
    second_cm = s.ids['reviewer']
    pg.execute('INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) '
               "SELECT gen_random_uuid(),contact_id,%s,'manual' FROM contact_assignments", (second_cm,))
    assert ai._roster(pg, second_cm) == ai._roster(pg, s.ids['cm'])
    assert ai._related_chats(pg, ai._roster(pg, second_cm), cm_id=second_cm) == []
    assert summary_periods.selection(pg, s.ids['tester'], second_cm)[0] == second_cm
    pg.execute('UPDATE offhours_summary_settings SET next_period_check_at=NULL')
    summary_periods.seed_due(pg, now)
    other = pg.execute('SELECT * FROM offhours_summary_runs WHERE period IS NOT NULL '
                       'AND responsible_authorization_id=%s ORDER BY window_start DESC LIMIT 1',
                       (second_cm,)).fetchone()
    ai._schedule_run(pg, {**other, 'run_id': other['id']}, now)
    view = ai.get_summary_view(pg, s.ids['tester'], cm_id=second_cm,
                               day=other['report_date'], period=other['period'])
    assert view['responsible']['id'] == str(second_cm) and view['events'] == []
    assert pg.execute('SELECT count(*) n FROM offhours_summary_runs WHERE responsible_authorization_id=%s '
                      'AND period IS NOT NULL', (s.ids['cm'],)).fetchone()['n'] == len(first)
    pg.execute('DELETE FROM admin_page_permissions WHERE authorization_id=%s', (s.ids['tester'],))
    with pytest.raises(PermissionError):
        summary_periods.selection(pg, s.ids['tester'], second_cm)


def test_creator_enrollment_backfills_closed_periods_without_repaying_or_replacing_others(
    pg, seeded, monkeypatch, caplog,
):
    s = seeded

    def finish():
        finish_merges(pg, s)  # Finish newly queued conversations first.
        finish_merges(pg, s)  # Then schedule their per-creator merges.

    now = datetime.now(UTC)
    day = now.astimezone(ai.LONDON).date() - timedelta(days=3)
    start, end = summary_periods.window(day, 'work')
    pg.execute('DELETE FROM offhours_summary_backfills')
    pg.execute("DELETE FROM authorization_message_observations WHERE message_id='outside'")
    pg.execute("DELETE FROM chat_messages WHERE message_id='outside'")
    pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE message_id='m1'",
               (start + timedelta(hours=1),))
    s.save()
    pg.execute('UPDATE offhours_summary_settings SET recurring_enabled=TRUE,next_period_check_at=NULL')
    summary_periods.seed_due(pg, now)
    run = pg.execute("SELECT * FROM offhours_summary_runs WHERE report_date=%s AND period='work' "
                     'AND responsible_authorization_id=%s', (day, s.ids['cm'])).fetchone()
    pg.execute('UPDATE offhours_summary_runs SET next_check_at=NULL,refresh_until=window_end')
    run = {**run, 'run_id': run['id'], 'refresh_until': end}
    ai._schedule_run(pg, run, now)
    finish()
    assert len(s.calls) == 1

    def old_tasks():
        return pg.execute('''SELECT id,input_fingerprint,status,is_current,result_ciphertext,
            checkpoint_ciphertext,processed_messages,total_messages,attempts,usage,model,response_id
            FROM offhours_summary_tasks
            WHERE run_id=%s AND (chat_id='chat' OR contact_id=%s) ORDER BY id''',
            (run['id'], s.ids['person'])).fetchall()

    original = old_tasks()
    assert len(original) == 2 and all(t['status'] == 'succeeded' for t in original)
    missing_day = day - timedelta(days=1)
    pg.execute("DELETE FROM offhours_summary_runs WHERE responsible_authorization_id=%s "
               "AND report_date=%s AND period='off'", (s.ids['cm'], missing_day))
    old_gap = '尚未找到已同步的时段内相关消息；这不代表 Lark 中没有消息。'
    media_gap = '部分附件或正文不可读取；分析仅依据可读消息。'
    pg.execute("UPDATE offhours_summary_runs SET coverage=jsonb_set(coverage,'{gaps}',%s::jsonb) WHERE id=%s",
               (json.dumps([old_gap, media_gap]), run['id']))
    ancient_day = day - timedelta(days=5)
    ancient_start, ancient_end = summary_periods.window(ancient_day, 'work')
    ancient = uuid4()
    pg.execute('''INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,
        responsible_authorization_id,window_start,window_end,report_date,period,refresh_until)
        VALUES(%s,%s,%s,%s,%s,%s,'work',%s)''',
        (ancient, s.ids['owner'], s.ids['cm'], ancient_start, ancient_end, ancient_day, ancient_end))

    def add_private(chat, cm, message, text='Sample please'):
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'p2p')", (chat, chat))
        pg.execute('''INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
            p2p_counterpart_type,backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
            VALUES(%s,%s,'ou_other','user',TRUE,%s,%s,'succeeded')''',
            (cm, chat, start - timedelta(days=9), now))
        pg.execute('''INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
            body_ciphertext,message_create_time) VALUES(%s,%s,'ou_other','text',%s,%s)''',
            (chat, message, s.cipher.encrypt(json.dumps({'text': text})), start + timedelta(hours=2)))
        pg.execute('''INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
            VALUES(%s,%s,%s)''', (cm, chat, message))

    add_private('new-creator', s.ids['cm'], 'new-request')
    # Recognition can arrive after responsibility; neither handle nor group classification is required.
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (s.ids['other'],))
    assignment = uuid4()
    pg.execute('''INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        VALUES(%s,%s,%s,'ai')''', (assignment, s.ids['other'], s.ids['cm']))
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
               (s.ids['other'],))
    pending = pg.execute('SELECT * FROM offhours_summary_backfills').fetchone()
    assert pending['contact_id'] == s.ids['other'] and pending['responsible_authorization_id'] == s.ids['cm']
    # An upsert of the same formal facts is not a new enrollment signal.
    pg.execute("UPDATE contact_assignments SET source='manual',updated_at=NOW() WHERE id=%s", (assignment,))
    pg.execute("UPDATE contact_roles SET source='manual',updated_at=NOW() WHERE contact_id=%s",
               (s.ids['other'],))
    assert pg.execute('SELECT version FROM offhours_summary_backfills').fetchone()['version'] == pending['version']
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature=%s", (ai.FEATURE,))
    summary_periods.backfill_due(pg, now)
    assert pg.execute('SELECT version FROM offhours_summary_backfills').fetchone()['version'] == pending['version']
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE WHERE feature=%s", (ai.FEATURE,))
    # Missing consent postpones the durable pair instead of exporting or losing it.
    pg.execute('SAVEPOINT backfill_no_consent')
    pg.execute('UPDATE authorizations SET body_consent_at=NULL WHERE id=%s', (s.ids['cm'],))
    summary_periods.backfill_due(pg, now)
    postponed = pg.execute('SELECT * FROM offhours_summary_backfills').fetchone()
    assert postponed and postponed['not_before'] > now and len(s.calls) == 1
    pg.execute('ROLLBACK TO SAVEPOINT backfill_no_consent')
    pg.execute('SAVEPOINT backfill_no_identity')
    pg.execute('DELETE FROM lark_identities WHERE contact_id=%s', (s.ids['other'],))
    summary_periods.backfill_due(pg, now)
    assert pg.execute('SELECT not_before FROM offhours_summary_backfills').fetchone()['not_before'] > now
    pg.execute('ROLLBACK TO SAVEPOINT backfill_no_identity')
    pg.execute('SAVEPOINT backfill_invalid_identity')
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (s.ids['other'],))
    summary_periods.backfill_due(pg, now)
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone() and len(s.calls) == 1
    pg.execute('ROLLBACK TO SAVEPOINT backfill_invalid_identity')
    ai.schedule_if_due(pg, force=True)
    assert pg.execute('SELECT not_before FROM offhours_summary_backfills').fetchone()['not_before'] > now
    assert old_tasks() == original
    after = pg.execute('SELECT * FROM offhours_summary_runs WHERE id=%s', (run['id'],)).fetchone()
    assert after['refresh_until'] == end and after['next_check_at'] is None
    assert after['coverage']['confirmed_creators'] == 2
    assert after['coverage']['conversations'] == 2 and after['coverage']['messages'] == 2
    assert old_gap not in after['coverage']['gaps'] and media_gap in after['coverage']['gaps']
    missing = pg.execute("SELECT next_check_at,refresh_until,window_end FROM offhours_summary_runs "
                         "WHERE responsible_authorization_id=%s AND report_date=%s AND period='off'",
                         (s.ids['cm'], missing_day)).fetchone()
    assert missing and missing['next_check_at'] is None and missing['refresh_until'] == missing['window_end']
    assert not pg.execute('SELECT 1 FROM offhours_summary_tasks WHERE run_id=%s', (ancient,)).fetchone()
    target_task = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='new-creator' "
                             'AND is_current', (run['id'],)).fetchone()
    assert target_task and target_task['status'] == 'queued'
    # A zero-message old task does not inflate totals; a not-yet-synced related chat remains a gap.
    pg.execute('SAVEPOINT backfill_zero_message_coverage')
    add_private('empty-old-private', s.ids['cm'], 'empty-placeholder')
    pg.execute("DELETE FROM authorization_message_observations WHERE message_id='empty-placeholder'")
    pg.execute("DELETE FROM chat_messages WHERE message_id='empty-placeholder'")
    pg.execute("UPDATE authorization_chats SET p2p_counterpart_open_id='ou_person',last_incremental_at=%s "
               "WHERE chat_id='empty-old-private'", (start - timedelta(hours=1),))
    pg.execute('''INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff,status)
        VALUES(%s,%s,'empty-old-private','old-zero',%s,'succeeded')''', (uuid4(), run['id'], end))
    ai._schedule_run(pg, {**after, 'run_id': run['id']}, now, contact_ids={s.ids['other']})
    coverage = pg.execute('SELECT coverage FROM offhours_summary_runs WHERE id=%s', (run['id'],)).fetchone()['coverage']
    assert coverage['conversations'] == 2 and coverage['messages'] == 2 and not coverage['synced']
    assert any('同步进度' in gap for gap in coverage['gaps'])
    pg.execute('ROLLBACK TO SAVEPOINT backfill_zero_message_coverage')

    # A running source that changed is deferred, not cancelled or replaced by a second paid request.
    pg.execute('SAVEPOINT backfill_inflight')
    pg.execute("UPDATE offhours_summary_tasks SET status='running',attempts=1,lease_until=NOW()+INTERVAL '10 minutes' "
               'WHERE id=%s', (target_task['id'],))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='new-request'",
               (s.cipher.encrypt(json.dumps({'text': 'Sample please, another update'})),))
    pg.execute('SELECT enqueue_summary_backfill(%s,%s)', (s.ids['other'], s.ids['cm']))
    summary_periods.backfill_due(pg, now)
    deferred = pg.execute('SELECT * FROM offhours_summary_backfills').fetchone()
    assert deferred and deferred['not_before'] > now
    assert pg.execute('SELECT status,is_current FROM offhours_summary_tasks WHERE id=%s',
                      (target_task['id'],)).fetchone() == {'status': 'running', 'is_current': True}
    assert old_tasks() == original and len(s.calls) == 1
    pg.execute('ROLLBACK TO SAVEPOINT backfill_inflight')
    # Queuing never bypasses the existing last-moment identity, relationship or consent checks.
    for change in ('consent', 'relationship', 'identity'):
        pg.execute('SAVEPOINT backfill_revoke')
        job = ai.claim_next_summary()
        assert job['id'] == target_task['id']
        if change == 'consent':
            pg.execute('UPDATE authorizations SET body_consent_at=NULL WHERE id=%s', (s.ids['cm'],))
        elif change == 'relationship':
            pg.execute('UPDATE contact_assignments SET ended_at=NOW() WHERE id=%s', (assignment,))
        else:
            pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (s.ids['other'],))
        ai.run_claimed_summary(job, s.settings)
        assert len(s.calls) == 1
        assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',
                          (job['id'],)).fetchone()['status'] == 'cancelled'
        pg.execute('ROLLBACK TO SAVEPOINT backfill_revoke')
    # Real source edits after admission invalidate the queued task; the retained signal rebuilds it.
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='new-request'",
               (s.cipher.encrypt(json.dumps({'text': 'Sample please, latest corrected request'})),))
    stale = ai.claim_next_summary()
    assert stale['id'] == target_task['id']
    ai.run_claimed_summary(stale, s.settings)
    assert len(s.calls) == 1
    assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',
                      (stale['id'],)).fetchone()['status'] == 'cancelled'
    summary_periods.backfill_due(pg, now + timedelta(seconds=31))
    rebuilt = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='new-creator' "
                         'AND is_current', (run['id'],)).fetchone()
    assert rebuilt['id'] != stale['id'] and rebuilt['status'] == 'queued'
    assert pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    rebuilt_job = ai.claim_next_summary()
    assert rebuilt_job['id'] == rebuilt['id']
    ai.run_claimed_summary(rebuilt_job, s.settings)
    assert len(s.calls) == 2 and old_tasks() == original
    assert 'latest corrected request' in json.dumps(s.calls[-1])
    summary_periods.backfill_due(pg, now + timedelta(seconds=62))
    pending_merge = pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND contact_id=%s '
                               'AND is_current', (run['id'], s.ids['other'])).fetchone()
    assert pending_merge and pending_merge['status'] == 'queued'
    assert pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    pg.execute('SAVEPOINT backfill_late_merge_change')
    stale_source = pg.execute('''SELECT id,input_fingerprint,status,result_ciphertext,checkpoint_ciphertext,
        processed_messages,total_messages,attempts,usage,model,response_id FROM offhours_summary_tasks
        WHERE id=%s''', (rebuilt['id'],)).fetchone()
    assert stale_source['status'] == 'succeeded'
    pg.execute("UPDATE lark_identities SET display_name='Updated creator name' WHERE contact_id=%s",
               (s.ids['other'],))
    target_account = uuid4()
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','updated_creator','@updated_creator')", (target_account,))
    pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
               "VALUES(%s,%s,'owner','confirmed','cm_confirmation')", (s.ids['other'], target_account))
    merge_job = ai.claim_next_summary()
    assert merge_job['id'] == pending_merge['id']
    ai.run_claimed_summary(merge_job, s.settings)
    assert len(s.calls) == 2 and pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',
                      (merge_job['id'],)).fetchone()['status'] == 'cancelled'
    cancelled = pg.execute('SELECT status,attempts,input_fingerprint FROM offhours_summary_tasks WHERE id=%s',
                           (merge_job['id'],)).fetchone()
    for _ in range(3):
        summary_dialogues.schedule_merges(pg, s.settings)
        assert pg.execute('SELECT status,attempts,input_fingerprint FROM offhours_summary_tasks WHERE id=%s',
                          (merge_job['id'],)).fetchone() == cancelled
    fresh_source = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='new-creator' "
                              'AND is_current', (run['id'],)).fetchone()
    assert fresh_source['id'] != stale_source['id'] and fresh_source['status'] == 'queued'
    assert fresh_source['input_fingerprint'] != stale_source['input_fingerprint']
    assert pg.execute('''SELECT id,input_fingerprint,status,result_ciphertext,checkpoint_ciphertext,
        processed_messages,total_messages,attempts,usage,model,response_id FROM offhours_summary_tasks
        WHERE id=%s''', (stale_source['id'],)).fetchone() == stale_source
    assert pg.execute('SELECT next_check_at,refresh_until FROM offhours_summary_runs WHERE id=%s',
                      (run['id'],)).fetchone() == {'next_check_at': None, 'refresh_until': end}
    assert old_tasks() == original and len(s.calls) == 2
    pg.execute('SAVEPOINT backfill_reprepared_source_changed')
    pg.execute('DELETE FROM offhours_summary_backfills')
    changed_job = ai.claim_next_summary()
    assert changed_job['id'] == fresh_source['id']
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='new-request'",
               (s.cipher.encrypt(json.dumps({'text': 'Changed again after preparation'})),))
    ai.run_claimed_summary(changed_job, s.settings)
    assert len(s.calls) == 2
    changed_cancelled = pg.execute(
        'SELECT status,attempts,input_fingerprint FROM offhours_summary_tasks WHERE id=%s',
        (changed_job['id'],),
    ).fetchone()
    assert changed_cancelled == {'status': 'cancelled', 'attempts': 1,
                                 'input_fingerprint': fresh_source['input_fingerprint']}
    for _ in range(2):
        summary_dialogues.schedule_merges(pg, s.settings)
        assert pg.execute('SELECT status,attempts,input_fingerprint FROM offhours_summary_tasks WHERE id=%s',
                          (changed_job['id'],)).fetchone() == changed_cancelled
    changed_source = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='new-creator' "
                                'AND is_current', (run['id'],)).fetchone()
    assert changed_source['id'] != fresh_source['id'] and changed_source['status'] == 'queued'
    assert changed_source['input_fingerprint'] != fresh_source['input_fingerprint']
    assert pg.execute('SELECT next_check_at,refresh_until FROM offhours_summary_runs WHERE id=%s',
                      (run['id'],)).fetchone() == {'next_check_at': None, 'refresh_until': end}
    changed_job = ai.claim_next_summary()
    assert changed_job['id'] == changed_source['id']
    ai.run_claimed_summary(changed_job, s.settings)
    assert len(s.calls) == 3 and 'Changed again after preparation' in json.dumps(s.calls[-1])
    finish()
    changed_merge = pg.execute('SELECT status,input_fingerprint FROM offhours_summary_tasks WHERE run_id=%s '
                               'AND contact_id=%s AND is_current', (run['id'], s.ids['other'])).fetchone()
    assert changed_merge['status'] == 'succeeded'
    assert changed_merge['input_fingerprint'] != cancelled['input_fingerprint']
    assert old_tasks() == original and len(s.calls) == 3
    assert pg.execute('SELECT next_check_at,refresh_until FROM offhours_summary_runs WHERE id=%s',
                      (run['id'],)).fetchone() == {'next_check_at': None, 'refresh_until': end}
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    pg.execute('ROLLBACK TO SAVEPOINT backfill_reprepared_source_changed')
    s.calls.pop()
    pg.execute('SAVEPOINT backfill_reprepared_revoke')
    fresh_job = ai.claim_next_summary()
    assert fresh_job['id'] == fresh_source['id']
    pg.execute('UPDATE authorizations SET body_consent_at=NULL WHERE id=%s', (s.ids['cm'],))
    ai.run_claimed_summary(fresh_job, s.settings)
    for _ in range(3):
        summary_dialogues.schedule_merges(pg, s.settings)
        assert ai.claim_next_summary() is None
    assert pg.execute('SELECT status,attempts FROM offhours_summary_tasks WHERE id=%s',
                      (fresh_job['id'],)).fetchone() == {'status': 'cancelled', 'attempts': 1}
    assert len(s.calls) == 2 and old_tasks() == original
    pg.execute('ROLLBACK TO SAVEPOINT backfill_reprepared_revoke')
    fresh_job = ai.claim_next_summary()
    assert fresh_job['id'] == fresh_source['id']
    ai.run_claimed_summary(fresh_job, s.settings)
    assert len(s.calls) == 3 and old_tasks() == original
    assert s.calls[-1]['users']['U1'] == {'name': 'Updated creator name', 'handles': ['@updated_creator']}
    summary_dialogues.schedule_merges(pg, s.settings)
    fresh_merge = pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND contact_id=%s '
                             'AND is_current', (run['id'], s.ids['other'])).fetchone()
    assert fresh_merge['id'] != merge_job['id']
    assert fresh_merge['status'] == 'queued' and fresh_merge['input_fingerprint'] != cancelled['input_fingerprint']
    pg.execute('SAVEPOINT backfill_missing_source_chat')
    missing_source = pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s',
                                (fresh_source['id'],)).fetchone()
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='new-creator'")
    missing_job = ai.claim_next_summary()
    assert missing_job['id'] == fresh_merge['id']
    ai.run_claimed_summary(missing_job, s.settings)
    load_chat = ai._load_chat

    def do_not_read_removed_chat(conn, state, chat_id, cutoff, **kwargs):
        assert chat_id != 'new-creator'
        return load_chat(conn, state, chat_id, cutoff, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ai, '_load_chat', do_not_read_removed_chat)
        for _ in range(3):
            summary_dialogues.schedule_merges(pg, s.settings)
            assert ai.claim_next_summary() is None
    assert pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s',
                      (fresh_source['id'],)).fetchone() == {**missing_source, 'is_current': False}
    assert pg.execute('SELECT status,attempts FROM offhours_summary_tasks WHERE id=%s',
                      (missing_job['id'],)).fetchone() == {'status': 'cancelled', 'attempts': 1}
    assert old_tasks() == original and len(s.calls) == 3
    pg.execute('ROLLBACK TO SAVEPOINT backfill_missing_source_chat')
    pg.execute('SAVEPOINT backfill_merge_db_retry')
    current = ai._current

    def transient_database_error(conn, job):
        if job['id'] == fresh_merge['id']:
            raise psycopg.errors.SerializationFailure('private-message-payload-must-not-be-logged')
        return current(conn, job)

    with caplog.at_level('INFO', logger=ai.LOGGER.name), monkeypatch.context() as patch:
        patch.setattr(ai, '_current', transient_database_error)
        for attempt in range(1, ai.MAX_ATTEMPTS + 1):
            retry = ai.claim_next_summary()
            assert retry['id'] == fresh_merge['id'] and retry['attempts'] == attempt
            ai.run_claimed_summary(retry, s.settings)
            expected_status = 'queued' if attempt < ai.MAX_ATTEMPTS else 'failed'
            summary_dialogues.schedule_merges(pg, s.settings)
            assert pg.execute('SELECT status,attempts FROM offhours_summary_tasks WHERE id=%s',
                              (retry['id'],)).fetchone() == {'status': expected_status, 'attempts': attempt}
            pg.execute('UPDATE offhours_summary_tasks SET scheduled_at=NOW() WHERE id=%s', (retry['id'],))
        assert ai.claim_next_summary() is None and len(s.calls) == 3
    assert 'SerializationFailure' in caplog.text
    assert 'private-message-payload-must-not-be-logged' not in caplog.text
    pg.execute('ROLLBACK TO SAVEPOINT backfill_merge_db_retry')
    finish()
    assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',
                      (fresh_merge['id'],)).fetchone()['status'] == 'succeeded'
    assert pg.execute('SELECT status,attempts,input_fingerprint FROM offhours_summary_tasks WHERE id=%s',
                      (merge_job['id'],)).fetchone() == cancelled
    assert old_tasks() == original and len(s.calls) == 3
    pg.execute('ROLLBACK TO SAVEPOINT backfill_late_merge_change')
    s.calls.pop()  # Roll back the in-memory observation with the isolated stale-material scenario.
    finish()
    summary_periods.backfill_due(pg, now + timedelta(seconds=93))
    finish()
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone() and len(s.calls) == 2
    view = ai.get_summary_view(pg, s.ids['cm'], day=day, period='work')
    assert len(view['events']) == 2
    tasks = pg.execute('SELECT id,input_fingerprint FROM offhours_summary_tasks WHERE run_id=%s AND is_current '
                       'ORDER BY id', (run['id'],)).fetchall()
    # A failed unrelated creator cannot block this creator's merge or cause its private source to be reread.
    pg.execute('SAVEPOINT backfill_failure_isolation')
    pg.execute("UPDATE offhours_summary_tasks SET status='failed',result_ciphertext=NULL,attempts=3 "
               "WHERE run_id=%s AND chat_id='chat' AND is_current", (run['id'],))
    old_merge = pg.execute('SELECT id,input_fingerprint,result_ciphertext FROM offhours_summary_tasks '
                           'WHERE run_id=%s AND contact_id=%s AND is_current',
                           (run['id'], s.ids['person'])).fetchone()
    previous_target_merge = pg.execute('SELECT id FROM offhours_summary_tasks WHERE run_id=%s AND contact_id=%s '
                                       'AND is_current', (run['id'], s.ids['other'])).fetchone()['id']
    add_private('retry-target-private', s.ids['cm'], 'retry-target-message', 'Routine target-only update')
    pg.execute('SELECT enqueue_summary_backfill(%s,%s)', (s.ids['other'], s.ids['cm']))
    failure_calls, source_reads = [], []
    load = ai._load_chat

    def observed_load(conn, state, chat_id, cutoff, **kwargs):
        source_reads.append(chat_id)
        return load(conn, state, chat_id, cutoff, **kwargs)

    def target_only_model(_cfg, _key, pack, _cm, **_kwargs):
        assert pack['chat_name'] == 'retry-target-private'
        failure_calls.append(pack)
        return {'text': json.dumps({'processed_refs': pack['core_refs'], 'events': []}),
                'usage': {'input_tokens': 1}}

    with monkeypatch.context() as patch:
        patch.setattr(ai, '_load_chat', observed_load)
        patch.setattr(openai_client, 'call_openai', target_only_model)
        summary_periods.backfill_due(pg, now)
        related = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s "
                             "AND chat_id='retry-target-private' AND is_current", (run['id'],)).fetchone()
        assert related and related['status'] == 'queued'
        pg.execute("UPDATE offhours_summary_tasks SET status='failed',attempts=3 WHERE id=%s", (related['id'],))
        summary_periods.backfill_due(pg, now + timedelta(seconds=31))
        blocked_signal = pg.execute('SELECT * FROM offhours_summary_backfills').fetchone()
        assert blocked_signal['not_before'] == now + timedelta(minutes=15, seconds=31)
        assert not pg.execute('SELECT 1 FROM offhours_summary_tasks WHERE run_id=%s AND contact_id=%s '
                              "AND is_current AND id<>%s",
                              (run['id'], s.ids['other'], previous_target_merge)).fetchone()
        assert not failure_calls and 'chat' not in source_reads
        pg.execute('SAVEPOINT backfill_failure_grace_expired')
        pg.execute('UPDATE offhours_summary_backfills SET requested_at=%s,not_before=%s',
                   (now - timedelta(hours=25), now))
        failed_before = pg.execute('SELECT status,attempts,is_current,result_ciphertext FROM offhours_summary_tasks '
                                   'WHERE id=%s', (related['id'],)).fetchone()
        gaps_before = pg.execute('SELECT coverage FROM offhours_summary_runs WHERE id=%s',
                                 (run['id'],)).fetchone()['coverage']['gaps']
        summary_periods.backfill_due(pg, now)
        assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
        assert pg.execute('SELECT status,attempts,is_current,result_ciphertext FROM offhours_summary_tasks '
                          'WHERE id=%s', (related['id'],)).fetchone() == failed_before
        assert pg.execute('SELECT coverage FROM offhours_summary_runs WHERE id=%s',
                          (run['id'],)).fetchone()['coverage']['gaps'] == gaps_before
        assert pg.execute("SELECT 1 FROM audit_events WHERE action='creator_summary_backfill_scheduled' "
                          "AND metadata->>'contact_id'=%s AND metadata->>'cm_id'=%s "
                          "AND metadata->>'has_failed_tasks'='true'",
                          (str(s.ids['other']), str(s.ids['cm']))).fetchone()
        assert not failure_calls and 'chat' not in source_reads
        pg.execute('ROLLBACK TO SAVEPOINT backfill_failure_grace_expired')
        pg.execute("UPDATE offhours_summary_tasks SET status='queued',attempts=0,scheduled_at=NOW() WHERE id=%s",
                   (related['id'],))
        finish()
        summary_periods.backfill_due(pg, now + timedelta(minutes=16))
        finish()
        assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
        assert len(failure_calls) == 1 and 'chat' not in source_reads
        isolated = ai.get_summary_view(pg, s.ids['cm'], day=day, period='work')
        assert len(isolated['events']) == 1
        assert {e['chat_id'] for e in isolated['events'][0]['evidence']} == {'new-creator'}
        assert 'chat' not in source_reads
        assert pg.execute('SELECT id,input_fingerprint,result_ciphertext FROM offhours_summary_tasks '
                          'WHERE id=%s', (old_merge['id'],)).fetchone() == old_merge
    pg.execute('ROLLBACK TO SAVEPOINT backfill_failure_isolation')
    pg.execute('SELECT enqueue_summary_backfill(%s,%s)', (s.ids['other'], s.ids['cm']))
    version = pg.execute('SELECT version FROM offhours_summary_backfills').fetchone()['version']
    schedule, signalled = ai._schedule_run, False

    def schedule_and_receive_new_signal(*args, **kwargs):
        nonlocal signalled
        result = schedule(*args, **kwargs)
        if not signalled:
            signalled = True
            pg.execute('SELECT enqueue_summary_backfill(%s,%s)', (s.ids['other'], s.ids['cm']))
        return result

    # An enrollment arriving during scheduling must survive the old version's conditional DELETE.
    with monkeypatch.context() as patch:
        patch.setattr(ai, '_schedule_run', schedule_and_receive_new_signal)
        summary_periods.backfill_due(pg, now)
    assert pg.execute('SELECT version FROM offhours_summary_backfills').fetchone()['version'] == version + 1
    summary_periods.backfill_due(pg, now)
    finish()
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    assert len(s.calls) == 2
    assert pg.execute('SELECT id,input_fingerprint FROM offhours_summary_tasks WHERE run_id=%s AND is_current '
                      'ORDER BY id', (run['id'],)).fetchall() == tasks
    pg.execute('SAVEPOINT legacy_backfill_trigger')
    legacy = uuid4()
    pg.execute('''INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,
        contact_id,relationship,source,created_by_authorization_id,candidate_id)
        VALUES(%s,%s,%s,%s,'representative','cm_confirmation',%s,%s)''',
        (legacy, s.account, s.ids['reviewer'], s.ids['person'], s.ids['owner'], s.candidate_id))
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    pg.execute("UPDATE creator_account_assignments SET relationship='owner' WHERE id=%s", (legacy,))
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    assert not ai._roster(pg, s.ids['reviewer'])
    pg.execute("UPDATE creator_account_assignments SET source='excel_verified' WHERE id=%s", (legacy,))
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    pg.execute('ROLLBACK TO SAVEPOINT legacy_backfill_trigger')
    # The same creator can belong to another CM, but only that CM's observed messages are exported.
    second_cm = s.ids['reviewer']
    pg.execute('''INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        VALUES(%s,%s,%s,'ai')''', (uuid4(), s.ids['other'], second_cm))
    summary_periods.backfill_due(pg, now)
    assert len(s.calls) == 2 and not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone()
    add_private('second-cm-private', second_cm, 'second-cm-request', 'Second CM private sample request')
    pg.execute('SELECT enqueue_summary_backfill(%s,%s)', (s.ids['other'], second_cm))
    summary_periods.backfill_due(pg, now)
    finish()
    assert len(s.calls) == 3 and 'Second CM private' in json.dumps(s.calls[-1])
    summary_periods.backfill_due(pg, now + timedelta(seconds=31))
    finish()
    assert not pg.execute('SELECT 1 FROM offhours_summary_backfills').fetchone() and len(s.calls) == 3
    assert 'Second CM private' not in json.dumps(s.calls[:-1])
    second_run = pg.execute("SELECT * FROM offhours_summary_runs WHERE responsible_authorization_id=%s "
                            "AND report_date=%s AND period='work'", (second_cm, day)).fetchone()
    assert second_run and second_run['id'] != run['id']
    assert pg.execute('SELECT chat_id FROM offhours_summary_tasks WHERE run_id=%s AND chat_id IS NOT NULL '
                      'AND is_current', (second_run['id'],)).fetchall() == [{'chat_id': 'second-cm-private'}]
    assert old_tasks() == original
    # Seven-day execution guards cover discovery, ordinary scheduling, claim and an already-held lease.
    pg.execute('SAVEPOINT backfill_ancient_execution')
    pg.execute('UPDATE offhours_summary_runs SET next_check_at=NULL')
    pg.execute('UPDATE offhours_summary_runs SET coverage=%s::jsonb,next_check_at=%s WHERE id=%s',
               (json.dumps({'policy': ai.POLICY_VERSION}), now, ancient))
    source = pg.execute("SELECT result_ciphertext,checkpoint_ciphertext FROM offhours_summary_tasks "
                        "WHERE run_id=%s AND chat_id='chat' AND is_current", (run['id'],)).fetchone()
    ancient_source = uuid4()
    pg.execute('''INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff,status,
        result_ciphertext,checkpoint_ciphertext,attempts,usage)
        VALUES(%s,%s,'chat','ancient-copied-source',%s,'succeeded',%s,%s,1,%s::jsonb)''',
        (ancient_source, ancient, ancient_end, source['result_ciphertext'], source['checkpoint_ciphertext'],
         json.dumps({'input_tokens': 9})))
    summary_dialogues.schedule_merges(pg, s.settings)
    assert not pg.execute('SELECT 1 FROM offhours_summary_tasks WHERE run_id=%s AND contact_id IS NOT NULL',
                          (ancient,)).fetchone()
    assert ai.schedule_if_due(pg, force=True) == 0
    assert pg.execute('SELECT next_check_at FROM offhours_summary_runs WHERE id=%s',
                      (ancient,)).fetchone()['next_check_at'] == now
    ancient_merge = uuid4()
    pg.execute('''INSERT INTO offhours_summary_tasks(id,run_id,contact_id,input_fingerprint,cutoff,
        result_ciphertext,checkpoint_ciphertext,attempts,usage)
        VALUES(%s,%s,%s,'ancient-existing-merge',%s,%s,%s,2,%s::jsonb)''',
        (ancient_merge, ancient, s.ids['person'], ancient_end, source['result_ciphertext'],
         source['checkpoint_ciphertext'], json.dumps({'input_tokens': 17, 'output_tokens': 3})))
    assert ai.claim_next_summary() is None
    pg.execute("UPDATE offhours_summary_tasks SET status='running',lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s",
               (ancient_merge,))
    with pytest.raises(ai.SummaryInputError):
        ai._current(pg, {'id': ancient_merge, 'attempts': 2})
    assert len(s.calls) == 3 and old_tasks() == original
    pg.execute('ROLLBACK TO SAVEPOINT backfill_ancient_execution')




def finish_merges(pg, s):
    summary_dialogues.schedule_merges(pg, s.settings)
    for _ in range(50):
        job = ai.claim_next_summary() or ai.claim_next_summary(interactive=True)
        if job is None:
            return
        ai.run_claimed_summary(job, s.settings)
    raise AssertionError('queue did not finish')


def test_merge_batches_bound_calls_preserve_events_and_replay_paid_pair(pg, seeded, monkeypatch):
    s = seeded
    for index in range(1, 20):
        s.add(f"case-{index}", text=f"Distinct enquiry TRACK-{index:03d}")
    calls = []

    def no_match(ref):
        return {"same": False, "basis_kind": "none", "basis_value": "", "quote_a_ref": ref,
                "quote_b_ref": ref, "title": "", "summary": "", "latest_progress": "",
                "handling_status": "uncertain", "handling_reason": "没有具体关联。",
                "handling_evidence_refs": [ref], "reply_evidence_refs": []}

    def model(_cfg, _key, pack, _cm, **_kwargs):
        calls.append(pack)
        if "candidates" in pack:
            assert 1 <= len(pack["candidates"]) <= 8
            return {"text": json.dumps({"match_id": None, **no_match(next(iter(pack["evidence_messages"])))})}
        return {"text": json.dumps({"processed_refs": pack["core_refs"], "events": [
            sample_event(evidence_refs=[ref]) for ref in pack["core_refs"]]})}

    monkeypatch.setattr(openai_client, "call_openai", model)
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    summary_dialogues.schedule_merges(pg, s.settings)
    task = ai.claim_next_summary()
    ai.run_claimed_summary(task, s.settings)  # The first event needs no model comparison.
    with pg.transaction(force_rollback=True):
        legacy_job = ai.claim_next_summary()
        checkpoint = summary_dialogues.decode(legacy_job["checkpoint_ciphertext"], s.settings)
        checkpoint.pop("merge_mode")
        checkpoint["against"] = 0
        run = pg.execute("SELECT * FROM offhours_summary_runs WHERE id=%s", (task["run_id"],)).fetchone()
        source, _ = summary_dialogues.merge_input(pg, run, legacy_job, s.settings)
        left, right = checkpoint["events"][0], source[1]
        refs = summary_dialogues.merged_evidence(left, right)
        query = {"a": {k: v for k, v in left.items() if k != "reason"},
                 "b": {k: v for k, v in right.items() if k != "reason"}, "evidence_messages": refs}
        rules = summary_dialogues._event_instructions(legacy_job, s.settings, "merge")
        checkpoint["response_received"] = {"signature": ai._hash([
            query, summary_dialogues.merge_format(refs, left, right), rules]),
            "response": {"text": json.dumps(no_match(next(iter(refs))))}}
        legacy_job["checkpoint_ciphertext"] = summary_dialogues.encode(checkpoint, s.settings)
        pg.execute("UPDATE offhours_summary_tasks SET checkpoint_ciphertext=%s WHERE id=%s",
                   (legacy_job["checkpoint_ciphertext"], legacy_job["id"]))
        before = len(calls)
        ai.run_claimed_summary(legacy_job, s.settings)
        saved = pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=%s", (legacy_job["id"],)).fetchone()
        restored = summary_dialogues.decode(saved["checkpoint_ciphertext"], s.settings)
        assert saved["status"] == "queued" and restored["merge_mode"] == "batch" and "against" not in restored
        assert len(calls) == before  # An already-paid legacy pair is consumed locally exactly once.
    with pg.transaction(force_rollback=True), monkeypatch.context() as patch:
        build = summary_dialogues.merge_batch

        def revoked_after_preparation(*args):
            pack = build(*args)
            pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='chat'")
            return pack

        patch.setattr(summary_dialogues, "merge_batch", revoked_after_preparation)
        blocked = ai.claim_next_summary()
        before = len(calls)
        ai.run_claimed_summary(blocked, s.settings)
        assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE id=%s",
                          (blocked["id"],)).fetchone()["status"] == "cancelled"
        assert len(calls) == before  # Live per-chat revocation still blocks paid dispatch.
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert len(view["events"]) == 20
    assert {e["message_id"] for event in view["events"] for e in event["evidence"]} == {
        "m1", *(f"case-{index}" for index in range(1, 20))}
    assert sum("candidates" in call for call in calls) == 19  # Not the previous 190 pair requests.
    ledger = view["events"]
    candidate = {**ledger[0], "evidence": [{**ledger[0]["evidence"][0], "message_id": "later",
        "parent_message_id": ledger[0]["evidence"][0]["message_id"], "text": "That is now done"}]}
    _, _, matches = summary_dialogues.merge_batch(ledger, candidate)
    assert "0" in matches  # A reply anchor remains eligible beyond the eight most recent events.


def test_full_shift_chunks_resume_filter_speakers_merge_and_recheck_access(pg, seeded, monkeypatch):
    s = seeded
    pinned_rules = assistant_rules.event_snapshot(pg, s.settings)
    expected_rules = pinned_rules
    long_body = 'Long message beginning ' + 'z' * 19000 + ' final-marker'
    for i in range(215):
        s.add(f'noise-{i}', text=f'Routine {i}', at=s.start + timedelta(minutes=i + 1))
    s.add('long', text=long_body, at=s.start + timedelta(hours=5))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
               (s.cipher.encrypt(json.dumps({'text': 'Please send sample order ABC-123'})),))
    s.add('ancient', text='too old secret', at=s.start - timedelta(days=8))
    s.add('history', text='Earlier sample ABC-123', at=s.start - timedelta(days=6))
    pg.execute("UPDATE chat_messages SET parent_id='history' WHERE message_id='m1'")
    pg.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) VALUES(%s,%s,%s,'manual')",
        (uuid4(), s.ids["other"], s.ids["cm"]),
    )
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('second','Related campaign group','group')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,
        backfill_window_start_at,last_incremental_at,sync_status) VALUES(%s,'second',TRUE,%s,%s,'succeeded')""",
               (s.ids['cm'], s.start - timedelta(days=9), s.end))
    for who in ('person', 'other'):
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('second',%s)", ('ou_' + who,))
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
            body_ciphertext,message_create_time) VALUES('second',%s,%s,'text',%s,%s)""",
                   (who, 'ou_' + who, s.cipher.encrypt(json.dumps({'text':
                    'Sample order ABC-123 arrived' if who == 'person' else 'unrelated other creator secret'})),
                    s.start + timedelta(hours=6)))
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
            "VALUES(%s,'second',%s)",
            (s.ids["cm"], who),
        )
    pg.execute("""INSERT INTO lark_identities(open_id,display_name,tenant_classification)
        VALUES('ou_reviewer','reviewer','internal')""")
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,
        message_create_time,parent_id) VALUES('second','colleague','ou_reviewer','text',%s,%s,'person')""",
        (s.cipher.encrypt(json.dumps({'text': 'I can help check sample order ABC-123'})),
         s.start + timedelta(hours=6, minutes=15)))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'second','colleague')""", (s.ids['cm'],))
    calls, failed = [], False
    saved_receipt = summary_dialogues._receipt
    crashed = local_failed = False

    def receipt(*args, **kwargs):
        nonlocal crashed, local_failed
        saved_receipt(*args, **kwargs)
        if kwargs.get('received'):
            if not crashed:
                crashed = True
                raise SystemExit('fixture interrupted after durable response')
            if not local_failed:
                local_failed = True
                raise psycopg.errors.SerializationFailure('fixture local save retry')

    monkeypatch.setattr(summary_dialogues, '_receipt', receipt)

    def model(_cfg, _key, pack, _cm, **kwargs):
        nonlocal failed
        calls.append(pack)
        stage = "merge" if "candidates" in pack else "extract"
        assert assistant_rules.event_instructions(expected_rules, stage) in kwargs["instructions"]
        assert ("new-event-rules-marker" in kwargs["instructions"]) == (expected_rules != pinned_rules)
        if len(calls) == 1:
            for key, document in pinned_rules["documents"].items():
                assistant_rules.save_document(pg, s.settings, s.ids["owner"], key,
                    document["title"], document["description"], "manual", "published", "owner",
                    document["text"] + "\nnew-event-rules-marker", document["revision"])
        if 'candidates' in pack:
            a, b = pack['candidates'][0], pack['candidate']
            return {'text': json.dumps({'match_id': a['id'], 'same': True,
                'basis_kind': 'identifier', 'basis_value': 'ABC-123',
                'quote_a_ref': a['evidence_refs'][0], 'quote_b_ref': b['evidence_refs'][0],
                'title': '样品订单', 'summary': '样品订单已送达。', 'latest_progress': '达人确认收到。',
                'handling_status': 'resolved', 'handling_reason': '达人已确认对应订单收到。',
                'handling_evidence_refs': [r for r, e in pack['evidence_messages'].items() if 'arrived' in e['text']],
                'reply_evidence_refs': [r for r, e in pack['evidence_messages'].items() if e.get('is_staff')]})}
        user = next(iter(pack['users']))
        assert {m['speaker'] for m in pack['messages']} <= {user, *pack['staff']}
        assert len(pack['users']) == 1
        if pack['users'][user]['name'] == 'person':
            assert 'other creator secret' not in json.dumps(pack)
            assert 'outsider secret' not in json.dumps(pack)
        assert 'future CM reply' not in json.dumps(pack) and 'too old secret' not in json.dumps(pack)
        if len(calls) == 2 and not failed:
            failed = True
            raise openai_client.OpenAIRequestError('synthetic transient failure', retryable=True,
                                                   telemetry={'http_status': 429})
        targets = [m for m in pack['messages'] if m['ref'] in pack['core_refs'] and m['speaker'] == user
                   and m['message_id'] in ('m1', 'person')]
        return {'text': json.dumps({'processed_refs': pack['core_refs'], 'events': [sample_event(
            user=user, evidence_refs=[m['ref']], handle=None,
            reply_evidence_refs=[r['ref'] for r in pack['messages'] if r['message_id'] == 'colleague'])
            for m in targets]}), 'response_id': f'resp-summary-{len(calls)}',
            'usage': {'input_tokens': 7, 'output_tokens': 2}}

    monkeypatch.setattr(openai_client, 'call_openai', model)
    queued = s.save()
    saved_run = pg.execute("SELECT * FROM offhours_summary_runs WHERE id=%s", (queued["run_id"],)).fetchone()
    assert saved_run["event_rules_digest"] == pinned_rules["digest"]
    assert summary_dialogues.decode(saved_run["event_rules_ciphertext"], s.settings) == pinned_rules
    assert pinned_rules["documents"]["event-extraction"]["text"] not in saved_run["event_rules_ciphertext"]
    with pg.transaction(force_rollback=True):
        old_job = ai.claim_next_summary()
        newer_lease = old_job['lease_until'] + timedelta(seconds=1)
        pg.execute("UPDATE offhours_summary_tasks SET lease_until=%s WHERE id=%s", (newer_lease, old_job['id']))
        ai.run_claimed_summary(old_job, s.settings)
        ai._finish(old_job, 'failed', 'late worker must not terminate the new phase')
        assert pg.execute("SELECT status,lease_until FROM offhours_summary_tasks WHERE id=%s",
                          (old_job['id'],)).fetchone() == {'status': 'running', 'lease_until': newer_lease}
    with pg.transaction(force_rollback=True):
        damaged = ai.claim_next_summary()
        pg.execute("UPDATE offhours_summary_tasks SET checkpoint_ciphertext='broken',"
                   "lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (damaged['id'],))
        ai.claim_next_summary()
        assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE id=%s",
                          (damaged['id'],)).fetchone()['status'] == 'failed'
    for _ in range(100):
        job = ai.claim_next_summary()
        if not job:
            break
        try:
            ai.run_claimed_summary(job, s.settings)
        except SystemExit:
            pg.execute("UPDATE offhours_summary_tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
                       (job['id'],))
        pg.execute("UPDATE offhours_summary_tasks SET scheduled_at=NOW() WHERE status='queued'")
        ai.ai_dispatch.resume(pg)  # Advance the synthetic 429 without waiting on the fixture clock.
    else:
        pytest.fail('conversation queue did not finish')
    rows = pg.execute("SELECT * FROM offhours_summary_tasks WHERE is_current").fetchall()
    assert all(t['status'] == 'succeeded' for t in rows) and crashed and local_failed
    for task in rows:
        state = summary_dialogues.decode(task['checkpoint_ciphertext'], s.settings)
        assert not state.get('response_received') and not state.get('provider_call')
        assert task['usage'] == state['usage']
        assert task['usage'].get('input_tokens', 0) == sum(
            receipt['usage'].get('input_tokens', 0) for receipt in state['receipts'])
    assert sum(t['processed_messages'] for t in rows) > 215
    first_pack = calls[0]
    assert sum(p == first_pack for p in calls) == 1  # Successful chunks were not repeated after the failure.
    person_packs = [p for p in calls if 'messages' in p and next(iter(p['users'].values()))['name'] == 'person']
    long_parts = {m['ref']: m['text'] for p in person_packs for m in p['messages']
                  if m['message_id'] == 'long' and m['ref'] in p['core_refs']}
    assert ''.join(long_parts.values()) == long_body
    assert any(m['message_id'] == 'history' for p in person_packs for m in p['messages'])
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids['owner'])
    assert view['run']['counts']['failed'] == 0 and view['run']['status'] == 'completed'
    assert len(view['events']) == 1
    assert {e['chat_id'] for e in view['events'][0]['evidence']} == {'chat', 'second'}
    assert next(e for e in view['events'][0]['evidence'] if e['message_id'] == 'm1')[
        'parent_message_id'] == 'history'
    assert view['events'][0]['latest_progress'] == '达人确认收到。'
    assert view['events'][0]['handling_status'] == 'resolved'
    assert view['events'][0]['handling_evidence'][0]['chat_id'] == 'second'
    assert view['events'][0]['replied_by'] == ['reviewer']
    assert view['events'][0]['reply_evidence'][0]['message_id'] == 'colleague'
    assert assistant_rules.event_snapshot(pg, s.settings)["digest"] != saved_run["event_rules_digest"]
    assert "new-event-rules-marker" not in json.dumps(ai.get_summary_view(pg, s.ids["cm"]))
    completed = pg.execute("SELECT id,input_fingerprint,status FROM offhours_summary_tasks WHERE is_current "
                           "ORDER BY id").fetchall()
    assert s.save(key="")["run_id"] == queued["run_id"]
    assert pg.execute("SELECT id,input_fingerprint,status FROM offhours_summary_tasks WHERE is_current "
                      "ORDER BY id").fetchall() == completed
    assert ai.claim_next_summary() is None
    # Replace one source/its affected merges only; mixed versions must remain readable together.
    with pg.transaction(force_rollback=True):
        expected_rules = assistant_rules.event_snapshot(pg, s.settings)
        override_cipher = summary_dialogues.encode(expected_rules, s.settings)
        original_source = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s "
                                     "AND chat_id='chat' AND is_current", (saved_run["id"],)).fetchone()
        people = [UUID(user["contact_id"]) for user in summary_dialogues.decode(
            original_source["result_ciphertext"], s.settings)["users"].values()]
        original_merges = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s "
            "AND contact_id=ANY(%s::uuid[]) AND is_current", (saved_run["id"], people)).fetchall()
        affected_ids = [original_source["id"], *(row["id"] for row in original_merges)]
        unchanged_ids = [row["id"] for row in pg.execute("SELECT id FROM offhours_summary_tasks "
            "WHERE run_id=%s AND id<>ALL(%s::uuid[]) ORDER BY id",
            (saved_run["id"], affected_ids)).fetchall()]
        unchanged = pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=ANY(%s::uuid[]) ORDER BY id",
                               (unchanged_ids,)).fetchall()
        override = {"event_rules_ciphertext": override_cipher, "event_rules_digest": expected_rules["digest"]}
        material = ai._load_chat(pg, ai._task_run(saved_run, override), "chat", original_source["cutoff"],
                                 settings=s.settings)
        pg.execute("UPDATE offhours_summary_tasks SET is_current=FALSE WHERE id=%s", (original_source["id"],))
        new_id = uuid4()
        pg.execute("""INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff,
            event_rules_ciphertext,event_rules_digest) VALUES(%s,%s,'chat',%s,%s,%s,%s)""",
            (new_id, saved_run["id"], material["fingerprint"], original_source["cutoff"],
             override_cipher, expected_rules["digest"]))
        count_before = len(calls)
        for _ in range(50):
            job = ai.claim_next_summary()
            if job is None:
                break
            assert job["id"] == new_id and job["event_rules_digest"] == expected_rules["digest"]
            ai.run_claimed_summary(job, s.settings)
        else:
            pytest.fail("targeted source did not finish")
        assert len(calls) > count_before
        assert all(pack["chat_name"] == "Alice X TT" for pack in calls[count_before:])
        assert summary_dialogues.schedule_merges(pg, s.settings) == len(original_merges)
        new_merges = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s "
            "AND contact_id=ANY(%s::uuid[]) AND is_current", (saved_run["id"], people)).fetchall()
        new_merge_ids = [row["id"] for row in new_merges]
        assert len(new_merges) == len(original_merges) and not set(new_merge_ids) & set(affected_ids)
        assert all(row["status"] == "queued" for row in new_merges)
        pg.execute("UPDATE offhours_summary_tasks SET event_rules_ciphertext=%s,event_rules_digest=%s "
                   "WHERE id=ANY(%s::uuid[])", (override_cipher, expected_rules["digest"], new_merge_ids))
        for _ in range(50):
            job = ai.claim_next_summary()
            if job is None:
                break
            assert job["id"] in new_merge_ids and job["event_rules_digest"] == expected_rules["digest"]
            ai.run_claimed_summary(job, s.settings)
        else:
            pytest.fail("targeted merge did not finish")
        refreshed = ai.get_summary_view(pg, s.ids["cm"], run_id=saved_run["id"], settings=s.settings)
        assert refreshed["run"]["status"] == "completed" and len(refreshed["events"]) == 1, refreshed["run"]
        assert {item["chat_id"] for item in refreshed["events"][0]["evidence"]} == {"chat", "second"}
        assert pg.execute("SELECT * FROM offhours_summary_runs WHERE id=%s", (saved_run["id"],)).fetchone()[
            "event_rules_ciphertext"] == saved_run["event_rules_ciphertext"]
        for old in (original_source, *original_merges):
            assert pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=%s", (old["id"],)).fetchone() == {
                **old, "is_current": False}
        assert pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=ANY(%s::uuid[]) ORDER BY id",
                          (unchanged_ids,)).fetchall() == unchanged
        prepared = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s ORDER BY id",
                              (saved_run["id"],)).fetchall()
        assert summary_dialogues.schedule_merges(pg, s.settings) == 0
        state = pg.execute("SELECT *,id AS run_id FROM offhours_summary_runs WHERE id=%s",
                           (saved_run["id"],)).fetchone()
        state["refresh_until"] = pg.execute("SELECT refresh_until FROM offhours_summary_settings WHERE id").fetchone()[
            "refresh_until"]
        assert ai._schedule_run(pg, state, original_source["cutoff"]) == 0
        assert pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s ORDER BY id",
                          (saved_run["id"],)).fetchall() == prepared
        s.add("after-targeted-rerun", text="New sample order ABC-123 follow-up",
              at=original_source["cutoff"] - timedelta(minutes=1))
        assert ai._schedule_run(pg, state, original_source["cutoff"]) == 1
        successor = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s "
                               "AND chat_id='chat' AND is_current", (saved_run["id"],)).fetchone()
        assert successor["id"] != new_id and successor["status"] == "queued"
        assert successor["event_rules_ciphertext"] == override_cipher
        assert successor["event_rules_digest"] == expected_rules["digest"]
        assert pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=ANY(%s::uuid[]) ORDER BY id",
                          (unchanged_ids,)).fetchall() == unchanged
    expected_rules = pinned_rules
    # Changed access hides the merged narrative as well as its evidence.
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='second'")
    assert ai.get_summary_view(pg, s.ids['owner'])['events'] == []
    # A shared topic without a concrete identifier is not sufficient merge proof.
    e = {'evidence': [{'text': 'payment please'}]}
    with pytest.raises(ai.InvalidModelOutput):
        summary_dialogues.combine(json.dumps({'same': True, 'basis_kind': 'identifier', 'basis_value': 'payment',
            'quote_a_ref': 'E1', 'quote_b_ref': 'E1', 'title': 'x', 'summary': 'x', 'latest_progress': 'x',
            'handling_status': 'unresolved', 'handling_reason': '未完成', 'handling_evidence_refs': ['E1'],
            'reply_evidence_refs': []}), e, e)


def test_reply_draft_latest_dialogue_translation_staleness_and_access(pg, seeded, monkeypatch):
    from app import summary_replies as replies
    s = seeded
    monkeypatch.setattr(replies, "connection", lambda: nullcontext(pg))
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    event_id = ai.get_summary_view(pg, s.ids["cm"])["events"][0]["id"]
    pg.execute("""INSERT INTO lark_identities(open_id,display_name,tenant_classification)
        VALUES('ou_reviewer','reviewer','internal')""")
    s.add("colleague-current", who="reviewer", text="I've asked fulfillment to check sample A-200.",
          at=datetime.now(UTC))
    long_text = "Sample A-200: thanks for explaining that, lovely. " * 900
    s.add("long-current", who="cm", text=long_text, at=datetime.now(UTC))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('reply-group','Shared group','group')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,
        last_incremental_at,sync_status) VALUES(%s,'reply-group',FALSE,NOW(),'succeeded')""", (s.ids["cm"],))
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('reply-group','ou_person')")
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,
        message_create_time) VALUES('reply-group','broadcast','ou_cm','text',%s,NOW())""",
        (s.cipher.encrypt(json.dumps({"text": "OUTSIDER_GROUP_SECRET"})),))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'reply-group','broadcast')""", (s.ids["cm"],))
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,
        message_create_time) VALUES('reply-group','later-payment','ou_person','text',%s,NOW())""",
        (s.cipher.encrypt(json.dumps({"text": "Please check the payment for sample A-200."})),))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'reply-group','later-payment')""", (s.ids["cm"],))
    with pytest.raises(PermissionError):
        replies.enqueue(pg, s.ids["reviewer"], event_id, s.settings)
    payment_knowledge = assistant_rules.save_document(pg, s.settings, s.ids["owner"], None,
        "付款核对资料", "仅用于付款咨询", "knowledge", "published", "all", "PAYMENT_KNOWLEDGE_V1", 0,
        reply_usable=True)
    assistant_rules.save_document(pg, s.settings, s.ids["owner"], None,
        "活动资料", "仅用于活动报名", "knowledge", "published", "all", "UNSELECTED_EVENT_KNOWLEDGE", 0,
        reply_usable=True)
    assistant_rules.save_document(pg, s.settings, s.ids["owner"], None,
        "内部诊断", "内部资料", "knowledge", "published", "all", "UNAPPROVED_INTERNAL_KNOWLEDGE", 0)
    calls = []
    instructions_seen = []
    saved_receipt = summary_dialogues._receipt
    crashed = local_failed = False

    def receipt(*args, **kwargs):
        nonlocal crashed, local_failed
        saved_receipt(*args, **kwargs)
        if kwargs.get("reply") and kwargs.get("received"):
            if not crashed:
                crashed = True
                raise SystemExit("fixture reply interrupted after durable response")
            if not local_failed and '"outcome"' in args[3]["text"]:
                local_failed = True
                raise psycopg.errors.SerializationFailure("fixture draft local save retry")

    monkeypatch.setattr(summary_dialogues, "_receipt", receipt)

    def model(cfg, key, pack, actor, **kwargs):
        calls.append(pack)
        instructions_seen.append(kwargs["instructions"])
        receipt = {"response_id": f"resp-reply-{len(calls)}", "usage": {"input_tokens": 5, "output_tokens": 3}}
        assert "unrelated outsider secret" not in json.dumps(pack)
        assert "OUTSIDER_GROUP_SECRET" not in json.dumps(pack)
        assert "future CM reply" not in json.dumps(pack)
        assert "UNAPPROVED_INTERNAL_KNOWLEDGE" not in json.dumps(pack) + kwargs["instructions"]
        assert "UNSELECTED_EVENT_KNOWLEDGE" not in json.dumps(pack) + kwargs["instructions"]
        if "document_catalog" in pack:
            assert "PAYMENT_KNOWLEDGE" not in json.dumps(pack) + kwargs["instructions"]
            return {**receipt, "text": json.dumps({"document_ids": [payment_knowledge]})}
        if "edited_draft" in pack:
            assert set(pack) == {"edited_draft"}
            return {**receipt, "text": json.dumps({"chinese_translation": "我可以帮你核对这笔 £200 的付款。"})}
        if "core_refs" in pack:
            return {**receipt, "text": json.dumps({"processed_refs": pack["core_refs"],
                "relevant_refs": pack["core_refs"],
                "style_refs": [m["ref"] for m in pack["messages"] if m["speaker"] == "CM"]})}
        resolved = any("now resolved" in m["text"] for m in pack["messages"])
        return {**receipt, "text": json.dumps({"outcome": "resolved" if resolved else "ready", "language": "en",
            "draft": "" if resolved else "Happy to check that £200 payment with you.",
            "chinese_translation": "" if resolved else "我可以帮你核对这笔 £200 的付款。",
            "explanation": "后续已确认解决。" if resolved else "尚未看到处理完成结果。",
            "checks": [] if resolved else ["确认能够进行核对。"],
            "evidence_refs": [m["ref"] for m in pack["messages"]]})}

    monkeypatch.setattr(openai_client, "call_openai", model)
    job_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    assert replies.enqueue(pg, s.ids["cm"], event_id, s.settings) == job_id
    assistant_rules.save_document(pg, s.settings, s.ids["owner"], payment_knowledge,
        "付款核对资料", "仅用于付款咨询", "knowledge", "published", "all", "PAYMENT_KNOWLEDGE_V2", 1,
        reply_usable=True)

    def finish():
        for _ in range(12):
            job = replies.claim()
            if not job:
                break
            try:
                replies.run(job, s.settings)
            except SystemExit:
                pg.execute("UPDATE summary_reply_tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
                           (job["id"],))
            pg.execute("UPDATE summary_reply_tasks SET scheduled_at=NOW() WHERE status='queued'")
        else:
            pytest.fail("reply queue did not finish")
        assert not pg.execute("SELECT 1 FROM summary_reply_tasks WHERE status<>'succeeded'").fetchone()

    finish()
    result = replies.read(pg, s.ids["cm"], job_id, s.settings, fresh=True)
    assert result["result"]["outcome"] == "ready" and result["result"]["chinese_translation"]
    assert calls[-1]["sync_gaps"] == ["reply-group"]
    assert crashed and local_failed and sum(p == calls[0] for p in calls) == 1
    assert sum("checked_at" in p for p in calls) == 1
    assert result["result"]["checked_at"] == calls[-1]["checked_at"]
    processed = {m["ref"]: m for pack in calls if "core_refs" in pack for m in pack["messages"]
                 if m["ref"] in pack["core_refs"] and m["message_id"] == "long-current"}
    assert "".join(m["text"] for m in sorted(processed.values(), key=lambda m: m["part"])) == long_text
    colleagues = [m for pack in calls for m in pack.get("messages", []) if m.get("message_id") == "colleague-current"]
    assert colleagues and all(m["is_staff"] and m["speaker"] != "CM" for m in colleagues)
    stored = pg.execute("SELECT result_ciphertext,checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s",
                        (job_id,)).fetchone()
    assert "Happy" not in stored["result_ciphertext"]
    state = summary_dialogues.decode(stored["checkpoint_ciphertext"], s.settings)
    assert state["usage"] == {"input_tokens": len(calls) * 5, "output_tokens": len(calls) * 3}
    assert len(state["receipts"]) == len(calls) and not state.get("response_received")
    assert state["document_ids"] == [payment_knowledge] and state["reply_rules"]["entry"] == "event"
    assert sum("document_catalog" in pack for pack in calls) == 1
    assert calls[-1]["business_documents"][0]["key"] == payment_knowledge
    assert calls[-1]["business_documents"][0]["text"] == "PAYMENT_KNOWLEDGE_V1"
    assert calls[-1]["business_documents"][0]["document_ref"] == "doc." + payment_knowledge
    with pytest.raises(PermissionError):
        replies.read(pg, s.ids["owner"], job_id, s.settings)
    s.add("new-before-translation", text="Thanks, I will wait for the payment check.", at=datetime.now(UTC))
    translated = replies.enqueue(pg, s.ids["cm"], event_id, s.settings, parent_id=job_id,
                                 text="Happy to check the £200 payment with you.")
    with monkeypatch.context() as patch:
        patch.setattr(replies, "collect", lambda *_args, **_kwargs: pytest.fail("translation reread chat bodies"))
        finish()
    translated_result = replies.read(pg, s.ids["cm"], translated, s.settings, fresh=True)
    assert translated_result["result"]["draft"].startswith("Happy")
    assert translated_result["reason_code"] == "new_messages"
    translated_state = summary_dialogues.decode(pg.execute(
        "SELECT checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s", (translated,)
    ).fetchone()["checkpoint_ciphertext"], s.settings)
    assert translated_state["reply_rules"] == state["reply_rules"]
    assert translated_state["document_ids"] == state["document_ids"]
    with pg.transaction(force_rollback=True):
        denied_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings, parent_id=job_id,
                                    text="Please check this payment.")
        denied_job = replies.claim()
        paid_count = len(calls)
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='reply-group'")
        for saved_id in (job_id, translated):
            with pytest.raises(ai.SummaryInputError, match="原文读取权限"):
                replies.read(pg, s.ids["cm"], saved_id, s.settings)
        replies.run(denied_job, s.settings)
        assert len(calls) == paid_count
        assert pg.execute("SELECT status FROM summary_reply_tasks WHERE id=%s", (denied_id,)).fetchone()[
            "status"] == "stale"
    with pg.transaction(force_rollback=True):
        assistant_rules.save_document(pg, s.settings, s.ids["owner"], payment_knowledge,
            "付款核对资料", "仅用于付款咨询", "knowledge", "draft", "all", "PAYMENT_KNOWLEDGE_V2", 2,
            reply_usable=True)
        for saved_id in (job_id, translated):
            with pytest.raises(PermissionError):
                replies.read(pg, s.ids["cm"], saved_id, s.settings)
    s.add("resolved-later", text="This specific issue is now resolved, thank you.", at=datetime.now(UTC))
    paid_count = len(calls)
    for previous_id in (job_id, translated):
        changed = replies.read(pg, s.ids["cm"], previous_id, s.settings, fresh=True)
        assert changed["status"] == "stale" and changed["reason_code"] == "new_messages"
        assert changed["result"]["draft"].startswith("Happy")
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='long-current'",
                   (s.cipher.encrypt(json.dumps({"text": "Changed payment terms."})),))
        changed = replies.read(pg, s.ids["cm"], job_id, s.settings, fresh=True)
        assert changed["status"] == "stale" and changed.get("reason_code") != "new_messages"
        assert not changed.get("result")
    assert len(calls) == paid_count
    latest = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    finish()
    latest_result = replies.read(pg, s.ids["cm"], latest, s.settings, fresh=True)["result"]
    assert latest_result["outcome"] == "resolved" and latest_result["draft"] == ""
    assert ai.get_summary_view(pg, s.ids["cm"])["events"][0]["handling_status"] == "unresolved"
    # Existing paid phases without a document pin retain their original request and validator.
    legacy_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    pg.execute("UPDATE summary_reply_tasks SET checkpoint_ciphertext=NULL WHERE id=%s", (legacy_id,))
    before_legacy = len(calls)
    crashed = False
    finish()
    legacy_state = summary_dialogues.decode(pg.execute(
        "SELECT checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s", (legacy_id,)
    ).fetchone()["checkpoint_ciphertext"], s.settings)
    assert "reply_rules" not in legacy_state and "document_ids" not in legacy_state
    assert all("document_catalog" not in pack and "business_documents" not in pack
               for pack in calls[before_legacy:])
    assert instructions_seen[before_legacy] == replies.SELECT_INSTRUCTIONS
    # A late response cannot replace a newer phase even when the attempt number repeats.
    late_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    late = replies.claim()
    old = {"provider_call": {"id": str(uuid4()), "signature": "old", "model": "fixture"}}
    newer = {"provider_call": {"id": str(uuid4()), "signature": "new", "model": "fixture"}}
    new_job = {**late, "lease_until": late["lease_until"] + timedelta(seconds=1)}
    pg.execute("UPDATE summary_reply_tasks SET checkpoint_ciphertext=%s,lease_until=%s WHERE id=%s",
               (summary_dialogues.encode(newer, s.settings), new_job["lease_until"], late_id))
    with pytest.raises(ai.SummaryInputError):
        summary_dialogues._store_receipt(late, s.settings, old,
            {"text": "must-not-replace", "usage": {"input_tokens": 2}}, reply=True, received=True)
    saved = summary_dialogues.decode(pg.execute("SELECT checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s",
                                              (late_id,)).fetchone()["checkpoint_ciphertext"], s.settings)
    assert saved["provider_call"] == newer["provider_call"] and not saved.get("response_received")
    assert saved["usage"] == {"input_tokens": 2} and saved["receipts"][0]["call_id"] == old["provider_call"]["id"]
    replies.run(late, s.settings)
    row = pg.execute("SELECT status,lease_until FROM summary_reply_tasks WHERE id=%s", (late_id,)).fetchone()
    assert row == {"status": "running", "lease_until": new_job["lease_until"]}
    summary_dialogues._store_receipt(new_job, s.settings, newer,
        {"text": "new-phase", "usage": {"input_tokens": 3}}, reply=True, received=True)
    with pytest.raises(ai.SummaryInputError):
        summary_dialogues._store_receipt(late, s.settings, old,
            {"text": "must-not-replace", "usage": {"input_tokens": 2}}, reply=True, received=True)
    saved = summary_dialogues.decode(pg.execute("SELECT checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s",
                                              (late_id,)).fetchone()["checkpoint_ciphertext"], s.settings)
    assert saved["usage"] == {"input_tokens": 5}
    assert {item["call_id"] for item in saved["receipts"]} == {
        old["provider_call"]["id"], newer["provider_call"]["id"]}
    assert saved["response_received"]["response"]["text"] == "new-phase"
    pg.execute("UPDATE summary_reply_tasks SET checkpoint_ciphertext='broken',lease_until=NOW()-INTERVAL '1 second' "
               "WHERE id=%s", (late_id,))
    assert replies.claim() is None
    assert pg.execute("SELECT status FROM summary_reply_tasks WHERE id=%s", (late_id,)).fetchone()["status"] == "failed"
    # Known malformed output is failed with its receipt, never an unknown automatic retry.
    def malformed(*_args, **_kwargs):
        calls.append("malformed")
        return {"text": "{", "response_id": "resp-malformed", "usage": {"input_tokens": 9}}
    monkeypatch.setattr(openai_client, "call_openai", malformed)
    invalid_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    replies.run(replies.claim(), s.settings)
    assert replies.claim() is None
    invalid = pg.execute("SELECT status,checkpoint_ciphertext FROM summary_reply_tasks WHERE id=%s",
                         (invalid_id,)).fetchone()
    assert invalid["status"] == "failed"
    assert summary_dialogues.decode(invalid["checkpoint_ciphertext"], s.settings)["usage"] == {"input_tokens": 9}
    # An uncertain provider timeout or expired paid lease must never silently dispatch twice.
    count = len(calls)
    def timeout(*_args, **_kwargs):
        calls.append("unknown-request")
        raise openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    monkeypatch.setattr(openai_client, "call_openai", timeout)
    unknown_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    replies.run(replies.claim(), s.settings)
    assert replies.claim() is None and len(calls) == count + 1
    assert replies.read(pg, s.ids["cm"], unknown_id, s.settings)["status"] == "failed"
    assert "请求结果未知" in replies.read(pg, s.ids["cm"], unknown_id, s.settings)["error"]
    interrupted_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    interrupted = replies.claim()
    pg.execute("UPDATE summary_reply_tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
               (interrupted_id,))
    assert replies.claim() is None
    replies.run(interrupted, s.settings)
    assert len(calls) == count + 1 and replies.read(pg, s.ids["cm"], interrupted_id, s.settings)["status"] == "failed"
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='chat'")
    with pytest.raises(ai.SummaryInputError):
        replies.read(pg, s.ids["cm"], job_id, s.settings, fresh=True)


def test_assistant_window_uses_cm_identity_separate_switch_and_frozen_cutoff(pg, seeded, monkeypatch):
    from app import summary_replies as replies
    s = seeded
    monkeypatch.setattr(replies, "connection", lambda: nullcontext(pg))
    queued = s.save()
    # Simulate a pre-upgrade scheduled run, including its original material fingerprint.
    pg.execute("UPDATE offhours_summary_runs SET event_rules_ciphertext=NULL,event_rules_digest=NULL WHERE id=%s",
               (queued["run_id"],))
    legacy_run = pg.execute("SELECT * FROM offhours_summary_runs WHERE id=%s", (queued["run_id"],)).fetchone()
    legacy_task = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='chat'",
                            (queued["run_id"],)).fetchone()
    legacy_material = ai._load_chat(pg, legacy_run, 'chat', legacy_task['cutoff'], settings=s.settings)
    pg.execute("UPDATE offhours_summary_tasks SET input_fingerprint=%s WHERE id=%s",
               (legacy_material["fingerprint"], legacy_task["id"]))
    s.save(False, key="")
    settings_before = pg.execute("SELECT * FROM offhours_summary_settings").fetchone()
    pinned_rules = assistant_rules.event_snapshot(pg, s.settings)
    summary_instructions = []
    original_model = openai_client.call_openai
    def summary_model(*args, **kwargs):
        summary_instructions.append(kwargs["instructions"])
        return original_model(*args, **kwargs)
    monkeypatch.setattr(openai_client, "call_openai", summary_model)
    save_feature_config(pg, api_key_cipher(s.settings.token_encryption_key), ai.ASSISTANT_FEATURE,
        provider="openai", project_id="assistant-test", model="assistant-model", enabled=True,
        api_key="assistant-synthetic-key", updated_by_authorization_id=s.ids["owner"])
    session_id, turn_id = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session_id, s.ids["cm"]))
    pg.execute("""INSERT INTO assistant_turns(id,session_id,client_request_id,status,input_ciphertext)
        VALUES(%s,%s,%s,'waiting','synthetic')""", (turn_id, session_id, uuid4()))
    turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (turn_id,)).fetchone()
    view = ai.ensure_assistant_run(pg, s.ids["cm"], turn, s.ids["cm"], s.start, s.end,
                                   [s.ids["person"]], s.settings)
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "queued" and view["responsible"]["id"] == str(s.ids["cm"])
    stored_run = pg.execute("SELECT * FROM offhours_summary_runs WHERE id=%s", (run_id,)).fetchone()
    assert summary_dialogues.decode(stored_run["event_rules_ciphertext"], s.settings) == pinned_rules
    assert stored_run["event_rules_digest"] == pinned_rules["digest"]
    assert ai.ensure_assistant_run(pg, s.ids["cm"], turn, s.ids["cm"], s.start, s.end,
                                   [s.ids["person"]], s.settings)["run"]["id"] == run_id
    s.add("after-assistant", who="cm", text="This arrived after the fixed request cutoff",
          at=s.end - timedelta(minutes=1))
    unfrozen = ai._load_chat(pg, stored_run, 'chat', s.end, settings=s.settings)
    assert any(message['message_id'] == 'after-assistant' and message['in_window']
               for message in unfrozen['pack']['messages'])
    assert ai.prepare_next_summary(s.settings)
    ai._stop_tasks(pg, "old function stopped")
    assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE run_id=%s AND is_current",
                      (run_id,)).fetchone()["status"] == "queued"
    job = ai.claim_next_summary(interactive=True)
    assert job["origin"] == "assistant" and job["cutoff"] == turn["created_at"]
    ai.run_claimed_summary(job, s.settings)
    document = pinned_rules["documents"]["event-priority"]
    assistant_rules.save_document(pg, s.settings, s.ids["owner"], "event-priority",
        document["title"], document["description"], "manual", "published", "owner",
        document["text"] + "\nnew-event-priority-marker", document["revision"])
    assert ai.ensure_assistant_run(pg, s.ids["cm"], turn, s.ids["cm"], s.start, s.end,
                                   [s.ids["person"]], s.settings)["run"]["id"] == run_id
    assert ai.prepare_next_summary(s.settings)
    ai.run_claimed_summary(ai.claim_next_summary(interactive=True), s.settings)
    read_chats = []
    original_load = ai._load_chat

    def load_once(*args, **kwargs):
        read_chats.append(args[2])
        return original_load(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ai, "_load_chat", load_once)
        result = ai.get_summary_view(pg, s.ids["cm"], run_id=run_id, settings=s.settings)
    assert read_chats == ["chat"]
    assert result["run"]["status"] == "completed" and len(result["events"]) == 1
    assert len(summary_instructions) == 1
    assert assistant_rules.event_instructions(pinned_rules, "extract") in summary_instructions[0]
    assert "new-event-priority-marker" not in json.dumps(result)
    assert "after-assistant" not in json.dumps(s.calls)
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == settings_before
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["owner"], run_id=run_id, settings=s.settings)
    event = result["events"][0]
    context = replies.read_event_context(pg, s.ids["cm"], event["id"], s.settings, mode="period")
    assert context["version"] == event["version"]
    assert all(m["message_id"] != "after-assistant" for m in context["messages"])
    ai._stop_tasks(pg, "old function stopped")
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=run_id, settings=s.settings)["events"]
    # Identical messages/cutoff under an edited manual must not reuse the old completed cache.
    pg.execute("UPDATE assistant_turns SET status='completed' WHERE id=%s", (turn_id,))
    next_turn = uuid4()
    pg.execute("""INSERT INTO assistant_turns(id,session_id,client_request_id,status,input_ciphertext,created_at)
        VALUES(%s,%s,%s,'waiting','synthetic',%s)""", (next_turn, session_id, uuid4(), turn["created_at"]))
    next_view = ai.ensure_assistant_run(pg, s.ids["cm"], next_turn, s.ids["cm"], s.start, s.end,
                                       [s.ids["person"]], s.settings)
    assert next_view["run"]["id"] != run_id
    assert ai.prepare_next_summary(s.settings)
    next_job = ai.claim_next_summary(interactive=True)
    assert str(next_job["run_id"]) == next_view["run"]["id"]
    assert next_job["input_fingerprint"] != job["input_fingerprint"]
    ai.run_claimed_summary(next_job, s.settings)
    finish_merges(pg, s)
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=next_view["run"]["id"],
                               settings=s.settings)["run"]["status"] == "completed"
    assert len(summary_instructions) == 2 and "new-event-priority-marker" in summary_instructions[-1]
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=run_id, settings=s.settings)["run"]["status"] == "completed"
    pg.execute("UPDATE assistant_turns SET status='completed' WHERE id=%s", (next_turn,))
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE assistant_rule_overrides SET content_ciphertext='broken' WHERE rule_key='event-priority'")
        damaged_turn = uuid4()
        pg.execute("""INSERT INTO assistant_turns(id,session_id,client_request_id,status,input_ciphertext,created_at)
            VALUES(%s,%s,%s,'waiting','synthetic',%s)""", (damaged_turn, session_id, uuid4(), turn["created_at"]))
        with pytest.raises(ValueError, match="invalid encrypted token"):
            ai.ensure_assistant_run(pg, s.ids["cm"], damaged_turn, s.ids["cm"], s.start, s.end,
                                    [s.ids["person"]], s.settings)
        assert ai.get_summary_view(pg, s.ids["cm"], run_id=run_id, settings=s.settings)["run"]["status"] == "completed"
    # Drafting from a scheduled report must also use the explicit assistant credential.
    s.save(key="")
    ai.stop_assistant_tasks(pg)
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    assert len(summary_instructions) == 3 and summary_instructions[-1].startswith(ai._INSTRUCTIONS + "\n")
    assert "new-event-priority-marker" not in summary_instructions[-1]
    legacy_event = ai.get_summary_view(pg, s.ids["cm"])["events"][0]
    reply_id = replies.enqueue(pg, s.ids["cm"], legacy_event["id"], s.settings,
                               feature=ai.ASSISTANT_FEATURE, instruction="Use English", language="en")
    s.save(False)
    observed = []
    def reply_model(cfg, key, pack, actor, **kwargs):
        observed.append((cfg["model"], key, pack))
        if "document_catalog" in pack:
            return {"text": json.dumps({"document_ids": []})}
        if "core_refs" in pack:
            return {"text": json.dumps({"processed_refs": pack["core_refs"],
                "relevant_refs": pack["core_refs"], "style_refs": []})}
        return {"text": json.dumps({"outcome": "ready", "language": "en", "draft": "I can check that.",
            "chinese_translation": "我可以核对。", "explanation": "资料明确。", "checks": [],
            "evidence_refs": [m["ref"] for m in pack["messages"]]})}
    monkeypatch.setattr(openai_client, "call_openai", reply_model)
    for _ in range(8):
        reply_job = replies.claim()
        if not reply_job:
            break
        replies.run(reply_job, s.settings)
    assert replies.read(pg, s.ids["cm"], reply_id, s.settings)["status"] == "succeeded"
    assert observed and all(model == "assistant-model" and key == "assistant-synthetic-key"
                            for model, key, _ in observed)
    assert observed[-1][2]["draft_request"] == {"instruction": "Use English", "language": "en"}

    # Assistant policy is stricter than the retained scheduled-report pilot/management policy.
    from app import assistant_tools
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (s.ids["owner"],))
    legacy_ref = f"{legacy_event['id']}:{legacy_event['version']}"
    assert replies.read_event_context(pg, s.ids["tester"], legacy_event["id"], s.settings)["messages"]
    with pytest.raises(PermissionError):
        assistant_tools.evidence(pg, s.ids["tester"], legacy_ref, s.settings)
    with pytest.raises(PermissionError):
        replies.enqueue(pg, s.ids["tester"], legacy_event["id"], s.settings, feature=ai.ASSISTANT_FEATURE)

    owner_reply = replies.enqueue(pg, s.ids["owner"], legacy_event["id"], s.settings,
                                 feature=ai.ASSISTANT_FEATURE, instruction="Owner test")
    for _ in range(8):
        reply_job = replies.claim()
        if not reply_job:
            break
        replies.run(reply_job, s.settings)
    assert replies.read(pg, s.ids["owner"], owner_reply, s.settings, fresh=True)["status"] == "succeeded"
    queued_reply = replies.enqueue(pg, s.ids["owner"], legacy_event["id"], s.settings,
                                  feature=ai.ASSISTANT_FEATURE, instruction="Queued Owner test")
    reply_job = replies.claim()
    assert str(reply_job["id"]) == queued_reply
    owner_session, owner_turn = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (owner_session, s.ids["owner"]))
    pg.execute("""INSERT INTO assistant_turns(id,session_id,client_request_id,status,input_ciphertext)
        VALUES(%s,%s,%s,'waiting','synthetic')""", (owner_turn, owner_session, uuid4()))
    owner_run = ai.ensure_assistant_run(pg, s.ids["owner"], owner_turn, s.ids["cm"], s.start, s.end,
                                        [s.ids["person"]], s.settings)["run"]["id"]
    assert ai.prepare_next_summary(s.settings)
    summary_job = ai.claim_next_summary(interactive=True)
    assert str(summary_job["run_id"]) == owner_run
    count = len(observed)
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],))
    replies.run(reply_job, s.settings)
    ai.run_claimed_summary(summary_job, s.settings)
    assert len(observed) == count
    assert pg.execute("SELECT status FROM summary_reply_tasks WHERE id=%s", (queued_reply,)).fetchone()[
        "status"] == "stale"
    with pytest.raises(PermissionError):
        replies.read(pg, s.ids["owner"], owner_reply, s.settings, fresh=True)
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["owner"], run_id=owner_run, settings=s.settings)
    with pytest.raises(PermissionError):
        ai.ensure_assistant_run(pg, s.ids["owner"], owner_turn, s.ids["cm"], s.start, s.end,
                                [s.ids["person"]], s.settings)
    assert ai._authorize(pg, s.ids["owner"], cm_id=s.ids["cm"])[0]["id"] == s.ids["cm"]
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (s.ids["owner"],))

    # A request already accepted by the provider cannot save or expose its late cross-CM result.
    late_reply = replies.enqueue(pg, s.ids["owner"], legacy_event["id"], s.settings,
                                feature=ai.ASSISTANT_FEATURE, instruction="Late Owner result")
    def downgraded_reply(*args, **kwargs):
        output = reply_model(*args, **kwargs)
        pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],))
        return output
    monkeypatch.setattr(openai_client, "call_openai", downgraded_reply)
    replies.run(replies.claim(), s.settings)
    late = pg.execute("SELECT status,result_ciphertext FROM summary_reply_tasks WHERE id=%s",
                      (late_reply,)).fetchone()
    assert late["status"] == "stale" and late["result_ciphertext"] is None
    with pytest.raises(PermissionError):
        replies.read(pg, s.ids["owner"], late_reply, s.settings)


def test_assistant_unknown_material_cannot_be_replayed_by_a_new_turn(pg, seeded, monkeypatch):
    s = seeded
    save_feature_config(pg, api_key_cipher(s.settings.token_encryption_key), ai.ASSISTANT_FEATURE,
        provider="openai", project_id="assistant-test", model="assistant-model", enabled=True,
        api_key="assistant-synthetic-key", updated_by_authorization_id=s.ids["owner"])
    def request():
        session_id, turn_id = uuid4(), uuid4()
        pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session_id, s.ids["cm"]))
        pg.execute("""INSERT INTO assistant_turns(id,session_id,client_request_id,status,input_ciphertext)
            VALUES(%s,%s,%s,'waiting','synthetic')""", (turn_id, session_id, uuid4()))
        return ai.ensure_assistant_run(pg, s.ids["cm"], turn_id, s.ids["cm"], s.start, s.end, None, s.settings)
    calls = []
    original_model = openai_client.call_openai
    def timeout(*_args, **_kwargs):
        calls.append("paid attempt")
        raise openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    monkeypatch.setattr(openai_client, "call_openai", timeout)
    first = request()
    assert ai.prepare_next_summary(s.settings)
    ai.run_claimed_summary(ai.claim_next_summary(interactive=True), s.settings)
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=first["run"]["id"],
                               settings=s.settings)["run"]["status"] == "unknown"
    document = assistant_rules.event_snapshot(pg, s.settings)["documents"]["event-extraction"]
    assistant_rules.save_document(pg, s.settings, s.ids["owner"], "event-extraction",
        document["title"], document["description"], "manual", "published", "owner",
        document["text"] + "\nnew-event-rules-marker", document["revision"])
    repeated = request()
    if repeated["run"]["id"] != first["run"]["id"]:
        ai.prepare_next_summary(s.settings)
    assert ai.claim_next_summary(interactive=True) is None and len(calls) == 1
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=repeated["run"]["id"],
                               settings=s.settings)["run"]["status"] == "unknown"
    # A real message change is new material, so the frozen request cannot hide it.
    s.add("new-material", text="Please check sample SKU-200", at=s.start + timedelta(hours=2))
    monkeypatch.setattr(openai_client, "call_openai", original_model)
    changed = request()
    assert changed["run"]["id"] != first["run"]["id"]
    assert ai.prepare_next_summary(s.settings)
    fresh = ai.claim_next_summary(interactive=True)
    assert fresh is not None
    ai.run_claimed_summary(fresh, s.settings)
    ai.prepare_next_summary(s.settings)
    ai.run_claimed_summary(ai.claim_next_summary(interactive=True), s.settings)
    assert ai.get_summary_view(pg, s.ids["cm"], run_id=changed["run"]["id"],
                               settings=s.settings)["run"]["status"] == "completed"


def test_message_material_cache_rechecks_access_and_recovers_one_paid_receipt(pg, seeded, monkeypatch):
    import io

    from PIL import Image

    from app import message_materials as materials
    from app import worker

    s = seeded
    s.settings.lark_sync_max_inflight = 1
    s.settings.lark_sync_requests_per_second = 1
    monkeypatch.setattr(materials, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(materials, "get_settings", lambda: s.settings)
    monkeypatch.setattr(worker, "_usable_access_token", lambda *_args: "synthetic-user-token")
    save_feature_config(pg, api_key_cipher(s.settings.token_encryption_key), ai.ASSISTANT_FEATURE,
        provider="openai", project_id="attachment-test", model="attachment-model", enabled=True,
        api_key="synthetic-attachment-key", updated_by_authorization_id=s.ids["owner"])
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    downloaded, called = [], []

    def download(_token, message_id, key, kind):
        downloaded.append(message_id)
        assert _token == "synthetic-user-token" and key.startswith("img_") and kind == "image"
        if len(downloaded) == 1:
            raise materials.LarkApiError(429, 99991400, "synthetic resource rate limit", 1)
        return {"content": image.getvalue(), "filename": "synthetic.png", "mime_type": "image/png"}

    monkeypatch.setattr(materials, "LarkClient", lambda *_args, **_kwargs:
                        SimpleNamespace(download_message_resource=download))

    def model(_cfg, _key, payload, cm_id, **options):
        called.append(payload)
        assert _key == "synthetic-attachment-key" and cm_id == s.ids["cm"]
        assert options["input_content"][0]["image_url"].startswith("data:image/png;base64,")
        if len(called) == 2:
            raise openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
        return {"text": json.dumps({"text": "截图显示样品已寄出", "complete": True, "notes": []}),
                "usage": {"input_tokens": 7, "output_tokens": 3}, "response_id": "synthetic-material-response"}

    monkeypatch.setattr(openai_client, "call_openai", model)

    def add_image(message_id, actor=None):
        s.add(message_id, kind="image")
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat' AND message_id=%s",
                   (s.cipher.encrypt(json.dumps({"image_key": "img_" + message_id})), message_id))
        row = materials._sources(pg, s.ids["cm"], [{"chat_id": "chat", "message_id": message_id}])[
            ("chat", message_id)]
        result = materials.attach(pg, actor or s.ids["cm"], s.ids["cm"], [row], s.settings, enqueue=True)
        assert len(result["pending_ids"]) == 1
        return row, UUID(result["pending_ids"][0])

    first_row, first_id = add_image("om_first")
    second_row, second_id = add_image("om_unknown")
    pg.execute("UPDATE message_material_jobs SET scheduled_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (first_id,))
    first = materials.claim()
    assert first["id"] == first_id and materials.claim() is None  # One global attachment lease.
    materials.run(first, s.settings)
    assert pg.execute(
        "SELECT status FROM message_material_jobs WHERE id=%s", (first_id,)
    ).fetchone()["status"] == "queued"
    assert called == []  # The first read may be rate limited before a checkpoint has been saved.
    pg.execute("UPDATE message_material_jobs SET scheduled_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (first_id,))
    materials.run(materials.claim(), s.settings)
    assert pg.execute(
        "SELECT status FROM message_material_jobs WHERE id=%s", (first_id,)
    ).fetchone()["status"] == "succeeded"
    cached = materials.attach(pg, s.ids["owner"], s.ids["cm"], [dict(first_row)], s.settings, enqueue=True)
    assert cached["pending_ids"] == []
    assert cached["rows"][0]["_material"]["text"] == "[图片解析，非发言人原话]\n截图显示样品已寄出"
    assert cached["rows"][0]["_material"]["complete"] and len(called) == 1
    with pytest.raises(PermissionError):
        materials.attach(pg, s.ids["tester"], s.ids["cm"], [dict(first_row)], s.settings)

    second = materials.claim()
    assert second["id"] == second_id
    materials.run(second, s.settings)
    saved = pg.execute("SELECT * FROM message_material_jobs WHERE id=%s", (second_id,)).fetchone()
    assert saved["status"] == "unknown" and len(called) == 2
    frozen = materials.attach(pg, s.ids["cm"], s.ids["cm"], [dict(second_row)], s.settings, enqueue=True)
    assert frozen["pending_ids"] == [] and not frozen["rows"][0]["_material"]["complete"]
    assert materials.claim() is None  # A new query must not repeat an unknown paid request.
    checkpoint = materials._decode(saved["checkpoint_ciphertext"], s.settings)
    assert not {"inputs", "content", "image_url", "file_data"} & checkpoint.keys()
    response = {"text": json.dumps({"text": "截图显示收款成功", "complete": True, "notes": []}),
                "usage": {"input_tokens": 11, "output_tokens": 4}, "response_id": "synthetic-late-response"}
    materials._receipt(second, s.settings, checkpoint, response)
    materials._receipt(second, s.settings, checkpoint, response)
    recovered = materials.claim()
    assert recovered["id"] == second_id
    materials.run(recovered, s.settings)
    current = pg.execute("SELECT * FROM message_material_jobs WHERE id=%s", (second_id,)).fetchone()
    assert current["status"] == "succeeded" and current["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert "截图显示收款成功" in materials._decode(current["result_ciphertext"], s.settings)["text"]
    assert len(called) == 2 and downloaded == ["om_first", "om_first", "om_unknown"]
    # A duplicate late receipt remains idempotent even after its result was finalized.
    materials._receipt(second, s.settings, checkpoint, response)
    assert pg.execute(
        "SELECT usage FROM message_material_jobs WHERE id=%s", (second_id,)
    ).fetchone()["usage"] == current["usage"]

    _, owner_job = add_image("om_owner_revoked", s.ids["owner"])
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],))
    with pytest.raises(PermissionError):
        materials.attach(pg, s.ids["owner"], s.ids["cm"], [dict(first_row)], s.settings)
    assert materials.claim() is None
    assert pg.execute(
        "SELECT status FROM message_material_jobs WHERE id=%s", (owner_job,)
    ).fetchone()["status"] == "cancelled"
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (s.ids["owner"],))
    _, consent_job = add_image("om_consent_revoked")
    pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (s.ids["cm"],))
    with pytest.raises((PermissionError, ai.SummaryInputError)):
        materials.attach(pg, s.ids["cm"], s.ids["cm"], [dict(first_row)], s.settings)
    assert materials.claim() is None
    assert pg.execute(
        "SELECT status FROM message_material_jobs WHERE id=%s", (consent_job,)
    ).fetchone()["status"] == "cancelled"
    assert len(called) == 2 and len(downloaded) == 3


def test_pending_material_refresh_targets_one_chat_and_keeps_original_cutoff(pg, seeded, monkeypatch):
    from app import message_materials, summary_replies

    s = seeded
    pg.execute("DELETE FROM authorization_message_observations WHERE message_id='outside'")
    pg.execute("DELETE FROM chat_messages WHERE message_id='outside'")
    pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
               "VALUES(%s,%s,%s,'manual')", (uuid4(), s.ids['other'], s.ids['cm']))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('peer','Other creator','p2p')")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
        p2p_counterpart_type,backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
        VALUES(%s,'peer','ou_other','user',TRUE,%s,%s,'succeeded')""",
               (s.ids['cm'], s.start - timedelta(days=9), s.end))
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,
        body_ciphertext,message_create_time) VALUES('peer','peer_request','ou_other','text',%s,%s)""",
               (s.cipher.encrypt(json.dumps({'text': 'My sample request'})), s.start + timedelta(hours=1)))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'peer','peer_request')", (s.ids['cm'],))
    saved = s.save()
    assert saved['queued'] == 2
    for _ in range(2):
        ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    run_id = UUID(saved['run_id'])
    original = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s ORDER BY id",
                          (run_id,)).fetchall()
    target = next(t for t in original if t['chat_id'] == 'chat')
    peer = next(t for t in original if t['chat_id'] == 'peer')
    assert target['status'] == peer['status'] == 'succeeded'
    before_calls = len(s.calls)
    pg.execute("""UPDATE offhours_summary_runs SET period='work',report_date=%s,
        refresh_until=%s,next_check_at=NULL WHERE id=%s""",
               (datetime.now(ai.LONDON).date(), target['cutoff'], run_id))
    pg.execute("UPDATE chat_messages SET message_type='image',body_ciphertext=%s WHERE message_id='m1'",
               (s.cipher.encrypt(json.dumps({'image_key': 'synthetic_image'})),))
    s.add('after_original_cutoff', text='Do not include this newer request',
          at=target['cutoff'] + timedelta(minutes=1))
    ready, attaches = False, []

    def attach(_conn, actor, cm, rows, _settings, *, feature='operations_assistant', enqueue=False):
        assert cm == s.ids['cm'] and actor in (s.ids['cm'], s.ids['owner'])
        attaches.append((enqueue, {r['chat_id'] for r in rows}))
        pending = []
        for row in rows:
            if row['chat_id'] == 'chat' and row['message_id'] == 'm1':
                row['_material'] = {'text': '[图片解析，非发言人原话]\n样品已寄出' if ready else '',
                                    'complete': ready, 'notes': [] if ready else ['图片正在解析']}
                if not ready:
                    pending.append('synthetic-material-job')
        return {'rows': rows, 'pending_ids': pending}

    monkeypatch.setattr(message_materials, 'attach', attach)

    def state():
        row = pg.execute('SELECT *,id AS run_id FROM offhours_summary_runs WHERE id=%s', (run_id,)).fetchone()
        return dict(row)

    later = target['cutoff'] + timedelta(minutes=10)
    assert ai._schedule_run(pg, state(), later, chat_ids={'chat'}) == 0
    assert pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s ORDER BY id',
                      (run_id,)).fetchall() == original
    assert state()['coverage']['material_chats'] == ['chat']
    assert summary_dialogues._blocked_people(
        [{'chat_id': 'new_material', 'contact_ids': {s.ids['person']}},
         {'chat_id': 'peer', 'contact_ids': {s.ids['other']}}], [], material_chats=['new_material']
    ) == {str(s.ids['person'])}  # A pending new chat has no source task yet, but must block its creator's merge.
    assert ai.get_summary_view(pg, s.ids['owner'], run_id=run_id, settings=s.settings)['run']['status'] == 'running'
    assert len(s.calls) == before_calls
    assert all(chats <= {'chat'} for enqueue, chats in attaches if enqueue)

    ready = True
    ai.request_material_refresh(pg, {'cm_id': s.ids['cm'], 'chat_id': 'chat'},
                                {'message_create_time': s.start + timedelta(hours=1)})
    assert state()['next_check_at'] is not None
    assert pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s ORDER BY id',
                      (run_id,)).fetchall() == original
    assert ai._schedule_run(pg, state(), later) == 1
    new = pg.execute("SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND chat_id='chat' AND is_current",
                     (run_id,)).fetchone()
    assert new['id'] != target['id'] and new['cutoff'] == target['cutoff'] and new['status'] == 'queued'
    assert state()['coverage']['material_chats'] == []
    assert pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s', (target['id'],)).fetchone() == {
        **target, 'is_current': False}
    assert pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s', (peer['id'],)).fetchone() == peer
    assert len(s.calls) == before_calls
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    assert '样品已寄出' in json.dumps(s.calls[-1], ensure_ascii=False)
    assert 'Do not include this newer request' not in json.dumps(s.calls[-1])
    finish_merges(pg, s)
    view = ai.get_summary_view(pg, s.ids['owner'], run_id=run_id, settings=s.settings)
    event = next(e for e in view['events'] if e['contact_id'] == str(s.ids['person']))
    attaches.clear()
    summary_replies.read_event_context(pg, s.ids['owner'], event['id'], s.settings)
    assert attaches and all(not enqueue for enqueue, _chats in attaches)
