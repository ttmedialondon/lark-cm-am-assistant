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


def test_import_group_identity_evidence_preserves_scope_and_rechecks_changes(pg, scope):
    from app import creator_import_evidence as imports

    ids, settings, add = scope
    own_id = add("creator", "I create my own product videos and stream LIVE every week.", chat="group")
    add("other", "HIDDEN_IMPORT_SECRET I create videos", observer="owner")
    material = imports.build_evidence(pg, settings, ids["cm"], "@Creator._123")
    assert material["eligible"] and material["stats"]["users"] == 1
    packed = json.dumps(material["pack"])
    assert "HIDDEN_IMPORT_SECRET" not in packed and "ou_creator" not in packed and str(ids["creator"]) not in packed
    assert imports.resolve_contact(material["refs"], "U1") == str(ids["creator"])
    result = {"groups": [{"group_ref": g["ref"], "match": "matched", "confidence": 1,
                          "basis": "explicit_handle", "evidence_refs": [g["ref"]]}
                         for g in material["pack"]["groups"]],
              "people": [{"user_ref": "U1", "role": "creator", "confidence": .99,
                          "basis": "self_declaration", "evidence_refs": [material["pack"]["messages"][0]["ref"]]}]}
    assert imports.validate_result(json.dumps(result), material["pack"])["outcome"] == "matched"
    result["people"][0]["user_ref"] = "U999"
    with pytest.raises(ValueError):
        imports.validate_result(json.dumps(result), material["pack"])

    def current():
        return imports.validate_evidence(pg, settings, ids["cm"], "creator._123", material["pack"],
                                         material["refs"], material["fingerprint"])

    assert current()
    pg.execute("UPDATE chats SET name='An unrelated new chat name' WHERE chat_id='private'")
    assert current()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id=%s", (own_id,))
        assert not current()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s AND chat_id='group'",
                   (ids["cm"],))
        assert not current()
    with pg.transaction(force_rollback=True):
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                   "VALUES(%s,'merchant_contact','confirmed','manual',TRUE)", (ids["creator"],))
        assert not current()
        protected = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
        assert not protected["eligible"] and protected["checked"]["user_ref"] is None
    # A private handle declaration cannot substitute for a matching group.
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE chats SET name='Business discussion' WHERE chat_id='group'")
        add("creator", "My account is @creator._123", chat="private")
        missing = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
        assert not missing["eligible"] and missing["checked"]["user_ref"] is None

    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'creator','confirmed','manual',TRUE)", (ids["creator"],))
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    known = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
    assert not known["eligible"] and known["checked"]["outcome"] == "matched"
    assert known["pack"]["messages"] == []
    pg.execute("INSERT INTO chats(chat_id,chat_mode,name) VALUES('second','group','creator._123。cm1')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'second')", (ids["cm"],))
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('second','ou_creator'),('second','ou_other')")
    assert imports.build_evidence(pg, settings, ids["cm"], "creator._123")["checked"]["creator_count"] == 1
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'creator','confirmed','manual',TRUE)", (ids["other"],))
    multiple = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
    assert multiple["stats"]["groups"] == 2 and multiple["checked"]["creator_count"] == 2
    assert not multiple["eligible"] and multiple["checked"]["user_ref"] is None

    # Every exact group is authoritative input; an AI response cannot drop a second creator's group.
    forced = {"groups": [{"group_ref": g["ref"], "match": "matched", "confidence": 1,
                          "basis": "explicit_handle", "evidence_refs": [g["ref"]]}
                         for g in multiple["pack"]["groups"]],
              "people": [{"user_ref": u["ref"], "role": "creator", "confidence": 1,
                          "basis": "weak_context", "evidence_refs": []} for u in multiple["pack"]["users"]]}
    forced["groups"].pop()
    with pytest.raises(ValueError):
        imports.validate_result(json.dumps(forced), multiple["pack"])


def test_import_exact_handle_boundaries_preserve_dots_and_underscores(pg, scope):
    from app import creator_import_evidence as imports

    ids, settings, _ = scope
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'creator','confirmed','manual',TRUE)", (ids["creator"],))
    for index, name in enumerate((
        "prefixcreator._123 X TT", "creator._123suffix X TT", "creator_123 X TT", "creator.123 X TT",
    )):
        chat = f"lookalike_{index}"
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'group')", (chat, name))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s)", (ids["cm"], chat))
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,'ou_creator')", (chat,))
    material = imports.build_evidence(pg, settings, ids["cm"], "@Creator._123")
    assert material["checked"]["outcome"] == "matched" and len(material["pack"]["groups"]) == 1
    assert material["pack"]["groups"][0]["match_kind"] == "exact"
    pg.execute("UPDATE chats SET name='unrelated project' WHERE chat_id='group'")
    similar = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
    assert similar["pack"]["groups"] and all(g["match_kind"] == "similar" for g in similar["pack"]["groups"])
    assert similar["eligible"] and similar["checked"] is None


def test_joint_group_validation_rejects_forged_and_cross_group_evidence(pg, scope):
    from copy import deepcopy

    from app import creator_import_evidence as imports

    ids, settings, add = scope
    pg.execute("UPDATE chats SET name='creatr._123 X TT' WHERE chat_id='group'")
    pg.execute("INSERT INTO chats(chat_id,chat_mode,name) VALUES('second','group','creator._12 X TT')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'second')", (ids["cm"],))
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('second','ou_creator')")
    for chat in ("group", "second"):
        add("creator", "This is the group for @creator._123. I create my own videos and stream live.", chat=chat)
    material = imports.build_evidence(pg, settings, ids["cm"], "creator._123")
    pack = material["pack"]
    assert material["eligible"] and len(pack["groups"]) == 2 and len(pack["users"]) == 1
    by_group = {g["ref"]: [m["ref"] for m in pack["messages"] if m["group_ref"] == g["ref"]]
                for g in pack["groups"]}
    result = {"groups": [{"group_ref": g["ref"], "match": "matched", "confidence": .99,
                          "basis": "explicit_handle", "evidence_refs": [g["ref"], *by_group[g["ref"]]]}
                         for g in pack["groups"]],
              "people": [{"user_ref": pack["users"][0]["ref"], "role": "creator", "confidence": .99,
                          "basis": "self_declaration", "evidence_refs": [m["ref"] for m in pack["messages"]]}]}
    checked = imports.validate_result(json.dumps(result), pack)
    assert checked["creator_count"] == 1 and checked["matched_group_count"] == 2
    assert checked["outcome"] == "matched"
    forged = deepcopy(result)
    forged["groups"][0]["group_ref"] = "G999"
    with pytest.raises(ValueError):
        imports.validate_result(json.dumps(forged), pack)
    cross = deepcopy(result)
    cross["groups"][0]["evidence_refs"] = by_group[result["groups"][1]["group_ref"]]
    with pytest.raises(ValueError):
        imports.validate_result(json.dumps(cross), pack)
    uncertain = deepcopy(result)
    uncertain["groups"][1].update(match="uncertain", basis="context_only")
    checked = imports.validate_result(json.dumps(uncertain), pack)
    assert checked["user_ref"] is None and checked["reason_code"] == "group_uncertain"
    uncertain["groups"][1].update(match="unrelated", confidence=.2)
    checked = imports.validate_result(json.dumps(uncertain), pack)
    assert checked["user_ref"] is None and checked["reason_code"] == "group_uncertain"


def test_cap_context_refs_must_identify_the_person_in_the_same_group(pg, scope):
    from copy import deepcopy

    from app import creator_import_evidence as imports

    ids, settings, add = scope
    pg.execute("INSERT INTO chats(chat_id,chat_mode,name) VALUES('second','group','creator._123 X TT')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'second')", (ids["cm"],))
    pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('second','ou_other'),('second','ou_cm')")
    add("cm", "@_user_1 is the CAP creator for this group; please send your video.", chat="group", mention="creator")
    add("other", "I represent the seller brand and handle product inventory for our store.", chat="second")
    add("cm", "@_user_1 is a creator in a different group.", chat="second", mention="creator")
    material = imports.build_evidence(pg, settings, ids["cm"], "creator._123", creator_type="cap")
    pack = material["pack"]
    assert material["eligible"]
    creator = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(ids["creator"]))
    other = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(ids["other"]))
    target_group = next(r["label"] for r in material["refs"] if r.get("chat_id") == "group" and r["kind"] == "group")
    directed = next(m for m in pack["messages"] if m["speaker"] == "CM1" and m["group_ref"] == target_group)
    elsewhere = next(m for m in pack["messages"] if m["speaker"] == "CM1" and m["group_ref"] != target_group)
    own_other = next(m for m in pack["messages"] if m["speaker"] == other)
    result = {"groups": [{"group_ref": g["ref"], "match": "matched", "confidence": .99,
                          "basis": "explicit_handle", "evidence_refs": [g["ref"]]} for g in pack["groups"]],
              "people": [
                  {"user_ref": creator, "role": "creator", "confidence": .99,
                   "basis": "direct_interaction", "evidence_refs": [directed["ref"]]},
                  {"user_ref": other, "role": "merchant_contact", "confidence": .99,
                   "basis": "self_declaration", "evidence_refs": [own_other["ref"]]},
              ]}
    assert imports.validate_result(json.dumps(result), pack)["user_ref"] == creator
    for refs in ([], [target_group]):
        contextual = deepcopy(result)
        contextual["people"][0].update(confidence=.9, evidence_refs=refs)
        assert imports.validate_result(json.dumps(contextual), pack)["user_ref"] == creator
    unproven_exclusion = deepcopy(result)
    unproven_exclusion["people"][1]["evidence_refs"] = [own_other["group_ref"]]
    checked = imports.validate_result(json.dumps(unproven_exclusion), pack)
    assert checked["user_ref"] is None and checked["reason_code"] == "person_ambiguous"
    for person_index, ref in ((0, elsewhere["ref"]), (1, directed["ref"])):
        invalid = deepcopy(result)
        invalid["people"][person_index]["evidence_refs"] = [ref]
        with pytest.raises(ValueError):
            imports.validate_result(json.dumps(invalid), pack)
    strict = deepcopy(pack)
    strict["roster"]["creator_type"] = "non_cap"
    with pytest.raises(ValueError):
        imports.validate_result(json.dumps(result), strict)
    undecided = deepcopy(result)
    undecided["people"][1].update(role="insufficient", confidence=0, basis="weak_context", evidence_refs=[])
    checked = imports.validate_result(json.dumps(undecided), pack)
    assert checked["user_ref"] is None and checked["reason_code"] == "person_ambiguous"
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE lark_identities SET display_name='SharedName' WHERE open_id='ou_creator'")
        pg.execute("UPDATE authorizations SET display_name='SharedName' WHERE id=%s", (ids["cm"],))
        message_id = add("cm", "SharedName is the CAP creator in this group.", chat="group")
        ambiguous = imports.build_evidence(pg, settings, ids["cm"], "creator._123", creator_type="cap")
        message_refs = {r["label"] for r in ambiguous["refs"] if r.get("message_id") == message_id}
        assert not any(m["target_refs"] for m in ambiguous["pack"]["messages"] if m["ref"] in message_refs)
    with pg.transaction(force_rollback=True):
        for contact in (ids["creator"], ids["other"]):
            pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                       "VALUES(%s,'merchant_contact','confirmed','manual',TRUE)", (contact,))
        empty = imports.build_evidence(pg, settings, ids["cm"], "creator._123", creator_type="cap")
        assert empty["checked"]["user_ref"] is None and empty["checked"]["reason_code"] == "person_unresolved"
