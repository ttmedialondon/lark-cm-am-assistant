"""One durable send workflow: no impersonation, repeat sends or blind timeout retries."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from io import BytesIO
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from PIL import Image
from psycopg.rows import dict_row

from app import broadcast_content, broadcast_images, creator_broadcast, worker
from app import creator_broadcast_delivery as delivery
from app.crypto import TokenCipher, message_body_cipher
from app.lark_api import LarkApiError, LarkProfile, build_chat_post_payload


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

    # Feature access and send authorization are separate grants.
    pg.execute(
        "UPDATE authorizations SET broadcast_enabled=FALSE,scopes=%s WHERE id=%s", (list(settings.oauth_scopes), other)
    )
    with pytest.raises(PermissionError):
        creator_broadcast._require_broadcast_grant(pg, other)
    with pytest.raises(PermissionError):
        delivery.history(other)
    pg.execute("UPDATE authorizations SET broadcast_enabled=TRUE WHERE id=%s", (other,))
    assert creator_broadcast._require_broadcast_grant(pg, other) is False
    assert delivery.history(other) == {"batches": []}
    with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired):
        delivery.prepare(other, {**fields, "cm_id": str(other)})
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
    lookups = []
    joined_groups = creator_broadcast._joined_groups

    def target_groups(conn, cm, purpose, chat_ids=None):
        lookups.append(chat_ids)
        return joined_groups(conn, cm, purpose, chat_ids)

    with monkeypatch.context() as patch:
        patch.setattr(creator_broadcast, "_joined_groups", target_groups)
        first = delivery.submit(actor, ready["preview_id"], request)
    assert lookups == [["oc_a", "oc_b", "oc_c"]]  # One targeted lookup for the whole submission.
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


def test_rich_content_upload_template_frozen_delivery_and_legacy_dedup(pg, tmp_path, monkeypatch):
    settings = SimpleNamespace(oauth_scopes=("offline_access", "im:chat:read", "im:message:readonly"),
        token_encryption_key=b"r" * 32, broadcast_image_dir=str(tmp_path / "images"))
    cipher, actor, other = TokenCipher(settings.token_encryption_key), uuid4(), uuid4()
    scopes = [*settings.oauth_scopes, *creator_broadcast.SEND_SCOPES, "im:resource"]
    for who in (actor, other):
        pg.execute("""INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
            scopes,access_token_expires_at,body_consent_at,broadcast_enabled)
            VALUES(%s,%s,'CM',%s,%s,NOW()+INTERVAL '1 hour',NOW(),TRUE)""",
            (who, "ou_" + who.hex, cipher.encrypt(who.hex), scopes))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (other,))
    pg.execute("INSERT INTO chats(chat_id,name) VALUES('oc_rich','Rich message group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'oc_rich')", (actor,))
    pg.execute("""INSERT INTO chat_group_classifications(chat_id,purpose,confirmed_by_authorization_id)
        VALUES('oc_rich','category',%s)""", (actor,))
    depth, uploaded, sent = 0, [], []

    @contextmanager
    def connect():
        nonlocal depth
        depth += 1
        try:
            yield pg
        finally:
            depth -= 1

    class Client:
        def __init__(self, received, *, http_client):
            assert received is settings

        def user_info(self, token):
            assert depth == 0
            return LarkProfile("ou_" + token, None, None, None)

        def upload_message_image(self, token, content, filename, mime_type):
            assert depth == 0 and token == actor.hex
            uploaded.append(content)
            return "img_fixture_rich"

        def check_chat_send_access(self, token, chat_id, **kwargs):
            assert depth == 0 and token == actor.hex and chat_id == "oc_rich"
            return {"chat_name": "Rich message group", "member_count": 3}

        def send_chat_post(self, token, chat_id, body, **kwargs):
            assert depth == 0 and token == actor.hex
            sent.append(build_chat_post_payload(chat_id, body, **kwargs))
            return "om_fixture_" + str(len(sent))

    def preview(who, cm, purpose, selections, message, mention):
        assert cm == str(who)
        return {"message": creator_broadcast._message(message), "purpose": purpose, "issues": [],
            "groups": [{"id": item["chat_id"], "name": "Rich message group", "people": [],
                        "mentions": [], "mention_all": mention} for item in selections]}

    for module in (delivery, creator_broadcast, broadcast_images):
        monkeypatch.setattr(module, "connection", connect)
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for module in (delivery, broadcast_images):
        monkeypatch.setattr(module, "LarkClient", Client)
    monkeypatch.setattr(creator_broadcast, "preview", preview)
    original = BytesIO()
    Image.new("RGB", (3, 4), "red").save(original, "GIF", save_all=True,
        append_images=[Image.new("RGB", (3, 4), "blue")], duration=100, loop=0)
    image = broadcast_images.upload(actor, original.getvalue(), "animation.gif")
    document = {"version": 2, "ops": [
        {"insert": "Announcement", "attributes": {"bold": True, "italic": True}},
        {"insert": " Details", "attributes": {"link": "https://example.test/product", "underline": True}},
        {"insert": "\nFirst item"}, {"insert": "\n", "attributes": {"list": "ordered"}},
        {"insert": {"image": image["id"]}}, {"insert": "\n"}]}
    document = broadcast_content.normalize(document)
    with pytest.raises(PermissionError):
        creator_broadcast.template_save(other, {"name": "Not my image", "body": document})
    template = creator_broadcast.template_save(actor, {"name": "Campaign", "body": document})["template"]
    assert template["body"] == document
    fields = {"cm_id": str(actor), "purpose": "category", "selections": [{"chat_id": "oc_rich"}],
              "message": template["body"], "mention": True}
    prepared = delivery.prepare(actor, fields)
    frozen = pg.execute("SELECT * FROM creator_broadcast_previews WHERE id=%s", (prepared["preview_id"],)).fetchone()
    assert document != frozen["body_ciphertext"] and frozen["snapshot"]["image_ids"] == [image["id"]]
    assert json.loads(message_body_cipher(settings.token_encryption_key).decrypt(frozen["body_ciphertext"])) == document
    changed_style = json.loads(json.dumps(document))
    changed_style["ops"][0]["attributes"].pop("bold")
    styled = delivery.prepare(actor, {**fields, "message": changed_style})
    assert pg.execute("SELECT fingerprint FROM creator_broadcast_previews WHERE id=%s",
                      (styled["preview_id"],)).fetchone()["fingerprint"] != frozen["fingerprint"]
    creator_broadcast.template_save(actor, {"id": template["id"], "revision": template["revision"],
        "name": "Changed after preview", "body": "Not the frozen message"})
    request_id = uuid4()
    batch_id = delivery.submit(actor, prepared["preview_id"], request_id)["batch"]["id"]
    assert delivery.submit(actor, prepared["preview_id"], request_id)["deduplicated"]
    duplicate = delivery.prepare(actor, fields)
    assert delivery.submit(actor, duplicate["preview_id"], uuid4())["batch"]["id"] == batch_id
    delivery.run_claimed(delivery.claim_next(), settings)
    assert delivery.claim_next() is None and len(sent) == 1 and uploaded == [original.getvalue()]
    nodes = [node for row in json.loads(sent[0]["content"])["zh_cn"]["content"] for node in row]
    assert {"tag": "at", "user_id": "all", "user_name": "all"} in nodes
    assert any(node.get("style") == ["bold", "italic"] and node["text"] == "Announcement" for node in nodes)
    assert any(node.get("href") == "https://example.test/product" for node in nodes)
    assert {"tag": "text", "text": "1. "} in nodes
    assert {"tag": "img", "image_key": "img_fixture_rich"} in nodes
    receipt = delivery.batch(actor, batch_id)
    assert receipt["message"] == document and receipt["batch"]["succeeded"] == 1
    assert delivery.history(actor)["batches"][0]["id"] == batch_id
    identical_image = broadcast_images.upload(actor, original.getvalue(), "same-bytes.gif")
    same_content = json.loads(json.dumps(document))
    for op in same_content["ops"]:
        if isinstance(op["insert"], dict):
            op["insert"]["image"] = identical_image["id"]
    repeated_upload = delivery.prepare(actor, {**fields, "message": same_content})
    assert delivery.submit(actor, repeated_upload["preview_id"], uuid4())["batch"]["id"] == batch_id
    assert len(sent) == 1 and delivery.claim_next() is None
    # A later resource revocation stops a queued rich send, without retrying or dropping its image.
    another = delivery.prepare(actor, {**fields, "message": changed_style})
    blocked_id = delivery.submit(actor, another["preview_id"], uuid4())["batch"]["id"]
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (scopes[:-1], actor))
    delivery.run_claimed(delivery.claim_next(), settings)
    assert delivery.batch(actor, blocked_id)["batch"]["failed"] == 1 and len(sent) == 1
    assert delivery.batch(actor, blocked_id)["message"] == changed_style
    # Text-only messages keep working without im:resource and deduplicate against the legacy representation.
    plain = "Legacy plain announcement"
    legacy = delivery.prepare(actor, {**fields, "message": plain})
    legacy_id = delivery.submit(actor, legacy["preview_id"], uuid4())["batch"]["id"]
    delivery.run_claimed(delivery.claim_next(), settings)
    rich_plain = delivery.prepare(actor, {**fields, "message": broadcast_content.normalize(plain)})
    same = delivery.submit(actor, rich_plain["preview_id"], uuid4())
    assert same["batch"]["id"] == legacy_id and same["deduplicated"] and len(sent) == 2
    assert delivery.batch(actor, legacy_id)["message"] == plain and delivery.claim_next() is None
    pg.execute("UPDATE creator_broadcast_images SET created_at=NOW()-INTERVAL '8 days'")
    assert broadcast_images.cleanup(settings) == 0
    assert broadcast_images.read(actor, image["id"])[0] == original.getvalue() and len(uploaded) == 2


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
    monkeypatch.setattr(delivery, "_routes_current", lambda *_args: None)
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
    dsn = os.environ["TEST_DATABASE_URL"]

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
