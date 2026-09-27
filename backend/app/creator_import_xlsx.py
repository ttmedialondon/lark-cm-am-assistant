"""Read a one-column handle roster without executing spreadsheet content."""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .handle_evidence import normalize_handle

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
HEADERS = {"handle", "handles", "tiktok handle", "tiktok_handle", "达人handle", "达人 handle", "账号"}


def parse_xlsx(filename, content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("请选择不超过 2 MB 的 Excel 文件。")
    if not isinstance(filename, str) or not filename.lower().endswith(".xlsx"):
        raise ValueError("目前只支持 .xlsx 文件，请只保留一列 handle。")
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 300 or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                raise ValueError("Excel 内容过大，请拆成较小的名单。")
            names = {i.filename for i in infos}
            if len(names) != len(infos) or any(".." in PurePosixPath(n).parts for n in names):
                raise ValueError("Excel 文件结构不正确。")
            if any("vbaProject" in n or "externalLinks/" in n for n in names):
                raise ValueError("请上传不含宏或外部数据链接的普通 Excel。")

            def xml(name):
                raw = archive.read(name)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ValueError("Excel 文件含不支持的 XML 内容。")
                return ET.fromstring(raw)

            strings = []
            if "xl/sharedStrings.xml" in names:
                strings = ["".join(si.itertext()) for si in xml("xl/sharedStrings.xml").findall("s:si", NS)]
            all_rows = []
            for name in sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
                sheet_rows = []
                columns = set()
                for row in xml(name).findall("s:sheetData/s:row", NS):
                    populated = []
                    for cell in row.findall("s:c", NS):
                        if cell.find("s:f", NS) is not None:
                            raise ValueError("handle 列不能包含公式，请先粘贴为纯文本。")
                        kind = cell.get("t", "n")
                        value = cell.findtext("s:v", "", NS)
                        if kind == "s":
                            index = int(value)
                            if index < 0:
                                raise ValueError("Excel 文件结构不正确。")
                            value = strings[index]
                        elif kind == "inlineStr":
                            value = "".join(cell.find("s:is", NS).itertext())
                        elif value.strip() and kind not in {"str"}:
                            raise ValueError("请把 handle 列设为文本，避免数字账号丢失开头的 0。")
                        if not value.strip():
                            continue
                        column = "".join(c for c in cell.get("r", "") if c.isalpha())
                        columns.add(column)
                        populated.append(value.strip())
                    if populated:
                        if len(populated) != 1 or len(columns) != 1:
                            raise ValueError("这次只需要一列 handle，负责人请在上传页面选择。")
                        sheet_rows.append((int(row.get("r", len(sheet_rows) + 1)), populated[0]))
                if sheet_rows:
                    if all_rows:
                        raise ValueError("请只保留一个有数据的工作表。")
                    all_rows = sheet_rows
            if not all_rows or len(all_rows) > MAX_ROWS + 1:
                raise ValueError("请上传 1–2000 条 handle。")
            if all_rows[0][1].casefold() in HEADERS:
                all_rows = all_rows[1:]
            if not all_rows or len(all_rows) > MAX_ROWS:
                raise ValueError("请上传 1–2000 条 handle。")
    except (BadZipFile, ET.ParseError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        raise ValueError("无法读取 Excel，请重新保存为普通 .xlsx 文件。") from exc
    rows, seen = [], set()
    for number, value in all_rows:
        try:
            handle = normalize_handle(value) if len(value) <= 320 else None
        except ValueError:
            handle = None
        error = "handle 格式不正确" if not handle else "重复 handle" if handle in seen else ""
        rows.append({"row_number": number, "handle": handle or value[:160], "error": error})
        if handle:
            seen.add(handle)
    counts = {
        "valid": sum(not r["error"] for r in rows),
        "duplicates": sum(r["error"] == "重复 handle" for r in rows),
        "invalid": sum(r["error"] == "handle 格式不正确" for r in rows),
    }
    return {"rows": rows, "counts": counts}
