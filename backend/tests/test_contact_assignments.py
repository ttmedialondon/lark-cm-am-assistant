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


def test_creator_workbench_scopes_pending_and_confirmed_rows_to_one_authorization(monkeypatch) -> None:
    authorization_id = uuid4()
    assessment_id = uuid4()
    calls = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            if "contact_assignment_assessments" in query:
                return Result(
                    [
                        {
                            "assessment_id": assessment_id,
                            "display_name": None,
                            "confidence": "0.95",
                            "suggested_at": None,
                        }
                    ]
                )
            return Result([{"display_name": "Ada", "started_at": None}])

    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(Connection()))

    workbench = contact_assignments.creator_workbench(authorization_id)

    assert workbench == {
        "pending_candidates": [
            {
                "assessment_id": str(assessment_id),
                "display_name": "未命名达人",
                "confidence_label": "95%",
                "suggested_at_label": "时间未知",
            }
        ],
        "confirmed_creators": [{"display_name": "Ada", "started_at_label": "时间未知"}],
    }
    pending_query, pending_params = next(call for call in calls if "contact_assignment_assessments" in call[0])
    confirmed_query, confirmed_params = next(call for call in calls if "contact_assignment_assessments" not in call[0])
    assert pending_params == (contact_assignments.POLICY_VERSION, authorization_id, contact_assignments.FEATURE)
    assert "DISTINCT ON (assessment.contact_id)" in pending_query
    assert "WHERE assessment.policy_version = %s ORDER BY assessment.contact_id" in pending_query
    assert "ORDER BY assessment.contact_id, assessment.created_at DESC, assessment.id DESC" in pending_query
    assert "latest.status = 'succeeded'" in pending_query
    assert "config.feature = %s AND config.enabled" in pending_query
    assert "config.auto_confirm_threshold" in pending_query
    assert "candidate_aliases" in pending_query
    assert "actor.id::text" in pending_query
    assert "assignment.ended_at IS NULL" in pending_query
    assert "LIMIT 25" not in pending_query
    assert confirmed_params == (authorization_id,)
    assert "assignment.responsible_authorization_id = %s" in confirmed_query
    assert "assignment.ended_at IS NULL" in confirmed_query
    assert "LIMIT 25" not in confirmed_query


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


class _ConfirmationResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _ConfirmationConnection:
    def __init__(
        self,
        *,
        actor_id,
        assessment_id,
        contact_id,
        suggested_actor_id=None,
        confidence=0.95,
        latest_id=None,
        current_owner_id=None,
    ):
        self.actor_id = actor_id
        self.assessment_id = assessment_id
        self.contact_id = contact_id
        self.suggested_actor_id = suggested_actor_id or actor_id
        self.confidence = confidence
        self.latest_id = assessment_id if latest_id is None else latest_id
        self.current_owner_id = current_owner_id
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        if "SELECT id, contact_id, status, outcome, policy_version, result" in normalized:
            return _ConfirmationResult(
                {
                    "id": self.assessment_id,
                    "contact_id": self.contact_id,
                    "status": "succeeded",
                    "outcome": "review_required",
                    "policy_version": contact_assignments.POLICY_VERSION,
                    "result": {
                        "decision": "assign",
                        "candidate": "A1",
                        "confidence": self.confidence,
                        "candidate_aliases": {"A1": str(self.suggested_actor_id)},
                    },
                }
            )
        if normalized.startswith("SELECT id FROM contact_assignment_assessments"):
            return _ConfirmationResult({"id": self.latest_id})
        if normalized.startswith("SELECT responsible_authorization_id"):
            return _ConfirmationResult(
                {"responsible_authorization_id": self.current_owner_id} if self.current_owner_id else None
            )
        if normalized.startswith("SELECT auto_confirm_threshold"):
            return _ConfirmationResult({"auto_confirm_threshold": 0.9})
        if "INSERT INTO contact_assignments" in normalized or "INSERT INTO audit_events" in normalized:
            return _ConfirmationResult()
        raise AssertionError(f"unexpected query: {normalized}")


def _allow_confirmation_checks(monkeypatch) -> None:
    monkeypatch.setattr(contact_assignments, "_lock_assignment_confirmation_rows_in", lambda *_args: None)
    monkeypatch.setattr(contact_assignments, "_is_assignable_in", lambda *_args: True)
    monkeypatch.setattr(contact_assignments, "_refs_are_still_authorized_in", lambda *_args: True)
    monkeypatch.setattr(contact_assignments, "_confirmed_creator_in", lambda *_args: True)


def test_suggested_owner_confirmation_retries_a_database_deadlock(monkeypatch) -> None:
    actor_id = uuid4()
    assessment_id = uuid4()
    attempts = []

    def confirm_once(received_actor_id, received_assessment_id):
        attempts.append((received_actor_id, received_assessment_id))
        if len(attempts) < 3:
            raise contact_assignments.psycopg.errors.DeadlockDetected("deadlock")
        return {"contact_id": "contact", "status": "assigned"}

    monkeypatch.setattr(contact_assignments, "_confirm_suggested_assignment_once", confirm_once)

    assert contact_assignments.confirm_suggested_assignment(actor_id, assessment_id) == {
        "contact_id": "contact",
        "status": "assigned",
    }
    assert attempts == [(actor_id, assessment_id)] * 3


def test_suggested_owner_confirmation_turns_repeated_deadlocks_into_a_conflict(monkeypatch) -> None:
    attempts = []

    def confirm_once(*args):
        attempts.append(args)
        raise contact_assignments.psycopg.errors.DeadlockDetected("deadlock")

    monkeypatch.setattr(contact_assignments, "_confirm_suggested_assignment_once", confirm_once)

    with pytest.raises(contact_assignments.ContactAssignmentInputError, match="正在同步"):
        contact_assignments.confirm_suggested_assignment(uuid4(), uuid4())
    assert len(attempts) == 3


def test_suggested_owner_confirmation_writes_one_manual_assignment_with_provenance(monkeypatch) -> None:
    actor_id = uuid4()
    assessment_id = uuid4()
    contact_id = uuid4()
    conn = _ConfirmationConnection(actor_id=actor_id, assessment_id=assessment_id, contact_id=contact_id)
    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(conn))
    _allow_confirmation_checks(monkeypatch)

    assert contact_assignments.confirm_suggested_assignment(actor_id, assessment_id) == {
        "contact_id": str(contact_id),
        "status": "assigned",
    }

    insert_query, insert_params = next(call for call in conn.calls if "INSERT INTO contact_assignments" in call[0])
    assert "'manual'" in insert_query
    assert insert_params[1:] == (contact_id, actor_id, 0.95, assessment_id, actor_id)
    config_query = next(query for query, _params in conn.calls if query.startswith("SELECT auto_confirm_threshold"))
    assert "WHERE feature = %s AND enabled" in config_query
    assert config_query.endswith("FOR UPDATE")
    latest_query = next(
        query for query, _params in conn.calls if query.startswith("SELECT id FROM contact_assignment_assessments")
    )
    assert "status = 'succeeded'" not in latest_query
    assert "ORDER BY created_at DESC, id DESC" in latest_query
    audit_params = next(params for query, params in conn.calls if "INSERT INTO audit_events" in query)
    assert audit_params[1:3] == (actor_id, "contact_assignment_suggestion_confirmed")


@pytest.mark.parametrize("case", ["other_user", "low_confidence", "stale", "other_owner"])
def test_suggested_owner_confirmation_rejects_unsafe_cases(monkeypatch, case) -> None:
    actor_id = uuid4()
    assessment_id = uuid4()
    contact_id = uuid4()
    other_id = uuid4()
    conn = _ConfirmationConnection(
        actor_id=actor_id,
        assessment_id=assessment_id,
        contact_id=contact_id,
        suggested_actor_id=other_id if case == "other_user" else actor_id,
        confidence=0.89 if case == "low_confidence" else 0.95,
        latest_id=other_id if case == "stale" else assessment_id,
        current_owner_id=other_id if case == "other_owner" else None,
    )
    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(conn))
    _allow_confirmation_checks(monkeypatch)

    with pytest.raises(contact_assignments.ContactAssignmentInputError):
        contact_assignments.confirm_suggested_assignment(actor_id, assessment_id)

    assert all("INSERT INTO contact_assignments" not in query for query, _params in conn.calls)


def test_repeated_confirmation_by_the_same_owner_is_idempotent(monkeypatch) -> None:
    actor_id = uuid4()
    assessment_id = uuid4()
    contact_id = uuid4()
    conn = _ConfirmationConnection(
        actor_id=actor_id,
        assessment_id=assessment_id,
        contact_id=contact_id,
        current_owner_id=actor_id,
    )
    monkeypatch.setattr(contact_assignments, "connection", lambda: nullcontext(conn))
    _allow_confirmation_checks(monkeypatch)

    assert contact_assignments.confirm_suggested_assignment(actor_id, assessment_id) == {
        "contact_id": str(contact_id),
        "status": "unchanged",
    }
    assert all("INSERT INTO contact_assignments" not in query for query, _params in conn.calls)


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


def test_confirmation_locks_mutable_rows_in_sync_writer_order() -> None:
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"assignable": True}

        @staticmethod
        def fetchall():
            return []

    class Connection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    contact_id = uuid4()
    authorization_id = uuid4()
    assessment_id = uuid4()
    conn = Connection()

    contact_assignments._lock_assignment_confirmation_rows_in(
        conn, contact_id, authorization_id, assessment_id
    )

    assert [params for _query, params in calls] == [
        (authorization_id, assessment_id),
        (assessment_id,),
        (contact_id,),
        (assessment_id,),
        (assessment_id,),
        (assessment_id,),
    ]
    assert calls[0][0].endswith("FOR UPDATE OF auth")
    assert calls[1][0].endswith("FOR UPDATE OF access")
    assert calls[2][0].endswith("FOR UPDATE")
    assert calls[3][0].endswith("FOR UPDATE OF message")
    assert calls[4][0].endswith("FOR UPDATE OF observation")
    assert calls[5][0].endswith("FOR UPDATE OF reference")


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
