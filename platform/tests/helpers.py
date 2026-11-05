from django.db import transaction

from apps.content.services import create_item
from apps.production.tasks import start_pipeline


def generate_ready_item(owner, niche, topic="The rings of Saturn"):
    """Run the whole pipeline eagerly and return an item waiting for review."""
    with transaction.atomic():
        item = create_item(owner, niche, topic)
    run_pipeline(item)
    item.refresh_from_db()
    return item


def run_pipeline(item):
    from apps.production import tasks

    for task in (
        tasks.generate_script,
        tasks.generate_voiceover,
        tasks.source_footage,
        tasks.render_video,
    ):
        task.apply(args=[str(item.pk)])


__all__ = ["generate_ready_item", "run_pipeline", "start_pipeline"]
