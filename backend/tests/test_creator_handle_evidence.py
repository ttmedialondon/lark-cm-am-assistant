"""Exact handle parsing and CAP evidence redaction."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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


def test_contact_matching_evidence_auth_replies_and_reencryption(pg, pilot, monkeypatch):
    """Synthetic end-to-end evidence: scope, bounded QA, exact handles and freshness."""
    contact, cm = pilot.ids["creator"], pilot.ids["cm"]
    chat = "chat_creator"
    pg.execute("UPDATE lark_identities SET tenant_classification='internal' WHERE open_id='ou_cm'")
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
    assert answer["previous"] == question["id"]
    assert pack["chats"][0]["mode"] == "p2p"
    hints = {hint["h"]: hint["groups"] for hint in pack["evidence_hints"]}
    h1 = next(h["id"] for h in pack["handles"] if h["handle"] == "first.handle")
    h2 = next(h["id"] for h in pack["handles"] if h["handle"] == "backup_2")
    assert {"kind": "qa", "refs": [question["id"], answer["id"]]} in hints[h1]
    assert h2 not in hints  # The group title alone produces no ownership hint.
    result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": h1, "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "qa_confirm", "refs": [question["id"], answer["id"]], "needed": []},
        {"h": h2, "decision": "review", "relation": "uncertain", "period": "unknown",
         "code": "no_link_evidence", "refs": [], "needed": ["account_context"]},
    ]}
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
    assert matcher.validate(no_reply, result) == result  # Proven continuous private question and answer.
    no_reply["chats"][0]["mode"] = "group"
    assert matcher.validate(no_reply, result)["links"][0]["validation_error"] == "reply_relation"
    no_reply["chats"][0]["mode"] = "p2p"
    next(m for m in no_reply["messages"] if m["id"] == answer["id"])["previous"] = None
    assert matcher.validate(no_reply, result)["links"][0]["decision"] == "review"
    requested = deepcopy(pack)
    next(m for m in requested["messages"] if m["id"] == question["id"])["text"] = "Please send your TT user name"
    next(m for m in requested["messages"] if m["id"] == answer["id"])["text"] = "first.handle thank you"
    assert matcher.validate(requested, result) == result  # No question mark or literal handle required in request.
    next(m for m in requested["messages"] if m["id"] == question["id"])["text"] = (
        "Have you received your TikTok samples for first.handle?")
    assert matcher.validate(requested, result) == result  # Account question intent belongs to AI.
    declaration = next(m for m in pack["messages"] if "/video/" in m["text"])
    claimed_result = deepcopy(result)
    claimed_result["links"][0].update(code="self_claim", refs=[declaration["id"]])
    assert matcher.validate(pack, claimed_result)["links"][0]["decision"] == "supported"
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
    pg.execute("SAVEPOINT core_packing")
    message("core_claim", "ou_creator", "My user name is first.handle", 80)
    for index in range(8):
        message(f"noisy_context_{index}", "ou_cm", f"Shipping update {index}", 80 + index / 100)
    message("core_private_question", "ou_cm", "Can I have your TikTok handle please?", 90)
    message("core_private_answer", "ou_creator", "backup_2", 91)
    with monkeypatch.context() as limits:
        limits.setattr(matcher, "MAX_MESSAGES", 4)
        core = matcher.build(pg, pilot.settings, contact, catalog)
    core_ids = {ref["message_id"]: ref["label"] for ref in core["refs"] if ref["kind"] == "message"}
    assert {"core_claim", "video", "core_private_question", "core_private_answer"} <= core_ids.keys()
    assert len(core["pack"]["messages"]) == 4  # Noisy neighbours cannot consume core QA's slots.
    backup = next(h for h in core["pack"]["handles"] if h["handle"] == "backup_2")
    core_hints = {hint["h"]: hint["groups"] for hint in core["pack"]["evidence_hints"]}
    assert {"kind": "qa", "refs": [core_ids["core_private_question"], core_ids["core_private_answer"]]} in (
        core_hints[backup["id"]])
    assert matcher._support_groups(core["pack"], backup, []) == ([], [])  # Empty model refs never use all-pack hints.
    core_result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": h["id"], "decision": "review", "relation": "uncertain", "period": "unknown",
         "code": "no_link_evidence", "refs": [], "needed": ["account_context"]} for h in core["pack"]["handles"]]}
    backup_link = next(link for link in core_result["links"] if link["h"] == backup["id"])
    backup_link.update(decision="supported", relation="self_account", period="at_message", code="qa_confirm",
                       refs=[core_ids["core_private_question"], core_ids["core_private_answer"]], needed=[])
    assert next(link for link in matcher.validate(core["pack"], core_result)["links"]
                if link["h"] == backup["id"])["decision"] == "supported"
    backup_link["refs"] = [core_ids["core_private_answer"]]
    assert next(link for link in matcher.validate(core["pack"], core_result)["links"]
                if link["h"] == backup["id"])["validation_error"] == "reply_relation"
    pg.execute("ROLLBACK TO SAVEPOINT core_packing")
    pg.execute("SAVEPOINT correction_core")
    pg.execute("UPDATE chats SET name='shop.with.carla & TT Media' WHERE chat_id=%s", (chat,))
    message('correction_uid', 'ou_cm', "I applied for u, but tiktok official can't find ur uid", 150)
    message('correction_appeal', 'ou_cm', 'I was appealing...', 150.1)
    message('correction_fault', 'ou_cm', 'That is my fault', 150.2)
    message('correction_answer', 'ou_creator', 'It’s shop.with.Carla', 150.3)
    with monkeypatch.context() as limits:
        limits.setattr(matcher, 'MAX_MESSAGES', 4)
        correction = matcher.build(pg, pilot.settings, contact, frozenset({'shop.with.carla'}))
    correction_ids = {r['message_id']: r['label'] for r in correction['refs'] if r['kind'] == 'message'}
    assert set(correction_ids) == {'correction_uid', 'correction_appeal', 'correction_fault', 'correction_answer'}
    correction_result = {'creator': {'state': 'known', 'refs': []}, 'links': [
        {'h': correction['pack']['handles'][0]['id'], 'decision': 'supported', 'relation': 'self_account',
         'period': 'at_message', 'code': 'qa_confirm', 'needed': [],
         'refs': [correction_ids['correction_uid'], correction_ids['correction_answer']]}]}
    assert matcher.validate(correction['pack'], correction_result) == correction_result
    pg.execute("ROLLBACK TO SAVEPOINT correction_core")
    pg.execute("SAVEPOINT omitted_foreign_main")
    pilot.person('main_peer')
    pg.execute("UPDATE chats SET chat_mode='group',name='first.handle & TT Media' WHERE chat_id=%s", (chat,))
    message('main_owner', 'ou_creator', 'My TikTok account is first.handle', 180)
    message('peer_main', 'ou_main_peer', 'Please check my main to promote across both accounts', 180.1)
    message('peer_main_handle', 'ou_main_peer', 'first.handle', 180.2)
    with monkeypatch.context() as limits:
        limits.setattr(matcher, 'MAX_CHARS', 44)
        omitted_main = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    omitted_ids = {r['message_id'] for r in omitted_main['refs'] if r['kind'] == 'message'}
    assert 'main_owner' in omitted_ids and 'peer_main' not in omitted_ids
    assert omitted_main['stats']['critical_evidence_omitted_handles'] == ['first.handle']
    pg.execute("ROLLBACK TO SAVEPOINT omitted_foreign_main")
    pg.execute("SAVEPOINT human_pairs")
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','first.handle','first.handle'),(%s,'tiktok','outside_catalog','outside_catalog')",
               (uuid4(), uuid4()))
    pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,"
               "reviewed_by_authorization_id,reviewed_at) SELECT %s,id,'owner','confirmed','cm_confirmation',%s,NOW() "
               "FROM social_accounts WHERE normalized_handle IN ('first.handle','outside_catalog')", (contact, cm))
    only_known = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert only_known['pack'] is None and only_known['reason'] == 'already_confirmed'
    assert only_known['stats']['human_confirmed_handles'] == ['first.handle', 'outside_catalog']
    message("human_claim", "ou_creator", "My TikTok account is first.handle", 93)
    message("unbound_new", "ou_creator", "Here is backup_2", 94)
    message("new_handle", "ou_creator", "My backup account is backup_2", 95)
    with monkeypatch.context() as limits:
        limits.setattr(matcher, 'MAX_HANDLES', 1)
        new_handle = matcher.build(pg, pilot.settings, contact, catalog)
    assert new_handle['reason'] == 'ready'
    assert [h['handle'] for h in new_handle['pack']['handles']] == ['backup_2']  # Filter human pairs before top-N.
    assert new_handle['pack']['confirmed_handles'] == ['first.handle', 'outside_catalog']
    human_ids = {r['message_id']: r['label'] for r in new_handle['refs'] if r['kind'] == 'message'}
    wrong_claim = {'creator': {'state': 'known', 'refs': []}, 'links': [
        {'h': 'H1', 'decision': 'supported', 'relation': 'self_account', 'period': 'at_message',
         'code': 'self_claim', 'refs': [human_ids['human_claim'], human_ids['unbound_new']], 'needed': []}]}
    wrong_claim['links'][0].update(decision='review', relation='uncertain', period='unknown',
                                  code='no_link_evidence', needed=['account_context'])
    assert matcher.validate(new_handle['pack'], wrong_claim) == wrong_claim  # Hints cannot override AI review.
    assert matcher._support_groups(new_handle['pack'], new_handle['pack']['handles'][0],
                                   wrong_claim['links'][0]['refs']) == ([], [])
    wrong_claim['links'][0].update(decision='supported', relation='self_account', period='at_message',
                                  code='self_claim', needed=[], refs=[human_ids['new_handle']])
    assert matcher.validate(new_handle['pack'], wrong_claim)['links'][0]['decision'] == 'supported'
    pg.execute("UPDATE contact_social_accounts SET source='excel_ai_verified',reviewed_by_authorization_id=NULL "
               "WHERE contact_id=%s AND social_account_id=(SELECT id FROM social_accounts "
               "WHERE normalized_handle='first.handle')", (contact,))
    ai_import = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert ai_import['reason'] == 'ready'
    assert [h['handle'] for h in ai_import['pack']['handles']] == ['backup_2']
    assert ai_import['stats']['human_confirmed_handles'] == ['outside_catalog']  # Upload is not human review.
    assert ai_import['stats']['confirmed_handles'] == ['first.handle', 'outside_catalog']
    manual = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}), recheck=True)
    assert manual['reason'] == 'ready' and manual['pack']['handles'][0]['handle'] == 'first.handle'
    assert manual['stats']['manual_recheck'] and manual['stats']['recheck_handles'] == ['first.handle']
    message('ownership_denial', 'ou_creator', 'first.handle is not my account', 97)
    skipped = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert skipped['reason'] == 'ready'
    assert [h['handle'] for h in skipped['pack']['handles']] == ['backup_2']  # Only the new account is rechecked.
    assert matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}), recheck=True)['reason'] == 'ready'
    pg.execute("ROLLBACK TO SAVEPOINT human_pairs")
    pg.execute("SAVEPOINT intro_counter")
    pilot.person("intro_peer")
    message("peer_intro", "ou_intro_peer", "I'm Alex from first.handle on tik tok :)", 2.2)
    peer_material = matcher.build(pg, pilot.settings, contact, catalog)
    peer_question = next(m['id'] for m in peer_material['pack']['messages'] if m['text'] == question['text'])
    peer_answer = next(m['id'] for m in peer_material['pack']['messages'] if m['text'] == answer['text'])
    peer_result = deepcopy(result)
    peer_result['links'][0]['refs'] = [peer_question, peer_answer]
    assert matcher.validate(peer_material['pack'], peer_result) == peer_result  # Chat conflicts belong to AI.
    with monkeypatch.context() as limits:
        limits.setattr(matcher, 'MAX_CHARS', 55)
        omitted_peer = matcher.build(pg, pilot.settings, contact, catalog)
    assert omitted_peer['stats']['critical_evidence_omitted_handles'] == ['first.handle']
    pg.execute("ROLLBACK TO SAVEPOINT intro_counter")
    # A skipped unauthorized/non-text predecessor must not create fake private adjacency.
    pg.execute("SAVEPOINT adjacency_check")
    message("unreadable_between", "ou_cm", "Unrelated topic", 0.5, observed=start - timedelta(days=2))
    pg.execute("UPDATE chat_messages SET message_type='image' WHERE message_id='unreadable_between'")
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    inserted = matcher.build(pg, pilot.settings, contact, catalog)
    assert next(m for m in inserted["pack"]["messages"] if m["text"] == "Yes correct")["previous"] is None
    pg.execute("ROLLBACK TO SAVEPOINT adjacency_check")
    assert matcher.still_valid(pg, material["refs"], pilot.settings)
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
    message("unlink", "ou_creator", "I have unlinked the agency for my account backup_2", 72)
    updated = matcher.build(pg, pilot.settings, contact, catalog)
    updated_pack = updated["pack"]
    real_ids = {ref["message_id"]: ref["label"] for ref in updated["refs"] if ref["kind"] == "message"}
    assert {"negative_question", "negative_answer", "pair", "transfer"} <= real_ids.keys()
    assert not updated["stats"]["critical_evidence_omitted"]
    assert updated["stats"]["critical_evidence_omitted_handles"] == []
    assert all(1 <= len(hint["groups"]) <= 3 and all(set(group) == {"kind", "refs"}
               and 1 <= len(group["refs"]) <= 2 for group in hint["groups"])
               for hint in updated_pack["evidence_hints"])
    assert len(next(hint for hint in updated_pack["evidence_hints"]
                    if hint["h"] == next(h["id"] for h in updated_pack["handles"]
                                         if h["handle"] == "first.handle"))["groups"]) == 3
    with monkeypatch.context() as limits:
        limits.setattr(matcher, "MAX_CHARS", 40)
        limited = matcher.build(pg, pilot.settings, contact, catalog)
    assert limited["stats"]["critical_evidence_omitted"]
    assert limited["stats"]["critical_evidence_omitted_handles"] == ["backup_2", "first.handle"]
    assert limited["pack"]["coverage"]["omitted_handle_ids"] == [h["id"] for h in limited["pack"]["handles"]]
    pg.execute("SAVEPOINT omitted_rename")
    message("renamed", "ou_creator", "Changed my username from first.handle to backup_2", 73)
    with monkeypatch.context() as limits:
        limits.setattr(matcher, "MAX_CHARS", 40)
        omitted_rename = matcher.build(pg, pilot.settings, contact, catalog)
    assert "first.handle" in omitted_rename["stats"]["critical_evidence_omitted_handles"]
    pg.execute("ROLLBACK TO SAVEPOINT omitted_rename")
    by_label = {m["id"]: m for m in updated_pack["messages"]}
    assert by_label[real_ids["negative_answer"]]["reply"] == real_ids["negative_question"]
    assert by_label[real_ids["other_owner"]]["speaker"] != updated_pack["target"]
    multi_result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": handle["id"], "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "self_claim", "refs": [real_ids["pair"]], "needed": []}
        for handle in updated_pack["handles"]]}
    assert matcher.validate(updated_pack, multi_result) == multi_result  # No keyword-based transfer veto.
    by_handle = {handle["id"]: handle["handle"] for handle in updated_pack["handles"]}
    model_review = deepcopy(multi_result)
    next(link for link in model_review["links"] if by_handle[link["h"]] == "first.handle").update(
        decision="review", relation="uncertain", period="historical", code="conflict",
        refs=[real_ids["transfer"]], needed=["recent_state"])
    assert matcher.validate(updated_pack, model_review) == model_review  # AI review is never promoted.
    assert all(link["decision"] == "supported" for link in multi_result["links"])
    pg.execute("UPDATE chat_messages SET parent_id=NULL WHERE message_id='answer'")
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    pg.execute("UPDATE chat_messages SET parent_id='question' WHERE message_id='answer'")
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
    assert not matcher.still_valid(pg, material["refs"], pilot.settings)
    assert matcher.build(pg, pilot.settings, contact, catalog)["reason"] == "no_text"
    assert matcher.build(pg, pilot.settings, next(iter(matcher.RESTRICTED_CONTACTS)), catalog)["reason"] == "restricted"
    # A title plus readable text is sent to AI; retrieval hints never decide ownership.
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (cm,))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE chat_id=%s",
               (cipher.encrypt(json.dumps({"text": "Thanks for sending the sample"})), chat))
    group_only = matcher.build(pg, pilot.settings, contact, catalog)
    assert group_only["reason"] == "ready" and group_only["pack"] is not None
    assert group_only["stats"]["readable_messages"] > 0
    # Explicit TikTok account evidence can introduce a precise handle without a Curva row.
    pg.execute("SAVEPOINT non_catalog_claim")
    pg.execute("UPDATE chats SET name='MAC Creators',chat_mode='group' WHERE chat_id=%s", (chat,))
    assert matcher.build(pg, pilot.settings, contact, frozenset())["reason"] == "no_candidates"
    message("outside_self_claim", "ou_creator", "My TikTok username is outside.creator", 200)
    outside = matcher.build(pg, pilot.settings, contact, frozenset())
    assert outside["reason"] == "ready"
    assert outside["pack"]["handles"] == [
        {"id": "H1", "handle": "outside.creator", "catalog_verified": False}]
    own_reference = next(m["id"] for m in outside["pack"]["messages"] if "outside.creator" in m["text"])
    outside_result = {"creator": {"state": "known", "refs": []}, "links": [
        {"h": "H1", "decision": "supported", "relation": "self_account", "period": "at_message",
         "code": "self_claim", "refs": [own_reference], "needed": []}]}
    assert matcher.validate(outside["pack"], outside_result) == outside_result
    pg.execute("ROLLBACK TO SAVEPOINT non_catalog_claim")

    # A new person without any handle still gets assessed from real personal business and QA.
    newcomer = pilot.person("identity_newcomer", pairs=1)
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s AND role='creator'", (newcomer,))
    pg.execute("UPDATE chats SET name='New contact conversation' WHERE chat_id='chat_identity_newcomer'")
    for mid, sender, body, minute in (
        ("identity_question", "ou_cm", "Could you film a TikTok review of this sample?", 250),
        ("identity_answer", "ou_identity_newcomer", "Yes, I'll film the review tonight and post it tomorrow.", 251),
    ):
        pg.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,body_ciphertext,"
            "message_create_time) VALUES('chat_identity_newcomer',%s,%s,'user','text',%s,%s)",
            (mid, sender, cipher.encrypt(json.dumps({"text": body})), start + timedelta(minutes=minute)),
        )
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,'chat_identity_newcomer',%s)", (cm, mid))
    assert matcher.build(pg, pilot.settings, newcomer, frozenset())["reason"] == "no_candidates"
    identity = matcher.build(pg, pilot.settings, newcomer, frozenset(), purpose="identity")
    pack = identity["pack"]
    assert identity["reason"] == "ready" and pack["purpose"] == "identity"
    assert not pack["creator_known"] and not pack["handles"]
    labels = {r["message_id"]: r["label"] for r in identity["refs"] if r["kind"] == "message"}
    assert {"identity_question", "identity_answer"} <= labels.keys()
    result = {"creator": {"state": "supported", "code": "business_reply", "confidence": 0.98,
                          "refs": [labels["identity_question"], labels["identity_answer"]]}, "links": []}
    assert matcher.validate(pack, result) == result
    assert matcher.still_valid(pg, identity["refs"], pilot.settings)
    no_owner = deepcopy(result)
    no_owner["creator"]["refs"] = [labels["identity_question"]]
    assert matcher.validate(pack, no_owner)["creator"]["state"] == "review"
    no_parent = deepcopy(pack)
    next(m for m in no_parent["messages"] if m["id"] == labels["identity_answer"])["previous"] = None
    assert matcher.validate(no_parent, result)["creator"]["state"] == "review"
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
    assert not matcher.still_valid(pg, identity["refs"], pilot.settings)
    assert matcher.build(pg, pilot.settings, newcomer, frozenset(), purpose="identity")["reason"] == "no_text"
