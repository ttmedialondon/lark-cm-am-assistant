"""Exact handle parsing and CAP evidence redaction."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from cap_fixtures import pilot as parent_pilot

from app import creator_handle_evidence as matcher
from app import handle_evidence as handles
from app.crypto import message_body_cipher
from app.openai_client import ModelOutputError

pilot = parent_pilot


def test_exact_normalization_and_group_patterns_reject_guessing():
    for value in (".horsegirlatheart", " @.Horsegirlatheart ", "https://www.tiktok.com/@.horsegirlatheart"):
        assert handles.normalize_handle(value) == ".horsegirlatheart"
    assert handles._name_handles("❤️ .horsegirlatheart X TT Media") == [".horsegirlatheart"]
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
        assert "已提供广告授权码" not in text
    redacted = handles._body(
        "I'm Ava Stone (@ava._123)", {"message_type": "text", "mentions_json": []}, {}, {"Ava Stone": "U1"}
    )
    assert "Ava Stone" not in redacted and "@ava._123" in redacted
    protected = handles._body(
        json.dumps({"text": "Alice email a+b@example.org phone +44 7700 900123"}),
        {"message_type": "text", "mentions_json": []}, {}, {"Alice": "U1"},
    )
    assert "U1" in protected and "[邮箱已隐藏]" in protected and "[电话/编号已隐藏]" in protected
    assert all(secret not in protected for secret in ("Alice", "example.org", "7700"))


def test_contact_matching_evidence_auth_replies_and_reencryption(pg, pilot):
    """Synthetic end-to-end evidence: scope, bounded QA, exact handles and freshness."""
    contact, cm = pilot.ids["creator"], pilot.ids["cm"]
    chat = "chat_creator"
    pg.execute("UPDATE chats SET name='first.handle / backup_2 & TT Media' WHERE chat_id=%s", (chat,))
    cipher = message_body_cipher(pilot.settings.token_encryption_key)
    start = datetime.now(UTC)

    def message(label, sender, body, minute=0, parent=None, observed=None):
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,body_ciphertext,"
            "message_create_time,parent_id) VALUES(%s,%s,%s,'user','text',%s,%s,%s)",
            (chat, label, sender, cipher.encrypt(json.dumps({"text": body})),
             start + timedelta(minutes=minute), parent),
        )
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id,observed_at) "
                   "VALUES(%s,%s,%s,%s)", (cm, chat, label, observed or start))

    message("question", "ou_cm", "Is your TikTok handle first.handle?", 0)
    message("answer", "ou_creator", "Yes correct", 1, "question")
    message("video", "ou_creator", "My TikTok account https://www.tiktok.com/@first.handle/video/123456789", 2)
    message("unauthorized", "ou_creator", "My TikTok account is hidden_handle", 3,
            observed=start - timedelta(days=2))
    catalog = frozenset({"first.handle", "backup_2", "hidden_handle"})
    material = matcher.build(pg, pilot.settings, contact, catalog)
    assert material["reason"] == "ready"
    pack = material["pack"]
    assert len(pack["messages"]) <= 48 and sum(len(m["text"]) for m in pack["messages"]) <= 20_000
    assert {h["handle"] for h in pack["handles"]} == {"first.handle", "backup_2"}
    assert all("hidden_handle" not in m["text"] for m in pack["messages"])
    assert any("/video/123456789" in m["text"] for m in pack["messages"])
    by_text = {m["text"]: m for m in pack["messages"]}
    question, answer = by_text["Is your TikTok handle first.handle?"], by_text["Yes correct"]
    assert answer["reply"] == question["id"]
    h1 = next(h["id"] for h in pack["handles"] if h["handle"] == "first.handle")
    h2 = next(h["id"] for h in pack["handles"] if h["handle"] == "backup_2")
    result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": h1, "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "qa_confirm", "refs": [question["id"], answer["id"]], "needed": []},
        {"h": h2, "decision": "review", "relation": "uncertain", "period": "unknown",
         "code": "no_link_evidence", "refs": [], "needed": ["account_context"]},
    ]}
    dynamic = matcher.output_format(pack)["schema"]
    fields = dynamic["properties"]["links"]["items"]["properties"]
    assert fields["h"]["enum"] == [h["id"] for h in pack["handles"]]
    assert fields["refs"]["items"]["enum"] == [m["id"] for m in pack["messages"]]
    assert fields["refs"]["maxItems"] == 6
    assert dynamic["properties"]["links"]["minItems"] == dynamic["properties"]["links"]["maxItems"] == 2
    assert dynamic["properties"]["creator"]["properties"]["state"]["enum"] == ["known"]
    assert dynamic["properties"]["creator"]["properties"]["refs"]["maxItems"] == 0
    assert "enum" not in matcher.output_format()["schema"]["properties"]["links"]["items"]["properties"]["h"]
    unknown = deepcopy(pack)
    unknown["creator_known"] = False
    unknown_creator = matcher.output_format(unknown)["schema"]["properties"]["creator"]["properties"]
    assert "known" not in unknown_creator["state"]["enum"]
    assert matcher.validate(pack, result) == result
    assert matcher.still_valid(pg, material["refs"], pilot.settings)
    invalid = deepcopy(result)
    invalid["links"][0]["refs"] = [answer["id"]]
    assert matcher.validate(pack, invalid)["links"][0]["validation_error"] == "unsupported_evidence"
    invalid = deepcopy(result)
    invalid["links"][1] = {**invalid["links"][0], "h": h2}
    reviewed = matcher.validate(pack, invalid)
    assert reviewed["links"][0] == result["links"][0]  # Bad second account does not discard valid QA.
    assert reviewed["links"][1]["decision"] == "review"
    assert reviewed["links"][1]["validation_error"] == "unsupported_evidence"
    assert matcher.validate(pack, reviewed) == reviewed  # Local diagnostics survive a safe cache replay.
    invalid["creator"] = {"state": "supported", "refs": ["nonexistent"]}
    invalid["links"][1]["refs"] = ["nonexistent", answer["id"], answer["id"]] * 3
    reviewed = matcher.validate(pack, invalid)
    assert reviewed["creator"] == {"state": "known", "refs": []}
    assert reviewed["links"][0] == result["links"][0]
    assert reviewed["links"][1]["refs"] == [answer["id"]]
    assert reviewed["links"][1]["decision"] == "review" and reviewed["links"][1]["needed"]
    assert reviewed["links"][1]["validation_error"] == "references"
    no_reply = deepcopy(pack)
    next(m for m in no_reply["messages"] if m["id"] == answer["id"])["reply"] = None
    assert matcher.validate(no_reply, result)["links"][0]["validation_error"] == "unconnected_owner_evidence"
    declaration = next(m for m in pack["messages"] if "/video/" in m["text"])
    for body in ("No worries, my TikTok account is first.handle", "My TikTok is first.handle"):
        claimed_pack = deepcopy(pack)
        next(m for m in claimed_pack["messages"] if m["id"] == declaration["id"])["text"] = body
        claimed_result = deepcopy(result)
        claimed_result["links"][0].update(code="self_claim", refs=[declaration["id"]])
        assert matcher.validate(claimed_pack, claimed_result)["links"][0]["decision"] == "supported"
    for body in ("No worries, not my TikTok account first.handle", "My friend's TikTok is first.handle",
                 "My TikTok Shop seller account is first.handle"):
        next(m for m in claimed_pack["messages"] if m["id"] == declaration["id"])["text"] = body
        assert matcher.validate(claimed_pack, claimed_result)["links"][0]["decision"] == "review"
    invalid = deepcopy(result)
    invalid["links"].append(invalid["links"][0])
    with pytest.raises(ModelOutputError):
        matcher.validate(pack, invalid)
    invalid = deepcopy(result)
    invalid["links"][0]["h"] = "first.handle"
    with pytest.raises(ModelOutputError, match="candidates"):
        matcher.validate(pack, invalid)
    invalid = deepcopy(result)
    invalid["links"][0]["refs"] = "not an array"
    with pytest.raises(ModelOutputError, match="array"):
        matcher.validate(pack, invalid)
    raw = json.dumps({"text": "Yes correct"})
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='answer'", (cipher.encrypt(raw),))
    assert matcher.build(pg, pilot.settings, contact, catalog)["fingerprint"] == material["fingerprint"]
    assert matcher.still_valid(pg, material["refs"], pilot.settings)
    # JSON-container formatting is not new evidence. Old source hashes still fail freshness checks.
    compact_raw = json.dumps({"text": "Yes correct"}, separators=(",", ":"))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='answer'", (cipher.encrypt(compact_raw),))
    refreshed = matcher.build(pg, pilot.settings, contact, catalog)
    assert refreshed["pack"] == pack and refreshed["fingerprint"] == material["fingerprint"]
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    assert matcher.still_valid(pg, refreshed["refs"], pilot.settings)
    material = refreshed
    # A dense positive history cannot hide an earlier No or later account transfer.
    pilot.person("different")
    message("pair", "ou_creator", "I have multiple accounts: first.handle and backup_2. Both are mine.", 5)
    message("other_owner", "ou_different", "My TikTok account is first.handle", 6)
    message("negative_question", "ou_cm", "Is your TikTok handle first.handle?", 7)
    message("negative_answer", "ou_creator", "No", 8, "negative_question")
    for index in range(30):
        message(f"frequent_positive_{index}", "ou_creator", "My account is first.handle", 20 + index)
    message("transfer", "ou_creator", "I transferred the first.handle account to Dan", 70)
    message("backup", "ou_creator", "My backup account is backup_2", 71)
    updated = matcher.build(pg, pilot.settings, contact, catalog)
    updated_pack = updated["pack"]
    real_ids = {ref["message_id"]: ref["label"] for ref in updated["refs"] if ref["kind"] == "message"}
    assert {"negative_question", "negative_answer", "pair", "transfer"} <= real_ids.keys()
    assert not updated["stats"]["critical_evidence_omitted"]
    by_label = {m["id"]: m for m in updated_pack["messages"]}
    assert by_label[real_ids["negative_answer"]]["reply"] == real_ids["negative_question"]
    assert by_label[real_ids["other_owner"]]["speaker"] != updated_pack["target"]
    multi_result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": handle["id"], "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "self_claim", "refs": [real_ids["pair"]], "needed": []}
        for handle in updated_pack["handles"]]}
    reviewed = matcher.validate(updated_pack, multi_result)
    by_handle = {handle["id"]: handle["handle"] for handle in updated_pack["handles"]}
    outcomes = {by_handle[link["h"]]: link for link in reviewed["links"]}
    assert outcomes["first.handle"]["decision"] == "review"
    assert outcomes["first.handle"]["code"] == "conflict"
    assert real_ids["transfer"] in outcomes["first.handle"]["refs"]
    assert outcomes["backup_2"]["decision"] == "supported"
    assert all(link["decision"] == "supported" for link in multi_result["links"])  # Validation does not mutate input.
    pg.execute("UPDATE chat_messages SET parent_id=NULL WHERE message_id='answer'")
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    pg.execute("UPDATE chat_messages SET parent_id='question' WHERE message_id='answer'")
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    assert matcher.build(pg, pilot.settings, contact, catalog)["reason"] == "no_text"
    assert matcher.build(pg, pilot.settings, next(iter(matcher.RESTRICTED_CONTACTS)), catalog)["reason"] == "restricted"
    # A known creator and Curva-named group are not account-ownership evidence.
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (cm,))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id=%s",
               (cipher.encrypt(json.dumps({"text": "Thanks for sending the sample"})), chat))
    group_only = matcher.build(pg, pilot.settings, contact, catalog)
    assert group_only["reason"] == "no_account_evidence" and group_only["pack"] is None
    assert group_only["stats"]["readable_messages"] > 0


def test_contact_matching_rejects_disconnected_and_third_party_claims():
    pack = {"target": "U1", "creator_known": True, "handles": [
        {"id": "H1", "handle": ".creator_1", "catalog_verified": True}],
        "messages": [
            {"id": "M1", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:00:00Z",
             "reply": None, "text": "My friend's account is .creator_1"},
            {"id": "M2", "speaker": "U1", "chat": "G2", "at": "2026-10-01T01:01:00Z",
             "reply": None, "text": "That is my page"}],
        "coverage": {"full_history": False, "missing_reply_ids": [], "excerpted_ids": []}}
    result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": "H1", "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "self_claim", "refs": ["M1", "M2"], "needed": []}]}
    assert matcher.validate(pack, result)["links"][0]["decision"] == "review"
    pack["messages"][0]["text"] = "My account is not .creator_1"
    assert matcher.validate(pack, result)["links"][0]["decision"] == "review"
    pack["messages"][0]["text"] = "My account is .creator_1"
    assert matcher.validate(pack, result) == result
    pack["messages"].append({"id": "M3", "speaker": "unknown", "chat": "G1",
                             "at": "2026-10-01T01:02:00Z", "reply": None,
                             "text": "My account is .creator_1"})
    assert matcher.validate(pack, result)["links"][0]["decision"] == "review"
    pack["messages"].pop()
    pack["handles"][0]["catalog_verified"] = False
    assert matcher.validate(pack, result)["links"][0]["code"] == "out_of_catalog"
    assert matcher._tokens("@.Creator_1. / .creator_1 / .creator_10", frozenset({".creator_1"})) == {".creator_1"}
    assert not matcher._tokens("a" * 65, frozenset({"a" * 64, "a"}))
    assert matcher._plain(json.dumps({"content": [[{"tag": "img", "image_key": "hidden-pixels"}]]}),
                          {"message_type": "post"}) == ""
