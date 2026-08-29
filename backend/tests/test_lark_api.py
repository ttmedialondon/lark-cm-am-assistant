from app.config import Settings
from app.lark_api import LarkClient


def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_redirect_uri="https://larkbot.ttapp.help/auth/lark/callback",
        token_encryption_key=b"a" * 32,
        oauth_authorize_url="https://accounts.larksuite.com/open-apis/authen/v1/authorize",
        oauth_token_url="https://open.larksuite.com/open-apis/authen/v2/oauth/token",
        user_info_url="https://open.larksuite.com/open-apis/authen/v1/user_info",
        lark_api_base_url="https://open.larksuite.com",
        oauth_scopes=("offline_access", "im:chat:read"),
        sync_lookback_days=0,
        sync_interval_seconds=180,
        sync_overlap_seconds=600,
        max_pages_per_job=30,
        worker_idle_seconds=3,
    )


def test_authorization_url_uses_oauth_client_id_and_pkce() -> None:
    url = LarkClient(settings()).authorization_url("state value", "challenge-value")

    assert "client_id=cli_test" in url
    assert "app_id=" not in url
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "scope=offline_access+im%3Achat%3Aread" in url
