from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import (
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


@pytest.mark.parametrize(
    "changes",
    [
        {"user": "outsider"},
        {"handling_status": "invented"},
        {"handling_evidence_refs": ["unknown"]},
        {"reply_evidence_refs": ["M1"]},
        {"handling_reason": ""},
        {"handle": "@fabricated"},
        {"evidence_refs": ["unknown"]},
        {
            "priority_basis": "three_workdays",
            "priority": "P1",
            "aging_request_ref": "M1",
            "no_effective_response": True,
        },
        {"priority": "P9"},
        {"summary": ""},
        {"aging_request_ref": "invented"},
    ],
)
def test_invalid_or_unsupported_model_claims_rejected(changes):
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"events": [sample_event(**changes)]}), pack())


def test_missing_media_and_multi_handle_do_not_invent_risk_or_duplicate_account():
    data = pack()
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


def test_reply_and_resolution_remain_independent_through_context_and_merge():
    data = pack()
    data["pack"].update(is_private=False, unique_group_user="U1",
                        staff={"CM": {"name": "Remi"}, "CM2": {"name": "Shang"}})
    data["pack"]["messages"][0].update(message_id="request", text="Sample order ABC-123 please",
        mentions=[{"target": "unknown", "ambiguous": True}])
    for ref, speaker, text in (
        ("M2", "CM", "I'll look into sample order ABC-123 tomorrow"),
        ("M3", "CM2", "I can help check sample order ABC-123"),
        ("M4", "U1", "[附件或正文不可读取]"),
    ):
        data["pack"]["messages"].append({"ref": ref, "message_id": ref, "speaker": speaker,
            "text": text, "in_window": True, "unreadable": ref == "M4",
            "unavailable_or_truncated": ref == "M4"})
    data["refs"] = {m["ref"]: {**m, "sender_name": {"CM": "Remi", "CM2": "Shang"}.get(m["speaker"], "Alice"),
        "is_staff": m["speaker"] in {"CM", "CM2"}, "chat_id": "group", "created_at": m["ref"]}
        for m in data["pack"]["messages"]}
    pending = ai._validate(json.dumps({"events": [sample_event(
        handling_evidence_refs=["M1", "M4"], reply_evidence_refs=["M2", "M3"])]}), data)[0]
    assert pending["handling_status"] == "unresolved" and pending["replied_by"] == ["Remi", "Shang"]
    assert not pending["needs_review"]  # An unknown @ or optional attachment does not erase a readable request.
    pending["evidence"] = [data["refs"][r] for r in pending["evidence_refs"]]
    done_message = {"text": "Sample order ABC-123 delivered today", "speaker": "CM2", "is_staff": True,
                    "sender_name": "Shang", "chat_id": "group", "message_id": "done", "created_at": "M5"}
    done = {**pending, "evidence": [done_message]}
    refs = summary_dialogues.merged_evidence(pending, done)
    request_ref = next(r for r, m in refs.items() if m["message_id"] == "request")
    done_ref = next(r for r, m in refs.items() if m["message_id"] == "done")
    value = {"same": True, "basis_kind": "identifier", "basis_value": "ABC-123", "quote_a_ref": request_ref,
        "quote_b_ref": done_ref, "title": "样品订单", "summary": "样品已送达", "latest_progress": "同事已确认送达",
        "handling_status": "resolved", "handling_reason": "已确认对应样品送达", "handling_evidence_refs": [done_ref],
        "reply_evidence_refs": [r for r, m in refs.items() if m.get("is_staff")]}
    merged = summary_dialogues.combine(json.dumps(value), pending, done)
    assert merged["handling_status"] == "resolved" and merged["replied_by"] == ["Remi", "Shang"]
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
    data["refs"].update(M5={}, M6={})
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
def pg(monkeypatch):
    dsn = os.environ.get("OFFHOURS_SUMMARY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set OFFHOURS_SUMMARY_TEST_DATABASE_URL for isolated PostgreSQL checks")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        schema = "test_offhours_" + uuid4().hex
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            if int(path.name[:3]) <= 51 or int(path.name[:3]) >= 54:
                conn.execute(path.read_text())
        conn.execute("ALTER TABLE authorizations ALTER scopes SET DEFAULT "
                     "ARRAY['im:chat:read','im:message:readonly']::text[]")
        monkeypatch.setattr(ai, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(ai.ai_dispatch, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(ai, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"x" * 32))

        def fail_on_error(*_a, **_k):
            raise sys.exception()

        monkeypatch.setattr(ai.LOGGER, "warning", fail_on_error)
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def seeded(pg, monkeypatch):
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


@pytest.mark.parametrize("roster_kind", ["current", "missing_handle", "multi_handle", "legacy", "both"])
def test_save_enabled_immediately_queues_claims_and_stores_encrypted_summary(pg, seeded, roster_kind, monkeypatch):
    s = seeded
    if roster_kind == "missing_handle":
        pg.execute("DELETE FROM contact_social_accounts")
    if roster_kind in {"legacy", "both"}:
        pg.execute(
            """INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,
                   contact_id,relationship,source,created_by_authorization_id,candidate_id)
                   VALUES(%s,%s,%s,%s,'owner','cm_confirmation',%s,%s)""",
            (uuid4(), s.account, s.ids["cm"], s.ids["person"], s.ids["cm"], s.candidate_id),
        )
        if roster_kind == "legacy":
            pg.execute("DELETE FROM contact_assignments")
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


@pytest.mark.parametrize("change", ["hide", "delete", "reassign", "rejected_identity", "consent"])
def test_revocation_or_edit_before_provider_exports_nothing(pg, seeded, change):
    s = seeded
    s.save()
    job = ai.claim_next_summary()
    if change == "hide":
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE")
    elif change == "delete":
        pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'")
    elif change == "reassign":
        pg.execute("UPDATE contact_assignments SET ended_at=NOW()")
    elif change == "rejected_identity":
        pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (s.ids["person"],))
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


def test_group_reply_to_older_creator_is_context_but_cannot_prove_silence(pg, seeded):
    s = seeded
    pg.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=NULL")
    pg.execute("UPDATE chats SET chat_mode='group'")
    pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE message_id='m1'", (s.start - timedelta(hours=1),))
    s.add("reply", who="cm", text="I have received your request", parent_id="m1")
    s.save()
    job = ai.claim_next_summary()
    evidence = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    assert evidence and evidence["history_complete"] is False
    refs = [m["ref"] for m in evidence["pack"]["messages"]]
    result = ai._validate(json.dumps({"events": [sample_event(evidence_refs=refs)]}), evidence)
    assert result[0]["priority"] == "P2"


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
        if 'a' in input_pack:
            return {'text': json.dumps({'same': False, 'basis_kind': 'none', 'handling_status': 'uncertain',
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
                job.update(pg.execute("SELECT * FROM offhours_summary_tasks WHERE id=%s", (job["id"],)).fetchone())
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




def test_shared_admission_limits_summary_claims_to_five(pg, seeded):
    s = seeded
    s.save()
    run = pg.execute("SELECT run_id FROM offhours_summary_settings").fetchone()["run_id"]
    for i in range(5):
        chat = f"parallel-{i}"
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute(
            """INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff)
                   VALUES(%s,%s,%s,%s,NOW())""",
            (uuid4(), run, chat, str(i)),
        )
    assert all(ai.claim_next_summary() is not None for _ in range(4))
    assert ai.claim_next_summary() is not None  # Four running leases permit the fifth, not a sixth.
    assert ai.claim_next_summary() is None


def test_disabled_save_keeps_window_without_enqueuing(pg, seeded):
    s = seeded
    s.fields["window_start"] = (s.start - timedelta(hours=1)).isoformat()
    assert not s.save(False)["enabled"]
    expected = (s.start - timedelta(hours=1)).astimezone(ai.LONDON).strftime("%Y-%m-%dT%H:%M")
    assert ai.get_settings_view(pg)["window_start_input"] == expected
    assert not pg.execute("SELECT 1 FROM offhours_summary_tasks").fetchone()


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


def test_end_boundary_is_exclusive_even_when_message_was_synced_early(pg, seeded):
    s = seeded
    s.save()
    job = ai.claim_next_summary()
    snapshot = ai._load_chat(pg, job, "chat", s.end, settings=s.settings)
    assert "future CM reply" not in json.dumps(snapshot)




@pytest.mark.parametrize("http_status,stop_during_request", [(None, False), (None, True), (503, False)])
def test_network_failure_retries_are_bounded_not_cancelled_refresh_loop(
    pg, seeded, monkeypatch, http_status, stop_during_request,
):
    s = seeded
    s.save()

    calls = []
    def fail(*_a, **_k):
        calls.append(True)
        if stop_during_request:
            assert not s.save(False)["enabled"]
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
    assert before["fingerprint"] == ai._hash([before["pack"], before["users"]])
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
    legacy_signal = pg.execute('SELECT * FROM offhours_summary_backfills').fetchone()
    assert legacy_signal['contact_id'] == s.ids['person']
    assert legacy_signal['responsible_authorization_id'] == s.ids['reviewer']
    pg.execute("UPDATE creator_account_assignments SET source='excel_verified' WHERE id=%s", (legacy,))
    assert pg.execute('SELECT version FROM offhours_summary_backfills').fetchone()['version'] == \
        legacy_signal['version']
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
    durable = pg.execute('SELECT result_ciphertext,checkpoint_ciphertext,usage,attempts FROM offhours_summary_tasks '
                         'WHERE id=%s', (ancient_merge,)).fetchone()
    pg.execute((Path(__file__).parents[1] / 'migrations' / '082_bound_summary_period_queue.sql').read_text())
    assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',
                      (ancient_merge,)).fetchone()['status'] == 'cancelled'
    assert pg.execute('SELECT result_ciphertext,checkpoint_ciphertext,usage,attempts FROM offhours_summary_tasks '
                      'WHERE id=%s', (ancient_merge,)).fetchone() == durable
    assert pg.execute('SELECT status,result_ciphertext FROM offhours_summary_tasks WHERE id=%s',
                      (ancient_source,)).fetchone() == {
        'status': 'succeeded', 'result_ciphertext': source['result_ciphertext']}
    assert len(s.calls) == 3 and old_tasks() == original
    pg.execute('ROLLBACK TO SAVEPOINT backfill_ancient_execution')




def finish_merges(pg, s):
    summary_dialogues.schedule_merges(pg, s.settings)
    for _ in range(50):
        job = ai.claim_next_summary()
        if job is None:
            return
        ai.run_claimed_summary(job, s.settings)
    raise AssertionError('queue did not finish')


def test_full_shift_chunks_resume_filter_speakers_merge_and_recheck_access(pg, seeded, monkeypatch):
    s = seeded
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

    def model(_cfg, _key, pack, _cm, **kwargs):
        nonlocal failed
        calls.append(pack)
        if 'a' in pack:
            a, b = pack['a'], pack['b']
            return {'text': json.dumps({'same': True, 'basis_kind': 'identifier', 'basis_value': 'ABC-123',
                'quote_a_ref': next(r for r,e in pack['evidence_messages'].items() if e in a['evidence']),
                'quote_b_ref': next(r for r,e in pack['evidence_messages'].items() if e in b['evidence']),
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
            raise openai_client.AIInputError('synthetic transient failure')
        targets = [m for m in pack['messages'] if m['ref'] in pack['core_refs'] and m['speaker'] == user
                   and m['message_id'] in ('m1', 'person')]
        return {'text': json.dumps({'processed_refs': pack['core_refs'], 'events': [sample_event(
            user=user, evidence_refs=[m['ref']], handle=None,
            reply_evidence_refs=[r['ref'] for r in pack['messages'] if r['message_id'] == 'colleague'])
            for m in targets]})}

    monkeypatch.setattr(openai_client, 'call_openai', model)
    s.save()
    for _ in range(100):
        job = ai.claim_next_summary()
        if not job:
            break
        ai.run_claimed_summary(job, s.settings)
        pg.execute("UPDATE offhours_summary_tasks SET scheduled_at=NOW() WHERE status='queued'")
    else:
        pytest.fail('conversation queue did not finish')
    rows = pg.execute("SELECT * FROM offhours_summary_tasks WHERE is_current").fetchall()
    assert all(t['status'] == 'succeeded' for t in rows)
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
    assert view['events'][0]['latest_progress'] == '达人确认收到。'
    assert view['events'][0]['handling_status'] == 'resolved'
    assert view['events'][0]['handling_evidence'][0]['chat_id'] == 'second'
    assert view['events'][0]['replied_by'] == ['reviewer']
    assert view['events'][0]['reply_evidence'][0]['message_id'] == 'colleague'
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
        last_incremental_at,sync_status) VALUES(%s,'reply-group',TRUE,NOW(),'succeeded')""", (s.ids["cm"],))
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('reply-group','ou_person')")
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,
        message_create_time) VALUES('reply-group','broadcast','ou_cm','text',%s,NOW())""",
        (s.cipher.encrypt(json.dumps({"text": "OUTSIDER_GROUP_SECRET"})),))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'reply-group','broadcast')""", (s.ids["cm"],))
    with pytest.raises(PermissionError):
        replies.enqueue(pg, s.ids["reviewer"], event_id, s.settings)
    calls = []

    def model(cfg, key, pack, actor, **kwargs):
        calls.append(pack)
        assert "unrelated outsider secret" not in json.dumps(pack)
        assert "OUTSIDER_GROUP_SECRET" not in json.dumps(pack)
        assert "future CM reply" not in json.dumps(pack)
        if "edited_draft" in pack:
            return {"text": json.dumps({"chinese_translation": "我可以帮你核对这笔 £200 的付款。"})}
        if "core_refs" in pack:
            return {"text": json.dumps({"processed_refs": pack["core_refs"],
                "relevant_refs": pack["core_refs"],
                "style_refs": [m["ref"] for m in pack["messages"] if m["speaker"] == "CM"]})}
        resolved = any("now resolved" in m["text"] for m in pack["messages"])
        return {"text": json.dumps({"outcome": "resolved" if resolved else "ready", "language": "en",
            "draft": "" if resolved else "Happy to check that £200 payment with you.",
            "chinese_translation": "" if resolved else "我可以帮你核对这笔 £200 的付款。",
            "explanation": "后续已确认解决。" if resolved else "尚未看到处理完成结果。",
            "checks": [] if resolved else ["确认能够进行核对。"],
            "evidence_refs": [m["ref"] for m in pack["messages"]]})}

    monkeypatch.setattr(openai_client, "call_openai", model)
    job_id = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    assert replies.enqueue(pg, s.ids["cm"], event_id, s.settings) == job_id

    def finish():
        for _ in range(12):
            job = replies.claim()
            if not job:
                break
            replies.run(job, s.settings)
        else:
            pytest.fail("reply queue did not finish")
        assert not pg.execute("SELECT 1 FROM summary_reply_tasks WHERE status<>'succeeded'").fetchone()

    finish()
    result = replies.read(pg, s.ids["cm"], job_id, s.settings, fresh=True)
    assert result["result"]["outcome"] == "ready" and result["result"]["chinese_translation"]
    processed = {m["ref"]: m for pack in calls if "core_refs" in pack for m in pack["messages"]
                 if m["ref"] in pack["core_refs"] and m["message_id"] == "long-current"}
    assert "".join(m["text"] for m in sorted(processed.values(), key=lambda m: m["part"])) == long_text
    colleagues = [m for pack in calls for m in pack.get("messages", []) if m.get("message_id") == "colleague-current"]
    assert colleagues and all(m["is_staff"] and m["speaker"] != "CM" for m in colleagues)
    stored = pg.execute("SELECT result_ciphertext FROM summary_reply_tasks WHERE id=%s", (job_id,)).fetchone()
    assert "Happy" not in stored["result_ciphertext"]
    with pytest.raises(PermissionError):
        replies.read(pg, s.ids["owner"], job_id, s.settings)
    translated = replies.enqueue(pg, s.ids["cm"], event_id, s.settings, parent_id=job_id,
                                 text="Happy to check the £200 payment with you.")
    finish()
    assert replies.read(pg, s.ids["cm"], translated, s.settings, fresh=True)["result"]["draft"].startswith("Happy")
    s.add("resolved-later", text="This specific issue is now resolved, thank you.", at=datetime.now(UTC))
    assert replies.read(pg, s.ids["cm"], job_id, s.settings, fresh=True)["status"] == "stale"
    assert replies.read(pg, s.ids["cm"], translated, s.settings, fresh=True)["status"] == "stale"
    latest = replies.enqueue(pg, s.ids["cm"], event_id, s.settings)
    finish()
    latest_result = replies.read(pg, s.ids["cm"], latest, s.settings, fresh=True)["result"]
    assert latest_result["outcome"] == "resolved" and latest_result["draft"] == ""
    assert ai.get_summary_view(pg, s.ids["cm"])["events"][0]["handling_status"] == "unresolved"
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
    s.save()
    s.save(False)
    settings_before = pg.execute("SELECT * FROM offhours_summary_settings").fetchone()
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
    assert ai.ensure_assistant_run(pg, s.ids["cm"], turn, s.ids["cm"], s.start, s.end,
                                   [s.ids["person"]], s.settings)["run"]["id"] == run_id
    s.add("after-assistant", who="cm", text="This arrived after the fixed request cutoff",
          at=turn["created_at"] + timedelta(seconds=1))
    assert ai.prepare_next_summary(s.settings)
    ai._stop_tasks(pg, "old function stopped")
    assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE run_id=%s AND is_current",
                      (run_id,)).fetchone()["status"] == "queued"
    job = ai.claim_next_summary()
    assert job["origin"] == "assistant" and job["cutoff"] == turn["created_at"]
    ai.run_claimed_summary(job, s.settings)
    assert ai.prepare_next_summary(s.settings)
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    result = ai.read_assistant_run(pg, s.ids["cm"], run_id, s.settings)
    assert result["run"]["status"] == "completed" and len(result["events"]) == 1
    assert "after-assistant" not in json.dumps(s.calls)
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == settings_before
    with pytest.raises(PermissionError):
        ai.read_assistant_run(pg, s.ids["owner"], run_id, s.settings)
    event = result["events"][0]
    context = replies.read_event_context(pg, s.ids["cm"], event["id"], s.settings, mode="period")
    assert context["version"] == event["version"]
    assert all(m["message_id"] != "after-assistant" for m in context["messages"])
    ai._stop_tasks(pg, "old function stopped")
    assert ai.read_assistant_run(pg, s.ids["cm"], run_id, s.settings)["events"]
    # Drafting from a scheduled report must also use the explicit assistant credential.
    s.save()
    ai.stop_assistant_tasks(pg)
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    finish_merges(pg, s)
    legacy_event = ai.get_summary_view(pg, s.ids["cm"])["events"][0]
    reply_id = replies.enqueue(pg, s.ids["cm"], legacy_event["id"], s.settings,
                               feature=ai.ASSISTANT_FEATURE, instruction="Use English", language="en")
    s.save(False)
    observed = []
    def reply_model(cfg, key, pack, actor, **kwargs):
        observed.append((cfg["model"], key, pack))
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
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    assert ai.read_assistant_run(pg, s.ids["cm"], first["run"]["id"], s.settings)["run"]["status"] == "unknown"
    repeated = request()
    if repeated["run"]["id"] != first["run"]["id"]:
        ai.prepare_next_summary(s.settings)
    assert ai.claim_next_summary() is None and len(calls) == 1
    assert ai.read_assistant_run(pg, s.ids["cm"], repeated["run"]["id"], s.settings)["run"]["status"] == "unknown"
    # A real message change is new material, so the frozen request cannot hide it.
    s.add("new-material", text="Please check sample SKU-200", at=s.start + timedelta(hours=2))
    monkeypatch.setattr(openai_client, "call_openai", original_model)
    changed = request()
    assert changed["run"]["id"] != first["run"]["id"]
    assert ai.prepare_next_summary(s.settings)
    fresh = ai.claim_next_summary()
    assert fresh is not None
    ai.run_claimed_summary(fresh, s.settings)
    ai.prepare_next_summary(s.settings)
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    assert ai.read_assistant_run(pg, s.ids["cm"], changed["run"]["id"], s.settings)["run"]["status"] == "completed"
