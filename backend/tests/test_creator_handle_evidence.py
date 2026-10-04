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
    assert matcher.validate(no_reply, result) == result  # Proven continuous private question and answer.
    no_reply["chats"][0]["mode"] = "group"
    assert matcher.validate(no_reply, result)["links"][0]["validation_error"] == "unconnected_owner_evidence"
    no_reply["chats"][0]["mode"] = "p2p"
    next(m for m in no_reply["messages"] if m["id"] == answer["id"])["previous"] = None
    assert matcher.validate(no_reply, result)["links"][0]["decision"] == "review"
    requested = deepcopy(pack)
    next(m for m in requested["messages"] if m["id"] == question["id"])["text"] = "Please send your TT user name"
    next(m for m in requested["messages"] if m["id"] == answer["id"])["text"] = "first.handle thank you"
    assert matcher.validate(requested, result) == result  # No question mark or literal handle required in request.
    next(m for m in requested["messages"] if m["id"] == question["id"])["text"] = (
        "Have you received your TikTok samples for first.handle?")
    assert matcher.validate(requested, result)["links"][0]["decision"] == "review"
    declaration = next(m for m in pack["messages"] if "/video/" in m["text"])
    for body in ("No worries, my TikTok account is first.handle", "My TikTok is first.handle",
                 "My user name is first.handle thank you. X", "My TT first.handle",
                 "My main account is first.handle"):
        claimed_pack = deepcopy(pack)
        next(m for m in claimed_pack["messages"] if m["id"] == declaration["id"])["text"] = body
        claimed_result = deepcopy(result)
        claimed_result["links"][0].update(code="self_claim", refs=[declaration["id"]])
        assert matcher.validate(claimed_pack, claimed_result)["links"][0]["decision"] == "supported"
    for body in ("No worries, not my TikTok account first.handle", "My friend's TikTok is first.handle",
                 "My TikTok Shop seller account is first.handle", "My TT videos promote first.handle",
                 "Our account is first.handle"):
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
                if link["h"] == backup["id"])["validation_error"] == "unconnected_owner_evidence"
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
    assert matcher.validate(new_handle['pack'], wrong_claim)['links'][0]['decision'] == 'review'
    assert matcher._support_groups(new_handle['pack'], new_handle['pack']['handles'][0],
                                   wrong_claim['links'][0]['refs']) == ([], [])
    wrong_claim['links'][0]['refs'] = [human_ids['new_handle']]
    assert matcher.validate(new_handle['pack'], wrong_claim)['links'][0]['decision'] == 'supported'
    pg.execute("UPDATE contact_social_accounts SET source='excel_ai_verified',reviewed_by_authorization_id=NULL "
               "WHERE contact_id=%s AND social_account_id=(SELECT id FROM social_accounts "
               "WHERE normalized_handle='first.handle')", (contact,))
    ai_import = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert ai_import['reason'] == 'already_confirmed' and ai_import['pack'] is None
    assert ai_import['stats']['human_confirmed_handles'] == ['outside_catalog']  # Upload is not human review.
    assert ai_import['stats']['confirmed_handles'] == ['first.handle', 'outside_catalog']
    manual = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}), recheck=True)
    assert manual['reason'] == 'ready' and manual['pack']['handles'][0]['handle'] == 'first.handle'
    assert manual['stats']['manual_recheck'] and manual['stats']['recheck_handles'] == ['first.handle']
    pg.execute("SAVEPOINT formal_counter")
    for label, body in (
        ('multiple_accounts', 'My main page is first.handle and my other page is backup_2. Both are mine.'),
        ('rename_known', 'Changed my username from first.handle to backup_2'),
        ('never_sold_item', 'I never sold this item on first.handle'),
        ('different_page_denial', 'first.handle is my account and backup_2 is not my account'),
        ('voucher_denial', 'first.handle voucher is not my code'),
        ('no_transfer', 'I did not transfer the first.handle account'),
    ):
        message(label, 'ou_creator', body, 96)
    unchanged = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert unchanged['reason'] == 'already_confirmed' and unchanged['pack'] is None
    message('ownership_denial', 'ou_creator', 'first.handle is not my account', 97)
    contradicted = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert contradicted['reason'] == 'ready' and contradicted['stats']['recheck_handles'] == ['first.handle']
    assert contradicted['pack']['handles'][0]['handle'] == 'first.handle'
    message('ownership_clarified', 'ou_creator', 'My TikTok account is first.handle', 98)
    clarified = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert clarified['reason'] == 'already_confirmed' and clarified['pack'] is None
    message('ownership_question_after_clear', 'ou_cm', 'Is your TikTok handle first.handle?', 99)
    message('ownership_answer_after_clear', 'ou_creator', 'That is not mine', 100, 'ownership_question_after_clear')
    parent_denial = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert parent_denial['reason'] == 'ready' and parent_denial['stats']['recheck_handles'] == ['first.handle']
    pg.execute("UPDATE chat_messages SET parent_id=NULL WHERE message_id='ownership_answer_after_clear'")
    continuous_denial = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert continuous_denial['reason'] == 'ready' and continuous_denial['stats']['recheck_handles'] == ['first.handle']
    message('clear_before_staff_chain', 'ou_creator', 'My TikTok account is first.handle', 101)
    assert matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))['reason'] == 'already_confirmed'
    message('staff_chain_question', 'ou_cm', 'Is your TikTok handle first.handle?', 102)
    message('staff_chain_supplement', 'ou_cm', 'I was checking the application', 102.1)
    message('staff_chain_fault', 'ou_cm', 'That is my fault', 102.2)
    message('staff_chain_no', 'ou_creator', 'No', 102.3)
    denied_after_staff = matcher.build(pg, pilot.settings, contact, frozenset({'first.handle'}))
    assert denied_after_staff['reason'] == 'ready'
    assert denied_after_staff['stats']['recheck_handles'] == ['first.handle']
    pg.execute("ROLLBACK TO SAVEPOINT formal_counter")
    pg.execute("ROLLBACK TO SAVEPOINT human_pairs")
    pg.execute("SAVEPOINT intro_counter")
    pilot.person("intro_peer")
    message("peer_intro", "ou_intro_peer", "I'm Alex from first.handle on tik tok :)", 2.2)
    peer_material = matcher.build(pg, pilot.settings, contact, catalog)
    peer_question = next(m['id'] for m in peer_material['pack']['messages'] if m['text'] == question['text'])
    peer_answer = next(m['id'] for m in peer_material['pack']['messages'] if m['text'] == answer['text'])
    peer_result = deepcopy(result)
    peer_result['links'][0]['refs'] = [peer_question, peer_answer]
    assert matcher.validate(peer_material['pack'], peer_result)['links'][0]['validation_error'] == 'counterevidence'
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
    assert limited["stats"]["critical_evidence_omitted_handles"] == ["first.handle"]
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
    reviewed = matcher.validate(updated_pack, multi_result)
    by_handle = {handle["id"]: handle["handle"] for handle in updated_pack["handles"]}
    outcomes = {by_handle[link["h"]]: link for link in reviewed["links"]}
    assert outcomes["first.handle"]["decision"] == "review"
    assert outcomes["first.handle"]["code"] == "conflict"
    assert real_ids["transfer"] in outcomes["first.handle"]["refs"]
    assert outcomes["backup_2"]["decision"] == "supported"
    unrelated_later_claim = deepcopy(multi_result)
    next(link for link in unrelated_later_claim["links"] if by_handle[link["h"]] == "first.handle")["refs"].append(
        real_ids["backup"])
    first_outcome = next(link for link in matcher.validate(updated_pack, unrelated_later_claim)["links"]
                         if by_handle[link["h"]] == "first.handle")
    assert real_ids["transfer"] in first_outcome["refs"]  # A later backup declaration cannot hide this transfer.
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
    # These three production-shaped cases are development regressions, not an independent accuracy set.
    sabiha = deepcopy(pack)
    sabiha['handles'][0]['handle'] = 'sabihaara'
    sabiha['chats'] = [{'id': 'G1', 'mode': 'group', 'name': 'sabihaara & TT Media'}]
    sabiha['messages'] = [
        {'id': 'M5', 'speaker': 'U1', 'chat': 'G1', 'at': '2024-05-03T13:56:49Z', 'reply': None,
         'text': 'My Handel is sabihaara'},
        {'id': 'M6', 'speaker': 'U1', 'chat': 'G1', 'at': '2024-05-03T13:57:57Z', 'reply': None,
         'text': 'I can share my TikTok here with you if you want'}]
    sabiha_result = deepcopy(result)
    sabiha_result['links'][0]['refs'] = ['M5', 'M6']
    assert matcher.validate(sabiha, sabiha_result) == sabiha_result
    for body in ('My Handel is sabiharaa', "My client's Handel is sabihaara", 'Our Handel is sabihaara'):
        wrong = deepcopy(sabiha)
        wrong['messages'][0]['text'] = body
        assert matcher.validate(wrong, sabiha_result)['links'][0]['decision'] == 'review'
    shared_typo = deepcopy(sabiha)
    shared_typo['messages'].append({'id': 'M7', 'speaker': 'U1', 'chat': 'G1', 'at': '2024-05-03T14:00:00Z',
                                    'reply': None, 'text': 'Our Handel is sabihaara'})
    assert matcher.validate(shared_typo, sabiha_result)['links'][0]['validation_error'] == 'counterevidence'

    robin = deepcopy(pack)
    robin['handles'][0]['handle'] = 'robc825'
    robin['chats'] = [{'id': 'G3', 'mode': 'group', 'name': '💙robc825(tiktokshopdeals_uk)& TT Media'}]
    robin['messages'] = [
        {'id': 'M35', 'speaker': 'U1', 'chat': 'G3', 'at': '2025-11-10T16:33:22Z', 'reply': None,
         'text': 'Please can you check my sample requests on my main to promote across both accounts please'},
        {'id': 'M36', 'speaker': 'U1', 'chat': 'G3', 'at': '2025-11-10T16:33:26.400Z', 'reply': None,
         'text': 'Robc825'}]
    robin_result = deepcopy(result)
    robin_result['links'][0]['refs'] = ['M35', 'M36']
    assert matcher.validate(robin, robin_result) == robin_result
    denied_main = deepcopy(robin)
    denied_main['messages'].append({'id': 'M37', 'speaker': 'U1', 'chat': 'G3', 'at': '2025-11-10T16:34:00Z',
                                    'reply': None, 'text': 'robc825 is not my main account'})
    assert matcher.validate(denied_main, robin_result)['links'][0]['validation_error'] == 'counterevidence'
    punctuated = deepcopy(robin)
    punctuated['messages'][1]['text'] = '@Robc825.'
    assert matcher.validate(punctuated, robin_result) == robin_result
    for body in ('Thanks for checking the samples', 'Please check our main account for samples',
                 "Please check my client's main account for samples", 'My main product needs a TikTok sample'):
        wrong = deepcopy(robin)
        wrong['messages'][0]['text'] = body
        assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong = deepcopy(robin)
    wrong['chats'][0]['name'] = 'other_creator & TT Media'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong['chats'][0]['name'] = 'Team robc825 TikTok campaign with TT Media'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong['chats'][0]['name'] = robin['chats'][0]['name']
    wrong['confirmed_handles'] = ['other_creator']
    wrong['messages'][0]['text'] = 'Please check my main account other_creator for samples'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong['messages'][0]['text'] = robin['messages'][0]['text']
    wrong['messages'][1]['text'] = 'Robc825 and other_creator'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong = deepcopy(robin)
    wrong['messages'][0]['text'] = 'My main is outside_curva_99; I promote across both accounts'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong = deepcopy(robin)
    wrong['messages'][1]['text'] = 'Please check the sample for Robc825'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong = deepcopy(robin)
    wrong['messages'][1]['at'] = '2025-11-10T16:39:22Z'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'
    wrong['messages'][1]['at'] = '2025-11-10T16:33:20Z'
    assert matcher.validate(wrong, robin_result)['links'][0]['decision'] == 'review'

    carla = deepcopy(pack)
    carla['handles'][0]['handle'] = 'shop.with.carla'
    carla['people'] = [{'id': 'U1', 'role': 'external'}, {'id': 'U2', 'role': 'staff'}]
    carla['chats'] = [{'id': 'G1', 'mode': 'p2p', 'name': 'Carla and Nero'}]
    carla['messages'] = [
        {'id': label, 'speaker': speaker, 'chat': 'G1', 'at': '2025-07-23T' + at + 'Z',
         'previous': previous, 'reply': reply, 'text': body}
        for label, speaker, at, previous, reply, body in (
            ('M12', 'U1', '10:52:40', None, None, "I don't get how it works"),
            ('M13', 'U2', '10:52:47', 'M12', None, "I applied for u, but tiktok official can't find ur uid"),
            ('M14', 'U2', '10:53:08', 'M13', None, 'I was appealing...'),
            ('M15', 'U2', '10:53:36', 'M14', 'M12', 'That is my fault'),
            ('M16', 'U1', '10:53:46', 'M15', None, 'It’s shop.with.Carla'),
            ('M20', 'U1', '10:55:27', None, None, 'I did change it on the group message but I should of said'),
        )]
    carla_result = deepcopy(result)
    carla_result['links'][0].update(code='qa_confirm', refs=['M13', 'M16'])
    assert matcher.validate(carla, carla_result) == carla_result
    for change in ('group', 'missing_chain', 'excerpted_chain', 'new_account_question',
                   'account_reply', 'sample_context'):
        wrong = deepcopy(carla)
        if change == 'group':
            wrong['chats'][0]['mode'] = 'group'
        elif change == 'missing_chain':
            wrong['messages'] = [m for m in wrong['messages'] if m['id'] != 'M14']
        elif change == 'excerpted_chain':
            wrong['coverage']['excerpted_ids'] = ['M14']
        elif change == 'new_account_question':
            next(m for m in wrong['messages'] if m['id'] == 'M14')['text'] = 'Is your TikTok handle other_creator?'
        elif change == 'account_reply':
            wrong['messages'].append({'id': 'M11', 'speaker': 'U2', 'chat': 'G1', 'at': '2025-07-23T10:50:00Z',
                                      'reply': None, 'text': 'Is your TikTok handle other_creator?'})
            next(m for m in wrong['messages'] if m['id'] == 'M14')['reply'] = 'M11'
        else:
            next(m for m in wrong['messages'] if m['id'] == 'M13')['text'] = "TikTok can't find ur sample request"
        assert matcher.validate(wrong, carla_result)['links'][0]['decision'] == 'review'
    missing_question = deepcopy(carla_result)
    missing_question['links'][0]['refs'] = ['M16']
    assert matcher.validate(carla, missing_question)['links'][0]['decision'] == 'review'
    model_review = deepcopy(carla_result)
    model_review['links'][0].update(decision='review', relation='uncertain', period='unknown',
                                   code='no_link_evidence', needed=['account_context'])
    assert matcher.validate(carla, model_review) == model_review  # Local context cannot promote model review.
    counter = deepcopy(carla)
    counter['messages'].insert(0, {'id': 'M10', 'speaker': 'U1', 'chat': 'G1', 'at': '2025-07-23T10:40:00Z',
                                   'previous': None, 'reply': None, 'text': 'My TikTok account is shop.with.carla'})
    next(m for m in counter['messages'] if m['id'] == 'M13')['text'] = 'Is your TikTok handle shop.with.carla?'
    previous_claim = deepcopy(carla_result)
    previous_claim['links'][0].update(code='self_claim', refs=['M10'])
    for denial in ('No', 'That is not mine'):
        next(m for m in counter['messages'] if m['id'] == 'M16')['text'] = denial
        assert matcher.validate(counter, previous_claim)['links'][0]['validation_error'] == 'counterevidence'

    natural = deepcopy(pack)
    natural['messages'] = [
        {"id": "M1", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:00:00Z", "reply": None,
         "text": "I'm Daisy from .creator_1 on tik tok :)"},
        {"id": "M2", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:01:00Z", "reply": None,
         "text": "I posted the three videos for the collaboration, I'm not stopping there"}]
    natural_result = deepcopy(result)
    natural_result['links'][0]['refs'] = ['M1']
    assert matcher.validate(natural, natural_result) == natural_result
    assert matcher._support_groups(natural, natural['handles'][0])[0] == [{'kind': 'self_claim', 'refs': ['M1']}]
    natural['messages'][1]['text'] = 'I have not posted the three videos'
    assert matcher.validate(natural, natural_result)['links'][0]['decision'] == 'review'
    natural['messages'][1]['text'] = 'I didn’t get around to posting a video'
    assert matcher.validate(natural, natural_result)['links'][0]['decision'] == 'review'
    natural['messages'][1]['text'] = "I posted the three videos for the collaboration, I'm not stopping there"
    natural['creator_known'] = False
    assert matcher.validate(natural, natural_result)['links'][0]['decision'] == 'review'
    natural['creator_known'] = True
    natural['messages'][1]['speaker'] = 'U2'
    assert matcher.validate(natural, natural_result)['links'][0]['decision'] == 'review'  # Staff's activity is not本人.
    natural['messages'][1]['speaker'] = 'U1'
    for intro in ("I'm .creator_1", "I'm Daisy from .creator_1 on tik tok as their assistant",
                  "I'm a representative from .creator_1 on TikTok", "I'm Daisy from .creator_1 on TikTok for my client",
                  "I'm Daisy from .creator_1 on TikTok, but not my account"):
        natural['messages'][0]['text'] = intro
        assert matcher.validate(natural, natural_result)['links'][0]['decision'] == 'review'
    natural['messages'][0]['text'] = "I'm Daisy from .creator_1 on tik tok :)"
    natural['messages'].append({"id": "M3", "speaker": "unknown", "chat": "G1",
                                "at": "2026-10-01T01:02:00Z", "reply": None,
                                "text": "I'm another person from .creator_1 on TikTok"})
    assert matcher.validate(natural, natural_result)['links'][0]['validation_error'] == 'counterevidence'
    pack["messages"].append({"id": "M3", "speaker": "unknown", "chat": "G1",
                             "at": "2026-10-01T01:02:00Z", "reply": None,
                             "text": "My account is .creator_1"})
    assert matcher.validate(pack, result)["links"][0]["decision"] == "review"
    pack["messages"].pop()
    # Regression evidence used to develop the rules, not an independent accuracy test.
    pack["chats"] = [{"id": "G1", "mode": "p2p", "name": "untrusted title"}]
    pack["people"] = [{"id": "U1", "role": "external"}, {"id": "U2", "role": "staff"}]
    pack["messages"] = [
        {"id": "M1", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:00:00Z", "reply": None,
         "text": "I joined after this time but mine is .creator_1"},
        {"id": "M2", "speaker": "U2", "chat": "G1", "at": "2026-10-01T01:01:00Z", "reply": None,
         "text": "Award list: .creator_1, someone_else. Have the invites gone out?"},
        {"id": "M3", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:02:00Z", "reply": "M2",
         "text": "Not yet xx"},
        {"id": "M4", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:03:00Z", "reply": "M2",
         "text": "Not sure which ones I want lol so much choice! Anyone had a message yet?"},
        {"id": "M5", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:04:00Z", "reply": None,
         "text": "I have unlinked the agency for .creator_1"},
    ]
    result["links"][0]["refs"] = ["M1"]
    assert matcher.validate(pack, result) == result  # Awards and agency membership do not contradict ownership.
    pack["messages"][1]["text"] = "Is your TikTok handle .creator_1?"
    pack["messages"][2].update(text="No", reply=None, previous="M2")
    assert matcher.validate(pack, result)["links"][0]["validation_error"] == "counterevidence"
    pack["chats"][0]["mode"] = "group"
    assert matcher.validate(pack, result) == result  # No reply in a group cannot target this ownership question.
    pack["messages"][2]["reply"] = "M2"
    assert matcher.validate(pack, result)["links"][0]["validation_error"] == "counterevidence"
    pack["messages"] = pack["messages"][:2]
    pack["messages"][0].update(text="Our account is .creator_1", reply=None)
    assert matcher.validate(pack, result)["links"][0]["decision"] == "review"
    pack["messages"][0]["text"] = "I make the videos and livestream on our account .creator_1"
    shared = deepcopy(result)
    shared["links"][0].update(relation="account_creator", code="own_content")
    assert matcher.validate(pack, shared) == shared  # Shared creation is not an owner write.
    pack["messages"][0]["text"] = "My user name is .creator_1 thank you. X"
    pack["coverage"]["missing_reply_ids"] = ["M1"]
    assert matcher.validate(pack, result) == result  # Missing unrelated parent does not erase explicit self-claim.
    pack["coverage"]["missing_reply_ids"] = []
    pack["messages"][0].update(text="Please drop your username below if you did NOT receive a sample", speaker="U2")
    pack["messages"][1].update(text=".creator_1", speaker="U1", reply="M1")
    qa = deepcopy(result)
    qa["links"][0].update(code="qa_confirm", refs=["M1", "M2"])
    # A negative sample condition before the actual username request is unrelated.
    for request in ("Creators who did NOT receive samples please drop your username below",
                    "Please drop your username below if you did NOT receive a sample",
                    "Can I have your tiktok handle please?", "Could I get your TT username please?"):
        pack["messages"][0]["text"] = request
        assert matcher.validate(pack, qa) == qa
    pack["chats"][0]["mode"] = "p2p"
    pack["messages"][1].update(reply=None, previous="M1")
    assert matcher.validate(pack, qa) == qa
    pack["messages"][1]["at"] = "2026-10-01T01:35:00Z"
    assert matcher.validate(pack, qa)["links"][0]["decision"] == "review"
    pack["messages"][1].update(at="2026-10-01T01:01:00Z", reply="M1")
    for request in ("Do NOT send your username", "Is this NOT your handle .creator_1?",
                    "Please tell me your friend's username", "Did your TikTok handle .creator_1 receive samples?"):
        pack["messages"][0]["text"] = request
        assert matcher.validate(pack, qa)["links"][0]["decision"] == "review"
    pack["messages"][0]["text"] = "Can I have your TikTok handle please?"
    for answer in ("Our account is .creator_1", "Our team account is .creator_1",
                   "The shared account is .creator_1", "The company account is .creator_1",
                   "Our TikTok is .creator_1", "Our team's account is .creator_1"):
        pack["messages"][1]["text"] = answer
        assert matcher.validate(pack, qa)["links"][0]["decision"] == "review"
        assert matcher._support_groups(pack, pack["handles"][0])[0] == []
    pack["messages"][1]["text"] = "My account .creator_1 is shared"
    self_claim = deepcopy(qa)
    self_claim["links"][0].update(code="self_claim", refs=["M2"])
    assert matcher.validate(pack, self_claim)["links"][0]["decision"] == "review"
    pack["messages"][1]["text"] = "Thanks team, my account is .creator_1"
    assert matcher.validate(pack, self_claim) == self_claim  # The word team alone is not shared ownership.
    shared_history = deepcopy(pack)
    shared_history["handles"].append({"id": "H2", "handle": "another_creator", "catalog_verified": True})
    shared_history["messages"].extend([
        {"id": "M3", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:02:00Z", "reply": None,
         "text": "Our team account is .creator_1"},
        {"id": "M4", "speaker": "U1", "chat": "G1", "at": "2026-10-01T01:03:00Z", "reply": None,
         "text": "My account is another_creator"}])
    shared_result = deepcopy(self_claim)
    shared_result["links"].append({**self_claim["links"][0], "h": "H2", "refs": ["M4"]})
    for statement in ("Our team account is .creator_1", "Our TikTok is .creator_1", "Our team's account is .creator_1"):
        shared_history["messages"][2]["text"] = statement
        outcomes = matcher.validate(shared_history, shared_result)["links"]
        assert outcomes[0]["validation_error"] == "counterevidence" and "M3" in outcomes[0]["refs"]
        assert outcomes[1]["decision"] == "supported"  # Shared ownership does not spread to a different account.
    for statement in ("I shared the .creator_1 account link with you", "My account link is shared: .creator_1"):
        shared_history["messages"][2]["text"] = statement
        assert matcher.validate(shared_history, shared_result)["links"][0]["decision"] == "supported"
    contradictory = deepcopy(result)
    contradictory["links"][0].update(decision="review", relation="uncertain", period="unknown",
                                    code="out_of_catalog", refs=[], needed=[])
    normalized = matcher.validate(pack, contradictory)["links"][0]
    assert normalized["decision"] == "review" and normalized["code"] == "no_link_evidence"
    assert normalized["needed"] == ["account_context"]
    pack["handles"][0]["catalog_verified"] = False
    assert matcher.validate(pack, result)["links"][0]["code"] == "out_of_catalog"
    assert matcher._tokens("@.Creator_1. / .creator_1 / .creator_10", frozenset({".creator_1"})) == {".creator_1"}
    assert not matcher._tokens("a" * 65, frozenset({"a" * 64, "a"}))
    assert matcher._plain(json.dumps({"content": [[{"tag": "img", "image_key": "hidden-pixels"}]]}),
                          {"message_type": "post"}) == ""
