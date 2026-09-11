from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from uuid import uuid4

import pytest
import test_creator_workflow_ai as creator_tests

from app import contact_assessments
from app.crypto import message_body_cipher

# Reuse the existing isolated PostgreSQL setup; no new service or live API.
pg = creator_tests.pg
real_scan = creator_tests.real_scan


def _valid_result(*, evidence_ref: str = "M1") -> str:
    return json.dumps(
        {
            "roles": [
                {
                    "role": "creator",
                    "confidence": 0.86,
                    "basis": "direct_interaction",
                    "evidence_refs": [evidence_ref],
                }
            ]
        }
    )


def test_model_result_rejects_an_invented_message_reference() -> None:
    with pytest.raises(ValueError):
        contact_assessments._validate_model_result(
            _valid_result(evidence_ref="M999"),
            {"M1"},
        )


def test_model_result_keeps_a_valid_role() -> None:
    result = contact_assessments._validate_model_result(
        json.dumps(
            {
                "roles": [
                    {
                        "role": "creator",
                        "confidence": 0.95,
                        "basis": "self_declaration",
                        "evidence_refs": ["M1"],
                    }
                ]
            }
        ),
        {"M1"},
    )

    assert result == {
        "roles": [{"role": "creator", "confidence": 0.95, "basis": "self_declaration", "evidence_refs": ["M1"]}]
    }


def test_model_result_rejects_insufficient_mixed_with_a_business_role() -> None:
    payload = json.loads(_valid_result())
    payload["roles"].append(
        {"role": "insufficient", "confidence": 0.2, "basis": "weak_context", "evidence_refs": ["M1"]}
    )
    with pytest.raises(ValueError):
        contact_assessments._validate_model_result(
            json.dumps(payload),
            {"M1"},
        )


def test_evidence_uses_readable_text_not_lark_body_json() -> None:
    assert (
        contact_assessments._readable_body('{"content":[[{"text":"hello"},{"text":" world"}]]}', "post")
        == "hello\n world"
    )


def test_responses_request_is_nonpersistent_and_uses_configured_model(monkeypatch) -> None:
    captured = {}

    class Response:
        status_code = 200
        headers = {
            "x-request-id": "req_test",
            "x-ratelimit-remaining-requests": "497",
            "x-ratelimit-remaining-tokens": "499000",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-reset-tokens": "6m",
        }

        @staticmethod
        def json():
            return {
                "id": "resp_test",
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": _valid_result()}]}],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            }

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(contact_assessments.httpx, "post", post)
    clock = iter((1.0, 1.125))
    monkeypatch.setattr(contact_assessments.time, "perf_counter", lambda: next(clock))
    result = contact_assessments._call_openai(
        {"model": "gpt-5.6-luna", "project_id": "proj_test"},
        "sk-test-only",
        {"subject": {"alias": "S1"}, "messages": []},
        uuid4(),
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["json"]["model"] == "gpt-5.6-luna"
    assert captured["json"]["store"] is False
    assert captured["json"]["reasoning"] == {"effort": "low"}
    assert captured["json"]["max_output_tokens"] == 500
    assert captured["headers"]["OpenAI-Project"] == "proj_test"
    schema = captured["json"]["text"]["format"]["schema"]
    assert "summary" not in schema["properties"]
    assert "reason" not in schema["properties"]["roles"]["items"]["properties"]
    evidence_refs = schema["properties"]["roles"]["items"]["properties"]["evidence_refs"]
    assert evidence_refs["minItems"] == 1
    assert evidence_refs["maxItems"] == 16
    assert schema["properties"]["roles"]["maxItems"] == 1
    assert "organization_candidates" not in schema["properties"]
    assert "account_relations" not in schema["properties"]
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 8}
    assert result["telemetry"] == {
        "openai_request_id": "req_test",
        "response_latency_ms": 125,
        "rate_limit_remaining_requests": 497,
        "rate_limit_remaining_tokens": 499000,
        "rate_limit_reset_requests": "1s",
        "rate_limit_reset_tokens": "6m",
    }


def test_responses_429_keeps_safe_retry_metadata(monkeypatch) -> None:
    class Response:
        status_code = 429
        headers = {"retry-after": "7", "x-request-id": "req_limited", "x-ratelimit-reset-requests": "7s"}

        @staticmethod
        def json():
            return {"error": {"code": "rate_limit_exceeded"}}

    monkeypatch.setattr(contact_assessments.httpx, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(contact_assessments.OpenAIRequestError) as raised:
        contact_assessments._call_openai(
            {"model": "gpt-5.6-luna", "project_id": "proj_test"},
            "sk-test-only",
            {"messages": []},
            uuid4(),
        )

    assert raised.value.retry_after_seconds == 7
    assert raised.value.retryable
    assert not raised.value.is_quota
    assert raised.value.telemetry["openai_request_id"] == "req_limited"


def test_responses_quota_429_is_not_treated_as_a_temporary_limit(monkeypatch) -> None:
    class Response:
        status_code = 429
        headers = {"x-request-id": "req_quota"}

        @staticmethod
        def json():
            return {"error": {"code": "insufficient_quota"}}

    monkeypatch.setattr(contact_assessments.httpx, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(contact_assessments.OpenAIRequestError) as raised:
        contact_assessments._call_openai({"model": "gpt-5.6-luna"}, "sk-test-only", {"messages": []}, uuid4())

    assert raised.value.is_quota


def test_retry_after_accepts_an_http_date(monkeypatch) -> None:
    class Response:
        headers = {"retry-after": "Thu, 01 Jan 1970 00:00:07 GMT"}

    monkeypatch.setattr(contact_assessments.time, "time", lambda: 0.0)

    assert contact_assessments._retry_after_header(Response()) == 7


def test_retry_after_rejects_a_non_finite_provider_value() -> None:
    class Response:
        headers = {"retry-after": "NaN"}

    assert contact_assessments._retry_after_header(Response()) is None


def test_non_429_http_error_keeps_safe_telemetry_without_a_blind_retry(monkeypatch) -> None:
    class Response:
        status_code = 500
        headers = {"x-request-id": "req_server_error"}

        @staticmethod
        def json():
            return {"error": {"message": "untrusted provider text"}}

    monkeypatch.setattr(contact_assessments.httpx, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(contact_assessments.OpenAIRequestError) as raised:
        contact_assessments._call_openai({"model": "gpt-5.6-luna"}, "sk-test-only", {"messages": []}, uuid4())

    assert not raised.value.retryable
    assert raised.value.telemetry["openai_request_id"] == "req_server_error"


@pytest.fixture
def people(pg, real_scan, monkeypatch):
    ids, settings = real_scan
    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(contact_assessments, "get_settings", lambda: settings)

    def raise_error(*_args, **_kwargs):
        raise sys.exception()

    monkeypatch.setattr(contact_assessments.LOGGER, "warning", raise_error)
    pg.execute("UPDATE ai_feature_configs SET enabled=(feature='contact_assessment')")
    return ids, settings


def _person(pg, name, *, role=None, source="manual"):
    contact_id = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact_id, name))
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) "
        "VALUES(%s,%s,%s,'known_external')",
        ("ou_" + name, contact_id, name),
    )
    if role:
        pg.execute(
            "INSERT INTO contact_roles(contact_id,role,status,source,is_primary) VALUES(%s,%s,'confirmed',%s,TRUE)",
            (contact_id, role, source),
        )
    return contact_id


def _message(pg, settings, cm, message_id, speaker, text, *, days=0):
    cipher = message_body_cipher(settings.token_encryption_key)
    pg.execute(
        "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_type,body_ciphertext,message_create_time)"
        " VALUES('chat',%s,%s,'text',%s,NOW()-(%s*INTERVAL '1 day'))",
        (message_id, speaker, cipher.encrypt(json.dumps({"text": text})), days),
    )
    pg.execute(
        "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,'chat',%s)",
        (cm, message_id),
    )


def _response(role="creator", confidence=0.99, refs=("M1",)):
    return {
        "text": json.dumps(
            {
                "roles": [
                    {
                        "role": role,
                        "confidence": confidence,
                        "basis": "self_declaration",
                        "evidence_refs": list(refs),
                    }
                ]
            }
        ),
        "response_id": "test-only",
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _job(pg, person):
    pg.execute(
        "UPDATE contact_assessments SET scheduled_at=NOW()+INTERVAL '1 hour' WHERE status='queued' AND contact_id<>%s",
        (person,),
    )
    pg.execute("UPDATE contact_assessments SET scheduled_at=NOW() WHERE contact_id=%s", (person,))
    return contact_assessments._claim_next()


def test_all_people_once_own_history_only_six_classes_and_source_data_preserved(pg, people, monkeypatch):
    ids, settings = people
    pg.execute("DELETE FROM authorization_message_observations WHERE message_id='2'")
    pg.execute("DELETE FROM chat_messages WHERE message_id='2'")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat')", (ids["owner"],))
    pg.execute(
        "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,'chat','1')",
        (ids["owner"],),
    )
    for index in range(25):
        _message(
            pg,
            settings,
            ids["cm"],
            f"h{index}",
            "ou_person",
            f"I create my own LIVE videos for brands. person test@example.com +447777777777 item {index}",
            days=index * 30,
        )
    pg.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) VALUES(%s,%s,%s,'manual')",
        (uuid4(), ids["person"], ids["cm"]),
    )
    before = {
        table: pg.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in ("contacts", "lark_identities", "chat_messages", "contact_assignments")
    }
    evidence = contact_assessments._build_evidence(ids["person"], settings)
    assert len(evidence["refs"]) == 16 and evidence["char_count"] <= 8000
    assert len({(r["chat_id"], r["message_id"]) for r in evidence["refs"]}) == 16
    assert any(f["row"]["message_id"] == "h24" for f in evidence["fragments"])
    sent = json.dumps(evidence["pack"])
    assert "ou_person" not in sent and "test@example.com" not in sent and "+447777777777" not in sent
    assert "chat_name" not in sent and "tenant_classification" not in sent
    assert all(f["row"]["sender_open_id"] == "ou_person" for f in evidence["fragments"])
    assert contact_assessments.queue_eligible_contact_assessments() == 3
    pg.execute("UPDATE ai_feature_configs SET automatic_queue_checked_at='-infinity'")
    assert contact_assessments.queue_eligible_contact_assessments() == 0
    called = []
    monkeypatch.setattr(contact_assessments, "_call_openai", lambda *_args: called.append(_args[2]) or _response())
    job = _job(pg, ids["person"])
    contact_assessments._run(job, settings)
    row = pg.execute("SELECT * FROM contact_assessments WHERE id=%s", (job["id"],)).fetchone()
    assert row["status"] == "succeeded" and row["outcome"] == "auto_confirmed"
    assert row["result"]["reason_code"] == "classified" and row["evidence_count"] == 16
    assert pg.execute("SELECT role,source FROM contact_roles WHERE contact_id=%s", (ids["person"],)).fetchone() == {
        "role": "creator",
        "source": "ai_auto_confirmed",
    }
    assert len(called) == 1
    assert {table: pg.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in before} == before
    assert contact_assessments.assessment_detail(job["id"])["can_confirm"]
    contact_assessments.confirm_assessment_role(ids["owner"], job["id"], "creator")
    # A superseded successful result is never a valid manual-confirmation link.
    pg.execute(
        "INSERT INTO contact_assessments(id,contact_id,policy_version,created_at) "
        "VALUES(%s,%s,%s,NOW()+INTERVAL '1 second')",
        (uuid4(), ids["person"], contact_assessments.POLICY_VERSION),
    )
    assert not contact_assessments.assessment_detail(job["id"])["can_confirm"]
    with pytest.raises(contact_assessments.ContactAssessmentInputError):
        contact_assessments.confirm_assessment_role(ids["owner"], job["id"], "creator")
    for role in contact_assessments.FACT_ROLES:
        assert contact_assessments._validate_model_result(_response(role)["text"], {"M1"})["roles"][0]["role"] == role
    invalid = json.loads(_response()["text"])
    invalid["raw_quote"] = "PII should never persist"
    with pytest.raises(ValueError):
        contact_assessments._validate_model_result(json.dumps(invalid), {"M1"})


def test_no_material_manual_internal_and_low_confidence_do_not_invent_classification(pg, people, monkeypatch):
    ids, settings = people
    manual = _person(pg, "manual", role="merchant_contact")
    hello = _person(pg, "hello")
    _message(pg, settings, ids["cm"], "hello", "ou_hello", "Thanks! 👍")
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
        "VALUES(%s,'creator','confirmed','ai_auto_confirmed',TRUE)",
        (ids["person"],),
    )
    contact_assessments.queue_eligible_contact_assessments()
    called = []
    monkeypatch.setattr(
        contact_assessments, "_call_openai", lambda *_args: called.append(True) or _response(confidence=0.94)
    )
    for person, reason in (
        (ids["cm"], "verified_internal"),
        (manual, "protected_manual"),
        (ids["other"], "no_readable_messages"),
        (hello, "no_substantive_messages"),
        (ids["person"], "low_confidence"),
    ):
        job = _job(pg, person)
        contact_assessments._run(job, settings)
        row = pg.execute("SELECT * FROM contact_assessments WHERE id=%s", (job["id"],)).fetchone()
        assert row["status"] == "succeeded" and row["result"]["reason_code"] == reason
    assert len(called) == 1
    assert pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s", (ids["person"],)).fetchone() is None
    assert (
        pg.execute("SELECT source FROM contact_roles WHERE contact_id=%s", (manual,)).fetchone()["source"] == "manual"
    )
    assert not any(
        "confidence" in r
        for r in pg.execute("SELECT result FROM contact_assessments WHERE contact_id=%s", (manual,)).fetchone()[
            "result"
        ]["roles"]
    )
    pg.execute("UPDATE ai_feature_configs SET automatic_queue_checked_at='-infinity'")
    assert contact_assessments.queue_eligible_contact_assessments() == 0
    # An explicit human rejection outranks directory membership, without changing tenant data.
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'internal_colleague','rejected','manual')",
        (ids["cm"],),
    )
    contact_assessments.requeue_contact_assessment(ids["cm"])
    internal_job = _job(pg, ids["cm"])
    contact_assessments._run(internal_job, settings)
    protected = pg.execute("SELECT result FROM contact_assessments WHERE id=%s", (internal_job["id"],)).fetchone()[
        "result"
    ]
    assert protected == {"roles": [], "reason_code": "protected_manual"}
    # An obsolete automatic tenant primary must not collide with the current high-confidence AI class.
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
        "VALUES(%s,'internal_colleague','confirmed','lark_internal_tenant',TRUE)",
        (ids["person"],),
    )
    contact_assessments.requeue_contact_assessment(ids["person"])
    monkeypatch.setattr(contact_assessments, "_call_openai", lambda *_args: _response())
    contact_assessments._run(_job(pg, ids["person"]), settings)
    assert (
        pg.execute("SELECT role FROM contact_roles WHERE contact_id=%s AND is_primary", (ids["person"],)).fetchone()[
            "role"
        ]
        == "creator"
    )


@pytest.mark.parametrize("change", ["body", "author", "scope", "partial_ref", "disable", "config"])
def test_inflight_evidence_or_configuration_change_cannot_save_or_manually_confirm(pg, people, monkeypatch, change):
    ids, settings = people
    _message(pg, settings, ids["cm"], "extra", "ou_person", "I produce product videos for my followers.")
    contact_assessments.queue_eligible_contact_assessments()
    job = _job(pg, ids["person"])

    def respond(*_args):
        if change == "body":
            pg.execute(
                "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='extra'",
                (message_body_cipher(settings.token_encryption_key).encrypt('{"text":"changed"}'),),
            )
        elif change == "author":
            pg.execute("UPDATE chat_messages SET sender_open_id='ou_other' WHERE message_id='extra'")
        elif change == "scope":
            pg.execute("UPDATE authorization_chats SET sync_enabled=FALSE")
        elif change == "partial_ref":
            pg.execute("DELETE FROM authorization_message_observations WHERE message_id='extra'")
        elif change == "disable":
            pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature='contact_assessment'")
        else:
            pg.execute("UPDATE ai_feature_configs SET model='changed' WHERE feature='contact_assessment'")
        return _response()

    monkeypatch.setattr(contact_assessments, "_call_openai", respond)
    contact_assessments._run(job, settings)
    row = pg.execute("SELECT * FROM contact_assessments WHERE id=%s", (job["id"],)).fetchone()
    assert row["status"] == "cancelled"
    assert pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s", (ids["person"],)).fetchone() is None
    with pytest.raises(contact_assessments.ContactAssessmentInputError):
        contact_assessments.confirm_assessment_role(ids["owner"], job["id"], "creator")


def test_retry_budget_stale_claim_and_legacy_manual_link_are_fenced(pg, people, monkeypatch):
    ids, settings = people
    legacy = uuid4()
    pg.execute(
        "INSERT INTO contact_assessments(id,contact_id,status,result) VALUES(%s,%s,'succeeded',%s)",
        (legacy, ids["person"], _response()["text"]),
    )
    with pytest.raises(contact_assessments.ContactAssessmentInputError):
        contact_assessments.confirm_assessment_role(ids["owner"], legacy, "creator")
    contact_assessments.queue_eligible_contact_assessments()
    first = _job(pg, ids["person"])
    attempts = []

    def limited(*_args):
        attempts.append(True)
        raise contact_assessments.OpenAIRequestError("safe", retryable=True, retry_after_seconds=0)

    monkeypatch.setattr(contact_assessments, "_call_openai", limited)
    monkeypatch.setattr(contact_assessments.ai_dispatch, "throttle", lambda *_args: None)
    contact_assessments._run(first, settings)
    second = _job(pg, ids["person"])
    assert second["id"] == first["id"] and second["attempts"] == 2
    contact_assessments._run(first, settings)
    assert len(attempts) == 1
    contact_assessments._run(second, settings)
    assert len(attempts) == 2
    assert (
        pg.execute("SELECT status FROM contact_assessments WHERE id=%s", (first["id"],)).fetchone()["status"]
        == "failed"
    )
    with pytest.raises(contact_assessments.ContactAssessmentInputError, match="预算"):
        contact_assessments.requeue_contact_assessment(ids["person"])
    pg.execute("UPDATE ai_feature_configs SET automatic_queue_checked_at='-infinity'")
    assert contact_assessments.queue_eligible_contact_assessments() == 0


def test_local_save_deadlock_retry_never_repeats_the_paid_request(pg, people, monkeypatch):
    ids, settings = people
    contact_assessments.queue_eligible_contact_assessments()
    job = _job(pg, ids["person"])
    calls, saves = [], []
    original = contact_assessments._finish_with_decision

    def save(*args, **kwargs):
        saves.append(True)
        if len(saves) == 1:
            raise contact_assessments.psycopg.errors.DeadlockDetected()
        return original(*args, **kwargs)

    monkeypatch.setattr(contact_assessments, "_call_openai", lambda *_args: calls.append(True) or _response())
    monkeypatch.setattr(contact_assessments, "_finish_with_decision", save)
    contact_assessments._run(job, settings)
    assert len(calls) == 1 and len(saves) == 2
    row = pg.execute("SELECT status,attempts FROM contact_assessments WHERE id=%s", (job["id"],)).fetchone()
    assert row == {"status": "succeeded", "attempts": 1}
