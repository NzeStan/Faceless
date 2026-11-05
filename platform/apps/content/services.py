"""Content lifecycle. Every status change goes through ``transition``."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import ContentEvent, ContentItem

Status = ContentItem.Status

ALLOWED = {
    Status.DRAFT: {Status.SCRIPTING, Status.FAILED},
    Status.SCRIPTING: {Status.VOICING, Status.FAILED},
    Status.VOICING: {Status.SOURCING, Status.FAILED},
    Status.SOURCING: {Status.RENDERING, Status.FAILED},
    Status.RENDERING: {Status.AWAITING_REVIEW, Status.FAILED},
    Status.AWAITING_REVIEW: {Status.APPROVED, Status.REJECTED},
    Status.APPROVED: {Status.QUEUED, Status.FAILED},
    Status.QUEUED: {Status.UPLOADING, Status.FAILED},
    Status.UPLOADING: {Status.QUEUED, Status.PUBLISHED, Status.FAILED},
    Status.FAILED: {Status.DRAFT, Status.QUEUED},
    Status.PUBLISHED: set(),
    Status.REJECTED: set(),
}


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"A {current} item cannot move to {target}.")
        self.current = current
        self.target = target


class DailyCapExceeded(Exception):
    pass


def transition(
    item: ContentItem,
    target: str,
    *,
    actor=None,
    note: str = "",
    **fields,
) -> ContentItem:
    """Move an item to a new status inside a transaction and write an audit row."""
    with transaction.atomic():
        current = ContentItem.objects.select_for_update().get(pk=item.pk)
        if target not in ALLOWED[Status(current.status)]:
            raise InvalidTransition(current.status, target)
        event = ContentEvent(
            item=current,
            from_status=current.status,
            to_status=target,
            actor=actor,
            note=note[:512],
        )
        current.status = target
        for name, value in fields.items():
            setattr(current, name, value)
        current.save(update_fields=["status", "updated_at", *fields])
        event.save()
    item.refresh_from_db()
    return item


def create_item(owner, niche, topic: str) -> ContentItem:
    """Create a draft, enforcing the per-user daily cap under a row lock."""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        used = ContentItem.objects.filter(owner=owner, created_at__gte=start).count()
        if used >= settings.GENERATION_DAILY_CAP:
            raise DailyCapExceeded()
        item = ContentItem.objects.create(owner=owner, niche=niche, topic=topic)
        ContentEvent.objects.create(item=item, kind="created", to_status=item.status, actor=owner)
    return item


def generations_left_today(owner) -> int:
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = ContentItem.objects.filter(owner=owner, created_at__gte=start).count()
    return max(0, settings.GENERATION_DAILY_CAP - used)


def set_disclosure(item: ContentItem, value: bool, *, actor) -> ContentItem:
    """Change the AI disclosure flag and record who did it."""
    with transaction.atomic():
        current = ContentItem.objects.select_for_update().get(pk=item.pk)
        if current.status != Status.AWAITING_REVIEW:
            raise InvalidTransition(current.status, "disclosure change")
        if current.contains_synthetic_media != value:
            current.contains_synthetic_media = value
            current.save(update_fields=["contains_synthetic_media", "updated_at"])
            ContentEvent.objects.create(
                item=current,
                kind="disclosure_changed",
                from_status=current.status,
                to_status=current.status,
                actor=actor,
                note=f"contains_synthetic_media set to {value} with explicit confirmation",
            )
    item.refresh_from_db()
    return item


def record_failure(item: ContentItem, code: str, message: str) -> ContentItem:
    return transition(
        item, Status.FAILED, status_detail=code[:64], status_message=message[:512], note=code
    )
