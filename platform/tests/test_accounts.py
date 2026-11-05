import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

pytestmark = pytest.mark.django_db
PASSWORD = "a-Strong-pass-123"


def test_register_login_refresh_rotates_and_me():
    api = APIClient()
    created = api.post(
        "/api/v1/auth/register/", {"email": "New@Example.com", "password": PASSWORD}, format="json"
    )
    assert created.status_code == 201
    assert "password" not in created.data
    token = api.post(
        "/api/v1/auth/token/", {"email": "new@example.com", "password": PASSWORD}, format="json"
    )
    assert token.status_code == 200
    refreshed = api.post(
        "/api/v1/auth/token/refresh/", {"refresh": token.data["refresh"]}, format="json"
    )
    assert refreshed.status_code == 200
    assert refreshed.data["refresh"] != token.data["refresh"]
    reused = api.post(
        "/api/v1/auth/token/refresh/", {"refresh": token.data["refresh"]}, format="json"
    )
    assert reused.status_code == 401
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
    assert api.get("/api/v1/auth/me/").data["email"] == "new@example.com"


def test_weak_password_and_duplicate_email_are_rejected():
    api = APIClient()
    weak = api.post("/api/v1/auth/register/", {"email": "a@b.co", "password": "123"}, format="json")
    assert weak.status_code == 400
    assert (
        api.post(
            "/api/v1/auth/register/", {"email": "a@b.co", "password": PASSWORD}, format="json"
        ).status_code
        == 201
    )
    assert (
        api.post(
            "/api/v1/auth/register/", {"email": "A@B.CO", "password": PASSWORD}, format="json"
        ).status_code
        == 400
    )


def test_endpoints_require_authentication():
    api = APIClient()
    for path in (
        "/api/v1/niches/",
        "/api/v1/content/",
        "/api/v1/usage/",
        "/api/v1/social/accounts/",
    ):
        assert api.get(path).status_code == 401


def test_auth_endpoints_are_rate_limited(monkeypatch):
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "2/min"})
    api = APIClient()
    codes = [
        api.post(
            "/api/v1/auth/token/", {"email": "x@y.z", "password": "no"}, format="json"
        ).status_code
        for _ in range(3)
    ]
    assert codes[-1] == 429


def test_openapi_schema_builds(client):
    response = client.get("/api/v1/schema/")
    assert response.status_code == 200
