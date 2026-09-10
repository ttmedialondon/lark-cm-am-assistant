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


def evidence():
    return {
        "pack": {
            "cm": {"id": "CM1", "name": "Remi"},
            "chat_name": "@alice X TT",
            "chat_type": "group",
            "users": [{"id": "U1", "name": "Alice"}],
            "messages": [
                {"ref": "M1", "user": "U1", "text": "My creator account is @alice", "speaker": "external_or_unknown"},
                {
                    "ref": "M2",
                    "user": None,
                    "text": "I am your CM and will manage your campaign",
                    "speaker": "CM1",
                    "reply_to": "M1",
                },
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
        "identity_refs": ["M1"],
        "responsibility_refs": ["M2"],
        "creator_confidence": 0.99,
        "identity_confidence": 0.99,
        "cm_confidence": 0.99,
        "business_topic": "agency",
        "creator_evidence_type": "self_identification",
        "identity_evidence_type": "self_handle",
        "cm_evidence_type": "managing_creator",
        "creator_quote": "My creator account is @alice" if changes.get("creator_refs") != [] else "",
        "identity_quote": "My creator account is @alice",
        "cm_quote": "I am your CM and will manage your campaign" if changes.get("responsibility_refs") != [] else "",
        "creator_type": None,
        "type_refs": [],
        "type_quote": "",
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
    assert not ai._verified_facts(item, evidence())["verified_business_dialogue"]


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


def test_cap_direct_evidence_is_independent_and_does_not_infer_signed_type():
    facts = ai._verified_facts(candidate(), evidence())
    assert facts["cap_policy_version"] == "cap_v1"
    assert facts["business_policy_version"] == "business_v2" and facts["verified_business_dialogue"]
    assert facts["exact_handle_evidence"] and facts["direct_creator_evidence"] and facts["direct_cm_evidence"]
    assert facts["creator_type"] is None
    assert (
        ai._verified_facts(
            candidate(creator_type="cap", type_refs=["M1"], type_quote="My creator account is @alice"), evidence()
        )["creator_type"]
        is None
    )
    assert ai._verified_facts(candidate(relationship="unknown"), evidence()) == facts
    weak = evidence()
    weak["pack"]["messages"][1].pop("reply_to")
    assert not ai._verified_facts(candidate(), weak)["direct_cm_evidence"]
    weak["pack"]["messages"][1]["mentions"] = ["U1"]
    assert ai._verified_facts(candidate(), weak)["direct_cm_evidence"]
    weak["pack"]["messages"][1]["mentions"] = ["other"]
    assert not ai._verified_facts(candidate(), weak)["direct_cm_evidence"]


def test_group_title_fuzzy_handle_and_other_speakers_never_prove_account_identity():
    pack = evidence()
    assert not ai._verified_facts(candidate(handle="@ali"), pack)["exact_handle_evidence"]
    assert not ai._exact_handle("@alice_shop", "alice")
    assert not ai._exact_handle("xalice", "alice")
    assert not ai._exact_handle("@alice-shop", "alice")
    pack["pack"]["messages"][0]["user"] = "other"
    facts = ai._verified_facts(candidate(), pack)
    assert not facts["exact_handle_evidence"] and not facts["direct_creator_evidence"]
    assert not facts["direct_cm_evidence"]


def test_cm_group_handle_statement_cannot_prove_arbitrary_selected_creator_user():
    pack = evidence()
    pack["pack"]["messages"][0].update(user=None, speaker="CM1", mentions=["U1"])
    facts = ai._verified_facts(candidate(), pack)
    assert not facts["exact_handle_evidence"] and not facts["direct_creator_evidence"]


def test_real_business_dialogue_needs_no_handle_or_cap_but_group_membership_is_not_dialogue():
    pack = evidence()
    creator_quote = "Could you arrange samples for my next LIVE?"
    cm_quote = "I have requested those samples for your LIVE."
    pack["pack"]["messages"][0]["text"] = creator_quote
    pack["pack"]["messages"][1]["text"] = cm_quote
    item = candidate(handle=None, handle_source="missing", handle_refs=[], identity_refs=[],
                     identity_quote="", identity_confidence=0, identity_evidence_type="unknown",
                     creator_quote=creator_quote, cm_quote=cm_quote, business_topic="samples")
    assert ai._validate(json.dumps({"candidates": [item]}), pack) == [item]
    facts = ai._verified_facts(item, pack)
    assert facts["verified_business_dialogue"] and not facts["exact_handle_evidence"]
    assert facts["creator_type"] is None
    pack["pack"]["messages"][1].pop("reply_to")
    assert not ai._verified_facts(item, pack)["verified_business_dialogue"]
    pack["pack"]["messages"][0]["mentions"] = ["CM1"]
    pack["pack"]["messages"][1]["mentions"] = ["another-user"]
    assert not ai._verified_facts(item, pack)["verified_business_dialogue"]
    pack["pack"]["messages"][0].pop("mentions")
    pack["pack"]["messages"][1].pop("mentions")
    pack["pack"].update(chat_type="p2p", private_user="U1")
    assert ai._verified_facts(item, pack)["verified_business_dialogue"]
    pack["pack"]["private_user"] = "another-user"
    assert not ai._verified_facts(item, pack)["verified_business_dialogue"]


def test_greeting_exchange_never_becomes_a_creator_even_with_claimed_high_scores():
    pack = evidence()
    pack["pack"]["messages"][0]["text"] = "Hello Remi!"
    pack["pack"]["messages"][1]["text"] = "Good morning Alice!"
    item = candidate(handle=None, handle_source="missing", handle_refs=[], identity_refs=[],
                     identity_quote="", creator_quote="Hello Remi!", cm_quote="Good morning Alice!")
    assert ai._validate(json.dumps({"candidates": [item]}), pack) == []
    assert not ai._verified_facts(item, pack)["verified_business_dialogue"]


@pytest.mark.parametrize(
    "change",
    [
        {"creator_confidence": True},
        {"identity_confidence": float("nan")},
        {"cm_confidence": 1.1},
        {"creator_quote": "made up claim"},
        {"identity_refs": ["missing"]},
    ],
)
def test_confidence_and_ephemeral_direct_quotes_are_validated(change):
    with pytest.raises(ai.InvalidModelOutput):
        ai._validate(json.dumps({"candidates": [candidate(**change)]}), evidence())


def test_plain_text_mentions_resolve_only_metadata_and_remove_link_secrets():
    cipher = SimpleNamespace(
        decrypt=lambda _: json.dumps({"text": "Hi @_user_1 https://tiktok.com/@alice?token=private"})
    )
    row = {
        "message_type": "text",
        "body_ciphertext": "cipher",
        "mentions_json": [{"key": "@_user_1", "id_type": "open_id", "id": "ou_cm", "name": "old"}],
    }
    assert ai._body(cipher, row, {"ou_cm": "Remi"}) == "Hi @Remi https://tiktok.com/@alice?token=[redacted]"


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


def test_runner_passes_verified_business_source_to_workflow_with_independent_feature(run_harness):
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


def test_runner_keeps_unverified_handle_auxiliary_without_blocking_person_evidence(run_harness, monkeypatch):
    h = run_harness
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {
        "text": json.dumps({"candidates": [candidate(identity_confidence=0.2)]})
    })
    ai.run_claimed_creator_scan(h.job, h.settings)
    data = h.ingested[0][1]
    assert data["handle"] is None and data["creator_type"] is None
    assert data["source_data"]["handle_raw"] == "@alice"
    assert data["source_data"]["verified_business_dialogue"] is True
    assert data["source_data"]["business_policy_version"] == "business_v2"
    assert data["source_data"]["unknowns"] == []


def test_source_metadata_never_persists_quotes_or_model_private_prose(monkeypatch, run_harness):
    h = run_harness
    monkeypatch.setattr(
        ai.contact_assessments,
        "call_openai",
        lambda *_a, **_k: {
            "text": json.dumps(
                {
                    "candidates": [
                        candidate(reason="private customer details", unknowns=["private quote in doubt"]),
                    ]
                }
            )
        },
    )
    ai.run_claimed_creator_scan(h.job, h.settings)
    source = h.ingested[0][1]["source_data"]
    stored = json.dumps(source)
    assert "private customer" not in stored and "private quote" not in stored
    assert "My creator account" not in stored
    assert source["creator_confidence"] == 0.99 and source["cap_policy_version"] == "cap_v1"
    assert "ai_review" not in source["unknowns"]


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


@pytest.mark.parametrize("excel_first", [True, False])
def test_verified_ai_and_cm_cap_excel_auto_confirm_in_either_order(pg, real_scan, monkeypatch, excel_first):
    ids, settings = real_scan

    def upload():
        return ai.creator_workflow.ingest_source(
            ids["cm"],
            source_type="excel",
            source_key="my-cap-row",
            source_data={"cap_policy_version": "cap_v1", "creator_type": "cap", "type_evidence": "cm_excel_upload"},
            responsible_id=ids["cm"],
            handle="alice",
            creator_type="cap",
            conn=pg,
        )

    if excel_first:
        assert upload()["status"] == "pending"
    monkeypatch.setattr(
        ai.contact_assessments,
        "call_openai",
        lambda *_a, **_k: {"text": json.dumps({"candidates": [candidate(relationship="unknown")]})},
    )
    start_scan(pg, ids["cm"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    if not excel_first:
        assert pg.execute("SELECT status,creator_type FROM creator_workflow_candidates").fetchone() == {
            "status": "confirmed",
            "creator_type": None,
        }
        upload()
    row = pg.execute(
        "SELECT status,creator_type,relationship,uncertainties FROM creator_workflow_candidates"
    ).fetchone()
    assert row == {"status": "confirmed", "creator_type": "cap", "relationship": "associated", "uncertainties": []}
    assignment = pg.execute("SELECT * FROM creator_account_assignments").fetchone()
    assert assignment["responsible_authorization_id"] == ids["cm"] and assignment["creator_type"] == "cap"


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
        monkeypatch.setattr(ai.creator_workflow, "connection", lambda: nullcontext(conn))
        monkeypatch.setattr(onboarding, "connection", lambda: nullcontext(conn))
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
    ai.queue_scan(actor, cm)
    advance_scan(pg)
    return ai.scan_status(actor, cm)


def advance_scan(pg):
    pg.execute("UPDATE creator_workflow_onboarding SET next_check_at=NOW() WHERE status='scanning'")
    onboarding.advance_initial_scans()


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
    run = start_scan(pg, ids["owner"], ids["cm"])
    assert run["total_chats"] == 1 and run["status"] == "queued"
    assert ai.queue_scan(ids["owner"], ids["cm"])["run_id"] == run["id"]
    job = ai.claim_next_creator_scan()
    assert job["attempts"] == 1 and job["chat_id"] == "chat"
    ai.run_claimed_creator_scan(job, settings)
    status = ai.scan_status(ids["owner"], ids["cm"])
    assert status["status"] == "succeeded", status
    assert status["candidate_count"] == 1 and status["counts"] == {"succeeded": 1}
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row["status"] == "confirmed" and row["responsible_authorization_id"] == ids["cm"]
    assert row["contact_id"] == ids["person"] and row["normalized_handle"] == "alice"
    assert not row["uncertainties"]
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1
    assert pg.execute("SELECT COUNT(*) AS n FROM contact_roles").fetchone()["n"] == 0
    assert pg.execute("SELECT COUNT(*) AS n FROM contact_assignments").fetchone()["n"] == 0
    assert (
        pg.execute(
            "SELECT COUNT(*) AS n FROM ai_feature_configs WHERE feature <> %s AND enabled", (ai.FEATURE,)
        ).fetchone()["n"]
        == 0
    )
    advance_scan(pg)
    again = start_scan(pg, ids["owner"], ids["cm"])
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
    start_scan(pg, ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row and row["status"] == "pending" and row["normalized_handle"] is None and row["contact_id"] is None
    assert pg.execute("SELECT COUNT(*) AS n FROM social_accounts").fetchone()["n"] == 0


def test_real_revoke_between_queue_and_execution_cancels_without_model(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    start_scan(pg, ids["owner"], ids["cm"])
    job = ai.claim_next_creator_scan()
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (ids["cm"],))

    def never(*_a, **_k):
        pytest.fail("revoked content reached OpenAI")

    monkeypatch.setattr(ai.contact_assessments, "call_openai", never)
    ai.run_claimed_creator_scan(job, settings)
    assert ai.scan_status(ids["owner"], ids["cm"])["status"] == "cancelled"
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_workflow_sources").fetchone()["n"] == 0


def test_real_unknown_responsibility_stays_in_scanned_cm_review_inbox_without_assignment(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    output = candidate(responsible=None, responsibility_refs=[], unknowns=["负责人不明"])
    monkeypatch.setattr(
        ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": json.dumps({"candidates": [output]})}
    )
    start_scan(pg, ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    row = pg.execute("SELECT * FROM creator_workflow_candidates").fetchone()
    assert row and row["status"] == "pending" and row["responsible_authorization_id"] == ids["cm"]
    assert row["contact_id"] == ids["person"] and "ownership_uncertain" in row["uncertainties"]
    source = pg.execute("SELECT source_data FROM creator_workflow_sources").fetchone()["source_data"]
    assert not source["responsibility_confirmed"] and not source["direct_cm_evidence"]
    assert not pg.execute("SELECT 1 FROM creator_account_assignments").fetchone()


def test_real_failed_output_is_retryable_only_by_explicit_request(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    monkeypatch.setattr(ai.contact_assessments, "call_openai", lambda *_a, **_k: {"text": "refusal"})
    start_scan(pg, ids["owner"], ids["cm"])
    ai.run_claimed_creator_scan(ai.claim_next_creator_scan(), settings)
    assert ai.scan_status(ids["owner"], ids["cm"])["status"] == "failed"
    advance_scan(pg)
    assert onboarding.get_status(pg, ids["cm"])["status"] == "failed"
    assert ai.claim_next_creator_scan() is None
    assert ai.queue_scan(ids["owner"], ids["cm"], retry_failed=True)["onboarding"]["status"] == "scanning"
    assert ai.claim_next_creator_scan()["attempts"] == 2
