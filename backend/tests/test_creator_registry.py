import pytest

from app.creator_registry import CreatorInputError, _normalize_identifier


def test_handle_normalization_keeps_a_single_stable_platform_key() -> None:
    assert _normalize_identifier("handle", " TikTok ", "@Creator.Name") == ("creator.name", "tiktok")


def test_handle_requires_a_platform() -> None:
    with pytest.raises(CreatorInputError):
        _normalize_identifier("handle", "", "@creator")
