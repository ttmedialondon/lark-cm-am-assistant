"""One real PostgreSQL group-classification workflow; every model call is synthetic."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import ai_dispatch, ai_feature_config, config
from app import creator_group_evidence as evidence
from app import creator_group_matching as matching
from app import creator_group_routes as routes
from app.crypto import message_body_cipher


def test_creator_group_queue_real_authorization_manual_lock_and_billing_fences(pg, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    owner, creator, employee = uuid4(), uuid4(), uuid4()
    pg.execute("""INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at)
        VALUES(%s,'ou_owner','synthetic',NOW()-INTERVAL '1 day'),
              (%s,'ou_employee','synthetic',NOW()-INTERVAL '1 day')""", (owner, employee))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (owner,))
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Synthetic creator')", (creator,))
    pg.execute("""INSERT INTO lark_identities(open_id,contact_id,tenant_classification)
        VALUES('ou_creator',%s,'known_external')""", (creator,))
    pg.execute("""INSERT INTO contact_roles(contact_id,role,status,source)
        VALUES(%s,'creator','confirmed','manual')""", (creator,))
    cipher = message_body_cipher(settings.token_encryption_key)
    for chat, mode in (("oc_group", "group"), ("oc_topic", "topic"), ("oc_p2p", "p2p")):
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'TT creator cooperation',%s)", (chat, mode))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (owner, chat))
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,'ou_creator')", (chat,))
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
            body_ciphertext,message_create_time) VALUES(%s,'om_one','ou_creator','user','text',%s,NOW())""",
                   (chat, cipher.encrypt(json.dumps({"text": "Can you fix my sample request?"}))))
        pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
            VALUES(%s,%s,'om_one')""", (owner, chat))
    monkeypatch.setattr(matching, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(routes, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(ai_dispatch, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    calls = []
    answer = {"purpose": "support", "confidence": .94, "tags": [], "refs": ["G1", "M1"]}

    def respond(feature, key, pack, target, **kwargs):
        calls.append({"feature": feature, "key": key, "pack": pack, "kwargs": kwargs})
        return {
            "text": json.dumps(answer),
            "response_id": "resp_synthetic", "usage": {"input_tokens": 100, "output_tokens": 50},
            "telemetry": {"openai_request_id": "req_synthetic"},
        }

    monkeypatch.setattr(matching.api, "call_openai", respond)

    def enable(key="sk-synthetic-only", enabled=True, threshold=.95):
        ai_feature_config.save_feature_config(
            pg, ai_feature_config.api_key_cipher(settings.token_encryption_key), matching.FEATURE,
            provider="openai", project_id="proj_synthetic", model="gpt-test", enabled=enabled,
            api_key=key, updated_by_authorization_id=owner, auto_confirm_threshold=threshold,
        )

    def prepared():
        assert matching.prepare_next(settings)
        job = matching.claim()
        assert job is not None
        return job

    assert not matching._enabled(matching._config(pg))
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs").fetchone()["n"] == 0
    assert pg.execute("SELECT creator_group_is_eligible('oc_p2p') eligible").fetchone()["eligible"] is False
    pg.execute("SAVEPOINT group_scope")
    pg.execute("DELETE FROM chat_members WHERE chat_id='oc_topic'")
    assert pg.execute("SELECT creator_group_is_eligible('oc_topic') eligible").fetchone()["eligible"] is True
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE chat_id='oc_topic'")
    assert pg.execute("SELECT creator_group_is_eligible('oc_topic') eligible").fetchone()["eligible"] is False
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (owner,))
    assert pg.execute("SELECT creator_group_is_eligible('oc_group') eligible").fetchone()["eligible"] is False
    pg.execute("ROLLBACK TO SAVEPOINT group_scope")
    assert not any(r["enabled"] for r in pg.execute(
        "SELECT enabled FROM ai_feature_configs WHERE feature IN ('creator_group_classification',"
        "'creator_responsibility_matching','creator_handle_matching')").fetchall())
    matching.confirm(pg, owner, "oc_group", "unknown", [], locked=False)  # No API key required.
    with pytest.raises(PermissionError):
        matching.confirm(pg, employee, "oc_group", "support", [])
    enable()
    assert matching.enqueue(pg, owner) == {"queued": 2}  # Canonical group + topic, never private chat.
    assert matching.enqueue(pg, owner) == {"queued": 0}
    for _ in range(2):
        job = prepared()
        assert evidence.validate(job["pack"], {**answer, "reason": "Legacy paid result"}) == answer
        answer["confidence"] = .99 if job["chat_id"] == "oc_group" else .94
        matching.run(job, settings)
    assert all(set(c["kwargs"]["output_format"]["schema"]["required"]) == set(answer) for c in calls)
    assert pg.execute("""SELECT purpose,source,locked FROM chat_group_classifications
        WHERE chat_id='oc_group'""").fetchone() == {
        "purpose": "unknown", "source": "manual", "locked": False,
    }  # Even unlocked human decisions survive a high-confidence AI result.
    assert pg.execute("SELECT chat_id FROM chat_group_classifications WHERE chat_id='oc_topic'").fetchone() is None
    assert len(calls) == 2
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs WHERE status='completed'").fetchone()["n"] == 2
    assert matching.enqueue(pg, owner) == {"queued": 0}
    matching.touch_chats(pg, ["oc_group", "oc_topic"], settings)
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs").fetchone()["n"] == 2
    assert pg.execute("SELECT sum(input_tokens) i,sum(output_tokens) o FROM creator_group_requests").fetchone() == {
        "i": 200, "o": 100,
    }
    data = matching.page_data(pg, owner, settings)
    assert len(data["jobs"]) == 2 and all(j["result"]["purpose"] == "support" for j in data["jobs"])
    assert all("pack_ciphertext" not in j for j in data["jobs"])
    first = next(j for j in data["jobs"] if j["chat_id"] == "oc_group")
    assert first["display_purpose"] == "support" and first["display_confidence"] == "99%"
    assert first["display_status"] == "待核实"  # Manual unknown must not hide a valid new suggestion.
    material = matching.task_evidence(pg, owner, settings, first["id"])
    assert material["pack"]["messages"][0]["text"] == "Can you fix my sample request?"
    # A normal sync can re-encrypt identical plaintext with a fresh nonce; that is not new evidence.
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='oc_group' AND message_id='om_one'",
               (cipher.encrypt(json.dumps({"text": "Can you fix my sample request?"})),))
    matching.touch_chats(pg, ["oc_group"], settings)
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs").fetchone()["n"] == 2
    assert next(j for j in matching.page_data(pg, owner, settings)["jobs"]
                if j["chat_id"] == "oc_group")["result"]["purpose"] == "support"
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='oc_group' AND message_id='om_one'",
               (cipher.encrypt(json.dumps({"text": "This is now a different campaign conversation."})),))
    matching.touch_chats(pg, ["oc_group"], settings)
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs").fetchone()["n"] == 3
    # Name/structure changes invalidate the entire evidence chain, even when the message still exists.
    pg.execute("UPDATE chats SET name='Changed group purpose' WHERE chat_id='oc_group'")
    with pytest.raises(PermissionError):
        matching.confirm(pg, owner, "oc_group", "support", [], job_id=first["id"])
    fresh = next(j for j in matching.page_data(pg, owner, settings)["jobs"] if j["chat_id"] == "oc_group")
    assert fresh["id"] != first["id"] and fresh["result"] is None
    matching.confirm(pg, owner, "oc_group", "support", ["Manual"], locked=True)
    assert pg.execute("SELECT purpose,locked FROM chat_group_classifications WHERE chat_id='oc_group'").fetchone() == {
        "purpose": "support", "locked": True,
    }
    assert pg.execute("""SELECT count(*) n FROM creator_group_jobs WHERE chat_id='oc_group'
        AND status NOT IN ('completed','failed','cancelled')""").fetchone()["n"] == 0
    matching.touch_chats(pg, ["oc_group"], settings)
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs WHERE chat_id='oc_group'").fetchone()["n"] == 2

    def classification(chat_id="oc_topic"):
        return pg.execute("SELECT * FROM chat_group_classifications WHERE chat_id=%s", (chat_id,)).fetchone()

    def reclassify(purpose, confidence, refs=None, chat_id="oc_topic"):
        last = pg.execute("""SELECT id FROM creator_group_jobs WHERE chat_id=%s
            ORDER BY created_at DESC,id DESC LIMIT 1""", (chat_id,)).fetchone()
        matching.action(pg, owner, last["id"], "recheck")
        answer.update(purpose=purpose, confidence=confidence, refs=refs or ["G1", "M1"])
        job = prepared()
        matching.run(job, settings)
        return job

    auto_classify = matching._auto_classify
    monkeypatch.setattr(matching, "_auto_classify", lambda *args, **kwargs: None)  # One temporary source-lock miss.
    high = reclassify("support", .95)
    assert classification() is None
    pending = pg.execute("SELECT status,stats FROM creator_group_jobs WHERE id=%s", (high["id"],)).fetchone()
    assert pending["status"] == "completed" and pending["stats"]["auto_deferred"]
    before = len(calls)
    monkeypatch.setattr(matching, "_auto_classify", auto_classify)
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW() WHERE id=%s", (high["id"],))
    assert matching.prepare_next(settings) and matching.claim() is None
    assert len(calls) == before  # The real authorization/admission chain retries locally, never another paid call.
    assert not pg.execute("SELECT stats FROM creator_group_jobs WHERE id=%s", (high["id"],)).fetchone()["stats"][
        "auto_deferred"]
    formal = classification()
    assert formal["source"] == "ai" and formal["purpose"] == "support" and not formal["locked"]
    assert float(formal["confidence"]) == .95 and formal["confirmed_by_authorization_id"] is None
    assert next(j for j in matching.page_data(pg, owner, settings)["jobs"] if j["id"] == high["id"])["auto_linked"]
    with monkeypatch.context() as no_list_evidence:
        no_list_evidence.setattr(evidence, "still_valid",
                                lambda *_a, **_k: pytest.fail("Formal list needs no evidence"))
        compact_page = matching.page_data(pg, owner, settings)
    assert all(j["result"] is None for j in compact_page["jobs"])
    assert compact_page["summary_counts"] == {"classified": 2}
    assert next(j for j in compact_page["jobs"] if j["id"] == high["id"])["has_evidence"]
    assert next(j for j in compact_page["jobs"] if j["id"] == high["id"])["display_status"] == "已入库"
    pg.execute("""UPDATE creator_group_jobs SET stats=stats||'{"auto_deferred":true}'::jsonb,
        not_before=NOW()+INTERVAL '1 hour' WHERE id=%s""", (high["id"],))
    reclassify("training", .99)
    newer = classification()
    assert newer["purpose"] == "training" and newer["revision"] == formal["revision"] + 1
    assert evidence.still_valid(pg, high["refs"], settings)  # Old material is valid, but a newer decision wins.
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW() WHERE id=%s", (high["id"],))
    before = len(calls)
    assert matching.prepare_next(settings) and matching.claim() is None
    assert len(calls) == before and classification() == newer
    assert not pg.execute("SELECT stats FROM creator_group_jobs WHERE id=%s", (high["id"],)).fetchone()["stats"][
        "auto_deferred"]
    formal = newer
    for purpose, confidence, refs in (("community", .94, ["M1"]), ("unknown", 1, ["M1"]),
                                      ("category", 1, ["G1"])):
        reclassify(purpose, confidence, refs)
        assert classification() == formal  # Low confidence, unknown and a name alone never become formal.
        assert not next(j for j in matching.page_data(pg, owner, settings)["jobs"]
                        if j["chat_id"] == "oc_topic")["auto_linked"]
    pg.execute("UPDATE chat_messages SET parent_id='om_unavailable' WHERE chat_id='oc_topic'")
    missing = reclassify("community", 1, ["M1"])
    assert missing["pack"]["coverage"]["missing_reply_ids"] == ["M1"]
    assert classification() == formal  # A cited reply without its readable question remains a suggestion.
    pg.execute("UPDATE chat_messages SET parent_id=NULL WHERE chat_id='oc_topic'")
    # The same paid material may be admitted under a newly lowered threshold without another call.
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='oc_topic' AND message_id='om_one'",
               (cipher.encrypt(json.dumps({"text": "Please join this one-off campaign this week."})),))
    reclassify("campaign", .94)
    assert classification() == formal
    legacy_job = pg.execute("SELECT id,result_ciphertext FROM creator_group_jobs WHERE chat_id='oc_topic' "
                            "ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
    legacy_result = json.loads(matching.creator_data.cipher(settings).decrypt(legacy_job["result_ciphertext"]))
    legacy_result["reason"] = "Previously paid v1.1 explanation"
    pg.execute("UPDATE creator_group_jobs SET stats=stats||jsonb_build_object('prompt_version',%s::text),"
               "result_ciphertext=%s WHERE id=%s", (evidence.CLASSIFICATION_VERSION,
                   matching.creator_data.cipher(settings).encrypt(json.dumps(legacy_result)), legacy_job["id"]))
    assert matching.enqueue(pg, owner) == {"queued": 0}  # Output-only upgrades never restart already-paid groups.
    enable(threshold=.93)
    before = len(calls)
    cached_id = pg.execute("SELECT enqueue_creator_group('oc_topic','history',TRUE) id").fetchone()["id"]
    assert matching.prepare_next(settings) and matching.claim() is None
    assert len(calls) == before
    assert pg.execute("SELECT status,reason_code FROM creator_group_jobs WHERE id=%s", (cached_id,)).fetchone() == {
        "status": "completed", "reason_code": "cached",
    }
    cached_formal = classification()
    assert cached_formal["source"] == "ai" and cached_formal["purpose"] == "campaign"
    assert cached_formal["source_job_id"] == cached_id and float(cached_formal["confidence"]) == .94
    assert cached_formal["revision"] == formal["revision"] + 1
    enable()
    formal = cached_formal
    manual = classification("oc_group")
    reclassify("category", 1, chat_id="oc_group")
    assert classification("oc_group") == manual  # Explicit rechecking cannot overwrite a human lock.

    topic = next(j for j in matching.page_data(pg, owner, settings)["jobs"] if j["chat_id"] == "oc_topic")
    matching.action(pg, owner, topic["id"], "recheck")
    timeout_job = prepared()

    def unknown_transport(*args, **kwargs):
        calls.append({"transport_unknown": True})
        raise matching.api.OpenAIRequestError(
            "Synthetic timeout", retryable=True, telemetry={"usage": {"input_tokens": 40, "output_tokens": 0}},
        )

    monkeypatch.setattr(matching.api, "call_openai", unknown_transport)
    matching.run(timeout_job, settings)
    assert pg.execute("SELECT status,reason_code,attempts FROM creator_group_jobs WHERE id=%s",
                      (timeout_job["id"],)).fetchone() == {
        "status": "failed", "reason_code": "request_unknown", "attempts": 1,
    }
    # This fixture wraps all operations in one transaction; mimic separate real request timestamps.
    pg.execute("UPDATE creator_group_jobs SET created_at=clock_timestamp() WHERE id=%s", (timeout_job["id"],))
    matching.touch_chats(pg, ["oc_topic"], settings)
    before = len(calls)
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW() WHERE chat_id='oc_topic' AND status='queued'")
    assert matching.prepare_next(settings) and matching.claim() is None
    assert len(calls) == before  # Same material is re-prepared locally, never paid again.
    assert pg.execute("SELECT input_tokens,error_code FROM creator_group_requests WHERE job_id=%s",
                      (timeout_job["id"],)).fetchone() == {"input_tokens": 40, "error_code": "request_unknown"}
    history = pg.execute("SELECT enqueue_creator_group('oc_topic','history',TRUE) id").fetchone()["id"]
    assert matching.prepare_next(settings) and matching.claim() is None and len(calls) == before
    reused = pg.execute("SELECT status,reason_code FROM creator_group_jobs WHERE id=%s", (history,)).fetchone()
    assert (reused["status"], reused["reason_code"]) in {
        ("failed", "request_unknown"), ("completed", "cached"),
    }  # Reusing a known valid result is also safe; neither path submits another paid request.

    matching.action(pg, owner, timeout_job["id"], "recheck")
    rotated_job = prepared()
    before = len(calls)
    enable("sk-rotated-synthetic-only")
    matching.configuration_changed(pg)
    matching.run(rotated_job, settings)
    assert len(calls) == before
    assert pg.execute("SELECT input_tokens,output_tokens,error_code FROM creator_group_requests WHERE id=%s",
                      (rotated_job["reservation_id"],)).fetchone() == {
        "input_tokens": 0, "output_tokens": 0, "error_code": "not_sent",
    }
    monkeypatch.setattr(matching.api, "call_openai", respond)
    revoked_job = prepared()
    answer.update(purpose="community", confidence=1, refs=["M1"])

    def revoke_during_response(*args, **kwargs):
        result = respond(*args, **kwargs)
        pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (owner,))
        return result

    monkeypatch.setattr(matching.api, "call_openai", revoke_during_response)
    matching.run(revoked_job, settings)
    assert pg.execute("SELECT status,result_ciphertext FROM creator_group_jobs WHERE id=%s",
                      (revoked_job["id"],)).fetchone() == {"status": "cancelled", "result_ciphertext": None}
    assert pg.execute("SELECT input_tokens FROM creator_group_requests WHERE id=%s",
                      (revoked_job["reservation_id"],)).fetchone()["input_tokens"] == 100
    assert classification() == formal  # A high score cannot bypass authorization revoked during the response.
    assert pg.execute("SELECT purpose,locked FROM chat_group_classifications WHERE chat_id='oc_group'").fetchone() == {
        "purpose": "support", "locked": True,
    }
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (owner,))
    monkeypatch.setattr(matching.api, "call_openai", respond)
    matching.confirm(pg, owner, "oc_topic", "category", ["Human"], locked=False)
    manual = classification()
    assert manual["source"] == "manual" and manual["confidence"] is None
    reclassify("support", 1)
    assert classification() == manual  # Human override replaces an AI row and remains authoritative unlocked.

    pg.execute("SAVEPOINT group_context_reprocessing")

    def seed_group(chat_id, count=1, parents=None):
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'Synthetic context group','group')", (chat_id,))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (owner, chat_id))
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,'ou_creator')", (chat_id,))
        for n in range(count):
            pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
                body_ciphertext,message_create_time,parent_id)
                VALUES(%s,%s,'ou_creator','user','text',%s,NOW()+(%s*INTERVAL '1 second'),%s)""",
                (chat_id, f"{chat_id}_{n}", cipher.encrypt(json.dumps({"text": f"Synthetic sample service {n}"})),
                 n, (parents or {}).get(n)))
            pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
                VALUES(%s,%s,%s)""", (owner, chat_id, f"{chat_id}_{n}"))
        return pg.execute("SELECT enqueue_creator_group(%s,'history',TRUE) id", (chat_id,)).fetchone()["id"]

    context_chat = "oc_context"
    seed_group(context_chat, 72, {18: "oc_context_17", 17: "oc_context_16", 16: "oc_context_13",
                                31: "oc_context_26", 26: "oc_context_23",
                                65: "oc_context_66", 66: "oc_context_65", 67: "om_unavailable"})
    context = evidence.build(pg, settings, context_chat)
    context_pack = context["pack"]
    labels = {r["message_id"]: r["label"] for r in context["refs"] if r["kind"] == "message"}
    by_label = {m["id"]: m for m in context_pack["messages"]}
    # A previous message and a fetched parent both need their own complete parent chains.
    for child, parent in ((18, 17), (17, 16), (16, 13), (31, 26), (26, 23)):
        assert by_label[labels[f"oc_context_{child}"]]["reply"] == labels[f"oc_context_{parent}"]
    assert len(by_label) <= evidence.MAX_MESSAGES
    assert sum(len(m["text"]) for m in by_label.values()) <= evidence.MAX_CHARS
    missing_labels = set(context_pack["coverage"]["missing_reply_ids"])
    assert {labels[f"oc_context_{n}"] for n in (65, 66, 67)} <= missing_labels
    assert context_pack["coverage"]["missing_reply_reasons"][labels["oc_context_65"]] == "cycle"
    assert context_pack["coverage"]["missing_reply_reasons"][labels["oc_context_67"]] == "unavailable"
    allowed = set(evidence.output_format(context_pack)["schema"]["properties"]["refs"]["items"]["enum"])
    assert not missing_labels.intersection(allowed)
    complete_label = labels["oc_context_18"]
    assert complete_label in allowed
    legacy_missing = {"purpose": "support", "confidence": .98, "tags": [],
                      "refs": ["G1", complete_label, labels["oc_context_67"]]}
    assert evidence.validate(context_pack, legacy_missing) == legacy_missing  # Never prune refs and inherit .98.
    context_job = pg.execute("SELECT * FROM creator_group_jobs WHERE chat_id=%s", (context_chat,)).fetchone()
    assert matching._auto_classify(pg, context_job, context_pack, context["refs"], legacy_missing,
                                  matching._config(pg), settings) is False
    complete_result = {**legacy_missing, "confidence": .95, "refs": ["G1", complete_label]}
    assert matching._auto_classify(pg, context_job, context_pack, context["refs"], complete_result,
                                  matching._config(pg), settings) is True  # Uncited incomplete material is harmless.
    pg.execute("SAVEPOINT group_context_budget")
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id=%s",
               (cipher.encrypt(json.dumps({"text": "S" * 900})), context_chat))
    budget = evidence.build(pg, settings, context_chat)
    assert budget["stats"]["omitted_messages"] > 0
    assert len(budget["pack"]["messages"]) <= evidence.MAX_MESSAGES
    assert sum(len(m["text"]) for m in budget["pack"]["messages"]) <= evidence.MAX_CHARS
    pg.execute("ROLLBACK TO SAVEPOINT group_context_budget")

    storage = matching.creator_data.cipher(settings)

    def saved_suggestion(chat_id, *, confidence=.98, purpose="support", parent=None):
        job_id = seed_group(chat_id, parents={0: parent} if parent else None)
        material = evidence.build(pg, settings, chat_id)
        result = {"purpose": purpose, "confidence": confidence, "tags": [], "refs": ["G1", "M1"]}
        pg.execute("""UPDATE creator_group_jobs SET status='completed',reason_code='suggestions_saved',
            pack_ciphertext=%s,result_ciphertext=%s,refs=%s::jsonb,stats=%s::jsonb,
            snapshot_version=desired_version,config_fingerprint=%s,finished_at=NOW() WHERE id=%s""",
            (storage.encrypt(json.dumps(material["pack"])), storage.encrypt(json.dumps(result)),
             matching.creator_data.encode(material["refs"]), json.dumps({**material["stats"],
                 "prompt_version": "creator-group-purpose-v1.2", "auto_linked": False, "auto_deferred": False}),
             matching._config_hash(matching._config(pg)), job_id))
        return pg.execute("SELECT * FROM creator_group_jobs WHERE id=%s", (job_id,)).fetchone()

    safe = saved_suggestion("oc_local_admit")
    low = saved_suggestion("oc_low", confidence=.94)
    unknown = saved_suggestion("oc_unknown", purpose="unknown")
    stale = saved_suggestion("oc_stale")
    missing_old = saved_suggestion("oc_missing_old", parent="om_original_question")
    incompatible = saved_suggestion("oc_incompatible")
    pg.execute("UPDATE creator_group_jobs SET stats=stats||'{\"prompt_version\":\"unsupported\"}'::jsonb WHERE id=%s",
               (incompatible["id"],))
    saved_suggestion("oc_manual_protected")
    matching.confirm(pg, owner, "oc_manual_protected", "unknown", [], locked=False)
    active_id = seed_group("oc_active")
    pg.execute("UPDATE creator_group_jobs SET status='running',claim_token=%s,lease_until=NOW()+INTERVAL '10 minutes' "
               "WHERE id=%s", (uuid4(), active_id))
    active_before = pg.execute("SELECT * FROM creator_group_jobs WHERE id=%s", (active_id,)).fetchone()
    unknown_request_id = seed_group("oc_request_unknown")
    pg.execute("UPDATE creator_group_jobs SET status='failed',reason_code='request_unknown' WHERE id=%s",
               (unknown_request_id,))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id='oc_stale'",
               (cipher.encrypt(json.dumps({"text": "Synthetic changed campaign evidence"})),))
    before = len(calls)
    before_requests = pg.execute("SELECT count(*) n FROM creator_group_requests").fetchone()["n"]
    with pytest.raises(PermissionError):
        matching.reprocess_unclassified(pg, employee)
    counts = matching.reprocess_unclassified(pg, owner)
    assert counts["admitted"] == 1 and counts["queued"] == 5
    assert counts["active"] >= 1 and counts["protected"] >= 3 and counts["request_unknown"] == 1
    assert len(calls) == before
    assert pg.execute("SELECT count(*) n FROM creator_group_requests").fetchone()["n"] == before_requests
    assert classification("oc_local_admit")["source_job_id"] == safe["id"]
    assert float(classification("oc_local_admit")["confidence"]) == .98
    assert "creator-group-purpose-v1.2" in evidence.COMPATIBLE_PROMPT_VERSIONS
    assert classification("oc_manual_protected")["source"] == "manual"
    assert pg.execute("SELECT * FROM creator_group_jobs WHERE id=%s", (active_id,)).fetchone() == active_before
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs WHERE chat_id='oc_request_unknown'").fetchone()[
        "n"] == 1
    for old in (low, unknown, stale, missing_old, incompatible):
        latest = pg.execute("SELECT * FROM creator_group_jobs WHERE chat_id=%s "
                            "ORDER BY created_at DESC,id DESC LIMIT 1", (old["chat_id"],)).fetchone()
        assert latest["id"] != old["id"] and latest["status"] == "queued" and latest["source"] == "history"
        assert latest["stats"]["force_reclassify"] and latest["result_ciphertext"] is None
        assert pg.execute("SELECT result_ciphertext FROM creator_group_jobs WHERE id=%s", (old["id"],)).fetchone()[
            "result_ciphertext"] == old["result_ciphertext"]
    missing_latest = pg.execute("SELECT * FROM creator_group_jobs WHERE chat_id='oc_missing_old' "
                                "ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW()+INTERVAL '1 day' "
               "WHERE status='queued' AND id<>%s", (missing_latest["id"],))
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
        body_ciphertext,message_create_time) VALUES('oc_missing_old','om_original_question','ou_creator','user','text',
        %s,NOW()+INTERVAL '1 minute')""", (cipher.encrypt(json.dumps({"text": "Synthetic original sample question"})),))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'oc_missing_old','om_original_question')""", (owner,))
    answer.update(purpose="support", confidence=.96, refs=["G1", "M1", "M2"])
    assert matching.prepare_next(settings)
    assert pg.execute("SELECT status,stats FROM creator_group_jobs WHERE id=%s", (missing_latest["id"],)).fetchone()[
        "stats"]["force_reclassify"]
    enable(key="sk-synthetic-reprocess-rotation")
    assert matching.claim() is None  # Invalidation must not lose the explicit request for a new model score.
    fresh_missing = prepared()
    assert fresh_missing["chat_id"] == "oc_missing_old" and not fresh_missing["pack"]["coverage"]["missing_reply_ids"]
    matching.run(fresh_missing, settings)
    assert len(calls) == before + 1
    assert float(classification("oc_missing_old")["confidence"]) == .96  # New evidence gets a new score, not old .98.
    pg.execute("ROLLBACK TO SAVEPOINT group_context_reprocessing")

    # An unclassified group's unknown attempt must not freeze genuinely new conversation material.
    fresh_chat = "oc_new_material_after_unknown"
    seed_group(fresh_chat)
    monkeypatch.setattr(matching.api, "call_openai", unknown_transport)
    frozen = prepared()
    matching.run(frozen, settings)
    pg.execute("UPDATE creator_group_jobs SET created_at=clock_timestamp() WHERE id=%s", (frozen["id"],))
    before = len(calls)
    matching.touch_chats(pg, [fresh_chat], settings)
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW() WHERE chat_id=%s AND status='queued'", (fresh_chat,))
    assert matching.prepare_next(settings) and matching.claim() is None and len(calls) == before
    history = pg.execute("SELECT enqueue_creator_group(%s,'history',TRUE) id", (fresh_chat,)).fetchone()["id"]
    assert matching.prepare_next(settings) and matching.claim() is None and len(calls) == before
    assert pg.execute("SELECT reason_code FROM creator_group_jobs WHERE id=%s", (history,)).fetchone() == {
        "reason_code": "request_unknown",
    }
    pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
        body_ciphertext,message_create_time) VALUES(%s,'om_new_material','ou_creator','user','text',%s,NOW())""",
        (fresh_chat, cipher.encrypt(json.dumps({"text": "Please send a product sample for our next campaign."}))))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,%s,'om_new_material')""", (owner, fresh_chat))
    matching.touch_chats(pg, [fresh_chat], settings)
    pg.execute("UPDATE creator_group_jobs SET not_before=NOW() WHERE chat_id=%s AND status='queued'", (fresh_chat,))
    fresh_material = prepared()
    assert fresh_material["pack"] != frozen["pack"]
    monkeypatch.setattr(matching.api, "call_openai", respond)
    answer.update(purpose="support", confidence=.96, refs=["M1"])
    matching.run(fresh_material, settings)
    assert len(calls) == before + 1

    # The HTTP boundary also applies live owner checks, CSRF and independent manual classification.
    actor = {"id": owner, "status": "active"}
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                            autoescape=select_autoescape(["html"]))
    app = FastAPI()
    app.include_router(routes.create_router(
        current_authorization=lambda _: actor,
        render_template=lambda request, name, context, status_code=200: HTMLResponse(
            templates.get_template(name).render(**context), status_code=status_code),
        csrf_token=lambda _: "csrf",
    ))
    client = TestClient(app)
    path = "/admin/ai/creator-groups/chats/oc_group/classification"
    assert client.post(path, data={"csrf_token": "wrong", "purpose": "training"}).status_code == 400
    actor["id"] = employee
    assert client.get("/admin/ai/creator-groups").status_code == 403
    assert client.post(path, data={"csrf_token": "csrf", "purpose": "training"}).status_code == 403
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission,granted_by) VALUES(%s,'ai',%s)",
               (employee, owner))
    readonly = client.get("/admin/ai/creator-groups")
    assert readonly.status_code == 200 and 'method="post"' not in readonly.text
    assert "/evidence" not in readonly.text and "Can you fix my sample request?" not in readonly.text
    assert client.get(f"/admin/ai/creator-groups/jobs/{high['id']}/evidence").status_code == 403
    assert client.post(path, data={"csrf_token": "csrf", "purpose": "training"}).status_code == 403
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (employee,))
    assert client.get("/admin/ai/creator-groups").status_code == 403
    actor["id"] = owner
    enable(enabled=False)
    matching.configuration_changed(pg)
    assert client.post(path, data={"csrf_token": "csrf", "purpose": "support", "tags": "Manual", "locked": "1"},
                       follow_redirects=False).status_code == 303
    audits = pg.execute("SELECT metadata FROM audit_events WHERE action LIKE 'creator_group_%'").fetchall()
    assert "Can you fix my sample request?" not in json.dumps(audits, default=str)
    assert pg.execute("SELECT count(*) n FROM contact_roles WHERE role='creator' AND status='confirmed'").fetchone()[
        "n"] == 1
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 0
    assert evidence.PROMPT_VERSION == matching.page_data(pg, owner, settings)["prompt_version"]
