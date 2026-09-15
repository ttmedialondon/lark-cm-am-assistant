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

from app import creator_import_evidence, creator_imports, creator_ownership, summary_periods
from app import offhours_summary as ai
from app.ai_feature_config import save_feature_config
from app.crypto import message_body_cipher


def test_window_and_working_days_skip_weekend_at_same_london_time():
    start, end = ai._parse_window(ai.DEFAULT_START, ai.DEFAULT_END)
    assert start == datetime(2026, 9, 9, 17, tzinfo=UTC)
    assert end == datetime(2026, 9, 10, 11, tzinfo=UTC)
    friday = datetime(2026, 9, 4, 16, tzinfo=UTC)
    assert ai.three_workday_deadline(friday) == datetime(2026, 9, 9, 16, tzinfo=UTC)
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
        "reason": "正常样品申请。",
        "needs_review": False,
        "evidence_refs": ["M1"],
        "aging_request_ref": None,
        "no_effective_response": False,
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
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"events": [sample_event(), sample_event()]}), data)


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
        pg.execute("INSERT INTO contacts(id,display_name,creator_type) VALUES(%s,%s,'cap')", (ids[name], name))
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
            ai.api_key_cipher(settings.token_encryption_key),
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
        assert kwargs["output_format"]["strict"]
        if kwargs.get('conversation'):
            input_pack = json.loads(kwargs['conversation'][0]['content'])
            calls.append(input_pack)
            messages = input_pack['context']
            old = input_pack['ledger'][0] if input_pack['ledger'] else None
            refs = [m['ref'] for m in messages if m['speaker'] == 'creator']
            update = timeline_event(refs, existing_id=old['id'] if old else None)
            return {'text': json.dumps({'processed_refs': input_pack['core_refs'], 'updates': [update] if refs else []})}
        calls.append(input_pack)
        message = next(m for m in input_pack["messages"] if m["in_window"] and m["speaker"] == "U1")
        return {
            "text": json.dumps({"events": [sample_event(
                evidence_refs=[message["ref"]], handle=next(iter(input_pack["users"]["U1"]["handles"]), None)
            )]}),
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
    return SimpleNamespace(
        ids=ids, settings=settings, save=save, calls=calls, add=add, start=start, end=end, fields=fields, cipher=cipher,
        account=account, candidate_id=candidate_id,
    )


@pytest.mark.parametrize("roster_kind", ["current", "missing_handle", "multi_handle", "legacy", "both"])
def test_save_enabled_immediately_queues_claims_and_stores_encrypted_summary(pg, seeded, roster_kind):
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
    assert not pg.execute("SELECT 1 FROM contact_roles").fetchone()
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_candidates").fetchone()["n"] == 1
    s.save(key="")
    ai.schedule_if_due(pg, force=True)
    assert ai.claim_next_summary() is None and len(s.calls) == 1


def test_acl_owner_remi_and_explicit_test_grant_not_arbitrary_roster_reviewer(pg, seeded):
    s = seeded
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    for name in ("owner", "cm", "tester"):
        assert len(ai.get_summary_view(pg, s.ids[name])["events"]) == 1
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["reviewer"])
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (s.ids["tester"],))
    assert len(ai.get_summary_view(pg, s.ids["tester"])["events"]) == 1
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (s.ids["tester"],))
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["tester"])


@pytest.mark.parametrize("change", ["hide", "delete", "reassign", "non_cap", "consent"])
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
    elif change == "non_cap":
        pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (s.ids["person"],))
    else:
        pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (s.ids["cm"],))
    ai.run_claimed_summary(job, s.settings)
    assert not s.calls
    assert (
        pg.execute("SELECT status FROM offhours_summary_tasks WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "cancelled"
    )


def test_routine_reencrypt_does_not_rescan_but_late_message_does(pg, seeded):
    s = seeded
    s.save()
    first = ai.claim_next_summary()
    ai.run_claimed_summary(first, s.settings)
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
        (s.cipher.encrypt(json.dumps({"text": "Sample please"})),),
    )
    pg.execute("UPDATE authorization_message_observations SET observed_at=NOW()")
    ai.schedule_if_due(pg, force=True)
    assert ai.claim_next_summary() is None
    s.add("late", text="Now I need a voucher", at=s.start + timedelta(hours=2))
    ai.schedule_if_due(pg, force=True)
    second = ai.claim_next_summary()
    assert second and second["input_fingerprint"] != first["input_fingerprint"]
    assert pg.execute("SELECT COUNT(*) AS n FROM offhours_summary_tasks WHERE is_current").fetchone()["n"] == 1


def test_disable_reenable_keeps_fencing_monotonic_and_old_call_cannot_commit(pg, seeded):
    s = seeded
    s.save()
    stale = ai.claim_next_summary()
    s.save(False, key="")
    s.save(True, key="")
    current = ai.claim_next_summary()
    assert current["id"] == stale["id"] and current["attempts"] > stale["attempts"]
    ai.run_claimed_summary(stale, s.settings)
    assert not s.calls
    ai.run_claimed_summary(current, s.settings)
    assert len(s.calls) == 1


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
    cipher = creator_ownership._cipher(s.settings)
    pg.execute(
        """INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,row_number,original_handle,
               handle,contact_id,status,review_status,outcome,pack_ciphertext,result_ciphertext)
               VALUES(%s,%s,%s,1,'alice','alice',%s,'succeeded','confirmed','matched',%s,%s)""",
        (row_id, batch_id, s.ids["cm"], s.ids["person"],
         cipher.encrypt(json.dumps(chain)), cipher.encrypt(json.dumps(decisions))),
    )
    pg.execute("UPDATE contact_social_accounts SET import_row_id=%s WHERE social_account_id=%s", (row_id, s.account))

    def model(_cfg, _key, input_pack, _cm, **_kwargs):
        s.calls.append(input_pack)
        assert len(input_pack["users"]) == 1
        assert input_pack["users"]["U1"]["handles"] == ["@alice"]
        if input_pack["chat_name"] == groups["group_ai"][0]:
            assert input_pack["unique_group_user"] is None
            return {"text": json.dumps({"events": []}), "usage": {"input_tokens": 1}}
        if not input_pack["is_private"]:
            assert input_pack["unique_group_user"] == "U1"
        message = next(m for m in input_pack["messages"] if m["in_window"])
        return {"text": json.dumps({"events": [sample_event(evidence_refs=[message["ref"]])]}),
                "usage": {"input_tokens": 1}}

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
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
            ai.run_claimed_summary(job, s.settings)
    assert len(s.calls) == 3
    assert any(p["is_private"] for p in s.calls)
    assert {p["chat_name"] for p in s.calls if not p["is_private"]} == {
        groups["group_named"][0], groups["group_ai"][0],
    }
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert len(view["events"]) == 2 and view["run"]["counts"]["completed"] == 3
    assert not any("group_hidden" in json.dumps(p) or "group_unrelated" in json.dumps(p) for p in s.calls)

    # A second, unassigned creator still prevents a uniqueness inference. The
    # conversation remains relevant, but CM-only text needs an explicit target.
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('group_named','ou_other')")
    pg.execute("UPDATE authorization_chats SET member_count=3 WHERE chat_id='group_named'")
    run = pg.execute(
        "SELECT r.* FROM offhours_summary_runs r JOIN offhours_summary_settings s ON s.run_id=r.id"
    ).fetchone()
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
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert not any(ref["chat_id"] == "group_ai" for event in view["events"] for ref in event["evidence"])


def test_current_acl_hides_already_generated_results(pg, seeded):
    s = seeded
    s.save()
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'")
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert not view["events"] and view["run"]["counts"]["failed"] == 1


def test_relevant_chats_are_filtered_before_limit(pg, seeded):
    s = seeded
    for i in range(ai.MAX_CHATS + 1):
        chat = f"aaa-unrelated-{i}"
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (s.ids["cm"], chat))
    assert s.save()["queued"] == 1
    assert ai.claim_next_summary()["chat_id"] == "chat"


@pytest.mark.parametrize("complete_first", [False, True])
def test_a_to_b_to_a_reuses_success_or_requeues_cancelled_with_monotonic_fence(pg, seeded, complete_first):
    s = seeded
    s.save()
    first = ai.claim_next_summary()
    if complete_first:
        ai.run_claimed_summary(first, s.settings)
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
        (s.cipher.encrypt(json.dumps({"text": "A different new request"})),),
    )
    ai.schedule_if_due(pg, force=True)
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
        (s.cipher.encrypt(json.dumps({"text": "Sample please"})),),
    )
    ai.schedule_if_due(pg, force=True)
    if complete_first:
        assert ai.claim_next_summary() is None
        assert len(ai.get_summary_view(pg, s.ids["owner"])["events"]) == 1
        assert len(s.calls) == 1
    else:
        revived = ai.claim_next_summary()
        assert revived["id"] == first["id"] and revived["attempts"] > first["attempts"]
        ai.run_claimed_summary(first, s.settings)
        assert not s.calls
        ai.run_claimed_summary(revived, s.settings)
        assert len(s.calls) == 1


def test_shared_admission_limits_summary_claims_to_three(pg, seeded):
    s = seeded
    s.save()
    run = pg.execute("SELECT run_id FROM offhours_summary_settings").fetchone()["run_id"]
    for i in range(3):
        chat = f"parallel-{i}"
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat,))
        pg.execute(
            """INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff)
                   VALUES(%s,%s,%s,%s,NOW())""",
            (uuid4(), run, chat, str(i)),
        )
    assert all(ai.claim_next_summary() is not None for _ in range(3))
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


def test_failed_or_quota_model_is_never_success_or_routine(pg, seeded, monkeypatch):
    s = seeded
    s.save()
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "refusal"})
    ai.run_claimed_summary(ai.claim_next_summary(), s.settings)
    view = ai.get_summary_view(pg, s.ids["owner"])
    assert not view["events"] and view["run"]["counts"]["failed"] == 1
    assert (
        pg.execute("SELECT last_test_status FROM ai_feature_configs WHERE feature=%s", (ai.FEATURE,)).fetchone()[
            "last_test_status"
        ]
        == "failed"
    )
    ai.ai_dispatch.pause_for_quota()
    assert "配额" in " ".join(ai.get_summary_view(pg, s.ids["owner"])["run"]["gaps"])


def test_network_failure_retries_are_bounded_not_cancelled_refresh_loop(pg, seeded, monkeypatch):
    s = seeded
    s.save()

    def fail(*_a, **_k):
        raise ai.contact_assessments.ContactAssessmentInputError("simulated timeout")

    monkeypatch.setattr(ai.contact_assessments, "call_openai", fail)
    for _ in range(ai.MAX_ATTEMPTS):
        job = ai.claim_next_summary()
        assert job
        ai.run_claimed_summary(job, s.settings)
        pg.execute("UPDATE offhours_summary_tasks SET scheduled_at=NOW()")
    ai.schedule_if_due(pg, force=True)
    assert ai.claim_next_summary() is None
    assert pg.execute("SELECT status FROM offhours_summary_tasks WHERE is_current").fetchone()["status"] == "failed"


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
    original = pg.execute("SELECT body_ciphertext,mentions_json FROM chat_messages WHERE message_id='m1'").fetchone()
    s.save()
    job = ai.claim_next_summary()
    data = ai._load_chat(pg, job, "chat", job["cutoff"], settings=s.settings)
    message = data["pack"]["messages"][0]
    assert message["text"] == "Hi @Remi（Quanzhen Liang）, @person, @other, @Remi: I haven’t heard from you"
    assert [m["target"] for m in message["mentions"]] == ["CM", "U1", "other", "unknown"]
    assert data["refs"][message["ref"]]["text"] == message["text"]
    assert "ou_" not in json.dumps(data["pack"])
    ai.run_claimed_summary(job, s.settings)
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
    ai.schedule_if_due(pg, force=True)
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


def test_rich_mentions_render_before_clipping_and_keep_credentials_redacted():
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
    row.update(message_type="text", body_ciphertext=cipher.encrypt(json.dumps({"text": "@_user_1" + "x" * 2000})))
    # An identifier-like suffix is not a Lark placeholder boundary.
    assert ai._body(row, cipher)[0].startswith("@_user_1x")
    row["body_ciphertext"] = cipher.encrypt(json.dumps({"text": "@_user_1 " + "x" * 2000}))
    text, incomplete = ai._body(row, cipher, {"ou_cm": "Remi（Original）"})
    assert text.startswith("@Remi（Original） ") and len(text) == ai.MAX_BODY_CHARS and incomplete


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
    historical = ai.get_summary_view(pg, s.ids['cm'], day=target['report_date'], period=target['period'])
    if not historical['events']:
        job = ai.claim_next_summary()
        ai.run_claimed_summary(job, s.settings)
        historical = ai.get_summary_view(pg, s.ids['cm'], day=target['report_date'], period=target['period'])
    assert historical['events'] and len(s.calls) == 2
    ai._schedule_run(pg, {**target, 'run_id': target['id']}, now)
    assert pg.execute('SELECT count(*) n FROM offhours_summary_tasks WHERE run_id=%s',
                      (target['id'],)).fetchone()['n'] == 1
    with pytest.raises(PermissionError):
        summary_periods.selection(pg, s.ids['reviewer'], s.ids['cm'])
    cm_id, start, end, selected = summary_periods.selection(pg, s.ids['cm'])
    assert cm_id == s.ids['cm'] and len(selected['dates']) == 7 and end > start
    with pytest.raises(ai.SummaryInputError):
        summary_periods.selection(pg, s.ids['cm'], day='2020-01-01', period='work')
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (s.ids['tester'],))
    second_cm = s.ids['reviewer']
    pg.execute('UPDATE contact_assignments SET responsible_authorization_id=%s', (second_cm,))
    assert ai._roster(pg, second_cm) and not ai._roster(pg, s.ids['cm'])
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


def timeline_event(refs, **changes):
    return dict(existing_id=None, merged_ids=[], anchor_ref=refs[0],handle='@alice', priority='P2',
                status='open',needs_review=False,evidence_refs=refs,title='样品申请',summary='达人申请样品。',
                latest_progress='等待确认。',reason='业务请求。',merge_reason='同一申请的原始消息。') | changes


def test_creator_timeline_full_coverage_history_tools_resume_and_revocation(pg, seeded, monkeypatch):
    from app import creator_summary as timeline
    s = seeded
    s.save()
    run = pg.execute('SELECT * FROM offhours_summary_runs').fetchone()
    run = dict(run, run_id=run['id'], refresh_until=s.end + timedelta(days=1))
    # Long text and >200 current messages; preserve other participants and >7d history.
    for i in range(215):
        s.add(f'bulk-{i}',who='other' if i%3 else 'person', text='x'*25001 if i==0 else f'Sample order {i}',
              at=s.start+timedelta(seconds=i))
    s.add('ancient',text='Original request',at=s.start-timedelta(days=20))
    s.add('future',text='Must never export',at=s.end+timedelta(seconds=1))
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",(s.ids['other'],))
    data = timeline.load(pg,run,s.ids['person'],s.end,s.settings)
    assert len(data['messages']) == 220
    assert sum(len(m['text']) for m in data['messages'] if m['message_id']=='bulk-0') == 25001
    assert all(m['speaker']=='other_creator' for m in data['messages'] if m['message_id']=='outside')
    assert any(m['message_id']=='ancient' for m in data['messages'])
    assert not any(m['message_id']=='future' for m in data['messages'])
    segments = timeline.chunks(data['messages'])
    assert len(segments)>5
    assert [ref for chunk in segments for ref in chunk] == [m['ref'] for m in data['messages']]
    assert not timeline.history(data,dict(query='',chat_id='unauthorized',message_id='',offset=0))['messages']
    assert timeline.history(data,dict(query='Original',chat_id='',message_id='',offset=0))['messages'][0]['message_id']=='ancient'
    # Schedule/claim a creator task in the existing manual run for this isolated fixture.
    timeline.schedule(pg,run,s.end)
    pg.execute('UPDATE offhours_summary_settings SET next_check_at=NULL')
    pg.execute('UPDATE offhours_summary_runs SET next_check_at=NULL')
    job = ai.claim_next_summary()
    ai.run_claimed_summary(job,s.settings)
    progress = pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s',(job['id'],)).fetchone()
    assert progress['status']=='queued' and progress['processed_messages']>0
    checkpoint = timeline.decode(progress['checkpoint_ciphertext'],s.settings)
    assert checkpoint['index']==1
    # Invalid coverage gets feedback and retries the same segment, without advancing it.
    original_model=ai.contact_assessments.call_openai
    monkeypatch.setattr(ai.contact_assessments,'call_openai',lambda *a,**k: {'text':json.dumps({'processed_refs':[], 'updates':[]})})
    following=ai.claim_next_summary()
    ai.run_claimed_summary(following,s.settings)
    bad=pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s',(job['id'],)).fetchone()
    bad_checkpoint=timeline.decode(bad['checkpoint_ciphertext'],s.settings)
    assert bad['status']=='queued' and bad_checkpoint['index']==1
    assert '系统证据校验未通过' in bad_checkpoint['conversation'][-1]['content']
    monkeypatch.setattr(ai.contact_assessments,'call_openai',original_model)
    pg.execute('UPDATE offhours_summary_tasks SET scheduled_at=NOW() WHERE id=%s',(job['id'],))
    # Restart claims the same segment, does not repay/reprocess the successful segment.
    following = ai.claim_next_summary()
    ai.run_claimed_summary(following,s.settings)
    progress = pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s',(job['id'],)).fetchone()
    assert timeline.decode(progress['checkpoint_ciphertext'],s.settings)['index']==2
    calls = len(s.calls)
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s",(s.ids['cm'],))
    following = ai.claim_next_summary()
    ai.run_claimed_summary(following,s.settings)
    assert len(s.calls)==calls
    assert pg.execute('SELECT status FROM offhours_summary_tasks WHERE id=%s',(job['id'],)).fetchone()['status']=='cancelled'


def test_creator_timeline_merge_preserves_id_and_rejects_unsupported_evidence(pg,seeded):
    from app import creator_summary as timeline
    s=seeded
    s.save()
    run=pg.execute('SELECT * FROM offhours_summary_runs').fetchone()
    s.add('second',text='Still waiting for same sample',at=s.start+timedelta(hours=2))
    data=timeline.load(pg,run,s.ids['person'],s.end,s.settings)
    refs=list(data['refs'])
    state={'ledger':[]}
    timeline.apply_updates(json.dumps({'processed_refs':[refs[0]],'updates':[timeline_event([refs[0]])]}),
                           state,data,[refs[0]],set(refs))
    event_id=state['ledger'][0]['id']
    timeline.apply_updates(json.dumps({'processed_refs':[refs[1]],'updates':[
        timeline_event(refs,existing_id=event_id)]}),state,data,[refs[1]],set(refs))
    assert len(state['ledger'])==1 and state['ledger'][0]['id']==event_id
    with pytest.raises(ai.InvalidModelOutput):
        timeline.apply_updates(json.dumps({'processed_refs':[],'updates':[]}),state,data,refs,set(refs))
    with pytest.raises(ai.InvalidModelOutput):
        timeline.apply_updates(json.dumps({'processed_refs':refs,'updates':[timeline_event(['invented'])]}),
                               state,data,refs,set(refs))


def test_creator_timeline_cross_chat_tool_call_and_period_identity(pg, seeded, monkeypatch):
    from app import creator_summary as timeline
    s=seeded
    s.save()
    base=dict(pg.execute('SELECT * FROM offhours_summary_runs').fetchone())
    pg.execute("UPDATE offhours_summary_tasks SET is_current=FALSE,status='cancelled'")
    pg.execute('UPDATE offhours_summary_settings SET next_check_at=NULL')
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('group-b','Payments','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,last_incremental_at) VALUES(%s,'group-b',TRUE,%s)",
               (s.ids['cm'],s.end))
    pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('group-b','ou_person','user')")
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,message_create_time)
        VALUES('group-b','paid','ou_other','text',%s,%s)""",
        (s.cipher.encrypt(json.dumps({'text':'Sample order confirmed in the other chat'})),s.start+timedelta(hours=3)))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,'group-b','paid')",(s.ids['cm'],))
    calls=[]
    def model(_cfg,_key,_pack,_cm,**kwargs):
        conversation=kwargs['conversation']
        pack=json.loads(conversation[0]['content'])
        calls.append(pack)
        if len(calls)==1:
            call={'type':'function_call','name':'read_history','call_id':'call-one',
                  'arguments':json.dumps(dict(query='',chat_id='chat',message_id='m1',offset=0))}
            return {'text':'','output':[call],'function_calls':[call]}
        if len(calls)==2:
            assert conversation[-1]['type']=='function_call_output'
            assert json.loads(conversation[-1]['output'])['messages'][0]['message_id']=='m1'
        refs=[m['ref'] for m in pack['context'] if m['speaker']=='creator' or m['message_id']=='paid']
        old=pack['ledger'][0] if pack['ledger'] else None
        return {'text':json.dumps({'processed_refs':pack['core_refs'],'updates':[
            timeline_event(refs,existing_id=old['id'] if old else None,
                           status='resolved' if any(m['message_id']=='paid' for m in pack['context']) else 'open')]})}
    monkeypatch.setattr(ai.contact_assessments,'call_openai',model)
    def finish(run,cutoff):
        timeline.schedule(pg,dict(run,run_id=run['id'],refresh_until=s.end+timedelta(days=1)),cutoff)
        pg.execute('UPDATE offhours_summary_runs SET next_check_at=NULL')
        pg.execute('UPDATE offhours_summary_settings SET run_id=%s,next_check_at=NULL',(run['id'],))
        for _ in range(12):
            job=ai.claim_next_summary()
            if not job:
                break
            ai.run_claimed_summary(job,s.settings)
        task=pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND is_current',(run['id'],)).fetchone()
        assert task['status']=='succeeded',task['error_message']
        return timeline.view_events(pg,run,task,s.settings)
    base['window_end']=s.start+timedelta(hours=2)
    pg.execute('UPDATE offhours_summary_runs SET window_end=%s WHERE id=%s',(base['window_end'],base['id']))
    first=finish(base,base['window_end'])
    assert len(first)==1 and first[0]['period_activity']=='new' and first[0]['event_status']=='open'
    second=dict(base,id=uuid4(),window_start=base['window_end'],window_end=s.start+timedelta(hours=4))
    pg.execute('''INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,responsible_authorization_id,window_start,window_end)
        VALUES(%s,%s,%s,%s,%s)''',(second['id'],s.ids['owner'],s.ids['cm'],second['window_start'],second['window_end']))
    later=finish(second,second['window_end'])
    assert len(later)==1 and later[0]['id']==first[0]['id']
    assert later[0]['period_activity']=='updated' and later[0]['event_status']=='resolved'
    assert {m['chat_id'] for m in later[0]['evidence']}=={'chat','group-b'}
    oldtask=pg.execute('SELECT * FROM offhours_summary_tasks WHERE run_id=%s AND is_current',(base['id'],)).fetchone()
    historic=timeline.view_events(pg,base,oldtask,s.settings)
    assert historic[0]['event_status']=='open'
    assert all(m['message_id']!='paid' for m in historic[0]['evidence'])
    raw=pg.execute("SELECT body_ciphertext FROM chat_messages WHERE message_id='m1'").fetchone()['body_ciphertext']
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",(s.cipher.encrypt(s.cipher.decrypt(raw)),))
    assert timeline.view_events(pg,base,oldtask,s.settings)[0]['id']==first[0]['id']
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='m1'",
               (s.cipher.encrypt(json.dumps({'text':'Edited message'})),))
    with pytest.raises(ai.SummaryInputError):
        timeline.view_events(pg,base,oldtask,s.settings)
