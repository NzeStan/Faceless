"""Upload tasks.

Deferred work is picked up by the periodic sweep and not by long Celery ETAs. Redis redelivers
ETA tasks after its visibility timeout, and a sweep also recovers from a lost worker.
"""

from __future__ import annotations

from celery import shared_task
from django.db.models import Q
from django.utils import timezone
from social_video_publisher.models import Publication

from apps.content.models import ContentItem

from . import services

RECONCILE_DELAY_SECONDS = 30
Status = ContentItem.Status


@shared_task(name="publishing.publish_item")
def publish_item(item_id: str) -> str:
    item = ContentItem.objects.get(pk=item_id)
    item = services.publish(item)
    if item.status == Status.UPLOADING and item.publication_id:
        reconcile_item.apply_async((item_id,), countdown=RECONCILE_DELAY_SECONDS)
    return item.status


@shared_task(name="publishing.reconcile_item")
def reconcile_item(item_id: str) -> str:
    return services.reconcile(ContentItem.objects.get(pk=item_id)).status


@shared_task(name="publishing.sweep")
def sweep() -> dict[str, int]:
    """Start uploads that are due, resume interrupted ones, and reconcile the rest."""
    now = timezone.now()
    due = ContentItem.objects.filter(status=Status.QUEUED).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )
    started = 0
    for item_id in due.values_list("pk", flat=True):
        publish_item.delay(str(item_id))
        started += 1

    reconciled = 0
    resumed = 0
    for item in ContentItem.objects.filter(status=Status.UPLOADING).select_related("publication"):
        publication = item.publication
        settled = publication is not None and publication.status in (
            Publication.Status.PROCESSING,
            Publication.Status.OUTCOME_UNKNOWN,
        )
        if settled:
            reconcile_item.delay(str(item.pk))
            reconciled += 1
        else:
            # publish() holds a lease, so this is harmless if a worker is still uploading.
            publish_item.delay(str(item.pk))
            resumed += 1
    return {"started": started, "reconciled": reconciled, "resumed": resumed}
