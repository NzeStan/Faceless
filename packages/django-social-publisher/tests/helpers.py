"""Shared test helpers.

JSON in tests/fixtures is hand-written from the shapes in Google's API reference. These are
not recordings of live traffic; no live credentials were available when they were written.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from social_publisher.adapters.youtube import REQUIRED_SCOPES
from social_publisher.models import ConnectedAccount, OAuthToken
from social_publisher.services import _owner_fields
from social_publisher.types import MediaSpec, PublishMetadata, TokenBundle

CHUNK = 256 * 1024
MEDIA = bytes(range(256)) * 1024 * 2  # 512 KiB, two chunks with the test chunk size
FIXTURES = Path(__file__).parent / "fixtures"
SESSION_URI = (
    "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=SESSION123"
)
ACCESS = "ya29.ACCESS-TOKEN-SECRET"
REFRESH = "1//REFRESH-TOKEN-SECRET"


def open_media(ref: str) -> io.BytesIO:
    return io.BytesIO(MEDIA)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def make_user(name: str = "owner") -> Any:
    return get_user_model().objects.create_user(username=name, password="pw")


def make_account(owner: Any = None, *, expires_in: int = 3600) -> ConnectedAccount:
    owner = owner or make_user()
    account = ConnectedAccount.objects.create(
        platform="youtube",
        external_account_id="UC123",
        display_name="Test Channel",
        scopes=list(REQUIRED_SCOPES),
        **_owner_fields(owner),
    )
    record = OAuthToken(account=account)
    record.set_bundle(
        TokenBundle(ACCESS, REFRESH, expires_at=timezone.now() + timedelta(seconds=expires_in))
    )
    record.save()
    return account


def media_spec(**overrides: Any) -> MediaSpec:
    values: dict[str, Any] = {
        "ref": "videos/a.mp4",
        "mime_type": "video/mp4",
        "size_bytes": len(MEDIA),
        "duration_seconds": 30.0,
        "width": 1080,
        "height": 1920,
        "kind": "short",
    }
    return MediaSpec(**{**values, **overrides})


def metadata(**overrides: Any) -> PublishMetadata:
    values: dict[str, Any] = {
        "title": "A title",
        "description": "A description",
        "tags": ("one", "two"),
        "visibility": "public",
        "made_for_kids": False,
        "contains_synthetic_media": True,
    }
    return PublishMetadata(**{**values, **overrides})
