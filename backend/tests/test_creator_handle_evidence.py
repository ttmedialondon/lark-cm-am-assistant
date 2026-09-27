"""Exact handle parsing and CAP evidence redaction."""
import json

import pytest

from app import handle_evidence as handles


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
