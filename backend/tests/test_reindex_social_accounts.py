from scripts import reindex_social_accounts as reindex


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        if "INSERT INTO social_accounts" in query:
            return _Result({"id": "account-id"})
        return _Result()


class _Cipher:
    def decrypt(self, _value):
        return "TikTok account: @Creator.One"


def test_reindex_rows_records_a_raw_account_observation_without_ownership() -> None:
    conn = _Connection()
    stats = reindex.ReindexStats()

    reindex._index_rows(
        conn,
        [
            {
                "body_ciphertext": "encrypted",
                "sender_open_id": "ou_external",
                "chat_id": "oc_chat",
                "message_id": "om_message",
                "observed_by_authorization_id": "authorization-id",
            }
        ],
        _Cipher(),
        stats,
    )

    assert stats.messages_scanned == 1
    assert stats.account_occurrences == 1
    assert stats.unreadable_bodies == 0
    account_call, observation_call = conn.calls
    assert account_call[1][1:] == ("tiktok", "creator.one", "@Creator.One")
    assert observation_call[1][2:7] == (
        "ou_external",
        "oc_chat",
        "om_message",
        "authorization-id",
        "rule:labeled_handle",
    )
    assert "contact_social_accounts" not in observation_call[0]
    assert "ON CONFLICT (social_account_id, chat_id, message_id) DO UPDATE" in observation_call[0]


def test_reindex_query_requires_active_consent_valid_visible_message_scope() -> None:
    query = reindex.READABLE_MESSAGES_SQL

    assert "authorization.status = 'active'" in query
    assert "authorization.body_consent_at IS NOT NULL" in query
    assert "observation.observed_at >= authorization.body_consent_at" in query
    assert "chat_scope.is_visible" in query
    assert "chat_scope.sync_enabled" in query
