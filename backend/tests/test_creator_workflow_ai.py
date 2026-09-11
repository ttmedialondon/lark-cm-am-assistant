from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import creator_onboarding as onboarding
from app import creator_workflow_ai as ai
from app.crypto import message_body_cipher


def stage_answer(pack, verdict="pass", **changes):
    stage = pack["stage"]
    user = next(m for m in pack["messages"] if m.get("user") == "U1")
    value = {"verdict": verdict, "confidence": 0.99, "user_refs": [user["ref"]], "user_quote": user["text"][:300]}
    if stage == "ownership":
        cm = next(m for m in pack["messages"] if m.get("speaker") == "CM1")
        value.update(cm_refs=[cm["ref"]], cm_quote=cm["text"][:300])
    if stage == "handle":
        value["handle"] = "@alice" if verdict == "pass" else None
    return {**value, **changes}


def mock_model(monkeypatch, *, overrides=None, before_return=None):
    calls = []

    def model(config, key, pack, cm_id, **kwargs):
        calls.append(pack)
        assert config["model"] == "existing-model" and key == "test-key-not-real"
        assert kwargs["output_format"]["strict"] and pack["users"] == [{"id": "U1", "name": "U1"}]
        value = stage_answer(pack, **(overrides or {}).get(pack["stage"], {}))
        if before_return:
            before_return(pack)
        return {"text": json.dumps(value), "response_id": f"response-{len(calls)}",
                "usage": {"input_tokens": 15, "output_tokens": 5}}

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
    return calls


def execute_stage(settings, expected):
    job = ai.claim_next_creator_scan()
    assert job and job["stage"] == expected
    ai.run_claimed_creator_scan(job, settings)
    return job


def model_evidence(stage):
    messages = [{"ref": "M1", "user": "U1", "text": "My creator account is @alice.",
                 "speaker": "external_or_unknown", "chat": "C1", "interaction": "E1"}]
    if stage == "ownership":
        messages.append({"ref": "M2", "user": None, "speaker": "CM1",
                         "text": "I will arrange your campaign samples.", "reply_to": "M1",
                         "chat": "C1", "interaction": "E1"})
    return {"pack": {"stage": stage, "messages": messages},
            "refs": [{"label": m["ref"]} for m in messages]}


def test_stage_schema_and_exact_reference_guards():
    for stage in ai.STAGES:
        evidence = model_evidence(stage)
        shape = ai._format(evidence, stage)
        assert shape["strict"] and not shape["schema"]["additionalProperties"]
        assert set(shape["schema"]["required"]) == set(shape["schema"]["properties"])
        assert "handle" in shape["schema"]["properties"] if stage == "handle" else (
            "handle" not in shape["schema"]["properties"])
        value = ai._validate(json.dumps(stage_answer(evidence["pack"])), evidence, stage)
        assert value["verdict"] == "pass"
        for change in ({"user_refs": ["fabricated"]}, {"user_quote": "invented private quote"},
                       {"confidence": True}, {"confidence": float("nan")}, {"confidence": 1.1},
                       {"user": "foreign-person"}, {"reason": "private model prose"}):
            with pytest.raises(ai.InvalidModelOutput):
                ai._validate(json.dumps(stage_answer(evidence["pack"], **change)), evidence, stage)
        assert ai._validate(json.dumps(stage_answer(evidence["pack"], confidence=0.5)),
                            evidence, stage)["verdict"] == "unknown"
        # A model refusal is not a negative creator finding.
        with pytest.raises(ai.InvalidModelOutput):
            ai._validate("I cannot help", evidence, stage)


def test_handle_and_ownership_cannot_be_proven_by_wrong_speaker_or_fuzzy_account():
    evidence = model_evidence("handle")
    for handle in ("@ali", "@unseen", "—", "@alice-shop"):
        with pytest.raises(ai.InvalidModelOutput):
            ai._validate(json.dumps(stage_answer(evidence["pack"], handle=handle)), evidence, "handle")
    for text in ("@alice_shop", "xalice", "@alice-shop"):
        assert not ai._exact_handle(text, "alice")
    evidence["pack"]["messages"][0].update(user=None, speaker="CM1")
    value = {"verdict": "pass", "confidence": 0.99, "user_refs": ["M1"],
             "user_quote": "My creator account is @alice.", "handle": "@alice"}
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps(value), evidence, "handle")
    evidence = model_evidence("ownership")
    evidence["pack"]["messages"][1].pop("reply_to")
    assert ai._validate(json.dumps(stage_answer(evidence["pack"])), evidence, "ownership")["verdict"] == "unknown"
    evidence["pack"]["messages"][1]["mentions"] = ["U1"]
    assert ai._validate(json.dumps(stage_answer(evidence["pack"])), evidence, "ownership")["verdict"] == "pass"
    evidence["pack"]["messages"][1]["interaction"] = "E2"
    assert ai._validate(json.dumps(stage_answer(evidence["pack"])), evidence, "ownership")["verdict"] == "unknown"
    evidence = model_evidence("identity")
    evidence["pack"]["messages"][0]["text"] = "Hello CM1!"
    assert ai._validate(json.dumps(stage_answer(evidence["pack"])), evidence)["verdict"] == "unknown"


def test_body_preserves_exact_profiles_resolves_mentions_and_removes_link_secrets():
    cipher = SimpleNamespace(decrypt=lambda _: json.dumps({"content": [[
        {"tag": "a", "text": "my profile", "href": "https://tiktok.com/@alice?access_token=private"},
        {"image_key": "unneeded-media-secret"},
    ]]}))
    text = ai._body(cipher, {"message_type": "post", "body_ciphertext": "cipher"})
    assert "@alice" in text and "my profile" in text and "private" not in text and "unneeded-media-secret" not in text
    cipher = SimpleNamespace(decrypt=lambda _: json.dumps({"text": "Hi @_user_1 https://tiktok.com/@alice?token=private"}))
    row = {"message_type": "text", "body_ciphertext": "cipher",
           "mentions_json": [{"key": "@_user_1", "id_type": "open_id", "id": "ou_cm", "name": "old"}]}
    assert ai._body(cipher, row, {"ou_cm": "CM1"}) == "Hi @CM1 https://tiktok.com/@alice?token=[redacted]"


class Result:
    def __init__(self, row=None, rows=()):
        self.row, self.rows = row, list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def test_authorization_does_not_equate_readonly_review_with_permission_to_scan(monkeypatch):
    actor, cm = uuid4(), uuid4()
    rows = [
        {
            "id": who,
            "status": "active",
            "body_consent_at": datetime.now(UTC),
            "assignment_eligible": True,
            "role": None,
            "role_active": False,
        }
        for who in (actor, cm)
    ]
    conn = SimpleNamespace(execute=lambda *_: Result(rows=rows))
    with pytest.raises(PermissionError):
        ai._authorize(conn, actor, cm)
    rows[0].update(role="owner", role_active=True)
    ai._authorize(conn, actor, cm)
    rows[1]["assignment_eligible"] = False
    with pytest.raises(ai.ScanInputError):
        ai._authorize(conn, actor, cm)


def test_paid_request_gate_prevents_claim(monkeypatch):
    calls = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append(query)
            return Result()

    monkeypatch.setattr(ai, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(ai, "_enabled", lambda _: True)
    monkeypatch.setattr(ai.ai_dispatch, "can_claim", lambda _: False)
    assert ai.claim_next_creator_scan() is None
    assert not any("FOR UPDATE OF t SKIP LOCKED" in q for q in calls)


def test_blank_feature_key_can_be_enabled_on_real_postgres(pg):
    from app.ai_feature_config import api_key_cipher, save_feature_config

    owner = uuid4()
    pg.execute(
        "INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
        (owner, "ou_" + owner.hex),
    )
    values = dict(
        provider="openai", project_id="proj_test", model="configured-model", updated_by_authorization_id=owner
    )
    cipher = api_key_cipher(b"k" * 32)
    save_feature_config(pg, cipher, "offhours_summary", **values, enabled=False, api_key="sk-test-only-1234")
    key_columns = "api_key_ciphertext,key_updated_at,key_updated_by_authorization_id"
    before = pg.execute(f"SELECT {key_columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    saved = save_feature_config(pg, cipher, "offhours_summary", **values, enabled=True, api_key="")
    after = pg.execute(f"SELECT {key_columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    assert saved["enabled"] and saved["key_suffix"] == "1234"
    assert after["api_key_ciphertext"] == before["api_key_ciphertext"]
    assert after["key_updated_at"] == before["key_updated_at"]
    assert after["key_updated_by_authorization_id"] == before["key_updated_by_authorization_id"]


@pytest.fixture
def pg(monkeypatch):
    dsn = os.environ.get("CREATOR_WORKFLOW_AI_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CREATOR_WORKFLOW_AI_TEST_DATABASE_URL for isolated PostgreSQL checks")

    def raise_test_failure(*_args, **_kwargs):
        raise sys.exception()

    monkeypatch.setattr(ai.LOGGER, "warning", raise_test_failure)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        schema = "test_creator_ai_" + uuid4().hex
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            conn.execute(path.read_text())
        monkeypatch.setattr(ai, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(ai.ai_dispatch, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(ai.creator_workflow, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(onboarding, "connection", lambda: nullcontext(conn))
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def real_scan(pg, monkeypatch):
    ids = {name: uuid4() for name in ("cm", "owner", "person", "other")}
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(ai.creator_workflow, "get_settings", lambda: settings)
    for name in ("cm", "owner"):
        pg.execute(
            """INSERT INTO authorizations
                      (id,open_id,display_name,access_token_ciphertext,assignment_eligible,body_consent_at)
                      VALUES(%s,%s,%s,'test',%s,NOW()-INTERVAL '1 day')""",
            (ids[name], "ou_" + name, name, name == "cm"),
        )
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (ids["owner"],))
    for name in ("cm", "person", "other"):
        contact = ids[name]
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact, name))
        pg.execute(
            "INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) VALUES(%s,%s,%s,%s)",
            ("ou_" + name, contact, name, "internal" if name == "cm" else "known_external"),
        )
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('chat','@alice X TT','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat')", (ids["cm"],))
    cipher = message_body_cipher(settings.token_encryption_key)
    for number, name, text in (
        (1, "person", "My creator account is @alice and I make LIVE videos for product campaigns."),
        (2, "cm", "I am your CM and will manage your campaign"),
    ):
        pg.execute(
            """INSERT INTO chat_messages
                      (chat_id,message_id,sender_open_id,message_type,body_ciphertext,message_create_time)
                      VALUES('chat',%s,%s,'text',%s,NOW()-INTERVAL '1 minute'+%s*INTERVAL '1 second')""",
            (str(number), "ou_" + name, cipher.encrypt(json.dumps({"text": text})), number),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,'chat',%s)",
            (ids["cm"], str(number)),
        )
    pg.execute("UPDATE chat_messages SET parent_id='1' WHERE message_id='2'")
    key = ai.api_key_cipher(settings.token_encryption_key).encrypt("test-key-not-real")
    for feature in ("creator_workflow", "contact_assessment", "contact_assignment"):
        pg.execute(
            """INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix)
                      VALUES(%s,'openai','test-project','existing-model',%s,%s,'real')""",
            (feature, feature == ai.FEATURE, key),
        )
    return ids, settings


def start_scan(pg, actor, cm):
    run = ai.preflight_scan(actor, cm)
    return ai.queue_scan(actor, cm, preflight_id=run["id"])


def test_creator_library_reset_clears_only_derived_creator_data_and_cannot_replay(pg, real_scan):
    ids, _settings = real_scan
    action = "creator_library_reset_complete_identity_20260911"
    # The fixture applied this one-off migration to an empty schema; seed its pre-release state.
    pg.execute("DELETE FROM audit_events WHERE action=%s", (action,))
    run = start_scan(pg, ids["owner"], ids["cm"])
    assert ai.claim_next_creator_scan() is not None
    pg.execute(
        "INSERT INTO creator_workflow_onboarding(authorization_id,status,run_id,next_check_at) "
        "VALUES(%s,'scanning',%s,NOW())", (ids["cm"], run["id"]),
    )
    pg.execute("UPDATE creator_workflow_ai_runs SET onboarding_authorization_id=%s", (ids["cm"],))
    account, shared, unrelated, confirmed, pending = (uuid4() for _ in range(5))
    for account_id, handle in ((account, "old_creator"), (shared, "shared"), (unrelated, "merchant")):
        pg.execute(
            "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok',%s,%s)",
            (account_id, handle, handle),
        )
    for contact_id, account_id, source in (
        (ids["person"], account, "creator_workflow_ai_business_verified"),
        (ids["person"], shared, "creator_workflow_cm_confirmation"),
        (ids["person"], unrelated, "manual"),
        (ids["other"], shared, "manual"),
        (ids["other"], unrelated, "manual"),
    ):
        pg.execute(
            "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
            "VALUES(%s,%s,'associated','confirmed',%s)", (contact_id, account_id, source),
        )
    for candidate_id, account_id, handle, status in (
        (confirmed, account, "old_creator", "confirmed"), (pending, shared, "shared", "pending"),
    ):
        pg.execute(
            "INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,contact_id,"
            "relationship,status,social_account_id) VALUES(%s,%s,%s,%s,'associated',%s,%s)",
            (candidate_id, handle, ids["cm"], ids["person"], status, account_id),
        )
        pg.execute(
            "INSERT INTO creator_workflow_sources(id,candidate_id,source_type,source_key,fingerprint) "
            "VALUES(%s,%s,'ai',%s,'old')", (uuid4(), candidate_id, str(candidate_id)),
        )
    pg.execute(
        "INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,contact_id,"
        "relationship,source,candidate_id,created_by_authorization_id) "
        "VALUES(%s,%s,%s,%s,'associated','ai_business_verified',%s,%s)",
        (uuid4(), account, ids["cm"], ids["person"], confirmed, ids["owner"]),
    )
    pg.execute("INSERT INTO creator_workflow_requests VALUES(%s,%s,'old',%s,NOW())",
               (ids["owner"], uuid4(), confirmed))
    pg.execute("INSERT INTO creator_workflow_ai_chat_checks VALUES(%s,'chat',NOW())", (ids["cm"],))
    pg.execute(
        "INSERT INTO creator_workflow_import_batches(id,actor_id,responsible_id,filename,file_hash,rows) "
        "VALUES(%s,%s,%s,'old.xlsx','old','[]')", (uuid4(), ids["owner"], ids["cm"]),
    )
    for person, role in (("person", "creator"), ("person", "partner_contact"), ("other", "merchant_contact")):
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,%s,'confirmed','manual')",
                   (ids[person], role))
    for person, role in (("person", "creator"), ("other", "merchant_contact")):
        assessment_id = uuid4()
        pg.execute("INSERT INTO contact_assignment_assessments(id,contact_id) VALUES(%s,%s)",
                   (assessment_id, ids[person]))
        pg.execute(
            "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,assessment_id) "
            "VALUES(%s,%s,%s,'manual',%s)", (uuid4(), ids[person], ids["cm"], assessment_id),
        )
        pg.execute("INSERT INTO contact_assessments(id,contact_id,result) VALUES(%s,%s,%s::jsonb)",
                   (uuid4(), ids[person], json.dumps({"roles": [{"role": role}]})))
    pg.execute(
        "INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix) "
        "SELECT 'offhours_summary',provider,project_id,model,TRUE,api_key_ciphertext,key_suffix "
        "FROM ai_feature_configs WHERE feature='creator_workflow'",
    )
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE")
    summary = uuid4()
    pg.execute(
        "INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,responsible_authorization_id,"
        "window_start,window_end) VALUES(%s,%s,%s,NOW()-INTERVAL '1 day',NOW())",
        (summary, ids["owner"], ids["cm"]),
    )
    pg.execute("INSERT INTO offhours_summary_settings(run_id,config_fingerprint,refresh_until) VALUES(%s,'old',NOW())",
               (summary,))
    pg.execute("INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff) "
               "VALUES(%s,%s,'chat','old',NOW())", (uuid4(), summary))
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'messages')",
               (ids["cm"],))
    protected = (
        "contacts", "lark_identities", "chats", "chat_messages", "authorization_message_observations",
        "authorizations", "authorization_chats", "admin_roles", "admin_page_permissions",
    )
    snapshot = {table: pg.execute(f"SELECT * FROM {table}").fetchall() for table in protected}
    config_columns = "feature,provider,project_id,model,api_key_ciphertext,key_suffix"
    config = pg.execute(f"SELECT {config_columns} FROM ai_feature_configs ORDER BY feature").fetchall()
    migration = (Path(__file__).parents[1] / "migrations/041_reset_creator_library_complete_identity.sql").read_text()
    pg.execute(migration)
    for table in (
        "creator_workflow_candidates", "creator_workflow_sources", "creator_account_assignments",
        "creator_workflow_requests", "creator_workflow_import_batches", "creator_workflow_ai_runs",
        "creator_workflow_ai_tasks", "creator_workflow_onboarding", "creator_workflow_ai_chat_checks",
        "offhours_summary_settings", "offhours_summary_runs", "offhours_summary_tasks",
    ):
        assert pg.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0, table
    assert all(pg.execute(f"SELECT * FROM {table}").fetchall() == rows for table, rows in snapshot.items())
    assert {row["id"] for row in pg.execute("SELECT id FROM social_accounts")} == {shared, unrelated}
    remaining_links = pg.execute("SELECT contact_id,social_account_id FROM contact_social_accounts").fetchall()
    assert {(row["contact_id"], row["social_account_id"]) for row in remaining_links} == {
        (ids["person"], unrelated), (ids["other"], shared), (ids["other"], unrelated),
    }
    assert {row["role"] for row in pg.execute("SELECT role FROM contact_roles")} == {
        "partner_contact", "merchant_contact",
    }
    for table in ("contact_assignments", "contact_assignment_assessments", "contact_assessments"):
        assert [row["contact_id"] for row in pg.execute(f"SELECT contact_id FROM {table}")] == [ids["other"]]
    assert pg.execute(f"SELECT {config_columns} FROM ai_feature_configs ORDER BY feature").fetchall() == config
    assert pg.execute("SELECT count(*) AS n FROM ai_feature_configs WHERE enabled").fetchone()["n"] == 0
    assert ai.claim_next_creator_scan() is None
    assert not onboarding.advance_initial_scans()
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action=%s", (action,)).fetchone()["metadata"]
    assert audit["deleted"]["candidates"] == 2 and audit["deleted"]["social_accounts"] == 1
    assert audit["deleted"]["social_links"] == 2 and audit["protected_counts"]["chat_messages"] == 2
    # Replaying the migration must not erase a new roster or replace the original audit.
    pg.execute("INSERT INTO creator_workflow_candidates(id,normalized_handle) VALUES(%s,'new_roster')", (uuid4(),))
    pg.execute(migration)
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_candidates").fetchone()["n"] == 1
    assert pg.execute("SELECT count(*) AS n FROM audit_events WHERE action=%s", (action,)).fetchone()["n"] == 1


def test_three_ordered_stages_auto_admit_once_and_save_retry_never_replays_openai(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    calls = mock_model(monkeypatch)
    run = ai.preflight_scan(ids["owner"], ids["cm"])
    assert run["total_people"] == 1 and run["status"] == "ready" and not run["preview_only"]
    assert run["request_count"] == 0 and run["request_limit"] == 4
    assert not calls and ai.claim_next_creator_scan() is None
    preview_person = ai.preflight_details(ids["owner"], ids["cm"], run["id"])["people"][0]
    assert len(preview_person["messages"]) == 1
    assert "candidate_snapshot" not in preview_person and "excel_refs" not in preview_person
    for _ in range(2):
        assert ai.queue_scan(ids["owner"], ids["cm"], preflight_id=run["id"])["id"] == run["id"]
    first = execute_stage(settings, "identity")
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["status"] == "pending" and row["normalized_handle"] is None
    execute_stage(settings, "ownership")
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()
    refresh, saves = ai._refresh_run, []

    def deadlock_once(conn, run_id):
        saves.append(True)
        if len(saves) == 1:
            raise psycopg.errors.DeadlockDetected("rollback only the save transaction")
        refresh(conn, run_id)

    job = ai.claim_next_creator_scan()
    assert job["stage"] == "handle" and job["initial_fingerprint"] == first["input_fingerprint"]
    monkeypatch.setattr(ai, "_refresh_run", deadlock_once)
    ai.run_claimed_creator_scan(job, settings)
    monkeypatch.setattr(ai, "_refresh_run", refresh)
    assert len(saves) == 2 and [p["stage"] for p in calls] == list(ai.STAGES)
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["counts"] == {"succeeded": 1} and status["outcomes"] == {"confirmed": 1}
    assert status["candidate_count"] == 1 and status["request_count"] == 3 and not status["error_summary"]
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["status"] == "confirmed" and row["normalized_handle"] == "alice" and row["creator_type"] is None
    assert row["contact_id"] == ids["person"] and row["responsible_authorization_id"] == ids["cm"]
    assert pg.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["usage"] == {"input_tokens": 45, "output_tokens": 15} and task["retry_count"] == 0
    source = pg.execute("SELECT source_data FROM creator_workflow_sources ORDER BY created_at,id").fetchall()
    stored = json.dumps([task["stage_results"], source])
    assert "user_quote" not in stored and "cm_quote" not in stored and "My creator account" not in stored
    assert not pg.execute("SELECT 1 FROM contact_roles").fetchone()
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    assert ai.claim_next_creator_scan() is None and len(calls) == 3


@pytest.mark.parametrize("stage,verdict", [
    ("identity", "fail"), ("identity", "unknown"), ("ownership", "fail"), ("ownership", "unknown"),
])
def test_fail_or_unknown_stops_before_later_stages_and_never_assigns(pg, real_scan, monkeypatch, stage, verdict):
    ids, settings = real_scan
    calls = mock_model(monkeypatch, overrides={stage: {"verdict": verdict}})
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    if stage == "ownership":
        execute_stage(settings, "ownership")
    assert [p["stage"] for p in calls] == (["identity", "ownership"] if stage == "ownership" else ["identity"])
    assert ai.claim_next_creator_scan() is None
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["status"] == "succeeded" and task["stage_results"][stage]["verdict"] == verdict
    if verdict == "unknown":
        row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
        assert row and row["status"] == "pending" and row["contact_id"] == ids["person"]
        # Having both fields or an Excel line does not bypass the two AI gates.
        pg.execute("UPDATE creator_workflow_candidates SET normalized_handle='alice' WHERE id=%s", (row["id"],))
        scope = ai.creator_workflow._scope(pg, ids["cm"], ids["cm"])
        with pytest.raises(ai.creator_workflow.WorkflowInputError):
            ai.creator_workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0


def test_identity_without_cm_reply_then_missing_ownership_is_unknown_not_negative(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    pg.execute("UPDATE chat_messages SET parent_id=NULL WHERE message_id='2'")
    calls = mock_model(monkeypatch)
    assert start_scan(pg, ids["owner"], ids["cm"])["total_people"] == 1
    execute_stage(settings, "identity")
    assert len(calls) == 1 and calls[0]["stage"] == "identity"
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["stage"] == "ownership" and task["person_outcome"] == "ownership_unknown"
    assert task["stage_results"]["identity"]["verdict"] == "pass"
    assert task["stage_results"]["ownership"]["verdict"] == "unknown"
    assert ai.claim_next_creator_scan() is None
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()


def test_missing_handle_stays_pending_after_two_passes_and_empty_handle_cannot_confirm(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    cipher = message_body_cipher(settings.token_encryption_key)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='1'",
               (cipher.encrypt(json.dumps({"text": "Please arrange samples for my next LIVE video."})),))
    calls = mock_model(monkeypatch)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    execute_stage(settings, "ownership")
    assert len(calls) == 2 and ai.claim_next_creator_scan() is None
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["status"] == "pending" and row["normalized_handle"] is None and row["contact_id"] == ids["person"]
    assert ai.scan_status(ids["owner"], ids["cm"])["outcomes"] == {"handle_missing": 1}
    scope = ai.creator_workflow._scope(pg, ids["cm"], ids["cm"])
    with pytest.raises(ai.creator_workflow.WorkflowInputError, match="handle"):
        ai.creator_workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()


@pytest.mark.parametrize("when", ["before_next_request", "during_next_request"])
def test_changed_prior_stage_source_cancels_later_work(pg, real_scan, monkeypatch, when):
    ids, settings = real_scan
    calls = mock_model(monkeypatch)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")

    def change(_pack=None):
        pg.execute("UPDATE chat_messages SET message_update_time=NOW() WHERE message_id='1'")

    if when == "before_next_request":
        change()
    else:
        calls = mock_model(monkeypatch, before_return=change)
    execute_stage(settings, "ownership")
    assert len(calls) == 1
    assert ai.scan_status(ids["owner"], ids["cm"])["counts"] == {"cancelled": 1}
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_sources").fetchone()["n"] == 1
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_ai_tasks").fetchone()["n"] == 1


def test_revoked_scope_hides_frozen_preview_and_never_sends_model(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    start_scan(pg, ids["owner"], ids["cm"])
    job = ai.claim_next_creator_scan()
    pg.execute("UPDATE creator_workflow_ai_runs SET preflight_json=preflight_json||%s::jsonb WHERE id=%s",
               (json.dumps({"local_pending": [{"contact_id": str(ids["person"]), "display_name": "old name"}]}),
                job["run_id"]))
    assert ai.preflight_details(ids["owner"], ids["cm"], job["run_id"])["local_pending"][0]["display_name"] == "person"
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (ids["cm"],))
    calls = mock_model(monkeypatch)
    ai.run_claimed_creator_scan(job, settings)
    assert not calls and ai.scan_status(ids["owner"], ids["cm"])["status"] == "cancelled"
    preview = ai.preflight_details(ids["owner"], ids["cm"], job["run_id"])
    person = preview["people"][0]
    assert person["unavailable"] and person["messages"] == [] and "contact_id" not in person and "name" not in person
    assert preview["local_pending"] == [] and preview["local_pending_hidden_count"] == 1
    assert "local_pending" not in preview["preflight_json"]


def test_pause_fences_inflight_results_and_cannot_restore_legacy_bulk_scan(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    with pytest.raises(PermissionError):
        ai.preflight_scan(ids["other"], ids["cm"])
    with pytest.raises(ai.ScanInputError):
        ai.preflight_scan(ids["owner"], ids["cm"], limit=31)
    run = start_scan(pg, ids["owner"], ids["cm"])
    mock_model(monkeypatch, before_return=lambda _: ai.pause_scan(ids["owner"], ids["cm"]))
    execute_stage(settings, "identity")
    assert ai.scan_status(ids["owner"], ids["cm"])["counts"] == {"cancelled": 1}
    assert not pg.execute("SELECT 1 FROM creator_workflow_sources").fetchone()
    assert ai.claim_next_creator_scan() is None
    pg.execute("UPDATE creator_workflow_ai_runs SET status='queued',scan_mode='legacy' WHERE id=%s", (run["id"],))
    pg.execute("UPDATE creator_workflow_ai_tasks SET status='queued' WHERE run_id=%s", (run["id"],))
    assert ai.claim_next_creator_scan() is None


def test_one_shared_retry_across_stages_and_new_preflight_cannot_reset_budget(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    calls = mock_model(monkeypatch)
    run = start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "refusal"})
    execute_stage(settings, "ownership")
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["counts"] == {"invalid": 1} and status["retryable_people"] == 1
    assert ai.claim_next_creator_scan() is None
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    with pytest.raises(ai.ScanInputError):
        ai.queue_scan(ids["owner"], ids["cm"], retry_failed=True)
    ai.queue_scan(ids["owner"], ids["cm"], preflight_id=run["id"], retry_failed=True)
    calls += mock_model(monkeypatch)
    retry = execute_stage(settings, "ownership")
    assert retry["attempts"] == 3 and retry["retry_count"] == 1 and retry["stage_attempts"] == 2
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "refusal"})
    handle = execute_stage(settings, "handle")
    assert handle["attempts"] == 4 and handle["retry_count"] == 1 and handle["stage_attempts"] == 1
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["retryable_people"] == 0 and status["request_count"] == 4
    with pytest.raises(ai.ScanInputError):
        ai.queue_scan(ids["owner"], ids["cm"], preflight_id=run["id"], retry_failed=True)
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()


@pytest.mark.parametrize("stage", ai.STAGES)
def test_new_evidence_reopens_only_unknown_stage_in_original_task_and_budget(pg, real_scan, monkeypatch, stage):
    ids, settings = real_scan
    calls = mock_model(monkeypatch, overrides={stage: {"verdict": "unknown"}})
    run = start_scan(pg, ids["owner"], ids["cm"])
    for current in ai.STAGES[:ai.STAGES.index(stage) + 1]:
        execute_stage(settings, current)
    before = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    assert ai.claim_next_creator_scan() is None
    cipher = message_body_cipher(settings.token_encryption_key)
    sender = "ou_cm" if stage == "ownership" else "ou_person"
    text = "I will arrange your next campaign samples." if stage == "ownership" else (
        "My TikTok handle is @alice and I create my own LIVE product videos.")
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,"
               "message_create_time,parent_id) VALUES('chat','new-evidence',%s,'text',%s,clock_timestamp(),'1')",
               (sender, cipher.encrypt(json.dumps({"text": text}))))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat','new-evidence')", (ids["cm"],))
    prepared = ai.preflight_scan(ids["owner"], ids["cm"])
    assert prepared["id"] == run["id"] and prepared["status"] == "ready" and prepared["resumed"]
    assert prepared["prepared_people"] == 1 and prepared["request_count"] == len(calls)
    assert ai.claim_next_creator_scan() is None  # preparing is not permission to pay
    resumed = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert resumed["id"] == before["id"] and resumed["attempts"] == before["attempts"]
    assert resumed["initial_fingerprint"] == before["initial_fingerprint"]
    for previous in ai.STAGES[:ai.STAGES.index(stage)]:
        assert resumed["stage_results"][previous] == before["stage_results"][previous]
    calls_after = mock_model(monkeypatch)
    ai.queue_scan(ids["owner"], ids["cm"], preflight_id=run["id"])
    for current in ai.STAGES[ai.STAGES.index(stage):]:
        execute_stage(settings, current)
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["attempts"] == 4 and task["retry_count"] == 1 and task["person_outcome"] == "confirmed"
    assert [p["stage"] for p in calls_after] == list(ai.STAGES[ai.STAGES.index(stage):])
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_ai_tasks").fetchone()["n"] == 1
    assert pg.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1


def test_previously_absent_handle_can_be_prepared_without_spending_retry(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    cipher = message_body_cipher(settings.token_encryption_key)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='1'",
               (cipher.encrypt(json.dumps({"text": "Please arrange samples for my next LIVE video."})),))
    calls = mock_model(monkeypatch)
    run = start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    execute_stage(settings, "ownership")
    old = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert old["stage_attempts"] == 0 and old["person_outcome"] == "handle_missing"
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,"
               "message_create_time) VALUES('chat','handle-later','ou_person','text',%s,clock_timestamp())",
               (cipher.encrypt(json.dumps({"text": "My TikTok handle is @alice."})),))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat','handle-later')", (ids["cm"],))
    assert ai.preflight_scan(ids["owner"], ids["cm"])["id"] == run["id"]
    # The account answer must cite the newly arrived self-identification, not an older business message.
    def handle_model(config, key, pack, cm, **kwargs):
        message = next(m for m in pack["messages"] if "@alice" in m["text"])
        return {"text": json.dumps(stage_answer(pack, user_refs=[message["ref"]], user_quote=message["text"]))}

    monkeypatch.setattr(ai.contact_assessments, "call_openai", handle_model)
    ai.queue_scan(ids["owner"], ids["cm"], preflight_id=run["id"])
    execute_stage(settings, "handle")
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert len(calls) == 2 and task["attempts"] == 3 and task["retry_count"] == 0
    assert task["person_outcome"] == "confirmed"


@pytest.mark.parametrize("action", ["reject", "change_user"])
def test_cm_change_during_request_cannot_resurrect_old_person_or_formal_assignment(
    pg, real_scan, monkeypatch, action,
):
    ids, settings = real_scan
    mock_model(monkeypatch)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")

    def change(_pack):
        row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
        if action == "reject":
            scope = ai.creator_workflow._scope(pg, ids["cm"], ids["cm"])
            ai.creator_workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4())
        else:
            # Also exercise the snapshot fence independently of the proactive workflow cancellation.
            pg.execute("UPDATE creator_workflow_candidates SET contact_id=%s,version=version+1 WHERE id=%s",
                       (ids["other"], row["id"]))

    mock_model(monkeypatch, before_return=change)
    execute_stage(settings, "ownership")
    assert ai.claim_next_creator_scan() is None
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_candidates").fetchone()["n"] == 1
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_sources").fetchone()["n"] == 1
    assert ai.preflight_scan(ids["owner"], ids["cm"])["total_people"] == 0
    assert pg.execute("SELECT count(*) AS n FROM creator_workflow_ai_tasks").fetchone()["n"] == 1


def test_cap_only_change_does_not_cancel_valid_person_stages(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    mock_model(monkeypatch)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")

    def cap_only(_pack):
        pg.execute("UPDATE creator_workflow_candidates SET creator_type='cap',version=version+1")

    calls = mock_model(monkeypatch, before_return=cap_only)
    execute_stage(settings, "ownership")
    execute_stage(settings, "handle")
    assert len(calls) == 2
    outcome = pg.execute("SELECT person_outcome FROM creator_workflow_ai_tasks").fetchone()["person_outcome"]
    assert outcome == "confirmed"
    assert pg.execute("SELECT creator_type FROM creator_workflow_candidates").fetchone()["creator_type"] == "cap"


def test_cm_can_complete_handle_during_ownership_without_canceling_or_third_paid_call(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    calls = mock_model(monkeypatch)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")

    def fill_handle(_pack):
        row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
        scope = ai.creator_workflow._scope(pg, ids["cm"], ids["cm"])
        saved = ai.creator_workflow.save_draft(
            scope, row["id"], row["version"], handle="@alice", contact_id=ids["person"],
            handle_verified=True, request_id=uuid4(),
        )
        assert saved["status"] == "pending"  # the CM cannot bypass unfinished ownership AI
        assert pg.execute("SELECT status FROM creator_workflow_ai_tasks").fetchone()["status"] == "running"

    ownership_calls = mock_model(monkeypatch, before_return=fill_handle)
    execute_stage(settings, "ownership")
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert len(calls) + len(ownership_calls) == 2 and task["attempts"] == 2
    assert task["person_outcome"] == "confirmed" and task["stage_results"]["handle"]["source"] == "cm"
    assert task["stage_results"]["handle"]["confidence"] is None
    assert ai.claim_next_creator_scan() is None
    assert pg.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1


def test_429_uses_shared_admission_and_exhausted_retry_remains_failure(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    throttled = []
    monkeypatch.setattr(ai.ai_dispatch, "throttle", lambda seconds: throttled.append(seconds))

    def limited(*_args, **_kwargs):
        raise ai.contact_assessments.OpenAIRequestError("provider-secret", retryable=True, retry_after_seconds=10)

    monkeypatch.setattr(ai.contact_assessments, "call_openai", limited)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    assert len(throttled) == 1
    pg.execute("UPDATE creator_workflow_ai_tasks SET scheduled_at=NOW()")
    job = execute_stage(settings, "identity")
    assert job["retry_count"] == 1
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["status"] == "failed" and "provider-secret" not in task["error_message"]
    assert len(throttled) == 1 and ai.claim_next_creator_scan() is None


@pytest.mark.parametrize("failure_kind", ["database", "timeout"])
def test_runtime_failures_keep_safe_reason_without_storing_exception_secrets(
    pg, real_scan, monkeypatch, failure_kind,
):
    ids, settings = real_scan
    calls, failures, logs = mock_model(monkeypatch), [], []
    secret = "private-chat-or-key"

    def fail(*_args, **_kwargs):
        failures.append(True)
        if failure_kind == "database":
            raise psycopg.errors.DeadlockDetected(secret)
        raise ai.contact_assessments.ContactAssessmentInputError(secret) from ai.httpx.ReadTimeout(secret)

    monkeypatch.setattr(ai.LOGGER, "warning", lambda *args: logs.append(args))
    if failure_kind == "database":
        monkeypatch.setattr(ai, "_persist_stage_candidate", fail)
    else:
        monkeypatch.setattr(ai.contact_assessments, "call_openai", fail)
    start_scan(pg, ids["owner"], ids["cm"])
    execute_stage(settings, "identity")
    task = pg.execute("SELECT * FROM creator_workflow_ai_tasks").fetchone()
    assert task["status"] == "failed" and task["person_outcome"] is None
    assert secret not in task["error_message"] + json.dumps(logs, default=str)
    assert not pg.execute("SELECT 1 FROM creator_workflow_sources").fetchone()
    if failure_kind == "database":
        assert len(failures) == 3 and len(calls) == 1 and task["response_id"] == "response-1"
        assert "数据库死锁" in task["error_message"] and "OpenAI 已返回" in task["error_message"]
    else:
        assert len(failures) == 1 and not calls and task["response_id"] is None
        assert "OpenAI 请求超时" in task["error_message"]


def test_disabled_feature_cancels_claimed_task_without_model(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    start_scan(pg, ids["owner"], ids["cm"])
    job = ai.claim_next_creator_scan()
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature=%s", (ai.FEATURE,))
    calls = mock_model(monkeypatch)
    ai.run_claimed_creator_scan(job, settings)
    assert not calls and ai.scan_status(ids["owner"], ids["cm"])["counts"] == {"cancelled": 1}
    assert ai.claim_next_creator_scan() is None and not pg.execute("SELECT 1 FROM creator_workflow_sources").fetchone()


def test_three_shared_slots_allow_different_people_not_parallel_stages_for_one_person(pg, real_scan):
    ids, settings = real_scan
    cipher = message_body_cipher(settings.token_encryption_key)
    for index in range(3):
        contact, open_id, mid = uuid4(), f"ou_extra_{index}", f"extra-{index}"
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'extra')", (contact,))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) "
                   "VALUES(%s,%s,'extra','known_external')", (open_id, contact))
        pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,"
                   "message_create_time) VALUES('chat',%s,%s,'text',%s,NOW()-INTERVAL '1 minute')",
                   (mid, open_id, cipher.encrypt(json.dumps({"text": "I make LIVE product videos."}))))
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,'chat',%s)", (ids["cm"], mid))
    assert start_scan(pg, ids["owner"], ids["cm"])["total_people"] == 4
    jobs = [ai.claim_next_creator_scan() for _ in range(3)]
    assert all(jobs) and len({job["contact_id"] for job in jobs}) == 3
    assert all(job["stage"] == "identity" for job in jobs)
    assert ai.claim_next_creator_scan() is None
    ai._finish(jobs[0], "failed", "test")
    assert ai.claim_next_creator_scan() is not None


def test_historical_stage_evidence_redaction_excel_freeze_and_own_speech_scope(pg, real_scan):
    from app import creator_targeting as targeting

    ids, settings = real_scan
    cipher = message_body_cipher(settings.token_encryption_key)
    original = ("My creator account is @alice. My TikTok is https://www.tiktok.com/@123456789. "
                "Contact alice@example.test or +44 7911 123456. "
                "Delivery address: 42 Example Road\nI need samples for my next LIVE.")
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='1'",
               (cipher.encrypt(json.dumps({"text": original})),))
    pg.execute("UPDATE chat_messages SET message_create_time=message_create_time-INTERVAL '2 years'")
    pg.execute("UPDATE lark_identities SET display_name='alice' WHERE open_id='ou_person'")
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,"
               "message_create_time) "
               "VALUES('chat','third-party','ou_other','text',%s,NOW()-INTERVAL '2 years')",
               (cipher.encrypt(json.dumps({"text": "THIRD-PARTY SECRET: I need campaign samples too"})),))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat','third-party')", (ids["cm"],))
    preflight = targeting.build_preflight(pg, settings, ids["cm"])
    assert preflight["summary"]["selected_people"] == 2 and preflight["summary"]["local_pending_people"] == 0
    person = next(p for p in preflight["people"] if p["contact_id"] == str(ids["person"]))
    assert person["message_count"] == 1 and person["pack"]["stage"] == "identity"
    encoded = json.dumps(person["pack"], ensure_ascii=False, separators=(",", ":"))
    assert person["char_count"] == len(encoded) <= targeting.MAX_PACK_CHARS
    assert "THIRD-PARTY SECRET" not in encoded and "alice@example.test" not in encoded and "7911" not in encoded
    assert "42 Example Road" not in encoded and "@alice" in encoded and "@123456789" in encoded
    for stage in ai.STAGES:
        evidence = targeting.build_stage_evidence(
            pg, settings, ids["cm"], ids["person"], stage, preflight["window_end"])
        assert evidence and evidence["pack"]["stage"] == stage
        job = {**person, **evidence, "stage": stage, "evidence_refs": evidence["refs"],
               "responsible_authorization_id": ids["cm"], "window_end": preflight["window_end"]}
        assert targeting.load_person_evidence(pg, job, settings)["pack"] == evidence["pack"]
    candidate, source = uuid4(), uuid4()
    pg.execute("INSERT INTO creator_workflow_candidates(id,normalized_handle,contact_id,responsible_authorization_id) "
               "VALUES(%s,'alice',%s,%s)", (candidate, ids["person"], ids["cm"]))
    pg.execute("INSERT INTO creator_workflow_sources(id,candidate_id,source_type,source_key,source_data,fingerprint) "
               "VALUES(%s,%s,'excel','sheet:1',%s::jsonb,'frozen')",
               (source, candidate, json.dumps({"proposed_cm_id": str(ids["cm"]), "proposed_handle": "alice"})))
    evidence = targeting.build_stage_evidence(pg, settings, ids["cm"], ids["person"], "handle", preflight["window_end"])
    assert evidence["pack"]["handle_candidates"] == [{"handle": "alice", "source": "excel", "ref": "X1"}]
    assert all(ref["label"].startswith("M") for ref in evidence["refs"])
    job = {**person, **evidence, "stage": "handle", "evidence_refs": evidence["refs"],
           "responsible_authorization_id": ids["cm"], "window_end": preflight["window_end"]}
    assert targeting.load_person_evidence(pg, job, settings)["pack"] == evidence["pack"]
    pg.execute("UPDATE creator_workflow_candidates SET contact_id=%s WHERE id=%s", (ids["other"], candidate))
    with pytest.raises(ai.ScanInputError, match="Excel"):
        targeting.load_person_evidence(pg, job, settings)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='1'",
               (cipher.encrypt(json.dumps({"text": original + " changed"})),))
    identity_job = {**person, "responsible_authorization_id": ids["cm"], "window_end": preflight["window_end"]}
    with pytest.raises(ai.ScanInputError, match="已变化"):
        targeting.load_person_evidence(pg, identity_job, settings)
