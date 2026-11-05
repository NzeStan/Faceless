from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from django.utils import timezone

from social_publisher import services
from social_publisher.adapters.youtube import TOKEN_URL
from social_publisher.errors import AuthExpired, OAuthStateInvalid, PlatformRejected
from social_publisher.models import ConnectedAccount, OAuthState, OAuthToken
from tests.helpers import ACCESS, REFRESH, fixture, make_account, make_user

pytestmark = pytest.mark.django_db
CHANNELS = "https://www.googleapis.com/youtube/v3/channels"


def _start(owner=None):
    owner = owner or make_user()
    url = services.start_connection(owner, "youtube")
    return owner, parse_qs(urlsplit(url).query)


def _mock_google():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=fixture("token_response")))
    respx.get(CHANNELS).mock(return_value=httpx.Response(200, json=fixture("channels_mine")))


def test_authorization_url_uses_pkce_offline_and_minimum_scopes():
    _, query = _start()
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert set(query["scope"][0].split()) == {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    }
    assert OAuthState.objects.get().state == query["state"][0]


@respx.mock
def test_complete_connection_stores_encrypted_tokens():
    owner, query = _start()
    _mock_google()
    account = services.complete_connection(query["state"][0], "code", owner=owner)
    assert account.status == "active"
    assert account.display_name == "Test Channel"
    record = OAuthToken.objects.get(account=account)
    assert ACCESS not in record.access_token_encrypted
    assert REFRESH not in record.refresh_token_encrypted
    assert record.get_bundle().access_token == ACCESS


@respx.mock
def test_state_cannot_be_reused():
    owner, query = _start()
    _mock_google()
    services.complete_connection(query["state"][0], "code", owner=owner)
    with pytest.raises(OAuthStateInvalid):
        services.complete_connection(query["state"][0], "code", owner=owner)


def test_expired_state_is_rejected():
    owner, query = _start()
    OAuthState.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(OAuthStateInvalid):
        services.complete_connection(query["state"][0], "code", owner=owner)


def test_unknown_state_is_rejected():
    with pytest.raises(OAuthStateInvalid):
        services.complete_connection("nope", "code")


def test_other_owner_cannot_complete_and_cannot_burn_the_state():
    owner, query = _start()
    with pytest.raises(OAuthStateInvalid):
        services.complete_connection(query["state"][0], "code", owner=make_user("attacker"))
    assert OAuthState.objects.get().consumed_at is None


@respx.mock
def test_failed_exchange_still_consumes_state():
    owner, query = _start()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(PlatformRejected):
        services.complete_connection(query["state"][0], "bad", owner=owner)
    assert OAuthState.objects.get().consumed_at is not None
    assert not ConnectedAccount.objects.exists()


@respx.mock
def test_invalid_grant_on_refresh_marks_needs_reauth():
    account = make_account(expires_in=-10)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthExpired):
        services.check_account_health(account)
    account.refresh_from_db()
    assert account.status == "needs_reauth"


@respx.mock
def test_refresh_keeps_existing_refresh_token():
    account = make_account(expires_in=-10)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )
    respx.get(CHANNELS).mock(return_value=httpx.Response(200, json=fixture("channels_mine")))
    services.check_account_health(account)
    bundle = OAuthToken.objects.get(account=account).get_bundle()
    assert bundle.access_token == "new"
    assert bundle.refresh_token == REFRESH


@respx.mock
def test_disconnect_revokes_and_deletes_tokens():
    account = make_account()
    revoke = respx.post("https://oauth2.googleapis.com/revoke").mock(
        return_value=httpx.Response(200)
    )
    assert services.disconnect(account) is True
    assert revoke.called
    assert REFRESH.encode() not in revoke.calls[0].request.url.query
    assert not OAuthToken.objects.exists()
    account.refresh_from_db()
    assert account.status == "revoked"


@respx.mock
def test_disconnect_reports_unconfirmed_revocation_but_still_deletes():
    account = make_account()
    respx.post("https://oauth2.googleapis.com/revoke").mock(return_value=httpx.Response(500))
    assert services.disconnect(account) is False
    assert not OAuthToken.objects.exists()
