"""One durable send workflow: no impersonation, repeat sends or blind timeout retries."""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from app import creator_broadcast, worker
from app import creator_broadcast_delivery as delivery
from app.crypto import TokenCipher, message_body_cipher
from app.lark_api import LarkApiError, LarkProfile


def test_confirmed_delivery_is_scoped_idempotent_and_recoverable(pg, monkeypatch):
    settings = SimpleNamespace(oauth_scopes=("offline_access", "im:chat:read"), token_encryption_key=b"d" * 32)
    cipher = TokenCipher(settings.token_encryption_key)
    actor, other = uuid4(), uuid4()
    for who, name in ((actor, "CM One"), (other, "CM Two")):
        pg.execute(
            """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
            refresh_token_ciphertext,access_token_expires_at,refresh_token_expires_at,scopes,
            body_consent_at,broadcast_enabled)
            VALUES(%s,%s,%s,%s,%s,NOW()+INTERVAL '1 hour',NOW()+INTERVAL '1 day',%s,NOW(),TRUE)""",
            (
                who,
                "ou_" + who.hex,
                name,
                cipher.encrypt(who.hex),
                cipher.encrypt("refresh"),
                [*settings.oauth_scopes, *creator_broadcast.SEND_SCOPES],
            ),
        )
    for chat in ("oc_a", "oc_b", "oc_c"):
        pg.execute("INSERT INTO chats(chat_id,name) VALUES(%s,%s)", (chat, "Category " + chat))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (actor, chat))
        pg.execute(
            """INSERT INTO chat_group_classifications(chat_id,purpose,confirmed_by_authorization_id)
            VALUES(%s,'category',%s)""",
            (chat, actor),
        )
    state = {
        "sent": [],
        "mode": {"oc_b": "unknown", "oc_c": "refused"},
        "permission_change": None,
        "during_send": None,
        "identity_checks": 0,
        "identity_error": None,
    }

    class Client:
        def __init__(self, received, *, http_client):
            assert received is settings and http_client.timeout.read == 8

        def user_info(self, token):
            state["identity_checks"] += 1
            if state["identity_error"]:
                raise state["identity_error"]
            return LarkProfile("ou_" + token, None, None, None)

        def check_chat_send_access(self, token, chat_id, *, sender_open_id, mention_all, mention_open_ids):
            assert sender_open_id == "ou_" + actor.hex and token == actor.hex
            if state["permission_change"]:
                state["permission_change"]()
                state["permission_change"] = None
            return {"chat_name": "Live " + chat_id, "member_count": 20}

        def send_chat_post(self, token, chat_id, body, *, mentions, mention_all, message_uuid):
            assert token == actor.hex and body.startswith("Announcement")
            assert mention_all and not mentions
            state["sent"].append((chat_id, message_uuid))
            if state["during_send"]:
                state["during_send"]()
                state["during_send"] = None
            mode = state["mode"].get(chat_id)
            if mode == "unknown":
                raise LarkApiError(503, None, "redacted timeout", delivery_unknown=True)
            if mode == "refused":
                raise LarkApiError(403, 230002, "redacted refusal")
            if mode == "limited":
                raise LarkApiError(429, None, "limited", retry_after=5)
            if mode == "too_large":
                raise ValueError("payload too large")
            return "om_" + chat_id

    def preview(who, cm, purpose, selections, message, mention):
        assert who == actor and cm == str(actor)
        groups = [
            {
                "id": item["chat_id"],
                "name": "Category " + item["chat_id"],
                "purpose": purpose,
                "people": [],
                "mentions": [],
                "mention_all": mention,
            }
            for item in selections
        ]
        return {
            "groups": groups,
            "issues": [],
            "message": message,
            "purpose": purpose,
            "selected_cm_id": str(actor),
            "mode": "groups",
        }

    monkeypatch.setattr(delivery, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    monkeypatch.setattr(delivery, "LarkClient", Client)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_broadcast, "preview", preview)
    fields = {
        "cm_id": str(actor),
        "purpose": "category",
        "message": "Announcement <at>literal</at>\nURL",
        "mention": True,
        "selections": [{"chat_id": chat} for chat in ("oc_a", "oc_b", "oc_c")],
    }

    # A temporary preview window grants only baseline-scoped reads, never sending.
    pg.execute(
        "UPDATE authorizations SET broadcast_enabled=FALSE,scopes=%s WHERE id=%s", (list(settings.oauth_scopes), other)
    )
    settings.broadcast_preview_until = datetime.now(UTC) + timedelta(minutes=1)
    assert creator_broadcast.preview_open()
    assert creator_broadcast._require_broadcast_grant(pg, other) is False
    assert delivery.history(other) == {"batches": []}
    with pytest.raises(PermissionError):
        delivery.prepare(other, {**fields, "cm_id": str(other)})
    pg.execute("UPDATE authorizations SET broadcast_enabled=TRUE WHERE id=%s", (other,))
    with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired):
        delivery.prepare(other, {**fields, "cm_id": str(other)})
    pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (other,))
    settings.broadcast_preview_until = datetime.now(UTC) - timedelta(seconds=1)
    assert not creator_broadcast.preview_open()
    with pytest.raises(PermissionError):
        creator_broadcast._require_broadcast_grant(pg, other)
    with pytest.raises(PermissionError):
        delivery.history(other)
    settings.broadcast_preview_until = None
    pg.execute(
        "UPDATE authorizations SET broadcast_enabled=TRUE,scopes=%s WHERE id=%s",
        ([*settings.oauth_scopes, *creator_broadcast.SEND_SCOPES], other),
    )

    # A token refresh between obtaining the token and re-reading the grant is not accepted as the new generation.
    usable_token = worker._usable_access_token

    def replace_token(*args):
        token = usable_token(*args)
        pg.execute(
            "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s", (cipher.encrypt("replacement"), actor)
        )
        return token

    before_identity_checks = state["identity_checks"]
    with monkeypatch.context() as patch:
        patch.setattr(worker, "_usable_access_token", replace_token)
        with pytest.raises(PermissionError, match="授权已更新"):
            delivery.prepare(actor, fields)
    assert state["identity_checks"] == before_identity_checks
    pg.execute("UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s", (cipher.encrypt(actor.hex), actor))

    # Connection failures are recoverable errors, not raw provider errors or unhandled 500s.
    for error in (LarkApiError(503, None, "private provider diagnostic"), httpx.ReadTimeout("private diagnostic")):
        state["identity_error"] = error
        with pytest.raises(ValueError, match="暂时无法核验本人连接"):
            delivery.prepare(actor, fields)
    state["identity_error"] = None
    pg.execute(
        "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s",
        (TokenCipher(b"x" * 32).encrypt(actor.hex), actor),
    )
    with pytest.raises(ValueError, match="暂时无法核验本人连接"):
        delivery.prepare(actor, fields)
    pg.execute("UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s", (cipher.encrypt(actor.hex), actor))
    with pytest.raises(PermissionError):
        delivery.prepare(actor, {**fields, "cm_id": str(other)})
    too_large = delivery.prepare(actor, {**fields, "message": "A" + "\n" * 3999})
    assert not too_large["can_send"] and too_large["preview_id"] is None and len(too_large["issues"]) == 3
    ready = delivery.prepare(actor, fields)
    assert ready["can_send"] and not ready["preview_only"] and ready["account_name"] == "CM One"
    assert ready["expires_at"] and len(ready["groups"]) == 3
    saved = pg.execute("SELECT * FROM creator_broadcast_previews WHERE id=%s", (ready["preview_id"],)).fetchone()
    assert fields["message"] not in saved["body_ciphertext"] and "message" not in saved["snapshot"]
    assert message_body_cipher(settings.token_encryption_key).decrypt(saved["body_ciphertext"]) == fields["message"]
    expired = delivery.prepare(actor, {**fields, "message": "Announcement expired"})
    pg.execute(
        "UPDATE creator_broadcast_previews SET expires_at=NOW()-INTERVAL '1 minute' WHERE id=%s",
        (expired["preview_id"],),
    )
    with pytest.raises(ValueError, match="预览已过期"):
        delivery.submit(actor, expired["preview_id"], uuid4())
    request = uuid4()
    first = delivery.submit(actor, ready["preview_id"], request)
    batch_id = first["batch"]["id"]
    assert first["batch"]["pending"] == 3 and not state["sent"]
    assert delivery.submit(actor, ready["preview_id"], request)["batch"]["id"] == batch_id
    again = delivery.prepare(actor, fields)
    assert delivery.submit(actor, again["preview_id"], uuid4())["batch"]["id"] == batch_id
    with pytest.raises(ValueError):
        delivery.submit(actor, again["preview_id"], request)
    assert pg.execute("SELECT COUNT(*) AS n FROM creator_broadcast_targets").fetchone()["n"] == 3
    with pytest.raises(LookupError):
        delivery.batch(other, batch_id)
    for _ in range(3):
        job = delivery.claim_next()
        assert job and delivery.claim_next() is None  # The same sender cannot run in parallel.
        delivery.run_claimed(job, settings)
    result = delivery.batch(actor, batch_id)
    assert result["batch"]["succeeded"] == result["batch"]["failed"] == result["batch"]["unknown"] == 1
    assert result["batch"]["status"] == "unknown" and result["batch"]["can_retry"]
    assert result["message"] == fields["message"]
    assert len(state["sent"]) == 3
    retry_uuid = next(item[1] for item in state["sent"] if item[0] == "oc_c")
    state["mode"].pop("oc_c")
    delivery.retry(actor, batch_id)
    assert delivery.batch(actor, batch_id)["batch"]["pending"] == 1
    job = delivery.claim_next()
    assert job["chat_id"] == "oc_c" and str(job["message_uuid"]) == retry_uuid
    delivery.run_claimed(job, settings)
    assert delivery.claim_next() is None
    with pytest.raises(ValueError, match="结果未知"):
        delivery.retry(actor, batch_id)
    assert delivery.history(actor)["batches"][0]["succeeded"] == 2

    # Stop includes claimed work only while its POST has not started.
    fresh = delivery.prepare(actor, {**fields, "message": "Announcement stop", "selections": fields["selections"][:2]})
    stopped = delivery.submit(actor, fresh["preview_id"], uuid4())["batch"]["id"]
    claimed = delivery.claim_next()
    before = len(state["sent"])
    assert delivery.cancel(actor, stopped)["batch"]["cancelled"] == 2
    delivery.run_claimed(claimed, settings)
    assert len(state["sent"]) == before and delivery.batch(actor, stopped)["batch"]["status"] == "cancelled"

    # Unstarted crashes can safely requeue; started crashes cannot prove non-delivery.
    fresh = delivery.prepare(
        actor, {**fields, "message": "Announcement recover", "selections": fields["selections"][:1]}
    )
    recover_id = delivery.submit(actor, fresh["preview_id"], uuid4())["batch"]["id"]
    claimed = delivery.claim_next()
    pg.execute(
        "UPDATE creator_broadcast_targets SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (claimed["id"],)
    )
    assert delivery.recover() == 1 and delivery.batch(actor, recover_id)["batch"]["pending"] == 1
    claimed_again = delivery.claim_next()
    assert claimed_again["message_uuid"] == claimed["message_uuid"]
    pg.execute(
        """UPDATE creator_broadcast_targets SET send_started_at=NOW(),lease_until=NOW()-INTERVAL '1 second'
        WHERE id=%s""",
        (claimed["id"],),
    )
    assert delivery.recover() == 1 and delivery.batch(actor, recover_id)["batch"]["unknown"] == 1
    with pytest.raises(ValueError):
        delivery.retry(actor, recover_id)

    # Revocation between confirmation and execution blocks the actual send.
    fresh = delivery.prepare(
        actor, {**fields, "message": "Announcement revoke", "selections": fields["selections"][:1]}
    )
    revoke_id = delivery.submit(actor, fresh["preview_id"], uuid4())["batch"]["id"]
    claimed = delivery.claim_next()
    pg.execute("UPDATE authorizations SET scopes='{}' WHERE id=%s", (actor,))
    delivery.run_claimed(claimed, settings)
    assert len(state["sent"]) == before and delivery.batch(actor, revoke_id)["batch"]["failed"] == 1
    with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired):
        delivery.retry(actor, revoke_id)

    # A grant change during the final live check must not reach the POST.
    pg.execute(
        "UPDATE authorizations SET scopes=%s WHERE id=%s",
        ([*settings.oauth_scopes, *creator_broadcast.SEND_SCOPES], actor),
    )
    fresh = delivery.prepare(
        actor, {**fields, "message": "Announcement grant race", "selections": fields["selections"][:1]}
    )
    race_id = delivery.submit(actor, fresh["preview_id"], uuid4())["batch"]["id"]
    state["permission_change"] = lambda: pg.execute(
        "UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor,)
    )
    delivery.run_claimed(delivery.claim_next(), settings)
    assert len(state["sent"]) == before
    pg.execute("UPDATE authorizations SET broadcast_enabled=TRUE WHERE id=%s", (actor,))
    assert delivery.batch(actor, race_id)["batch"]["failed"] == 1

    # A stop while Lark is rejecting a POST must not leave delayed pending work forever.
    fresh = delivery.prepare(
        actor, {**fields, "message": "Announcement cancel rate race", "selections": fields["selections"][:1]}
    )
    rate_id = delivery.submit(actor, fresh["preview_id"], uuid4())["batch"]["id"]
    state["mode"]["oc_a"] = "limited"
    state["during_send"] = lambda: delivery.cancel(actor, rate_id)
    delivery.run_claimed(delivery.claim_next(), settings)
    assert delivery.batch(actor, rate_id)["batch"]["cancelled"] == 1
    assert delivery.claim_next() is None
    assert not any(
        fields["message"] in str(row["metadata"]) for row in pg.execute("SELECT metadata FROM audit_events").fetchall()
    )


def test_literal_post_and_safe_send_errors_do_not_require_database(monkeypatch):
    # Simulate the worker boundary only: a refused send may retry; timeout may not.
    statuses = []
    authorization = {"open_id": "ou_sender"}
    settings = SimpleNamespace(token_encryption_key=b"e" * 32)
    job = {
        "id": uuid4(),
        "sender_authorization_id": uuid4(),
        "sender_open_id": "ou_sender",
        "snapshot": {"id": "oc_group", "mentions": [], "mention_all": False},
        "purpose": "category",
        "chat_id": "oc_group",
        "message_uuid": uuid4(),
        "attempts": 1,
        "body_ciphertext": message_body_cipher(settings.token_encryption_key).encrypt("literal <at>text</at>"),
    }
    failure = {"error": None}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_chat_post(self, *_args, **_kwargs):
            raise failure["error"]

    monkeypatch.setattr(delivery, "connection", lambda: nullcontext(None))
    monkeypatch.setattr(delivery, "_authorization", lambda *_args: authorization)
    monkeypatch.setattr(delivery, "_route_current", lambda *_args: None)
    monkeypatch.setattr(delivery, "_client_token", lambda *_args: (authorization, "token"))
    monkeypatch.setattr(delivery, "_live_group", lambda *_args: {})
    monkeypatch.setattr(delivery, "_mark_started", lambda *_args: True)
    monkeypatch.setattr(delivery, "LarkClient", Client)
    monkeypatch.setattr(delivery, "_finish", lambda _job, status, *_args, **kwargs: statuses.append((status, kwargs)))
    for error, expected, retryable in (
        (LarkApiError(503, None, "connection failed", delivery_unknown=False), "failed", True),
        (LarkApiError(503, None, "timeout", delivery_unknown=True), "unknown", False),
        (httpx.ReadTimeout("timeout"), "unknown", False),
        (LarkApiError(429, None, "limited", retry_after=5), "pending", False),
        (ValueError("payload too large"), "failed", False),
    ):
        failure["error"] = error
        delivery.run_claimed(job, settings)
        assert statuses[-1][0] == expected
        assert bool(statuses[-1][1].get("retryable")) == retryable


def test_parallel_claims_allow_only_one_inflight_target_per_sender(pg, monkeypatch):
    schema = pg.execute("SELECT current_schema() AS name").fetchone()["name"]
    assert re.fullmatch(r"test_people_[0-9a-f]{32}", schema)
    actor, preview_id, batch_id = uuid4(), uuid4(), uuid4()
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ["CREATOR_WORKFLOW_AI_TEST_DATABASE_URL"]

    @contextmanager
    def connect():
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
            yield conn

    try:
        pg.execute(
            """INSERT INTO authorizations(id,open_id,access_token_ciphertext)
            VALUES(%s,%s,'test-only')""",
            (actor, "ou_" + actor.hex),
        )
        pg.execute(
            """INSERT INTO creator_broadcast_previews(id,sender_authorization_id,sender_open_id,
            sender_name,purpose,body_ciphertext,snapshot,fingerprint)
            VALUES(%s,%s,%s,'Test','custom','test-only','{}','test-only')""",
            (preview_id, actor, "ou_" + actor.hex),
        )
        pg.execute(
            """INSERT INTO creator_broadcast_batches(id,sender_authorization_id,preview_id,request_id)
            VALUES(%s,%s,%s,%s)""",
            (batch_id, actor, preview_id, uuid4()),
        )
        for n in range(8):
            pg.execute(
                """INSERT INTO creator_broadcast_targets(batch_id,chat_id,chat_name,snapshot)
                VALUES(%s,%s,'Test','{}')""",
                (batch_id, "oc_" + str(n)),
            )
        pg.commit()  # Independent worker connections must see the isolated fixture.
        monkeypatch.setattr(delivery, "connection", connect)
        for _ in range(5):
            with connect() as conn:
                conn.execute("UPDATE creator_broadcast_targets SET status='pending',lease_token=NULL,lease_until=NULL")
            barrier = Barrier(8)

            def claim(_, barrier=barrier):
                barrier.wait()
                return delivery.claim_next()

            with ThreadPoolExecutor(max_workers=8) as executor:
                claimed = list(executor.map(claim, range(8)))
            assert sum(item is not None for item in claimed) == 1
            with connect() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM creator_broadcast_targets WHERE status='sending'"
                    ).fetchone()["n"]
                    == 1
                )
    finally:
        pg.rollback()
        pg.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        pg.commit()
