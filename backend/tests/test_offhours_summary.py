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
            if int(path.name[:3]) <= 32:
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
    for name in ("tester", "reviewer"):
        pg.execute(
            "INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-review')", (ids[name],)
        )
    for name in ("cm", "person", "other"):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (ids[name], name))
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
        """INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,
               contact_id,relationship,source,created_by_authorization_id,candidate_id)
               VALUES(%s,%s,%s,%s,'associated','cm_confirmation',%s,%s)""",
        (uuid4(), account, ids["cm"], ids["person"], ids["cm"], candidate_id),
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
        calls.append(input_pack)
        assert kwargs["output_format"]["strict"]
        message = next(m for m in input_pack["messages"] if m["in_window"] and m["speaker"] == "U1")
        return {
            "text": json.dumps({"events": [sample_event(evidence_refs=[message["ref"]])]}),
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
    return SimpleNamespace(
        ids=ids, settings=settings, save=save, calls=calls, add=add, start=start, end=end, fields=fields, cipher=cipher
    )


def test_save_enabled_immediately_queues_claims_and_stores_encrypted_summary(pg, seeded):
    s = seeded
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
    with pytest.raises(PermissionError):
        ai.get_summary_view(pg, s.ids["tester"])


@pytest.mark.parametrize("change", ["hide", "delete", "reassign", "consent"])
def test_revocation_or_edit_before_provider_exports_nothing(pg, seeded, change):
    s = seeded
    s.save()
    job = ai.claim_next_summary()
    if change == "hide":
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE")
    elif change == "delete":
        pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'")
    elif change == "reassign":
        pg.execute("UPDATE creator_account_assignments SET ended_at=NOW()")
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
