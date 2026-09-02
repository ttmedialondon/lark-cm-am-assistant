from app import organization_sync


def test_associated_organization_rows_keep_only_stable_tenant_mappings() -> None:
    rows = organization_sync._organization_rows(
        {
            "items": [
                {"tenant_key": "tenant_italy", "name": {"default_value": "TT MEDIA ITALY"}},
                {"tenant_key": "tenant_short", "short_name": "Trutonic Media"},
                {"tenant_key": "missing_name"},
                {"name": "missing tenant"},
            ]
        }
    )

    assert rows == [
        {"tenant_key": "tenant_italy", "name": "TT MEDIA ITALY"},
        {"tenant_key": "tenant_short", "name": "Trutonic Media"},
    ]


def test_associated_organization_fetch_follows_lark_page_tokens() -> None:
    class Client:
        def __init__(self):
            self.tokens = []

        def list_collaboration_tenants(self, _token, page_token=None):
            self.tokens.append(page_token)
            if page_token is None:
                return {"has_more": True, "page_token": "second", "items": [{"tenant_key": "a", "name": "A"}]}
            return {"has_more": False, "items": [{"tenant_key": "b", "name": "B"}]}

    client = Client()

    rows, tenant_keys = organization_sync._fetch_all(client, "tenant-token")

    assert rows == [
        {"tenant_key": "a", "name": "A"},
        {"tenant_key": "b", "name": "B"},
    ]
    assert tenant_keys == {"a", "b"}
    assert client.tokens == [None, "second"]


def test_associated_organization_keys_remain_visible_when_lark_omits_a_name() -> None:
    assert organization_sync._organization_tenant_keys(
        {"items": [{"tenant_key": "known"}, {"tenant_key": "also-known", "name": "Name"}]}
    ) == {"known", "also-known"}
