"""Real PostgreSQL receipts and races, using only a disposable local schema."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from cryptography.exceptions import InvalidTag
from psycopg.rows import dict_row

from app.crypto import TokenCipher
from app.desktop_reply_store import (
    ActorInactiveError,
    claim,
    finish_error,
    finish_success,
    read_request,
    result_cipher,
)

HASH = hashlib.sha256(b"synthetic input, never persisted").hexdigest()
CIPHER = result_cipher(b"k" * 32)


@pytest.fixture
def receipt_db():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to an isolated local PostgreSQL database")
    schema = "test_desktop_reply_" + uuid4().hex
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            conn.execute(path.read_text())
        actor, other = uuid4(), uuid4()
        for value in (actor, other):
            conn.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test-only')",
                         (value, "ou_" + value.hex))
        conn.commit()
        try:
            yield conn, actor, other, dsn, schema
        finally:
            conn.rollback()
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
            conn.commit()


def test_scoped_cache_and_conflicts_never_store_plaintext(receipt_db):
    conn, actor, other, _, _ = receipt_db
    request_id = uuid4()
    row = claim(conn, actor, request_id, HASH)
    assert row["claim_status"] == "new"
    assert claim(conn, actor, request_id, HASH)["claim_status"] == "running"
    assert claim(conn, actor, request_id, "f" * 64)["claim_status"] == "conflict"
    assert read_request(conn, other, request_id) is None
    assert claim(conn, other, request_id, HASH)["claim_status"] == "new"
    assert not finish_success(conn, actor, request_id, uuid4(), {"suggestion": "secret reply"}, CIPHER)
    assert finish_success(conn, actor, request_id, row["fence_token"], {"suggestion": "secret reply"}, CIPHER,
                          usage={"input_tokens": 12, "output_tokens": 4, "token": "must not persist"}, latency_ms=37)
    cached = claim(conn, actor, request_id, HASH)
    assert cached["claim_status"] == "cached"
    assert json.loads(CIPHER.decrypt(cached["result_ciphertext"])) == {"suggestion": "secret reply"}
    assert cached["usage"] == {"input_tokens": 12, "output_tokens": 4} and cached["latency_ms"] == 37
    assert "secret reply" not in repr(cached) and "synthetic input" not in repr(cached)
    assert "must not persist" not in repr(cached)
    assert read_request(conn, actor, request_id)["claim_status"] == "cached"
    with pytest.raises(InvalidTag):
        TokenCipher(b"k" * 32).decrypt(cached["result_ciphertext"])


def test_concurrent_claims_have_exactly_one_owner(receipt_db):
    _, actor, _, dsn, schema = receipt_db
    request_id = uuid4()
    ready = Barrier(8)

    def compete(_):
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            connection.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
            ready.wait(timeout=10)
            with connection.transaction():
                receipt = claim(connection, actor, request_id, HASH)
            connection.commit()
            return receipt

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(compete, range(8)))
    assert sorted(row["claim_status"] for row in rows) == ["new"] + ["running"] * 7
    assert len({row["fence_token"] for row in rows}) == 1


def test_terminal_errors_lease_and_expiry_do_not_reclaim_uncertain_work(receipt_db):
    conn, actor, other, _, _ = receipt_db
    for terminal in ("failed", "cancelled"):
        request_id = uuid4()
        row = claim(conn, actor, request_id, HASH)
        assert finish_error(conn, actor, request_id, row["fence_token"], code="provider_timeout",
                            message="sk-provider-secret AND input text", http_status=504, status=terminal)
        duplicate = claim(conn, actor, request_id, HASH)
        assert duplicate["claim_status"] == "failed" and duplicate["status"] == terminal
        assert duplicate["error_http_status"] == 504 and "secret" not in repr(duplicate)
        assert duplicate["error_code"] == ("cancelled" if terminal == "cancelled" else "provider_timeout")
        assert not finish_success(conn, actor, request_id, row["fence_token"], {"suggestion": "late"}, CIPHER)
    request_id = uuid4()
    old = claim(conn, actor, request_id, HASH)
    conn.execute("UPDATE desktop_reply_requests SET lease_until=clock_timestamp()-INTERVAL '1 second' "
                 "WHERE actor_id=%s AND request_id=%s",
                 (actor, request_id))
    assert not finish_success(conn, actor, request_id, old["fence_token"], {"suggestion": "late"}, CIPHER)
    abandoned = claim(conn, actor, request_id, HASH)
    assert abandoned["status"] == "abandoned" and abandoned["claim_status"] == "failed"
    assert not finish_error(conn, actor, request_id, old["fence_token"], code="provider_error")
    other_id = uuid4()
    claim(conn, other, other_id, HASH)
    conn.execute("UPDATE desktop_reply_requests SET expires_at=clock_timestamp()-INTERVAL '1 second'")
    assert read_request(conn, actor, request_id) is None
    assert conn.execute("SELECT 1 FROM desktop_reply_requests WHERE actor_id=%s", (other,)).fetchone()
    replacement = claim(conn, actor, request_id, HASH)
    assert replacement["claim_status"] == "new" and replacement["fence_token"] != old["fence_token"]
    assert not finish_success(conn, actor, request_id, old["fence_token"], {"suggestion": "late old job"}, CIPHER)


def test_every_operation_rechecks_revocation(receipt_db):
    conn, actor, _, _, _ = receipt_db
    request_id = uuid4()
    row = claim(conn, actor, request_id, HASH)
    conn.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,))
    calls = (
        lambda: claim(conn, actor, request_id, HASH),
        lambda: read_request(conn, actor, request_id),
        lambda: finish_success(conn, actor, request_id, row["fence_token"], {"suggestion": "blocked"}, CIPHER),
        lambda: finish_error(conn, actor, request_id, row["fence_token"], code="provider_error"),
    )
    for operation in calls:
        with pytest.raises(ActorInactiveError):
            operation()
    row = conn.execute("SELECT status FROM desktop_reply_requests WHERE actor_id=%s", (actor,)).fetchone()
    assert row["status"] == "running"
