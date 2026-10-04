"""One real PostgreSQL group-classification workflow; every model call is synthetic."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

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

    def respond(feature, key, pack, target, **kwargs):
        calls.append({"feature": feature, "key": key, "pack": pack, "kwargs": kwargs})
        return {
            "text": json.dumps({"purpose": "support", "confidence": .94, "tags": [],
                                "reason": "Individual creator support", "refs": ["G1", "M1"]}),
            "response_id": "resp_synthetic", "usage": {"input_tokens": 100, "output_tokens": 50},
            "telemetry": {"openai_request_id": "req_synthetic"},
        }

    monkeypatch.setattr(matching.api, "call_openai", respond)

    def enable(key="sk-synthetic-only", enabled=True):
        ai_feature_config.save_feature_config(
            pg, ai_feature_config.api_key_cipher(settings.token_encryption_key), matching.FEATURE,
            provider="openai", project_id="proj_synthetic", model="gpt-test", enabled=enabled,
            api_key=key, updated_by_authorization_id=owner,
        )

    def prepared():
        assert matching.prepare_next(settings)
        job = matching.claim()
        assert job is not None
        return job

    assert not matching._enabled(matching._config(pg))
    assert pg.execute("SELECT count(*) n FROM creator_group_jobs").fetchone()["n"] == 0
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
        matching.run(prepared(), settings)
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
    matching.touch_chats(pg, ["oc_topic"], settings)
    assert not matching.prepare_next(settings) and matching.claim() is None
    assert pg.execute("SELECT input_tokens,error_code FROM creator_group_requests WHERE job_id=%s",
                      (timeout_job["id"],)).fetchone() == {"input_tokens": 40, "error_code": "request_unknown"}

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
    assert pg.execute("SELECT purpose,locked FROM chat_group_classifications WHERE chat_id='oc_group'").fetchone() == {
        "purpose": "support", "locked": True,
    }
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (owner,))
    # The HTTP boundary also applies live owner checks, CSRF and independent manual classification.
    actor = {"id": owner, "status": "active"}
    app = FastAPI()
    app.include_router(routes.create_router(
        current_authorization=lambda _: actor, render_template=lambda *args: HTMLResponse("Synthetic page"),
        csrf_token=lambda _: "csrf",
    ))
    client = TestClient(app)
    path = "/admin/ai/creator-groups/chats/oc_group/classification"
    assert client.post(path, data={"csrf_token": "wrong", "purpose": "training"}).status_code == 400
    actor["id"] = employee
    assert client.get("/admin/ai/creator-groups").status_code == 403
    assert client.post(path, data={"csrf_token": "csrf", "purpose": "training"}).status_code == 403
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
