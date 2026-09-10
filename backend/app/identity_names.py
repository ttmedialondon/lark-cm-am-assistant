"""One display convention; source fields and stable identity keys remain separate."""

from __future__ import annotations


def _raw(value: str | None) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def format_display_name(name: str | None, en_name: str | None) -> str | None:
    """Show English (original), or one name when missing/equivalent."""
    original = " ".join((_raw(name) or "").split())
    english = " ".join((_raw(en_name) or "").split())
    if not original or not english or original.casefold() == english.casefold():
        return english or original or None
    return f"{english}（{original}）"


def update_identity_names(
    conn,
    open_id: str,
    *,
    name: str | None = None,
    en_name: str | None = None,
    en_name_present: bool = False,
    authoritative: bool = False,
) -> str | None:
    """Update one existing identity and its cached labels in the caller transaction.

    Structured profiles/member records may update source fields. Old messages
    and mentions only fill a missing original name, never overwrite a known
    profile. Omitted English preserves the cache; explicit empty clears it.
    Authorization labels are updated ONLY by the OAuth/profile caller, which
    locks that authorization before identities. Updating them from a page
    commit would invert the existing authorization→sync-job lock order.
    """
    row = conn.execute(
        "SELECT name,en_name,display_name,contact_id FROM lark_identities WHERE open_id=%s FOR UPDATE",
        (open_id,),
    ).fetchone()
    if not row:
        return None
    original, english = row["name"], row["en_name"]
    incoming_name = _raw(name)
    if incoming_name and (authoritative or not _raw(original)):
        original = incoming_name
    if authoritative and en_name_present:
        english = _raw(en_name)
    display = format_display_name(original, english)
    if not display and not (authoritative and en_name_present):
        # Preserve an unknown legacy label without treating a composite as raw.
        display = row["display_name"]
    conn.execute(
        """UPDATE lark_identities SET name=%s,en_name=%s,display_name=%s
           WHERE open_id=%s AND (name,en_name,display_name) IS DISTINCT FROM (%s,%s,%s)""",
        (original, english, display, open_id, original, english, display),
    )
    conn.execute(
        """UPDATE chat_members SET display_name=%s
           WHERE open_id=%s AND display_name IS DISTINCT FROM %s""",
        (display, open_id, display),
    )
    if row["contact_id"]:
        # ponytail: only an unambiguous single-identity contact inherits a name;
        # preserve multi-ID/manual labels until a primary identity is explicit.
        conn.execute(
            """UPDATE contacts SET display_name=%s,updated_at=NOW()
               WHERE id=%s AND display_name IS DISTINCT FROM %s
                 AND NOT EXISTS (SELECT 1 FROM lark_identities
                     WHERE contact_id=%s AND open_id<>%s)""",
            (display, row["contact_id"], display, row["contact_id"], open_id),
        )
    return display
