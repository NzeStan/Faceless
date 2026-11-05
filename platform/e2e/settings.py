"""Development settings pointed at the local stand-in for Google, with faster crash recovery."""

from datetime import timedelta

from config.settings.dev import *  # noqa: F403
from config.settings.dev import CELERY_BEAT_SCHEDULE, INSTALLED_APPS, SOCIAL_PUBLISHER

INSTALLED_APPS = [*INSTALLED_APPS, "e2e.apps.E2EConfig"]
SOCIAL_PUBLISHER = {**SOCIAL_PUBLISHER, "UPLOAD_LEASE_SECONDS": 40}
CELERY_BEAT_SCHEDULE = {
    **CELERY_BEAT_SCHEDULE,
    "publishing-sweep": {
        **CELERY_BEAT_SCHEDULE["publishing-sweep"],
        "schedule": timedelta(seconds=10),
    },
}
