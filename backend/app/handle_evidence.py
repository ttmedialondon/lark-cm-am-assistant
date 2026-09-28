"""Shared creator evidence helpers; no identity or ownership scanning."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from . import evidence_access as ownership
from . import evidence_text as people
from .message_mentions import mention_targets, resolve_mentions, rich_mention_text

_HANDLE = re.compile(r"[A-Za-z0-9._]{1,64}\Z", re.ASCII)

_URL = re.compile(r"https?://(?:www\.|m\.)?tiktok\.com/@([A-Za-z0-9._]+)(?:/video/\d+)?[^\s<>\"']*", re.I)

_AT = re.compile(r"(?<![\w@])@([A-Za-z0-9._]{1,64})(?![\w.])", re.ASCII)

_DECLARATION = re.compile(
    r"\b(?:my\s+)?(?:tiktok\s+(?:handle|username|account)|handle|username|account)\s*(?:is\b|[:=])\s*@?"
    r"([A-Za-z0-9._]{1,64})(?![\w.])|(?:我的)?(?:账号|帐号|用户名)\s*(?:是|为|[:：=])\s*@?"
    r"([A-Za-z0-9._]{1,64})(?![\w.])",
    re.I,
)

_GROUP_NAME = re.compile(
    r"^[\s\u2600-\u27bf\U0001f000-\U0001faff\ufe0f\u200d]*@?([A-Za-z0-9._]{1,64})\s*[xX×*＊&|·。]\s*"
    r"(?:TT(?:\s*Media)?|Trutonic\s*Media|cm\d+)\b",
    re.I,
)

_RESERVED = frozenset(
    "blocked banned hacked suspended restricted private public fine working not new deleted "
    "unknown missing active inactive disabled live video none null username handle account".split()
)

_CHANGE = re.compile(
    r"\b(?:renam\w*|changed?\s+(?:my\s+)?(?:handle|username|account)|old\s+(?:handle|username|account)|"
    r"(?:previous|former)\s+(?:handle|username)|no longer (?:use|own)|"
    r"(?:not|isn't|isn’t|wasn't|wasn’t) my (?:account|handle|username)|"
    r"second\s+account|another\s+account|backup\s+account|multiple\s+accounts)\b|"
    r"改名|旧账号|旧帐号|以前的账号|不是我的账号|小号|第二个账号|多个账号",
    re.I,
)

_AD_CODE = re.compile(
    r"((?:\b(?:spark\s*(?:ad\s*)?|ad\s*|advertising\s*)code|广告码|授权码)\s*(?:is\b|[:：=])\s*)"
    r"(#?[A-Za-z0-9+/=_-]{6,})|(?<![\w@])#[A-Za-z0-9+/=_-]{12,}|(?m:^\s*[A-Za-z0-9+/=_-]{24,}\s*$)",
    re.I,
)

def normalize_handle(value):
    """An exact token or TikTok profile URL, never a display-name/fuzzy conversion."""
    if not isinstance(value, str):
        raise ValueError("handle must be text")
    value = value.strip()
    if value.startswith(("http://", "https://")):
        url = urlsplit(value)
        if (url.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            raise ValueError("not a TikTok profile")
        if url.username or url.password or url.port or not re.fullmatch(r"/@[A-Za-z0-9._]+/?", url.path):
            raise ValueError("not an exact profile URL")
        value = url.path.removeprefix("/@").removesuffix("/")
    else:
        value = value.removeprefix("@")
    if not _HANDLE.fullmatch(value) or value.endswith(".") or not any(c.isalnum() for c in value):
        raise ValueError("invalid handle")
    return value.casefold()

def _clues(text, *, group_name=False):
    clues, occupied = [], []
    for pattern in (_URL, _DECLARATION, _AT, *((_GROUP_NAME,) if group_name else ())):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            raw_token = next((v for v in match.groups() if v is not None), "")
            token = raw_token.rstrip(".")  # A sentence full stop is not part of a TikTok username.
            try:
                handle = normalize_handle(token)
            except ValueError:
                continue
            if (
                handle in {"_all", "all"}
                or re.fullmatch(r"_user_\d+", handle)
                or (pattern is _AT and raw_token in {"U1", "CM1"})
            ):
                continue
            if pattern is _DECLARATION and handle in _RESERVED:
                continue
            occupied.append(match.span())
            if pattern is _URL:
                render = f"https://www.tiktok.com/@{handle}"
            elif pattern in {_DECLARATION, _GROUP_NAME}:
                render = match[0].replace(raw_token, handle, 1)
            else:
                render = "@" + handle
            clues.append({"handle": handle, "span": match.span(), "render": render})
    return sorted(clues, key=lambda item: item["span"])

def _name_handles(name):
    return sorted({c["handle"] for c in _clues(name, group_name=True)})

def _body(raw, row, aliases, names, *, group_name=False):
    targets = mention_targets(row.get("mentions_json"))
    mention_aliases = {t["open_id"]: aliases.get(t["open_id"], "其他用户") for t in targets if t["open_id"]}

    def pieces(value):
        if isinstance(value, dict):
            locales = {key: child for key, child in value.items() if re.fullmatch(r"[a-z]{2}_[a-z]{2}", key)}
            if locales:
                # Lark post locales repeat the same post. Preserve a variant with account
                # changes/extra handles; otherwise prefer English, then one available locale.
                variants = [(key, list(pieces(child))) for key, child in locales.items()]
                variants = [(key, parts) for key, parts in variants if any(p.strip() for p in parts)]
                if variants:
                    chosen = max(
                        variants,
                        key=lambda v: (
                            bool(_CHANGE.search("\n".join(v[1]))),
                            len(_clues("\n".join(v[1]))),
                            v[0] == "en_us",
                            v[0].startswith("en_"),
                        ),
                    )
                    yield from chosen[1]
                    known = {c["handle"] for c in _clues("\n".join(chosen[1]))}
                    for key, parts in variants:
                        extra = {c["handle"] for c in _clues("\n".join(parts))} - known
                        if key != chosen[0] and extra:
                            yield from parts  # Do not erase conflicting account tokens as a translation duplicate.
                            known.update(extra)
                return
            if value.get("tag") == "at":
                if value.get("user_id") in {"all", "_all"}:
                    yield "@_all"
                    return
                yield rich_mention_text(value.get("user_id"), row.get("mentions_json"), mention_aliases)
                return
            for key, child in value.items():
                if key in {"text", "href", "url", "title"} and isinstance(child, str):
                    yield child
                elif isinstance(child, list | dict):
                    yield from pieces(child)
        elif isinstance(value, list):
            for child in value:
                yield from pieces(child)

    if row["message_type"] == "text":
        text = people._readable_body(raw, "text")
    else:
        try:
            text = "\n".join(dict.fromkeys(p.strip() for p in pieces(json.loads(raw)) if p.strip()))
        except ValueError:
            text = ""
    safe_mentions = [
        {
            "key": t["key"],
            "id": t["open_id"] or "",
            "id_type": "open_id",
            "name": mention_aliases.get(t["open_id"], "未解析的@用户"),
        }
        for t in targets
    ]
    text = resolve_mentions(text, safe_mentions, mention_aliases)

    def hide_code(match):
        token = (match[2] or match[0]).strip().lstrip("#")
        opaque = len(token) >= 24 and any(c in "+/=" for c in token) and any(c.isalpha() for c in token)
        explicit = bool(match[1]) and any(c.isdigit() for c in token)
        if opaque or explicit:
            return (match[1] or "") + "[已提供广告授权码]"
        if not match[1] or len(token) >= 24 or token.isupper():
            return (match[1] or "") + "[编码已隐藏]"
        return match[0]

    text = _AD_CODE.sub(hide_code, text)
    # Reuse PII patterns but protect exact account spans: digits/dots must not become a phone/name guess.
    clues, replacements = _clues(text, group_name=group_name), {}
    protected = text
    salt = hashlib.sha256(raw.encode()).hexdigest()[:8]
    for index, clue in reversed(list(enumerate(clues))):
        marker = f"zzHANDLE{index}_{salt}zz"
        replacements[marker] = clue["render"]
        start, end = clue["span"]
        protected = protected[:start] + marker + protected[end:]
    text = people._EMAIL.sub("[邮箱已隐藏]", protected)
    text = people._ADDRESS.sub("[地址已隐藏]", people._PHONE.sub("[电话/编号已隐藏]", text))
    for name, alias in names.items():
        if len(name.strip()) > 2:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", alias, text, flags=re.I)

    def safe_url(match):
        try:
            url = urlsplit(match[0])
            return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
        except ValueError:
            return "[链接]"

    text = re.sub(r"https?://[^\s<>]+", safe_url, text)
    for marker, handle_text in replacements.items():
        text = text.replace(marker, handle_text)
    return text

def _members(conn, cm_id, chat_ids, aliases):
    members = conn.execute(
        """SELECT member.chat_id,member.open_id,member.member_type,identity.contact_id,
                  identity.identity_type,identity.tenant_classification
           FROM chat_members member LEFT JOIN lark_identities identity ON identity.open_id=member.open_id
           WHERE member.chat_id=ANY(%s::text[]) ORDER BY member.chat_id,member.open_id""",
        (chat_ids,),
    ).fetchall()
    others = conn.execute(
        "SELECT DISTINCT m.chat_id,m.sender_open_id AS open_id,m.sender_type AS member_type,"
        "identity.contact_id,identity.identity_type,identity.tenant_classification "
        + ownership._READABLE.replace(
            "WHERE a.id=%s", "LEFT JOIN lark_identities identity ON identity.open_id=m.sender_open_id WHERE a.id=%s"
        )
        + " AND m.chat_id=ANY(%s::text[]) AND NOT (m.sender_open_id=ANY(%s::text[])) "
        "ORDER BY m.chat_id,open_id",
        (cm_id, chat_ids, list(aliases)),
    ).fetchall()
    return members, others
