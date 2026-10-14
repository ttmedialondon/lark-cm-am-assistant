from contextlib import nullcontext
from uuid import uuid4

from app import organization_sync


def test_associated_organization_fetch_follows_lark_page_tokens() -> None:
    class Client:
        def __init__(self):
            self.tokens = []

        def list_collaboration_tenants(self, _token, page_token=None):
            self.tokens.append(page_token)
            if page_token is None:
                return {"has_more": True, "page_token": "second", "items": [
                    {"tenant_key": "a", "name": {"default_value": "A"}},
                    {"tenant_key": "unnamed"}, {"name": "missing tenant"},
                ]}
            return {"has_more": False, "items": [{"tenant_key": "b", "short_name": "B"}]}

    client = Client()

    rows, tenant_keys = organization_sync._fetch_all(client, "tenant-token")

    assert rows == [
        {"tenant_key": "a", "name": "A"},
        {"tenant_key": "b", "name": "B"},
    ]
    assert tenant_keys == {"a", "b", "unnamed"}
    assert client.tokens == [None, "second"]




def test_manual_tenant_mapping_uses_the_identity_opened_in_the_admin_page(monkeypatch) -> None:
    calls = []
    contact_id = uuid4()
    organization_id = uuid4()

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT tenant_key" in query:
                return Result({"tenant_key": "tenant-selected"})
            if "INSERT INTO organizations" in query:
                return Result({"id": organization_id})
            return Result()

    monkeypatch.setattr(organization_sync, "connection", lambda: nullcontext(Connection()))

    organization_sync.manually_confirm_contact_organization(
        uuid4(), contact_id, "ou-selected", "TT MEDIA ITALY", "mcn_or_agency", map_current_tenant=True
    )

    identity_query, identity_params = calls[0]
    assert "open_id = %s" in identity_query
    assert identity_params == (contact_id, "ou-selected")
