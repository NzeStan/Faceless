from __future__ import annotations

import logging

from django.apps import AppConfig


class _RedactUploadSessions(logging.Filter):
    """httpx logs full request URLs; a resumable session URL acts as a credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        if "upload_id" in record.getMessage():
            record.msg = "HTTP request to a resumable upload session (URL redacted)"
            record.args = ()
        return True


class SocialPublisherConfig(AppConfig):
    name = "social_publisher"
    verbose_name = "Social publisher"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from .conf import get_settings

        get_settings()
        logger = logging.getLogger("httpx")
        if not any(isinstance(item, _RedactUploadSessions) for item in logger.filters):
            logger.addFilter(_RedactUploadSessions())
