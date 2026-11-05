"""Public value types shared by services and adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import BinaryIO, Literal

MediaKind = Literal["short", "video"]
Visibility = Literal["public", "unlisted", "private"]
Operation = Literal["connect", "publish", "reconcile", "health"]


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """OAuth tokens. Token values are excluded from ``repr``."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountInfo:
    external_id: str
    display_name: str
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Capability:
    """Whether a feature is available, and why not when it is not."""

    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What one connected account can do right now."""

    upload: Capability
    public_visibility: Capability
    ai_disclosure: Capability
    media_kinds: frozenset[str] = frozenset()
    max_duration_seconds: int | None = None
    allowed_aspect_ratios: tuple[str, ...] = ()
    daily_limits: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class MediaSpec:
    """Describes a media file. ``ref`` is passed to the host's ``MEDIA_OPENER``."""

    ref: str
    mime_type: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    kind: MediaKind = "video"


@dataclass(frozen=True, slots=True)
class PublishMetadata:
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    category: str | None = None
    visibility: Visibility = "private"
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class QuotaCharge:
    """Units an operation takes from one daily platform quota bucket."""

    bucket: str
    units: int
    limit: int


@dataclass(frozen=True)
class PublishRequest:
    """Everything an adapter needs for one upload attempt.

    ``marker`` must be embedded so ``reconcile`` can find the upload later. ``resume_token``
    holds a value previously passed to ``on_checkpoint`` when an upload is being resumed.
    Call ``on_checkpoint`` with the resume token before sending bytes, then after each chunk.
    """

    tokens: TokenBundle
    media: MediaSpec
    metadata: PublishMetadata
    capabilities: Capabilities
    open_stream: Callable[[], BinaryIO]
    marker: str
    resume_token: str | None = field(default=None, repr=False)
    on_checkpoint: Callable[[str], None] = lambda _token: None


@dataclass(frozen=True, slots=True)
class PublishResult:
    """A platform-confirmed state. ``outcome_unknown`` means "not found yet"."""

    status: Literal["processing", "published", "failed", "outcome_unknown"]
    platform_media_id: str = ""
    url: str = ""
    visibility: str = ""
    visibility_reason: str = ""
    error_code: str = ""
    raw_excerpt: str = ""
