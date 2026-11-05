import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

pytest.importorskip("rest_framework")

from rest_framework.test import APIClient

from social_publisher import services
from social_publisher.adapters.youtube import TOKEN_URL, UPLOAD_URL
from social_publisher.contrib.drf.serializers import (
    ConnectedAccountSerializer,
    PublicationSerializer,
)
from tests.helpers import (
    ACCESS,
    REFRESH,
    SESSION_URI,
    fixture,
    make_account,
    make_user,
    media_spec,
    metadata,
)

pytestmark = pytest.mark.django_db


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@respx.mock
def test_connect_flow_through_api_and_tokens_absent_from_responses():
    user = make_user()
    client = _client(user)
    start = client.post("/social/connections/start/", {"platform": "youtube"})
    assert start.status_code == 201
    state = parse_qs(urlsplit(start.data["authorization_url"]).query)["state"][0]
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=fixture("token_response")))
    respx.get("https://www.googleapis.com/youtube/v3/channels").mock(
        return_value=httpx.Response(200, json=fixture("channels_mine"))
    )
    done = client.post("/social/connections/complete/", {"state": state, "code": "abc"})
    assert done.status_code == 201
    listing = client.get("/social/accounts/")
    for response in (done, listing):
        text = json.dumps(response.data, default=str)
        assert ACCESS not in text
        assert REFRESH not in text


def test_unknown_platform_is_a_clean_error():
    response = _client(make_user()).post("/social/connections/start/", {"platform": "nope"})
    assert response.status_code == 400


def test_invalid_state_returns_typed_error():
    response = _client(make_user()).post(
        "/social/connections/complete/", {"state": "bad", "code": "x"}
    )
    assert response.status_code == 400
    assert response.data["code"] == "oauth_state_invalid"


@pytest.mark.parametrize(
    "path",
    ["accounts/{a}/", "accounts/{a}/capabilities/", "publications/{p}/"],
)
@respx.mock
def test_other_users_objects_are_not_found(path):
    owner = make_user("owner")
    account = make_account(owner)
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": SESSION_URI}))
    respx.put(SESSION_URI).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": "bytes=0-262143"}),
            httpx.Response(201, json=fixture("video_inserted")),
        ]
    )
    publication = services.publish(account, media_spec(), metadata(), "k")
    url = "/social/" + path.format(a=account.pk, p=publication.pk)
    assert _client(owner).get(url).status_code == 200
    assert _client(make_user("other")).get(url).status_code == 404


def test_other_user_cannot_disconnect():
    account = make_account(make_user("owner"))
    response = _client(make_user("other")).post(f"/social/accounts/{account.pk}/disconnect/")
    assert response.status_code == 404
    account.refresh_from_db()
    assert account.status == "active"


def test_capabilities_endpoint_explains_private_uploads():
    owner = make_user()
    account = make_account(owner)
    data = _client(owner).get(f"/social/accounts/{account.pk}/capabilities/").data
    assert data["public_visibility"]["available"] is False
    assert data["public_visibility"]["reason"]
    assert data["daily_limits"]["uploads"] == 3


def test_serializers_expose_only_allowlisted_fields():
    forbidden = {"oauth_token", "access_token_encrypted", "refresh_token_encrypted"}
    assert not forbidden & set(ConnectedAccountSerializer.Meta.fields)
    assert "upload_session_encrypted" not in PublicationSerializer.Meta.fields
    assert "raw_response_excerpt" not in PublicationSerializer.Meta.fields
