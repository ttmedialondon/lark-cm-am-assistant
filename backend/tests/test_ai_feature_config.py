from contextlib import nullcontext
from datetime import UTC, datetime
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
                "rpm_limit": params[11],
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


def test_save_encrypts_key_and_only_returns_safe_fields() -> None:
    conn = _Connection()
    cipher = api_key_cipher(b"a" * 32)
    api_key = "sk-test-value-1234"

    saved = save_feature_config(
        conn,
        cipher,
        "contact_assessment",
        provider="OpenAI",
        project_id="proj_contact_assessment",
        model="gpt-5.6-luna",
        enabled=True,
        api_key=api_key,
        updated_by_authorization_id=uuid4(),
        rpm_limit="20",
    )

    inserted = conn.calls[-1][1]
    assert inserted[6] != api_key
    assert api_key not in inserted
    assert cipher.decrypt(inserted[6]) == api_key
    assert saved["key_suffix"] == "1234"
    assert "api_key_ciphertext" not in saved
    assert api_key not in repr(saved)
    assert saved["rpm_limit"] == 20
    assert "daily_token_budget" not in saved


def test_blank_key_retains_existing_credential() -> None:
    cipher = api_key_cipher(b"a" * 32)
    previous_key = "sk-existing-value-9876"
    conn = _Connection({"api_key_ciphertext": cipher.encrypt(previous_key), "key_suffix": "9876"})

    save_feature_config(
        conn,
        cipher,
        "contact_assessment",
        provider="openai",
        project_id="proj_contact_assessment",
        model="gpt-5.6-luna",
        enabled=True,
        api_key="",
        updated_by_authorization_id=uuid4(),
    )

    inserted = conn.calls[-1][1]
    assert cipher.decrypt(inserted[6]) == previous_key
    assert inserted[8:10] == (False, False)  # Retaining a key is not a rotation.
    assert conn.row["key_suffix"] == "9876"


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
    save_feature_config(pg, cipher, "offhours_summary", **values, enabled=False, api_key="sk-test-only-1234")
    pg.execute("UPDATE ai_feature_configs SET tpm_limit=1000,daily_token_budget=1000 WHERE feature='offhours_summary'")
    columns = "api_key_ciphertext,key_updated_at,key_updated_by_authorization_id,tpm_limit,daily_token_budget"
    before = pg.execute(f"SELECT {columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    saved = save_feature_config(pg, cipher, "offhours_summary", **values, enabled=True, api_key="")
    after = pg.execute(f"SELECT {columns} FROM ai_feature_configs WHERE feature='offhours_summary'").fetchone()
    assert saved["enabled"] and saved["key_suffix"] == "1234" and after == before
    assert "tpm_limit" not in saved and "daily_token_budget" not in saved


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
        "tpm_limit": 60_000,
        "daily_token_budget": 1_000,
    }

    loaded = get_feature_config(_Connection(row), "contact_assessment")
    shown = display_config(row)

    assert loaded is not None
    assert "api_key_ciphertext" not in loaded
    assert "api_key_ciphertext" not in shown
    assert "tpm_limit" not in loaded and "tpm_limit" not in shown
    assert "daily_token_budget" not in loaded and "daily_token_budget" not in shown
    assert "must-not-leak" not in repr(shown)
    assert shown["api_key_label"] == "已配置 · …1234"
    assert shown["last_test_status_label"] == "通过"
    initial = display_config({"feature": "creator_handle_matching", "project_id": "unconfigured"})
    assert not initial["enabled"] and not initial["key_configured"] and initial["project_id"] == ""
    assert initial["rpm_limit"] == 30
    assert "daily_token_budget" not in initial


def test_matching_configuration_rejects_invalid_limits_and_placeholder_project_before_saving():
    conn = _Connection()
    values = dict(provider="openai", project_id="proj_matching", model="gpt-5.6-luna", enabled=True,
                  api_key="sk-test-only-1234", updated_by_authorization_id=uuid4())
    for changes in ({"rpm_limit": "9" * 5000}, {"rpm_limit": "0"}, {"project_id": "unconfigured"}):
        with pytest.raises(FeatureConfigInputError):
            save_feature_config(conn, api_key_cipher(b"k" * 32), "creator_handle_matching", **{**values, **changes})
    assert conn.calls == []
