from app.crypto import TokenCipher


def test_token_cipher_round_trip_and_nonce_changes() -> None:
    cipher = TokenCipher(b"a" * 32)
    first = cipher.encrypt("refresh-token-value")
    second = cipher.encrypt("refresh-token-value")

    assert first != second
    assert cipher.decrypt(first) == "refresh-token-value"
    assert cipher.decrypt(second) == "refresh-token-value"
