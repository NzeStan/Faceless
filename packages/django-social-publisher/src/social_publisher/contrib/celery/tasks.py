"""Thin Celery wrappers around the service functions. They add no retries of their own."""

from __future__ import annotations

from typing import Any

from celery import shared_task

from social_publisher import services
from social_publisher.models import ConnectedAccount, Publication
from social_publisher.types import MediaSpec, PublishMetadata


@shared_task(name="social_publisher.publish")
def publish_task(
    account_id: str, media: dict[str, Any], metadata: dict[str, Any], idempotency_key: str
) -> str:
    metadata = {**metadata, "tags": tuple(metadata.get("tags", ()))}
    publication = services.publish(
        ConnectedAccount.objects.get(pk=account_id),
        MediaSpec(**media),
        PublishMetadata(**metadata),
        idempotency_key,
    )
    return str(publication.pk)


@shared_task(name="social_publisher.reconcile")
def reconcile_task(publication_id: str) -> str:
    publication = services.reconcile(Publication.objects.get(pk=publication_id))
    return str(publication.status)
