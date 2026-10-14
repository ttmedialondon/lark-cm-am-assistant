from types import SimpleNamespace

from app.message_mentions import mention_names, mention_targets, resolve_mentions


def mention(key="@_user_1", identity="ou_cm", name="Quanzhen Liang", id_type="open_id"):
    return {"key": key, "id": identity, "id_type": id_type, "name": name}


def test_exact_tokens_current_names_repeated_and_legitimate_numeric_user():
    mentions = [mention(), mention("@_user_10", "ou_numeric", "user805550")]
    text = "Hi @_user_1, @_user_10 and @_user_1. @_user_100 / @_user_1suffix"
    assert resolve_mentions(text, mentions, {"ou_cm": "Remi（Quanzhen Liang）"}) == (
        "Hi @Remi（Quanzhen Liang）, @user805550 and @Remi（Quanzhen Liang）. @_user_100 / @_user_1suffix"
    )


def test_chinese_prose_can_touch_mentions_but_ascii_identifiers_cannot():
    mentions = [mention(), mention("@_user_10", "ou_numeric", "user805550")]
    assert resolve_mentions("请@_user_1帮忙，@_user_10确认。辛苦@_user_1", mentions, {"ou_cm": "Remi"}) == (
        "请@Remi帮忙，@user805550确认。辛苦@Remi"
    )
    literal = "prefix@_user_1 @_user_1suffix @_user_1_more @@_user_1 @_user_100"
    assert resolve_mentions(literal, mentions) == literal
    # Keep Unicode whitespace as a URL separator, not part of a URL literal.
    assert resolve_mentions("看https://example.test/@_user_1\u3000请@_user_1帮忙", mentions, {"ou_cm": "Remi"}) == (
        "看https://example.test/@_user_1\u3000请@Remi帮忙"
    )


def test_message_literals_are_preserved_while_prose_mentions_resolve():
    text = ("查看https://example.test/@_user_1?q=1 person@_user_1.example `@_user_1`\n"
            "```python\nprint('@_user_1')\n```\n")
    assert resolve_mentions(text + "请@_user_1帮忙", [mention()]) == text + "请@Quanzhen Liang帮忙"


def test_duplicate_target_conflict_and_unknown_do_not_guess():
    duplicate = [mention(), mention()]
    assert resolve_mentions("@_user_1", duplicate) == "@Quanzhen Liang"
    conflict = [mention(), mention(identity="ou_other")]
    assert resolve_mentions("@_user_1", conflict, {"ou_cm": "Same", "ou_other": "Same"}) == "@_user_1"
    assert mention_targets(conflict)[0] == {
        "key": "@_user_1", "open_id": None, "name": None, "ambiguous": True
    }
    assert resolve_mentions("@_user_1", [mention(name="")]) == "@_user_1"
    assert resolve_mentions("@_user_1", [mention(name="@_user_1")]) == "@_user_1"
    different_names = [mention(name="One"), mention(name="Two")]
    assert resolve_mentions("@_user_1", different_names) == "@_user_1"
    assert resolve_mentions("@_user_1", different_names, {"ou_cm": "Canonical"}) == "@Canonical"


def test_non_open_id_only_uses_same_message_name_not_directory_identity():
    target = mention_targets([mention(id_type="user_id")], {"ou_cm": "Wrong lookup"})[0]
    assert target["open_id"] is None and target["name"] == "Quanzhen Liang"
    assert resolve_mentions("@_user_1", [mention(id_type="user_id")], {"ou_cm": "Wrong lookup"}) == "@Quanzhen Liang"
    for invalid in (None, 123, "", " ou_cm "):
        assert mention_targets([mention(identity=invalid)])[0]["open_id"] is None


def test_batch_lookup_is_only_explicit_metadata_ids_not_senders_or_other_rows():
    queries = []

    def execute(sql, params):
        queries.append((sql, params))
        return SimpleNamespace(fetchall=lambda: [{"open_id": "ou_cm", "display_name": "Remi（Quanzhen Liang）"}])

    conn = SimpleNamespace(execute=execute)
    rows = [
        {"sender_open_id": "ou_never_lookup", "mentions_json": [mention(), mention()]},
        {"mentions_json": [mention(identity="ou_nonopenid", id_type="union_id")]},
    ]
    assert mention_names(conn, rows) == {"ou_cm": "Remi（Quanzhen Liang）"}
    assert len(queries) == 1 and queries[0][1] == (["ou_cm"],)
    for invalid in (None, "bad JSON", {}, [None], [mention(id_type="user_id")]):
        assert mention_names(conn, [{"mentions_json": invalid}]) == {}
    assert len(queries) == 1

    assert "FOR SHARE" in queries[0][0]
    assert mention_names(conn, rows, lock=False) == {"ou_cm": "Remi（Quanzhen Liang）"}
    assert "FOR SHARE" not in queries[-1][0]
