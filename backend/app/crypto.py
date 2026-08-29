"""Authenticated encryption for server-side OAuth tokens and short-lived PKCE data."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD = b"lark-message-stats/v1"


class TokenCipher:
    """Encrypt values with AES-256-GCM; the database never receives plaintext tokens."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        sealed = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), _AAD)
        return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")

    def decrypt(self, encoded: str) -> str:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) <= 12:
            raise ValueError("invalid encrypted token")
        return self._cipher.decrypt(raw[:12], raw[12:], _AAD).decode("utf-8")
