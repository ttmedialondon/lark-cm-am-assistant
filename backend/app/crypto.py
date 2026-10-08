"""Authenticated encryption for server-side tokens and message bodies."""

from __future__ import annotations

import base64
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_TOKEN_AAD = b"lark-message-stats/v1"
_MESSAGE_BODY_AAD = b"lark-message-stats/message-body/v1"


class TokenCipher:
    """Encrypt one data class with AES-256-GCM."""

    def __init__(self, key: bytes, aad: bytes = _TOKEN_AAD) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)
        self._aad = aad

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        sealed = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), self._aad)
        return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")

    def decrypt(self, encoded: str) -> str:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) <= 12:
            raise ValueError("invalid encrypted token")
        return self._cipher.decrypt(raw[:12], raw[12:], self._aad).decode("utf-8")


def message_body_cipher(master_key: bytes) -> TokenCipher:
    """Derive a separate encryption domain without adding another deployed secret."""

    body_key = hmac.digest(master_key, b"lark-message-stats/message-body-key/v1", "sha256")
    return TokenCipher(body_key, _MESSAGE_BODY_AAD)


def message_body_fingerprint(master_key: bytes, plaintext: str) -> str:
    """Stable, keyed content identity; fresh encryption nonces are not message edits."""
    key = hmac.digest(master_key, b"lark-message-stats/message-fingerprint/v1", "sha256")
    return hmac.digest(key, plaintext.encode("utf-8"), "sha256").hex()
