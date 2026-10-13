"""Personal broadcast originals: validated bytes, private encryption and reusable Lark keys."""

import hashlib
import hmac
import os
import stat
import warnings
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageSequence, UnidentifiedImageError

from . import creator_access, creator_broadcast
from .config import get_settings
from .db import connection
from .lark_api import LarkClient

MAX_BYTES = 10 * 1024 * 1024
MIME_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
UNREFERENCED_SQL = """NOT EXISTS(SELECT 1 FROM creator_broadcast_templates t
    WHERE t.body_document @> jsonb_build_object('ops',jsonb_build_array(
        jsonb_build_object('insert',jsonb_build_object('image',i.id::text)))))
    AND NOT EXISTS(SELECT 1 FROM creator_broadcast_previews p WHERE p.snapshot->'image_ids' ? i.id::text)"""


def validate(content, filename):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
        raise ValueError("图片须为非空文件，且不超过 10 MB。")
    filename = Path(str(filename or "image").replace("\\", "/")).name
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise ValueError("图片文件名无效。")
    try:
        filename.encode("utf-8")
        with warnings.catch_warnings():
            # Our explicit dimensions/pixel budget permit 12000²; Pillow's lower warning is not the policy.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=list(MIME_TYPES)) as image:
                format_name, (width, height) = image.format, image.size
                maximum = 2000 if format_name == "GIF" else 12000
                if not 0 < width <= maximum or not 0 < height <= maximum:
                    raise ValueError("图片尺寸过大；GIF 每边最多 2000 像素，其他图片最多 12000 像素。")
                image.verify()
            with Image.open(BytesIO(content), formats=list(MIME_TYPES)) as image:
                pixels, frames = 0, 0
                for frame in ImageSequence.Iterator(image):
                    frames += 1
                    pixels += frame.width * frame.height
                    if frames > 2000 or pixels > 144_000_000:
                        raise ValueError("图片帧数或解码尺寸过大，请选择较小的图片。")
                    frame.load()
        return {"filename": filename, "mime_type": MIME_TYPES[format_name], "size_bytes": len(content),
                "width": width, "height": height, "animated": frames > 1,
                "content_sha256": hashlib.sha256(content).hexdigest()}
    except (UnidentifiedImageError, OSError, SyntaxError, UnicodeError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("请选择有效的 JPEG、PNG、WebP 或 GIF 图片。") from exc


def _authorize(conn, actor, settings, *, media=False):
    creator_access.require_broadcast_access(conn, actor)
    row = conn.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
    if not row or row["status"] != "active" or not row["body_consent_at"]:
        raise PermissionError("本人聊天授权已失效，请重新接入。")
    required = set(settings.oauth_scopes) | (set(creator_broadcast.SEND_SCOPES) | {"im:resource"} if media else set())
    if not required.issubset(row["scopes"] or ()):
        raise creator_broadcast.BroadcastAuthorizationRequired(actor, media=media)
    return row


def _same(current, checked):
    if any(current[key] != checked[key] for key in ("open_id", "access_token_ciphertext", "body_consent_at", "scopes")):
        raise PermissionError("授权已更新，请重新上传图片。")


def _directory(settings):
    root = Path(settings.broadcast_image_dir)
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError("Broadcast image storage must be a private absolute directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _path(settings, asset_id):
    return _directory(settings) / (str(UUID(str(asset_id))) + ".bin")


def _cipher(settings):
    return AESGCM(hmac.digest(settings.token_encryption_key, b"broadcast-image-key/v1", "sha256"))


def _aad(actor, asset_id):
    return f"broadcast-image/v1/{actor}/{asset_id}".encode("ascii")


def _metadata(row):
    return {key: row[key] for key in ("filename", "mime_type", "size_bytes", "width", "height", "animated")} | {
        "id": str(row["id"]), "url": f"/creators/broadcast/images/{row['id']}"}


def upload(actor_id, content, filename):
    from .creator_broadcast_delivery import _client_token

    settings, actor = get_settings(), creator_access.parse_id(actor_id)
    with connection() as conn:
        _authorize(conn, actor, settings, media=True)
    metadata = validate(content, filename)
    with httpx.Client(timeout=20.0) as http:
        client = LarkClient(settings, http_client=http)
        checked, token = _client_token(actor, client, settings)
        with connection() as conn:
            _same(_authorize(conn, actor, settings, media=True), checked)
        image_key = client.upload_message_image(token, content, metadata["filename"], metadata["mime_type"])
    if not isinstance(image_key, str) or not 1 <= len(image_key) <= 256:
        raise ValueError("图片上传结果无效，请重试。")
    asset_id, nonce = uuid4(), os.urandom(12)
    with connection() as conn, conn.transaction():
        current = _authorize(conn, actor, settings, media=True)
        _same(current, checked)
        path = _path(settings, asset_id)
        sealed = nonce + _cipher(settings).encrypt(nonce, content, _aad(actor, asset_id))
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as file:
            file.write(sealed)
        # Leave an encrypted orphan on an uncertain DB commit; cleanup removes only old unreferenced files.
        row = conn.execute("""INSERT INTO creator_broadcast_images(id,owner_authorization_id,owner_open_id,
            image_key,filename,mime_type,size_bytes,width,height,animated,content_sha256)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (asset_id, actor, current["open_id"], image_key, *[metadata[key] for key in (
                "filename", "mime_type", "size_bytes", "width", "height", "animated", "content_sha256")])).fetchone()
    return _metadata(row)


def resolve(conn, actor_id, asset_ids, settings):
    if not asset_ids:
        return {}
    actor = creator_access.parse_id(actor_id)
    current = _authorize(conn, actor, settings, media=True)
    ids = list(dict.fromkeys(creator_access.parse_id(value) for value in asset_ids))
    rows = conn.execute("""SELECT * FROM creator_broadcast_images
        WHERE owner_authorization_id=%s AND owner_open_id=%s AND id=ANY(%s::uuid[]) FOR SHARE""",
        (actor, current["open_id"], ids)).fetchall()
    if len(rows) != len(ids):
        raise PermissionError("图片不存在或不属于本人，请重新上传。")
    for row in rows:
        try:
            info = _path(settings, row["id"]).lstat()
        except FileNotFoundError as exc:
            raise ValueError("图片文件已不可用，请重新上传。") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size != row["size_bytes"] + 28:
            raise ValueError("图片文件已不可用，请重新上传。")
    return {str(row["id"]): {**_metadata(row), "image_key": row["image_key"],
                            "content_sha256": row["content_sha256"]} for row in rows}


def read(actor_id, asset_id):
    settings, actor = get_settings(), creator_access.parse_id(actor_id)
    with connection() as conn, conn.transaction():
        current = _authorize(conn, actor, settings)
        row = conn.execute("""SELECT * FROM creator_broadcast_images
            WHERE id=%s AND owner_authorization_id=%s AND owner_open_id=%s FOR SHARE""",
            (creator_access.parse_id(asset_id), actor, current["open_id"])).fetchone()
        if not row:
            raise PermissionError("图片不存在或不属于本人。")
        try:
            with os.fdopen(os.open(_path(settings, row["id"]), os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
                sealed = file.read(MAX_BYTES + 29)
            content = _cipher(settings).decrypt(sealed[:12], sealed[12:], _aad(actor, row["id"]))
        except (FileNotFoundError, InvalidTag) as exc:
            raise ValueError("图片文件已不可用，请重新上传。") from exc
        if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["content_sha256"]:
            raise ValueError("图片文件校验失败。")
        return content, row["mime_type"]


def cleanup(settings, *, age_days=7, limit=100):
    removed = []
    with connection() as conn, conn.transaction():
        rows = conn.execute(f"""SELECT i.id FROM creator_broadcast_images i
            WHERE i.created_at<NOW()-(%s*INTERVAL '1 day')
            AND {UNREFERENCED_SQL}
            ORDER BY i.created_at,i.id FOR UPDATE OF i SKIP LOCKED LIMIT %s""", (age_days, limit)).fetchall()
        for row in rows:
            # Recheck with the new statement snapshot after acquiring the asset lock.
            if conn.execute(f"DELETE FROM creator_broadcast_images i WHERE i.id=%s AND {UNREFERENCED_SQL} "
                            "RETURNING i.id", (row["id"],)).fetchone():
                removed.append(_path(settings, row["id"]))
    for path in removed:
        path.unlink(missing_ok=True)
    root = _directory(settings)
    cutoff = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    with connection() as conn:
        for path in root.iterdir():
            if len(removed) >= limit:
                break
            info = path.lstat()
            if path.suffix != ".bin" or not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            try:
                asset_id = UUID(path.stem)
            except ValueError:
                continue
            if str(asset_id) != path.stem or conn.execute(
                "SELECT 1 FROM creator_broadcast_images WHERE id=%s", (asset_id,)
            ).fetchone():
                continue
            path.unlink(missing_ok=True)
            removed.append(path)
    return len(removed)
