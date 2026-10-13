"""Durable matching workflow on isolated PostgreSQL; provider responses are synthetic."""

import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cap_fixtures import _snapshot
from cap_fixtures import pilot as parent_pilot

from app import ai_dispatch, ai_feature_config, config, contact_handles, creator_data, openai_client
from app import creator_handle_matching as matching
from app.crypto import message_body_cipher

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
        relation="self_account",
        period="at_message",
        needed=[],
        stats={},
        legacy_stats=False,
        missing_reply_ids=[],
        material_version=1,
        reference_version=1,
        catalog_handles=frozenset({"first.handle", "backup_2"}),
        real_build=matching.evidence.build,
        real_still_valid=matching.evidence.still_valid,
    )
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: state.settings)
    monkeypatch.setattr(matching, "catalog", lambda: (state.catalog_handles, "catalog-v1"))
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_, **__: state.valid)

    def prepare(_conn, _settings, contact, _catalog, *, recheck=False):
        pairs = contact_handles.confirmed_pairs(_conn, [contact])
        confirmed = sorted({p["handle"] for p in pairs})
        pack = {
            "target": "U1",
            "creator_known": True,
            "synthetic_material_revision": state.material_version,
            "handles": [
                {"id": "H1", "handle": "first.handle", "catalog_verified": "first.handle" in _catalog},
                {"id": "H2", "handle": "backup_2", "catalog_verified": "backup_2" in _catalog},
            ],
            "people": [{"id": "U1", "role": "external"}],
            "chats": [{"id": "G1", "name": "Synthetic test conversation", "mode": "group"}],
            "messages": [
                {
                    "id": "M1",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:00:00+00:00",
                    "reply": "M_absent" if "M1" in state.missing_reply_ids else None,
                    "text": "My TikTok account is first.handle",
                },
                {
                    "id": "M2",
                    "chat": "G1",
                    "speaker": "U1",
                    "at": "2026-10-03T10:01:00+00:00",
                    "reply": "M_absent" if "M2" in state.missing_reply_ids else None,
                    "text": "My backup account is backup_2",
                },
            ],
            "coverage": {"full_history": False, "missing_reply_ids": state.missing_reply_ids, "excerpted_ids": []},
        }
        if not recheck:
            pack["handles"] = [h for h in pack["handles"] if h["handle"] not in confirmed]
        return {
            "pack": pack if pack["handles"] else None,
            "refs": [
                {"kind": "message", "label": label, "synthetic_version": state.reference_version}
                for label in ("M1", "M2")
            ],
            "fingerprint": f"synthetic-{contact}-{state.material_version}-{[h['handle'] for h in pack['handles']]}",
            "reason": "ready" if pack["handles"] else "already_confirmed",
            "stats": {
                "messages": 2,
                "confirmed_handles": confirmed,
                "human_confirmed_handles": sorted({p["handle"] for p in pairs if p["human_confirmed"]}),
                "manual_recheck": recheck,
                "recheck_handles": confirmed if recheck else [],
                **({"critical_evidence_omitted_handles": []} if not state.legacy_stats else {}),
                **state.stats,
            },
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
                    "relation": state.relation,
                    "period": state.period,
                    "code": "self_claim",
                    "refs": ["M" + handle["id"][1:]],
                    "needed": state.needed,
                }
                for handle in pack["handles"]
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

    def enable(api_key="sk-synthetic-test-only"):
        ai_feature_config.save_feature_config(
            pg,
            ai_feature_config.api_key_cipher(state.settings.token_encryption_key),
            matching.FEATURE,
            provider="openai",
            project_id="proj_synthetic",
            model="gpt-5.6-luna",
            enabled=True,
            api_key=api_key,
            updated_by_authorization_id=pilot.ids["owner"],
        )
        # The fallback reconciliation is covered separately; keep this test to one chosen contact.
        pg.execute(
            "UPDATE ai_feature_configs SET automatic_queue_checked_at=NOW() WHERE feature=%s", (matching.FEATURE,)
        )

    state.enable = enable
    return state


def _enqueue(pg, contact, source="manual"):
    return pg.execute("SELECT enqueue_creator_handle(%s,%s,TRUE) id", (contact, source)).fetchone()["id"]


def _prepared_job(pg, flow, source="manual"):
    _enqueue(pg, flow.contact, source)
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job is not None
    return job


def test_opt_in_coalescing_multi_handle_auto_links_and_exact_cache(pg, matching_flow):
    flow = matching_flow
    assert _enqueue(pg, flow.contact) is None
    assert not matching.prepare_next(flow.settings) and matching.claim() is None
    assert not flow.calls
    flow.enable()
    before = _snapshot(
        pg,
        ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"),
    )
    original_links = _snapshot(pg, ("contact_social_accounts",))["contact_social_accounts"]
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
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    links = pg.execute(
        "SELECT relationship,status,source FROM contact_social_accounts WHERE contact_id=%s", (flow.contact,)
    ).fetchall()
    assert links == [{"relationship": "owner", "status": "confirmed", "source": "creator_handle_auto_verified"}] * 2
    audits = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchall()
    assert {row["metadata"]["handle"] for row in audits} == {"first.handle", "backup_2"}
    assert all(
        row["metadata"]["job_id"] == str(identifier) and row["metadata"]["contact_id"] == str(flow.contact)
        for row in audits
    )
    assert all(
        row["metadata"]["refs"] == ["M1" if row["metadata"]["handle"] == "first.handle" else "M2"]
        and row["metadata"]["prompt_version"] == matching.evidence.PROMPT_VERSION
        and row["metadata"]["evidence_refs"]
        == [{"kind": "message", "label": row["metadata"]["refs"][0], "synthetic_version": 1}]
        and "text" not in row["metadata"]
        for row in audits
    )
    # Rotating credentials or a readable grant does not change the model's semantic input.
    flow.enable("sk-success-cache-replacement")
    flow.reference_version += 1
    new_id = _enqueue(pg, flow.contact)
    assert new_id != identifier and matching.prepare_next(flow.settings)
    cached = pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (new_id,)).fetchone()
    assert cached == {"status": "completed", "reason_code": "cached"}
    assert matching.claim() is None and len(flow.calls) == 1
    assert pg.execute("SELECT refs FROM creator_handle_jobs WHERE id=%s", (new_id,)).fetchone()["refs"] == [
        {"kind": "message", "label": label, "synthetic_version": 2} for label in ("M1", "M2")
    ]
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 2
    )
    assert (
        pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchall() == audits
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    # Simulate a pre-upgrade suggestion-only cache using isolated fixture data.
    pg.execute(
        "DELETE FROM contact_social_accounts WHERE contact_id=%s AND source='creator_handle_auto_verified'",
        (flow.contact,),
    )
    pg.execute("DELETE FROM audit_events WHERE action='creator_handle_auto_linked'")
    migrated_id = _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["backup_2", "first.handle"]
    assert (
        pg.execute(
            "SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked' AND metadata->>'job_id'=%s",
            (str(migrated_id),),
        ).fetchone()["n"]
        == 2
    )
    assert all(row in _snapshot(pg, ("contact_social_accounts",))["contact_social_accounts"] for row in original_links)
    assert _snapshot(pg, before.keys()) == before


def test_stale_version_and_revoked_evidence_never_publish_suggestions(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    version = matching.evidence.PROMPT_VERSION
    # A ready pack built under retired rules must reprepare before reserving or sending a request.
    with monkeypatch.context() as rules:
        rules.setattr(matching.evidence, "PROMPT_VERSION", version + "-retired")
        old_id = _enqueue(pg, flow.contact)
        assert matching.prepare_next(flow.settings)
        old_hash = pg.execute("SELECT config_fingerprint FROM creator_handle_jobs WHERE id=%s", (old_id,)).fetchone()[
            "config_fingerprint"
        ]
    assert matching.claim() is None and not flow.calls
    assert not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (old_id,)).fetchone() == {
        "status": "queued", "reason_code": "snapshot_changed"
    }
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job is not None and job["stats"]["prompt_version"] == version
    assert job["config_fingerprint"] != old_hash
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
    # Revocation after networking must also fence the formal association, not only the suggestion.
    flow.valid = True
    flow.before_response = lambda: setattr(flow, "valid", False)
    after_network = _prepared_job(pg, flow)
    matching.run(after_network, flow.settings)
    assert len(flow.calls) == 2
    assert (
        pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)).fetchone()[
            "result_ciphertext"
        ]
        is None
    )
    # A grant can disappear while waiting for writer locks, after the post-response check passed.
    flow.valid, flow.before_response = True, None
    at_write = _prepared_job(pg, flow)
    validity = iter((True, True, False))
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_: next(validity))
    matching.run(at_write, flow.settings)
    assert len(flow.calls) == 3
    assert (
        pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (at_write["id"],)).fetchone()[
            "result_ciphertext"
        ]
        is None
    )
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (at_write["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # Rules changed during a paid request fence publication, but preserve the billed response ledger.
    with monkeypatch.context() as rules:
        rules.setattr(matching.evidence, "still_valid", lambda *_: True)
        changed_rules = _prepared_job(pg, flow)
        flow.before_response = lambda: rules.setattr(matching.evidence, "PROMPT_VERSION", version + "-next")
        matching.run(changed_rules, flow.settings)
        assert len(flow.calls) == 4
        assert pg.execute(
            "SELECT status,result_ciphertext,reason_code FROM creator_handle_jobs WHERE id=%s", (changed_rules["id"],)
        ).fetchone() == {"status": "queued", "result_ciphertext": None, "reason_code": "snapshot_changed"}
        response = pg.execute("SELECT billed_tokens,response_ciphertext FROM creator_handle_requests WHERE id=%s",
                              (changed_rules["reservation_id"],)).fetchone()
        assert response["billed_tokens"] == 150 and response["response_ciphertext"] is not None
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    flow.before_response = None


def test_provider_delay_quota_pause_and_uncertain_usage_survive_retries(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic overload", retry_after_seconds=7200, retryable=True,
                                                telemetry={"http_status": 429})
    delays = []
    monkeypatch.setattr(matching.api, "retry_delay", lambda wait, attempts: delays.append((wait, attempts)) or 7200.5)
    matching.run(job, flow.settings)
    row = pg.execute(
        "SELECT status,EXTRACT(EPOCH FROM not_before-NOW()) seconds FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert row["status"] == "retry_wait" and row["seconds"] >= 7200
    assert delays == [(7200, job["attempts"])]
    assert pg.execute("SELECT j.not_before=s.not_before AS same_deadline FROM creator_handle_jobs j "
                      "CROSS JOIN ai_admission_state s WHERE j.id=%s", (job["id"],)).fetchone()["same_deadline"]
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
    ai_dispatch.resume(pg)
    unknown = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    matching.run(unknown, flow.settings)
    assert matching.claim() is None and len(flow.calls) == 3
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s",
                      (unknown["id"],)).fetchone() == {"status": "failed", "reason_code": "request_unknown"}
    logged = pg.execute("SELECT error_code,billed_tokens FROM creator_handle_requests WHERE id=%s",
                        (unknown["reservation_id"],)).fetchone()
    assert logged == {"error_code": "request_unknown", "billed_tokens": None}
    flow.enable("sk-rotated-after-unknown")
    blocked = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (blocked,)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown",
    }
    assert len(flow.calls) == 3
    flow.error = None
    retried = _prepared_job(pg, flow)  # Explicit Owner recheck is still allowed.
    matching.run(retried, flow.settings)
    assert len(flow.calls) == 4


def test_config_change_during_unknown_call_does_not_redispatch_same_material(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    flow.before_response = lambda: flow.enable("sk-rotated-during-call")
    matching.run(job, flow.settings)
    flow.before_response = None
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert len(flow.calls) == 1
    assert pg.execute("SELECT reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "reason_code": "request_unknown",
    }
    # New actual message content is distinct material, not another attempt at the frozen pack.
    original_build = matching.evidence.build

    def changed(*args, **kwargs):
        material = original_build(*args, **kwargs)
        material["pack"]["messages"][0]["text"] += " I confirm it is mine."
        material["fingerprint"] += "-new-message"
        return material

    monkeypatch.setattr(matching.evidence, "build", changed)
    flow.error = None
    fresh = _prepared_job(pg, flow, "incremental")
    matching.run(fresh, flow.settings)
    assert len(flow.calls) == 2


def test_legacy_limits_do_not_block_restart_and_retry_fences(pg, matching_flow):
    flow = matching_flow
    flow.enable()
    # Historical limits must not reject requests or delay them until the next minute/day.
    pg.execute(
        "UPDATE ai_feature_configs SET rpm_limit=1,daily_token_budget=1000,tpm_limit=1000 WHERE feature=%s",
        (matching.FEATURE,),
    )
    historical_id = uuid4()
    # Simulate the applied-090 schema, then upgrade without deleting accounting history.
    pg.execute("ALTER TABLE creator_handle_requests ALTER COLUMN quota_scope SET NOT NULL, "
               "ALTER COLUMN reserved_tokens SET NOT NULL")
    pg.execute("CREATE INDEX creator_handle_request_budget ON creator_handle_requests(quota_scope,created_at)")
    pg.execute("INSERT INTO creator_handle_requests(id,quota_scope,reserved_tokens,billed_tokens) "
               "VALUES(%s,'historical-project',10000,123)", (historical_id,))
    historical = pg.execute("SELECT * FROM creator_handle_requests WHERE id=%s", (historical_id,)).fetchone()
    pg.execute((Path(__file__).parents[1] / "migrations/091_retire_handle_budget_estimates.sql").read_text())
    job = _prepared_job(pg, flow)
    assert pg.execute("SELECT quota_scope,reserved_tokens FROM creator_handle_requests WHERE id=%s",
                      (job["reservation_id"],)).fetchone() == {
        "quota_scope": None, "reserved_tokens": None,
    }
    pg.execute("UPDATE creator_handle_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (job["id"],))
    assert matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown",
    }
    assert pg.execute("SELECT count(*) n FROM creator_handle_requests WHERE job_id IS NOT NULL").fetchone()["n"] == 1
    matching.run(job, flow.settings)
    assert not flow.calls
    recovered = _prepared_job(pg, flow)  # Only an explicit owner recheck allows another request.
    matching.run(recovered, flow.settings)
    assert len(flow.calls) == 1
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (recovered["id"],)).fetchone()["status"]
        == "completed"
    )
    assert pg.execute("SELECT * FROM creator_handle_requests WHERE id=%s", (historical_id,)).fetchone() == historical
    # Reopen only fixture-generated pairs to exercise provider failure caching for unlinked candidates.
    pg.execute(
        "DELETE FROM contact_social_accounts WHERE contact_id=%s AND source='creator_handle_auto_verified'",
        (flow.contact,),
    )
    flow.error = openai_client.OpenAIRequestError("synthetic transient failure", retryable=True,
                                                telemetry={"http_status": 503})
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
    # A new credential may fix a provider failure: do not reuse the old Key's failed request.
    flow.enable("sk-replacement-test-only")
    flow.error = None
    refreshed_id = pg.execute("SELECT enqueue_creator_handle(%s,'incremental',TRUE) id", (flow.contact,)).fetchone()[
        "id"
    ]
    assert matching.prepare_next(flow.settings)
    refreshed = matching.claim()
    assert refreshed is not None and refreshed["id"] == refreshed_id
    matching.run(refreshed, flow.settings)
    assert len(flow.calls) == previous_calls + 1
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (refreshed_id,)).fetchone()["status"]
        == "completed"
    )


def test_review_and_incomplete_evidence_never_auto_link_and_cache_can_reuse(pg, matching_flow):
    flow = matching_flow
    flow.catalog_handles = frozenset()
    flow.enable()
    # A supported answer is not enough if it describes historical ownership or still needs evidence.
    for period, needed, stats, legacy in (
        ("historical", [], {}, False),
        ("at_message", ["recent_state"], {}, False),
        ("at_message", [], {"history_truncated": True}, True),
        ("at_message", [], {"candidates_omitted": 1}, True),
        ("at_message", [], {"critical_evidence_omitted": True}, True),
    ):
        flow.period, flow.needed, flow.stats, flow.legacy_stats = period, needed, stats, legacy
        flow.material_version += 1
        matching.run(_prepared_job(pg, flow), flow.settings)
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    for relation in ("operator", "account_creator", "other_person"):
        flow.relation, flow.period, flow.needed, flow.stats, flow.legacy_stats = relation, "at_message", [], {}, False
        flow.material_version += 1
        matching.run(_prepared_job(pg, flow), flow.settings)
        assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
        assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # Typed coverage limits only the affected handle; other strong claims are not collateral damage.
    flow.relation = "self_account"
    flow.period, flow.needed, flow.legacy_stats = "at_message", [], False
    flow.stats = {
        "history_truncated": True,
        "candidates_omitted": 1,
        "critical_evidence_omitted": True,
        "critical_evidence_omitted_handles": ["backup_2"],
    }
    # This message independently names its own account: an unrelated unavailable reply is unnecessary.
    flow.missing_reply_ids = ["M1"]
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    # Both model claims are strong, but only the backup account has a critical evidence gap.
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    original_audit = pg.execute(
        "SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'"
    ).fetchone()
    assert original_audit["metadata"]["evidence_refs"] == [{"kind": "message", "label": "M1", "synthetic_version": 1}]
    flow.reference_version += 1
    assert _enqueue(pg, flow.contact) == job["id"]
    assert matching.prepare_next(flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "status": "review_required",
        "reason_code": "cached",
    }
    previous_calls = len(flow.calls)
    assert matching.claim() is None
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 1
    )
    assert (
        pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
        == original_audit
    )
    assert pg.execute("SELECT refs FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["refs"] == [
        {"kind": "message", "label": label, "synthetic_version": 2} for label in ("M1", "M2")
    ]
    # Legacy snapshots without per-handle coverage retain conservative behavior, including cache reuse.
    flow.legacy_stats, flow.missing_reply_ids = True, []
    flow.stats = {"critical_evidence_omitted": True}
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert matching.claim() is None and len(flow.calls) == previous_calls + 1
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]


def test_auto_link_preserves_rejections_non_owner_links_and_other_account_claims(pg, matching_flow, pilot):
    flow = matching_flow
    flow.catalog_handles = frozenset()
    flow.enable()
    accounts = {handle: uuid4() for handle in ("first.handle", "backup_2")}
    for handle, account in accounts.items():
        pg.execute(
            "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok',%s,%s)",
            (account, handle, handle),
        )
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,'owner','rejected','manual'),(%s,%s,'representative','confirmed','manual')",
        (flow.contact, accounts["first.handle"], flow.contact, accounts["backup_2"]),
    )
    before = _snapshot(pg, ("contact_social_accounts", "creator_account_assignments"))
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert _snapshot(pg, before.keys()) == before
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()
    # The alternate non-owner relation must be protected on the cached-result path too.
    pg.execute(
        "UPDATE contact_social_accounts SET relationship='associated' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, accounts["backup_2"]),
    )
    cached = pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    view = json.loads(creator_data.cipher(flow.settings).decrypt(cached["result_ciphertext"]))
    for link in view["result"]["links"]:
        link.update(decision="supported", relation="self_account", code="self_claim")
    pg.execute(
        "UPDATE creator_handle_jobs SET result_ciphertext=%s WHERE id=%s",
        (creator_data.cipher(flow.settings).encrypt(creator_data.encode(view)), job["id"]),
    )
    before = _snapshot(pg, before.keys())
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert _snapshot(pg, before.keys()) == before
    # Replace only fixture-created blockers: a different owner and a separate active account assignment.
    pg.execute("DELETE FROM contact_social_accounts WHERE contact_id=%s", (flow.contact,))
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
        "VALUES(%s,%s,'owner','confirmed','manual')",
        (pilot.ids["legacy"], accounts["first.handle"]),
    )
    candidate = pg.execute(
        "SELECT id FROM creator_workflow_candidates WHERE contact_id=%s", (pilot.ids["legacy"],)
    ).fetchone()["id"]
    pg.execute(
        "INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,"
        "contact_id,relationship,source,candidate_id,created_by_authorization_id) "
        "VALUES(%s,%s,%s,%s,'associated','cm_confirmation',%s,%s)",
        (uuid4(), accounts["backup_2"], pilot.ids["cm"], pilot.ids["legacy"], candidate, pilot.ids["owner"]),
    )
    before = _snapshot(pg, before.keys())
    flow.material_version += 1
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
        == "review_required"
    )
    assert _snapshot(pg, before.keys()) == before
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()


def test_role_and_chat_events_shared_slots_and_catalog_changes(pg, matching_flow, pilot, monkeypatch, tmp_path):
    flow = matching_flow
    (tmp_path / "handles.csv").write_text("handle\nfirst.handle\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"files": [{"name": "handles.csv", "sha256": "0" * 64}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        matching._load_catalog(str(tmp_path), 0)  # Optional reference must not weaken archive integrity.
    with monkeypatch.context() as reference:
        reference.setattr(matching, "catalog", lambda: matching._load_catalog(str(tmp_path), 0))
        assert matching.reference_catalog() == (frozenset(), "unavailable")
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
    for who in ("legacy", "silent", "cm", "owner"):
        pg.execute(
            """INSERT INTO creator_handle_jobs(contact_id,status,lease_until)
            VALUES(%s,'running',NOW()+INTERVAL '2 minutes')""",
            (pilot.ids[who],),
        )
    assert ai_dispatch.can_claim(pg)  # Four leases still leave one of the five shared slots free.
    pg.execute("SAVEPOINT admission_slots")
    fifth = pilot.person("fifth_slot")
    pg.execute(
        "UPDATE creator_handle_jobs SET status='running',lease_until=NOW()+INTERVAL '2 minutes' WHERE contact_id=%s",
        (fifth,),
    )
    assert matching.claim() is None
    pg.execute("ROLLBACK TO SAVEPOINT admission_slots")
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
    # Curva can also change while the final account/person writer lock is being acquired.
    flow.before_response = None
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    versions = iter(("catalog-v3", "catalog-v3", "catalog-v4"))
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), next(versions)))
    matching.run(job, flow.settings)
    assert len(flow.calls) == 2
    assert pg.execute("SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone() == {
        "status": "queued",
        "result_ciphertext": None,
    }
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert not pg.execute("SELECT 1 FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()

    # History admission upgrades unlinked prior-version contacts, never existing formal owners.
    monkeypatch.setattr(matching, "catalog", lambda: (frozenset({"first.handle", "backup_2"}), "catalog-v4"))
    pg.execute(
        "UPDATE creator_handle_jobs SET stats=jsonb_build_object('prompt_version','retired-prompt'),"
        "created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s",
        (pilot.ids["legacy"],),
    )
    pg.execute(
        "UPDATE creator_handle_jobs SET stats=jsonb_build_object('prompt_version','retired-prompt'),"
        "created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s",
        (pilot.ids["silent"],),
    )
    old_id = pg.execute("SELECT id FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["silent"],)).fetchone()[
        "id"
    ]
    assert matching.enqueue_history(pg, pilot.ids["owner"]) == 1
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["legacy"],)).fetchone()[
            "n"
        ]
        == 1
    )
    assert (
        pg.execute("SELECT count(*) n FROM creator_handle_jobs WHERE contact_id=%s", (pilot.ids["silent"],)).fetchone()[
            "n"
        ]
        == 2
    )
    latest = matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"]
    assert len({row["contact_id"] for row in latest}) == len(latest)
    assert old_id not in {row["id"] for row in latest}
    # Bulk history admission skips formal owners and preserves explicit manual review.
    for handle in ("first.handle", "backup_2"):
        account = uuid4()
        pg.execute(
            "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok',%s,%s)",
            (account, handle, handle),
        )
        pg.execute(
            "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
            "VALUES(%s,%s,'owner','confirmed','excel_ai_verified')",
            (flow.contact, account),
        )
    pg.execute(
        "UPDATE creator_handle_jobs SET status='review_required',source='manual',"
        "stats=jsonb_build_object('prompt_version','retired-prompt') WHERE id=%s",
        (job_id,),
    )
    assert matching.enqueue_history(pg, pilot.ids["owner"]) == 0
    assert matching.prepare_next(flow.settings)  # Only the earlier, unlinked silent history item.
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone() == {
        "status": "review_required",
        "reason_code": "snapshot_changed",
    }
    assert pg.execute("SELECT source FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone()["source"] == "manual"
    assert len(flow.calls) == 2


def test_failed_diagnostics_partial_results_and_queue_cleanup_preserve_data(pg, matching_flow, monkeypatch):
    flow = matching_flow
    flow.enable()
    respond = matching.api.call_openai
    secret = "synthetic private message must never enter diagnostics"
    broken = True

    def reply(*args, **kwargs):
        response = respond(*args, **kwargs)
        value = json.loads(response["text"])
        value["links"][1]["refs"] = [secret]
        if broken:
            value["links"][1]["h"] = "H1"
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", reply)
    job = _prepared_job(pg, flow)
    matching.run(job, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"] == "failed"
    )
    logged = pg.execute("SELECT * FROM creator_handle_requests WHERE job_id=%s", (job["id"],)).fetchone()
    assert logged["error_code"] == "candidates"
    assert (logged["input_tokens"], logged["output_tokens"], logged["billed_tokens"]) == (100, 50, 150)
    assert logged["response_id"] == "resp_synthetic" and logged["request_id"] == "req_synthetic"
    assert secret not in creator_data.cipher(flow.settings).decrypt(logged["response_ciphertext"])
    broken = False
    flow.material_version += 1
    partial = _prepared_job(pg, flow)
    matching.run(partial, flow.settings)
    row = pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (partial["id"],)
    ).fetchone()
    assert row["status"] == "review_required"
    result = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))["result"]
    assert [link["decision"] for link in result["links"]] == ["supported", "review"]
    assert result["links"][1]["validation_error"] == "references"
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    assert not matching.claim()
    # The worker can safely re-use a downgraded cached result without another paid call.
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 2
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_social_accounts", "creator_account_assignments"))
    pg.execute("DELETE FROM creator_handle_jobs WHERE contact_id=%s", (flow.contact,))
    assert _snapshot(pg, before.keys()) == before
    retained = pg.execute(
        "SELECT count(*) n,sum(billed_tokens) tokens FROM creator_handle_requests WHERE job_id IS NULL"
    ).fetchone()
    assert retained == {"n": 2, "tokens": 300}


def test_disabled_probe_retains_paid_diagnostics_when_postcall_catalog_fails(pg, matching_flow, monkeypatch, capsys):
    from scripts import probe_creator_handle_matching as probe

    flow = matching_flow
    flow.enable()
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature=%s", (matching.FEATURE,))
    before = _snapshot(
        pg,
        (
            "contacts",
            "contact_roles",
            "contact_assignments",
            "social_accounts",
            "contact_social_accounts",
            "creator_account_assignments",
        ),
    )

    def execute(sql, params=None):
        # The fixture owns the outer transaction; probe transactions become isolated savepoints.
        if isinstance(sql, str) and sql.startswith("SET TRANSACTION"):
            return None
        return pg.execute(sql, params)

    proxy = SimpleNamespace(execute=execute, commit=lambda: None, transaction=pg.transaction)
    monkeypatch.setattr(probe, "connection", lambda: nullcontext(proxy))
    monkeypatch.setattr(probe, "get_settings", lambda: flow.settings)
    monkeypatch.setattr(sys, "argv", ["probe_creator_handle_matching", "--contact-id", str(flow.contact)])
    catalogs = []

    def catalog():
        catalogs.append(True)
        if len(catalogs) == 3:
            raise OSError("synthetic postcall catalog failure")
        return frozenset({"first.handle", "backup_2"}), "catalog-v1"

    monkeypatch.setattr(matching, "catalog", catalog)
    try:
        assert probe.main() == 0
    finally:
        pg.execute("SELECT pg_advisory_unlock(%s)", (ai_dispatch._ADMISSION_LOCK,))
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["evidence_fresh"] is False and output[0]["evidence_eligible_candidates"] == []
    assert output[-1]["business_links_written"] == 0 and output[-1]["matching_enabled_by_probe"] is False
    assert len(flow.calls) == 1 and len(catalogs) == 3
    logged = pg.execute("SELECT * FROM creator_handle_requests").fetchone()
    assert logged["job_id"] is None
    assert (logged["input_tokens"], logged["output_tokens"], logged["billed_tokens"]) == (100, 50, 150)
    assert logged["response_id"] == "resp_synthetic" and logged["request_id"] == "req_synthetic"
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(logged["response_ciphertext"]))
    assert diagnostic["probe"]["contact_id"] == str(flow.contact) and diagnostic["validated"]["links"]
    assert _snapshot(pg, before.keys()) == before
    assert not pg.execute("SELECT enabled FROM ai_feature_configs WHERE feature=%s", (matching.FEATURE,)).fetchone()[
        "enabled"
    ]
    assert not pg.execute("SELECT 1 FROM creator_handle_jobs").fetchone()


def test_fixed_group_context_auto_links_with_live_scope_and_auditable_title(pg, matching_flow, pilot, monkeypatch):
    flow = matching_flow
    flow.catalog_handles = frozenset()  # A dedicated group can prove an account absent from Curva.
    def unavailable_catalog():
        raise OSError("synthetic reference unavailable")

    monkeypatch.setattr(matching, "catalog", unavailable_catalog)
    flow.enable()
    monkeypatch.setattr(matching.evidence, "build", flow.real_build)
    monkeypatch.setattr(matching.evidence, "still_valid", flow.real_still_valid)
    title = "❤️ first.handle X TT Media"
    body = "My account was accessed in US. Now all my shop links are gone."
    cipher = message_body_cipher(flow.settings.token_encryption_key)
    pg.execute("UPDATE chats SET name=%s,chat_mode='group' WHERE chat_id='chat_creator'", (title,))
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='chat_creator'",
        (cipher.encrypt(json.dumps({"text": "Thanks for the update"})),),
    )
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s,message_create_time=NOW() "
        "WHERE chat_id='chat_creator' AND message_id='creator_0_question'",
        (cipher.encrypt(json.dumps({"text": body})),),
    )
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"))
    respond = matching.api.call_openai

    def contextual_response(*args, **kwargs):
        response = respond(*args, **kwargs)
        pack, value = args[2], json.loads(response["text"])
        reference = next(m["id"] for m in pack["messages"] if m["speaker"] == pack["target"] and m["text"] == body)
        for link in value["links"]:
            link.update(code="group_context", refs=[reference])
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", contextual_response)
    pg.execute("SAVEPOINT ordinary_group_name")
    pg.execute("UPDATE chats SET name='MAC Creators' WHERE chat_id='chat_creator'")
    ordinary = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles)
    assert ordinary["reason"] == "no_candidates" and ordinary["pack"] is None
    pg.execute("ROLLBACK TO SAVEPOINT ordinary_group_name")
    material = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles)
    assert material["reason"] == "ready" and [h["handle"] for h in material["pack"]["handles"]] == ["first.handle"]
    assert material["pack"]["handles"][0]["catalog_verified"] is False
    assert all("first.handle" not in m["text"] for m in material["pack"]["messages"])

    # A title edited during a paid response invalidates the old context, but keeps the usage ledger.
    job = _prepared_job(pg, flow, "incremental")
    flow.before_response = lambda: pg.execute(
        "UPDATE chats SET name='Other collaboration' WHERE chat_id='chat_creator'"
    )
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)
    ).fetchone() == {"status": "queued", "result_ciphertext": None}
    assert pg.execute(
        "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
    ).fetchone()["billed_tokens"] == 150
    flow.before_response = None
    pg.execute("UPDATE chats SET name=%s WHERE chat_id='chat_creator'", (title,))

    # Losing the actual readable grant before calling prevents a second paid request and any relation.
    job = _prepared_job(pg, flow, "incremental")
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (pilot.ids["cm"],))
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == []
    assert pg.execute(
        "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (job["reservation_id"],)
    ).fetchone()["billed_tokens"] == 0
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (pilot.ids["cm"],))

    job = _prepared_job(pg, flow, "incremental")
    matching.run(job, flow.settings)
    assert len(flow.calls) == 2 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    saved = pg.execute("SELECT status,stats FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    assert saved["status"] == "completed" and saved["stats"]["auto_linked_handles"] == ["first.handle"]
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()[
        "metadata"
    ]
    reference = next(m["id"] for m in job["pack"]["messages"] if m["text"] == body)
    chat_ref = next(ref for ref in job["refs"] if ref["kind"] == "chat" and ref["chat_id"] == "chat_creator")
    assert audit["refs"] == [reference] and audit["prompt_version"] == matching.evidence.PROMPT_VERSION
    assert chat_ref in audit["evidence_refs"]
    assert any(ref["kind"] == "message" and ref["label"] == reference for ref in audit["evidence_refs"])
    assert all("text" not in ref and "body" not in ref for ref in audit["evidence_refs"])
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"])
    assert [item["handle"] for item in row["handle_rows"]] == ["first.handle"]
    assert row["handle_rows"][0]["associated"] and row["handle_rows"][0]["check"] is None
    assert row["suggestions"][0]["handle"] == "first.handle" and row["suggestions"][0]["decision"] == "supported"
    assert _snapshot(pg, before.keys()) == before

    # The same contextual signal from another person is a claimant conflict, not a second owner.
    pg.execute("SAVEPOINT group_competing_owner")
    pilot.person("group_peer")
    pg.execute(
        "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,body_ciphertext,"
        "message_create_time) VALUES('chat_creator','group_peer_control','ou_group_peer','user','text',%s,NOW())",
        (cipher.encrypt(json.dumps({"text": "My account login has a 2FA problem"})),),
    )
    pg.execute(
        "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
        "VALUES(%s,'chat_creator','group_peer_control')",
        (pilot.ids["cm"],),
    )
    competing = flow.real_build(pg, flow.settings, flow.contact, flow.catalog_handles, recheck=True)
    reference = next(m["id"] for m in competing["pack"]["messages"] if m["text"] == body)
    result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": competing["pack"]["handles"][0]["id"], "decision": "supported", "relation": "self_account",
         "period": "at_message", "code": "group_context", "refs": [reference], "needed": []}]}
    reviewed = matching.evidence.validate(competing["pack"], result)["links"][0]
    assert reviewed == result["links"][0]  # Competing chat text is judged by AI, not backend regex.
    assert contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == ["first.handle"]
    pg.execute("ROLLBACK TO SAVEPOINT group_competing_owner")


def test_formal_pairs_skip_paid_matching_without_freezing_new_handles_or_manual_review(
    pg, matching_flow, pilot, monkeypatch
):
    flow = matching_flow
    flow.enable()
    monkeypatch.setattr(matching.evidence, "build", flow.real_build)
    monkeypatch.setattr(matching.evidence, "still_valid", flow.real_still_valid)
    respond = matching.api.call_openai

    def real_references(*args, **kwargs):
        response = respond(*args, **kwargs)
        pack = args[2]
        value = json.loads(response["text"])
        by_handle = {h["id"]: h["handle"] for h in pack["handles"]}
        for link in value["links"]:
            link["refs"] = [
                next(
                    m["id"]
                    for m in pack["messages"]
                    if m["speaker"] == pack["target"] and by_handle[link["h"]] in m["text"]
                )
            ]
        response["text"] = json.dumps(value)
        return response

    monkeypatch.setattr(matching.api, "call_openai", real_references)
    before = _snapshot(pg, ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments"))
    cipher = message_body_cipher(flow.settings.token_encryption_key)
    start = datetime.now(UTC)

    def message(handle, minute, body=None):
        message_id = f"{handle}-{minute}"
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
            "body_ciphertext,message_create_time) VALUES('chat_creator',%s,'ou_creator','user','text',%s,%s)",
            (
                message_id,
                cipher.encrypt(json.dumps({"text": body or "My TikTok account is " + handle})),
                start + timedelta(minutes=minute),
            ),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
            "VALUES(%s,'chat_creator',%s)",
            (pilot.ids["cm"], message_id),
        )

    first = uuid4()
    pg.execute(
        "INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
        "VALUES(%s,'tiktok','first.handle','first.handle')",
        (first,),
    )
    pg.execute(
        "INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,"
        "reviewed_by_authorization_id,reviewed_at) "
        "VALUES(%s,%s,'owner','confirmed','owner_reviewed_curva',%s,NOW())",
        (flow.contact, first, pilot.ids["owner"]),
    )
    message("first.handle", 0)
    message("backup_2", 1)
    existing_pairs = _snapshot(pg, ("contact_social_accounts",))
    all_confirmed = _enqueue(pg, flow.contact, "history")
    with monkeypatch.context() as no_material:
        no_material.setattr(matching.evidence, "build",
                            lambda *_a, **_k: pytest.fail("Linked history needs no material"))
        assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute(
        "SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (all_confirmed,)
    ).fetchone() == {"status": "completed", "reason_code": "already_confirmed"}
    assert not flow.calls and not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
    page = matching.page_data(pg, pilot.ids["owner"], flow.settings)
    assert page["jobs"][0]["associations"] == [{"handle": "first.handle", "human_confirmed": True}]
    assert page["jobs"][0]["suggestions"] == []
    assert {r["status"]: r["n"] for r in page["counts"]} == {"completed": 1}
    assert _snapshot(pg, existing_pairs.keys()) == existing_pairs

    # A formal association gained after preparation/admission closes history before networking.
    for after_claim in (False, True):
        pg.execute("SAVEPOINT late_formal")
        pg.execute("UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s", (flow.contact,))
        late = _enqueue(pg, flow.contact, "history")
        assert matching.prepare_next(flow.settings)
        running = matching.claim() if after_claim else None
        pg.execute("UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s", (flow.contact,))
        if running:
            matching.run(running, flow.settings)
            assert pg.execute("SELECT billed_tokens FROM creator_handle_requests WHERE id=%s",
                              (running["reservation_id"],)).fetchone()["billed_tokens"] == 0
        else:
            assert matching.claim() is None
            assert not pg.execute("SELECT 1 FROM creator_handle_requests").fetchone()
        assert not flow.calls
        assert pg.execute("SELECT status,reason_code,claim_token,lease_until FROM creator_handle_jobs WHERE id=%s",
                          (late,)).fetchone() == {"status": "completed", "reason_code": "already_confirmed",
                                                "claim_token": None, "lease_until": None}
        pg.execute("ROLLBACK TO SAVEPOINT late_formal")

    # The rollout repairs existing latest history jobs without rewriting business/billing records.
    pg.execute("SAVEPOINT history_rollout")
    pg.execute("UPDATE creator_handle_jobs SET status='queued',result_ciphertext='preserved-old-result',"
               "input_tokens=123 WHERE id=%s", (all_confirmed,))
    protected = _snapshot(pg, (*before.keys(), "contact_social_accounts", "creator_handle_requests"))
    migration = Path(__file__).parents[1] / "migrations/079_complete_linked_creator_history.sql"
    pg.execute(migration.read_text())
    assert pg.execute("SELECT status,reason_code,result_ciphertext,input_tokens FROM creator_handle_jobs WHERE id=%s",
                      (all_confirmed,)).fetchone() == {"status": "completed", "reason_code": "already_confirmed",
                                                     "result_ciphertext": "preserved-old-result", "input_tokens": 123}
    assert _snapshot(pg, protected.keys()) == protected
    page = matching.page_data(pg, pilot.ids["owner"], flow.settings)
    assert page["jobs"][0]["suggestions"] == [] and page["jobs"][0]["validation_error"] is None
    assert {r["status"]: r["n"] for r in page["counts"]} == {"completed": 1}
    pg.execute("UPDATE creator_handle_jobs SET source='manual',status='queued' WHERE id=%s", (all_confirmed,))
    obsolete = uuid4()
    pg.execute("INSERT INTO creator_handle_jobs(id,contact_id,source,status,created_at) "
               "VALUES(%s,%s,'history','failed',NOW()-INTERVAL '1 minute')", (obsolete, flow.contact))
    pg.execute(migration.read_text())
    assert pg.execute("SELECT status,source FROM creator_handle_jobs WHERE id=%s", (all_confirmed,)).fetchone() == {
        "status": "queued", "source": "manual"}
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s",
                      (obsolete,)).fetchone()["status"] == "failed"
    assert pg.execute("SELECT metadata->>'jobs_completed' n FROM audit_events "
                      "WHERE action='creator_handle_history_associations_reused'").fetchall() == [{"n": "1"}]
    pg.execute("ROLLBACK TO SAVEPOINT history_rollout")
    pg.execute("UPDATE creator_handle_jobs SET created_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (all_confirmed,))

    # The formal exact pair is excluded, not the entire contact.
    job = _prepared_job(pg, flow, "incremental")
    assert [h["handle"] for h in job["pack"]["handles"]] == ["backup_2"]
    assert job["stats"]["prompt_version"] == matching.evidence.PROMPT_VERSION
    matching.run(job, flow.settings)
    assert len(flow.calls) == 1 and contact_handles.for_contacts(pg, [flow.contact])[flow.contact] == [
        "backup_2",
        "first.handle",
    ]
    pairs = {row["handle"]: row for row in contact_handles.confirmed_pairs(pg, [flow.contact])}
    assert pairs["first.handle"]["human_confirmed"] and not pairs["backup_2"]["human_confirmed"]
    assert pairs["backup_2"]["source"] == "creator_handle_auto_verified"
    pg.execute(
        "UPDATE contact_social_accounts SET source='excel_ai_verified',reviewed_by_authorization_id=%s,"
        "reviewed_at=NOW() WHERE contact_id=%s AND social_account_id=(SELECT id FROM social_accounts "
        "WHERE platform='tiktok' AND normalized_handle='backup_2')",
        (pilot.ids["owner"], flow.contact),
    )
    assert not next(row for row in contact_handles.confirmed_pairs(pg, [flow.contact]) if row["handle"] == "backup_2")[
        "human_confirmed"
    ]
    assert flow.real_build(pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}))["reason"] == (
        "already_confirmed"
    )
    assert {
        h["handle"]
        for h in flow.real_build(
            pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}), recheck=True
        )["pack"]["handles"]
    } == {"first.handle", "backup_2"}
    pg.execute(
        "UPDATE contact_social_accounts SET source='creator_handle_auto_verified' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    assert not next(row for row in contact_handles.confirmed_pairs(pg, [flow.contact]) if row["handle"] == "backup_2")[
        "human_confirmed"
    ]
    pairs_before = _snapshot(pg, ("contact_social_accounts",))
    automatic = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (automatic,)).fetchone() == {
        "status": "completed",
        "reason_code": "already_confirmed",
    }
    # Skipping does not invent a human review or rewrite source.
    assert _snapshot(pg, pairs_before.keys()) == pairs_before
    pg.execute(
        "UPDATE creator_handle_jobs SET created_at=NOW()-INTERVAL '1 minute' WHERE contact_id=%s", (flow.contact,)
    )

    # Explicit manual review includes existing pairs, but the DB does not add duplicate links or audit events.
    job = _prepared_job(pg, flow)
    assert {h["handle"] for h in job["pack"]["handles"]} == {"first.handle", "backup_2"}
    assert job["stats"]["manual_recheck"] and set(job["stats"]["recheck_handles"]) == {"first.handle", "backup_2"}
    matching.run(job, flow.settings)
    assert len(flow.calls) == 2 and _snapshot(pg, pairs_before.keys()) == pairs_before
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handle_auto_linked'").fetchone()["n"] == 1
    )

    # Formal relationships and this round's AI conclusion are independent page fields.
    saved = pg.execute("SELECT result_ciphertext FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()
    view = json.loads(creator_data.cipher(flow.settings).decrypt(saved["result_ciphertext"]))
    link = view["result"]["links"][0]
    link.update(
        decision="review", relation="uncertain", period="unknown", code="no_link_evidence", needed=["account_context"]
    )
    for code, validation_error, conflict in (
        ("no_link_evidence", None, False),
        ("conflict", None, False),
        ("no_link_evidence", "counterevidence", False),
    ):
        link["code"] = code
        if validation_error:
            link["validation_error"] = validation_error
        else:
            link.pop("validation_error", None)
        pg.execute(
            "UPDATE creator_handle_jobs SET result_ciphertext=%s,status='review_required' WHERE id=%s",
            (creator_data.cipher(flow.settings).encrypt(creator_data.encode(view)), job["id"]),
        )
        row = next(
            row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
        )
        assert {p["handle"]: p["human_confirmed"] for p in row["associations"]} == {
            "first.handle": True,
            "backup_2": False,
        }
        assert row["suggestions"][0]["decision"] == "review" and row["suggestions"][0]["conflict"] is conflict
        assert "associated" not in row["suggestions"][0]
        assert len(row["handle_rows"]) == 2
        assert {item["handle"] for item in row["handle_rows"]} == {"first.handle", "backup_2"}
        assert all(item["associated"] and item["check"] is not None for item in row["handle_rows"])
        assert "display_status" not in row  # Explicit manual review is never silently presented as completed.

    # Historical weak reproof is hidden on the existing handle row, not duplicated or shown as a false conflict.
    pg.execute("UPDATE creator_handle_jobs SET source='history' WHERE id=%s", (job["id"],))
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
    )
    assert len(row["handle_rows"]) == 2 and all(
        item["associated"] and item["check"] is None and not item["conflict"] for item in row["handle_rows"]
    )
    assert row["display_status"] == "completed" and row["display_reason_code"] == "already_confirmed"
    assert pg.execute("SELECT status FROM creator_handle_jobs WHERE id=%s", (job["id"],)).fetchone()["status"] == (
        "review_required"
    )  # Derived presentation does not rewrite the paid historical outcome.
    # Opening a history page does not reinterpret denial keywords as a verified database conflict.
    pg.execute("SAVEPOINT actual_counter")
    message("first.handle", 3, "first.handle is not my account")
    material = flow.real_build(pg, flow.settings, flow.contact, frozenset({"first.handle", "backup_2"}), recheck=True)
    denial = next(m["id"] for m in material["pack"]["messages"] if "not my account" in m["text"])
    counter_view = {
        "handles": {h["id"]: h["handle"] for h in material["pack"]["handles"]},
        "result": {
            "creator": {"state": "known", "refs": []},
            "links": [
                {
                    "h": h["id"],
                    "decision": "review",
                    "relation": "uncertain",
                    "period": "unknown",
                    "code": "no_link_evidence",
                    "refs": [denial] if h["handle"] == "first.handle" else [],
                    "needed": ["account_context"],
                }
                for h in material["pack"]["handles"]
            ],
        },
    }
    pg.execute(
        "UPDATE creator_handle_jobs SET pack_ciphertext=%s,result_ciphertext=%s,refs=%s::jsonb WHERE id=%s",
        (
            creator_data.cipher(flow.settings).encrypt(creator_data.encode(material["pack"])),
            creator_data.cipher(flow.settings).encrypt(creator_data.encode(counter_view)),
            creator_data.encode(material["refs"]),
            job["id"],
        ),
    )
    row = next(
        row for row in matching.page_data(pg, pilot.ids["owner"], flow.settings)["jobs"] if row["id"] == job["id"]
    )
    assert not next(item for item in row["handle_rows"] if item["handle"] == "first.handle")["conflict"]
    assert next(item for item in row["handle_rows"] if item["handle"] == "backup_2")["check"] is None
    pg.execute("ROLLBACK TO SAVEPOINT actual_counter")

    # Any formal pair gained before admission invalidates the automatic snapshot before billing.
    message("backup_2", 2)
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    job_id = _enqueue(pg, flow.contact, "incremental")
    assert matching.prepare_next(flow.settings)
    pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    assert matching.claim() is None and len(flow.calls) == 2
    assert pg.execute("SELECT count(*) n FROM creator_handle_requests").fetchone()["n"] == 2
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (job_id,)).fetchone() == {
        "status": "completed",
        "reason_code": "already_confirmed",
    }

    # Removing a formal pair after admission is fenced before networking, even when it is another handle.
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    ready = _prepared_job(pg, flow, "incremental")
    assert [h["handle"] for h in ready["pack"]["handles"]] == ["backup_2"]
    pg.execute(
        "UPDATE contact_social_accounts SET status='rejected' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, first),
    )
    matching.run(ready, flow.settings)
    assert len(flow.calls) == 2
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (ready["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 0
    )
    pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s AND social_account_id=%s",
        (flow.contact, first),
    )
    # The same full-set fence after networking retains usage without publishing a stale result.
    after_network = _prepared_job(pg, flow, "incremental")
    flow.before_response = lambda: pg.execute(
        "UPDATE contact_social_accounts SET status='confirmed' WHERE contact_id=%s "
        "AND social_account_id=(SELECT id FROM social_accounts WHERE normalized_handle='backup_2')",
        (flow.contact,),
    )
    matching.run(after_network, flow.settings)
    assert len(flow.calls) == 3
    assert (
        pg.execute(
            "SELECT billed_tokens FROM creator_handle_requests WHERE id=%s", (after_network["reservation_id"],)
        ).fetchone()["billed_tokens"]
        == 150
    )
    assert pg.execute(
        "SELECT status,result_ciphertext FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)
    ).fetchone() == {"status": "queued", "result_ciphertext": None}
    assert _snapshot(pg, pairs_before.keys()) == pairs_before
    assert _snapshot(pg, before.keys()) == before
    # An explicitly restricted contact stops at waiting_evidence, not an endless snapshot-changed requeue.
    monkeypatch.setattr(matching.evidence, "RESTRICTED_CONTACTS", frozenset({str(flow.contact)}))
    assert matching.prepare_next(flow.settings)
    assert pg.execute(
        "SELECT status,reason_code FROM creator_handle_jobs WHERE id=%s", (after_network["id"],)
    ).fetchone() == {"status": "waiting_evidence", "reason_code": "restricted"}
    assert not matching.prepare_next(flow.settings) and len(flow.calls) == 3
