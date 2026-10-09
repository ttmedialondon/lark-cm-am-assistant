"""Chat-only ranking on isolated PostgreSQL; every provider response is synthetic."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import ai_feature_config, creator_access, openai_client
from app import creator_importance as importance
from app.crypto import message_body_cipher, message_body_fingerprint


@pytest.fixture
def ranking_flow(pg, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b"i" * 32)
    owner, other_cm, person = uuid4(), uuid4(), uuid4()
    now = pg.execute("SELECT NOW() now").fetchone()["now"]
    pg.execute(
        """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,body_consent_at)
        VALUES(%s,'ou_owner','Owner','synthetic',NOW()-INTERVAL '1 day'),
        (%s,'ou_other','Other CM','synthetic',NOW()-INTERVAL '1 day')""",
        (owner, other_cm),
    )
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (owner,))
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Synthetic creator')", (person,))
    pg.execute(
        """INSERT INTO lark_identities(open_id,contact_id,tenant_classification)
        VALUES('ou_creator',%s,'known_external'),('ou_owner',NULL,'internal'),('ou_other',NULL,'internal')""",
        (person,),
    )
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')", (person,)
    )
    for cm, chat in ((owner, "own_private"), (other_cm, "other_private")):
        pg.execute(
            """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
            VALUES(%s,%s,%s,'manual')""",
            (uuid4(), person, cm),
        )
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'p2p')", (chat, chat))
        pg.execute(
            """INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
            backfill_complete,backfill_window_start_at,backfill_window_end_at,last_backfill_at,sync_status)
            VALUES(%s,%s,'ou_creator',TRUE,%s,NOW()-INTERVAL '1 hour',NOW(),'succeeded')""",
            (cm, chat, None if cm == owner else now - timedelta(days=100)),
        )
    cipher = message_body_cipher(settings.token_encryption_key)
    state = SimpleNamespace(
        settings=settings,
        owner=owner,
        other=other_cm,
        person=person,
        now=now,
        calls=[],
        before_response=None,
        error=None,
    )

    def message(identifier, text, *, cm=owner, creator=False, at=None, parent=None):
        chat = "own_private" if cm == owner else "other_private"
        sender = "ou_creator" if creator else "ou_owner" if cm == owner else "ou_other"
        raw = json.dumps({"text": text})
        pg.execute(
            """INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,
            body_ciphertext,body_fingerprint,message_create_time,parent_id)
            VALUES(%s,%s,%s,'user','text',%s,%s,%s,%s)""",
            (
                chat,
                identifier,
                sender,
                cipher.encrypt(raw),
                message_body_fingerprint(settings.token_encryption_key, raw),
                at or now - timedelta(days=10),
                parent,
            ),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,%s,%s)",
            (cm, chat, identifier),
        )

    def enable(enabled=True):
        ai_feature_config.save_feature_config(
            pg,
            ai_feature_config.api_key_cipher(settings.token_encryption_key),
            importance.FEATURE,
            provider="openai",
            project_id="proj_synthetic",
            model="gpt-synthetic",
            enabled=enabled,
            api_key="sk-synthetic-ranking",
            updated_by_authorization_id=owner,
        )
        importance.configuration_changed(pg)

    def respond(_config, key, pack, target, **kwargs):
        assert key == "sk-synthetic-ranking" and str(target) == str(person)
        state.calls.append(deepcopy(pack))
        if state.before_response:
            callback, state.before_response = state.before_response, None
            callback()
        if state.error:
            raise state.error
        rows = []
        for marker, kind, obj in (
            ("ONGOING", "ongoing", "Acme weekly lives"),
            ("SINGLE", "agreed", "Beta single video"),
            ("GAMMA", "agreed", "Gamma autumn video"),
        ):
            cited = [m for m in pack["messages"] if marker in m["text"] and m["role"] in {"creator", "cm"}]
            if {m["role"] for m in cited} == {"creator", "cm"}:
                old = next((f["key"] for f in pack["previous_facts"] if f["object"] == obj), None)
                rows.append({"kind": kind, "object": obj, "previous_key": old, "refs": [m["ref"] for m in cited]})
        return {
            "text": json.dumps({"facts": rows}),
            "response_id": "resp_synthetic",
            "usage": {"input_tokens": 101, "output_tokens": 23},
            "telemetry": {"http_status": 200, "openai_request_id": "req_synthetic"},
        }

    def due():
        pg.execute("UPDATE creator_importance_jobs SET not_before=NOW()-INTERVAL '1 second'")

    def drain():
        for _ in range(40):
            due()
            progressed = importance.prepare_next(settings)
            job = importance.claim()
            if job:
                importance.run(job, settings)
            if not progressed and not job:
                return
        pytest.fail("importance queue did not drain")

    monkeypatch.setattr(importance, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(importance, "get_settings", lambda: settings)
    monkeypatch.setattr(importance.api, "call_openai", respond)
    monkeypatch.setattr(importance, "MAX_MESSAGES", 1)
    state.message, state.enable, state.due, state.drain = message, enable, due, drain
    state.cipher = cipher
    message("a1", "ONGOING Acme: please join our agreed weekly lives", at=now - timedelta(days=20))
    message(
        "a2",
        "ONGOING Acme: agreed, I will do our weekly lives",
        creator=True,
        at=now - timedelta(days=20) + timedelta(minutes=1),
    )
    message("a3", "Thanks", at=now - timedelta(days=19))
    message("a4", "Thank you", creator=True, at=now - timedelta(days=19) + timedelta(minutes=1), parent="a3")
    message("b1", "SINGLE Beta: our confidential one-video agreement", cm=other_cm, at=now - timedelta(days=35))
    message(
        "b2",
        "SINGLE Beta: I accept the confidential one-video agreement",
        cm=other_cm,
        creator=True,
        at=now - timedelta(days=35) + timedelta(minutes=1),
        parent="b1",
    )
    return state


@pytest.mark.parametrize("missing", ["backfill_complete", "backfill_window_end_at", "last_backfill_at",
                                    "recent_start", "not_readable"])
def test_history_coverage_requires_successful_full_sync(pg, ranking_flow, missing):
    flow = ranking_flow
    flow.enable()
    if missing == "recent_start":
        pg.execute("UPDATE authorization_chats SET backfill_window_start_at=NOW()-INTERVAL '7 days'")
    elif missing == "not_readable":
        pg.execute("UPDATE authorization_chats SET sync_status='not_readable'")
    else:
        value = "FALSE" if missing == "backfill_complete" else "NULL"
        pg.execute(f"UPDATE authorization_chats SET {missing}={value}")
    job = pg.execute("""SELECT *,NOW()-INTERVAL '90 days' history_start,NOW() history_end
        FROM creator_importance_jobs WHERE responsible_authorization_id=%s""", (flow.owner,)).fetchone()
    material = importance.build_batch(pg, job, flow.settings, importance._state(job, flow.settings))
    assert material["state"]["chat_coverage"] == {"own_private": False}
    assert not flow.calls


def test_history_incremental_ranking_permissions_and_source_fences(pg, ranking_flow):
    flow = ranking_flow
    assert not importance.prepare_next(flow.settings) and importance.claim() is None
    assert pg.execute("SELECT count(*) n FROM creator_importance_jobs").fetchone()["n"] == 0
    flow.enable()
    assert pg.execute("SELECT count(*) n FROM creator_importance_jobs").fetchone()["n"] == 2
    # AFTER triggers must see the newly admitted role/assignment in that very SQL command.
    pg.execute("SAVEPOINT importance_admission")
    new_creator, new_cm = uuid4(), uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'New creator')", (new_creator,))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id) VALUES('ou_new_creator',%s)", (new_creator,))
    pg.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        VALUES(%s,%s,%s,'manual')""",
        (uuid4(), new_creator, flow.owner),
    )
    assert pg.execute("SELECT id FROM creator_importance_jobs WHERE contact_id=%s", (new_creator,)).fetchone() is None
    pg.execute(
        "INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
        (new_creator,),
    )
    assert (
        pg.execute("SELECT status FROM creator_importance_jobs WHERE contact_id=%s", (new_creator,)).fetchone()[
            "status"
        ]
        == "queued"
    )
    pg.execute(
        """INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at)
        VALUES(%s,'ou_new_cm','synthetic',NOW()-INTERVAL '1 day')""",
        (new_cm,),
    )
    pg.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source)
        VALUES(%s,%s,%s,'manual')""",
        (uuid4(), flow.person, new_cm),
    )
    assert (
        pg.execute(
            "SELECT status FROM creator_importance_jobs WHERE responsible_authorization_id=%s", (new_cm,)
        ).fetchone()["status"]
        == "queued"
    )
    pg.execute("ROLLBACK TO SAVEPOINT importance_admission")
    flow.due()
    assert importance.prepare_next(flow.settings)
    first = importance.claim()
    assert first is not None
    importance.run(first, flow.settings)
    partial = pg.execute(
        "SELECT auto_valid FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
        (first["responsible_authorization_id"],),
    ).fetchone()
    assert partial and not partial["auto_valid"]
    assert (
        importance.view_for(pg, flow.person, first["responsible_authorization_id"], flow.settings)["tier"] == "pending"
    )
    flow.drain()
    profiles = {
        r["responsible_authorization_id"]: r for r in pg.execute("SELECT * FROM creator_importance_profiles").fetchall()
    }
    assert profiles[flow.owner]["auto_tier"] == "priority" and profiles[flow.other]["auto_tier"] == "normal"
    assert profiles[flow.owner]["last_substantive_progress_at"] == flow.now - timedelta(days=20) + timedelta(minutes=1)
    assert all(r["coverage"]["history_complete"] for r in profiles.values())
    assert all(r["coverage"]["source_history_complete"] for r in profiles.values())
    assert len(flow.calls) >= 3  # The first history was actually split, not read as one unbounded request.
    for pack in flow.calls:
        private = "own_private" if pack["cm_id"] == str(flow.owner) else "other_private"
        assert all(m["chat_id"] == private for m in pack["messages"])
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["tier"] == "priority"
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "dormant"
    pg.execute("SAVEPOINT brief_roster")
    pg.execute("UPDATE creator_importance_profiles SET facts_ciphertext='not-a-ciphertext'")
    audits = pg.execute("SELECT count(*) n FROM audit_events").fetchone()["n"]
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings, details=False)["tier"] == "priority"
    assert importance.view_for(pg, flow.person, flow.other, flow.settings, details=False)["tier"] == "dormant"
    assert pg.execute("SELECT count(*) n FROM audit_events").fetchone()["n"] == audits
    pg.execute("ROLLBACK TO SAVEPOINT brief_roster")
    assert profiles[flow.owner]["coverage"]["has_readable"]
    assert profiles[flow.owner]["coverage"]["priority"]["business_days"]
    join, args = importance.roster_sql(flow.other)
    # An old stored focus label must not defeat the current 30-day maintenance window.
    pg.execute("UPDATE creator_importance_profiles SET auto_tier='focus' "
               "WHERE responsible_authorization_id=%s", (flow.other,))
    assert pg.execute("SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s",
                      [*args, flow.person]).fetchone()["effective_tier"] == "dormant"
    pg.execute("SAVEPOINT current_priority_metadata")
    today = flow.now.astimezone(importance.LONDON).date().isoformat()
    yesterday = (flow.now.astimezone(importance.LONDON).date() - timedelta(days=1)).isoformat()
    pg.execute("UPDATE creator_importance_profiles SET last_substantive_progress_at=%s,"
               "coverage=jsonb_set(coverage,'{priority}',%s::jsonb) "
               "WHERE responsible_authorization_id=%s", (flow.now - timedelta(days=35), json.dumps({
                   "primary_at": (flow.now - timedelta(days=35)).isoformat(),
                   "business_at": (flow.now - timedelta(minutes=2)).isoformat(),
                   "business_days": [today, today, yesterday, "2020-01-01"], "historical": True,
               }), flow.other))
    assert pg.execute("SELECT ip.effective_tier,ip.recency_rank,ip.business_days,ip.has_history,"
                      "ip.last_substantive_progress_at "
                      "FROM contacts c" + join + " WHERE c.id=%s", [*args, flow.person]).fetchone() == {
                          "effective_tier": "dormant", "recency_rank": 2, "business_days": 2, "has_history": True,
                          "last_substantive_progress_at": flow.now - timedelta(days=35),
                      }
    pg.execute("UPDATE creator_importance_profiles SET coverage=jsonb_set(coverage,'{priority,primary_at}',%s::jsonb) "
               "WHERE responsible_authorization_id=%s", (json.dumps(
                   (flow.now - timedelta(days=20)).isoformat()), flow.other))
    assert pg.execute("SELECT ip.effective_tier,ip.recency_rank,ip.last_substantive_progress_at "
                      "FROM contacts c" + join + " WHERE c.id=%s", [*args, flow.person]).fetchone() == {
                          "effective_tier": "priority", "recency_rank": 1,
                          "last_substantive_progress_at": flow.now - timedelta(days=20),
                      }
    pg.execute("UPDATE creator_importance_profiles SET last_substantive_progress_at=%s "
               "WHERE responsible_authorization_id=%s", (flow.now - timedelta(minutes=2), flow.other))
    assert pg.execute("SELECT ip.effective_tier,ip.recency_rank,ip.last_substantive_progress_at "
                      "FROM contacts c" + join + " WHERE c.id=%s", [*args, flow.person]).fetchone() == {
                          "effective_tier": "priority", "recency_rank": 0,
                          "last_substantive_progress_at": flow.now - timedelta(minutes=2),
                      }
    pg.execute("ROLLBACK TO SAVEPOINT current_priority_metadata")
    pg.execute("SAVEPOINT readable_without_facts")
    other_job = dict(pg.execute("SELECT * FROM creator_importance_jobs WHERE responsible_authorization_id=%s",
                                (flow.other,)).fetchone())
    proof = next(source for source in profiles[flow.other]["sources"] if source["message_id"] == "b2")
    importance._save_profile(pg, other_job, {
        "facts": [], "sources": {proof["ref"]: proof}, "readable_ref": proof["ref"],
        "seen": {}, "unreadable": 0, "gaps": 0,
    }, flow.settings, history_complete=True)
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "dormant"
    assert pg.execute("SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s",
                      [*args, flow.person]).fetchone()["effective_tier"] == "dormant"
    flow.enable(False)
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE,body_ciphertext=NULL WHERE message_id='b2'")
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "pending"
    pg.execute("ROLLBACK TO SAVEPOINT readable_without_facts")
    pg.execute("SAVEPOINT activity_metadata")
    flow.enable(False)
    pg.execute("UPDATE creator_importance_profiles SET auto_valid=TRUE,facts_ciphertext=NULL,sources='[]',"
               "coverage='{}' WHERE responsible_authorization_id=%s", (flow.other,))
    pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE chat_id='other_private'",
               (flow.now - timedelta(days=45),))
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "dormant"
    assert pg.execute("SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s",
                      [*args, flow.person]).fetchone()["effective_tier"] == "dormant"
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["tier"] == "priority"
    # A new media message is contact even without readable text; it prevents a false inactivity label.
    flow.message("activity_image", "", cm=flow.other, creator=True, at=flow.now - timedelta(hours=1))
    pg.execute("UPDATE chat_messages SET message_type='image',body_ciphertext=NULL WHERE message_id='activity_image'")
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "pending"
    pg.execute("UPDATE chat_messages SET message_create_time=NOW()+INTERVAL '1 day' WHERE message_id='activity_image'")
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "dormant"
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (flow.other,))
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "pending"
    pg.execute("ROLLBACK TO SAVEPOINT activity_metadata")
    # Unresolved rich-text @ must not discard readable prose, but it proves no recipient identity.
    for prose, parent, readable in (("Please confirm Friday's video", None, True),
                                    ("", None, False),
                                    ("，👍", None, False),
                                    ("Please confirm Friday's video", "missing_original", False)):
        pg.execute("SAVEPOINT rough_mentions")
        flow.message("rough_at", "", cm=flow.other, creator=True,
                     at=flow.now - timedelta(days=80), parent=parent)
        raw = json.dumps({"content": [[{"tag": "at", "user_id": "ou_unknown"},
                                        {"tag": "text", "text": prose}]]})
        pg.execute("UPDATE chat_messages SET message_type='post',body_ciphertext=%s,body_fingerprint=%s "
                   "WHERE message_id='rough_at'", (flow.cipher.encrypt(raw),
                   message_body_fingerprint(flow.settings.token_encryption_key, raw)))
        original = pg.execute("SELECT * FROM chat_messages WHERE message_id='rough_at'").fetchone()
        assert importance.offhours_summary._body(original, flow.cipher)[1] is (not prose.strip())
        job = {**other_job, "needs_history": False, "checkpoint_ciphertext": None,
               "pending_messages": {"other_private/rough_at": {"chat_id": "other_private",
                                                              "message_id": "rough_at"}}}
        material = importance.build_batch(pg, job, flow.settings, importance._state(job, flow.settings))
        assert bool(material["pack"]) is readable
        if readable:
            message = material["pack"]["messages"][0]
            assert message["role"] == "creator" and message["author_id"] == "ou_creator"
            assert message["context_complete"] and "[未解析的@用户]" in message["text"]
        pg.execute("ROLLBACK TO SAVEPOINT rough_mentions")
        pg.execute("RELEASE SAVEPOINT rough_mentions")
    with pytest.raises(PermissionError):
        creator_access.access(pg, flow.other, flow.owner, read_only=True)
    with pytest.raises(PermissionError):
        importance.set_manual(pg, flow.other, flow.person, flow.other, "focus", "not owner", flow.settings)
    importance.set_manual(pg, flow.owner, flow.person, flow.owner, "normal", "Human-reviewed priority", flow.settings)
    raw_manual = pg.execute(
        "SELECT manual_reason_ciphertext FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
        (flow.owner,),
    ).fetchone()["manual_reason_ciphertext"]
    assert "Human-reviewed priority" not in raw_manual
    # Natural replies do not always carry parent_id; the newly confirmed object must survive incremental batching.
    flow.message(
        "gamma_question", "GAMMA Gamma: one agreed video on Friday, please confirm", at=flow.now - timedelta(minutes=2)
    )
    flow.drain()
    flow.message(
        "gamma_acceptance", "GAMMA Gamma: confirmed, I will post the agreed video on Friday",
        creator=True, at=flow.now - timedelta(minutes=1),
    )
    flow.drain()
    material = pg.execute(
        "SELECT facts_ciphertext,sources FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
        (flow.owner,),
    ).fetchone()
    incremental = next(
        fact for fact in json.loads(importance._cipher(flow.settings).decrypt(material["facts_ciphertext"]))
        if fact["object"] == "Gamma autumn video" and fact["kind"] == "agreed"
    )
    refs = {source["ref"]: source["message_id"] for source in material["sources"]}
    assert {refs[ref] for ref in incremental["refs"]} == {"gamma_question", "gamma_acceptance"}
    calls = len(flow.calls)
    flow.message("new_empty", "Thank you, have a nice day", creator=True, at=flow.now - timedelta(minutes=5))
    # Arrival during an active request must survive finishing that request, including an older business timestamp.
    flow.before_response = lambda: flow.message(
        "late", "No new cooperation facts", creator=True, at=flow.now - timedelta(days=80)
    )
    flow.drain()
    assert len(flow.calls) > calls
    owner_profile = pg.execute(
        "SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s", (flow.owner,)
    ).fetchone()
    assert owner_profile["auto_tier"] == "focus" and owner_profile["manual_tier"] == "normal"
    assert (
        pg.execute(
            "SELECT pending_messages FROM creator_importance_jobs WHERE responsible_authorization_id=%s", (flow.owner,)
        ).fetchone()["pending_messages"]
        == {}
    )
    assert any(any(m["message_id"] == "late" for m in pack["messages"]) for pack in flow.calls)
    before = pg.execute(
        "SELECT desired_version FROM creator_importance_jobs WHERE responsible_authorization_id=%s", (flow.owner,)
    ).fetchone()["desired_version"]
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='a2'",
        (flow.cipher.encrypt(json.dumps({"text": "ONGOING Acme: agreed, I will do our weekly lives"})),),
    )
    assert (
        pg.execute(
            "SELECT desired_version FROM creator_importance_jobs WHERE responsible_authorization_id=%s", (flow.owner,)
        ).fetchone()["desired_version"]
        == before
    )
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE,body_ciphertext=NULL WHERE message_id='a2'")
    view = importance.view_for(pg, flow.person, flow.owner, flow.settings)
    assert view["tier"] == "normal" and not view["grounds"] and "依据已变化" in view["gap_label"]
    join, args = importance.roster_sql(flow.owner)
    ordered = pg.execute(
        "SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s", [*args, flow.person]
    ).fetchone()
    assert ordered["effective_tier"] == "normal"
    flow.enable(False)
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (flow.owner,))
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["grounds"] == []
    assert (
        pg.execute(
            "SELECT manual_tier FROM creator_importance_profiles WHERE responsible_authorization_id=%s", (flow.owner,)
        ).fetchone()["manual_tier"]
        == "normal"
    )
    assert importance.view_for(pg, flow.person, flow.other, flow.settings)["tier"] == "dormant"


def test_paid_retry_cache_and_stale_results_keep_billing_without_repeat(pg, ranking_flow):
    flow = ranking_flow
    flow.enable()
    pg.execute(
        "UPDATE creator_importance_jobs SET status='cancelled' WHERE responsible_authorization_id=%s", (flow.other,)
    )

    def prepared():
        for _ in range(20):
            flow.due()
            progressed = importance.prepare_next(flow.settings)
            job = importance.claim()
            if job:
                return job
            assert progressed
        pytest.fail("no payable batch was prepared")

    first = prepared()
    flow.error = openai_client.OpenAIRequestError(
        "synthetic 503", retryable=True, telemetry={"http_status": 503, "usage": {"input_tokens": 2}}
    )
    importance.run(first, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_importance_jobs WHERE id=%s", (first["id"],)).fetchone()["status"]
        == "retry_wait"
    )
    retry_before = pg.execute(
        "SELECT status,attempts,not_before FROM creator_importance_jobs WHERE id=%s", (first["id"],)
    ).fetchone()
    flow.message("during_retry", "An unrelated new statement", creator=True, at=flow.now + timedelta(minutes=1))
    assert pg.execute(
        "SELECT status,attempts,not_before FROM creator_importance_jobs WHERE id=%s", (first["id"],)
    ).fetchone() == retry_before
    flow.error = None
    flow.due()
    retried = importance.claim()
    assert retried and retried["attempts"] == 2
    importance.run(retried, flow.settings)
    successful = pg.execute(
        "SELECT * FROM creator_importance_requests WHERE id=%s", (retried["reservation_id"],)
    ).fetchone()
    assert successful["input_tokens"] == 101 and successful["output_tokens"] == 23
    # Replay the same frozen, already-billed batch from before a crash: reuse encrypted cache, not the provider.
    calls = len(flow.calls)
    checkpoint = importance._state(
        pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (first["id"],)).fetchone(), flow.settings
    )
    checkpoint["facts"] = importance.facts.validate({"facts": [{
        "kind": "intent", "object": "Old private-chat attribution", "previous_key": None,
        "refs": [first["material"]["pack"]["messages"][0]["ref"]],
    }]}, first["material"]["pack"])
    pg.execute(
        """UPDATE creator_importance_jobs SET status='queued',checkpoint_ciphertext=%s,
        cursor_time=NULL,cursor_chat_id=NULL,cursor_message_id=NULL,needs_history=TRUE,not_before=NOW()
        WHERE id=%s""",
        (importance._cipher(flow.settings).encrypt(json.dumps(checkpoint)), first["id"]),
    )
    assert importance.prepare_next(flow.settings)
    assert len(flow.calls) == calls
    restored = importance._state(
        pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (first["id"],)).fetchone(), flow.settings
    )
    assert not restored["facts"]  # Old attribution cannot survive just because the original body is unchanged.
    assert pg.execute(
        "SELECT status FROM creator_importance_jobs WHERE id=%s", (first["id"],)
    ).fetchone()["status"] == "queued"  # A cache miss would prepare a paid 'ready' batch instead.
    assert importance.metrics(pg)["usage"]["api_requests"] == calls

    def edit_source():
        raw = json.dumps({"text": "Edited acknowledgement, not a new cooperation"})
        pg.execute(
            "UPDATE chat_messages SET body_ciphertext=%s,body_fingerprint=%s WHERE message_id='a3'",
            (flow.cipher.encrypt(raw), message_body_fingerprint(flow.settings.token_encryption_key, raw)),
        )

    for change in (
        edit_source,
        lambda: pg.execute("UPDATE chat_messages SET is_deleted=TRUE,body_ciphertext=NULL WHERE message_id='a2'"),
        lambda: pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (flow.owner,)),
    ):
        second = prepared()
        flow.before_response = change
        importance.run(second, flow.settings)
        billed = pg.execute(
            "SELECT * FROM creator_importance_requests WHERE id=%s", (second["reservation_id"],)
        ).fetchone()
        assert billed["input_tokens"] == 101 and billed["output_tokens"] == 23 and billed["finished_at"]
        assert not pg.execute(
            "SELECT auto_valid FROM creator_importance_profiles WHERE responsible_authorization_id=%s", (flow.owner,)
        ).fetchone()["auto_valid"]
    # Recovering a lost paid lease cannot blindly resubmit a possibly billed request.
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (flow.owner,))
    flow.message(
        "unknown_reservation",
        "A newly received statement, no cooperation",
        creator=True,
        at=flow.now - timedelta(minutes=1),
    )
    last = prepared()
    pg.execute("UPDATE creator_importance_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (last["id"],))
    importance._recover(pg)
    assert importance.claim() is None
    expired = pg.execute("SELECT status,reason_code FROM creator_importance_jobs WHERE id=%s", (last["id"],)).fetchone()
    assert expired == {"status": "failed", "reason_code": "request_unknown"}
    assert (
        pg.execute(
            "SELECT error_code FROM creator_importance_requests WHERE id=%s", (last["reservation_id"],)
        ).fetchone()["error_code"]
        == "request_unknown"
    )
    assert importance.metrics(pg)["usage"]["api_requests"] == len(flow.calls) + 1  # Includes the unknown reservation.
    calls = len(flow.calls)
    flow.message("after_unknown", "Another new message", creator=True, at=flow.now)
    flow.due()
    assert not importance.prepare_next(flow.settings) and importance.claim() is None
    pending = pg.execute(
        "SELECT status,reason_code,pending_messages FROM creator_importance_jobs WHERE id=%s", (last["id"],)
    ).fetchone()
    assert pending["status"] == "failed" and pending["reason_code"] == "request_unknown"
    assert "own_private/after_unknown" in pending["pending_messages"] and len(flow.calls) == calls
    # Only an explicit configuration save (or source reset) may reopen terminal failures.
    importance.configuration_changed(pg)
    restarted = pg.execute(
        "SELECT status,attempts,pending_messages FROM creator_importance_jobs WHERE id=%s", (last["id"],)
    ).fetchone()
    assert restarted["status"] == "queued" and restarted["attempts"] == 0
    assert "own_private/after_unknown" in restarted["pending_messages"]
