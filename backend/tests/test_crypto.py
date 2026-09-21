import hmac
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag

from app import creator_data
from app.crypto import TokenCipher, message_body_cipher


def test_token_cipher_round_trip_and_nonce_changes() -> None:
    cipher = TokenCipher(b"a" * 32)
    first = cipher.encrypt("refresh-token-value")
    second = cipher.encrypt("refresh-token-value")

    assert first != second
    assert cipher.decrypt(first) == "refresh-token-value"
    assert cipher.decrypt(second) == "refresh-token-value"


def test_message_bodies_use_a_separate_encryption_domain() -> None:
    master = b"a" * 32
    sealed = message_body_cipher(master).encrypt('{"text":"customer message"}')

    assert message_body_cipher(master).decrypt(sealed) == '{"text":"customer message"}'
    with pytest.raises(InvalidTag):
        TokenCipher(master).decrypt(sealed)

    # Refactoring storage must keep both existing ciphertext and its AAD readable.
    legacy_creator = TokenCipher(hmac.digest(master, b"creator-ownership/v1", "sha256"),
                                 b"creator-ownership/data/v1")
    current_creator = creator_data.cipher(SimpleNamespace(token_encryption_key=master))
    assert current_creator.decrypt(legacy_creator.encrypt("历史证据")) == "历史证据"
    assert legacy_creator.decrypt(current_creator.encrypt("new evidence")) == "new evidence"
    with pytest.raises(InvalidTag):
        current_creator.decrypt(sealed)
