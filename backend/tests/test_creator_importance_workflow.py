"""Chat-only ranking on isolated PostgreSQL; every provider response is synthetic."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import ai_feature_config, assistant_rules, creator_access, openai_client
from app import creator_authorization_rescans as rescans
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
        instructions=[],
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
        assert kwargs["max_output_tokens"] == 8192
        assert kwargs["reasoning_effort"] == "none" and kwargs["total_timeout"] == 70
        state.calls.append(deepcopy(pack))
        state.instructions.append(kwargs["instructions"])
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

    def drain_rescans():
        for _ in range(40):
            if not rescans.prepare_next(settings):
                return
        pytest.fail("authorization rescan queue did not drain")

    def drain():
        drain_rescans()
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
    monkeypatch.setattr(rescans, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(importance, "get_settings", lambda: settings)
    monkeypatch.setattr(importance.api, "call_openai", respond)
    monkeypatch.setattr(importance, "MAX_MESSAGES", 1)
    state.message, state.enable, state.due, state.drain = message, enable, due, drain
    state.drain_rescans = drain_rescans
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


@pytest.mark.parametrize("missing", ["backfill_complete", "recent_start"])
def test_history_coverage_requires_successful_full_sync(pg, ranking_flow, missing):
    flow = ranking_flow
    flow.enable()
    if missing == "recent_start":
        pg.execute("UPDATE authorization_chats SET backfill_window_start_at=NOW()-INTERVAL '7 days'")
    else:
        pg.execute("UPDATE authorization_chats SET backfill_complete=FALSE")
    job = pg.execute("""SELECT *,NOW()-INTERVAL '90 days' history_start,NOW() history_end
        FROM creator_importance_jobs WHERE responsible_authorization_id=%s""", (flow.owner,)).fetchone()
    material = importance.build_batch(pg, job, flow.settings, importance._state(job, flow.settings))
    assert material["state"]["chat_coverage"] == {"own_private": False}
    assert not flow.calls


def test_history_batches_yield_to_other_waiting_jobs(pg, ranking_flow):
    flow = ranking_flow
    flow.enable()
    pg.execute("""UPDATE creator_importance_jobs SET created_at=NOW()-CASE
        WHEN responsible_authorization_id=%s THEN INTERVAL '7 days' ELSE INTERVAL '1 day' END""", (flow.owner,))
    flow.due()
    pg.execute("SAVEPOINT first_assessment")
    pg.execute("INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id) VALUES(%s,%s)",
               (flow.person, flow.owner))
    pg.execute("UPDATE creator_importance_jobs SET not_before=NOW()-INTERVAL '2 seconds' "
               "WHERE responsible_authorization_id=%s", (flow.owner,))
    assert importance.prepare_next(flow.settings)
    assert pg.execute("SELECT status FROM creator_importance_jobs WHERE responsible_authorization_id=%s",
                      (flow.owner,)).fetchone()["status"] == "queued"
    assert importance.prepare_next(flow.settings)
    initial = importance.claim()
    assert initial and initial["responsible_authorization_id"] == flow.other
    pg.execute("ROLLBACK TO SAVEPOINT first_assessment")
    pg.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id)
        SELECT contact_id,responsible_authorization_id FROM creator_importance_jobs""")
    assert importance.prepare_next(flow.settings)
    first = importance.claim()
    assert first and first["responsible_authorization_id"] == flow.owner
    importance.run(first, flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_importance_jobs WHERE id=%s",
                      (first["id"],)).fetchone() == {"status": "queued", "reason_code": "next_batch"}

    assert importance.prepare_next(flow.settings)
    assert pg.execute("SELECT status FROM creator_importance_jobs WHERE id=%s",
                      (first["id"],)).fetchone()["status"] == "queued"
    assert importance.prepare_next(flow.settings)
    # Both are ready, but the older job's next batch must not jump the CM already waiting.
    following = importance.claim()
    assert following and following["responsible_authorization_id"] == flow.other
    importance.run(following, flow.settings)
    flow.drain()
    assert pg.execute("SELECT count(*) n FROM creator_importance_jobs WHERE status<>'completed'").fetchone()["n"] == 0


def test_history_incremental_ranking_permissions_and_source_fences(pg, ranking_flow, monkeypatch):
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
    # An invalid old candidate must not requeue a newer prepared snapshot.
    pg.execute("SAVEPOINT stale_importance_candidate")
    ready = pg.execute("SELECT * FROM creator_importance_jobs WHERE status='ready'").fetchone()
    source_checks = importance._source_checks
    newer = {}

    def replace_ready_candidate(*args, **kwargs):
        checks = source_checks(*args, **kwargs)
        assert checks
        newer.update(pg.execute("""UPDATE creator_importance_jobs SET desired_version=desired_version+1,
            snapshot_version=snapshot_version+1,pack_ciphertext=%s,fingerprint=%s WHERE id=%s RETURNING *""",
            (importance._cipher(flow.settings).encrypt(importance._cipher(flow.settings).decrypt(
                ready["pack_ciphertext"])), importance._hash("newer prepared batch"), ready["id"])).fetchone())
        return {ref: False for ref in checks}

    with monkeypatch.context() as stale:
        stale.setattr(importance, "_source_checks", replace_ready_candidate)
        assert importance.claim() is None
    assert pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (ready["id"],)).fetchone() == newer
    pg.execute("ROLLBACK TO SAVEPOINT stale_importance_candidate")
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
    pg.execute("SAVEPOINT rebuilding_history")
    calls = len(flow.calls)
    pg.execute("""UPDATE creator_importance_jobs SET status='queued',needs_history=TRUE,
        cursor_time=NULL,cursor_chat_id=NULL,cursor_message_id=NULL,checkpoint_ciphertext=NULL,
        pending_messages='{}',not_before=NOW()-INTERVAL '1 second'
        WHERE responsible_authorization_id=%s""", (flow.owner,))
    assert importance.prepare_next(flow.settings)
    if rebuilding := importance.claim():
        importance.run(rebuilding, flow.settings)
    progressed = pg.execute("SELECT * FROM creator_importance_jobs WHERE responsible_authorization_id=%s",
                            (flow.owner,)).fetchone()
    assert progressed["needs_history"] and progressed["cursor_time"] and progressed["checkpoint_ciphertext"]
    assert pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                      (flow.owner,)).fetchone() == profiles[flow.owner]
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["tier"] == "priority"
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE,body_ciphertext=NULL WHERE message_id='a2'")
    invalidated = pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                             (flow.owner,)).fetchone()
    assert not invalidated["auto_valid"] and invalidated["coverage"]["history_complete"]
    importance._save_profile(pg, progressed, importance._state(progressed, flow.settings), flow.settings,
                             history_complete=False)
    assert pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                      (flow.owner,)).fetchone() == invalidated
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (flow.owner,))
    inaccessible = importance.view_for(pg, flow.person, flow.owner, flow.settings)
    assert inaccessible["tier"] == "pending" and not inaccessible["grounds"]
    pg.execute("ROLLBACK TO SAVEPOINT rebuilding_history")

    # A rebuilt fact exists only in the checkpoint while the previous full profile stays published.
    pg.execute("SAVEPOINT checkpoint_source_retraction")
    with monkeypatch.context() as limited:
        limited.setattr(importance, "MAX_MESSAGES", 2)
        flow.message("checkpoint_c1", "GAMMA new agreement: please film the autumn video",
                     at=flow.now-timedelta(days=8))
        flow.message("checkpoint_c2", "GAMMA I agree to film the autumn video", creator=True,
                     at=flow.now-timedelta(days=8)+timedelta(minutes=1), parent="checkpoint_c1")
        for n in range(4):
            flow.message(f"checkpoint_gap{n}", "Thanks for the update", creator=True,
                         at=flow.now-timedelta(days=7)+timedelta(hours=n))
        flow.message("checkpoint_d", "No further business today", creator=True, at=flow.now-timedelta(hours=1))
        pg.execute("SELECT enqueue_creator_importance(%s,%s,TRUE)", (flow.person, flow.owner))
        pending = None
        for _ in range(24):
            flow.due()
            progressed = importance.prepare_next(flow.settings)
            candidate = importance.claim()
            if candidate:
                if any(m["message_id"] == "checkpoint_d" for m in candidate["material"]["pack"]["messages"]):
                    pending = candidate
                    break
                importance.run(candidate, flow.settings)
            else:
                assert progressed
        assert pending is not None and pending["material"]["done"]
        checkpoint = importance._state(pending, flow.settings)
        c_refs = {key for key, source in checkpoint["sources"].items() if source["message_id"] == "checkpoint_c2"}
        assert c_refs and any(c_refs.intersection(fact["refs"]) for fact in checkpoint["facts"])
        public = pg.execute("SELECT sources FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                            (flow.owner,)).fetchone()
        assert not any(ref["message_id"] == "checkpoint_c2" for ref in [*public["sources"], *pending["refs"]])

        def retract_checkpoint_source():
            before = pg.execute("SELECT desired_version,claim_token,status FROM creator_importance_jobs WHERE id=%s",
                                (pending["id"],)).fetchone()
            pg.execute("UPDATE chat_messages SET is_deleted=TRUE,body_ciphertext=NULL "
                       "WHERE message_id='checkpoint_c2'")
            assert pg.execute("SELECT desired_version,claim_token,status FROM creator_importance_jobs WHERE id=%s",
                              (pending["id"],)).fetchone() == before

        locked = []
        source_checks = importance._source_checks

        def check_sources(*args, **kwargs):
            if kwargs.get("lock"):
                locked.extend(source["message_id"] for source in kwargs.get("additional_refs", ()))
            return source_checks(*args, **kwargs)

        limited.setattr(importance, "_source_checks", check_sources)
        flow.before_response = retract_checkpoint_source
        paid_calls = len(flow.calls)
        importance.run(pending, flow.settings)
        assert len(flow.calls) == paid_calls+1 and "checkpoint_c2" in locked
        finished = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (pending["id"],)).fetchone()
        assert finished["status"] == "completed" and finished["reason_code"] == "evaluated"
        profile = pg.execute("SELECT p.*,(" + importance._PROFILE_VALID + ") AS current_valid "
                             "FROM creator_importance_profiles p WHERE responsible_authorization_id=%s",
                             (flow.owner,)).fetchone()
        assert profile["auto_valid"] and profile["coverage"]["history_complete"] and profile["current_valid"]
        assert not any(ref["message_id"] == "checkpoint_c2" for ref in profile["sources"])
        remaining = importance._state(finished, flow.settings)["facts"]
        assert not any(c_refs.intersection(fact["refs"]) for fact in remaining)
        assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["tier"] != "pending"
    pg.execute("ROLLBACK TO SAVEPOINT checkpoint_source_retraction")
    del flow.calls[calls:]
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
    pg.execute("UPDATE authorization_chats SET sync_status='not_readable' WHERE authorization_id=%s", (flow.owner,))
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["grounds"] == []
    assert not any(importance._source_checks(pg, material["sources"], flow.person, flow.owner).values())
    pg.execute("UPDATE authorization_chats SET sync_status='succeeded' WHERE authorization_id=%s", (flow.owner,))
    pg.execute("UPDATE authorizations SET scopes=ARRAY['im:message.send_as_user'] WHERE id=%s", (flow.owner,))
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["grounds"] == []
    assert not any(importance._source_checks(pg, material["sources"], flow.person, flow.owner).values())
    pg.execute("UPDATE authorizations SET scopes=ARRAY['im:chat:read','im:message:readonly'] WHERE id=%s",
               (flow.owner,))
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


def test_paid_retry_cache_and_stale_results_keep_billing_without_repeat(pg, ranking_flow, monkeypatch):
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
        "synthetic 503", retryable=True, telemetry={"http_status": 503, "usage": {"input_tokens": 2},
            "provider_error": {"code": "server_is_overloaded", "type": "server_error"}}
    )
    importance.run(first, flow.settings)
    assert (
        pg.execute("SELECT status FROM creator_importance_jobs WHERE id=%s", (first["id"],)).fetchone()["status"]
        == "retry_wait"
    )
    stats = pg.execute("SELECT stats FROM creator_importance_jobs WHERE id=%s", (first["id"],)).fetchone()["stats"]
    assert stats["provider_error"] == {"code": "server_is_overloaded", "type": "server_error"}
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
        assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["grounds"] == []
        flow.drain_rescans()
        assert not pg.execute(
            "SELECT auto_valid FROM creator_importance_profiles WHERE responsible_authorization_id=%s", (flow.owner,)
        ).fetchone()["auto_valid"]
    # Recovering a lost paid lease cannot blindly resubmit a possibly billed request.
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (flow.owner,))
    flow.drain_rescans()
    flow.message(
        "unknown_reservation",
        "A newly received statement, no cooperation",
        creator=True,
        at=flow.now - timedelta(minutes=1),
    )
    last = prepared()
    # A request created before migration 092 has no frozen material hash.
    pg.execute("UPDATE creator_importance_requests SET material_fingerprint=NULL WHERE id=%s",
               (last["reservation_id"],))
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
    # Saving configuration must not silently repeat a possibly charged frozen batch.
    importance.configuration_changed(pg)
    restarted = pg.execute(
        "SELECT status,attempts,pending_messages FROM creator_importance_jobs WHERE id=%s", (last["id"],)
    ).fetchone()
    assert restarted["status"] == "failed" and restarted["attempts"] == 1
    assert "own_private/after_unknown" in restarted["pending_messages"]
    # The config save may enroll the separate CM; this assertion concerns the frozen owner's pair only.
    pg.execute("UPDATE creator_importance_jobs SET status='cancelled' WHERE responsible_authorization_id=%s",
               (flow.other,))
    assert not importance.prepare_next(flow.settings) and importance.claim() is None
    # A real topology reset loses the frozen batch; it must not repay earlier history to rediscover the unknown.
    pg.execute("UPDATE creator_importance_requests SET material_fingerprint=%s,created_at=NOW()+INTERVAL '1 second' "
               "WHERE id=%s", (importance._hash(last["material"]["pack"]), last["reservation_id"]))
    receipts = pg.execute("SELECT * FROM creator_importance_requests ORDER BY id").fetchall()
    pg.execute("SELECT enqueue_creator_importance(%s,%s,TRUE)", (flow.person, flow.owner))
    reset = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (last["id"],)).fetchone()
    assert reset["status"] == "queued" and reset["reason_code"] == "source_changed"
    assert reset["needs_history"] and not reset["pack_ciphertext"] and reset["cursor_time"] is None
    with monkeypatch.context() as patch:
        patch.setattr(importance, "build_batch", lambda *_args: pytest.fail("unknown reset restarted history"))
        assert importance.prepare_next(flow.settings)
    assert importance.claim() is None and len(flow.calls) == calls
    assert not importance.prepare_next(flow.settings)
    assert pg.execute("SELECT * FROM creator_importance_requests ORDER BY id").fetchall() == receipts
    assert pg.execute("SELECT status,reason_code FROM creator_importance_jobs WHERE id=%s",
                      (last["id"],)).fetchone() == {"status": "failed", "reason_code": "request_unknown"}
    # An HTTP 200 with incomplete model output is still billed, but must not publish facts or retry.
    pg.execute("UPDATE creator_importance_jobs SET status='queued',not_before=NOW() "
               "WHERE responsible_authorization_id=%s", (flow.other,))
    incomplete = prepared()
    flow.error = openai_client.ModelOutputError("unfinished response", telemetry={
        "http_status": 200, "openai_request_id": "req-incomplete", "response_id": "resp-incomplete",
        "usage": {"input_tokens": 31, "output_tokens": 17}, "response_status": "incomplete",
        "incomplete_reason": "max_output_tokens",
    })
    importance.run(incomplete, flow.settings)
    receipt = pg.execute("SELECT request_id,response_id,error_code,input_tokens,output_tokens,finished_at "
                         "FROM creator_importance_requests WHERE id=%s", (incomplete["reservation_id"],)).fetchone()
    assert receipt["finished_at"] is not None
    assert {key: value for key, value in receipt.items() if key != "finished_at"} == {
        "request_id": "req-incomplete", "response_id": "resp-incomplete", "error_code": "invalid_output",
        "input_tokens": 31, "output_tokens": 17,
    }
    assert pg.execute("SELECT status,reason_code FROM creator_importance_jobs WHERE id=%s",
                      (incomplete["id"],)).fetchone() == {"status": "failed", "reason_code": "invalid_output"}
    stats = pg.execute("SELECT stats FROM creator_importance_jobs WHERE id=%s", (incomplete["id"],)).fetchone()["stats"]
    assert stats["response_status"] == "incomplete" and stats["incomplete_reason"] == "max_output_tokens"
    assert stats["usage"] == {"input_tokens": 31, "output_tokens": 17}
    assert importance.claim() is None and len(flow.calls) == calls + 1
    view = importance.view_for(pg, incomplete["contact_id"], incomplete["responsible_authorization_id"],
                               flow.settings, details=False)
    assert view["gap_label"] == "上次评估未完成，等待处理"
    pg.execute("DELETE FROM creator_importance_profiles WHERE contact_id=%s AND responsible_authorization_id=%s",
               (incomplete["contact_id"], incomplete["responsible_authorization_id"]))
    assert importance.view_for(pg, incomplete["contact_id"], incomplete["responsible_authorization_id"],
                               flow.settings, details=False)["gap_label"] == "上次评估未完成，等待处理"
    pg.execute("UPDATE creator_importance_jobs SET status='ready' WHERE id=%s", (incomplete["id"],))
    restarted = importance.claim()
    assert restarted and restarted["id"] == incomplete["id"]
    checkpoint = pg.execute("SELECT checkpoint_ciphertext FROM creator_importance_jobs WHERE id=%s",
                            (incomplete["id"],)).fetchone()
    with monkeypatch.context() as malformed:
        malformed.setattr(importance.api, "call_openai", lambda *_args, **_kwargs: {
            "text": '{"facts":[]}HOOK\n {"facts":[]}', "telemetry": {"http_status": 200},
            "usage": {"input_tokens": 11, "output_tokens": 9}, "response_id": "resp-bad-json"})
        importance.run(restarted, flow.settings)
    failed = pg.execute("SELECT status,stats,checkpoint_ciphertext FROM creator_importance_jobs WHERE id=%s",
                        (restarted["id"],)).fetchone()
    assert failed["status"] == "failed" and failed["stats"]["output_error"] == "json_extra_data"
    assert failed["checkpoint_ciphertext"] == checkpoint["checkpoint_ciphertext"]
    pg.execute("UPDATE creator_importance_jobs SET status='ready' WHERE id=%s", (incomplete["id"],))
    invalid_schema = importance.claim()
    flow.error = openai_client.OpenAIRequestError("HTTP 400", telemetry={
        "http_status": 400, "provider_error": {"code": "invalid_json_schema"}},
        provider_diagnostic={"code": "invalid_json_schema", "message": "PRIVATE PROVIDER DETAIL"})
    importance.run(invalid_schema, flow.settings)
    receipt = pg.execute("SELECT error_code,response_ciphertext FROM creator_importance_requests WHERE id=%s",
                         (invalid_schema["reservation_id"],)).fetchone()
    assert receipt["error_code"] == "http_400"
    assert json.loads(importance._cipher(flow.settings).decrypt(receipt["response_ciphertext"])) == {
        "provider_diagnostic": {"code": "invalid_json_schema", "message": "PRIVATE PROVIDER DETAIL"}}
    stats = pg.execute("SELECT stats FROM creator_importance_jobs WHERE id=%s", (incomplete["id"],)).fetchone()["stats"]
    assert "PRIVATE PROVIDER DETAIL" not in str(stats) and importance.claim() is None

    pg.execute("UPDATE creator_importance_jobs SET status='ready',attempts=0 WHERE id=%s", (incomplete["id"],))
    rejected = importance.claim()
    flow.error = openai_client.OpenAIRequestError("HTTP 400", telemetry={
        "http_status": 400, "provider_error": {"code": "invalid_prompt"}},
        provider_diagnostic={"code": "invalid_prompt", "message": "PRIVATE REJECTION DETAIL"})
    importance.run(rejected, flow.settings)
    assert pg.execute("SELECT status,reason_code FROM creator_importance_jobs WHERE id=%s",
                      (rejected["id"],)).fetchone() == {"status": "failed", "reason_code": "provider_rejected"}
    assert importance.view_for(pg, rejected["contact_id"], rejected["responsible_authorization_id"],
                               flow.settings, details=False)["gap_label"] == "评估请求未通过，需人工处理"
    calls = len(flow.calls)
    importance.configuration_changed(pg)
    assert importance.claim() is None and len(flow.calls) == calls
    assert pg.execute("SELECT reason_code FROM creator_importance_jobs WHERE id=%s",
                      (rejected["id"],)).fetchone()["reason_code"] == "provider_rejected"


def test_document_versions_preserve_incremental_ranking_and_reuse_paused_paid_work(pg, ranking_flow):
    flow = ranking_flow
    flow.enable()
    flow.drain()
    importance.set_manual(pg, flow.owner, flow.person, flow.owner, "normal", "Reviewed by owner", flow.settings)

    def save(key="importance-facts", *, status="published", append=""):
        document = assistant_rules.library(pg, flow.settings)[key]
        assistant_rules.save_document(pg, flow.settings, flow.owner, key, document["title"], document["description"],
            "manual", status, "owner", document["text"] + append, document["revision"])

    def persisted():
        return {table: pg.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in (
            "creator_importance_jobs", "creator_importance_profiles", "creator_importance_requests",
            "contact_assignments")}

    def ready(identifier, minutes):
        flow.message(identifier, "Thank you, no new cooperation", creator=True,
                     at=flow.now - timedelta(minutes=minutes))
        flow.due()
        assert importance.prepare_next(flow.settings)
        return pg.execute("SELECT * FROM creator_importance_jobs WHERE responsible_authorization_id=%s",
                          (flow.owner,)).fetchone()

    legacy = ready("legacy_frozen", 7)
    legacy_material = json.loads(importance._cipher(flow.settings).decrypt(legacy["pack_ciphertext"]))
    del legacy_material["_rules"]
    pg.execute("UPDATE creator_importance_jobs SET pack_ciphertext=%s,fingerprint=%s WHERE id=%s",
        (importance._cipher(flow.settings).encrypt(json.dumps(legacy_material)),
         importance._hash([legacy_material["pack"], legacy["config_fingerprint"]]), legacy["id"]))
    importance.run(importance.claim(), flow.settings)
    assert flow.instructions[-1] == (
        importance.facts.instructions() + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")

    ready("rules_pinned", 6)
    running = importance.claim()
    assert running is not None and not running["needs_history"]
    pinned = running["material"]["_rules"]
    before = persisted()
    save(append="\n测试：新的事实判断规则。")
    facts_digest = assistant_rules.importance_snapshot(pg, flow.settings)["digest"]
    save("importance-tiers", status="archived", append="\n测试：档位规则仍由程序执行。")
    assert persisted() == before  # Editing manuals never restarts history or rewrites a manual choice.
    updated = assistant_rules.importance_snapshot(pg, flow.settings)
    assert updated["digest"] == facts_digest and updated["digest"] != pinned["digest"]
    importance.run(running, flow.settings)
    assert flow.instructions[-1] == (
        assistant_rules.importance_instructions(pinned) + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert "测试：档位规则仍由程序执行。" not in flow.instructions[-1]
    previous = importance._state(running, flow.settings)["facts"]
    profile = importance.view_for(pg, flow.person, flow.owner, flow.settings)
    assert profile["tier"] == "normal" and profile["manual_reason"] == "Reviewed by owner"

    ready("rules_next_batch", 5)
    following = importance.claim()
    assert following is not None and not following["needs_history"]
    assert following["material"]["_rules"]["digest"] == updated["digest"]
    assert following["material"]["state"]["facts"] == previous
    importance.run(following, flow.settings)
    assert flow.instructions[-1] == (
        assistant_rules.importance_instructions(updated) + "\n只输出最终 JSON 对象，不输出前言、中间说明或重复结果。")
    assert "测试：新的事实判断规则。" in flow.instructions[-1]
    assert "测试：档位规则仍由程序执行。" not in flow.instructions[-1]
    checkpoint = importance._state(pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s",
                                            (following["id"],)).fetchone(), flow.settings)
    assert checkpoint["last_rules"] == {"digest": updated["digest"], "protocol": updated["protocol"],
        "revisions": {key: item["revision"] for key, item in updated["documents"].items()}}

    waiting = ready("rules_archived", 4)
    requests = pg.execute("SELECT count(*) n FROM creator_importance_requests").fetchone()["n"]
    calls = len(flow.calls)
    save(status="archived")
    assert importance.claim() is None and len(flow.calls) == calls
    paused = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (waiting["id"],)).fetchone()
    assert paused["status"] == "cancelled" and paused["reason_code"] == "rules_unavailable"
    assert paused["checkpoint_ciphertext"] == waiting["checkpoint_ciphertext"]
    assert paused["cursor_time"] == waiting["cursor_time"] and paused["pack_ciphertext"]
    assert pg.execute("SELECT count(*) n FROM creator_importance_requests").fetchone()["n"] == requests

    save()
    resumed = importance.claim()
    assert resumed is not None and resumed["id"] == waiting["id"]
    assert resumed["material"]["_rules"]["digest"] == updated["digest"]
    pg.execute("SAVEPOINT stale_rule_pause")
    pg.execute("UPDATE creator_importance_jobs SET status='queued',claim_token=NULL,desired_version=desired_version+1 "
               "WHERE id=%s", (resumed["id"],))
    replacement = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (resumed["id"],)).fetchone()
    importance._pause_rules(pg, resumed)
    assert pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (resumed["id"],)).fetchone() == replacement
    pg.execute("ROLLBACK TO SAVEPOINT stale_rule_pause")
    profile_before = pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                               (flow.owner,)).fetchone()
    flow.before_response = lambda: save(status="archived")
    importance.run(resumed, flow.settings)
    paid = pg.execute("SELECT * FROM creator_importance_requests WHERE id=%s",
                      (resumed["reservation_id"],)).fetchone()
    assert paid["input_tokens"] == 101 and paid["output_tokens"] == 23
    assert paid["response_ciphertext"] and paid["finished_at"] and not paid["error_code"]
    assert pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                      (flow.owner,)).fetchone() == profile_before
    paused = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (waiting["id"],)).fetchone()
    assert paused["status"] == "cancelled" and paused["reason_code"] == "rules_unavailable"
    assert paused["checkpoint_ciphertext"] == waiting["checkpoint_ciphertext"]
    assert paused["cursor_time"] == waiting["cursor_time"]
    pg.execute("UPDATE creator_importance_jobs SET attempts=3 WHERE id=%s", (waiting["id"],))
    importance.configuration_changed(pg)
    assert pg.execute("SELECT status,pack_ciphertext FROM creator_importance_jobs WHERE id=%s",
                      (waiting["id"],)).fetchone() == {
                          "status": "cancelled", "pack_ciphertext": paused["pack_ciphertext"]}
    flow.message("rules_arrival_while_paused", "Another new message, no cooperation", creator=True,
                 at=flow.now - timedelta(minutes=3))
    flow.due()
    assert importance.prepare_next(flow.settings) and importance.claim() is None
    assert len(flow.calls) == calls + 1
    calls = len(flow.calls)
    save()
    importance.configuration_changed(pg)  # Saving unchanged settings after republishing must retain paid material.
    assert importance.claim() is None and len(flow.calls) == calls  # Reuse the paid, frozen response locally.
    finished = pg.execute("SELECT * FROM creator_importance_jobs WHERE id=%s", (waiting["id"],)).fetchone()
    assert finished["status"] == "queued" and not finished["needs_history"]
    assert list(finished["pending_messages"]) == ["own_private/rules_arrival_while_paused"]
    assert pg.execute("SELECT count(*) n FROM creator_importance_requests").fetchone()["n"] == requests + 1
    flow.drain()
    assert len(flow.calls) == calls + 1  # Only the genuinely new message needs a provider request.
    assert importance.view_for(pg, flow.person, flow.owner, flow.settings)["tier"] == "normal"


def test_pending_authorization_rescan_blocks_old_ranking_and_paid_work(pg, ranking_flow):
    flow = ranking_flow
    flow.enable()
    flow.drain()
    old_profile = pg.execute("SELECT * FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                             (flow.owner,)).fetchone()
    assert old_profile["auto_valid"]
    flow.message("pending_rescan", "A new unrelated acknowledgement", creator=True, at=flow.now)
    flow.due()
    assert importance.prepare_next(flow.settings)
    pg.execute("SAVEPOINT pending_candidate")
    pg.execute("INSERT INTO creator_authorization_rescans(authorization_id) VALUES(%s)", (flow.owner,))
    assert importance.claim() is None
    pg.execute("UPDATE creator_importance_jobs SET status='queued' WHERE responsible_authorization_id=%s",
               (flow.owner,))
    assert not importance.prepare_next(flow.settings)
    pg.execute("ROLLBACK TO SAVEPOINT pending_candidate")
    job = importance.claim()
    assert job and job["responsible_authorization_id"] == flow.owner
    flow.before_response = lambda: pg.execute(
        "INSERT INTO creator_authorization_rescans(authorization_id) VALUES(%s)", (flow.owner,))
    importance.run(job, flow.settings)
    billed = pg.execute("SELECT input_tokens,output_tokens FROM creator_importance_requests WHERE id=%s",
                        (job["reservation_id"],)).fetchone()
    assert billed == {"input_tokens": 101, "output_tokens": 23}
    assert pg.execute("SELECT revision FROM creator_importance_profiles WHERE responsible_authorization_id=%s",
                      (flow.owner,)).fetchone()["revision"] == old_profile["revision"]
    # Legacy empty sources cannot prove that an old ranking belongs to the current authorization.
    pg.execute("UPDATE creator_importance_profiles SET sources='[]',facts_ciphertext='not-a-ciphertext' "
               "WHERE responsible_authorization_id=%s", (flow.owner,))
    view = importance.view_for(pg, flow.person, flow.owner, flow.settings)
    assert view["tier"] == "pending" and view["grounds"] == []
    join, args = importance.roster_sql(flow.owner)
    assert pg.execute("SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s",
                      [*args, flow.person]).fetchone()["effective_tier"] == "pending"
    importance.set_manual(pg, flow.owner, flow.person, flow.owner, "normal", "Human-reviewed priority", flow.settings)
    view = importance.view_for(pg, flow.person, flow.owner, flow.settings)
    assert view["tier"] == "normal" and not view["grounds"] and view["manual_reason"] == "Human-reviewed priority"
    assert pg.execute("SELECT ip.effective_tier FROM contacts c" + join + " WHERE c.id=%s",
                      [*args, flow.person]).fetchone()["effective_tier"] == "normal"
    flow.enable(False)
    flow.drain_rescans()
    assert not pg.execute("SELECT 1 FROM creator_authorization_rescans WHERE authorization_id=%s",
                          (flow.owner,)).fetchone()
    profile = pg.execute("SELECT auto_valid,manual_tier FROM creator_importance_profiles "
                         "WHERE responsible_authorization_id=%s", (flow.owner,)).fetchone()
    assert profile == {"auto_valid": False, "manual_tier": "normal"}
