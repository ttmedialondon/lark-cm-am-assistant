"""Existing creator evidence storage and shared model config; no workflow decisions."""

import hashlib
import hmac
import json

from .crypto import TokenCipher


def cipher(settings):
    return TokenCipher(
        hmac.digest(settings.token_encryption_key, b"creator-ownership/v1", "sha256"), b"creator-ownership/data/v1"
    )


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def config(conn):
    # Creator matching is explicitly started per batch; saving credentials never starts a scan.
    return conn.execute(
        """SELECT provider,project_id,model,api_key_ciphertext FROM ai_feature_configs
           WHERE feature='creator_import' AND provider='openai' AND api_key_ciphertext IS NOT NULL"""
    ).fetchone()


def config_fingerprint(config):
    return hashlib.sha256(encode({k: config.get(k) for k in ("provider", "project_id", "model")}).encode()).hexdigest()
