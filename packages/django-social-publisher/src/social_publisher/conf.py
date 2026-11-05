"""Validated access to the ``SOCIAL_PUBLISHER`` settings dict."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, BinaryIO

from cryptography.fernet import Fernet
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.utils.module_loading import import_string

_KNOWN_KEYS = {
    "OWNER_MODEL",
    "TOKEN_ENCRYPTION_KEYS",
    "MEDIA_OPENER",
    "PLATFORMS",
    "OAUTH_STATE_TTL_SECONDS",
    "UPLOAD_LEASE_SECONDS",
    "RECONCILE_GRACE_SECONDS",
    "DRF_OWNER_RESOLVER",
}


@dataclass(frozen=True)
class PublisherSettings:
    owner_model: str
    token_encryption_keys: tuple[str, ...]
    media_opener: Callable[[str], BinaryIO]
    platforms: Mapping[str, Mapping[str, Any]]
    oauth_state_ttl_seconds: int
    upload_lease_seconds: int
    reconcile_grace_seconds: int
    drf_owner_resolver: str | None


def _positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ImproperlyConfigured(f"SOCIAL_PUBLISHER[{key!r}] must be a positive integer.")
    return value


@lru_cache(maxsize=1)
def get_settings() -> PublisherSettings:
    raw = getattr(django_settings, "SOCIAL_PUBLISHER", None)
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured("The SOCIAL_PUBLISHER setting must be a dict.")
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ImproperlyConfigured(f"Unknown SOCIAL_PUBLISHER keys: {sorted(unknown)}")

    owner_model = raw.get("OWNER_MODEL") or django_settings.AUTH_USER_MODEL
    if not isinstance(owner_model, str) or owner_model.count(".") != 1:
        raise ImproperlyConfigured("SOCIAL_PUBLISHER['OWNER_MODEL'] must be 'app_label.Model'.")

    keys = raw.get("TOKEN_ENCRYPTION_KEYS")
    if not keys or isinstance(keys, str | bytes):
        raise ImproperlyConfigured(
            "SOCIAL_PUBLISHER['TOKEN_ENCRYPTION_KEYS'] must be a non-empty list of Fernet keys."
        )
    for index, key in enumerate(keys):
        try:
            Fernet(key)
        except (ValueError, TypeError):
            # The key value is deliberately left out of the message.
            raise ImproperlyConfigured(
                f"TOKEN_ENCRYPTION_KEYS[{index}] is not a valid Fernet key."
            ) from None

    opener_path = raw.get("MEDIA_OPENER")
    if not opener_path:
        raise ImproperlyConfigured("SOCIAL_PUBLISHER['MEDIA_OPENER'] is required.")
    try:
        opener = import_string(opener_path) if isinstance(opener_path, str) else opener_path
    except ImportError as exc:
        raise ImproperlyConfigured(f"Cannot import MEDIA_OPENER {opener_path!r}") from exc
    if not callable(opener):
        raise ImproperlyConfigured("SOCIAL_PUBLISHER['MEDIA_OPENER'] must be callable.")

    platforms = raw.get("PLATFORMS") or {}
    if not isinstance(platforms, Mapping):
        raise ImproperlyConfigured("SOCIAL_PUBLISHER['PLATFORMS'] must be a dict.")

    return PublisherSettings(
        owner_model=owner_model,
        token_encryption_keys=tuple(keys),
        media_opener=opener,
        platforms=platforms,
        oauth_state_ttl_seconds=_positive_int(raw, "OAUTH_STATE_TTL_SECONDS", 600),
        upload_lease_seconds=_positive_int(raw, "UPLOAD_LEASE_SECONDS", 900),
        reconcile_grace_seconds=_positive_int(raw, "RECONCILE_GRACE_SECONDS", 3600),
        drf_owner_resolver=raw.get("DRF_OWNER_RESOLVER"),
    )


def _reset(*, setting: str, **_: Any) -> None:
    if setting == "SOCIAL_PUBLISHER":
        get_settings.cache_clear()


setting_changed.connect(_reset, dispatch_uid="social_publisher.conf.reset")
