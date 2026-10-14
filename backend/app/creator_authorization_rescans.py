"""Resume authorization-derived creator signals in small, durable worker batches."""

from __future__ import annotations

import logging

import psycopg

from . import creator_responsibility_matching
from .db import connection

LOGGER = logging.getLogger(__name__)
BATCH_SIZE = 10


def prepare_next(_settings):
    job = None
    completed = False
    try:
        with connection() as conn, conn.transaction():
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            candidates = conn.execute("""SELECT * FROM creator_authorization_rescans WHERE not_before<=NOW()
                ORDER BY not_before,requested_at,authorization_id LIMIT 10""").fetchall()
            for candidate in candidates:
                # Serialize workers without holding a queue row or the OAuth material lock while scanning.
                if conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) locked",
                                ("creator-auth-rescan:" + str(candidate["authorization_id"]),)).fetchone()["locked"]:
                    job = conn.execute("""SELECT * FROM creator_authorization_rescans
                        WHERE authorization_id=%s AND not_before<=NOW()""",
                        (candidate["authorization_id"],)).fetchone()
                    if job:
                        break
            if job is None:
                return False
            actor = job["authorization_id"]
            if job["stage"] == "importance":
                people = conn.execute("""SELECT contact_id FROM (
                    SELECT contact_id FROM creator_importance_profiles WHERE responsible_authorization_id=%s
                    UNION SELECT contact_id FROM creator_importance_jobs WHERE responsible_authorization_id=%s
                    UNION SELECT contact_id FROM contact_assignments
                      WHERE responsible_authorization_id=%s AND ended_at IS NULL
                    ) people WHERE (%s::uuid IS NULL OR contact_id>%s)
                    ORDER BY contact_id LIMIT %s""",
                    (actor, actor, actor, job["contact_cursor"], job["contact_cursor"], BATCH_SIZE)).fetchall()
                for person in people:
                    conn.execute("SELECT enqueue_creator_importance(%s,%s,TRUE)", (person["contact_id"], actor))
                stage = "importance" if len(people) == BATCH_SIZE else "chats"
                contact_cursor = people[-1]["contact_id"] if people else job["contact_cursor"]
                conn.execute("""UPDATE creator_authorization_rescans SET stage=%s,contact_cursor=%s,
                    not_before=NOW(),last_error_code=''
                    WHERE authorization_id=%s AND generation=%s AND request_id=%s""",
                    (stage, contact_cursor, actor, job["generation"], job["request_id"]))
            else:
                chats = conn.execute("""SELECT chat_id FROM authorization_chats
                    WHERE authorization_id=%s AND (%s::text IS NULL OR chat_id>%s)
                    ORDER BY chat_id LIMIT %s""",
                    (actor, job["chat_cursor"], job["chat_cursor"], BATCH_SIZE)).fetchall()
                for chat in chats:
                    conn.execute("SELECT creator_handle_chat_signal(%s)", (chat["chat_id"],))
                    conn.execute("SELECT enqueue_creator_group(%s)", (chat["chat_id"],))
                creator_responsibility_matching.touch_chats(
                    conn, [chat["chat_id"] for chat in chats], authorization_id=actor, new_scope=True)
                if len(chats) < BATCH_SIZE:
                    completed = conn.execute(
                        "DELETE FROM creator_authorization_rescans "
                        "WHERE authorization_id=%s AND generation=%s AND request_id=%s",
                        (actor, job["generation"], job["request_id"]),
                    ).rowcount == 1
                else:
                    conn.execute("""UPDATE creator_authorization_rescans SET chat_cursor=%s,
                        not_before=NOW(),last_error_code=''
                        WHERE authorization_id=%s AND generation=%s AND request_id=%s""",
                        (chats[-1]["chat_id"], actor, job["generation"], job["request_id"]))
    except psycopg.Error as error:
        LOGGER.warning("creator_authorization_rescan_failed authorization_id=%s stage=%s sqlstate=%s",
                       job["authorization_id"] if job else None, job["stage"] if job else None, error.sqlstate)
        if job:
            with connection() as conn, conn.transaction():
                conn.execute("SET LOCAL lock_timeout='1s'")
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute("""UPDATE creator_authorization_rescans SET not_before=NOW()+INTERVAL '30 seconds',
                    last_error_code=%s WHERE authorization_id=%s AND generation=%s AND request_id=%s""",
                    (error.sqlstate or "database_unavailable", job["authorization_id"], job["generation"],
                     job["request_id"]))
        return False
    if completed:
        LOGGER.info("creator_authorization_rescan_completed authorization_id=%s generation=%s",
                    job["authorization_id"], job["generation"])
    return True
