"""Private image upload/reuse, revocation and reference-aware retention in one workflow."""

import os
from contextlib import contextmanager, nullcontext
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from app import broadcast_images as images
from app import creator_broadcast
from app import creator_broadcast_delivery as delivery
from app.crypto import TokenCipher
from app.lark_api import LarkProfile


def test_private_images_keep_originals_and_only_clean_unused_assets(pg, tmp_path, monkeypatch):
    settings = SimpleNamespace(oauth_scopes=("offline_access", "im:chat:read"),
        token_encryption_key=b"i" * 32, broadcast_image_dir=str(tmp_path / "images"))
    cipher = TokenCipher(settings.token_encryption_key)
    actor, other = uuid4(), uuid4()
    scopes = [*settings.oauth_scopes, *creator_broadcast.SEND_SCOPES, "im:resource"]
    for who in (actor, other):
        pg.execute("""INSERT INTO authorizations(id,open_id,access_token_ciphertext,scopes,
            access_token_expires_at,body_consent_at,broadcast_enabled)
            VALUES(%s,%s,%s,%s,NOW()+INTERVAL '1 hour',NOW(),TRUE)""",
            (who, "ou_" + who.hex, cipher.encrypt(who.hex), scopes))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (other,))
    database_depth, uploaded = 0, []

    @contextmanager
    def connect():
        nonlocal database_depth
        database_depth += 1
        try:
            yield pg
        finally:
            database_depth -= 1

    class Client:
        def __init__(self, received, *, http_client):
            assert received is settings

        def user_info(self, token):
            assert database_depth == 0
            return LarkProfile("ou_" + token, None, None, None)

        def upload_message_image(self, token, content, filename, mime_type):
            assert token == actor.hex and database_depth == 0
            uploaded.append(content)
            return "img_fixture_" + str(len(uploaded))

    for module in (images, delivery):
        monkeypatch.setattr(module, "connection", connect)
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(creator_broadcast, "get_settings", lambda: settings)
    monkeypatch.setattr(images, "LarkClient", Client)
    png, gif = BytesIO(), BytesIO()
    Image.new("RGB", (3, 4), "red").save(png, "PNG")
    Image.new("RGB", (3, 4), "red").save(gif, "GIF", save_all=True,
        append_images=[Image.new("RGB", (3, 4), "blue")], duration=100, loop=0)
    oversize = []
    for format_name, width in (("PNG", 12001), ("GIF", 2001)):
        file = BytesIO()
        Image.new("RGB", (width, 1)).save(file, format_name)
        oversize.append(file.getvalue())
    for content in (b"", b"<svg></svg>", png.getvalue()[:-8], b"x" * (images.MAX_BYTES + 1), *oversize):
        with pytest.raises(ValueError):
            images.validate(content, "spoofed.png")
    first = images.upload(actor, png.getvalue(), "../../photo.png")
    animated = images.upload(actor, gif.getvalue(), "animation.gif")
    unused = images.upload(actor, png.getvalue(), "unused.png")
    assert first["filename"] == "photo.png" and animated["animated"]
    assert "image_key" not in first and "image_key" not in animated
    assert uploaded == [png.getvalue(), gif.getvalue(), png.getvalue()]
    assert images.read(actor, first["id"]) == (png.getvalue(), "image/png")
    assert images.read(actor, animated["id"]) == (gif.getvalue(), "image/gif")
    path = images._path(settings, first["id"])
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert png.getvalue() not in path.read_bytes()
    assert set(images.resolve(pg, actor, [first["id"], animated["id"], first["id"]], settings)) == {
        first["id"], animated["id"]}
    parked = path.with_suffix(".parked")
    path.rename(parked)
    try:
        with pytest.raises(ValueError):
            images.resolve(pg, actor, [first["id"]], settings)
    finally:
        parked.rename(path)
    sealed = path.read_bytes()
    path.write_bytes(bytes([sealed[0] ^ 1]) + sealed[1:])
    try:
        with pytest.raises(ValueError):
            images.read(actor, first["id"])
    finally:
        path.write_bytes(sealed)
    assert len(uploaded) == 3  # Templates and multiple recipients reuse one server-side image key.
    for who in (other,):  # Even Owner cannot access another user's assets.
        with pytest.raises(PermissionError):
            images.read(who, first["id"])
        with pytest.raises(PermissionError):
            images.resolve(pg, who, [first["id"]], settings)
    pg.execute("UPDATE authorizations SET scopes=%s WHERE id=%s", (scopes[:-1], actor))
    assert images.read(actor, first["id"])[0] == png.getvalue()
    with pytest.raises(creator_broadcast.BroadcastAuthorizationRequired) as missing:
        images.resolve(pg, actor, [first["id"]], settings)
    assert missing.value.media
    pg.execute("UPDATE authorizations SET scopes=%s,broadcast_enabled=FALSE WHERE id=%s", (scopes, actor))
    with pytest.raises(PermissionError):
        images.read(actor, first["id"])
    pg.execute("UPDATE authorizations SET broadcast_enabled=TRUE,status='revoked' WHERE id=%s", (actor,))
    with pytest.raises(PermissionError):
        images.resolve(pg, actor, [first["id"]], settings)
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (actor,))
    doc = {"version": 2, "ops": [{"insert": {"image": first["id"]}}]}
    pg.execute("""INSERT INTO creator_broadcast_templates(name,body,body_document,owner_authorization_id,deleted_at)
        VALUES('Image template','',%s,%s,NOW())""", (Jsonb(doc), actor))
    pg.execute("""INSERT INTO creator_broadcast_previews(sender_authorization_id,sender_open_id,sender_name,
        purpose,body_ciphertext,snapshot,fingerprint,expires_at)
        VALUES(%s,%s,'CM','category','encrypted',%s,'fixture',NOW()-INTERVAL '1 day')""",
        (actor, "ou_" + actor.hex, Jsonb({"image_ids": [animated["id"]]})))
    pg.execute("UPDATE creator_broadcast_images SET created_at=NOW()-INTERVAL '8 days'")
    # Inject a newly visible reference after candidate selection; the next statement must retain the asset.
    def concurrent_reference(query, params=None):
        cursor = pg.execute(query, params)
        if query.startswith("SELECT i.id FROM creator_broadcast_images"):
            pg.execute("""INSERT INTO creator_broadcast_templates(name,body,body_document,owner_authorization_id)
                VALUES('Just saved','',%s,%s)""",
                (Jsonb({"version": 2, "ops": [{"insert": {"image": unused["id"]}}]}), actor))
        return cursor

    with pg.transaction(force_rollback=True), monkeypatch.context() as patch:
        proxy = SimpleNamespace(transaction=pg.transaction, execute=concurrent_reference)
        patch.setattr(images, "connection", lambda: nullcontext(proxy))
        assert images.cleanup(settings) == 0
        assert images._path(settings, unused["id"]).exists()
    orphan = images._path(settings, uuid4())
    orphan.write_bytes(b"private orphan")
    os.utime(orphan, (1, 1))
    assert images.cleanup(settings) == 2
    assert path.exists() and images._path(settings, animated["id"]).exists()
    assert not images._path(settings, unused["id"]).exists() and not orphan.exists()
    assert pg.execute("SELECT count(*) AS n FROM creator_broadcast_images").fetchone()["n"] == 2
    assert pg.execute("SELECT body,body_document FROM creator_broadcast_templates").fetchone() == {
        "body": "", "body_document": doc}
    assert len(uploaded) == 3
