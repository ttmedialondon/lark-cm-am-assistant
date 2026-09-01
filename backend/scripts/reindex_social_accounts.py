"""Rebuild raw social-account observations from already readable local messages.

This is deliberately local-only: it makes no Lark or model request.  Run it
after migration 010 when historical encrypted message bodies need indexing.
"""

from __future__ import annotations

import argparse
import binascii
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.creator_rules import extract_creator_claims
from app.crypto import TokenCipher, message_body_cipher
from app.db import connection


DEFAULT_BATCH_SIZE = 250

# Select exactly one currently-valid authorization for each canonical message.
# The schema has one raw observation per account/message, so this is the scope
# recorded alongside the reindexed evidence.
READABLE_MESSAGES_SQL = """
WITH scoped_messages AS (
    SELECT DISTINCT ON (message.chat_id, message.message_id)
        message.chat_id,
        message.message_id,
        message.body_ciphertext,
        message.sender_open_id,
        observation.authorization_id AS observed_by_authorization_id,
        COALESCE(message.message_create_time, message.first_indexed_at) AS sort_time
    FROM chat_messages message
    JOIN authorization_message_observations observation
      ON observation.chat_id = message.chat_id
     AND observation.message_id = message.message_id
    JOIN authorizations authorization ON authorization.id = observation.authorization_id
    JOIN authorization_chats chat_scope
      ON chat_scope.authorization_id = observation.authorization_id
     AND chat_scope.chat_id = message.chat_id
    WHERE message.body_ciphertext IS NOT NULL
      AND NOT message.is_deleted
      AND authorization.status = 'active'
      AND authorization.body_consent_at IS NOT NULL
      AND observation.observed_at >= authorization.body_consent_at
      AND chat_scope.is_visible
      AND chat_scope.sync_enabled
    ORDER BY message.chat_id, message.message_id, observation.observed_at DESC, observation.authorization_id DESC
)
SELECT chat_id, message_id, body_ciphertext, sender_open_id,
       observed_by_authorization_id, sort_time
FROM scoped_messages
WHERE %s::timestamptz IS NULL
   OR (sort_time, chat_id, message_id) > (%s, %s, %s)
ORDER BY sort_time, chat_id, message_id
LIMIT %s
"""


@dataclass
class ReindexStats:
    messages_scanned: int = 0
    account_occurrences: int = 0
    unreadable_bodies: int = 0


def _index_rows(
    conn: Any,
    rows: list[dict[str, Any]],
    cipher: TokenCipher,
    stats: ReindexStats,
) -> None:
    for row in rows:
        stats.messages_scanned += 1
        try:
            body_content = cipher.decrypt(row["body_ciphertext"])
        except (binascii.Error, InvalidTag, UnicodeDecodeError, ValueError):
            stats.unreadable_bodies += 1
            continue

        for claim in extract_creator_claims(body_content):
            account = conn.execute(
                """
                INSERT INTO social_accounts (id, platform, normalized_handle, display_handle)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (platform, normalized_handle) DO UPDATE SET
                    display_handle = EXCLUDED.display_handle,
                    updated_at = NOW()
                RETURNING id
                """,
                (uuid.uuid4(), claim.platform, claim.normalized_value, claim.display_value),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO social_account_observations (
                    id, social_account_id, source_open_id, chat_id, message_id,
                    observed_by_authorization_id, extractor, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (social_account_id, chat_id, message_id) DO UPDATE SET
                    source_open_id = COALESCE(EXCLUDED.source_open_id, social_account_observations.source_open_id),
                    observed_by_authorization_id = EXCLUDED.observed_by_authorization_id,
                    extractor = EXCLUDED.extractor,
                    metadata = EXCLUDED.metadata,
                    last_observed_at = NOW()
                """,
                (
                    uuid.uuid4(),
                    account["id"],
                    row["sender_open_id"],
                    row["chat_id"],
                    row["message_id"],
                    row["observed_by_authorization_id"],
                    f"rule:{claim.source}",
                    json.dumps({"extractor": "historical-reindex-v1", "rule": claim.source}),
                ),
            )
            stats.account_occurrences += 1


def reindex(conn: Any, cipher: TokenCipher, batch_size: int) -> ReindexStats:
    """Index all locally stored bodies that remain readable under an active grant."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    stats = ReindexStats()
    cursor: tuple[datetime, str, str] | None = None
    while True:
        params = (
            cursor[0] if cursor else None,
            cursor[0] if cursor else None,
            cursor[1] if cursor else None,
            cursor[2] if cursor else None,
            batch_size,
        )
        with conn.transaction():
            rows = list(conn.execute(READABLE_MESSAGES_SQL, params))
            if not rows:
                break
            _index_rows(conn, rows, cipher, stats)
        last = rows[-1]
        cursor = (last["sort_time"], last["chat_id"], last["message_id"])
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    settings = get_settings()
    with connection() as conn:
        stats = reindex(conn, message_body_cipher(settings.token_encryption_key), args.batch_size)
    print(
        "Reindexed "
        f"{stats.messages_scanned} readable messages; found {stats.account_occurrences} account occurrences; "
        f"skipped {stats.unreadable_bodies} unreadable encrypted bodies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
