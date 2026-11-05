"""One user must never read or act on another user's objects (IDOR)."""

import pytest
from social_publisher.models import Publication

from tests.factories import ContentItemFactory, NicheFactory, UserFactory, connect_account
from tests.helpers import generate_ready_item

pytestmark = pytest.mark.django_db


@pytest.fixture
def victim_objects(fake_render):
    victim = UserFactory()
    niche = NicheFactory(owner=victim)
    item = generate_ready_item(victim, niche)
    account = connect_account(victim)
    publication = Publication.objects.create(
        account=account, idempotency_key="k", marker="m", request_fingerprint="f"
    )
    return {"niche": niche, "item": item, "account": account, "publication": publication}


def test_read_endpoints_return_404_for_other_users(other_client, victim_objects):
    objects = victim_objects
    for path in (
        f"/api/v1/niches/{objects['niche'].pk}/",
        f"/api/v1/content/{objects['item'].pk}/",
        f"/api/v1/social/accounts/{objects['account'].pk}/",
        f"/api/v1/social/accounts/{objects['account'].pk}/capabilities/",
        f"/api/v1/social/publications/{objects['publication'].pk}/",
    ):
        assert other_client.get(path).status_code == 404, path


def test_lists_show_only_own_objects(other_client, victim_objects):
    for path in (
        "/api/v1/niches/",
        "/api/v1/content/",
        "/api/v1/social/accounts/",
        "/api/v1/social/publications/",
    ):
        assert other_client.get(path).data["results"] == [], path


def test_actions_return_404_and_change_nothing(other_client, victim_objects):
    item = victim_objects["item"]
    before = item.status
    for action, body in (
        ("approve", {"account": str(victim_objects["account"].pk), "made_for_kids": False}),
        ("reject", {}),
        ("retry", {}),
        ("disclosure", {"contains_synthetic_media": False, "confirm": True}),
    ):
        response = other_client.post(f"/api/v1/content/{item.pk}/{action}/", body, format="json")
        assert response.status_code == 404, action
    item.refresh_from_db()
    assert item.status == before
    assert item.contains_synthetic_media is True


def test_niche_cannot_be_changed_or_deleted_by_others(other_client, victim_objects):
    url = f"/api/v1/niches/{victim_objects['niche'].pk}/"
    assert other_client.patch(url, {"name": "x"}, format="json").status_code == 404
    assert other_client.delete(url).status_code == 404


def test_disconnect_by_other_user_is_404(other_client, victim_objects):
    account = victim_objects["account"]
    response = other_client.post(f"/api/v1/social/accounts/{account.pk}/disconnect/")
    assert response.status_code == 404
    account.refresh_from_db()
    assert account.status == "active"


def test_cannot_generate_from_another_users_niche(other_client, victim_objects):
    response = other_client.post(
        "/api/v1/content/",
        {"niche": str(victim_objects["niche"].pk), "topic": "Stolen"},
        format="json",
    )
    assert response.status_code == 400


def test_cannot_approve_own_item_onto_another_users_account(client, user, fake_render):
    item = generate_ready_item(user, NicheFactory(owner=user))
    other_account = connect_account(UserFactory())
    response = client.post(
        f"/api/v1/content/{item.pk}/approve/",
        {"account": str(other_account.pk), "made_for_kids": False},
        format="json",
    )
    assert response.status_code == 400
    item.refresh_from_db()
    assert item.status == "awaiting_review"


def test_usage_counts_only_own_spend(client, user, fake_render):
    generate_ready_item(UserFactory(), NicheFactory())
    response = client.get("/api/v1/usage/")
    assert response.data["cost_usd"]["total"] == 0


def test_create_flow_through_api_runs_pipeline(
    client, user, fake_render, django_capture_on_commit_callbacks
):
    niche = NicheFactory(owner=user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            "/api/v1/content/", {"niche": str(niche.pk), "topic": "Black holes"}, format="json"
        )
    assert response.status_code == 201
    detail = client.get(f"/api/v1/content/{response.data['id']}/").data
    assert detail["status"] == "awaiting_review"
    assert detail["script"]["sources_verified"] is False
    assert detail["script"]["sources"]
    assert detail["clips"][0]["licence"]
    assert detail["preview_url"]
    usage = client.get("/api/v1/usage/").data
    assert usage["cost_usd"]["total"] > 0


def test_unused_factory_guard():
    assert ContentItemFactory._meta.model.__name__ == "ContentItem"
