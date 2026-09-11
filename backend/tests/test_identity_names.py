from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import identity_profiles, worker
from app.crypto import TokenCipher
from app.identity_names import format_display_name, update_identity_names
from app.lark_api import LarkApiError, LarkProfile
from scripts import backfill_identity_names as backfill


@pytest.mark.parametrize(
    ("name", "english", "expected"),
    [
        ("Quanzhen Liang", "Remi", "Remi（Quanzhen Liang）"),
        ("  Alex   Smith ", "alex smith", "alex smith"),
        ("Ａlex", "Alex", "Alex（Ａlex）"),
        ("张三", None, "张三"),
        (None, "Alice", "Alice"),
        ("", "  ", None),
        (None, None, None),
        ("Remi（Quanzhen Liang）", None, "Remi（Quanzhen Liang）"),
    ],
)
def test_display_format(name, english, expected):
    assert format_display_name(name, english) == expected


def test_member_records_keep_sources_and_distinguish_missing_english():
    values = worker._member_records(
        {
            "items": [
                {"member_id": "ou_a", "name": "Quanzhen Liang", "en_name": "Remi"},
                {"member_id": "ou_b", "name": "No English"},
                {"member_id": "ou_c", "name": "Cleared English", "en_name": ""},
            ]
        }
    )
    assert values[0]["name"] == "Quanzhen Liang" and values[0]["en_name"] == "Remi"
    assert values[0]["display_name"] == "Remi（Quanzhen Liang）"
    assert not values[1]["en_name_present"]
    assert values[2]["en_name_present"] and values[2]["en_name"] is None


@pytest.fixture
def pg(monkeypatch):
    dsn = os.environ.get("IDENTITY_NAMES_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set IDENTITY_NAMES_TEST_DATABASE_URL for isolated PostgreSQL checks")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        schema = "test_identity_names_" + uuid4().hex
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            if int(path.name[:3]) < 33:
                conn.execute(path.read_text())
        conn.execute("INSERT INTO lark_identities(open_id,display_name) VALUES('ou_legacy','Old Raw Name')")
        conn.execute((Path(__file__).parents[1] / "migrations/033_identity_names.sql").read_text())
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            if int(path.name[:3]) > 33:
                conn.execute(path.read_text())
        monkeypatch.setattr(worker, "connection", lambda: nullcontext(conn))
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def person(pg):
    contact_id, authorization_id = uuid4(), uuid4()
    pg.execute("INSERT INTO contacts(id,display_name,entity_type) VALUES(%s,'Old Name','person')", (contact_id,))
    pg.execute(
        """INSERT INTO lark_identities(open_id,contact_id,name,display_name)
               VALUES('ou_person',%s,'Quanzhen Liang','Old Name')""",
        (contact_id,),
    )
    pg.execute(
        """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext)
               VALUES(%s,'ou_person','Verified Old Profile','test')""",
        (authorization_id,),
    )
    for chat_id in ("chat_a", "chat_b"):
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (chat_id,))
        pg.execute(
            """INSERT INTO chat_members(chat_id,open_id,display_name)
                   VALUES(%s,'ou_person','Old Member Name')""",
            (chat_id,),
        )
    return contact_id, authorization_id


def test_migration_preserves_legacy_raw_name_without_inventing_english(pg):
    row = pg.execute("SELECT name,en_name,display_name FROM lark_identities WHERE open_id='ou_legacy'").fetchone()
    assert row == {"name": "Old Raw Name", "en_name": None, "display_name": "Old Raw Name"}


def test_authoritative_update_propagates_unambiguous_labels_but_not_authorization(pg, person):
    contact_id, _ = person
    label = update_identity_names(
        pg, "ou_person", name="Quanzhen Liang", en_name="Remi", en_name_present=True, authoritative=True
    )
    assert label == "Remi（Quanzhen Liang）"
    assert (
        pg.execute("SELECT display_name FROM contacts WHERE id=%s", (contact_id,)).fetchone()["display_name"] == label
    )
    assert {r["display_name"] for r in pg.execute("SELECT display_name FROM chat_members").fetchall()} == {label}
    assert pg.execute("SELECT display_name FROM authorizations").fetchone()["display_name"] == "Verified Old Profile"
    # Repair a downstream stale cache even though the canonical identity is unchanged.
    pg.execute("UPDATE contacts SET display_name='Stale' WHERE id=%s", (contact_id,))
    update_identity_names(pg, "ou_person")
    assert (
        pg.execute("SELECT display_name FROM contacts WHERE id=%s", (contact_id,)).fetchone()["display_name"] == label
    )


def test_old_message_cannot_revert_known_profile_name_and_omitted_english_is_preserved(pg, person):
    update_identity_names(
        pg, "ou_person", name="Quanzhen Liang", en_name="Remi", en_name_present=True, authoritative=True
    )
    worker._upsert_lark_identity(pg, "ou_person", display_name="Old Message Nickname")
    worker._upsert_lark_identity(pg, "ou_person", display_name="Quanzhen Liang", authoritative=True)
    row = pg.execute("SELECT name,en_name,display_name FROM lark_identities WHERE open_id='ou_person'").fetchone()
    assert row == {"name": "Quanzhen Liang", "en_name": "Remi", "display_name": "Remi（Quanzhen Liang）"}
    assert (
        update_identity_names(pg, "ou_person", en_name="", en_name_present=True, authoritative=True) == "Quanzhen Liang"
    )


def test_english_only_is_not_copied_into_original_field(pg):
    worker._upsert_lark_identity(pg, "ou_english", en_name="English Only", en_name_present=True, authoritative=True)
    row = pg.execute("SELECT name,en_name,display_name FROM lark_identities WHERE open_id='ou_english'").fetchone()
    assert row == {"name": None, "en_name": "English Only", "display_name": "English Only"}
    assert update_identity_names(pg, "ou_english", en_name="", en_name_present=True, authoritative=True) is None


def test_multi_identity_contact_label_is_preserved(pg, person):
    contact_id, _ = person
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,name) VALUES('ou_other',%s,'Another Profile')", (contact_id,)
    )
    label = update_identity_names(
        pg, "ou_person", name="Quanzhen Liang", en_name="Remi", en_name_present=True, authoritative=True
    )
    assert label == "Remi（Quanzhen Liang）"
    assert (
        pg.execute("SELECT display_name FROM contacts WHERE id=%s", (contact_id,)).fetchone()["display_name"]
        == "Old Name"
    )


def test_new_contact_and_new_member_keep_canonical_composite(pg, person):
    now = datetime.now(UTC)
    members = worker._member_records({"items": [{"member_id": "ou_new", "name": "张三", "en_name": "Alice"}]})
    measurement = worker.MemberSnapshotMeasurement(1, "succeeded", "succeeded", now, tuple(members))
    worker._store_member_snapshot(pg, person[1], "chat_a", measurement, now, set())
    row = pg.execute("""SELECT i.name,i.en_name,i.display_name,c.display_name AS contact_name,
                   m.display_name AS member_name FROM lark_identities i JOIN contacts c ON c.id=i.contact_id
                   JOIN chat_members m USING(open_id) WHERE i.open_id='ou_new'""").fetchone()
    assert row == {
        "name": "张三",
        "en_name": "Alice",
        "display_name": "Alice（张三）",
        "contact_name": "Alice（张三）",
        "member_name": "Alice（张三）",
    }
    worker._ensure_contact_for_identity(
        pg, "ou_new", tenant_classification="unresolved", identity_type="user", display_name="张三"
    )
    assert (
        pg.execute(
            "SELECT display_name FROM contacts WHERE id=(SELECT contact_id FROM lark_identities WHERE open_id='ou_new')"
        ).fetchone()["display_name"]
        == "Alice（张三）"
    )


def test_tenant_refresh_updates_changed_facts_without_rewriting_other_identities(pg, person):
    pg.execute("UPDATE lark_identities SET tenant_key='company' WHERE open_id='ou_person'")
    pg.execute(
        """INSERT INTO lark_identities(open_id,tenant_key,identity_type) VALUES
               ('ou_external','external','user'), ('ou_app',NULL,'app')"""
    )
    worker._refresh_tenant_classifications(pg, set())
    before = {
        row["open_id"]: row
        for row in pg.execute("SELECT open_id,tenant_classification,ctid::text AS version FROM lark_identities")
    }
    assert before["ou_person"]["tenant_classification"] == "known_external"
    assert before["ou_external"]["tenant_classification"] == "known_external"
    assert before["ou_legacy"]["tenant_classification"] == "unresolved"
    assert before["ou_app"]["tenant_classification"] == "internal"

    worker._refresh_tenant_classifications(pg, {"company"})
    after = {
        row["open_id"]: row
        for row in pg.execute("SELECT open_id,tenant_classification,ctid::text AS version FROM lark_identities")
    }
    assert after.pop("ou_person")["tenant_classification"] == "internal"
    before.pop("ou_person")
    assert after == before  # An UPDATE, even to the same value, creates a new tuple/lock.
    role = pg.execute("SELECT role,status,is_primary FROM contact_roles WHERE contact_id=%s", (person[0],)).fetchone()
    assert role == {"role": "internal_colleague", "status": "confirmed", "is_primary": True}

    # A business classification is distinct from the unchanged tenant fact:
    # preserve manual source/status/primary, and replace only automatic guesses.
    decisions = (
        ("manual_primary", "merchant_contact", "confirmed", "ai_manual_confirmed", True),
        ("manual_rejected", "internal_colleague", "rejected", "manual", False),
        ("manual_secondary", "internal_colleague", "confirmed", "manual", False),
        ("ai_colleague", "internal_colleague", "confirmed", "ai_auto_confirmed", True),
        ("ai_merchant", "merchant_contact", "confirmed", "ai_auto_confirmed", True),
    )
    ids = {}
    for name, category, status, source, primary in decisions:
        contact_id = ids[name] = uuid4()
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact_id, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,identity_type,tenant_classification) "
                   "VALUES(%s,%s,'user','internal')", ("ou_" + name, contact_id))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) VALUES(%s,%s,%s,%s,%s)",
                   (contact_id, category, status, source, primary))
    manual_sql = ("SELECT *,ctid::text AS version FROM contact_roles "
                  "WHERE source IN ('manual','ai_manual_confirmed') ORDER BY contact_id,role")
    identities_sql = "SELECT *,ctid::text AS version FROM lark_identities ORDER BY open_id"
    roles_sql = "SELECT *,ctid::text AS version FROM contact_roles ORDER BY contact_id,role"
    manual_before = pg.execute(manual_sql).fetchall()
    identities_before = pg.execute(identities_sql).fetchall()
    worker._ensure_internal_contact_roles(pg)
    assert pg.execute(manual_sql).fetchall() == manual_before
    assert pg.execute(identities_sql).fetchall() == identities_before
    primary_roles = pg.execute(
        "SELECT contact_id,role,source FROM contact_roles WHERE is_primary AND status='confirmed'")
    primary_roles = {row["contact_id"]: (row["role"], row["source"]) for row in primary_roles}
    assert primary_roles[ids["manual_primary"]] == ("merchant_contact", "ai_manual_confirmed")
    assert ids["manual_rejected"] not in primary_roles and ids["manual_secondary"] not in primary_roles
    for name in ("ai_colleague", "ai_merchant"):
        assert primary_roles[ids[name]] == ("internal_colleague", "lark_internal_tenant")
    before_repeat = pg.execute(roles_sql).fetchall()
    worker._ensure_internal_contact_roles(pg)
    assert pg.execute(roles_sql).fetchall() == before_repeat


def test_group_display_variant_cannot_override_authorized_profile(pg, person):
    update_identity_names(
        pg, "ou_person", name="Quanzhen Liang", en_name="Remi", en_name_present=True, authoritative=True
    )
    members = worker._member_records({"items": [{"member_id": "ou_person", "name": "Remi", "en_name": ""}]})
    now = datetime.now(UTC)
    measurement = worker.MemberSnapshotMeasurement(1, "succeeded", "succeeded", now, tuple(members))
    worker._store_member_snapshot(pg, person[1], "chat_a", measurement, now, set())
    row = pg.execute("SELECT name,en_name,display_name FROM lark_identities WHERE open_id='ou_person'").fetchone()
    assert row == {"name": "Quanzhen Liang", "en_name": "Remi", "display_name": "Remi（Quanzhen Liang）"}


@pytest.mark.parametrize("change", [None, "revoked", "token", "identity", "job", "api"])
def test_optional_profile_refresh_fences_writes_and_never_breaks_authorization(pg, person, change):
    _, auth_id = person
    key = b"x" * 32
    cipher = TokenCipher(key)
    pg.execute(
        "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s", (cipher.encrypt("test-access"), auth_id)
    )
    job = worker.SyncJob(uuid4(), auth_id, "incremental", 1, "running", 1)
    pg.execute(
        """INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_status,discovery_generation,scheduled_at)
               VALUES(%s,%s,'incremental','running','running',1,NOW())""",
        (job.id, auth_id),
    )
    authorization = worker._authorization(auth_id)

    class Client:
        def user_info(self, _token):
            if change == "api":
                raise LarkApiError(403, 99991679, "simulated denied")
            if change == "revoked":
                pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (auth_id,))
            if change == "token":
                pg.execute(
                    "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s",
                    (cipher.encrypt("new-token"), auth_id),
                )
            if change == "job":
                pg.execute("UPDATE sync_jobs SET discovery_generation=2 WHERE id=%s", (job.id,))
            return LarkProfile(
                open_id="ou_mismatch" if change == "identity" else "ou_person",
                union_id=None,
                name="Quanzhen Liang",
                en_name="Remi",
                en_name_present=True,
                avatar_url="https://s16-imfile-sg.feishucdn.com/new-avatar",
                avatar_present=True,
            )

    worker._refresh_profile_name(job, authorization, "test-access", Client(), SimpleNamespace(token_encryption_key=key))
    auth = pg.execute("SELECT display_name,status FROM authorizations WHERE id=%s", (auth_id,)).fetchone()
    assert auth["display_name"] == ("Remi（Quanzhen Liang）" if change is None else "Verified Old Profile")
    assert auth["status"] == ("revoked" if change == "revoked" else "active")
    avatar = pg.execute("SELECT avatar_url FROM lark_identities WHERE open_id='ou_person'").fetchone()["avatar_url"]
    assert avatar == ("https://s16-imfile-sg.feishucdn.com/new-avatar" if change is None else None)


@pytest.mark.parametrize("change", [None, "identity", "token", "revoked"])
def test_backfill_profile_reuses_verified_profile_and_fences_changed_authorization(pg, person, monkeypatch, change):
    auth_id = person[1]
    cipher = TokenCipher(b"x" * 32)
    pg.execute(
        "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s", (cipher.encrypt("test-token"), auth_id)
    )
    monkeypatch.setattr(backfill, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(backfill, "_authorization", worker._authorization)
    monkeypatch.setattr(backfill, "_usable_access_token", lambda *_: "test-token")

    class Client:
        def user_info(self, _token):
            if change == "token":
                pg.execute(
                    "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s",
                    (cipher.encrypt("new-token"), auth_id),
                )
            elif change == "revoked":
                pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (auth_id,))
            return LarkProfile(
                open_id="ou_wrong" if change == "identity" else "ou_person",
                union_id=None,
                name="Quanzhen Liang",
                en_name="Remi",
                en_name_present=True,
                avatar_url=None,
            )

    status = backfill.refresh_profile(auth_id, Client(), SimpleNamespace(token_encryption_key=b"x" * 32))
    assert status == ({None: "refreshed", "identity": "identity_mismatch"}.get(change, "authorization_changed"))
    expected = "Remi（Quanzhen Liang）" if change is None else "Verified Old Profile"
    assert (
        pg.execute("SELECT display_name FROM authorizations WHERE id=%s", (auth_id,)).fetchone()["display_name"]
        == expected
    )


def test_expired_token_refresh_does_not_reactivate_a_concurrently_revoked_authorization(pg, person):
    auth_id = person[1]
    key = b"x" * 32
    cipher = TokenCipher(key)
    expired = datetime.now(UTC) - timedelta(hours=1)
    pg.execute(
        """UPDATE authorizations SET access_token_ciphertext=%s,refresh_token_ciphertext=%s,
               access_token_expires_at=%s,refresh_token_expires_at=%s WHERE id=%s""",
        (
            cipher.encrypt("old-access"),
            cipher.encrypt("old-refresh"),
            expired,
            datetime.now(UTC) + timedelta(days=1),
            auth_id,
        ),
    )
    stale = worker._authorization(auth_id)
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (auth_id,))

    class Client:
        def refresh_token(self, _token):
            pytest.fail("A revoked authorization must not call refresh or reactivate")

    with pytest.raises(worker.ReauthorizationRequired):
        worker._usable_access_token(auth_id, stale, Client(), SimpleNamespace(token_encryption_key=key))
    row = pg.execute("SELECT status,access_token_ciphertext FROM authorizations WHERE id=%s", (auth_id,)).fetchone()
    assert row["status"] == "revoked"
    assert cipher.decrypt(row["access_token_ciphertext"]) == "old-access"


@pytest.mark.parametrize(
    "change",
    [None, "hidden_before", "hidden_after", "revoked", "token", "generation", "scope", "disabled", "closed", "p2p"],
)
def test_member_name_refresh_only_updates_known_users_in_still_authorized_chat(pg, person, monkeypatch, change):
    auth_id = person[1]
    key = b"x" * 32
    cipher = TokenCipher(key)
    pg.execute(
        "UPDATE authorizations SET scopes=ARRAY['im:chat.members:read'],access_token_ciphertext=%s WHERE id=%s",
        (cipher.encrypt("current-token"), auth_id),
    )
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat_a')", (auth_id,))
    pg.execute("INSERT INTO lark_identities(open_id,name,display_name) VALUES('ou_external','user123','user123')")
    update_identity_names(
        pg, "ou_person", name="Quanzhen Liang", en_name="Remi", en_name_present=True, authoritative=True
    )
    generation = pg.execute("SELECT sync_generation FROM authorization_chats WHERE chat_id='chat_a'").fetchone()[
        "sync_generation"
    ]
    if change == "hidden_before":
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='chat_a'")
    monkeypatch.setattr(backfill, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(backfill, "_authorization", worker._authorization)
    monkeypatch.setattr(backfill, "_usable_access_token", lambda *_: "current-token")

    class Client:
        def list_chat_members(self, *_):
            assert change != "hidden_before", "Hidden chats must not be read from Lark"
            if change == "hidden_after":
                pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='chat_a'")
            elif change == "revoked":
                pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (auth_id,))
            elif change == "token":
                pg.execute(
                    "UPDATE authorizations SET access_token_ciphertext=%s WHERE id=%s",
                    (cipher.encrypt("replaced-token"), auth_id),
                )
            elif change == "generation":
                pg.execute("UPDATE authorization_chats SET sync_generation=sync_generation+1 WHERE chat_id='chat_a'")
            elif change == "scope":
                pg.execute("UPDATE authorizations SET scopes=ARRAY[]::text[] WHERE id=%s", (auth_id,))
            elif change == "disabled":
                pg.execute("UPDATE authorization_chats SET sync_enabled=FALSE WHERE chat_id='chat_a'")
            elif change == "closed":
                pg.execute("UPDATE chats SET chat_status='dissolved' WHERE chat_id='chat_a'")
            elif change == "p2p":
                pg.execute("UPDATE chats SET chat_mode='p2p' WHERE chat_id='chat_a'")
            return {
                "user_total": 3,
                "users": [
                    {"member_id": "ou_external", "name": "Current Name", "en_name": "English"},
                    {"member_id": "ou_not_in_our_database", "name": "Do not import"},
                    {"member_id": "ou_person", "name": "Conflicting group nickname", "en_name": ""},
                ],
            }

    outcome = backfill.refresh_members(
        {"authorization_id": auth_id, "chat_id": "chat_a", "sync_generation": generation},
        Client(),
        SimpleNamespace(token_encryption_key=key),
    )
    assert outcome["status"] == ("succeeded" if change is None else "authorization_changed")
    name = pg.execute("SELECT display_name FROM lark_identities WHERE open_id='ou_external'").fetchone()["display_name"]
    assert name == ("English（Current Name）" if change is None else "user123")
    assert pg.execute("SELECT 1 FROM lark_identities WHERE open_id='ou_not_in_our_database'").fetchone() is None
    profile = pg.execute("SELECT name,en_name,display_name FROM lark_identities WHERE open_id='ou_person'").fetchone()
    assert profile == {"name": "Quanzhen Liang", "en_name": "Remi", "display_name": "Remi（Quanzhen Liang）"}


@pytest.mark.parametrize("failures", [1, 3])
def test_member_name_refresh_retries_transaction_conflicts_without_aborting_batch(monkeypatch, failures):
    calls = []

    def refresh(*_):
        calls.append(1)
        if len(calls) <= failures:
            raise backfill.DeadlockDetected("simulated concurrent sync")
        return {"status": "succeeded", "users": ["ou_known"]}

    monkeypatch.setattr(backfill, "refresh_members", refresh)
    result = backfill.refresh_members_safely({}, None, None)
    assert len(calls) == min(failures + 1, 3)
    assert result == (
        {"status": "database_busy", "users": []} if failures == 3 else {"status": "succeeded", "users": ["ou_known"]}
    )


def test_avatar_cache_permission_backoff_visibility_and_late_response_guards(pg, person, monkeypatch):
    """One real queue workflow covers cache safety and a whole denied-provider sweep."""
    profiles = identity_profiles
    key, own_id = b"x" * 32, person[1]
    settings, cipher = SimpleNamespace(token_encryption_key=key), TokenCipher(key)
    old_url, new_url = ("https://s16-imfile-sg.feishucdn.com/old", "https://s16-imfile-sg.feishucdn.com/new")
    for bad in ("http://cdn.feishucdn.com/a", "https://cdn.feishucdn.com.evil/a", "https://127.0.0.1/a",
                "https://u@cdn.feishucdn.com/a", "https://cdn.feishucdn.com:444/a", "javascript:alert(1)"):
        assert profiles.safe_avatar_url(bad) is None
    assert profiles.safe_avatar_url(old_url) == old_url
    monkeypatch.setattr(profiles, "connection", lambda: nullcontext(pg))
    # The avatar-only 1rps reservation must not slow the shared message request clock.
    shared_clock = pg.execute("SELECT next_request_at FROM lark_sync_admission_state").fetchone()["next_request_at"]
    profiles._wait_avatar_slot()
    assert (pg.execute("SELECT next_request_at FROM lark_sync_admission_state").fetchone()["next_request_at"]
            == shared_clock)
    monkeypatch.setattr(profiles, "_wait_avatar_slot", lambda: None)
    monkeypatch.setattr(profiles.LOGGER, "warning", lambda *_: pytest.fail("Unexpected avatar worker error"))
    pg.execute("UPDATE authorizations SET access_token_ciphertext=%s,access_token_expires_at=NOW()+INTERVAL '1 hour',"
               "body_consent_at=NOW()-INTERVAL '1 day' WHERE id=%s", (cipher.encrypt("own-token"), own_id))
    other_cm = uuid4()
    pg.execute("INSERT INTO lark_identities(open_id) VALUES('ou_cm2')")
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,access_token_expires_at,body_consent_at) "
               "VALUES(%s,'ou_cm2',%s,NOW()+INTERVAL '1 hour',NOW()-INTERVAL '1 day')",
               (other_cm, cipher.encrypt("cm2-token")))
    # A login-only CM has no external visibility and must not prevent the bulk denied-source pass.
    pg.execute("INSERT INTO lark_identities(open_id) VALUES('ou_self_only')")
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,access_token_expires_at) "
               "VALUES(%s,'ou_self_only',%s,NOW()+INTERVAL '1 hour')", (uuid4(), cipher.encrypt("self-token")))
    for cm in (own_id, other_cm):
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat_a')", (cm,))
    for index in range(4):
        target = f"ou_external_{index}"
        pg.execute("INSERT INTO lark_identities(open_id,avatar_url) VALUES(%s,%s)", (target, old_url))
        pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id) VALUES('chat_a',%s,%s)",
                   (str(index), target))
        for cm in (own_id, other_cm):
            pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                       "VALUES(%s,'chat_a',%s)", (cm, str(index)))
    before = pg.execute("SELECT open_id,contact_id,name,en_name,display_name,last_seen_at FROM lark_identities "
                        "ORDER BY open_id").fetchall()
    calls = []

    class Client:
        mode = "denied"

        def user_info(self, token):
            calls.append(("self", token))
            return LarkProfile({"own-token": "ou_person", "cm2-token": "ou_cm2", "self-token": "ou_self_only"}[token],
                               None, None, new_url,
                               avatar_present=True)

        def contact_user(self, token, open_id):
            calls.append(("contact", token))
            if self.mode == "denied":
                raise LarkApiError(400, 99991679, "scope not allowed")
            if self.mode == "network":
                raise LarkApiError(503, None, "network failure")
            if self.mode == "other_reader" and token == self.first_token:
                raise LarkApiError(400, 41050, "This CM cannot access this person")
            if self.mode == "changed_scope":
                pg.execute("UPDATE authorizations SET body_consent_at=NOW()")
            if self.mode == "newer_cache":
                profiles.store_profile_avatar(pg, open_id, new_url)
            return LarkProfile(open_id, None, None, old_url, avatar_present=self.mode != "missing_field")

    client = Client()
    while job := profiles.claim_next():
        profiles.run_claimed(job, client, settings)
    # Both visible CMs get one probe; the first denied CM must not mask the untried one.
    assert sorted(token for kind, token in calls if kind == "contact") == ["cm2-token", "own-token"]
    assert len(calls) == 5
    statuses = {r["open_id"]: r for r in pg.execute(
        "SELECT open_id,avatar_status,avatar_url,avatar_retry_at>NOW() AS delayed FROM lark_identities")}
    assert all(row["delayed"] for row in statuses.values())
    assert statuses["ou_person"]["avatar_status"] == "succeeded"
    assert statuses["ou_external_3"]["avatar_status"] == "unavailable"
    assert statuses["ou_external_3"]["avatar_url"] == old_url  # Kept, but unavailable is hidden by the read model.
    assert pg.execute("SELECT avatar_error_code FROM lark_identities WHERE open_id='ou_legacy'").fetchone()[
        "avatar_error_code"] == "no_visible_authorization"
    assert pg.execute("SELECT open_id,contact_id,name,en_name,display_name,last_seen_at FROM lark_identities "
                      "ORDER BY open_id").fetchall() == before

    target = "ou_external_0"

    def due():
        pg.execute("UPDATE lark_identities SET avatar_retry_at=NOW() WHERE open_id=%s", (target,))
        return profiles.claim_next()

    retry = due()
    assert pg.execute("SELECT avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()[
        "avatar_status"] == "unavailable"  # A retry must not briefly reveal the old denied avatar.
    profiles.run_claimed(retry, client, settings)
    assert len(calls) == 5  # Same-day retry only observes the persisted provider cooldown.
    pg.execute("UPDATE authorizations SET scopes=ARRAY['new-profile-scope'] WHERE id=%s", (own_id,))
    client.mode = "good"
    profiles.run_claimed(due(), client, settings)
    assert pg.execute("SELECT avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()[
        "avatar_status"] == "succeeded"
    assert profiles.claim_next() is None
    # An otherwise working CM's per-person 41050 cannot hide another currently authorized reader.
    pg.execute("UPDATE identity_profile_providers SET status='available',retry_at=NOW()")
    client.mode = "other_reader"
    client.first_token = cipher.decrypt(profiles._source(pg, target)["access_token_ciphertext"])
    started = len(calls)
    profiles.run_claimed(due(), client, settings)
    assert len(calls) == started + 2 and len({token for _, token in calls[started:]}) == 2
    assert pg.execute("SELECT avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()[
        "avatar_status"] == "succeeded"
    # A restarted worker can reclaim an expired lease; the old worker must not send or overwrite it.
    stale = due()
    current = due()
    started = len(calls)
    profiles.run_claimed(stale, client, settings)
    assert len(calls) == started
    assert pg.execute("SELECT avatar_claim_id FROM lark_identities WHERE open_id=%s", (target,)).fetchone()[
        "avatar_claim_id"] == current["avatar_claim_id"]
    client.mode = "good"
    profiles.run_claimed(current, client, settings)
    client.mode = "newer_cache"
    profiles.run_claimed(due(), client, settings)
    assert (pg.execute("SELECT avatar_url FROM lark_identities WHERE open_id=%s", (target,)).fetchone()["avatar_url"]
            == new_url)
    client.mode = "network"
    profiles.run_claimed(due(), client, settings)
    row = pg.execute("SELECT avatar_url,avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()
    assert row == {"avatar_url": new_url, "avatar_status": "failed"}
    pg.execute("UPDATE identity_profile_providers SET status='available'")
    client.mode = "changed_scope"
    profiles.run_claimed(due(), client, settings)
    row = pg.execute("SELECT avatar_url,avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()
    assert row == {"avatar_url": new_url, "avatar_status": "unavailable"}
    assert pg.execute("SELECT COUNT(*) AS n FROM authorizations WHERE status='active'").fetchone()["n"] == 3
    # Local sources must be CM-specific and current: an old observation/foreign roster cannot grant access.
    pg.execute("UPDATE authorization_message_observations SET observed_at=NOW()-INTERVAL '2 days'")
    pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type,last_observed_by_authorization_id) "
               "VALUES('chat_a',%s,'user',%s)", (target, other_cm))
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (other_cm,))
    assert profiles._source(pg, target) is None
    pg.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=%s,p2p_counterpart_type='app'", (target,))
    assert profiles._source(pg, target) is None
    pg.execute("UPDATE chat_members SET last_observed_by_authorization_id=%s WHERE open_id=%s", (own_id, target))
    assert profiles._source(pg, target)["id"] == own_id
    pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (own_id,))
    assert profiles._source(pg, target) is None
    profiles.store_profile_avatar(pg, target, None, avatar_present=False)
    assert (pg.execute("SELECT avatar_url FROM lark_identities WHERE open_id=%s", (target,)).fetchone()["avatar_url"]
            == new_url)
    assert not profiles.store_profile_avatar(pg, target, "https://attacker.invalid/image")
    assert (pg.execute("SELECT avatar_url,avatar_status FROM lark_identities WHERE open_id=%s", (target,)).fetchone()
            == {"avatar_url": new_url, "avatar_status": "unavailable"})
    profiles.store_profile_avatar(pg, target, None, avatar_present=True)
    assert (pg.execute("SELECT avatar_url FROM lark_identities WHERE open_id=%s", (target,)).fetchone()["avatar_url"]
            is None)
