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
from .message_mentions import mention_names


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
    sheet.auto_filter.ref = f"A1:C{row_count}"
    with TemporaryFile() as output:
        book.save(output)
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


def stream_export(source_id, viewer_id, check_access):
    cipher = message_body_cipher(get_settings().token_encryption_key)
    with connection() as conn, conn.transaction(), ZipBuffer() as output:
        # XLSX is already compressed. Recompressing it adds CPU cost without useful savings.
        with ZipFile(output, "w", ZIP_STORED) as archive:
            yield from _write_export(conn, archive, output, source_id, viewer_id, cipher, check_access)
        yield from output.drain()


def _write_export(conn, archive, output, source_id, viewer_id, cipher, check_access):
    message_count = conversation_count = 0
    with conn.cursor(name="message_export") as cursor:
        cursor.execute(
            f"""
                WITH conversations AS ({reporting.MESSAGE_CONVERSATION_DIRECTORY_SQL})
                SELECT m.*, c.name, c.chat_mode, c.counterpart_display_name,
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
            name = first.get("counterpart_display_name") if first["chat_mode"] == "p2p" else first["name"]
            prefix = "私聊" if first["chat_mode"] == "p2p" else "群聊"
            conversation_count += 1
            filename = f"{prefix}_{safe_name(name or first['name'])}_{conversation_count}.xlsx"

            def text_rows(source):
                while batch := list(islice(source, 500)):
                    # Exports are long reads; holding identity locks blocks sync and can deadlock.
                    names = mention_names(conn, batch, lock=False)
                    for row in batch:
                        raw = cipher.decrypt(row["body_ciphertext"])
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            text = raw
                        else:
                            text = "\n".join(reporting._body_text_values(parsed, row.get("mentions_json"), names))
                        text = reporting.resolve_mentions(text, row.get("mentions_json"), names)
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
            message_count += write_conversation(archive, filename, text_rows(source_rows))
            check_access()
            yield from output.drain()
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
