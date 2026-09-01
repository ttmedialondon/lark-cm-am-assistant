import pytest
from cryptography.exceptions import InvalidTag

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
