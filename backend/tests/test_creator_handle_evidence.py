"""Exact account identity, group-business binding, permission and stale-evidence risks."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app import creator_handle_evidence as handles
from app.crypto import message_body_cipher


@pytest.fixture
def scope(pg, monkeypatch):
    ids = {key: uuid4() for key in ("cm", "owner", "creator", "other")}
    settings = SimpleNamespace(token_encryption_key=b"x" * 32)
    for key in ("cm", "owner"):
        pg.execute(
            "INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
            "VALUES(%s,%s,'test',NOW()-INTERVAL '1 day')",
            (ids[key], "ou_" + key),
        )
    for key in ("cm", "creator", "other"):
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (ids[key], key))
        pg.execute(
            "INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) VALUES(%s,%s,%s,%s)",
            ("ou_" + key, ids[key], key, "internal" if key == "cm" else "known_external"),
        )
    for chat, mode, title in (
        ("group", "group", "creator._123*Trutonic Media"),
        ("private", "p2p", "Name is not a handle"),
    ):
        pg.execute("INSERT INTO chats(chat_id,chat_mode,name) VALUES(%s,%s,%s)", (chat, mode, title))
        for key in ("cm", "owner"):
            pg.execute(
                "INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) VALUES(%s,%s,%s)",
                (ids[key], chat, "ou_creator" if mode == "p2p" else None),
            )
        for key in ("cm", "creator"):
            pg.execute(
                "INSERT INTO chat_members(chat_id,open_id,last_observed_by_authorization_id) VALUES(%s,%s,%s)",
                (chat, "ou_" + key, ids["cm"]),
            )
    number = 0

    def add(sender, text, *, chat="private", observer="cm", parent=None, mention=None, when=None, message_type="text"):
        nonlocal number
        number += 1
        message_id = f"m{number}"
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
            "body_ciphertext,message_create_time,parent_id,mentions_json) "
            "VALUES(%s,%s,%s,'user',%s,%s,%s,%s,%s)",
            (
                chat,
                message_id,
                "ou_" + sender,
                message_type,
                message_body_cipher(settings.token_encryption_key).encrypt(
                    json.dumps(text if isinstance(text, dict) else {"text": text})
                ),
                when or datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=number),
                parent,
                Jsonb([{"key": "@_user_1", "id": "ou_" + mention, "id_type": "open_id"}] if mention else []),
            ),
        )
        pg.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) VALUES(%s,%s,%s)",
            (ids[observer], chat, message_id),
        )
        return message_id

    monkeypatch.setattr(handles, "connection", lambda: nullcontext(pg))
    return ids, settings, add


def _result(pack, *, handle="creator._123", basis="self_declared", labels=None):
    return json.dumps(
        {
            "outcome": "matched",
            "reason": "已核对账号关系",
            "candidates": [
                {
                    "handle": handle,
                    "confidence": 0.99,
                    "basis": basis,
                    "evidence_refs": labels or [r["ref"] for r in [*pack["groups"], *pack["messages"]]],
                    "conflicts": [],
                    "missing_info": [],
                },
            ],
        }
    )


def test_exact_normalization_and_group_patterns_reject_guessing():
    for value in ("@Creator._123", "creator._123", "https://www.tiktok.com/@Creator._123/?lang=en"):
        assert handles.normalize_handle(value) == "creator._123"
    for bad in (
        "Some Person",
        "https://example.com/@creator._123",
        "https://tiktok.com/@a/video/12",
        "a.",
        "a-b",
        "__",
    ):
        with pytest.raises(ValueError):
            handles.normalize_handle(bad)
    assert handles._name_handles("creator._123*Trutonic Media") == ["creator._123"]
    assert handles._name_handles("creator._123。cm1") == ["creator._123"]
    assert handles._name_handles("creator._123 X TT") == ["creator._123"]
    assert handles._name_handles("💙❤️ creator_one1 X TT Media") == ["creator_one1"]
    assert handles._name_handles("❤️creator_two00 & TT media") == ["creator_two00"]
    assert handles._name_handles(" 💙 creator._123＊Trutonic Media") == ["creator._123"]
    assert handles._name_handles("Creator Management Team") == []
    assert handles._name_handles("❤️ Creator Management Team") == []
    assert handles._name_handles("Team creator._123 & TT Media") == []
    assert handles._name_handles("creator._123 & Unknown Agency") == []
    assert handles._name_handles("creator._123 TT Media") == []
    assert handles._clues("BrandForTest X TT please post a video") == []  # Titles are not a body pattern.
    assert handles._clues("@_all @all @_user_1 @U1 @CM1") == []
    for hashtag in ("#TikTokMadeMeBuyIt", "#SummerSale2026FashionDiscount"):
        text = handles._body(hashtag, {"message_type": "text", "mentions_json": []}, {}, {})
        assert "已提供广告授权码" not in text and not handles._OWN_ACTION.search(text)
    for intro in ("I'm Ava Stone (@ava._123)", "I am Ava Stone (@ava._123)", "I’m U1 (@ava._123)"):
        assert handles._self_handles(intro) == {"ava._123"}
    redacted = handles._body(
        "I'm Ava Stone (@ava._123)", {"message_type": "text", "mentions_json": []}, {}, {"Ava Stone": "U1"}
    )
    assert handles._self_handles(redacted) == {"ava._123"}
    for other in (
        "I'm helping (@someone)",
        "I am managing Ava Stone (@someone)",
        "I'm Helping Someone (@someone)",
        "I'm Ava Stone, managing (@someone)",
    ):
        assert not handles._self_handles(other)


def test_self_account_keeps_exact_handle_and_current_scope_not_other_bodies(pg, scope, monkeypatch):
    ids, settings, add = scope
    first = add("creator", "I'm Ava Stone (@creator._123). Email private@example.com, phone +447123456789")
    add("creator", "https://www.tiktok.com/@unverified_link", chat="group")
    add("creator", "My handle is @NOT_AUTHORIZED_SECRET", observer="owner")
    add("other", "PRIVATE_THIRD_PERSON_BODY my account is @someone_else", chat="group")
    material = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert material["eligible"]
    packed = json.dumps(material["pack"])
    assert "creator._123" in packed
    assert "unverified_link" in material["pack"]["allowed_handles"]  # U1 links remain clues, not proof.
    assert not any(
        secret in packed
        for secret in (
            "private@example.com",
            "+447123456789",
            "NOT_AUTHORIZED_SECRET",
            "PRIVATE_THIRD_PERSON_BODY",
            "someone_else",
        )
    )
    assert handles.validate_result(_result(material["pack"]), material["pack"])["outcome"] == "matched"
    assert handles.validate_evidence(
        ids["creator"], ids["cm"], settings, material["pack"], material["refs"], material["fingerprint"]
    )
    with monkeypatch.context() as patch:

        def no_history(*_args):
            raise AssertionError("list checks must not scan full history")

        patch.setattr(handles, "_load", no_history)
        assert handles.validate_evidence(
            ids["creator"],
            ids["cm"],
            settings,
            material["pack"],
            material["refs"],
            material["fingerprint"],
            check_history=False,
        )
    for sql, params in (
        ("UPDATE chats SET name='different_handle X TT' WHERE chat_id='group'", ()),
        ("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (ids["cm"],)),
        ("UPDATE chat_messages SET parent_id='changed' WHERE message_id=%s", (first,)),
        ("UPDATE authorizations SET body_consent_at=NOW()+INTERVAL '1 day' WHERE id=%s", (ids["cm"],)),
    ):
        pg.execute("SAVEPOINT changed")
        pg.execute(sql, params)
        assert not handles.validate_evidence(
            ids["creator"], ids["cm"], settings, material["pack"], material["refs"], material["fingerprint"]
        )
        assert not handles.validate_evidence(
            ids["creator"],
            ids["cm"],
            settings,
            material["pack"],
            material["refs"],
            material["fingerprint"],
            check_history=False,
        )
        pg.execute("ROLLBACK TO SAVEPOINT changed")
        pg.execute("RELEASE SAVEPOINT changed")
    assert not handles.validate_evidence(
        ids["creator"], ids["cm"], settings, material["pack"], material["refs"][:-1], material["fingerprint"]
    )
    with pytest.raises(ValueError):
        handles.validate_result(_result(material["pack"], handle="invented_name"), material["pack"])


def test_group_named_account_requires_actual_own_account_business_not_one_creator_or_at(pg, scope):
    ids, settings, add = scope
    for _ in range(45):
        add("cm", "@_all TOYBRAND offers for your next video and LIVE", chat="group")
    add("cm", "BrandForTest X TT send a video for our promotion", chat="group")
    add("cm", "My handle is @other_brand", chat="group")
    add("cm", "@_user_1 Send your video ad code", chat="group", mention="other")
    cm = add("cm", "@_user_1 Can you send me your video ad code?", chat="group", mention="creator")
    add("creator", "Sounds great I’ll get the ad code now..", chat="group", parent=cm)
    add(
        "creator",
        {
            "en_us": {"content": [[{"tag": "text", "text": "Here is my ad code: SPARKTEST123"}]]},
            "zh_cn": {"content": [[{"tag": "text", "text": "Here is my ad code: SPARKTEST123"}]]},
        },
        chat="group",
        message_type="post",
    )
    material = handles.build_evidence(ids["creator"], ids["cm"], settings)
    packed = json.dumps(material["pack"], ensure_ascii=False)
    assert not any(s in packed for s in ("TOYBRAND", "BrandForTest", "other_brand", "SPARKTEST123"))
    assert packed.count("[已提供广告授权码]") == 1
    assert not material["stats"]["material_incomplete"] and material["stats"]["excluded_group_messages"] == 48
    assert not any(m["handles"] for m in material["pack"]["messages"])  # Only the title supplies the handle.
    assert (
        handles.validate_result(_result(material["pack"], basis="group_business_identity"), material["pack"])["outcome"]
        == "matched"
    )
    promise_refs = [g["ref"] for g in material["pack"]["groups"]] + [
        m["ref"] for m in material["pack"]["messages"] if "已提供广告授权码" not in m["text"]
    ]
    assert (
        handles.validate_result(
            _result(material["pack"], basis="group_business_identity", labels=promise_refs), material["pack"]
        )["outcome"]
        == "matched"
    )
    title_only = [g["ref"] for g in material["pack"]["groups"]]
    assert (
        handles.validate_result(
            _result(material["pack"], basis="group_business_identity", labels=title_only), material["pack"]
        )["outcome"]
        == "review"
    )
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('group','ou_other')")
    other = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert (
        handles.validate_result(_result(other["pack"], basis="group_business_identity"), other["pack"])["outcome"]
        == "review"
    )
    assert not handles.validate_evidence(
        ids["creator"], ids["cm"], settings, material["pack"], material["refs"], material["fingerprint"]
    )
    assert not handles.validate_evidence(
        ids["creator"],
        ids["cm"],
        settings,
        material["pack"],
        material["refs"],
        material["fingerprint"],
        check_history=False,
    )
    pg.execute("DELETE FROM chat_members WHERE open_id='ou_other'")
    pg.execute("DELETE FROM authorization_message_observations")
    pg.execute("DELETE FROM chat_messages")
    cm = add("cm", "@_user_1 Can I apply for a sample for you?", chat="group", mention="creator")
    add("creator", "Okay thanks!", chat="group", parent=cm)
    weak = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert (
        handles.validate_result(_result(weak["pack"], basis="group_business_identity"), weak["pack"])["outcome"]
        == "review"
    )
    add("cm", "Can you send your video ad code?", chat="group")
    add("creator", "Here is my ad code: SPARKTEST456", chat="group")
    paired = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert len(paired["pack"]["messages"]) >= 2  # Same account-topic exchange can be reviewed without an @.


def test_old_rename_and_over_limit_material_cannot_silently_produce_high_confidence(pg, scope):
    ids, settings, add = scope
    add("creator", "My handle is @creator._123")
    material = handles.build_evidence(ids["creator"], ids["cm"], settings)
    add(
        "creator",
        "I changed my username; my old account is @creator._123 and new account is @new._456. " + "x" * 17000,
        when=datetime(2026, 1, 1, tzinfo=UTC),
    )
    current = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert current["stats"]["account_change_signal"] and current["stats"]["material_incomplete"]
    assert current["stats"]["messages"] <= 40 and current["stats"]["body_chars"] <= 16000
    assert handles.validate_result(_result(current["pack"]), current["pack"])["outcome"] == "review"
    assert not handles.validate_evidence(
        ids["creator"], ids["cm"], settings, material["pack"], material["refs"], material["fingerprint"]
    )
    assert handles.validate_evidence(
        ids["creator"],
        ids["cm"],
        settings,
        material["pack"],
        material["refs"],
        material["fingerprint"],
        check_history=False,
    )
    pg.execute("DELETE FROM authorization_message_observations")
    pg.execute("DELETE FROM chat_messages")
    pg.execute("UPDATE chats SET name='Unrelated conversation'")
    empty = handles.build_evidence(ids["creator"], ids["cm"], settings)
    assert not empty["eligible"] and empty["reason_code"] == "no_handle_clue"
