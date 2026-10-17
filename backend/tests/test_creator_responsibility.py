"""Formal multi-CM matching: synthetic provider, isolated database, fenced writes."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cap_fixtures import _snapshot
from cap_fixtures import pilot as parent_pilot
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from psycopg.errors import DeadlockDetected, DivisionByZero, RaiseException

from app import ai_dispatch, ai_feature_config, assistant_rules, config, creator_data, openai_client
from app import creator_handle_evidence as handle_evidence
from app import creator_matching_routes as routes
from app import creator_responsibility_evidence as evidence
from app import creator_responsibility_matching as matching
from app.crypto import message_body_cipher, message_body_fingerprint

pilot = parent_pilot


def _suggestion(cm, refs, **changes):
    return {
        "cm": cm,
        "decision": "strong_candidate",
        "confidence": 0.99,
        "relation_level": "person",
        "period": "current",
        "scope": ["account_ops"],
        "scope_note": "Follow-up for this creator's campaign",
        "support_refs": refs,
        "counter_refs": [],
        "reason": "Sustained, creator-specific business follow-up",
        **changes,
    }


def _internal(pg, label, *, connected=False):
    identifier = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (identifier, label))
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) "
        "VALUES(%s,%s,%s,'internal')",
        ("ou_" + label, identifier, label),
    )
    if connected:
        pg.execute(
            "INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,body_consent_at) "
            "VALUES(%s,%s,%s,'test-token',NOW()-INTERVAL '1 day')",
            (uuid4(), "ou_" + label, label),
        )
    return identifier


def test_shared_evidence_uses_connected_cm_roster_and_keeps_other_staff_as_context(pg, pilot, monkeypatch):
    """Only connected CM/AMs are candidates; their merged aliases do not widen message access."""
    creator, cm = pilot.ids["creator"], pilot.ids["cm"]
    pg.execute("UPDATE lark_identities SET tenant_classification='internal' WHERE open_id='ou_cm'")
    helper = _internal(pg, "helper")
    second_cm = _internal(pg, "second_cm", connected=True)
    pg.execute("UPDATE lark_identities SET tenant_classification='unresolved' WHERE open_id='ou_second_cm'")
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES('ou_second_cm_alias',%s,'CM alias')",
        (second_cm,),
    )
    unrelated = _internal(pg, "unrelated")
    cipher = message_body_cipher(pilot.settings.token_encryption_key)
    start = datetime.now(UTC)

    def message(label, sender, body, *, chat="chat_creator", parent=None, observed=True):
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
            "body_ciphertext,message_create_time,parent_id) VALUES(%s,%s,%s,'user','text',%s,%s,%s)",
            (chat, label, sender, cipher.encrypt(json.dumps({"text": body})), start, parent),
        )
        if observed:
            pg.execute(
                "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,%s,%s)",
                (cm, chat, label),
            )

    message("creator_new", "ou_creator", "Can you help me arrange tomorrow's live campaign?")
    message("helper_new", "ou_helper", "I will coordinate the live campaign with the brand.", parent="creator_new")
    message("creator_reply", "ou_creator", "Thank you, I will send you the completed video.", parent="helper_new")
    message("second_cm_new", "ou_second_cm_alias", "I will arrange your live campaign.", parent="creator_new")
    message("second_creator_reply", "ou_creator", "Please send me the live campaign plan.", parent="second_cm_new")
    message("unobserved", "ou_unrelated", "UNOBSERVED SECRET", observed=False)
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('unrelated_private','Unrelated private','p2p')")
    message("unrelated_secret", "ou_helper", "UNAUTHORIZED PRIVATE SECRET", chat="unrelated_private", observed=False)
    restricted = UUID(next(iter(handle_evidence.RESTRICTED_CONTACTS)))
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'restricted')", (restricted,))
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES('ou_restricted',%s,'restricted')",
        (restricted,),
    )
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
        (restricted,),
    )
    message("restricted_secret", "ou_restricted", "RESTRICTED SECRET")
    material = evidence.build(pg, pilot.settings, creator)
    assert material["pack"] and material["pack"]["creator_known"] is True
    by_id = {UUID(str(identifier)): alias for alias, identifier in material["candidate_ids"].items()}
    assert {cm, second_cm} <= by_id.keys() and helper not in by_id and unrelated not in by_id
    candidates = {c["id"]: c for c in material["pack"]["candidates"]}
    assert all("authorized" not in candidate for candidate in candidates.values())
    assert any(
        m["text"] == "I will coordinate the live campaign with the brand." and m["speaker"] not in candidates
        for m in material["pack"]["messages"]
    )
    assert any(
        m["text"] == "I will arrange your live campaign." and m["speaker"] == by_id[second_cm]
        for m in material["pack"]["messages"]
    )
    encoded = creator_data.encode(material["pack"])
    assert "UNOBSERVED SECRET" not in encoded
    assert "UNAUTHORIZED PRIVATE SECRET" not in encoded
    assert "RESTRICTED SECRET" not in encoded
    assert len(material["pack"]["messages"]) <= evidence.MAX_MESSAGES
    assert sum(len(row["text"]) for row in material["pack"]["messages"]) <= evidence.MAX_CHARS
    queries = []

    class Meter:
        def execute(self, sql, *args, **kwargs):
            queries.append(sql)
            return pg.execute(sql, *args, **kwargs)

    assert evidence.still_valid(Meter(), material["refs"], pilot.settings)
    assert sum(sql.startswith("SELECT " + handle_evidence._SPEAKER_COLUMNS) for sql in queries) == 1
    with monkeypatch.context() as no_plaintext:
        no_plaintext.setattr(handle_evidence.text, "_decrypt_body", lambda *_: pytest.fail("list must not decrypt"))
        assert evidence.still_valid(pg, material["refs"], pilot.settings, lightweight=True)
    pg.execute("SAVEPOINT changed_body")
    original = pg.execute("SELECT body_ciphertext FROM chat_messages WHERE message_id='helper_new'").fetchone()
    replacement = cipher.encrypt(cipher.decrypt(original["body_ciphertext"]))
    assert replacement != original["body_ciphertext"]
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='helper_new'", (replacement,))
    assert evidence.still_valid(pg, material["refs"], pilot.settings, lightweight=True)
    assert evidence.still_valid(pg, material["refs"], pilot.settings)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='helper_new'",
               (cipher.encrypt(json.dumps({"text": "Changed business responsibility"})),))
    assert not evidence.still_valid(pg, material["refs"], pilot.settings, lightweight=True)
    assert not evidence.still_valid(pg, material["refs"], pilot.settings)
    pg.execute("ROLLBACK TO SAVEPOINT changed_body")
    assert evidence.build(pg, pilot.settings, restricted)["pack"] is None
    pg.execute("SAVEPOINT revoked_permission")
    pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (cm,))
    assert not evidence.still_valid(pg, material["refs"], pilot.settings)
    assert not evidence.still_valid(pg, material["refs"], pilot.settings, lightweight=True)
    pg.execute("ROLLBACK TO SAVEPOINT revoked_permission")
    for change in ("body_consent_at=NULL", "status='revoked'"):
        pg.execute("SAVEPOINT candidate_revoked")
        pg.execute(f"UPDATE authorizations SET {change} WHERE open_id='ou_second_cm'")
        assert not evidence.still_valid(pg, material["refs"], pilot.settings)
        assert not evidence.still_valid(pg, material["refs"], pilot.settings, lightweight=True)
        revoked = evidence.build(pg, pilot.settings, creator)
        assert str(second_cm) not in revoked["candidate_ids"].values()
        pg.execute("ROLLBACK TO SAVEPOINT candidate_revoked")
    pg.execute("SAVEPOINT candidate_identity")
    pg.execute("UPDATE lark_identities SET identity_type='bot' WHERE open_id='ou_second_cm_alias'")
    assert not evidence.still_valid(pg, material["refs"], pilot.settings)
    pg.execute("ROLLBACK TO SAVEPOINT candidate_identity")
    pg.execute("SAVEPOINT assignment_change")
    pg.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) VALUES(%s,%s,%s,'manual')",
        (uuid4(), creator, cm),
    )
    assert not evidence.still_valid(pg, material["refs"], pilot.settings)
    refreshed = evidence.build(pg, pilot.settings, creator)
    assert next(c for c in refreshed["pack"]["candidates"] if c["id"] == by_id[cm])["known_assignment"]
    assert any(a["cm"] == by_id[cm] for a in refreshed["pack"]["existing_assignments"])
    second_authorization = pg.execute("SELECT id FROM authorizations WHERE open_id='ou_second_cm'").fetchone()["id"]
    pg.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
        "VALUES(%s,%s,%s,'manual')", (uuid4(), creator, second_authorization),
    )
    scoped = evidence.build(pg, pilot.settings, creator)
    assert {a["cm"] for a in scoped["pack"]["existing_assignments"]} == {by_id[cm], by_id[second_cm]}
    scopes = matching._existing_assignments(pg, [creator])[str(creator)]
    assert {row["scope_label"] for row in scopes} == {"达人本人"} and len(scopes) == 2
    pg.execute("DELETE FROM contact_assignments WHERE contact_id=%s AND responsible_authorization_id=%s",
               (creator, cm))
    historical_candidate = uuid4()
    pg.execute("INSERT INTO creator_workflow_candidates(id,contact_id) VALUES(%s,%s)",
               (historical_candidate, creator))
    pg.execute("""INSERT INTO creator_account_assignments(id,responsible_authorization_id,contact_id,
        relationship,source,candidate_id,created_by_authorization_id,ended_at)
        VALUES(%s,%s,%s,'owner','cm_confirmation',%s,%s,NOW())""",
        (uuid4(), cm, creator, historical_candidate, pilot.ids["owner"]))
    assignment_facts = _snapshot(pg, ("contact_assignments", "creator_account_assignments"))
    with pytest.raises(RaiseException, match="Migrate active account-level"), pg.transaction():
        account = uuid4()
        pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
                   "VALUES(%s,'tiktok','retirement_guard','retirement_guard')", (account,))
        pg.execute("UPDATE creator_account_assignments SET social_account_id=%s,ended_at=NULL WHERE contact_id=%s",
                   (account, creator))
        pg.execute((Path(__file__).parents[1] / "migrations/105_retire_legacy_creator_assignments.sql").read_text())
    # Real source fingerprints remain valid after OUR additions, not after unrelated manual changes.
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: pilot.settings)
    ai_feature_config.save_feature_config(
        pg,
        ai_feature_config.api_key_cipher(pilot.settings.token_encryption_key),
        matching.FEATURE,
        provider="openai",
        project_id="test-project",
        model="test-model",
        enabled=True,
        auto_confirm_threshold=0.95,
        api_key="sk-synthetic-only",
        updated_by_authorization_id=pilot.ids["owner"],
    )

    def respond(_config, _key, pack, _contact, **_kwargs):
        return {
            "text": json.dumps(
                {
                    "suggestions": [
                        _suggestion(
                            candidate["id"],
                            [m["id"] for m in pack["messages"] if m["speaker"] == candidate["id"]][:8],
                            decision="responsible",
                        )
                        for candidate in pack["candidates"]
                    ]
                }
            ),
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "telemetry": {},
            "response_id": "resp_real_evidence_synthetic",
        }

    monkeypatch.setattr(matching.api, "call_openai", respond)
    _enqueue(pg, creator)
    assert matching.prepare_next(pilot.settings)
    job = matching.claim()
    matching.run(job, pilot.settings)
    applied = pg.execute("SELECT refs FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)).fetchone()
    assert (
        pg.execute(
            "SELECT count(*) n FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL", (creator,)
        ).fetchone()["n"]
        == 1
    )  # Keep the other CM; never restore a manually ended responsibility, even from the retired table.
    assert _snapshot(pg, assignment_facts.keys()) == assignment_facts
    assert evidence.still_valid(pg, applied["refs"], pilot.settings)
    assert evidence.still_valid(pg, applied["refs"], pilot.settings, lightweight=True)
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s AND role='creator'", (creator,))
    assert not evidence.still_valid(pg, material["refs"], pilot.settings)
    assert evidence.build(pg, pilot.settings, creator)["pack"] is None


def test_late_reply_context_includes_its_readable_original_without_widening_access(pg, pilot, monkeypatch):
    cipher = message_body_cipher(pilot.settings.token_encryption_key)
    for key, sender, age, parent in (
        ('context_question', 'ou_cm', 180, None),
        ('context_reply', 'ou_cm', 120, 'context_question'),
        ('context_creator', 'ou_creator', 60, None),
        ('context_latest', 'ou_cm', 0, None),
    ):
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
            body_ciphertext,message_create_time,parent_id) VALUES('chat_creator',%s,%s,'user','text',%s,
            NOW()-(%s*INTERVAL '1 second'),%s)""",
            (key, sender, cipher.encrypt(json.dumps({'text': key + ': campaign payment follow-up'})), age, parent))
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,'chat_creator',%s)", (pilot.ids['cm'], key))
    monkeypatch.setattr(evidence, 'SEARCH_LIMIT', 1)
    protected = _snapshot(pg, ('contact_roles', 'contact_assignments', 'contact_social_accounts'))
    material = evidence.build(pg, pilot.settings, pilot.ids['creator'])
    labels = {ref['message_id']: ref['label'] for ref in material['refs'] if ref['kind'] == 'message'}
    reply = next(m for m in material['pack']['messages'] if m['id'] == labels['context_reply'])
    assert reply['reply'] == labels['context_question']
    assert reply['id'] not in material['pack']['coverage']['missing_reply_ids']
    assert len(material['pack']['messages']) <= evidence.MAX_MESSAGES
    assert sum(len(m['text']) for m in material['pack']['messages']) <= evidence.MAX_CHARS
    pg.execute("DELETE FROM authorization_message_observations WHERE authorization_id=%s "
               "AND message_id='context_question'", (pilot.ids['cm'],))
    limited = evidence.build(pg, pilot.settings, pilot.ids['creator'])
    refs = {ref['message_id']: ref['label'] for ref in limited['refs'] if ref['kind'] == 'message'}
    assert 'context_question' not in refs
    assert refs['context_reply'] in limited['pack']['coverage']['missing_reply_ids']
    assert _snapshot(pg, protected.keys()) == protected


def test_factual_validation_preserves_independent_cm_results_without_lexical_veto():
    pack = {
        "target": "U1",
        "creator_known": True,
        "candidates": [
            {"id": "CM1", "name": "one", "known_assignment": False},
            {"id": "CM2", "name": "two", "known_assignment": False},
        ],
        "existing_assignments": [],
        "people": [
            {"id": "U1", "role": "creator"},
            {"id": "CM1", "role": "internal"},
            {"id": "CM2", "role": "internal"},
        ],
        "chats": [{"id": "G1", "mode": "group", "name": "Business cooperation"}],
        "messages": [
            {
                "id": "M1",
                "chat": "G1",
                "speaker": "U1",
                "reply": None,
                "previous": None,
                "at": "2026-10-01T10:00:00+00:00",
                "mentions": [],
                "text": "The campaign goes live tomorrow; I need your help.",
            },
            {
                "id": "M2",
                "chat": "G1",
                "speaker": "CM1",
                "reply": "M1",
                "previous": "M1",
                "at": "2026-10-01T10:01:00+00:00",
                "mentions": [],
                "text": "I'll handle the sample request and commission adjustment.",
            },
            {
                "id": "M3",
                "chat": "G1",
                "speaker": "CM2",
                "reply": "M1",
                "previous": "M2",
                "at": "2026-10-01T10:02:00+00:00",
                "mentions": [],
                "text": "I'll coordinate your live schedule with the brand.",
            },
        ],
        "coverage": {"full_history": False},
    }
    result = {
        "suggestions": [_suggestion("CM1", ["M1", "M2"]), _suggestion("CM2", ["M1", "M3"], decision="responsible")]
    }
    assert evidence.validate(pack, result) == result  # No prescribed 'I am your manager' wording.
    broken = deepcopy(result)
    broken["suggestions"][1]["support_refs"] = ["NOT_REAL"]
    validated = evidence.validate(pack, broken)
    assert validated["suggestions"][0] == result["suggestions"][0]
    assert validated["suggestions"][1]["decision"] == "insufficient"
    assert validated["suggestions"][1]["validation_error"]
    assert "NOT_REAL" not in validated["suggestions"][1]["support_refs"]
    known = deepcopy(pack)
    known["candidates"][0]["known_assignment"] = True
    known["existing_assignments"] = [{"cm": "CM1", "level": "person", "source": "cm_confirmation"}]
    known_result = deepcopy(result)
    known_result["suggestions"][0] = _suggestion("CM1", [], decision="responsible")
    assert evidence.validate(known, known_result)["suggestions"][0]["decision"] == "responsible"
    known_result["suggestions"][0].update(decision="historical", period="historical")
    assert evidence.validate(known, known_result)["suggestions"][0]["decision"] == "insufficient"
    known_result["suggestions"][0]["counter_refs"] = ["M2"]
    assert evidence.validate(known, known_result)["suggestions"][0]["decision"] == "historical"
    malformed = deepcopy(result)
    malformed["suggestions"][1]["cm"] = "CM1"
    with pytest.raises(openai_client.ModelOutputError):
        evidence.validate(pack, malformed)
    malformed = deepcopy(result)
    malformed["suggestions"][0]["confidence"] = float("nan")
    with pytest.raises(openai_client.ModelOutputError):
        evidence.validate(pack, malformed)


@pytest.fixture
def responsibility_flow(pg, pilot, monkeypatch):
    helper = _internal(pg, "helper", connected=True)
    real_build = matching.evidence.build
    cipher = message_body_cipher(pilot.settings.token_encryption_key)
    raw_messages = (
        (
            "synthetic_creator",
            "ou_creator",
            None,
            "PRIVATE TEST BODY: <script>alert(1)</script> Can you help tomorrow?",
        ),
        ("synthetic_cm", "ou_cm", "synthetic_creator", "I will arrange your samples and commission adjustment."),
        ("synthetic_helper", "ou_helper", "synthetic_creator", "I'll handle the live schedule and follow up with you."),
    )
    for message_id, sender, parent, body in raw_messages:
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
            "body_ciphertext,message_create_time,parent_id) VALUES('chat_creator',%s,%s,'user','text',%s,NOW(),%s)",
            (message_id, sender, cipher.encrypt(json.dumps({"text": body})), parent),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
            "VALUES(%s,'chat_creator',%s)",
            (pilot.ids["cm"], message_id),
        )
    state = SimpleNamespace(
        settings=pilot.settings,
        contact=pilot.ids["creator"],
        calls=[],
        valid=True,
        before_response=None,
        error=None,
        material_version=1,
        review=False,
        suggestions=None,
        coverage={},
        helper=helper,
    )
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: state.settings)
    monkeypatch.setattr(matching.evidence, "still_valid", lambda *_, **__: state.valid)

    def prepare(_conn, _settings, contact):
        actual = real_build(_conn, _settings, contact)
        refs = [ref for ref in actual["refs"] if ref["kind"] != "message"]
        actual_messages = {ref["message_id"]: ref for ref in actual["refs"] if ref["kind"] == "message"}
        refs += [
            {**actual_messages[message_id], "label": f"M{n}"} for n, (message_id, *_rest) in enumerate(raw_messages, 1)
        ]
        pack = {
            "target": "U1",
            "creator_known": True,
            "synthetic_material_revision": state.material_version,
            "candidates": [
                {"id": "CM1", "name": "CM one", "known_assignment": False},
                {"id": "CM2", "name": "CM two", "known_assignment": False},
            ],
            "existing_assignments": [],
            "people": [
                {"id": "U1", "role": "creator"},
                {"id": "CM1", "role": "internal"},
                {"id": "CM2", "role": "internal"},
            ],
            "chats": [{"id": "G1", "name": "Synthetic business conversation", "mode": "group"}],
            "messages": [
                {
                    "id": "M1",
                    "chat": "G1",
                    "speaker": "U1",
                    "reply": None,
                    "previous": None,
                    "at": "2026-10-03T10:00:00+00:00",
                    "mentions": [],
                    "text": "PRIVATE TEST BODY: <script>alert(1)</script> Can you help tomorrow?",
                },
                {
                    "id": "M2",
                    "chat": "G1",
                    "speaker": "CM1",
                    "reply": "M1",
                    "previous": "M1",
                    "at": "2026-10-03T10:01:00+00:00",
                    "mentions": [],
                    "text": "I will arrange your samples and commission adjustment.",
                },
                {
                    "id": "M3",
                    "chat": "G1",
                    "speaker": "CM2",
                    "reply": "M1",
                    "previous": "M2",
                    "at": "2026-10-03T10:02:00+00:00",
                    "mentions": [],
                    "text": "I'll handle the live schedule and follow up with you.",
                },
            ],
            "coverage": {"full_history": False, **state.coverage},
        }
        return {
            "pack": pack,
            "candidate_ids": {"CM1": pilot.ids["cm"], "CM2": state.helper},
            "refs": refs,
            "fingerprint": f"synthetic-{contact}-{state.material_version}",
            "reason": "",
            "stats": {"messages": 3, "candidate_count": 2},
        }

    def respond(feature_config, key, pack, contact, **kwargs):
        state.calls.append({"config": feature_config, "key": key, "pack": pack, "contact": contact, **kwargs})
        if state.before_response:
            state.before_response()
        if state.error:
            raise state.error
        result = {
            "suggestions": deepcopy(state.suggestions)
            if state.suggestions is not None
            else [
                _suggestion("CM1", ["M1", "M2"], decision="responsible"),
                _suggestion("CM2", ["M1", "M3"], decision="responsible"),
            ]
        }
        if state.review:
            result["suggestions"][1].update(decision="insufficient", support_refs=[], reason="Incomplete context")
        return {
            "text": json.dumps(result),
            "response_id": "resp_responsibility_synthetic",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "telemetry": {"openai_request_id": "req_responsibility_synthetic"},
        }

    monkeypatch.setattr(matching.evidence, "build", prepare)
    monkeypatch.setattr(matching.api, "call_openai", respond)

    def enable(api_key="sk-responsibility-test-only", enabled=True, threshold=0.95):
        ai_feature_config.save_feature_config(
            pg,
            ai_feature_config.api_key_cipher(state.settings.token_encryption_key),
            matching.FEATURE,
            provider="openai",
            project_id="proj_responsibility",
            model="gpt-5.6-luna",
            enabled=enabled,
            auto_confirm_threshold=threshold,
            api_key=api_key,
            updated_by_authorization_id=pilot.ids["owner"],
        )

    state.enable = enable
    return state


def _enqueue(pg, contact, source="manual"):
    return pg.execute("SELECT enqueue_creator_responsibility(%s,%s) id", (contact, source)).fetchone()["id"]


def _prepared_job(pg, flow):
    _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    result = matching.claim()
    assert result is not None
    return result


def test_one_creator_one_call_formal_multi_cm_idempotency_and_cache_gates(pg, pilot, responsibility_flow):
    flow = responsibility_flow
    initial = pg.execute(
        "SELECT auto_confirm_threshold FROM ai_feature_configs WHERE feature=%s", (matching.FEATURE,)
    ).fetchone()
    assert float(initial["auto_confirm_threshold"]) == 0.95
    assert _enqueue(pg, flow.contact) is None
    assert not matching.prepare_next(flow.settings) and matching.claim() is None and not flow.calls
    flow.enable()
    assert pg.execute("SELECT count(*) n FROM creator_responsibility_jobs").fetchone()["n"] == 0
    unconfirmed = pilot.person("unconfirmed")
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (unconfirmed,))
    assert _enqueue(pg, unconfirmed) is None
    before = _snapshot(
        pg,
        ("contacts", "contact_roles", "creator_account_assignments", "contact_social_accounts"),
    )
    identifier = _enqueue(pg, flow.contact)
    assert _enqueue(pg, flow.contact) == identifier
    assert pg.execute("SELECT count(*) n FROM creator_responsibility_jobs WHERE contact_id=%s",
                      (flow.contact,)).fetchone()["n"] == 1
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    assert job["attempts"] == 1 and job["snapshot_version"] == job["desired_version"]
    matching.run(job, flow.settings)
    row = pg.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s", (identifier,)).fetchone()
    assert row["status"] in {"completed", "review_required"}
    assert len(flow.calls) == 1 and len(flow.calls[0]["pack"]["candidates"]) == 2
    assert flow.calls[0]["key"] == "sk-responsibility-test-only"  # No creator_import or handle key fallback.
    assert flow.calls[0]["contact"] == flow.contact
    assert flow.calls[0]["reasoning_effort"] == "none"
    assert flow.calls[0]["total_timeout"] == 70 and flow.calls[0]["max_output_tokens"] == 5600
    pinned = job["pack"]["_rules"]
    assert set(pinned["documents"]) == assistant_rules.RESPONSIBILITY_KEYS
    assert row["stats"]["rules_digest"] == pinned["digest"]
    assert flow.calls[0]["instructions"] == (
        assistant_rules.responsibility_instructions(pinned)
        + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert "_rules" not in flow.calls[0]["pack"]
    assert "suggestions" in flow.calls[0]["output_format"]["schema"]["properties"]
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    usage = pg.execute("SELECT input_tokens,output_tokens,key_feature FROM creator_responsibility_requests").fetchone()
    assert usage == {"input_tokens": 100, "output_tokens": 50, "key_feature": matching.FEATURE}
    saved = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))
    assert {s["cm"] for s in saved["result"]["suggestions"]} == {"CM1", "CM2"}
    assignments = pg.execute(
        "SELECT a.id,a.responsible_authorization_id,a.source,a.confidence,i.contact_id "
        "FROM contact_assignments a JOIN authorizations auth ON auth.id=a.responsible_authorization_id "
        "JOIN lark_identities i ON i.open_id=auth.open_id WHERE a.contact_id=%s AND a.ended_at IS NULL",
        (flow.contact,),
    ).fetchall()
    assert len(assignments) == 2 and {a["contact_id"] for a in assignments} == {pilot.ids["cm"], flow.helper}
    assert all(a["source"] == "ai" and float(a["confidence"]) == 0.99 for a in assignments)
    assert next(a for a in assignments if a["contact_id"] == flow.helper)["responsible_authorization_id"] != flow.helper
    assert {s["association_status"] for s in saved["result"]["suggestions"]} == {"auto_linked"}
    assert _snapshot(pg, before.keys()) == before
    assert "PRIVATE TEST BODY" not in creator_data.encode(_snapshot(pg, ("audit_events",)))
    formal = _snapshot(pg, ("contact_assignments",))
    next_id = _enqueue(pg, flow.contact)
    assert next_id != identifier and matching.prepare_next(flow.settings)
    assert matching.claim() is None and len(flow.calls) == 1
    assert _snapshot(pg, formal.keys()) == formal
    assert (
        pg.execute("SELECT reason_code FROM creator_responsibility_jobs WHERE id=%s", (next_id,)).fetchone()[
            "reason_code"
        ]
        == "cached"
    )
    # A cache hit still applies the CURRENT threshold and current contact-to-authorization mapping.
    flow.helper = _internal(pg, "replacement", connected=True)
    before = _snapshot(pg, before.keys())
    flow.enable("sk-responsibility-cache-replacement", threshold=0.999)
    matching.configuration_changed(pg)
    next_id = _enqueue(pg, flow.contact)
    assert next_id != identifier and matching.prepare_next(flow.settings)
    assert matching.claim() is None and len(flow.calls) == 1
    assert (
        pg.execute("SELECT reason_code FROM creator_responsibility_jobs WHERE id=%s", (next_id,)).fetchone()[
            "reason_code"
        ]
        == "cached"
    )
    assert _snapshot(pg, before.keys()) == before
    assert _snapshot(pg, formal.keys()) == formal
    cached = pg.execute("SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (next_id,)).fetchone()
    refreshed = json.loads(creator_data.cipher(flow.settings).decrypt(cached["result_ciphertext"]))
    assert UUID(refreshed["candidate_ids"]["CM2"]) == flow.helper
    assert refreshed["result"]["suggestions"][1]["association_status"] == "review_required"


def test_new_cm_material_wakes_existing_creator_and_signals_downstream(pg, pilot, monkeypatch):
    """New scope/real messages wake once; formal links continue into importance and seven-day backfill."""
    settings, creator = pilot.settings, pilot.ids["creator"]
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    for feature in (matching.FEATURE, "creator_importance"):
        ai_feature_config.save_feature_config(
            pg, ai_feature_config.api_key_cipher(settings.token_encryption_key), feature,
            provider="openai", project_id="test-project", model="test-model", enabled=True,
            auto_confirm_threshold=0.95, api_key="sk-synthetic-only",
            updated_by_authorization_id=pilot.ids["owner"],
        )
    calls = []

    def respond(_config, _key, pack, _contact, **_kwargs):
        calls.append(pack)
        return {
            "text": json.dumps({"suggestions": [
                _suggestion(c["id"], [m["id"] for m in pack["messages"] if m["speaker"] == c["id"]][:8],
                            decision="responsible") for c in pack["candidates"]]}),
            "usage": {"input_tokens": 100, "output_tokens": 50}, "telemetry": {},
            "response_id": "resp_new_cm_synthetic",
        }

    monkeypatch.setattr(matching.api, "call_openai", respond)
    initial = _enqueue(pg, creator)
    assert matching.prepare_next(settings)
    matching.run(matching.claim(), settings)
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (initial,)).fetchone()["status"] == "completed"
    pg.execute("UPDATE creator_responsibility_jobs SET created_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (initial,))
    assert len(calls) == 1
    before = _snapshot(pg, ("contact_roles", "contact_social_accounts"))
    newcomer = _internal(pg, "new_cm", connected=True)
    auth = pg.execute("SELECT id FROM authorizations WHERE open_id='ou_new_cm'").fetchone()["id"]
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
               "VALUES(%s,'chat_creator','ou_creator')", (auth,))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "SELECT %s,chat_id,message_id FROM chat_messages WHERE chat_id='chat_creator'", (auth,))
    body = json.dumps({"text": "I will handle your campaign and arrange your sample."})
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
        body_ciphertext,body_fingerprint,message_create_time,parent_id)
        VALUES('chat_creator','new_cm_reply','ou_new_cm','user','text',%s,%s,NOW(),'creator_7_question')""",
        (message_body_cipher(settings.token_encryption_key).encrypt(body),
         message_body_fingerprint(settings.token_encryption_key, body)))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat_creator','new_cm_reply')", (auth,))
    # Old globally stored messages newly observed by the CM must be usable onboarding anchors.
    signal = {"authorization_id": auth, "observed_message_ids": ["creator_7_question"], "new_scope": True}
    assert matching.touch_chats(pg, ["chat_creator"], **signal) == 1
    assert matching.touch_chats(pg, ["chat_creator"], **signal) == 0
    current = pg.execute("SELECT * FROM creator_responsibility_jobs WHERE contact_id=%s "
                         "ORDER BY created_at DESC,id DESC LIMIT 1", (creator,)).fetchone()
    marker = current["stats"]["auto_signals"]
    assert marker and current["id"] != initial
    pg.execute("UPDATE creator_responsibility_jobs SET not_before=NOW() WHERE id=%s", (current["id"],))
    assert matching.prepare_next(settings)
    prepared = pg.execute("SELECT * FROM creator_responsibility_jobs WHERE id=%s", (current["id"],)).fetchone()
    assert prepared["status"] == "ready" and prepared["stats"]["auto_signals"] == marker
    # Restored consent wakes even a known CM; identical scope observations cannot keep re-enqueueing.
    pg.execute("SAVEPOINT renewed_consent")
    pg.execute("UPDATE creator_responsibility_jobs SET status='completed',created_at=NOW()-INTERVAL '30 seconds' "
               "WHERE id=%s", (current["id"],))
    pg.execute("UPDATE authorizations SET body_consent_at=NOW()-INTERVAL '1 hour' WHERE id=%s", (auth,))
    assert matching.touch_chats(pg, ["chat_creator"], **signal) == 1
    assert matching.touch_chats(pg, ["chat_creator"], **signal) == 0
    pg.execute("ROLLBACK TO SAVEPOINT renewed_consent")
    matching.run(matching.claim(), settings)
    assert len(calls) == 2
    assignments = pg.execute("SELECT responsible_authorization_id FROM contact_assignments "
                             "WHERE contact_id=%s AND ended_at IS NULL", (creator,)).fetchall()
    assert {row["responsible_authorization_id"] for row in assignments} == {pilot.ids["cm"], auth}
    for table in ("creator_importance_jobs", "offhours_summary_backfills"):
        assert pg.execute(f"SELECT 1 FROM {table} WHERE contact_id=%s AND responsible_authorization_id=%s",
                          (creator, auth)).fetchone()
    assert _snapshot(pg, before.keys()) == before
    assert newcomer != auth
    assert matching.touch_chats(pg, ["chat_creator"], authorization_id=auth,
                                message_ids=["new_cm_reply"]) == 0  # Already formal, not the whole creator skipped.
    pg.execute("UPDATE contact_assignments SET ended_at=NOW(),ended_by_authorization_id=%s "
               "WHERE contact_id=%s AND responsible_authorization_id=%s", (pilot.ids["owner"], creator, auth))
    assert matching.touch_chats(pg, ["chat_creator"], authorization_id=auth, message_ids=["new_cm_reply"]) == 0
    # Waiting jobs wake from explicit handover, without requiring the new CM to have spoken already.
    target = pilot.ids["silent"]
    waiting = _enqueue(pg, target)
    assert matching.prepare_next(settings)
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (waiting,)).fetchone()["status"] == "waiting_evidence"
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('new_handover','Creator handover','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'new_handover')", (auth,))
    body = json.dumps({"text": "@_user_1 Please arrange the campaign sample for me."})
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
        body_ciphertext,body_fingerprint,message_create_time,mentions_json)
        VALUES('new_handover','handover','ou_silent','user','text',%s,%s,NOW(),%s::jsonb)""",
        (message_body_cipher(settings.token_encryption_key).encrypt(body),
         message_body_fingerprint(settings.token_encryption_key, body),
         json.dumps([{"key": "@_user_1", "id_type": "open_id", "id": "ou_new_cm"}])))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'new_handover','handover')", (auth,))
    assert matching.touch_chats(pg, ["new_handover"], authorization_id=auth, new_scope=True) == 1
    assert matching.touch_chats(pg, ["new_handover"], authorization_id=auth, new_scope=True) == 0
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (waiting,)).fetchone()["status"] == "queued"
    # Another CM stored this globally first; this known CM's first readable observation is still new evidence.
    pg.execute("UPDATE creator_responsibility_jobs SET status='waiting_evidence' WHERE id=%s", (waiting,))
    body = json.dumps({"text": "I will arrange your sample and follow up on the campaign."})
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
        body_ciphertext,body_fingerprint,message_create_time,parent_id)
        VALUES('new_handover','globally_stored_reply','ou_new_cm','user','text',%s,%s,NOW(),'handover')""",
        (message_body_cipher(settings.token_encryption_key).encrypt(body),
         message_body_fingerprint(settings.token_encryption_key, body)))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'new_handover','globally_stored_reply')", (auth,))
    assert matching.touch_chats(pg, ["new_handover"], authorization_id=auth,
                                observed_message_ids=["globally_stored_reply"]) == 1
    assert matching.touch_chats(pg, ["new_handover"], authorization_id=auth,
                                observed_message_ids=["globally_stored_reply"]) == 0
    assert len(calls) == 2


def test_stale_queue_version_prompt_and_revocation_fence_publication(pg, pilot, responsibility_flow, monkeypatch):
    flow = responsibility_flow
    flow.enable()
    version = evidence.PROMPT_VERSION
    with monkeypatch.context() as rules:
        rules.setattr(evidence, "PROMPT_VERSION", version + "-retired")
        old_id = _enqueue(pg, flow.contact)
        assert matching.prepare_next(flow.settings)
    assert matching.claim() is None and not flow.calls
    assert pg.execute(
        "SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s", (old_id,)
    ).fetchone() == {"status": "queued", "reason_code": "snapshot_changed"}
    assert not pg.execute("SELECT 1 FROM creator_responsibility_requests").fetchone()
    job = _prepared_job(pg, flow)
    flow.before_response = lambda: _enqueue(pg, flow.contact)
    matching.run(job, flow.settings)
    current = pg.execute(
        "SELECT status,result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert current == {"status": "queued", "result_ciphertext": None}
    usage = pg.execute("SELECT input_tokens,output_tokens FROM creator_responsibility_requests").fetchone()
    assert usage == {"input_tokens": 100, "output_tokens": 50}
    flow.before_response = None
    flow.material_version += 1  # A real material change cannot reuse the earlier successful response.
    assert matching.prepare_next(flow.settings)
    next_job = matching.claim()
    flow.valid = False
    matching.run(next_job, flow.settings)
    assert len(flow.calls) == 1
    unused = pg.execute(
        "SELECT input_tokens,output_tokens,finished_at FROM creator_responsibility_requests WHERE id=%s",
        (next_job["reservation_id"],),
    ).fetchone()
    assert unused["input_tokens"] == unused["output_tokens"] == 0 and unused["finished_at"] is not None
    flow.valid = True
    flow.before_response = lambda: setattr(flow, "valid", False)
    after_network = _prepared_job(pg, flow)
    matching.run(after_network, flow.settings)
    assert len(flow.calls) == 2
    assert (
        pg.execute(
            "SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (after_network["id"],)
        ).fetchone()["result_ciphertext"]
        is None
    )
    flow.valid, flow.before_response = True, None
    flow.material_version += 1
    with monkeypatch.context() as rules:
        changed_rules = _prepared_job(pg, flow)
        flow.before_response = lambda: rules.setattr(evidence, "PROMPT_VERSION", version + "-next")
        matching.run(changed_rules, flow.settings)
        assert pg.execute(
            "SELECT status,result_ciphertext,reason_code FROM creator_responsibility_jobs WHERE id=%s",
            (changed_rules["id"],),
        ).fetchone() == {"status": "queued", "result_ciphertext": None, "reason_code": "snapshot_changed"}
        assert len(flow.calls) == 3
    assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s", (flow.contact,)).fetchone()
    flow.before_response = None
    # Confidence alone never turns a suggestion, old responsibility, uncertain scope or conflict into a write.
    for first, second in (
        ({"decision": "strong_candidate"}, {"decision": "assist"}),
        ({"decision": "historical", "period": "historical"}, {"period": "uncertain"}),
        ({"relation_level": "account"}, {"relation_level": "unknown"}),
        ({"counter_refs": ["M1"]}, {"support_refs": []}),
    ):
        flow.material_version += 1
        flow.suggestions = [
            _suggestion("CM1", ["M1", "M2"], **{"decision": "responsible", **first}),
            _suggestion("CM2", ["M1", "M3"], **{"decision": "responsible", **second}),
        ]
        matching.run(_prepared_job(pg, flow), flow.settings)
        assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s", (flow.contact,)).fetchone()
    flow.suggestions = None
    pg.execute("SAVEPOINT coverage_guards")
    flow.coverage = {"critical_evidence_omitted": True}
    flow.material_version += 1
    incomplete = _prepared_job(pg, flow)
    matching.run(incomplete, flow.settings)
    incomplete_result = pg.execute(
        "SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (incomplete["id"],)
    ).fetchone()
    results = json.loads(creator_data.cipher(flow.settings).decrypt(incomplete_result["result_ciphertext"]))["result"][
        "suggestions"
    ]
    assert all(result["association_status"] == "review_required" and result["association_reason"] for result in results)
    assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s", (flow.contact,)).fetchone()
    flow.coverage = {"missing_reply_ids": ["M2"]}
    flow.material_version += 1
    partial = _prepared_job(pg, flow)
    matching.run(partial, flow.settings)
    partial_result = pg.execute(
        "SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (partial["id"],)
    ).fetchone()
    results = json.loads(creator_data.cipher(flow.settings).decrypt(partial_result["result_ciphertext"]))["result"][
        "suggestions"
    ]
    assert results[0]["association_status"] == "review_required" and results[0]["association_reason"]
    assert results[1]["association_status"] == "auto_linked"  # M2 is irrelevant to CM2's M1/M3 support.
    formal = _snapshot(pg, ("contact_assignments",))
    flow.coverage = {"critical_evidence_omitted": True}
    flow.material_version += 1
    existing = _prepared_job(pg, flow)
    matching.run(existing, flow.settings)
    existing_result = pg.execute(
        "SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (existing["id"],)
    ).fetchone()
    results = json.loads(creator_data.cipher(flow.settings).decrypt(existing_result["result_ciphertext"]))["result"][
        "suggestions"
    ]
    assert (
        results[0]["association_status"] == "review_required" and results[1]["association_status"] == "already_linked"
    )
    assert _snapshot(pg, formal.keys()) == formal
    pg.execute("ROLLBACK TO SAVEPOINT coverage_guards")
    flow.coverage = {}
    flow.material_version += 1
    candidate_auths = [
        row["id"]
        for row in pg.execute(
            "SELECT a.id FROM authorizations a JOIN lark_identities i ON i.open_id=a.open_id "
            "WHERE i.contact_id=ANY(%s::uuid[])",
            ([pilot.ids["cm"], flow.helper],),
        ).fetchall()
    ]
    # The writer independently checks live grants even when a cached/validated response reaches it.
    flow.before_response = lambda: pg.execute(
        "UPDATE authorizations SET status=CASE WHEN id=%s THEN 'revoked' ELSE status END,"
        "body_consent_at=CASE WHEN id=%s THEN NULL ELSE body_consent_at END WHERE id=ANY(%s::uuid[])",
        (candidate_auths[0], candidate_auths[1], candidate_auths),
    )
    matching.run(_prepared_job(pg, flow), flow.settings)
    assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s", (flow.contact,)).fetchone()
    pg.execute(
        "UPDATE authorizations SET status='active',body_consent_at=NOW() WHERE id=ANY(%s::uuid[])", (candidate_auths,)
    )
    flow.material_version += 1

    def manual_end():
        for authorization in candidate_auths:
            pg.execute(
                "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,"
                "ended_at,ended_by_authorization_id) VALUES(%s,%s,%s,'manual',NOW(),%s)",
                (uuid4(), flow.contact, authorization, pilot.ids["owner"]),
            )

    flow.before_response = manual_end
    matching.run(_prepared_job(pg, flow), flow.settings)
    assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL",
                          (flow.contact,)).fetchone()
    assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s AND ended_at IS NOT NULL",
                      (flow.contact,)).fetchone()["n"] == 2


def test_retry_ledger_and_disabled_config_keep_suggestions_isolated(pg, responsibility_flow, monkeypatch):
    flow = responsibility_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic overload", retry_after_seconds=120, retryable=True,
        telemetry={"http_status": 429, "provider_error": {"code": "rate_limit_exceeded"}})
    matching.run(job, flow.settings)
    row = pg.execute(
        "SELECT status,EXTRACT(EPOCH FROM not_before-NOW()) seconds FROM creator_responsibility_jobs WHERE id=%s",
        (job["id"],),
    ).fetchone()
    assert row["status"] == "retry_wait" and row["seconds"] >= 120
    usage = pg.execute("SELECT input_tokens,output_tokens FROM creator_responsibility_requests").fetchone()
    assert usage == {"input_tokens": None, "output_tokens": None}
    receipt = pg.execute("SELECT response_ciphertext FROM creator_responsibility_requests WHERE id=%s",
                         (job["reservation_id"],)).fetchone()
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(receipt["response_ciphertext"]))
    assert diagnostic["provider_error"] == {"code": "rate_limit_exceeded"}
    ai_dispatch.resume(pg)
    pg.execute("UPDATE creator_responsibility_jobs SET not_before=NOW() WHERE id=%s", (job["id"],))
    retry = matching.claim()
    assert retry["attempts"] == 2 and retry["reservation_id"] != job["reservation_id"]
    flow.error, flow.review = None, True
    matching.run(retry, flow.settings)
    row = pg.execute(
        "SELECT status,result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    saved = json.loads(creator_data.cipher(flow.settings).decrypt(row["result_ciphertext"]))["result"]["suggestions"]
    assert saved[0]["decision"] == "responsible" and saved[1]["decision"] == "insufficient"
    assert saved[0]["association_status"] == "auto_linked"
    assert (
        pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s", (flow.contact,)).fetchone()["n"]
        == 1
    )
    flow.material_version += 1
    unknown = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    matching.run(unknown, flow.settings)
    assert matching.claim() is None and len(flow.calls) == 3
    assert pg.execute("SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s",
                      (unknown["id"],)).fetchone() == {"status": "failed", "reason_code": "request_unknown"}
    assert pg.execute("SELECT error_code,finished_at FROM creator_responsibility_requests WHERE id=%s",
                      (unknown["reservation_id"],)).fetchone()["error_code"] == "request_unknown"
    frozen = _snapshot(pg, ("creator_responsibility_jobs", "creator_responsibility_requests", "contact_assignments"))
    assert not matching.recover_saved_result(pg, flow.settings, unknown["id"])
    assert _snapshot(pg, frozen.keys()) == frozen and len(flow.calls) == 3
    # Rotation and unrelated signals cannot turn an unknown paid attempt into an automatic retry.
    flow.enable("sk-rotated-after-unknown")
    blocked = _enqueue(pg, flow.contact, "history")
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown",
    }
    assert len(flow.calls) == 3
    flow.error = None
    manual = _prepared_job(pg, flow)
    matching.run(manual, flow.settings)
    assert len(flow.calls) == 4
    flow.material_version += 1
    malformed_job = _prepared_job(pg, flow)
    respond = matching.api.call_openai

    def malformed(*args, **kwargs):
        response = respond(*args, **kwargs)
        return {**response, "text": response["text"] + " pruned" + response["text"]}

    before = pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall()
    with monkeypatch.context() as broken:
        broken.setattr(matching.api, "call_openai", malformed)
        matching.run(malformed_job, flow.settings)
    receipt = pg.execute("SELECT error_code,response_ciphertext FROM creator_responsibility_requests WHERE id=%s",
                         (malformed_job["reservation_id"],)).fetchone()
    assert receipt["error_code"] == "invalid_output" and receipt["response_ciphertext"]
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(receipt["response_ciphertext"]))
    assert " pruned" in diagnostic["raw"] and "Extra data" in diagnostic["error"]
    assert pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall() == before
    flow.material_version += 1
    incomplete = _prepared_job(pg, flow)
    flow.error = openai_client.ModelOutputError("unfinished response", telemetry={
        "http_status": 200, "response_status": "incomplete", "incomplete_reason": "max_output_tokens",
        "usage": {"input_tokens": 15073, "output_tokens": 2200},
    })
    matching.run(incomplete, flow.settings)
    receipt = pg.execute("SELECT error_code,response_ciphertext FROM creator_responsibility_requests WHERE id=%s",
                         (incomplete["reservation_id"],)).fetchone()
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(receipt["response_ciphertext"]))
    assert receipt["error_code"] == "invalid_output" and diagnostic["response_status"] == "incomplete"
    assert diagnostic["incomplete_reason"] == "max_output_tokens"
    assert diagnostic["usage"] == {"input_tokens": 15073, "output_tokens": 2200}
    assert matching.claim() is None
    assert pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall() == before
    flow.material_version += 1
    invalid_schema = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("HTTP 400", telemetry={
        "http_status": 400, "provider_error": {"code": "invalid_json_schema"}},
        provider_diagnostic={"code": "invalid_json_schema", "message": "PRIVATE PROVIDER DETAIL"})
    matching.run(invalid_schema, flow.settings)
    receipt = pg.execute("SELECT error_code,response_ciphertext FROM creator_responsibility_requests WHERE id=%s",
                         (invalid_schema["reservation_id"],)).fetchone()
    diagnostic = json.loads(creator_data.cipher(flow.settings).decrypt(receipt["response_ciphertext"]))
    assert receipt["error_code"] == "http_400" and diagnostic["provider_diagnostic"] == {
        "code": "invalid_json_schema", "message": "PRIVATE PROVIDER DETAIL"}
    assert "PRIVATE PROVIDER DETAIL" not in diagnostic["error"]
    assert matching.claim() is None
    assert pg.execute("SELECT * FROM contact_assignments ORDER BY id").fetchall() == before
    flow.enable(enabled=False)
    matching.configuration_changed(pg)
    assert _enqueue(pg, flow.contact) is None and matching.claim() is None
    import_config = pg.execute("SELECT enabled FROM ai_feature_configs WHERE feature='creator_import'").fetchone()
    assert import_config["enabled"] is False


def test_unknown_request_after_config_race_only_blocks_identical_material(pg, pilot, responsibility_flow, monkeypatch):
    flow = responsibility_flow
    flow.enable()
    job = _prepared_job(pg, flow)
    flow.error = openai_client.OpenAIRequestError("synthetic timeout", retryable=True)
    flow.before_response = lambda: flow.enable("sk-rotated-during-call")
    matching.run(job, flow.settings)
    flow.before_response = None
    document = assistant_rules.library(pg, flow.settings)["responsibility-judgment"]
    assistant_rules.save_document(pg, flow.settings, pilot.ids["owner"], document["key"], document["title"],
        document["description"], "manual", "published", "owner", document["text"] + "\n测试：更新判断手册。",
        document["revision"])
    # A rules edit is not new chat material and cannot silently repeat an unknown paid request.
    assert matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 1
    assert pg.execute("SELECT reason_code FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)).fetchone() == {
        "reason_code": "request_unknown",
    }
    original_build = matching.evidence.build

    def changed(*args, **kwargs):
        material = original_build(*args, **kwargs)
        material["pack"]["messages"][0]["text"] += " Please arrange the next campaign."
        material["fingerprint"] += "-new-message"
        return material

    monkeypatch.setattr(matching.evidence, "build", changed)
    flow.error = None
    _enqueue(pg, flow.contact, "history")
    assert matching.prepare_next(flow.settings)
    fresh = matching.claim()
    assert fresh is not None
    matching.run(fresh, flow.settings)
    assert len(flow.calls) == 2


def test_document_versions_pause_only_affected_work_without_rewriting_relationships(pg, pilot, responsibility_flow):
    flow = responsibility_flow
    flow.enable()

    def save(*, status="published", append=""):
        document = assistant_rules.library(pg, flow.settings)["responsibility-judgment"]
        assistant_rules.save_document(pg, flow.settings, pilot.ids["owner"], document["key"], document["title"],
            document["description"], "manual", status, "owner", document["text"] + append, document["revision"])

    running = _prepared_job(pg, flow)
    pinned = running["pack"]["_rules"]
    before = _snapshot(pg, ("contact_assignments", "creator_responsibility_jobs", "creator_responsibility_requests"))
    save(append="\n测试：新准备的任务使用此版本。")
    assert _snapshot(pg, before.keys()) == before  # Saving a manual never starts a historical batch.
    updated = assistant_rules.responsibility_snapshot(pg, flow.settings)
    assert updated["digest"] != pinned["digest"]
    matching.run(running, flow.settings)
    assert flow.calls[-1]["instructions"] == (
        assistant_rules.responsibility_instructions(pinned)
        + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert len(flow.calls) == 1
    formal = _snapshot(pg, ("contact_assignments",))
    assert len(formal["contact_assignments"]) == len(before["contact_assignments"]) + 2

    refreshed = _prepared_job(pg, flow)
    assert refreshed["pack"]["_rules"]["digest"] == updated["digest"]
    matching.run(refreshed, flow.settings)
    assert len(flow.calls) == 2  # Changed rules cannot reuse an older ruleset's completed cache.
    assert flow.calls[-1]["instructions"] == (
        assistant_rules.responsibility_instructions(updated)
        + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert _snapshot(pg, formal.keys()) == formal

    flow.material_version += 1
    blocked = _enqueue(pg, flow.contact)
    assert matching.prepare_next(flow.settings)
    requests = _snapshot(pg, ("creator_responsibility_requests",))
    save(status="archived")
    assert matching.claim() is None and len(flow.calls) == 2
    assert _snapshot(pg, requests.keys()) == requests
    assert pg.execute("SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone() == {"status": "waiting_evidence", "reason_code": "rules_unavailable"}
    save()
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone()["status"] == "queued"
    assert matching.prepare_next(flow.settings)
    claimed = matching.claim()
    assert claimed is not None and claimed["id"] == blocked
    flow.before_response = lambda: save(status="archived")
    matching.run(claimed, flow.settings)
    flow.before_response = None
    paid = pg.execute("SELECT input_tokens,output_tokens,finished_at,response_ciphertext "
                      "FROM creator_responsibility_requests WHERE id=%s", (claimed["reservation_id"],)).fetchone()
    assert paid["input_tokens"] == 100 and paid["output_tokens"] == 50
    assert paid["finished_at"] is not None and paid["response_ciphertext"]
    assert pg.execute("SELECT status,reason_code,result_ciphertext FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone() == {
                          "status": "waiting_evidence", "reason_code": "rules_unavailable", "result_ciphertext": None}
    assert _snapshot(pg, formal.keys()) == formal
    save()
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone()["status"] == "waiting_evidence"
    matching.configuration_changed(pg)
    assert pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone()["status"] == "waiting_evidence"
    _enqueue(pg, flow.contact, "history")  # A new signal need not mean the bounded evidence actually changed.
    assert matching.prepare_next(flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s",
                      (blocked,)).fetchone() == {"status": "waiting_evidence", "reason_code": "rules_unavailable"}
    assert not matching.prepare_next(flow.settings) and matching.claim() is None and len(flow.calls) == 3
    legacy = {key: value for key, value in claimed["pack"].items() if key != "_rules"}
    _result, _usage, error, _diagnostic = matching.review(legacy, claimed["config"], flow.contact, flow.settings)
    assert error is None and flow.calls[-1]["instructions"] == (
        evidence.instructions() + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert _snapshot(pg, formal.keys()) == formal  # Legacy frozen input can be read without changing relationships.


def test_post_response_sql_failure_recovers_saved_success_without_redispatching_or_stale_writes(
    pg, pilot, responsibility_flow, monkeypatch
):
    flow = responsibility_flow
    flow.enable()
    job = _prepared_job(pg, flow)

    order = []
    original_lock, original_current = matching.lock_sync_material, matching._current

    def source_lock(conn):
        order.append('material')
        return original_lock(conn)

    def current(conn, job):
        assert order[-1] == 'material'  # The source gate is taken BEFORE config/job/source row locks.
        order.append('current')
        return original_current(conn, job)

    def failed_apply(*_, **__):
        assert order[-1] == 'current'
        pg.execute("DO $$ BEGIN RAISE EXCEPTION 'synthetic application deadlock' USING ERRCODE='40P01'; END $$")

    with monkeypatch.context() as failed_source:
        failed_source.setattr(matching, "lock_sync_material", source_lock)
        failed_source.setattr(matching, "_current", current)
        failed_source.setattr(matching, "_apply_assignments", failed_apply)
        with pytest.raises(DeadlockDetected):
            matching.run(job, flow.settings)
    ledger = pg.execute(
        "SELECT input_tokens,output_tokens,response_id,finished_at FROM creator_responsibility_requests WHERE id=%s",
        (job["reservation_id"],),
    ).fetchone()
    assert ledger["input_tokens"] == 100 and ledger["output_tokens"] == 50
    assert ledger["response_id"] == "resp_responsibility_synthetic" and ledger["finished_at"] is not None
    pg.execute("UPDATE creator_responsibility_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (job["id"],))
    assert matching.claim() is None
    recovered = pg.execute(
        "SELECT status,reason_code,result_ciphertext FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)
    ).fetchone()
    assert recovered == {"status": "queued", "reason_code": "application_pending", "result_ciphertext": None}
    assert len(flow.calls) == 1
    # Legacy rows were mislabeled unknown; a changed desired_version alone does not prove changed material.
    pg.execute("UPDATE creator_responsibility_jobs SET status='failed',reason_code='request_unknown',"
               "desired_version=desired_version+1 WHERE id=%s", (job["id"],))
    frozen = _snapshot(pg, ("creator_responsibility_jobs", "creator_responsibility_requests", "contact_assignments"))
    flow.material_version += 1
    assert not matching.recover_saved_result(pg, flow.settings, job["id"])
    assert _snapshot(pg, frozen.keys()) == frozen and len(flow.calls) == 1
    flow.material_version -= 1
    assert matching.recover_saved_result(pg, flow.settings, job["id"])
    completed = pg.execute("SELECT status,reason_code,snapshot_version=desired_version AS current "
                           "FROM creator_responsibility_jobs WHERE id=%s", (job["id"],)).fetchone()
    assert completed == {"status": "completed", "reason_code": "cached", "current": True}
    assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL",
                      (flow.contact,)).fetchone()['n'] == 2
    assert not matching.recover_saved_result(pg, flow.settings, job["id"]) and len(flow.calls) == 1
    flow.material_version += 1
    admission_job = _prepared_job(pg, flow)

    def failed_admission(_telemetry):
        with pg.transaction():
            pg.execute("SELECT 1/0")

    with monkeypatch.context() as failed_gate:
        failed_gate.setattr(ai_dispatch, "observe_limits", failed_admission)
        with pytest.raises(DivisionByZero):
            matching.run(admission_job, flow.settings)
    ledger = pg.execute(
        "SELECT input_tokens,output_tokens,finished_at FROM creator_responsibility_requests WHERE id=%s",
        (admission_job["reservation_id"],),
    ).fetchone()
    assert ledger["input_tokens"] == 100 and ledger["output_tokens"] == 50 and ledger["finished_at"] is not None
    pg.execute(
        "UPDATE creator_responsibility_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
        (admission_job["id"],),
    )
    assert matching.claim() is None and len(flow.calls) == 2
    pairs = _snapshot(pg, ("contact_assignments",))
    assert matching.prepare_next(flow.settings) and matching.claim() is None
    assert pg.execute("SELECT status,reason_code FROM creator_responsibility_jobs WHERE id=%s",
                      (admission_job['id'],)).fetchone() == {'status': 'completed', 'reason_code': 'cached'}
    assert _snapshot(pg, pairs.keys()) == pairs and len(flow.calls) == 2
    # Borrowed-key diagnostics are explicitly isolated even if the native feature is enabled.
    sample = _enqueue(pg, flow.contact, "sample")
    assert not matching.prepare_next(flow.settings) and matching.claim() is None
    assert (
        pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s", (sample,)).fetchone()["status"]
        == "queued"
    )
    pg.execute(
        "UPDATE creator_responsibility_jobs SET status='preparing',claim_token=%s,"
        "lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
        (uuid4(), sample),
    )
    assert matching.claim() is None
    assert (
        pg.execute("SELECT status FROM creator_responsibility_jobs WHERE id=%s", (sample,)).fetchone()["status"]
        == "failed"
    )
    assert len(flow.calls) == 2
    explicit_sample = _enqueue(pg, flow.contact, "sample")
    flow.material_version += 1
    matching.action(pg, pilot.ids["owner"], explicit_sample, "recheck")
    assert pg.execute(
        "SELECT source,status FROM creator_responsibility_jobs WHERE id=%s", (explicit_sample,)
    ).fetchone() == {"source": "manual", "status": "queued"}
    assert matching.prepare_next(flow.settings)
    claimed = matching.claim()
    assert claimed is not None and claimed["id"] == explicit_sample and claimed["key_feature"] == matching.FEATURE
    assert len(flow.calls) == 2


def test_ai_readonly_routes_csrf_lazy_evidence_and_safe_audit(pg, pilot, responsibility_flow, monkeypatch):
    """Rendering suggestions cannot read bodies; explicit evidence reads remain audited and escaped."""
    flow = responsibility_flow
    owner = pilot.ids["owner"]
    session = {"auth": {"id": owner, "status": "active", "display_name": "owner"}}
    monkeypatch.setattr(routes, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(routes, "get_settings", lambda: flow.settings)
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"), autoescape=select_autoescape(["html"])
    )
    app = FastAPI()
    app.include_router(
        routes.create_router(
            "responsibility",
            current_authorization=lambda _: session["auth"],
            csrf_token=lambda _: "csrf",
            render_template=lambda request, name, context, status_code=200: HTMLResponse(
                env.get_template(name).render(**context), status_code=status_code),
        )
    )
    client = TestClient(app)
    path = "/admin/ai/creator-responsibility"
    assert client.get(path).status_code == 200
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}).status_code == 400
    assert pg.execute("SELECT count(*) n FROM creator_responsibility_jobs").fetchone()["n"] == 0
    flow.enable()
    pg.execute("UPDATE contact_roles SET status='rejected' WHERE contact_id<>%s", (flow.contact,))
    for payload in (
        {"csrf_token": "wrong"},
        {"csrf_token": "csrf", "page": "not-a-number"},
        {"csrf_token": "csrf", "mode": "sample"},
        {"csrf_token": "csrf", "limit": "5"},
        {"csrf_token": "csrf", "contact_ids": str(flow.contact)},
    ):
        assert client.post(path + "/enqueue", data=payload).status_code == 400
    assert pg.execute("SELECT count(*) n FROM creator_responsibility_jobs").fetchone()["n"] == 0
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}, follow_redirects=False).status_code == 303
    assert matching.prepare_next(flow.settings)
    job = matching.claim()
    matching.run(job, flow.settings)
    row = pg.execute("SELECT result_ciphertext FROM creator_responsibility_jobs WHERE id=%s",
                     (job["id"],)).fetchone()
    storage = creator_data.cipher(flow.settings)
    result = json.loads(storage.decrypt(row["result_ciphertext"]))
    result["result"]["suggestions"][0].update(reason="PRIVATE TEST BODY", scope_note="PRIVATE TEST BODY")
    pg.execute("UPDATE creator_responsibility_jobs SET result_ciphertext=%s WHERE id=%s",
               (storage.encrypt(json.dumps(result)), job["id"]))
    before = _snapshot(pg, ("contact_assignments", "creator_account_assignments"))
    response = client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store, max-age=0"
    assert "PRIVATE TEST BODY" not in response.text and "未接入" not in response.text
    assert 'name="limit"' not in response.text and 'name="contact_ids"' not in response.text
    assert 'name="mode"' not in response.text and "小样本" not in response.text
    assert "99%" in response.text and "已自动关联" in response.text
    assert "helper" in response.text  # CM contact ID differs from the current authorization ID.
    assert "/confirm" not in response.text and "/apply" not in response.text
    with pg.transaction(force_rollback=True):
        extra = _enqueue(pg, pilot.person("second_page_creator"))
        pg.execute("UPDATE creator_responsibility_jobs SET status='completed',result_ciphertext=%s WHERE id=%s",
                   (storage.encrypt(json.dumps(result)), extra))
        queries = []

        class Meter:
            def execute(self, sql, *args, **kwargs):
                queries.append(sql)
                return pg.execute(sql, *args, **kwargs)

        page = matching.page_data(Meter(), owner, flow.settings)
        assert len([row for row in page["jobs"] if row["results"]]) == 2
        assert sum(sql.startswith("SELECT id,display_name FROM contacts") for sql in queries) == 1
        assert not any(sql.startswith("SELECT refs FROM creator_responsibility_jobs") for sql in queries)
    pg.execute("SAVEPOINT unknown_page")
    pg.execute("UPDATE creator_responsibility_jobs SET status='failed',reason_code='request_unknown',"
               "result_ciphertext=NULL WHERE id=%s", (job["id"],))
    unknown = client.get(path)
    assert unknown.status_code == 200 and "调用失败" in unknown.text
    assert unknown.text.count("请求结果未知，需核实后重新检查") == 1
    assert "request_unknown" not in unknown.text
    pg.execute("ROLLBACK TO SAVEPOINT unknown_page")
    read_path = path + f"/jobs/{job['id']}/evidence"
    response = client.get(read_path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store, max-age=0"
    assert "PRIVATE TEST BODY" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text and 'id="evidence-M1"' in response.text
    audits = pg.execute(
        "SELECT action,metadata FROM audit_events WHERE action LIKE 'creator_responsibility%'"
    ).fetchall()
    assert any("evidence" in row["action"] for row in audits)
    assert "PRIVATE TEST BODY" not in creator_data.encode(audits)
    assert _snapshot(pg, before.keys()) == before
    jobs_before = _snapshot(pg, ("creator_responsibility_jobs", "creator_responsibility_requests"))
    with monkeypatch.context() as upgraded_prompt:
        upgraded_prompt.setattr(evidence, "PROMPT_VERSION", evidence.PROMPT_VERSION + "-next")
        assert "旧版结果" in client.get(path).text and "旧版结果" in client.get(read_path).text
        assert not matching.prepare_next(flow.settings) and matching.claim() is None
        assert _snapshot(pg, jobs_before.keys()) == jobs_before and len(flow.calls) == 1
    flow.valid = False
    stale_page = client.get(path)
    assert "规则或材料已更新，请重新分析" in stale_page.text
    assert "候选 2 人" not in stale_page.text and "本次证据" not in stale_page.text
    assert client.get(read_path).status_code in {403, 404, 409}
    flow.valid = True
    session["auth"] = {"id": pilot.ids["cm"], "status": "active"}
    assert client.get(path).status_code == 403
    assert client.get(read_path).status_code == 403
    assert client.post(path + "/enqueue", data={"csrf_token": "csrf"}).status_code == 403
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) VALUES(%s,'ai',%s)",
               (pilot.ids["cm"], owner))
    pg.execute("UPDATE creator_responsibility_jobs SET status='running',lease_until=NOW()-INTERVAL '1 second' "
               "WHERE id=%s", (job["id"],))
    jobs_before = _snapshot(pg, ("creator_responsibility_jobs", "creator_responsibility_requests"))
    readonly = client.get(path)
    assert readonly.status_code == 200 and "99%" in readonly.text and "PRIVATE TEST BODY" not in readonly.text
    assert '<form method="post"' not in readonly.text and "/evidence" not in readonly.text
    assert _snapshot(pg, jobs_before.keys()) == jobs_before
    assert client.get(read_path).status_code == 403
    assert client.post(path + f"/jobs/{job['id']}/recheck", data={"csrf_token": "csrf"}).status_code == 403
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (pilot.ids["cm"],))
    assert client.get(path).status_code == 403
    session["auth"] = {"id": owner, "status": "active"}
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (owner,))
    assert client.get(path).status_code == 403
    assert client.get(read_path).status_code == 403
    assert client.post(path + f"/jobs/{job['id']}/recheck", data={"csrf_token": "csrf"}).status_code == 403
