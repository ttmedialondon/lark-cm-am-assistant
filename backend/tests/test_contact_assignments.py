from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest

from app import contact_assessments, contact_assignments


def test_assignment_result_accepts_only_known_candidate_and_evidence() -> None:
    result = contact_assignments._validate_model_result(
        '{"decision":"needs_review","candidate":"A1","confidence":0.72,"evidence_refs":["M1","M2"]}',
        {"A1"},
        {
            "M1": {"candidate": "A1", "speaker": "creator"},
            "M2": {"candidate": "A1", "speaker": "cm_am"},
        },
    )

    assert result == {
        "decision": "needs_review",
        "candidate": "A1",
        "confidence": 0.72,
        "evidence_refs": ["M1", "M2"],
    }
    with pytest.raises(contact_assignments._ModelOutputError):
        contact_assignments._validate_model_result(
            '{"decision":"assign","candidate":"A2","confidence":0.96,"evidence_refs":["M1"]}',
            {"A1"},
            {"M1": {"candidate": "A1", "speaker": "creator"}},
        )
    with pytest.raises(contact_assignments._ModelOutputError):
        contact_assignments._validate_model_result(
            '{"decision":"unassigned","candidate":"A1","confidence":0.4,"evidence_refs":["M1"]}',
            {"A1"},
            {"M1": {"candidate": "A1", "speaker": "creator"}},
        )
    with pytest.raises(contact_assignments._ModelOutputError):
        contact_assignments._validate_model_result(
            '{"decision":"assign","candidate":"A1","confidence":0.96,"evidence_refs":["M1","M2"]}',
            {"A1", "A2"},
            {
                "M1": {"candidate": "A1", "speaker": "creator"},
                "M2": {"candidate": "A2", "speaker": "cm_am"},
            },
        )
    with pytest.raises(contact_assignments._ModelOutputError):
        contact_assignments._validate_model_result(
            '{"decision":"assign","candidate":"A1","confidence":0.96,"evidence_refs":["M1","M3"]}',
            {"A1"},
            {
                "M1": {"candidate": "A1", "speaker": "creator"},
                "M3": {"candidate": "A1", "speaker": "creator"},
            },
        )


def test_assignment_review_requires_meaningful_confidence() -> None:
    candidate_id = uuid4()
    assert not contact_assignments._requires_manual_review(
        {"decision": "needs_review", "confidence": 0.69}, candidate_id
    )
    assert contact_assignments._requires_manual_review(
        {"decision": "needs_review", "confidence": 0.70}, candidate_id
    )
    assert not contact_assignments._requires_manual_review(
        {"decision": "unassigned", "confidence": 1.0}, candidate_id
    )


def test_assignment_queue_requires_direct_interaction_and_current_policy(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT automatic_queue_checked_at" in query:
                return Result(row={"watermark": "-infinity"})
            if "UPDATE ai_feature_configs" in query:
                return Result()
            return Result(rows=[{"id": "queued"}])

    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(Connection()))

    assert contact_assignments.queue_eligible_contact_assignments() == 1

    query, params = calls[-1]
    assert calls[0][1] == ("contact_assignment", 60)
    assert "FOR UPDATE" in calls[0][0]
    assert calls[1][1] == ("contact_assignment",)
    assert params == (
        contact_assignments.POLICY_VERSION,
        "-infinity",
        89,
        "-infinity",
        5,
        3,
        15,
        89,
        contact_assignments.POLICY_VERSION,
        3,
        15,
        contact_assignments.POLICY_VERSION,
        contact_assignments.POLICY_VERSION,
    )
    assert query.count("%s") == len(params)
    assert "automatic_queue_checked_at::text AS watermark" in calls[0][0]
    assert "observation.first_observed_at > %s::timestamptz" in query
    assert "changed_creator_roles AS" in query
    assert "role.updated_at >= %s::timestamptz - (%s * INTERVAL '1 minute')" in query
    assert "SELECT contact_id FROM changed_creator_roles" in query
    assert "role.updated_at AS role_updated_at" in query
    assert "MAX(GREATEST(" in query
    assert "role.role = 'creator' AND role.status = 'confirmed'" in query
    assert "auth.status = 'active'" in query
    assert "auth.assignment_eligible" in query
    assert "observation.sent_by_author" in query
    assert "changed_messages" in query
    assert "HAVING BOOL_OR(speaker = 'cm_am') AND BOOL_OR(speaker = 'creator')" in query
    assert "strong_interaction_chats" in query
    assert "p2p_counterpart_open_id" in query
    assert "mentions_json" in query
    assert "policy_version = %s" in query
    assert "contact_assignments assignment" in query


def test_high_confidence_assignment_is_saved_for_review_without_an_ai_assignment(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    candidate_id = uuid4()
    calls = []
    finished = []

    class Result:
        @staticmethod
        def fetchone():
            return {"id": assessment_id, "status": "running"}

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            return Result()

    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(contact_assignments, "_refs_are_still_authorized_in", lambda *_args: True)
    monkeypatch.setattr(contact_assignments, "_lock_contact", lambda *_args: None)
    monkeypatch.setattr(contact_assignments, "_creator_needs_assignment_in", lambda *_args: True)
    monkeypatch.setattr(
        contact_assignments,
        "_finish_in",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )
    monkeypatch.setattr(contact_assignments, "_audit", lambda *_args: None)

    result = {
        "decision": "assign",
        "candidate": "A1",
        "confidence": 0.99,
        "candidate_aliases": {"A1": str(candidate_id)},
    }

    assert contact_assignments._finish_with_decision(
        assessment_id,
        contact_id,
        result=result,
        model="gpt-5.6-luna",
        response_id="resp_test",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    assert finished[0][1]["outcome"] == "review_required"
    assert finished[0][1]["result"] == result
    assert all("INSERT INTO contact_assignments" not in query for query, _params in calls)


def test_disabled_assignment_feature_discards_an_in_flight_model_result(monkeypatch) -> None:
    assessment_id = uuid4()
    contact_id = uuid4()
    finished = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, _params=None):
            if "FROM ai_feature_configs" in query:
                return Result()
            if "FROM contact_assignment_assessments" in query:
                return Result({"id": assessment_id, "status": "running"})
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(
        contact_assignments,
        "_finish_in",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )

    assert not contact_assignments._finish_with_decision(
        assessment_id,
        contact_id,
        result={"decision": "assign", "candidate": "A1", "confidence": 0.99},
        model="gpt-5.6-luna",
        response_id="resp_test",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    assert finished[0][0][2:4] == (
        "cancelled",
        "达人负责人 AI 匹配已停用；模型结果未写入负责人建议。",
    )
    assert finished[0][1]["outcome"] == "no_action"


@pytest.mark.parametrize("running", [True, False])
def test_assignment_ref_save_locks_and_fences_the_running_assessment(monkeypatch, running) -> None:
    assessment_id = uuid4()
    calls = []

    class Result:
        def fetchone(self):
            return {"id": assessment_id} if running else None

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            return Result()

    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(Connection()))

    contact_assignments._save_refs(
        assessment_id,
        {"aliases": {}, "refs": [], "char_count": 0},
    )

    assert "status = 'running'" in calls[0][0]
    assert calls[0][0].endswith("FOR UPDATE")
    if running:
        assert calls[1][0].startswith("DELETE FROM contact_assignment_assessment_refs")
        assert calls[-1][0].startswith("UPDATE contact_assignment_assessments")
    else:
        assert len(calls) == 1


def test_assignment_ref_check_rejects_a_partially_deleted_evidence_set() -> None:
    calls = []

    class Result:
        def fetchone(self):
            return {"expected_count": 2, "all_count": 1, "allowed_count": 1}

    class Connection:
        def execute(self, query, params):
            calls.append((query, params))
            return Result()

    assert not contact_assignments._refs_are_still_authorized_in(Connection(), uuid4())
    assert "assessment.evidence_count AS expected_count" in calls[0][0]


def test_confirmation_migration_reopens_active_ai_assignments() -> None:
    sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "025_require_owner_confirmation_for_ai_assignments.sql"
    ).read_text(encoding="utf-8")

    assert "WHERE source = 'ai' AND ended_at IS NULL" in sql
    assert "SET outcome = 'review_required'" in sql
    assert f"assessment.policy_version = '{contact_assignments.POLICY_VERSION}'" in sql


def test_creator_shutdown_migration_is_scoped_and_preserves_queue_watermarks() -> None:
    sql = (
        Path(__file__).resolve().parents[1] / "migrations" / "026_disable_creator_ai_and_clear_derived_data.sql"
    ).read_text(encoding="utf-8")

    assert "feature IN ('contact_assessment', 'contact_assignment')" in sql
    assert "SET enabled = FALSE" in sql
    assert "DELETE FROM contact_assignments;" in sql
    assert "DELETE FROM contact_assignment_assessments;" in sql
    assert (
        "DELETE FROM contact_assessments assessment\n"
        "USING creator_cleanup_contacts affected\n"
        "WHERE assessment.contact_id = affected.contact_id;"
    ) in sql
    assert "DELETE FROM contact_assessments;" not in sql
    assert "DELETE FROM contact_roles" in sql
    assert "WHERE role = 'creator'" in sql
    assert "creator_ai_features_disabled_and_data_cleared" in sql
    assert "automatic_queue_checked_at" not in sql
    assert "-infinity" not in sql
    assert "TRUNCATE" not in sql.upper()
    assert "DELETE FROM contacts" not in sql
    assert "DELETE FROM lark_identities" not in sql
    assert "DELETE FROM chat_messages" not in sql
    assert "creator AI configuration is incomplete" not in sql


def test_assignment_model_boundary_preserves_shared_openai_retry_metadata(monkeypatch) -> None:
    expected = contact_assessments.OpenAIRequestError(
        "limited", retry_after_seconds=4, telemetry={"openai_request_id": "req_assignment"}
    )
    monkeypatch.setattr(
        contact_assessments,
        "call_openai",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(expected),
    )

    with pytest.raises(contact_assessments.OpenAIRequestError) as raised:
        contact_assignments._call_model(
            {"model": "gpt-5.6-luna"}, "sk-test", {}, uuid4(), {"A1": {}}
        )

    assert raised.value is expected
