"""Durable matching workflow on isolated PostgreSQL; provider responses are synthetic."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from cap_fixtures import _snapshot
from cap_fixtures import pilot as parent_pilot

from app import ai_dispatch, ai_feature_config, config, creator_data, openai_client
from app import creator_handle_matching as matching

pilot = parent_pilot


@pytest.fixture
def matching_flow(pg, pilot, monkeypatch):
    state = SimpleNamespace(
        settings=pilot.settings,
        contact=pilot.ids["creator"],
        calls=[],
        valid=True,
        before_response=None,
        error=None,
        review=False,
        material_version=1,
    )
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: state.settings)
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v1"))
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_: state.valid)

    def prepare(_conn, _settings, contact, _catalog):
        pack = {
            "target": "U1",
            "creator_known": True,
            "handles": [
                {"id": "H1", "handle": "first.handle", "catalog_verified": True},
                {"id": "H2", "handle": "backup_2", "catalog_verified": True},
            ],
            "people": [{"id": "U1", "role": "external"}],
            "chats": [{"id": "G1", "name": "Synthetic test conversation"}],
            "messages": [
                {
                    "id": "M1",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:00:00+00:00",
                    "reply": None,
                    "text": "My TikTok account is first.handle",
                },
                {
                    "id": "M2",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:01:00+00:00",
                    "reply": None,
                    "text": "My backup account is backup_2",
                },
            ],
            "coverage": {"full_history": False, "missing_reply_ids": [], "excerpted_ids": []},
        }
        return {
            "pack": pack,
            "refs": [{"kind": "message", "label": "M1"}],
            "fingerprint": f"synthetic-{contact}-{state.material_version}",
            "reason": "ready",
            "stats": {"messages": 2},
        }

    def respond(_config, _key, pack, _contact, **kwargs):
        state.calls.append(kwargs)
        if state.before_response:
            state.before_response()
        if state.error:
            raise state.error
        result = {
            "creator": {"state": "known", "refs": []},
            "links": [
                {
                    "h": handle["id"],
                    "decision": "supported",
                    "relation": "self_account",
                    "period": "at_message",
                    "code": "self_claim",
                    "refs": [f"M{index}"],
                    "needed": [],
                }
                for index, handle in enumerate(pack["handles"], 1)
            ],
        }
        if state.review:
            result["links"][-1].update(
                decision="review", relation="uncertain", code="ambiguous_reply", needed=["parent_reply"]
            )
        return {
            "text": json.dumps(result),
            "response_id": "resp_synthetic",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            },
            "telemetry": {"openai_request_id": "req_synthetic"},
        }

    monkeypatch.setattr(matching.evidence, "build", prepare)
    monkeypatch.setattr(matching.api, "call_openai", respond)

    def enable():
        ai_feature_config.save_feature_config(
            pg,
            ai_feature_config.api_key_cipher(state.settings.token_encryption_key),
            matching.FEATURE,
            provider="openai",
            project_id="proj_synthetic",
            model="gpt-5.6-luna",
            enabled=True,
            api_key="sk-synthetic-test-only",
            updated_by_authorization_id=pilot.ids["owner"],
        )
        # The fallback reconciliation is covered separately; keep this test to one chosen contact.
        pg.execute(
            "UPDATE ai_feature_configs SET automatic_queue_checked_at=NOW() WHERE feature=%s", (matching.FEATURE,)
        )

    state.enable = enable
    return state


def _enqueue(pg, contact):
    return pg.execute("SELECT enqueue_creator_handle(%s,'manual',TRUE) id", (contact,)).fetchone()["id"]


def _prepared_job(pg, flow):
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job is not None
    return job


def test_opt_in_coalescing_multi_handle_suggestions_and_exact_cache(pg, matching_flow):
    flow = matching_flow
    assert _enqueue(pg, flow.contact) is None
    assert not matching.prepare_next(flow.settings) and matching.claim() is None
    assert not flow.calls
    flow.enable()
    before = _snapshot(
        pg,
        ("contacts", "contact_roles", "contact_assignments", "contact_social_accounts", "creator_account_assignments"),
    )
    identifier = _enqueue(pg, flow.contact)
    assert _enqueue(pg, flow.contact) == identifier
    assert pg.execute("SELECT count(*) n FROM creator_handle_jobs").fetchone()["n"] == 1
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job["attempts"] == 1 and job["snapshot_version"] == job["desired_version"] == 2
    matching.run(job, flow.settings)
    row = pg.execute("SELECT * FROM creator_handle_jobs WHERE id=%s", (identifier,)).fetchone()
    assert row["status"] == "completed" and row["stats"]["supported_handles"] == ["first.handle", "backup_2"]
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    assert row["response_id"] == "resp_synthetic" and row["request_id"] == "req_synthetic"
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] == 150
    view = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))
    assert len(view["result"]["links"]) == 2
    assert flow.calls[0]["reasoning_effort"] == "none" and flow.calls[0]["total_timeout"] == 45
    new_id = _enqueue(pg, flow.contact)
    assert new_id != identifier and matching.prepare_next(flow.settings)
    cached = pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (new_id,)).fetchone()
    assert cached == {"status": "completed", "reason_code": "cached"}
    assert matching.claim() is None and len(flow.calls) == 1
    assert _snapshot(pg, before.keys()) == before


def test_stale_version_and_revoked_evidence_never_publish_suggestions(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.before_response = lambda: _enqueue(pg, flow.contact)
    matching.run(job, flow.settings)
    current = pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert current == {"status": "queued", "result_ciphertext": None}
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] == 150
    flow.before_response = None
    assert matching.prepare_next(flow.settings)
    next_job = matching.claim()
    assert next_job["snapshot_version"] > job["snapshot_version"]
    flow.valid = False
    matching.run(next_job, flow.settings)
    assert len(flow.calls) == 1  # Revocation before network releases the reservation, without another API call.
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (next_job["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 0
    )


def test_provider_delay_quota_pause_and_uncertain_budget_survive_retries(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic overload", retry_after_seconds=7200, retryable=True)
    matching.run(job, flow.settings)
    row = pg.execute(
        "SELECT status,EXTRACT(EPOCH FROM not_before-NOW()) seconds FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert row["status"] == "retry_wait" and row["seconds"] >= 7200
    assert ai_dispatch.status(pg)["state"] == "throttled" and matching.claim() is None
    assert pg.execute("SELECT billed_tokens FROM creator_handle_requests").fetchone()["billed_tokens"] is None
    ai_dispatch.resume(pg)
    pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (job["id"],))
    retry = matching.claim()
    assert retry["attempts"] == 2 and retry["reservation_id"] != job["reservation_id"]
    flow.error = openai_client.OpenAIRequestError("synthetic quota", is_quota=True)
    matching.run(retry, flow.settings)
    assert ai_dispatch.status(pg)["state"] == "paused" and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "failed",
        "reason_code": "quota_paused",
    }
    assert len(flow.calls) == 2


def test_budget_and_restart_lease_fence_bound_paid_work(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    pg.execute("UPDATE ai_feature_configs SET daily_token_budget=1000 WHERE feature=%s", (matching.FEATURE,))
    assert matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs").fetchone() == {
        "status": "failed",
        "reason_code": "request_exceeds_budget",
    }
    assert pg.execute("SELECT count(*) n FROM creator_handle_requests").fetchone()["n"] == 0
    pg.execute("UPDATE ai_feature_configs SET daily_token_budget=1000000 WHERE feature=%s", (matching.FEATURE,))
    job = _prepared_job(pg, flow)
    pg.execute("UPDATE creator_handle_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (job["id"],))
    recovered = matching.claim()
    assert recovered["attempts"] == 2 and recovered["claim_token"] != job["claim_token"]
    matching.run(job, flow.settings)
    assert not flow.calls
    matching.run(recovered, flow.settings)
    assert len(flow.calls) == 1
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "completed"
    )
    flow.error = openai_client.OpenAIRequestError("synthetic transient failure", retryable=True)
    flow.material_version += 1
    retry = _prepared_job(pg, flow)
    for attempt in range(1, 4):
        assert retry["attempts"] == attempt
        matching.run(retry, flow.settings)
        ai_dispatch.resume(pg)
        pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (retry["id"],))
        if attempt < 3:
            retry = matching.claim()
    assert matching.claim() is None
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (retry["id"],)).fetchone()["status"]
        == "failed"
    )
    previous_calls = len(flow.calls)
    cached_id = pg.execute("SELECT enqueue_creator_handle(%s,'incremental',TRUE) id", (flow.contact,)).fetchone()["id"]
    assert matching.prepare_next(flow.settings)
    assert matching.claim() is None
    assert len(flow.calls) == previous_calls
    assert (
        pg.execute("SELECT reason_code FROM creator_handle_jobs WHERE id=%s", (cached_id,)).fetchone()["reason_code"]
        == "failure_cached"
    )


def test_review_result_reuses_its_own_snapshot_when_signal_changes_no_material(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    flow.review = True
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert _enqueue(pg, flow.contact) == job["id"]
    assert matching.prepare_next(flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "review_required",
        "reason_code": "cached",
    }
    assert matching.claim() is None and len(flow.calls) == 1
    # Coverage alone still requires review, even if the model's selected links all support ownership.
    flow.review = False
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    pg.execute(
        "UPDATE creator_handle_jobs SET stats=stats || '{\"critical_evidence_omitted\":true}'::jsonb WHERE id=%s",
        (job["id"],),
    )
    matching.run(job, flow.settings)
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert matching.claim() is None and len(flow.calls) == 2


def test_role_and_chat_events_shared_slots_and_catalog_changes(pg, matching_flow, pilot, monkeypatch):
    flow = matching_flow
    flow.enable()
    # Role confirmation from any SQL writer is durable in the same transaction.
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (flow.contact,))
    pg.execute("UPDATE contact_roles SET status='confirmed' WHERE contact_id=%s", (flow.contact,))
    job_id = pg.execute("SELECT id FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,)).fetchone()["id"]
    pg.execute("UPDATE chats SET name='first.handle & TT Media' WHERE chat_id='chat_creator'")
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,)).fetchone()["n"]
        == 1
    )
    pg.execute("UPDATE creator_handle_jobs SET not_before=NOW() WHERE id=%s", (job_id,))
    assert matching.prepare_next(flow.settings)
    # Matching leases consume the shared admission counter, just as summary/reply leases do.
    for who in ("legacy", "silent", "cm"):
        pg.execute(
            """INSERT INTO creator_handle_jobs(contact_id,status,lease_until)
            VALUES(%s,'running',NOW()+INTERVAL '2 minutes')""",
            (pilot.ids[who],),
        )
    assert matching.claim() is None
    pg.execute("UPDATE creator_handle_jobs SET status='completed' WHERE id<>%s", (job_id,))
    job = matching.claim()
    assert job is not None
    # Curva version changed before networking: no charge, no obsolete suggestions.
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v2"))
    matching.run(job, flow.settings)
    assert not flow.calls
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 0
    )
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "queued"
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    flow.before_response = lambda: monkeypatch.setattr(
        matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v3")
    )
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1
    assert pg.execute("SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone() == {
        "status": "queued",
        "result_ciphertext": None,
    }
