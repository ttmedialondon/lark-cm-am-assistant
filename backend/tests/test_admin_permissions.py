import json
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_permissions

OWNER = UUID("00000000-0000-0000-0000-000000000001")
PERSON = UUID("00000000-0000-0000-0000-000000000002")


class FakeConnection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []
        self.in_transaction = False

    def transaction(self):
        self.in_transaction = True
        return nullcontext()

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.result = next(self.rows, [])
        return self

    def fetchall(self):
        return self.result


def database(monkeypatch, rows):
    conn = FakeConnection(rows)
    monkeypatch.setattr(admin_permissions, "connection", lambda: nullcontext(conn))
    return conn


def account(person=PERSON, status="active", role=None, role_active=None, permissions=()):
    return {"id": person, "status": status, "role": role, "role_active": role_active, "permissions": permissions}


@pytest.mark.parametrize("role", [None, "viewer", "operator"])
def test_no_page_grants_means_frontend_only_even_for_legacy_roles(monkeypatch, role):
    database(monkeypatch, [[account(role=role, role_active=True)]])
    result = admin_permissions.access_for(PERSON)
    assert result["permissions"] == frozenset()
    assert result["has_admin_access"] is False
    assert result["landing_path"] is None
    assert result["access_label"] == "仅前台"


def test_owner_implicitly_has_every_page_without_explicit_grants(monkeypatch):
    database(monkeypatch, [[account(OWNER, role="owner", role_active=True)]])
    result = admin_permissions.access_for(OWNER)
    assert result["is_owner"] is True
    assert result["permissions"] == admin_permissions.PAGE_KEYS
    assert result["configured_permissions"] == admin_permissions.PAGE_KEYS
    assert result["landing_path"] == "/admin"


@pytest.mark.parametrize("status", ["revoked", "attention_required"])
def test_inactive_account_has_no_effective_access_but_saved_values_remain(monkeypatch, status):
    database(monkeypatch, [[account(status=status, permissions=["messages"])]])
    result = admin_permissions.access_for(PERSON)
    assert not result["has_admin_access"]
    assert result["configured_permissions"] == {"messages"}


def test_inactive_owner_role_does_not_confer_owner_access(monkeypatch):
    database(monkeypatch, [[account(OWNER, role="owner", role_active=False, permissions=["messages"])]])
    result = admin_permissions.access_for(OWNER)
    assert result["is_owner_account"] is True
    assert result["is_owner"] is False
    assert not result["permissions"]
    assert result["configured_permissions"] == {"messages"}


def test_batch_load_is_single_query_and_only_allowlisted_pages_apply(monkeypatch):
    conn = database(monkeypatch, [[account(permissions=["jobs", "creators", "creator-review", "unrecognized"])]])
    results = admin_permissions.users_access_data([OWNER, PERSON, PERSON])
    assert len(conn.calls) == 1
    assert conn.calls[0][1] == ([OWNER, PERSON],)
    assert results[PERSON]["permissions"] == {"jobs"}
    assert results[PERSON]["landing_path"] == "/admin/jobs"
    assert results[PERSON]["permission_labels"] == ["同步任务"]
    assert results[OWNER]["has_admin_access"] is False
    assert admin_permissions.users_access_data([]) == {}
    assert len(conn.calls) == 1


def write_rows(*, actor_status="active", actor_role="owner", actor_active=True, target_role=None, before=()):
    return [
        [{"id": OWNER, "status": actor_status}, {"id": PERSON, "status": "active"}],
        [
            {"authorization_id": OWNER, "role": actor_role, "is_active": actor_active},
            {"authorization_id": PERSON, "role": target_role, "is_active": True},
        ],
        [{"permission": key} for key in before],
    ]


@pytest.mark.parametrize(
    "invalid", [["creators"], ["creator-review"], [None], ["jobs", "jobs"], ["jobs"] * 10]
)
def test_invalid_page_rejected_without_database_access(monkeypatch, invalid):
    conn = database(monkeypatch, [])
    with pytest.raises(ValueError):
        admin_permissions.set_permissions(OWNER, PERSON, invalid)
    assert not conn.calls


@pytest.mark.parametrize(
    "actor", [{"actor_status": "revoked"}, {"actor_role": "operator"}, {"actor_active": False}]
)
def test_permission_writer_rechecks_actor_in_transaction(monkeypatch, actor):
    conn = database(monkeypatch, write_rows(**actor))
    with pytest.raises(PermissionError):
        admin_permissions.set_permissions(OWNER, PERSON, ["messages"])
    assert conn.in_transaction
    assert len(conn.calls) == 2
    assert all("FOR UPDATE" in sql for sql, _ in conn.calls)


@pytest.mark.parametrize("target", [OWNER, PERSON])
def test_owner_permissions_cannot_be_changed_including_self(monkeypatch, target):
    conn = database(monkeypatch, write_rows(target_role="owner"))
    with pytest.raises(ValueError):
        admin_permissions.set_permissions(OWNER, target, [])
    assert len(conn.calls) == 2


def test_missing_target_is_not_created(monkeypatch):
    rows = write_rows()
    rows[0] = rows[0][:1]
    conn = database(monkeypatch, rows)
    with pytest.raises(LookupError):
        admin_permissions.set_permissions(OWNER, PERSON, ["users"])
    assert len(conn.calls) == 2


@pytest.mark.parametrize("requested", [[], ["messages", "jobs"]])
def test_replacement_locks_accounts_and_audits_exact_before_after(monkeypatch, requested):
    conn = database(monkeypatch, write_rows(before=["messages"]))
    admin_permissions.set_permissions(OWNER, PERSON, requested)
    assert conn.in_transaction
    assert "ORDER BY id FOR UPDATE" in conn.calls[0][0]
    assert conn.calls[0][1] == ([OWNER, PERSON],)
    assert "FOR UPDATE" in conn.calls[2][0]
    assert conn.calls[3] == ("DELETE FROM admin_page_permissions WHERE authorization_id = %s", (PERSON,))
    if requested:
        assert conn.calls[4][1] == (PERSON, OWNER, sorted(set(requested)))
    else:
        assert not any("INSERT INTO admin_page_permissions" in sql for sql, _ in conn.calls)
    query, params = conn.calls[-1]
    assert "INSERT INTO audit_events" in query
    assert params[1:3] == (OWNER, "admin_page_permissions_updated")
    assert json.loads(params[3]) == {
        "target_authorization_id": str(PERSON), "before": ["messages"], "after": sorted(set(requested))
    }
    assert not any("UPDATE authorizations" in sql or "UPDATE admin_roles" in sql for sql, _ in conn.calls)


def test_unchanged_permissions_do_not_rewrite_or_audit(monkeypatch):
    conn = database(monkeypatch, write_rows(before=["messages"]))
    admin_permissions.set_permissions(OWNER, PERSON, ["messages"])
    assert len(conn.calls) == 3


def test_migration_has_fixed_keys_and_no_implicit_grants():
    sql = (Path(__file__).parents[1] / "migrations/027_admin_page_permissions.sql").read_text()
    assert "PRIMARY KEY (authorization_id, permission)" in sql
    assert "INSERT" not in sql
    for key in admin_permissions.PAGE_KEYS:
        assert f"'{key}'" in sql
