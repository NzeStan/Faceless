import json
from datetime import timedelta

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from social_publisher import crypto, quota, services
from social_publisher.adapters import get_adapter
from social_publisher.adapters.youtube import TOKEN_URL, UPLOAD_URL
from social_publisher.errors import (
    AuthExpired,
    OutcomeUnknown,
    PlatformRejected,
    QuotaExhausted,
    RateLimited,
    ValidationFailed,
)
from social_publisher.models import Publication, QuotaLedger
from social_publisher.signals import publication_status_changed
from tests.helpers import CHUNK, MEDIA, SESSION_URI, fixture, make_account, media_spec, metadata

pytestmark = pytest.mark.django_db
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST = "https://www.googleapis.com/youtube/v3/playlistItems"


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


def _initiate():
    return respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": SESSION_URI})
    )


def _incomplete(end):
    return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})


def _complete():
    return httpx.Response(201, json=fixture("video_inserted"))


def _upload_ok():
    return respx.put(SESSION_URI).mock(side_effect=[_incomplete(CHUNK - 1), _complete()])


def _expire_lease():
    Publication.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))


def _platform_settings(**overrides):
    config = {**settings.SOCIAL_PUBLISHER}
    config["PLATFORMS"] = {"youtube": {**config["PLATFORMS"]["youtube"], **overrides}}
    return config


@respx.mock
def test_publish_uploads_in_chunks_and_never_claims_published():
    account = make_account()
    post, put = _initiate(), _upload_ok()
    publication = services.publish(account, media_spec(), metadata(), "key-1")
    assert publication.status == "processing"
    assert publication.platform_media_id == "VIDEO123"
    assert post.call_count == 1
    assert put.call_count == 2
    assert put.calls[0].request.headers["Content-Range"] == f"bytes 0-{CHUNK - 1}/{len(MEDIA)}"
    assert put.calls[1].request.headers["Content-Range"] == (
        f"bytes {CHUNK}-{len(MEDIA) - 1}/{len(MEDIA)}"
    )
    assert put.calls[0].request.content + put.calls[1].request.content == MEDIA


@respx.mock
def test_request_always_sends_privacy_disclosure_and_marker():
    post, _ = _initiate(), _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    body = json.loads(post.calls[0].request.content)
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert publication.marker in body["snippet"]["description"]


@respx.mock
def test_unaudited_project_forces_private_and_says_why():
    _initiate(), _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert publication.requested_visibility == "public"
    assert publication.visibility == "private"
    assert "not audited" in publication.visibility_reason


@respx.mock
def test_force_private_can_be_switched_off_explicitly():
    post, _ = _initiate(), _upload_ok()
    with override_settings(SOCIAL_PUBLISHER=_platform_settings(FORCE_PRIVATE_WHEN_UNAUDITED=False)):
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert json.loads(post.calls[0].request.content)["status"]["privacyStatus"] == "public"


@respx.mock
def test_session_uri_is_persisted_encrypted_before_any_bytes_are_sent():
    seen = {}

    def on_put(request):
        seen["stored"] = Publication.objects.get().upload_session_encrypted
        raise WorkerKilled()

    _initiate()
    respx.put(SESSION_URI).mock(side_effect=on_put)
    with pytest.raises(WorkerKilled):
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert SESSION_URI not in seen["stored"]
    assert crypto.decrypt(seen["stored"]) == SESSION_URI


@respx.mock
def test_crashed_worker_resumes_from_reported_offset_and_never_reinitiates():
    account = make_account()
    post = _initiate()
    put = respx.put(SESSION_URI).mock(side_effect=_sequence(_incomplete(CHUNK - 1), WorkerKilled()))
    with pytest.raises(WorkerKilled):
        services.publish(account, media_spec(), metadata(), "key-1")
    assert Publication.objects.get().status == "uploading"

    # While the lease is live a second worker must not touch the upload.
    assert services.publish(account, media_spec(), metadata(), "key-1").status == "uploading"
    assert post.call_count == 1

    _expire_lease()
    put.side_effect = [_incomplete(CHUNK - 1), _complete()]
    earlier = put.call_count
    publication = services.publish(account, media_spec(), metadata(), "key-1")

    assert publication.status == "processing"
    assert post.call_count == 1
    assert put.calls[earlier].request.headers["Content-Range"] == f"bytes */{len(MEDIA)}"
    assert put.calls[earlier + 1].request.headers["Content-Range"].startswith(f"bytes {CHUNK}-")
    assert quota.units_used("youtube", "uploads") == 1


@respx.mock
def test_resume_detects_an_upload_that_already_finished():
    account = make_account()
    post = _initiate()
    put = respx.put(SESSION_URI).mock(side_effect=_sequence(WorkerKilled()))
    with pytest.raises(WorkerKilled):
        services.publish(account, media_spec(), metadata(), "key-1")
    _expire_lease()
    put.side_effect = [httpx.Response(200, json=fixture("video_inserted"))]
    publication = services.publish(account, media_spec(), metadata(), "key-1")
    assert publication.platform_media_id == "VIDEO123"
    assert post.call_count == 1


@respx.mock
def test_duplicate_key_returns_existing_without_new_upload():
    account = make_account()
    post, put = _initiate(), _upload_ok()
    first = services.publish(account, media_spec(), metadata(), "key-1")
    second = services.publish(account, media_spec(), metadata(), "key-1")
    assert first.pk == second.pk
    assert post.call_count == 1
    assert put.call_count == 2
    assert Publication.objects.count() == 1


def test_same_key_for_different_content_is_rejected():
    account = make_account()
    Publication.objects.create(
        account=account, idempotency_key="key-1", marker="m", request_fingerprint="other"
    )
    with pytest.raises(ValidationFailed):
        services.publish(account, media_spec(), metadata(), "key-1")


@respx.mock
def test_connection_drop_after_bytes_is_outcome_unknown_and_keeps_quota():
    account = make_account()
    post = _initiate()
    respx.put(SESSION_URI).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(OutcomeUnknown) as caught:
        services.publish(account, media_spec(), metadata(), "key-1")
    assert caught.value.publication.status == "outcome_unknown"
    assert quota.units_used("youtube", "uploads") == 1
    # A repeat must not upload again.
    assert services.publish(account, media_spec(), metadata(), "key-1").status == "outcome_unknown"
    assert post.call_count == 1


def _publication_with_unknown_outcome(account):
    post = _initiate()
    respx.put(SESSION_URI).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(OutcomeUnknown) as caught:
        services.publish(account, media_spec(), metadata(), "key-1")
    return caught.value.publication, post


def _mock_uploads(items):
    respx.get(CHANNELS).mock(return_value=httpx.Response(200, json=fixture("channels_mine")))
    return respx.get(PLAYLIST).mock(return_value=httpx.Response(200, json={"items": items}))


@respx.mock
def test_reconcile_adopts_video_found_by_marker_without_a_second_upload():
    publication, post = _publication_with_unknown_outcome(make_account())
    _mock_uploads(
        [
            {"snippet": {"description": "unrelated", "resourceId": {"videoId": "OTHER"}}},
            {
                "snippet": {
                    "description": f"Hi\n\n[ref:{publication.marker}]",
                    "resourceId": {"videoId": "VIDEO123"},
                }
            },
        ]
    )
    respx.get(VIDEOS).mock(return_value=httpx.Response(200, json=fixture("videos_list_processed")))
    publication = services.reconcile(publication)
    assert publication.status == "published"
    assert publication.platform_media_id == "VIDEO123"
    assert publication.published_at is not None
    assert post.call_count == 1


@respx.mock
def test_reconcile_waits_during_grace_then_fails():
    publication, _ = _publication_with_unknown_outcome(make_account())
    _mock_uploads([])
    assert services.reconcile(publication).status == "outcome_unknown"
    Publication.objects.update(outcome_unknown_at=timezone.now() - timedelta(hours=2))
    publication.refresh_from_db()
    publication = services.reconcile(publication)
    assert publication.status == "failed"
    assert publication.error_code == "reconcile_not_found"


@respx.mock
@pytest.mark.parametrize(
    ("name", "status", "error"),
    [
        ("videos_list_processing", "processing", ""),
        ("videos_list_processed", "published", ""),
        ("videos_list_rejected", "failed", "rejected:duplicate"),
    ],
)
def test_reconcile_follows_video_status(name, status, error):
    _initiate(), _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    respx.get(VIDEOS).mock(return_value=httpx.Response(200, json=fixture(name)))
    publication = services.reconcile(publication)
    assert publication.status == status
    assert publication.error_code == error


@respx.mock
def test_video_missing_from_list_is_failed_not_published():
    _initiate(), _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    respx.get(VIDEOS).mock(return_value=httpx.Response(200, json={"items": []}))
    publication = services.reconcile(publication)
    assert publication.status == "failed"
    assert publication.error_code == "video_not_found"


@respx.mock
def test_reconcile_error_leaves_status_unchanged():
    _initiate(), _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    respx.get(VIDEOS).mock(return_value=httpx.Response(503))
    with pytest.raises(PlatformRejected):
        services.reconcile(publication)
    publication.refresh_from_db()
    assert publication.status == "processing"


@respx.mock
def test_quota_exhausted_leaves_pending_and_retry_with_same_key_works():
    account = make_account()
    post = _initiate()
    for _ in range(3):
        quota.reserve("youtube", get_adapter("youtube").quota_for("publish"))
    with pytest.raises(QuotaExhausted) as caught:
        services.publish(account, media_spec(), metadata(), "key-1")
    assert caught.value.reset_at > timezone.now()
    assert Publication.objects.get().status == "pending"
    assert post.call_count == 0

    QuotaLedger.objects.update(units_used=0)
    _upload_ok()
    assert services.publish(account, media_spec(), metadata(), "key-1").status == "processing"


@respx.mock
def test_platform_quota_error_defers_and_releases_reservation():
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(403, json=fixture("error_quota")))
    with pytest.raises(QuotaExhausted):
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert Publication.objects.get().status == "pending"
    assert quota.units_used("youtube", "uploads") == 0


@respx.mock
def test_rate_limit_carries_retry_after():
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "42"}))
    with pytest.raises(RateLimited) as caught:
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert caught.value.retry_after == 42
    assert Publication.objects.get().status == "pending"


@respx.mock
def test_definite_rejection_fails_and_releases_quota():
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "bad", "errors": [{"reason": "invalidTitle"}]}},
        )
    )
    with pytest.raises(PlatformRejected):
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    publication = Publication.objects.get()
    assert publication.status == "failed"
    assert publication.error_code == "invalidTitle"
    assert quota.units_used("youtube", "uploads") == 0


@respx.mock
def test_transport_failure_before_upload_is_retryable_not_unknown():
    respx.post(UPLOAD_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(PlatformRejected) as caught:
        services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert caught.value.retryable
    assert Publication.objects.get().status == "pending"


@respx.mock
def test_transient_5xx_checks_status_and_continues():
    _initiate()
    put = respx.put(SESSION_URI).mock(
        side_effect=[httpx.Response(503), _incomplete(CHUNK - 1), _complete()]
    )
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert publication.status == "processing"
    assert put.calls[1].request.headers["Content-Range"] == f"bytes */{len(MEDIA)}"


@respx.mock
def test_expired_session_on_resume_reconciles_instead_of_uploading_again():
    account = make_account()
    post = _initiate()
    put = respx.put(SESSION_URI).mock(side_effect=_sequence(WorkerKilled()))
    with pytest.raises(WorkerKilled):
        services.publish(account, media_spec(), metadata(), "key-1")
    _expire_lease()
    put.side_effect = [httpx.Response(404)]
    with pytest.raises(OutcomeUnknown):
        services.publish(account, media_spec(), metadata(), "key-1")
    assert Publication.objects.get().status == "outcome_unknown"
    assert post.call_count == 1


@respx.mock
def test_rejected_access_token_is_refreshed_once():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )
    post = respx.post(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, headers={"Location": SESSION_URI}),
        ]
    )
    _upload_ok()
    publication = services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert publication.status == "processing"
    assert post.calls[1].request.headers["Authorization"] == "Bearer new"


def test_inactive_account_cannot_publish():
    account = make_account()
    account.status = "needs_reauth"
    account.save()
    with pytest.raises(AuthExpired):
        services.publish(account, media_spec(), metadata(), "key-1")
    assert not Publication.objects.exists()


@pytest.mark.parametrize(
    ("media", "meta", "field"),
    [
        ({"width": 1920, "height": 1080}, {}, "aspect_ratio"),
        ({"duration_seconds": 181}, {}, "duration_seconds"),
        ({"mime_type": "image/png"}, {}, "mime_type"),
        ({"size_bytes": 0}, {}, "size_bytes"),
        ({}, {"title": "x" * 101}, "title"),
        ({}, {"title": "a<b"}, "title"),
        ({}, {"title": "  "}, "title"),
        ({}, {"description": "a>b"}, "description"),
        ({}, {"description": "x" * 5000}, "description"),
        ({}, {"tags": ("x" * 501,)}, "tags"),
        ({}, {"category": "music"}, "category"),
        ({}, {"made_for_kids": None}, "made_for_kids"),
        ({}, {"contains_synthetic_media": None}, "contains_synthetic_media"),
    ],
)
def test_validation_runs_before_any_network_or_quota_use(media, meta, field):
    with pytest.raises(ValidationFailed) as caught:
        services.publish(make_account(), media_spec(**media), metadata(**meta), "key-1")
    assert field in {issue.field for issue in caught.value.issues}
    assert quota.units_used("youtube", "uploads") == 0
    assert not Publication.objects.exists()


def test_square_short_at_the_limit_is_valid_and_landscape_video_is_allowed():
    adapter = get_adapter("youtube")
    capabilities = services.get_capabilities(make_account())
    square = media_spec(width=1080, height=1080, duration_seconds=180)
    landscape = media_spec(width=1920, height=1080, kind="video", duration_seconds=900)
    assert adapter.validate(square, capabilities) == []
    assert adapter.validate(landscape, capabilities) == []


def test_tag_length_counts_commas_and_implicit_quotes():
    adapter = get_adapter("youtube")
    capabilities = services.get_capabilities(make_account())
    # Each 'a b' counts as 5 characters. 83 tags: 415 + 82 commas = 497, allowed.
    assert not adapter.validate_metadata(metadata(tags=("a b",) * 83), capabilities)
    # 84 tags: 420 + 83 commas = 503, rejected.
    assert adapter.validate_metadata(metadata(tags=("a b",) * 84), capabilities)


def test_capabilities_explain_missing_scope():
    account = make_account()
    account.scopes = []
    account.save()
    capabilities = services.get_capabilities(account)
    assert not capabilities.upload.available
    assert "youtube.upload" in capabilities.upload.reason
    assert not capabilities.public_visibility.available
    assert capabilities.public_visibility.reason


def test_signal_fires_on_status_change(django_capture_on_commit_callbacks):
    received = []

    def listener(publication, previous_status, **_):
        received.append((previous_status, publication.status))

    publication_status_changed.connect(listener)
    try:
        with respx.mock, django_capture_on_commit_callbacks(execute=True):
            _initiate(), _upload_ok()
            services.publish(make_account(), media_spec(), metadata(), "key-1")
    finally:
        publication_status_changed.disconnect(listener)
    assert ("uploading", "processing") in received
