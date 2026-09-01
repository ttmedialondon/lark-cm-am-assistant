"""High-precision, model-free creator account claims from Lark message bodies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class CreatorClaim:
    platform: str
    normalized_value: str
    display_value: str
    source: str


_URL_RE = re.compile(
    r"(?<![A-Za-z0-9@._-])"
    r"(?P<url>(?:https?://)?(?:www\.)?(?:tiktok\.com|instagram\.com|youtube\.com|twitch\.tv)"
    r"(?:/[^\s<>()\[\]{}\"']*)?)",
    re.IGNORECASE,
)
_LABELLED_HANDLE_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_])"
    r"(?P<label>tiktok|抖音|instagram|insta|ig|ins|youtube|yt|油管|twitch)"
    r"(?![a-z0-9_])"
    r"\s*(?:账号|帐号|account|handle)?(?:\s*[:：]\s*|\s+)"
    r"@(?P<handle>[a-z0-9][a-z0-9._-]{0,63})",
)
_HANDLE_RE = {
    "tiktok": re.compile(r"[a-z0-9][a-z0-9._]{0,63}\Z", re.IGNORECASE),
    "instagram": re.compile(r"[a-z0-9][a-z0-9._]{0,63}\Z", re.IGNORECASE),
    "youtube": re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z", re.IGNORECASE),
    "twitch": re.compile(r"[a-z0-9][a-z0-9_]{0,63}\Z", re.IGNORECASE),
}
_LABEL_PLATFORMS = {
    "tiktok": "tiktok",
    "抖音": "tiktok",
    "instagram": "instagram",
    "insta": "instagram",
    "ig": "instagram",
    "ins": "instagram",
    "youtube": "youtube",
    "yt": "youtube",
    "油管": "youtube",
    "twitch": "twitch",
}
_RESERVED_PATHS = {
    "instagram": {
        "accounts",
        "about",
        "challenge",
        "developer",
        "direct",
        "explore",
        "legal",
        "p",
        "reel",
        "reels",
        "stories",
        "tv",
    },
    "twitch": {"directory", "downloads", "jobs", "login", "products", "search", "settings", "store", "videos"},
}
_TRAILING_URL_PUNCTUATION = ".,!?;:，。！？；：)]}>\\\"'"


def extract_creator_claims(body_content: str) -> tuple[CreatorClaim, ...]:
    """Return only explicit, high-confidence creator account claims in a Lark body."""
    claims: list[CreatorClaim] = []
    for text in _body_text(body_content):
        matches: list[tuple[int, CreatorClaim]] = []
        for match in _URL_RE.finditer(text):
            claim = _claim_from_url(match.group("url"))
            if claim:
                matches.append((match.start(), claim))
        for match in _LABELLED_HANDLE_RE.finditer(text):
            platform = _LABEL_PLATFORMS[match.group("label").casefold()]
            claim = _claim(platform, match.group("handle"), "labeled_handle")
            if claim:
                matches.append((match.start(), claim))
        claims.extend(claim for _, claim in sorted(matches, key=lambda item: item[0]))
    return _deduplicated(claims)


def _body_text(body_content: str) -> tuple[str, ...]:
    try:
        payload = json.loads(body_content)
    except (TypeError, json.JSONDecodeError):
        return (body_content,) if isinstance(body_content, str) else ()
    return tuple(_text_values(payload))


def _text_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _text_values(item)]
    if not isinstance(value, dict):
        return []

    text: list[str] = []
    for key, child in value.items():
        if key in {"text", "title", "href"} and isinstance(child, str):
            text.append(child)
        elif isinstance(child, dict | list):
            text.extend(_text_values(child))
    return text


def _claim_from_url(value: str) -> CreatorClaim | None:
    clean_value = value.rstrip(_TRAILING_URL_PUNCTUATION)
    parsed = urlsplit(clean_value if "://" in clean_value else f"https://{clean_value}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    platform = {
        "tiktok.com": "tiktok",
        "instagram.com": "instagram",
        "youtube.com": "youtube",
        "twitch.tv": "twitch",
    }.get(host)
    if not platform:
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None
    handle = parts[0]
    if platform in {"tiktok", "youtube"}:
        if not handle.startswith("@"):
            return None
        handle = handle[1:]
    elif handle.casefold() in _RESERVED_PATHS.get(platform, set()):
        return None
    return _claim(platform, handle, "profile_url")


def _claim(platform: str, handle: str, source: str) -> CreatorClaim | None:
    if not _HANDLE_RE[platform].fullmatch(handle):
        return None
    return CreatorClaim(platform, handle.casefold(), f"@{handle}", source)


def _deduplicated(claims: list[CreatorClaim]) -> tuple[CreatorClaim, ...]:
    unique: list[CreatorClaim] = []
    indexes: dict[tuple[str, str], int] = {}
    for claim in claims:
        key = (claim.platform, claim.normalized_value)
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(unique)
            unique.append(claim)
        elif claim.source == "profile_url" and unique[index].source != "profile_url":
            unique[index] = claim
    return tuple(unique)
