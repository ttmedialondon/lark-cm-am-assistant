"""Fill missing Lark sender names without decrypting or changing message bodies.

The script is idempotent: it only fills blank identity/contact display names and
can be rerun after an interruption. It scans only active, visible conversations
that already contain an unnamed sender under that user's authorization.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import connection  # noqa: E402
from app.lark_api import LarkApiError, LarkClient  # noqa: E402
from app.worker import (  # noqa: E402
    ReauthorizationRequired,
    _authorization,
    _continuation_token,
    _has_more,
    _internal_tenant_keys,
    _is_author_message,
    _items,
    _record_message_sender_identity,
    _refresh_tenant_classifications,
    _usable_access_token,
)

TARGET_CHATS_SQL = """
SELECT DISTINCT ac.authorization_id, ac.chat_id
FROM authorization_chats ac
JOIN authorizations auth ON auth.id = ac.authorization_id
JOIN chats chat ON chat.chat_id = ac.chat_id
JOIN authorization_message_observations observation
  ON observation.authorization_id = ac.authorization_id
 AND observation.chat_id = ac.chat_id
JOIN chat_messages message
  ON message.chat_id = observation.chat_id AND message.message_id = observation.message_id
JOIN lark_identities identity ON identity.open_id = message.sender_open_id
WHERE auth.status = 'active'
  AND ac.is_visible
  AND ac.sync_enabled
  AND COALESCE(chat.chat_status, 'normal') = 'normal'
  AND identity.identity_type = 'user'
  AND COALESCE(BTRIM(identity.display_name), '') = ''
  AND NOT message.is_deleted
ORDER BY ac.authorization_id, ac.chat_id
"""


@dataclass
class Stats:
    target_chats: int = 0
    completed_chats: int = 0
    failed_chats: int = 0
    skipped_authorizations: int = 0
    pages_scanned: int = 0
    messages_seen: int = 0
    sender_names_returned: int = 0


def _targets() -> list[dict[str, Any]]:
    with connection() as conn:
        return list(conn.execute(TARGET_CHATS_SQL))


def _message_page(
    client: LarkClient, access_token: str, chat_id: str, page_token: str | None
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            return client.list_chat_messages(access_token, chat_id, page_token=page_token)
        except LarkApiError as exc:
            if not exc.retryable or attempt == 2:
                raise
            time.sleep(exc.retry_after or 2**attempt)
    raise RuntimeError("unreachable")


def _store_sender_identities(authorization: dict[str, Any], messages: list[dict[str, Any]]) -> tuple[int, int]:
    for attempt in range(3):
        try:
            sender_names_returned = 0
            with connection() as conn, conn.transaction():
                internal_tenant_keys = _internal_tenant_keys(conn)
                for message in messages:
                    metadata = _record_message_sender_identity(
                        conn,
                        message,
                        is_own=_is_author_message(message, authorization["open_id"]),
                        internal_tenant_keys=internal_tenant_keys,
                    )
                    sender_names_returned += int(bool(metadata[-1]))
                _refresh_tenant_classifications(conn, internal_tenant_keys)
            return len(messages), sender_names_returned
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def _sync_chat(
    client: LarkClient, authorization: dict[str, Any], access_token: str, chat_id: str, stats: Stats
) -> None:
    page_token: str | None = None
    while True:
        data = _message_page(client, access_token, chat_id, page_token)
        messages = _items(data)
        messages_seen, sender_names_returned = _store_sender_identities(authorization, messages)
        stats.messages_seen += messages_seen
        stats.sender_names_returned += sender_names_returned
        stats.pages_scanned += 1
        if not _has_more(data):
            return
        page_token = _continuation_token(data)


def main() -> int:
    settings = get_settings()
    if not settings.app_secret_configured:
        print("Lark App Secret is not configured.")
        return 1

    targets = _targets()
    if not targets:
        print("No unnamed message senders need enrichment.")
        return 0

    stats = Stats(target_chats=len(targets))
    client = LarkClient(settings)
    tokens: dict[object, str | None] = {}
    skipped: set[object] = set()
    for target in targets:
        authorization_id = target["authorization_id"]
        if authorization_id in skipped:
            continue
        if authorization_id not in tokens:
            authorization = _authorization(authorization_id)
            try:
                tokens[authorization_id] = (
                    _usable_access_token(authorization_id, authorization, client, settings) if authorization else None
                )
            except ReauthorizationRequired:
                tokens[authorization_id] = None
            if not tokens[authorization_id]:
                skipped.add(authorization_id)
                stats.skipped_authorizations += 1
                continue

        authorization = _authorization(authorization_id)
        if not authorization:
            skipped.add(authorization_id)
            stats.skipped_authorizations += 1
            continue
        try:
            _sync_chat(client, authorization, tokens[authorization_id] or "", target["chat_id"], stats)
            stats.completed_chats += 1
        except LarkApiError:
            stats.failed_chats += 1

    print(
        "Sender-name enrichment: "
        f"{stats.completed_chats}/{stats.target_chats} chats completed, "
        f"{stats.pages_scanned} pages, {stats.messages_seen} messages, "
        f"{stats.sender_names_returned} sender names returned; "
        f"{stats.failed_chats} chats failed, {stats.skipped_authorizations} authorizations need reauthorization."
    )
    return 1 if stats.failed_chats or stats.skipped_authorizations else 0


if __name__ == "__main__":
    raise SystemExit(main())
