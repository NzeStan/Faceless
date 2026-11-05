"""Celery chain for the generation pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable

from celery import chain, shared_task
from celery.exceptions import Ignore
from django.core.cache import cache
from django.db import transaction

from apps.content.models import ContentItem
from apps.content.services import InvalidTransition, record_failure
from providers import ProviderError

from . import pipeline

logger = logging.getLogger(__name__)
STEP_LOCK_SECONDS = 2 * 60 * 60


def _run(task, item_id: str, step: Callable[[ContentItem], None]) -> str:
    item = ContentItem.objects.select_related("niche", "owner").get(pk=item_id)
    lock = f"pipeline:{item_id}:{step.__name__}"
    # Guards against a redelivered task running beside one that is still working.
    if not cache.add(lock, task.request.id or "1", STEP_LOCK_SECONDS):
        raise task.retry(countdown=60)
    try:
        step(item)
    except ProviderError as exc:
        if exc.retryable and task.request.retries < task.max_retries:
            cache.delete(lock)
            raise task.retry(exc=exc, countdown=30 * 2**task.request.retries) from exc
        _fail(item, exc.code, str(exc))
        raise Ignore() from exc
    except pipeline.PipelineError as exc:
        _fail(item, exc.code, str(exc))
        raise Ignore() from exc
    finally:
        cache.delete(lock)
    return item_id


def _fail(item: ContentItem, code: str, message: str) -> None:
    try:
        record_failure(item, code, message)
    except InvalidTransition:
        logger.warning("Item %s could not move to failed from %s", item.pk, item.status)


@shared_task(bind=True, name="production.script", max_retries=3)
def generate_script(self, item_id: str) -> str:
    return _run(self, item_id, pipeline.step_script)


@shared_task(bind=True, name="production.voiceover", max_retries=3)
def generate_voiceover(self, item_id: str) -> str:
    return _run(self, item_id, pipeline.step_voiceover)


@shared_task(bind=True, name="production.footage", max_retries=3)
def source_footage(self, item_id: str) -> str:
    return _run(self, item_id, pipeline.step_footage)


@shared_task(bind=True, name="production.render", max_retries=1)
def render_video(self, item_id: str) -> str:
    return _run(self, item_id, pipeline.step_render)


def start_pipeline(item: ContentItem) -> None:
    """Start the chain after commit, so workers never read an uncommitted item."""
    item_id = str(item.pk)
    workflow = chain(
        generate_script.si(item_id),
        generate_voiceover.si(item_id),
        source_footage.si(item_id),
        render_video.si(item_id),
    )
    transaction.on_commit(workflow.delay)
