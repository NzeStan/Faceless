import json
from datetime import timedelta

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from social_video_publisher.adapters.youtube import TOKEN_URL, UPLOAD_URL
from social_video_publisher.models import Publication, QuotaLedger

from apps.content.models import ContentItem
from apps.publishing import services as publishing
from apps.publishing import tasks as publishing_tasks
from tests.factories import ACCESS_TOKEN, REFRESH_TOKEN, NicheFactory, connect_account
from tests.helpers import generate_ready_item

pytestmark = pytest.mark.django_db
Status = ContentItem.Status
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=PLAT1"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST = "https://www.googleapis.com/youtube/v3/playlistItems"
CHUNK = 256 * 1024
INSERTED = {"id": "VID1", "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}}
PROCESSED = {
    "items": [
        {
            "id": "VID1",
            "status": {"uploadStatus": "processed", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "succeeded"},
        }
    ]
}


class WorkerKilled(BaseException):
    """Simulates a process dying: no ordinary exception handling runs."""


def _sequence(*steps):
    """A respx side effect that can also raise BaseException, which respx lists cannot do."""
    remaining = list(steps)

    def respond(request):
        step = remaining.pop(0)
        if isinstance(step, BaseException) or (
            isinstance(step, type) and issubclass(step, BaseException)
        ):
            raise step
        return step

    return respond


def _ready(user, fake_render, **kwargs):
    return generate_ready_item(user, NicheFactory(owner=user), **kwargs)


def _approve(client, item, account, **extra):
    body = {"account": str(account.pk), "made_for_kids": False, **extra}
    return client.post(f"/api/v1/content/{item.pk}/approve/", body, format="json")


def _mock_initiate():
    return respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": SESSION})
    )


def _mock_upload_ok():
    return respx.put(SESSION).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"}),
            httpx.Response(201, json=INSERTED),
        ]
    )


def _approved_in_db(item, account):
    """Approve without running the eager upload, leaving the item queued."""
    with _no_enqueue():
        publishing.approve(
            item, actor=item.owner, account=account, visibility="public", made_for_kids=False
        )
    item.refresh_from_db()
    return item


class _no_enqueue:
    def __enter__(self):
        self.original = publishing._enqueue_publish
        publishing._enqueue_publish = lambda item_id: None

    def __exit__(self, *exc):
        publishing._enqueue_publish = self.original


@respx.mock
def test_full_flow_is_published_only_after_videos_list_confirms(
    user, client, fake_render, django_capture_on_commit_callbacks
):
    item = _ready(user, fake_render)
    account = connect_account(user)
    post = _mock_initiate()
    _mock_upload_ok()
    with django_capture_on_commit_callbacks(execute=True):
        response = _approve(client, item, account, visibility="public")
    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == Status.UPLOADING
    assert item.publication.status == "processing"
    assert post.call_count == 1

    respx.get(VIDEOS).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "VID1", "status": {"uploadStatus": "uploaded"}}]},
        )
    )
    publishing.reconcile(item)
    item.refresh_from_db()
    assert item.status == Status.UPLOADING

    respx.get(VIDEOS).mock(return_value=httpx.Response(200, json=PROCESSED))
    publishing.reconcile(item)
    item.refresh_from_db()
    assert item.status == Status.PUBLISHED
    assert item.publication.url.endswith("VID1")


@respx.mock
def test_unaudited_mode_forces_private_and_explains_why(user, fake_render):
    assert settings.YOUTUBE_PROJECT_AUDITED is False
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    assert item.requested_visibility == "public"
    post = _mock_initiate()
    _mock_upload_ok()
    publishing.publish(item)
    body = json.loads(post.calls[0].request.content)
    assert body["status"]["privacyStatus"] == "private"
    item.refresh_from_db()
    assert item.publication.visibility == "private"
    assert "not audited" in item.status_message
    summary = publishing.status_summary()
    assert summary["audit_status"] == "unaudited"
    assert summary["uploads_forced_private"] is True


@respx.mock
def test_disclosure_defaults_on_and_is_always_sent(user, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    post = _mock_initiate()
    _mock_upload_ok()
    publishing.publish(item)
    assert json.loads(post.calls[0].request.content)["status"]["containsSyntheticMedia"] is True


def test_disclosure_off_requires_confirmation_and_is_audited(user, client, fake_render):
    item = _ready(user, fake_render)
    url = f"/api/v1/content/{item.pk}/disclosure/"
    refused = client.post(url, {"contains_synthetic_media": False}, format="json")
    assert refused.status_code == 400
    item.refresh_from_db()
    assert item.contains_synthetic_media is True
    accepted = client.post(url, {"contains_synthetic_media": False, "confirm": True}, format="json")
    assert accepted.status_code == 200
    item.refresh_from_db()
    assert item.contains_synthetic_media is False
    event = item.events.get(kind="disclosure_changed")
    assert event.actor == user


@respx.mock
def test_crashed_upload_resumes_and_never_creates_a_duplicate(user, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    post = _mock_initiate()
    put = respx.put(SESSION).mock(
        side_effect=_sequence(
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"}), WorkerKilled()
        )
    )
    with pytest.raises(WorkerKilled):
        publishing.publish(item)
    item.refresh_from_db()
    assert item.status == Status.UPLOADING

    # A worker comes back while the first lease is live: nothing happens.
    publishing_tasks.publish_item.apply(args=[str(item.pk)])
    assert post.call_count == 1

    Publication.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    put.side_effect = [
        httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"}),
        httpx.Response(201, json=INSERTED),
    ]
    earlier = put.call_count
    publishing.publish(item)
    item.refresh_from_db()
    assert item.publication.status == "processing"
    assert item.publication.platform_media_id == "VID1"
    assert post.call_count == 1
    assert Publication.objects.count() == 1
    assert put.calls[earlier].request.headers["Content-Range"] == f"bytes */{512 * 1024}"
    assert put.calls[earlier + 1].request.headers["Content-Range"].startswith(f"bytes {CHUNK}-")


@respx.mock
def test_lost_connection_reconciles_by_marker_and_never_uploads_again(user, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    post = _mock_initiate()
    respx.put(SESSION).mock(side_effect=httpx.ReadTimeout("gone"))
    publishing.publish(item)
    item.refresh_from_db()
    assert item.status == Status.UPLOADING
    assert item.status_detail == "outcome_unknown"
    publication = item.publication

    respx.get(CHANNELS).mock(
        return_value=httpx.Response(
            200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        )
    )
    respx.get(PLAYLIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {
                            "description": f"text [ref:{publication.marker}]",
                            "resourceId": {"videoId": "VID1"},
                        }
                    }
                ]
            },
        )
    )
    respx.get(VIDEOS).mock(return_value=httpx.Response(200, json=PROCESSED))
    # The sweep drives reconciliation, and a repeat publish changes nothing.
    publishing_tasks.sweep.apply()
    item.refresh_from_db()
    assert item.status == Status.PUBLISHED
    assert post.call_count == 1


@respx.mock
def test_quota_exhaustion_defers_instead_of_failing(user, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    post = _mock_initiate()
    today_limit = settings.SOCIAL_VIDEO_PUBLISHER["PLATFORMS"]["youtube"]["DAILY_UPLOAD_LIMIT"]
    from social_video_publisher import quota

    QuotaLedger.objects.create(
        platform="youtube", bucket="uploads", day=quota.pacific_day(), units_used=today_limit
    )
    publishing.publish(item)
    item.refresh_from_db()
    assert item.status == Status.QUEUED
    assert item.status_detail == "quota_exhausted"
    assert item.next_attempt_at > timezone.now()
    assert post.call_count == 0

    # The sweep leaves it alone until reset, then uploads under the same key.
    publishing_tasks.sweep.apply()
    assert post.call_count == 0
    QuotaLedger.objects.all().delete()
    ContentItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
    _mock_upload_ok()
    publishing_tasks.sweep.apply()
    item.refresh_from_db()
    assert item.status == Status.UPLOADING
    assert item.publication.status == "processing"
    assert post.call_count == 1


@respx.mock
def test_expired_authorisation_blocks_without_failing(user, fake_render):
    account = connect_account(user)
    item = _approved_in_db(_ready(user, fake_render), account)
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    publishing.publish(item)
    item.refresh_from_db()
    account.refresh_from_db()
    assert item.status == Status.QUEUED
    assert item.status_detail == "reconnect_required"
    assert account.status == "needs_reauth"


@respx.mock
def test_platform_rejection_fails_item_and_retry_uses_a_new_key(user, client, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "bad", "errors": [{"reason": "invalidTitle"}]}}
        )
    )
    publishing.publish(item)
    item.refresh_from_db()
    assert item.status == Status.FAILED
    assert item.status_detail == "invalidTitle"

    item.publication = Publication.objects.get()
    item.save(update_fields=["publication"])
    with _no_enqueue():
        response = client.post(f"/api/v1/content/{item.pk}/retry/")
    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == Status.QUEUED
    assert item.publish_attempt == 1
    assert publishing._idempotency_key(item).endswith(":1")


def test_retry_is_refused_while_outcome_may_still_exist(user, client, fake_render):
    item = _ready(user, fake_render)
    account = connect_account(user)
    publication = Publication.objects.create(
        account=account,
        idempotency_key="k",
        marker="m",
        request_fingerprint="f",
        status=Publication.Status.OUTCOME_UNKNOWN,
    )
    ContentItem.objects.filter(pk=item.pk).update(
        status=Status.FAILED, publication=publication, publish_account=account
    )
    response = client.post(f"/api/v1/content/{item.pk}/retry/")
    assert response.status_code == 409


def test_cannot_upload_before_review_is_complete(user, client, fake_render):
    from tests.factories import ContentItemFactory

    item = ContentItemFactory(owner=user)
    response = _approve(client, item, connect_account(user))
    assert response.status_code == 409


def test_approval_needs_explicit_made_for_kids(user, client, fake_render):
    item = _ready(user, fake_render)
    account = connect_account(user)
    response = client.post(
        f"/api/v1/content/{item.pk}/approve/", {"account": str(account.pk)}, format="json"
    )
    assert response.status_code == 400


def test_rejecting_stops_the_item(user, client, fake_render):
    item = _ready(user, fake_render)
    response = client.post(
        f"/api/v1/content/{item.pk}/reject/", {"reason": "Off topic"}, format="json"
    )
    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == Status.REJECTED
    assert _approve(client, item, connect_account(user)).status_code == 409


def test_description_lists_sources_as_unverified_and_credits_footage(user, fake_render):
    item = _ready(user, fake_render)
    text = publishing._description(item)
    assert "not independently verified" in text
    assert "Fake Stock" in text
    assert "<" not in text


@respx.mock
def test_tokens_never_appear_in_any_api_response(user, client, fake_render):
    item = _approved_in_db(_ready(user, fake_render), connect_account(user))
    _mock_initiate()
    _mock_upload_ok()
    publishing.publish(item)
    responses = [
        client.get("/api/v1/social/accounts/"),
        client.get(f"/api/v1/content/{item.pk}/"),
        client.get("/api/v1/content/"),
        client.get("/api/v1/social/publications/"),
        client.get("/api/v1/publishing/status/"),
        client.get("/api/v1/usage/"),
    ]
    for response in responses:
        assert response.status_code == 200
        text = json.dumps(response.data, default=str)
        assert ACCESS_TOKEN not in text
        assert REFRESH_TOKEN not in text
        assert "upload_id" not in text
        assert "encrypted" not in text


def test_status_explains_when_force_private_is_off():
    with override_settings(YOUTUBE_FORCE_PRIVATE_WHEN_UNAUDITED=False):
        assert publishing.status_summary()["uploads_forced_private"] is False
