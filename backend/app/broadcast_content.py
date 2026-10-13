"""The restricted Quill insert-only document stored and sent by broadcast."""

import json
import re
from urllib.parse import urlsplit
from uuid import UUID

STYLES = {"bold": "bold", "italic": "italic", "underline": "underline", "strike": "lineThrough"}


def normalize(value):
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError("消息最多 4,000 字符。")
        value = {"version": 2, "ops": [{"insert": value + ("" if value.endswith("\n") else "\n")} ]}
    if (not isinstance(value, dict) or set(value) != {"version", "ops"} or value["version"] != 2
            or isinstance(value["version"], bool) or not isinstance(value["ops"], list)
            or not 1 <= len(value["ops"]) <= 8000):
        raise ValueError("消息格式无效。")
    ops, text, images = [], "", 0
    for item in value["ops"]:
        if not isinstance(item, dict) or not {"insert"} <= set(item) <= {"insert", "attributes"}:
            raise ValueError("消息格式无效。")
        insert, attrs = item["insert"], item.get("attributes", {})
        if not isinstance(attrs, dict) or not set(attrs) <= {*STYLES, "link", "list"}:
            raise ValueError("消息包含不支持的格式。")
        attributes = {}
        for key in STYLES:
            if key in attrs and not isinstance(attrs[key], bool):
                raise ValueError("文字样式无效。")
            if attrs.get(key):
                attributes[key] = True
        if "link" in attrs:
            link = attrs["link"]
            if not isinstance(link, str) or not 1 <= len(link) <= 2048 or re.search(r"[\x00-\x20\x7f]", link):
                raise ValueError("请使用有效的 http 或 https 链接。")
            try:
                parsed = urlsplit(link)
                valid = (parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                         and not parsed.username and not parsed.password)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("请使用有效的 http 或 https 链接。")
            attributes["link"] = link
        if "list" in attrs:
            if (attrs["list"] not in ("ordered", "bullet") or not isinstance(insert, str)
                    or not insert or insert.strip("\n")):
                raise ValueError("仅支持简单列表。")
            attributes["list"] = attrs["list"]
        if isinstance(insert, dict):
            if set(insert) != {"image"} or attrs:
                raise ValueError("图片格式无效。")
            try:
                insert = {"image": str(UUID(str(insert["image"])))}
            except (ValueError, TypeError, AttributeError):
                raise ValueError("图片标识无效，请重新上传。") from None
            images += 1
            if images > 5:
                raise ValueError("每条消息最多 5 张图片。")
        elif isinstance(insert, str):
            if "\x00" in insert:
                raise ValueError("消息包含无效字符。")
            text += insert
            if len(text) > 4001:
                raise ValueError("消息最多 4,000 字符。")
            if not insert:
                continue
        else:
            raise ValueError("消息内容无效。")
        op = {"insert": insert}
        if attributes:
            op["attributes"] = attributes
        if (ops and isinstance(insert, str) and isinstance(ops[-1]["insert"], str)
                and ops[-1].get("attributes", {}) == attributes):
            ops[-1]["insert"] += insert
        else:
            ops.append(op)
    if len(text.removesuffix("\n")) > 4000 or not text.strip() and not images:
        raise ValueError("请填写消息或添加图片，文字最多 4,000 字符。")
    document = {"version": 2, "ops": ops}
    try:
        encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("消息包含无效字符。") from None
    if len(encoded) > 250000:
        raise ValueError("消息格式过多，请清除部分格式。")
    return document


def image_ids(document):
    return list(dict.fromkeys(op["insert"]["image"] for op in document["ops"] if isinstance(op["insert"], dict)))


def plain_text(document):
    return "".join(op["insert"] for op in document["ops"] if isinstance(op["insert"], str)).removesuffix("\n")


def post_rows(document, image_keys):
    rows, current, ordered = [], [], 0
    for op in document["ops"]:
        value, attrs = op["insert"], op.get("attributes", {})
        if isinstance(value, dict):
            if current:
                rows.append(current)
                current = []
            key = image_keys.get(value["image"])
            if not isinstance(key, str) or not re.fullmatch(r"img_[A-Za-z0-9_-]{1,256}", key):
                raise ValueError("图片无法读取，请重新上传。")
            rows.append([{"tag": "img", "image_key": key}])
            continue
        pieces = value.split("\n")
        for index, piece in enumerate(pieces):
            if piece:
                node = ({"tag": "a", "text": piece, "href": attrs["link"]} if attrs.get("link")
                        else {"tag": "text", "text": piece})
                styles = [lark for key, lark in STYLES.items() if attrs.get(key)]
                if styles:
                    node["style"] = styles
                current.append(node)
            if index < len(pieces) - 1:
                if attrs.get("list"):
                    ordered = ordered + 1 if attrs["list"] == "ordered" else 0
                    current.insert(0, {"tag": "text", "text": f"{ordered}. " if ordered else "• "})
                else:
                    ordered = 0
                rows.append(current or [{"tag": "text", "text": ""}])
                current = []
    if current:
        rows.append(current)
    return rows
