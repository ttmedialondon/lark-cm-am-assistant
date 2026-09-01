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
                    "evidence_refs": [evidence_ref],
                }
            ],
            "account_relations": [
                {
                    "account_ref": "A1",
                    "relationship": "owner",
                    "confidence": 0.91,
                    "evidence_refs": ["M1"],
                }
            ],
        }
    )


def test_model_result_rejects_an_invented_message_reference() -> None:
    with pytest.raises(ValueError):
        contact_assessments._validate_model_result(
            _valid_result(evidence_ref="M999"),
            {"M1"},
            {"A1": {"id": str(uuid4()), "label": "tiktok @creator"}},
        )


def test_model_result_keeps_a_valid_role_when_an_optional_account_suggestion_is_invalid() -> None:
    result = contact_assessments._validate_model_result(
        json.dumps(
            {
                "roles": [{"role": "creator", "confidence": 0.95, "evidence_refs": ["M1"]}],
                "account_relations": [
                    {
                        "account_ref": "A999",
                        "relationship": "owner",
                        "confidence": 0.99,
                        "evidence_refs": ["M1"],
                    }
                ],
            }
        ),
        {"M1"},
        {},
    )

    assert result == {
        "roles": [{"role": "creator", "confidence": 0.95, "evidence_refs": ["M1"]}],
        "account_relations": [],
    }


def test_model_result_rejects_insufficient_mixed_with_a_business_role() -> None:
    payload = json.loads(_valid_result())
    payload["roles"].append({"role": "insufficient", "confidence": 0.2, "evidence_refs": ["M1"]})
    with pytest.raises(ValueError):
        contact_assessments._validate_model_result(
            json.dumps(payload),
            {"M1"},
            {"A1": {"id": str(uuid4()), "label": "tiktok @creator"}},
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
                        "result": {"roles": [], "account_relations": []},
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
    assert captured["headers"]["OpenAI-Project"] == "proj_test"
    schema = captured["json"]["text"]["format"]["schema"]
    assert "summary" not in schema["properties"]
    assert "reason" not in schema["properties"]["roles"]["items"]["properties"]
    evidence_refs = schema["properties"]["roles"]["items"]["properties"]["evidence_refs"]
    assert evidence_refs["minItems"] == 1
    assert evidence_refs["maxItems"] == 12
    assert schema["properties"]["roles"]["maxItems"] == 3
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 8}


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
    assert params == (contact_id, authorization_id)
    assert "LIMIT 12" in query


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


def test_auto_decision_confirms_creator_without_an_account_suggestion(monkeypatch) -> None:
    confirmed: list[str] = []
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda _conn, _contact_id, role: confirmed.append(role) or "confirmed",
    )

    outcome, review_status, roles, relations = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        uuid4(),
        {"roles": [{"role": "creator", "confidence": 0.9}], "account_relations": []},
        0.9,
    )

    assert (outcome, review_status, roles, relations) == ("auto_confirmed", "not_required", ["creator"], [])
    assert confirmed == ["creator"]


def test_auto_decision_confirms_a_high_confidence_creator_and_keeps_a_low_role_for_review(monkeypatch) -> None:
    confirmed: list[str] = []
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda _conn, _contact_id, role: confirmed.append(role) or "confirmed",
    )

    outcome, review_status, roles, relations = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        uuid4(),
        {
            "roles": [
                {"role": "creator", "confidence": 0.95},
                {"role": "agent_or_agency", "confidence": 0.20},
            ],
            "account_relations": [],
        },
        0.9,
    )

    assert (outcome, review_status, roles, relations) == ("review_required", "pending", ["creator"], [])
    assert confirmed == ["creator"]


def test_auto_decision_keeps_a_role_below_ninety_percent_for_review(monkeypatch) -> None:
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda *_args: pytest.fail("a role below 90% must not be automatically written"),
    )

    outcome, review_status, roles, relations = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        uuid4(),
        {"roles": [{"role": "creator", "confidence": 0.899}], "account_relations": []},
        0.9,
    )

    assert (outcome, review_status, roles, relations) == ("review_required", "pending", [], [])


def test_auto_decision_sends_low_or_ambiguous_roles_to_review(monkeypatch) -> None:
    monkeypatch.setattr(
        contact_assessments,
        "_auto_confirm_role",
        lambda *_args: pytest.fail("ambiguous role must not be automatically written"),
    )

    outcome, review_status, roles, relations = contact_assessments._apply_automatic_decision(
        object(),
        uuid4(),
        uuid4(),
        {
            "roles": [
                {"role": "creator", "confidence": 0.99},
                {"role": "agent_or_agency", "confidence": 0.91},
            ],
            "account_relations": [],
        },
        0.9,
    )

    assert (outcome, review_status, roles, relations) == ("review_required", "pending", [], [])


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
