from __future__ import annotations

import json
from contextlib import nullcontext
from uuid import uuid4

import pytest

from app import contact_assessments


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
        "roles": [
            {"role": "creator", "confidence": 0.95, "basis": "self_declaration", "evidence_refs": ["M1"]}
        ]
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


def test_assessment_detail_marks_a_readable_evidence_item_available(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query, _params=None):
            if "FROM contact_assessments assessment" in query:
                return Result(
                    {
                        "id": assessment_id,
                        "contact_id": contact_id,
                        "display_name": "Creator",
                        "open_id": "ou_contact",
                        "status": "succeeded",
                        "model": "gpt-5.6-luna",
                        "result": {"roles": []},
                        "evidence_count": 3,
                    }
                )
            if "FROM contact_assessment_refs" in query:
                return Result(
                    rows=[
                        {
                            "label": "M1",
                            "relation": "anchor",
                            "message_type": "post",
                            "body_ciphertext": "sealed",
                            "chat_name": "Creator group",
                            "sender_display_name": "Creator",
                            "message_create_time": None,
                        },
                        {
                            "label": "M10",
                            "relation": "context",
                            "message_type": "post",
                            "body_ciphertext": "sealed",
                            "chat_name": "Creator group",
                            "sender_display_name": "Creator",
                            "message_create_time": None,
                        },
                        {
                            "label": "M2",
                            "relation": "history",
                            "message_type": "post",
                            "body_ciphertext": "sealed",
                            "chat_name": "Creator group",
                            "sender_display_name": "Creator",
                            "message_create_time": None,
                        },
                    ]
                )
            return Result(rows=[])

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(contact_assessments, "message_body_cipher", lambda _key: object())
    monkeypatch.setattr(
        contact_assessments, "get_settings", lambda: type("Settings", (), {"token_encryption_key": b"x"})()
    )
    monkeypatch.setattr(contact_assessments, "_decrypt_body", lambda _cipher, _body: '{"text":"readable"}')

    detail = contact_assessments.assessment_detail(assessment_id)

    assert detail is not None
    assert [item["label"] for item in detail["evidence"]] == ["M1", "M2", "M10"]
    assert detail["evidence"][0]["content"] == "readable"
    assert all(item["available"] for item in detail["evidence"])
    assert detail["can_confirm"] is True


def test_confirmation_refuses_an_assessment_after_its_evidence_leaves_scope(monkeypatch) -> None:
    assessment_id = uuid4()
    actor_id = uuid4()
    contact_id = uuid4()

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, *_args, **_kwargs):
            raise AssertionError("no contact fact should be written")

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(
        contact_assessments,
        "_successful_assessment",
        lambda *_args: {"contact_id": contact_id, "result": {"roles": [{"role": "creator"}]}},
    )
    monkeypatch.setattr(contact_assessments, "_refs_are_still_authorized_in", lambda *_args: False)

    with pytest.raises(contact_assessments.ContactAssessmentInputError, match="当前授权范围"):
        contact_assessments.confirm_assessment_role(actor_id, assessment_id, "creator")


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
    assert evidence_refs["maxItems"] == 12
    assert schema["properties"]["roles"]["maxItems"] == 3
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
        contact_assessments._call_openai(
            {"model": "gpt-5.6-luna"}, "sk-test-only", {"messages": []}, uuid4()
        )

    assert raised.value.is_quota


def test_retry_after_accepts_an_http_date(monkeypatch) -> None:
    class Response:
        headers = {"retry-after": "Thu, 01 Jan 1970 00:00:07 GMT"}

    monkeypatch.setattr(contact_assessments.time, "time", lambda: 0.0)

    assert contact_assessments._retry_after_header(Response()) == 7


def test_non_429_http_error_keeps_safe_telemetry_without_a_blind_retry(monkeypatch) -> None:
    class Response:
        status_code = 500
        headers = {"x-request-id": "req_server_error"}

        @staticmethod
        def json():
            return {"error": {"message": "untrusted provider text"}}

    monkeypatch.setattr(contact_assessments.httpx, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(contact_assessments.OpenAIRequestError) as raised:
        contact_assessments._call_openai(
            {"model": "gpt-5.6-luna"}, "sk-test-only", {"messages": []}, uuid4()
        )

    assert not raised.value.retryable
    assert raised.value.telemetry["openai_request_id"] == "req_server_error"


def test_context_history_uses_the_anchor_authorization_only() -> None:
    contact_id = uuid4()
    authorization_id = uuid4()

    class Result:
        @staticmethod
        def fetchall():
            return []

    class Connection:
        calls = []

        def execute(self, query, params):
            self.calls.append((" ".join(query.split()), params))
            return Result()

    conn = Connection()
    contact_assessments._recent_subject_history(conn, contact_id, authorization_id)

    query, params = conn.calls[0]
    assert "observation.authorization_id = %s" in query
    assert params == (contact_id, authorization_id, contact_assessments.MAX_SUBJECT_HISTORY_MESSAGES)
    assert "LIMIT %s" in query


def test_context_limits_are_balanced_and_group_name_is_not_repeated() -> None:
    anchor = {
        "authorization_id": uuid4(),
        "chat_id": "oc_test",
        "message_id": "om_anchor",
        "message_create_time": "2026-09-02T10:00:00Z",
        "chat_mode": "group",
        "chat_name": "Creator campaign",
        "external": True,
        "p2p_counterpart_open_id": None,
        "mentions_json": [],
        "sender_tenant_classification": "known_external",
        "message_type": "text",
        "root_id": None,
        "parent_id": None,
        "thread_id": None,
    }

    class Result:
        @staticmethod
        def fetchall():
            return []

    class Connection:
        calls = []

        def execute(self, query, params):
            self.calls.append((" ".join(query.split()), params))
            return Result()

    conn = Connection()
    contact_assessments._conversation_context(conn, anchor)

    before_query, before_params = conn.calls[0]
    after_query, after_params = conn.calls[1]
    assert "message.message_id < %s" in before_query
    assert "message.message_id > %s" in after_query
    assert before_params[-1] == after_params[-1] == contact_assessments.MAX_CONTEXT_MESSAGES_PER_SIDE

    base_fragment = {"label": "M1", "speaker": "S1", "row": anchor, "text": "hello"}
    anchor_payload = contact_assessments._model_fragment({**base_fragment, "relation": "anchor"}, set())
    context_payload = contact_assessments._model_fragment({**base_fragment, "relation": "context"}, set())
    assert anchor_payload["conversation"]["name"] == "Creator campaign"
    assert "speaker_party" not in anchor_payload
    assert "name" not in context_payload["conversation"]


def test_group_interaction_is_strong_only_when_the_other_party_is_internal() -> None:
    row = {
        "sender_open_id": "ou_other",
        "sender_tenant_classification": "known_external",
        "reply_to_open_id": "ou_subject",
        "p2p_counterpart_open_id": None,
        "mentions_json": [],
    }

    assert not contact_assessments._fragment_strength(row, {"ou_subject"})["direct_relation"]
    row["sender_tenant_classification"] = "internal"
    assert contact_assessments._fragment_strength(row, {"ou_subject"})["direct_relation"]


def test_saved_refs_record_the_single_source_authorization(monkeypatch) -> None:
    assessment_id = uuid4()
    source_authorization_id = uuid4()

    class Result:
        pass

    class Connection:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            self.calls.append((" ".join(query.split()), params))
            return Result()

    conn = Connection()
    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(conn))
    contact_assessments._save_refs(
        assessment_id,
        [
            {
                "label": "M1",
                "authorization_id": source_authorization_id,
                "chat_id": "oc_test",
                "message_id": "om_test",
                "relation": "anchor",
            }
        ],
        42,
        source_authorization_id,
    )

    update_params = next(params for query, params in conn.calls if "SET source_authorization_id" in query)
    assert update_params == (source_authorization_id, 1, 42, assessment_id)


def test_auto_decision_confirms_creator(monkeypatch) -> None:
    confirmed: list[str] = []
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda _conn, _contact_id, role: confirmed.append(role) or "confirmed",
    )

    outcome, review_status, roles = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        {"roles": [{"role": "creator", "confidence": 0.9, "basis": "direct_interaction", "evidence_refs": ["M1"]}]},
        0.9,
        {"M1": {"direct_relation": True, "subject_speaker": True}},
    )

    assert (outcome, review_status, roles) == ("auto_confirmed", "not_required", ["creator"])
    assert confirmed == ["creator"]


def test_auto_decision_keeps_any_multiple_role_output_for_review(monkeypatch) -> None:
    confirmed: list[str] = []
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda _conn, _contact_id, role: confirmed.append(role) or "confirmed",
    )

    outcome, review_status, roles = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        {
            "roles": [
                {"role": "creator", "confidence": 0.95, "basis": "direct_interaction", "evidence_refs": ["M1"]},
                {"role": "partner_contact", "confidence": 0.20, "basis": "weak_context", "evidence_refs": ["M1"]},
            ],
        },
        0.9,
        {"M1": {"direct_relation": True, "subject_speaker": True}},
    )

    assert (outcome, review_status, roles) == ("review_required", "pending", [])
    assert confirmed == []


def test_auto_decision_keeps_a_role_below_ninety_percent_for_review(monkeypatch) -> None:
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda *_args: pytest.fail("a role below 90% must not be automatically written"),
    )

    outcome, review_status, roles = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        {"roles": [{"role": "creator", "confidence": 0.899, "basis": "direct_interaction", "evidence_refs": ["M1"]}]},
        0.9,
        {"M1": {"direct_relation": True, "subject_speaker": True}},
    )

    assert (outcome, review_status, roles) == ("review_required", "pending", [])


def test_auto_decision_never_confirms_from_weak_group_context(monkeypatch) -> None:
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda *_args: pytest.fail("weak context must not automatically write a category"),
    )

    outcome, review_status, roles = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        {"roles": [{"role": "creator", "confidence": 0.99, "basis": "weak_context", "evidence_refs": ["M1"]}]},
        0.9,
        {"M1": {"direct_relation": False, "subject_speaker": False}},
    )

    assert (outcome, review_status, roles) == ("review_required", "pending", [])


def test_auto_decision_sends_low_or_ambiguous_roles_to_review(monkeypatch) -> None:
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda *_args: pytest.fail("ambiguous role must not be automatically written"),
    )

    outcome, review_status, roles = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        {
            "roles": [
                {"role": "creator", "confidence": 0.99, "basis": "direct_interaction", "evidence_refs": ["M1"]},
                {"role": "partner_contact", "confidence": 0.91, "basis": "direct_interaction", "evidence_refs": ["M1"]},
            ],
        },
        0.9,
        {"M1": {"direct_relation": True, "subject_speaker": True}},
    )

    assert (outcome, review_status, roles) == ("review_required", "pending", [])


def test_run_skips_every_confirmed_contact_before_loading_ai_or_sending_content(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    finished: list[tuple[object, ...]] = []

    class Result:
        @staticmethod
        def fetchone():
            return {"has_facts": True}

    class Connection:
        @staticmethod
        def execute(query, _params):
            assert "SELECT EXISTS" in query
            return Result()

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(
        contact_assessments,
        "_configured_openai",
        lambda *_args: pytest.fail("confirmed contact must not load or call AI"),
    )
    monkeypatch.setattr(
        contact_assessments,
        "_finish",
        lambda *args, **kwargs: finished.append((*args, kwargs)),
    )

    contact_assessments._run({"id": assessment_id, "contact_id": contact_id}, object())

    assert finished == [
        (
            assessment_id,
            "cancelled",
            "联系人已有已确认身份事实；判定未运行。",
            {"outcome": "no_action"},
        )
    ]


def test_run_without_an_organization_reaches_ai(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    called: list[object] = []

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(contact_assessments, "_contact_has_confirmed_facts", lambda *_args: False)
    monkeypatch.setattr(contact_assessments, "_configured_openai", lambda *_args: ({"model": "luna"}, "sk-test"))
    monkeypatch.setattr(
        contact_assessments,
        "_build_evidence",
        lambda *_args: {
            "fragments": [{}],
            "refs": [],
            "char_count": 0,
            "source_authorization_id": uuid4(),
            "pack": {"subject": {"alias": "S1"}, "messages": []},
            "labels": {"M1"},
            "evidence_strength": {},
        },
    )
    monkeypatch.setattr(contact_assessments, "_save_refs", lambda *_args: None)
    monkeypatch.setattr(contact_assessments, "_refs_are_still_authorized", lambda *_args: True)
    monkeypatch.setattr(contact_assessments, "_call_openai", lambda *_args: {"text": "{}", "usage": {}})
    monkeypatch.setattr(contact_assessments, "_validate_model_result", lambda *_args: {"roles": []})
    monkeypatch.setattr(
        contact_assessments,
        "_finish_with_decision",
        lambda *_args, **_kwargs: called.append("decision") or True,
    )
    monkeypatch.setattr(contact_assessments, "_mark_feature_test", lambda result: called.append(result))

    contact_assessments._run({"id": assessment_id, "contact_id": contact_id}, object())

    assert called == ["decision", "succeeded"]


def test_run_reschedules_a_temporary_429_and_throttles_both_queues(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    throttles: list[float] = []
    rescheduled: list[tuple[object, ...]] = []

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(contact_assessments, "_contact_has_confirmed_facts", lambda *_args: False)
    monkeypatch.setattr(contact_assessments, "_configured_openai", lambda *_args: ({"model": "luna"}, "sk-test"))
    monkeypatch.setattr(
        contact_assessments,
        "_build_evidence",
        lambda *_args: {
            "fragments": [{}], "refs": [], "char_count": 0, "source_authorization_id": uuid4(),
            "pack": {}, "labels": {"M1"}, "evidence_strength": {},
        },
    )
    monkeypatch.setattr(contact_assessments, "_save_refs", lambda *_args: None)
    monkeypatch.setattr(contact_assessments, "_refs_are_still_authorized", lambda *_args: True)
    monkeypatch.setattr(
        contact_assessments,
        "_call_openai",
        lambda *_args: (_ for _ in ()).throw(
                contact_assessments.OpenAIRequestError(
                    "limited", retry_after_seconds=7, retryable=True, telemetry={"openai_request_id": "req_1"}
                )
        ),
    )
    monkeypatch.setattr(contact_assessments.ai_dispatch, "throttle", throttles.append)
    monkeypatch.setattr(contact_assessments, "_reschedule", lambda *args, **kwargs: rescheduled.append((*args, kwargs)))
    monkeypatch.setattr(contact_assessments, "_mark_feature_test", lambda *_args: None)
    monkeypatch.setattr(contact_assessments.random, "uniform", lambda *_args: 0.5)

    contact_assessments._run({"id": assessment_id, "contact_id": contact_id, "attempts": 1}, object())

    assert throttles == [7.5]
    assert rescheduled == [
        (
            assessment_id,
            7.5,
            "OpenAI 请求暂时限速；将在约 8 秒后自动重试。",
            {"telemetry": {"openai_request_id": "req_1"}},
        )
    ]


def test_run_pauses_the_shared_queue_for_a_quota_429(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    paused: list[bool] = []
    rescheduled: list[tuple[object, ...]] = []

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(contact_assessments, "_contact_has_confirmed_facts", lambda *_args: False)
    monkeypatch.setattr(contact_assessments, "_configured_openai", lambda *_args: ({"model": "luna"}, "sk-test"))
    monkeypatch.setattr(
        contact_assessments,
        "_build_evidence",
        lambda *_args: {
            "fragments": [{}], "refs": [], "char_count": 0, "source_authorization_id": uuid4(),
            "pack": {}, "labels": {"M1"}, "evidence_strength": {},
        },
    )
    monkeypatch.setattr(contact_assessments, "_save_refs", lambda *_args: None)
    monkeypatch.setattr(contact_assessments, "_refs_are_still_authorized", lambda *_args: True)
    monkeypatch.setattr(
        contact_assessments,
        "_call_openai",
        lambda *_args: (_ for _ in ()).throw(contact_assessments.OpenAIRequestError("quota", is_quota=True)),
    )
    monkeypatch.setattr(contact_assessments.ai_dispatch, "pause_for_quota", lambda: paused.append(True))
    monkeypatch.setattr(contact_assessments, "_reschedule", lambda *args, **kwargs: rescheduled.append((*args, kwargs)))
    monkeypatch.setattr(contact_assessments, "_mark_feature_test", lambda *_args: None)

    contact_assessments._run({"id": assessment_id, "contact_id": contact_id}, object())

    assert paused == [True]
    assert rescheduled[0][:3] == (
        assessment_id,
        0,
        "OpenAI 项目因配额或账单问题已暂停；请由所有者处理后恢复队列。",
    )


def test_run_rechecks_confirmation_immediately_before_the_model_call(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    checks = iter((False, True))
    finished: list[tuple[object, ...]] = []

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(contact_assessments, "_contact_has_confirmed_facts", lambda *_args: next(checks))
    monkeypatch.setattr(
        contact_assessments,
        "_configured_openai",
        lambda _settings: ({"model": "gpt-5.6-luna"}, "sk-test"),
    )
    monkeypatch.setattr(
        contact_assessments,
        "_build_evidence",
        lambda *_args: {
            "fragments": [{}],
            "refs": [],
            "char_count": 0,
            "source_authorization_id": uuid4(),
            "pack": {},
        },
    )
    monkeypatch.setattr(contact_assessments, "_save_refs", lambda *_args: None)
    monkeypatch.setattr(contact_assessments, "_refs_are_still_authorized", lambda *_args: True)
    monkeypatch.setattr(
        contact_assessments,
        "_call_openai",
        lambda *_args: pytest.fail("a newly confirmed contact must not reach OpenAI"),
    )
    monkeypatch.setattr(
        contact_assessments,
        "_finish",
        lambda *args, **kwargs: finished.append((*args, kwargs)),
    )

    contact_assessments._run({"id": assessment_id, "contact_id": contact_id}, object())

    assert finished == [
        (
            assessment_id,
            "cancelled",
            "联系人已有已确认身份事实；未向模型发送消息内容。",
            {"outcome": "no_action"},
        )
    ]
