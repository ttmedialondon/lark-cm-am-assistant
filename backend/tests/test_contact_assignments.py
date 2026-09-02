from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

import pytest

from app import contact_assignments


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


def test_assignment_auto_confirmation_requires_coverage_direct_evidence_and_calibration(monkeypatch) -> None:
    candidate_id = uuid4()
    result = {
        "candidate": "A1",
        "evidence_policy": {
            "candidate_pool_total": 2,
            "candidate_pool_considered": 2,
            "strong_cited": True,
        },
    }

    monkeypatch.setattr(contact_assignments, "_manual_calibration_label_count_in", lambda _conn: 30)
    assert contact_assignments._automatic_confirmation_policy(object(), result, candidate_id)["blockers"] == []

    result["evidence_policy"]["candidate_pool_total"] = 1
    assert contact_assignments._automatic_confirmation_policy(object(), result, candidate_id)["blockers"] == [
        "candidate_pool_insufficient"
    ]

    result["evidence_policy"]["candidate_pool_total"] = 2
    result["evidence_policy"]["strong_cited"] = False
    assert contact_assignments._automatic_confirmation_policy(object(), result, candidate_id)["blockers"] == [
        "no_strong_cited_evidence"
    ]

    result["evidence_policy"]["strong_cited"] = True
    monkeypatch.setattr(contact_assignments, "_manual_calibration_label_count_in", lambda _conn: 29)
    assert contact_assignments._automatic_confirmation_policy(object(), result, candidate_id)["blockers"] == [
        "calibration_not_ready"
    ]


def test_assignment_strong_evidence_must_be_one_complete_cited_interaction() -> None:
    result = {"candidate": "A1", "evidence_refs": ["M1", "M2"]}
    evidence = {
        "M1": {"candidate": "A1", "speaker": "creator", "interaction": "I1", "strength": "strong"},
        "M2": {"candidate": "A1", "speaker": "cm_am", "interaction": "I2", "strength": "strong"},
    }
    assert not contact_assignments._has_strong_cited_interaction(result, evidence)

    evidence["M2"]["interaction"] = "I1"
    assert contact_assignments._has_strong_cited_interaction(result, evidence)


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
