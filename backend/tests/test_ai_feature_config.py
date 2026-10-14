from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from app.ai_feature_config import (
    FeatureConfigInputError,
    api_key_cipher,
    display_config,
    get_feature_config,
    save_feature_config,
)
from app.crypto import TokenCipher


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))
        if "FOR UPDATE" in query:
            return _Result(self.row)
        if "RETURNING" in query:
            self.row = {
                "feature": params[0],
                "provider": params[1],
                "project_id": params[2],
                "model": params[3],
                "enabled": params[4],
                "auto_confirm_threshold": params[5],
                "key_suffix": params[7] or (self.row or {}).get("key_suffix"),
                "key_updated_at": None,
                "last_tested_at": None,
                "last_test_status": "never",
                "created_at": None,
                "updated_at": None,
            }
            return _Result(self.row)
        return _Result(self.row)


def test_feature_key_uses_a_separate_encryption_domain() -> None:
    master_key = b"a" * 32
    cipher = api_key_cipher(master_key)
    sealed = cipher.encrypt("sk-test-value-1234")

    assert cipher.decrypt(sealed) == "sk-test-value-1234"
    with pytest.raises(InvalidTag):
        TokenCipher(master_key).decrypt(sealed)


def test_first_time_enable_requires_a_key() -> None:
    with pytest.raises(FeatureConfigInputError, match="首次启用"):
        save_feature_config(
            _Connection(),
            api_key_cipher(b"a" * 32),
            "contact_assessment",
            provider="openai",
            project_id="proj_contact_assessment",
            model="gpt-5.6-luna",
            enabled=True,
            api_key=None,
            updated_by_authorization_id=uuid4(),
        )


def test_blank_feature_key_can_be_enabled_on_real_postgres(pg):
    owner = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
               (owner, "ou_" + owner.hex))
    values = dict(provider="openai", project_id="proj_test", model="configured-model",
                  updated_by_authorization_id=owner)
    cipher = api_key_cipher(b"k" * 32)
    created = save_feature_config(pg, cipher, "offhours_summary", **values, enabled=False,
                                  api_key="sk-test-only-1234")
    ciphertext = pg.execute("SELECT api_key_ciphertext FROM ai_feature_configs "
                            "WHERE feature='offhours_summary'").fetchone()["api_key_ciphertext"]
    assert ciphertext != "sk-test-only-1234" and cipher.decrypt(ciphertext) == "sk-test-only-1234"
    assert "api_key_ciphertext" not in created and created["key_suffix"] == "1234"
    pg.execute("""UPDATE ai_feature_configs SET rpm_limit=1,tpm_limit=1000,daily_token_budget=1000
                  WHERE feature='offhours_summary'""")
    columns = "api_key_ciphertext,key_updated_at,key_updated_by_authorization_id,rpm_limit,tpm_limit,daily_token_budget"
    before = pg.execute(f"SELECT {columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    saved = save_feature_config(pg, cipher, "offhours_summary", **values, enabled=True, api_key="")
    after = pg.execute(f"SELECT {columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    assert saved["enabled"] and saved["key_suffix"] == "1234" and after == before
    assert not {"rpm_limit", "tpm_limit", "daily_token_budget"}.intersection(saved)


def test_luna_upgrade_preserves_credentials_switches_and_unknown_requests(pg):
    owner, contact, job, request = (uuid4() for _ in range(4))
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,%s,'test')",
               (owner, "ou_" + owner.hex))
    for feature, provider, model, enabled in (
        ("creator_handle_matching", "openai", "gpt-5.6-luna", True),
        ("creator_importance", "openai", "gpt-5.6-luna", False),
        ("desktop_reply", "openai", "custom-model", True),
        ("creator_workflow", "other", "gpt-5.6-luna", False),
    ):
        pg.execute("""INSERT INTO ai_feature_configs(feature,provider,project_id,model)
                      VALUES(%s,%s,'proj_fixture',%s) ON CONFLICT(feature) DO NOTHING""",
                   (feature, provider, model))
        updated = pg.execute("""UPDATE ai_feature_configs SET provider=%s,model=%s,enabled=%s,
            api_key_ciphertext='synthetic-encrypted-key',key_suffix='1234',
            key_updated_at='2026-09-01T12:00:00Z',key_updated_by_authorization_id=%s,
            auto_confirm_threshold=0.950,automatic_queue_checked_at='2026-09-02T12:00:00Z',
            last_tested_at='2026-09-03T12:00:00Z',last_test_status='succeeded'
            WHERE feature=%s""", (provider, model, enabled, owner, feature))
        assert updated.rowcount == 1
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Synthetic creator')", (contact,))
    pg.execute("""INSERT INTO creator_handle_jobs(id,contact_id,status,reason_code)
                  VALUES(%s,%s,'failed','request_unknown')""", (job, contact))
    pg.execute("INSERT INTO creator_handle_requests(id,job_id,error_code) VALUES(%s,%s,'request_unknown')",
               (request, job))
    snapshot_query = "SELECT feature,to_jsonb(c) AS config FROM ai_feature_configs c"
    before = {row["feature"]: row["config"] for row in pg.execute(snapshot_query)}
    history = (
        pg.execute("SELECT * FROM creator_handle_jobs WHERE id=%s", (job,)).fetchone(),
        pg.execute("SELECT * FROM creator_handle_requests WHERE id=%s", (request,)).fetchone(),
    )
    migration = (Path(__file__).parents[1] / "migrations/093_upgrade_luna_model.sql").read_text()
    pg.execute(migration)
    after = {row["feature"]: row["config"] for row in pg.execute(snapshot_query)}
    for feature, old in before.items():
        expected = dict(old)
        if old["provider"] == "openai" and old["model"] == "gpt-5.6-luna":
            expected.update(model="gpt-6-luna", last_tested_at=None, last_test_status="never",
                            updated_at=after[feature]["updated_at"])
        assert after[feature] == expected
    assert history == (
        pg.execute("SELECT * FROM creator_handle_jobs WHERE id=%s", (job,)).fetchone(),
        pg.execute("SELECT * FROM creator_handle_requests WHERE id=%s", (request,)).fetchone(),
    )
    pg.execute(migration)
    assert {row["feature"]: row["config"] for row in pg.execute(snapshot_query)} == after


def test_auto_confirmation_never_allows_a_threshold_below_ninety_percent() -> None:
    with pytest.raises(FeatureConfigInputError, match="0.90"):
        save_feature_config(
            _Connection(),
            api_key_cipher(b"a" * 32),
            "contact_assessment",
            provider="openai",
            project_id="proj_contact_assessment",
            model="gpt-5.6-luna",
            enabled=True,
            api_key="sk-test-value-1234",
            updated_by_authorization_id=uuid4(),
            auto_confirm_threshold="0.899",
        )


def test_get_and_display_never_expose_ciphertext() -> None:
    when = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    row = {
        "feature": "contact_assessment",
        "provider": "openai",
        "project_id": "proj_contact_assessment",
        "model": "gpt-5.6-luna",
        "enabled": True,
        "key_suffix": "1234",
        "key_updated_at": when,
        "last_tested_at": when,
        "last_test_status": "succeeded",
        "api_key_ciphertext": "must-not-leak",
        "rpm_limit": 1,
        "tpm_limit": 60_000,
        "daily_token_budget": 1_000,
    }

    loaded = get_feature_config(_Connection(row), "contact_assessment")
    shown = display_config(row)

    assert loaded is not None
    assert "api_key_ciphertext" not in loaded
    assert "api_key_ciphertext" not in shown
    assert not {"rpm_limit", "tpm_limit", "daily_token_budget"}.intersection(loaded)
    assert not {"rpm_limit", "tpm_limit", "daily_token_budget"}.intersection(shown)
    assert "must-not-leak" not in repr(shown)
    assert shown["api_key_label"] == "已配置 · …1234"
    assert shown["last_test_status_label"] == "通过"
    initial = display_config({"feature": "creator_handle_matching", "project_id": "unconfigured"})
    assert not initial["enabled"] and not initial["key_configured"] and initial["project_id"] == ""
    assert not {"rpm_limit", "tpm_limit", "daily_token_budget"}.intersection(initial)


def test_matching_configuration_rejects_placeholder_project_before_saving():
    conn = _Connection()
    values = dict(provider="openai", project_id="proj_matching", model="gpt-5.6-luna", enabled=True,
                  api_key="sk-test-only-1234", updated_by_authorization_id=uuid4())
    with pytest.raises(FeatureConfigInputError):
        save_feature_config(conn, api_key_cipher(b"k" * 32), "creator_handle_matching",
                            **{**values, "project_id": "unconfigured"})
    assert conn.calls == []
