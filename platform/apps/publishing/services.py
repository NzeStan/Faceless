"""Glue between content items and django-social-video-publisher."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from social_video_publisher import quota
from social_video_publisher import services as publisher
from social_video_publisher.errors import (
    AuthExpired,
    CapabilityUnavailable,
    OutcomeUnknown,
    PlatformRejected,
    QuotaExhausted,
    RateLimited,
    SocialVideoPublisherError,
    ValidationFailed,
)
from social_video_publisher.models import Publication
from social_video_publisher.types import MediaSpec, PublishMetadata

from apps.content.models import ContentItem
from apps.content.services import InvalidTransition, record_failure, transition
from apps.production.models import MediaAsset, RenderJob

logger = logging.getLogger(__name__)
Status = ContentItem.Status
RETRY_AFTER_ERROR = timedelta(minutes=5)
RETRY_AFTER_BLOCK = timedelta(hours=1)
DESCRIPTION_LIMIT_BYTES = 4900


def _enqueue_publish(item_id) -> None:
    from .tasks import publish_item

    transaction.on_commit(lambda: publish_item.delay(str(item_id)))


def approve(
    item: ContentItem, *, actor, account, visibility: str, made_for_kids: bool
) -> ContentItem:
    """Record the human approval and queue the upload."""
    if account.status != account.Status.ACTIVE:
        raise AuthExpired("This account must be reconnected before uploading.")
    capabilities = publisher.get_capabilities(account)
    if not capabilities.upload.available:
        raise CapabilityUnavailable("upload", capabilities.upload.reason)
    with transaction.atomic():
        transition(
            item,
            Status.APPROVED,
            actor=actor,
            publish_account=account,
            requested_visibility=visibility,
            made_for_kids=made_for_kids,
            note=f"approved for {visibility}",
        )
        transition(item, Status.QUEUED, actor=actor, next_attempt_at=None)
        _enqueue_publish(item.pk)
    return item


def retry(item: ContentItem, *, actor) -> ContentItem:
    """Retry a failed item. Completed pipeline steps are not repeated."""
    from apps.production.tasks import start_pipeline

    if item.status != Status.FAILED:
        raise InvalidTransition(item.status, "retry")
    publication = (
        item.publication
        or Publication.objects.filter(
            account=item.publish_account, idempotency_key=_idempotency_key(item)
        ).first()
    )
    if publication is not None and publication.status != Publication.Status.FAILED:
        # The upload may still exist. Reconciliation decides; a new upload could duplicate it.
        raise InvalidTransition(f"{item.status} ({publication.status})", "retry")
    with transaction.atomic():
        if item.publish_account_id:
            transition(
                item,
                Status.QUEUED,
                actor=actor,
                publication=None,
                publish_attempt=item.publish_attempt + 1,
                status_detail="",
                status_message="",
                note="upload retried",
            )
            _enqueue_publish(item.pk)
        else:
            transition(
                item,
                Status.DRAFT,
                actor=actor,
                status_detail="",
                status_message="",
                note="pipeline retried",
            )
            start_pipeline(item)
    return item


def _idempotency_key(item: ContentItem) -> str:
    return f"content-item:{item.pk}:{item.publish_attempt}"


def _description(item: ContentItem) -> str:
    script = item.script
    parts = [script.description.strip()]
    if script.sources:
        lines = [f"- {s['title']}: {s['url']}" for s in script.sources]
        parts.append("Sources (not independently verified):\n" + "\n".join(lines))
    credits = item.assets.filter(kind=MediaAsset.Kind.CLIP).exclude(attribution="")
    names = sorted(set(credits.values_list("attribution", flat=True)))
    if names:
        parts.append("Stock footage: " + "; ".join(names))
    text = "\n\n".join(part for part in parts if part)
    text = text.replace("<", "").replace(">", "")
    return text.encode()[:DESCRIPTION_LIMIT_BYTES].decode(errors="ignore")


def _build_request(item: ContentItem) -> tuple[MediaSpec, PublishMetadata]:
    job = item.render_jobs.filter(status=RenderJob.Status.SUCCEEDED).order_by("-created_at").first()
    if job is None or job.output_asset is None:
        raise ValidationFailed([])
    asset = job.output_asset
    script = item.script
    media = MediaSpec(
        ref=asset.storage_key,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        duration_seconds=asset.duration_seconds or 0,
        width=asset.width or 0,
        height=asset.height or 0,
        kind="short",
    )
    metadata = PublishMetadata(
        title=script.title,
        description=_description(item),
        tags=tuple(script.tags),
        visibility=item.requested_visibility,
        made_for_kids=item.made_for_kids,
        contains_synthetic_media=item.contains_synthetic_media,
    )
    return media, metadata


def _defer(item: ContentItem, code: str, message: str, at) -> None:
    fields = {"status_detail": code, "status_message": message[:512], "next_attempt_at": at}
    if item.status == Status.UPLOADING:
        transition(item, Status.QUEUED, note=code, **fields)
    else:
        ContentItem.objects.filter(pk=item.pk).update(updated_at=timezone.now(), **fields)
        item.refresh_from_db()


def publish(item: ContentItem) -> ContentItem:
    """Run or resume the upload for an item. Safe to call repeatedly."""
    if item.status not in (Status.QUEUED, Status.UPLOADING):
        return item
    account = item.publish_account
    if account is None:
        return record_failure(item, "no_account", "The connected account was removed.")
    if item.status == Status.QUEUED:
        try:
            transition(
                item, Status.UPLOADING, status_detail="", status_message="", next_attempt_at=None
            )
        except InvalidTransition:
            # Another worker picked it up first. The upload lease keeps this harmless.
            item.refresh_from_db()
            if item.status != Status.UPLOADING:
                return item
    media, metadata = _build_request(item)
    key = _idempotency_key(item)
    try:
        publication = publisher.publish(account, media, metadata, key)
    except OutcomeUnknown as exc:
        _link(item, exc.publication, "outcome_unknown", "Checking whether the upload arrived.")
        return item
    except QuotaExhausted as exc:
        _defer(
            item,
            "quota_exhausted",
            "The daily YouTube upload quota is used up. This upload will start after it resets.",
            exc.reset_at,
        )
        return item
    except RateLimited as exc:
        _defer(
            item,
            "rate_limited",
            "YouTube asked us to slow down. This upload will retry shortly.",
            timezone.now() + timedelta(seconds=exc.retry_after),
        )
        return item
    except (AuthExpired, CapabilityUnavailable) as exc:
        _defer(
            item,
            "reconnect_required",
            f"Reconnect your YouTube account to continue: {exc}",
            timezone.now() + RETRY_AFTER_BLOCK,
        )
        return item
    except PlatformRejected as exc:
        if exc.retryable:
            _defer(
                item,
                "platform_unavailable",
                "YouTube could not take this upload right now. It will retry.",
                timezone.now() + RETRY_AFTER_ERROR,
            )
            return item
        return record_failure(item, exc.code, f"YouTube rejected the upload: {exc}")
    except ValidationFailed as exc:
        return record_failure(item, exc.code, str(exc) or "The video failed upload validation.")
    return sync(item, publication)


def _link(item: ContentItem, publication: Publication, detail: str, message: str) -> None:
    ContentItem.objects.filter(pk=item.pk).update(
        publication=publication,
        status_detail=detail,
        status_message=message[:512],
        updated_at=timezone.now(),
    )
    item.refresh_from_db()


def sync(item: ContentItem, publication: Publication) -> ContentItem:
    """Update the item from the publication. Only a platform-confirmed state counts."""
    if publication.status == Publication.Status.PUBLISHED:
        message = publication.visibility_reason
        if item.publication_id != publication.pk:
            _link(item, publication, "", "")
        return transition(
            item,
            Status.PUBLISHED,
            status_detail=f"visibility_{publication.visibility}",
            status_message=message,
        )
    if publication.status == Publication.Status.FAILED:
        _link(item, publication, "", "")
        return record_failure(
            item, publication.error_code or "upload_failed", "The upload did not succeed."
        )
    _link(item, publication, publication.status, publication.visibility_reason)
    return item


def reconcile(item: ContentItem) -> ContentItem:
    """Ask the platform for the true state. An error leaves everything unchanged."""
    publication = item.publication
    if publication is None or item.status != Status.UPLOADING:
        return item
    try:
        publication = publisher.reconcile(publication)
    except SocialVideoPublisherError as exc:
        logger.info("Reconcile for item %s postponed: %s", item.pk, exc.code)
        return item
    return sync(item, publication)


def status_summary() -> dict:
    """Project-wide facts a client needs in order to explain upload behaviour."""
    public_forced_private = (
        not settings.YOUTUBE_PROJECT_AUDITED and settings.YOUTUBE_FORCE_PRIVATE_WHEN_UNAUDITED
    )
    return {
        "audit_status": "audited" if settings.YOUTUBE_PROJECT_AUDITED else "unaudited",
        "uploads_forced_private": public_forced_private,
        "explanation": (
            "Uploads stay private until this project's audit setting is enabled."
            if public_forced_private
            else ""
        ),
        "uploads_used_today": quota.units_used("youtube", "uploads"),
        "uploads_daily_limit": settings.SOCIAL_VIDEO_PUBLISHER["PLATFORMS"]["youtube"].get(
            "DAILY_UPLOAD_LIMIT", 100
        ),
        "quota_resets_at": quota.next_reset(),
    }
