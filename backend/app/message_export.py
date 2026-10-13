"""Export consented text messages, one workbook per conversation."""

import json
import re
import shutil
from io import RawIOBase
from itertools import chain, groupby, islice
from tempfile import TemporaryFile
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment

from . import admin_reporting as reporting
from .config import get_settings
from .crypto import message_body_cipher
from .db import connection
from .message_mentions import mention_names, resolve_mentions
from .message_text import body_text_values

_MESSAGE_VERSION_SQL = """md5(jsonb_build_array(
    COALESCE(m.body_fingerprint,m.body_ciphertext),m.message_type,m.message_create_time,
    m.sender_open_id,m.mentions_json)::text)"""
# cmin detects same-transaction edits; content hashes allow unchanged sync reobservations.
_ROW_VERSION_SQL = "m.xmin::text || ':' || m.cmin::text"


def has_exportable_messages(source_id):
    with connection() as conn:
        return conn.execute(
            f"""WITH conversations AS ({reporting.MESSAGE_CONVERSATION_DIRECTORY_SQL})
                SELECT EXISTS(
                    SELECT 1 FROM conversations c
                    JOIN authorization_message_observations o
                      ON o.authorization_id=c.authorization_id AND o.chat_id=c.chat_id
                    JOIN chat_messages m ON m.chat_id=o.chat_id AND m.message_id=o.message_id
                    WHERE o.observed_at>=c.body_consent_at AND NOT m.is_deleted
                      AND m.body_ciphertext IS NOT NULL AND m.message_type IN ('text','post')
                ) AS available""", (source_id,),
        ).fetchone()["available"]


def safe_name(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value or "未命名").strip(" .")[:70] or "未命名"


def write_conversation(archive, filename, rows):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("聊天记录")
    sheet.freeze_panes = "A2"
    for column, width in (("A", 28), ("B", 25), ("C", 100)):
        sheet.column_dimensions[column].width = width
    sheet.append(["发送时间（英国当地时间）", "发送人", "消息内容"])
    count = 0
    row_count = 1
    try:
        for timestamp, sender, text in rows:
            # Excel limits cells to 32767 characters; continue long messages without truncation.
            for start in range(0, len(text), 32000):
                if row_count >= 1_048_576:
                    raise ValueError("单个会话超过 Excel 行数限制，请联系项目负责人。")
                cells = []
                for value in (timestamp, sender, text[start : start + 32000]):
                    cell = WriteOnlyCell(sheet, ILLEGAL_CHARACTERS_RE.sub("", str(value)))
                    cell.data_type = "s"  # Message text must never execute as an Excel formula.
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    cells.append(cell)
                sheet.append(cells)
                row_count += 1
            count += 1
    except BaseException:
        # Saving disposes Openpyxl's temporary XML after a mid-generation access failure.
        with TemporaryFile() as abandoned:
            book.save(abandoned)
        raise
    sheet.auto_filter.ref = f"A1:C{row_count}"
    with TemporaryFile() as output:
        book.save(output)
        if not count:
            return 0
        output.seek(0)
        with archive.open(filename, "w", force_zip64=True) as destination:
            shutil.copyfileobj(output, destination)
    return count


class ZipBuffer(RawIOBase):
    """Unseekable ZIP sink, drained in bounded chunks after each workbook."""

    def __init__(self):
        self.buffer = TemporaryFile()
        self.position = 0

    def writable(self):
        return True

    def tell(self):
        return self.position

    def write(self, data):
        self.position += len(data)
        return self.buffer.write(data)

    def drain(self):
        self.buffer.seek(0)
        while chunk := self.buffer.read(65536):
            yield chunk
        self.buffer.seek(0)
        self.buffer.truncate()

    def close(self):
        self.buffer.close()
        super().close()


def stream_export(source_id, viewer_id, check_access, *, consent_at=None):
    cipher = message_body_cipher(get_settings().token_encryption_key)
    with connection() as conn, conn.transaction(), ZipBuffer() as output:
        # XLSX is already compressed. Recompressing it adds CPU cost without useful savings.
        with ZipFile(output, "w", ZIP_STORED) as archive:
            validate = yield from _write_export(conn, archive, output, source_id, viewer_id, cipher,
                                              check_access, consent_at)
        for chunk in output.drain():
            validate()
            yield chunk


def _write_export(conn, archive, output, source_id, viewer_id, cipher, check_access, consent_at):
    message_count = conversation_count = 0
    source_consent = consent_at
    analyzed_count = 0
    conn.execute("""CREATE TEMP TABLE IF NOT EXISTS message_export_versions(
        chat_id text NOT NULL,message_id text NOT NULL,row_version text NOT NULL,version text NOT NULL,
        PRIMARY KEY(chat_id,message_id)) ON COMMIT DROP""")
    conn.execute("""CREATE TEMP TABLE IF NOT EXISTS message_export_chats(
        chat_id text PRIMARY KEY,sync_generation bigint NOT NULL) ON COMMIT DROP""")
    conn.execute("TRUNCATE message_export_versions")
    conn.execute("TRUNCATE message_export_chats")

    def validate(message_ids=None, chat_id=None):
        check_access()
        version_filter = "v.chat_id=%s AND v.message_id=ANY(%s::text[]) AND " if message_ids is not None else ""
        args = [source_id, source_consent, source_id]
        if message_ids is not None:
            args.extend((chat_id, message_ids))
        current = conn.execute(
            f"""SELECT 1 FROM authorizations a WHERE a.id=%s AND a.status='active'
                AND a.body_consent_at=%s AND {reporting.BODY_READ_SCOPE_SQL}
                AND NOT EXISTS(
                    SELECT 1 FROM message_export_chats saved
                    LEFT JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=saved.chat_id
                    LEFT JOIN chats c ON c.chat_id=saved.chat_id
                    WHERE ac.chat_id IS NULL OR NOT ac.is_visible OR NOT ac.sync_enabled
                      OR ac.sync_status='not_readable' OR COALESCE(c.chat_status,'normal')<>'normal'
                      OR ac.sync_generation IS DISTINCT FROM saved.sync_generation)
                AND NOT EXISTS(
                    SELECT 1 FROM message_export_versions v
                    LEFT JOIN chat_messages m ON m.chat_id=v.chat_id AND m.message_id=v.message_id
                    LEFT JOIN authorization_message_observations o
                      ON o.authorization_id=%s AND o.chat_id=v.chat_id AND o.message_id=v.message_id
                    WHERE {version_filter}(m.message_id IS NULL OR m.is_deleted OR m.body_ciphertext IS NULL
                      OR o.observed_at IS NULL OR o.observed_at<a.body_consent_at
                      OR ({_ROW_VERSION_SQL} IS DISTINCT FROM v.row_version
                          AND {_MESSAGE_VERSION_SQL} IS DISTINCT FROM v.version)))""", args,
        ).fetchone()
        if not current:
            raise PermissionError("会话授权或消息内容已变化，请重新导出。")

    with conn.cursor(name="message_export") as cursor:
        cursor.execute(
            f"""
                WITH conversations AS ({reporting.MESSAGE_CONVERSATION_DIRECTORY_SQL})
                SELECT m.*, {_MESSAGE_VERSION_SQL} AS export_version, {_ROW_VERSION_SQL} AS row_version,
                       c.name, c.chat_mode, c.counterpart_display_name,
                       c.body_consent_at, c.sync_generation,
                       identity.display_name AS sender_display_name
                FROM conversations c
                JOIN authorization_message_observations o
                  ON o.authorization_id=c.authorization_id AND o.chat_id=c.chat_id
                JOIN chat_messages m ON m.chat_id=o.chat_id AND m.message_id=o.message_id
                LEFT JOIN lark_identities identity ON identity.open_id=m.sender_open_id
                WHERE o.observed_at >= c.body_consent_at AND NOT m.is_deleted
                  AND m.body_ciphertext IS NOT NULL AND m.message_type IN ('text','post')
                ORDER BY m.chat_id, m.message_create_time ASC NULLS LAST, m.message_id
            """,
            (source_id,),
        )
        for _chat_id, messages in groupby(cursor, key=lambda row: row["chat_id"]):
            first = next(messages)
            if source_consent is None:
                source_consent = first["body_consent_at"]
            conn.execute("INSERT INTO message_export_chats VALUES(%s,%s)",
                         (first["chat_id"], first["sync_generation"]))
            validate()
            name = first.get("counterpart_display_name") if first["chat_mode"] == "p2p" else first["name"]
            prefix = "私聊" if first["chat_mode"] == "p2p" else "群聊"
            filename = f"{prefix}_{safe_name(name or first['name'])}_{conversation_count + 1}.xlsx"

            def text_rows(source, chat_id=first["chat_id"]):
                while batch := list(islice(source, 500)):
                    with conn.cursor() as versions:
                        versions.executemany(
                            "INSERT INTO message_export_versions VALUES(%s,%s,%s,%s)",
                            [(row["chat_id"], row["message_id"], row["row_version"], row["export_version"])
                             for row in batch],
                        )
                    validate([row["message_id"] for row in batch], chat_id)
                    # Exports are long reads; holding identity locks blocks sync and can deadlock.
                    names = mention_names(conn, batch, lock=False)
                    for row in batch:
                        raw = cipher.decrypt(row["body_ciphertext"])
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            text = raw
                        else:
                            text = "\n".join(body_text_values(parsed, row.get("mentions_json"), names))
                        text = resolve_mentions(text, row.get("mentions_json"), names)
                        if not text.strip():
                            continue
                        timestamp = row["message_create_time"]
                        yield (
                            timestamp.astimezone(reporting.LONDON_TIME_ZONE).isoformat(" ", timespec="seconds")
                            if timestamp
                            else "",
                            row["sender_display_name"] or "未知发送人",
                            text,
                        )

            # The first row was consumed only to name the workbook; continue the same iterator.
            source_rows = chain((first,), messages)  # noqa: B031
            written = write_conversation(archive, filename, text_rows(source_rows))
            message_count += written
            conversation_count += bool(written)
            if message_count >= max(1, analyzed_count * 2):
                conn.execute("ANALYZE message_export_versions")
                analyzed_count = message_count
            for chunk in output.drain():
                validate()
                yield chunk
    if not message_count:
        raise ValueError("该人员暂无已同步且可导出的文字消息。")
    conn.execute(
        """INSERT INTO audit_events (id, authorization_id, action, metadata)
                        VALUES (%s,%s,'admin_messages_exported',%s::jsonb)""",
        (
            uuid4(),
            viewer_id,
            json.dumps(
                {
                    "target_authorization_id": str(source_id),
                    "message_count": message_count,
                    "conversation_count": conversation_count,
                }
            ),
        ),
    )
    return validate
