"""Adapter interface and registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from ..conf import get_settings
from ..types import (
    AccountInfo,
    Capabilities,
    MediaSpec,
    Operation,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    QuotaCharge,
    TokenBundle,
    ValidationIssue,
)


@runtime_checkable
class PlatformAdapter(Protocol):
    key: str

    def authorization_url(self, state: str, code_verifier: str | None) -> str: ...
    def exchange_code(self, code: str, code_verifier: str | None) -> TokenBundle: ...
    def refresh(self, tokens: TokenBundle) -> TokenBundle: ...
    def revoke(self, tokens: TokenBundle) -> None: ...
    def fetch_account(self, tokens: TokenBundle) -> AccountInfo: ...
    def capabilities(self, account: AccountInfo) -> Capabilities: ...
    def validate(self, media: MediaSpec, capabilities: Capabilities) -> list[ValidationIssue]: ...
    def validate_metadata(
        self, metadata: PublishMetadata, capabilities: Capabilities
    ) -> list[ValidationIssue]: ...
    def quota_for(self, operation: Operation) -> list[QuotaCharge]: ...
    def publish(self, request: PublishRequest) -> PublishResult: ...
    def reconcile(self, publication_ref: str, tokens: TokenBundle) -> PublishResult: ...


AdapterFactory = Callable[[Mapping[str, Any]], PlatformAdapter]

MARKER_REF_PREFIX = "marker:"
"""``reconcile`` receives ``marker:<value>`` when the media ID is not known yet."""

_registry: dict[str, AdapterFactory | str] = {
    "youtube": "social_publisher.adapters.youtube.YouTubeAdapter",
}


def register_adapter(key: str, factory: AdapterFactory) -> None:
    """Register or replace the adapter factory for a platform key."""
    _registry[key] = factory


def get_adapter(key: str) -> PlatformAdapter:
    config = get_settings().platforms.get(key)
    if config is None:
        raise ImproperlyConfigured(f"SOCIAL_PUBLISHER['PLATFORMS'] has no entry for {key!r}.")
    factory = config.get("ADAPTER") or _registry.get(key)
    if factory is None:
        raise ImproperlyConfigured(f"No adapter is registered for platform {key!r}.")
    if isinstance(factory, str):
        factory = import_string(factory)
    return factory(config)
