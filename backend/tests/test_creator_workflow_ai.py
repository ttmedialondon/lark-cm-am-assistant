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

from app import creator_workflow_ai as ai
from app.crypto import message_body_cipher


def evidence():
    return {
        "pack": {
            "cm": {"id": "CM1", "name": "Remi"},
            "chat_name": "@alice X TT",
            "chat_type": "group",
            "users": [{"id": "U1", "name": "Alice"}],
            "messages": [
                {"ref": "M1", "user": "U1", "text": "My creator account is @alice", "speaker": "external_or_unknown"},
                {"ref": "M2", "user": None, "text": "I am your CM and will manage your campaign", "speaker": "CM1"},
            ],
        },
        "users": {"U1": {"contact_id": uuid4(), "name": "Alice"}},
        "refs": [
            {"label": "M1", "authorization_id": str(uuid4()), "chat_id": "chat", "message_id": "one"},
            {"label": "M2", "authorization_id": str(uuid4()), "chat_id": "chat", "message_id": "two"},
        ],
    }


def candidate(**changes):
    return {
        "handle": "@alice",
        "handle_source": "message",
        "handle_refs": ["M1"],
        "user": "U1",
        "responsible": "CM1",
        "relationship": "owner",
        "creator_refs": ["M1"],
        "responsibility_refs": ["M2"],
        "reason": "本人说明账号，CM 明确承接合作。",
        "unknowns": [],
        "alternative_handles": [],
        **changes,
    }


def test_structured_candidates_preserve_missing_fields_and_exact_spelling():
    pack = evidence()
    item = candidate(handle=None, user=None, handle_source="missing", handle_refs=[], unknowns=["handle 和用户缺失"])
    assert ai._validate(json.dumps({"candidates": [item]}), pack) == [item]
    assert ai._validate('{"candidates": []}', pack) == []
    shape = ai._format(pack)
    assert shape["strict"] is True
    schema = shape["schema"]["properties"]["candidates"]["items"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"user": "arbitrary-population-id"},
        {"responsible": "other-CM"},
        {"creator_refs": []},
        {"creator_refs": ["fabricated"]},
        {"responsibility_refs": []},
        {"handle": "@unseen"},
        {"alternative_handles": ["@unseen"]},
        {"handle": "—"},
        {"handle": None},
        {"relationship": "creator"},
    ],
)
def test_untraceable_or_invented_candidates_are_rejected(change):
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"candidates": [candidate(**change)]}), evidence())


def test_group_handle_and_unclear_cm_remain_supported_candidates_not_assignments():
    item = candidate(
        handle_source="chat_name",
        handle_refs=[],
        responsible=None,
        responsibility_refs=[],
        relationship="representative",
        unknowns=["负责人不明"],
    )
    assert ai._validate(json.dumps({"candidates": [item]}), evidence()) == [item]
    assert "Mere P2P contact" in ai._INSTRUCTIONS
    assert "untrusted evidence" in ai._INSTRUCTIONS
    assert "not proof of creator identity" in ai._INSTRUCTIONS
    assert "Do not reply" in ai._INSTRUCTIONS


def test_fingerprint_ignores_routine_cipher_refresh_but_detects_edits_and_new_identity():
    first = {
        "message_id": "one",
        "message_update_time": None,
        "first_observed_at": "first",
        "sender_open_id": "ou_1",
        "contact_id": "contact",
        "sender_name": "Alice",
        "body_ciphertext": "random ciphertext",
        "last_observed_at": "today",
    }
    chat = {"name": "@alice X TT", "chat_type": "group"}
    stable = ai._fingerprint(chat, [first])
    assert ai._fingerprint(chat, [{**first, "body_ciphertext": "new cipher", "last_observed_at": "tomorrow"}]) == stable
    assert ai._fingerprint(chat, [{**first, "message_update_time": "new edit"}]) != stable
    assert ai._fingerprint(chat, [{**first, "contact_id": "other person"}]) != stable
    assert ai._fingerprint({**chat, "name": "renamed"}, [first]) != stable


def test_rich_text_keeps_product_handle_links_but_redacts_access_tokens():
    cipher = SimpleNamespace(
        decrypt=lambda _: json.dumps(
            {
                "content": [
                    [
                        {"tag": "a", "text": "my profile", "href": "https://tiktok.com/@alice?access_token=private"},
                        {"image_key": "unneeded-media-secret"},
                    ]
                ]
            }
        )
    )
    text = ai._body(cipher, {"message_type": "post", "body_ciphertext": "cipher"})
    assert "@alice" in text and "my profile" in text
    assert "private" not in text and "unneeded-media-secret" not in text


class Result:
    def __init__(self, row=None, rows=()):
        self.row, self.rows = row, list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


@pytest.fixture
def run_harness(monkeypatch):
    calls, ingested, finished = [], [], []
    job = {
        "id": uuid4(),
        "run_id": uuid4(),
        "attempts": 1,
        "responsible_authorization_id": uuid4(),
        "requested_by_authorization_id": uuid4(),
        "chat_id": "chat",
    }
    config = {"enabled": True, "provider": "openai", "api_key_ciphertext": "cipher", "model": "configured-model"}
    pack = evidence()

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT provider, model, project_id" in query:
                return Result(config)
            if "SELECT status, attempts" in query:
                return Result({"status": "running", "attempts": job["attempts"], "leased": True})
            return Result()

    monkeypatch.setattr(ai, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(ai, "_load_evidence", lambda *_: pack)
    monkeypatch.setattr(ai, "_authorize", lambda *_: None)
    monkeypatch.setattr(ai, "_enabled", lambda _: True)
    monkeypatch.setattr(ai.creator_workflow, "validate_evidence", lambda *_: pack["refs"])
    monkeypatch.setattr(ai, "api_key_cipher", lambda _: SimpleNamespace(decrypt=lambda _: "test-not-a-real-key"))
    monkeypatch.setattr(ai.creator_workflow, "ingest_source", lambda *args, **kwargs: ingested.append((args, kwargs)))
    monkeypatch.setattr(ai, "_finish", lambda *args, **kwargs: finished.append((args, kwargs)))
    model_calls = []

    def model(*args, **kwargs):
        model_calls.append((args, kwargs))
        return {"text": json.dumps({"candidates": [candidate()]}), "response_id": "response", "usage": {}}

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
    return SimpleNamespace(
        job=job,
        config=config,
        evidence=pack,
        calls=calls,
        ingested=ingested,
        finished=finished,
        model_calls=model_calls,
        settings=SimpleNamespace(token_encryption_key=b"x" * 32),
    )


def test_runner_only_ingests_pending_source_with_independent_feature(run_harness):
    h = run_harness
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.finished and len(h.ingested) == 1
    args, data = h.ingested[0]
    assert args == (h.job["requested_by_authorization_id"],)
    assert data["source_type"] == "ai" and data["trusted_identity"] is False
    assert data["responsible_id"] == h.job["responsible_authorization_id"]
    assert "ai_review" in data["uncertainties"] and data["handle"] == "@alice"
    assert data["source_data"]["evidence"] == h.evidence["refs"]
    assert h.model_calls[0][0][0]["model"] == "configured-model"
    assert h.model_calls[0][1]["output_format"]["strict"] is True
    writes = " ".join(q for q, _ in h.calls)
    assert "UPDATE contact_roles" not in writes and "INSERT INTO contact_assignments" not in writes
    assert "SET status = 'succeeded'" in writes


def test_refusal_invalid_output_does_not_become_success_or_candidate(monkeypatch, run_harness):
    h = run_harness
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "I cannot help", "usage": {}})
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.ingested and h.finished[0][0][1] == "invalid"


def test_valid_empty_result_is_distinct_from_error(monkeypatch, run_harness):
    h = run_harness
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": '{"candidates": []}'})
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.ingested and not h.finished
    updates = [(q, p) for q, p in h.calls if "SET status = 'succeeded'" in q]
    assert updates[0][1][0] == 0


def test_revocation_before_model_call_sends_nothing(monkeypatch, run_harness):
    h = run_harness

    def revoked(*_):
        raise PermissionError("revoked")

    monkeypatch.setattr(ai.creator_workflow, "validate_evidence", revoked)
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.model_calls and not h.ingested
    assert h.finished[0][0][1] == "cancelled"


def test_edited_evidence_during_model_call_cannot_commit(monkeypatch, run_harness):
    h = run_harness
    loads = []

    def load(*_):
        loads.append(True)
        if len(loads) > 1:
            raise ai.ScanInputError("edited")
        return h.evidence

    monkeypatch.setattr(ai, "_load_evidence", load)
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert len(h.model_calls) == 1 and not h.ingested
    assert h.finished[0][0][1] == "cancelled"


def test_feature_disabled_never_calls_old_or_new_models(run_harness):
    h = run_harness
    h.config["enabled"] = False
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.ingested and not h.model_calls
    assert h.finished[0][0][1] == "cancelled"


@pytest.mark.parametrize("attempts,expected", [(1, "queued"), (3, "failed")])
def test_rate_limit_has_a_finite_retry_budget(monkeypatch, run_harness, attempts, expected):
    h = run_harness
    h.job["attempts"] = attempts
    throttled = []

    def fail(*_a, **_k):
        raise ai.contact_assessments.OpenAIRequestError("429", retryable=True, retry_after_seconds=10)

    monkeypatch.setattr(ai.contact_assessments, "call_openai", fail)
    monkeypatch.setattr(ai.ai_dispatch, "throttle", lambda value: throttled.append(value))
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.ingested and h.finished[0][0][1] == expected
    assert bool(throttled) == (expected == "queued")


def test_same_evidence_never_reopens_a_denied_source(monkeypatch, run_harness):
    h = run_harness
    original = ai.connection
    base = original().__enter__()
    execute = base.execute

    def existing(query, params=None):
        return Result({"exists": 1}) if "SELECT 1 FROM creator_workflow_sources" in query else execute(query, params)

    base.execute = existing
    monkeypatch.setattr(ai, "connection", lambda: nullcontext(base))
    ai.run_claimed_creator_scan(h.job, h.settings)
    assert not h.ingested and not h.finished


def test_queue_rejects_unbounded_scan_arguments_without_reading_database():
    for args in ({"chat_limit": 101}, {"history_days": 91}, {"history_days": 0}, {"chat_limit": True}):
        with pytest.raises(ai.ScanInputError):
            ai.queue_scan(uuid4(), uuid4(), **args)


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
    monkeypatch.setattr(ai, "_queue_continuations", lambda: None)
    monkeypatch.setattr(ai, "_enabled", lambda _: True)
    monkeypatch.setattr(ai.ai_dispatch, "can_claim", lambda _: False)
    assert ai.claim_next_creator_scan() is None
    assert not any("FOR UPDATE OF t SKIP LOCKED" in q for q in calls)


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
            if int(path.name[:3]) <= 29:
                conn.execute(path.read_text())
        monkeypatch.setattr(ai, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(ai.creator_workflow, "connection", lambda: nullcontext(conn))
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def real_scan(pg):
    ids = {name: uuid4() for name in ("cm", "owner", "person", "other")}
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
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
        (1, "person", "My creator account is @alice"),
        (2, "cm", "I am your CM and will manage your campaign"),
    ):
        pg.execute(
            """INSERT INTO chat_messages
                      (chat_id,message_id,sender_open_id,message_type,body_ciphertext,message_create_time)
                      VALUES('chat',%s,%s,'text',%s,NOW()+%s*INTERVAL '1 second')""",
            (str(number), "ou_" + name, cipher.encrypt(json.dumps({"text": text})), number),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,'chat',%s)",
            (ids["cm"], str(number)),
        )
    key = ai.api_key_cipher(settings.token_encryption_key).encrypt("test-key-not-real")
    for feature in ("creator_workflow", "contact_assessment", "contact_assignment"):
        pg.execute(
            """INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix)
                      VALUES(%s,'openai','test-project','existing-model',%s,%s,'real')""",
            (feature, feature == ai.FEATURE, key),
        )
    return ids, settings


def test_real_queue_claim_model_candidate_and_unchanged_incremental(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    calls = []

    def model(config, key, pack, cm_id, **kwargs):
        calls.append(pack)
        assert cm_id == ids["cm"] and config["model"] == "existing-model"
        assert [u["name"] for u in pack["users"]] == ["person"]
        assert pack["messages"][1]["speaker"] == "CM1"
        return {"text": json.dumps({"candidates": [candidate()]}), "usage": {"input_tokens": 15}}

    monkeypatch.setattr(ai.contact_assessments, "call_openai", model)
    run = ai.queue_scan(ids["owner"], ids["cm"])
    assert run["total_chats"] == 1 and run["status"] == "queued"
    assert ai.queue_scan(ids["owner"], ids["cm"])["id"] == run["id"]
    job = ai.claim_next_creator_scan()
    assert job["attempts"] == 1 and job["chat_id"] == "chat"
    ai.run_claimed_creator_scan(job, settings)
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["status"] == "succeeded", status
    assert status["candidate_count"] == 1 and status["counts"] == {"succeeded": 1}
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["status"] == "pending" and row["responsible_authorization_id"] == ids["cm"]
    assert row["contact_id"] == ids["person"] and row["normalized_handle"] == "alice"
    assert "ai_review" in row["uncertainties"]
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_account_assignments").fetchone()["n"] == 0
    assert pg.execute("SELECT COUNT(*) AS n FROM contact_roles").fetchone()["n"] == 0
    assert pg.execute("SELECT COUNT(*) AS n FROM contact_assignments").fetchone()["n"] == 0
    assert (
        pg.execute(
            "SELECT COUNT(*) AS n FROM ai_feature_configs WHERE feature <> %s AND enabled", (ai.FEATURE,)
        ).fetchone()["n"]
        == 0
    )
    again = ai.queue_scan(ids["owner"], ids["cm"])
    assert again["total_chats"] == 0 and len(calls) == 1
    assert ai.claim_next_creator_scan() is None


def test_real_missing_handle_and_user_survive_in_pending_pool(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    output = candidate(
        handle=None, user=None, handle_source="missing", handle_refs=[], unknowns=["需要补充 handle 和具体用户"]
    )
    monkeypatch.setattr(
        ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": json.dumps({"candidates": [output]})}
    )
    ai.queue_scan(ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row and row["status"] == "pending" and row["normalized_handle"] is None and row["contact_id"] is None
    assert pg.execute("SELECT COUNT(*) AS n FROM social_accounts").fetchone()["n"] == 0


def test_real_revoke_between_queue_and_execution_cancels_without_model(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    ai.queue_scan(ids["owner"], ids["cm"])
    job = ai.claim_next_creator_scan()
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (ids["cm"],))

    def never(*_a, **_k):
        pytest.fail("revoked content reached OpenAI")

    monkeypatch.setattr(ai.contact_assessments, "call_openai", never)
    ai.run_claimed_creator_scan(job, settings)
    assert ai.scan_status(ids["owner"], ids["cm"])["status"] == "cancelled"
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_sources").fetchone()["n"] == 0


def test_real_unknown_cm_goes_to_admin_without_identity_grant(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    output = candidate(responsible=None, responsibility_refs=[], unknowns=["负责人不明"])
    monkeypatch.setattr(
        ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": json.dumps({"candidates": [output]})}
    )
    ai.queue_scan(ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row and row["status"] == "pending" and row["responsible_authorization_id"] is None
    assert row["contact_id"] is None


def test_real_failed_output_is_retryable_only_by_explicit_request(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "refusal"})
    ai.queue_scan(ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    assert ai.scan_status(ids["owner"], ids["cm"])["status"] == "failed"
    assert ai.queue_scan(ids["owner"], ids["cm"])["total_chats"] == 0
    assert ai.queue_scan(ids["owner"], ids["cm"], retry_failed=True)["total_chats"] == 1
