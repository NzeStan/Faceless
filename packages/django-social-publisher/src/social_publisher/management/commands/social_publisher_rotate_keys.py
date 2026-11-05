from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from social_publisher.services import rotate_tokens


class Command(BaseCommand):
    help = "Re-encrypt stored secrets with the first key in TOKEN_ENCRYPTION_KEYS."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted {rotate_tokens()} values."))
