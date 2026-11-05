"""Typed errors raised by the service layer and adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import ValidationIssue


class SocialPublisherError(Exception):
    """Base class for every error this package raises deliberately."""

    code = "error"


class AuthExpired(SocialPublisherError):
    """Stored credentials no longer work; the owner must reconnect."""

    code = "auth_expired"


class OAuthStateInvalid(SocialPublisherError):
    """The OAuth state is unknown, expired, reused or belongs to another owner."""

    code = "oauth_state_invalid"


class TokenDecryptionError(SocialPublisherError):
    """A stored secret cannot be decrypted with any configured key."""

    code = "token_decryption_failed"


class CapabilityUnavailable(SocialPublisherError):
    code = "capability_unavailable"

    def __init__(self, capability: str, reason: str | None = None) -> None:
        super().__init__(f"{capability} is unavailable: {reason or 'no reason given'}")
        self.capability = capability
        self.reason = reason


class QuotaExhausted(SocialPublisherError):
    code = "quota_exhausted"

    def __init__(self, bucket: str, reset_at: datetime) -> None:
        super().__init__(f"Quota bucket {bucket!r} is exhausted until {reset_at.isoformat()}")
        self.bucket = bucket
        self.reset_at = reset_at


class RateLimited(SocialPublisherError):
    code = "rate_limited"

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(f"Rate limited; retry after {retry_after} seconds")
        self.retry_after = retry_after


class ValidationFailed(SocialPublisherError):
    code = "validation_failed"

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        super().__init__("; ".join(f"{issue.field}: {issue.message}" for issue in issues))
        self.issues = tuple(issues)


class PlatformRejected(SocialPublisherError):
    """The platform gave a definite answer: this request did not succeed."""

    code = "platform_rejected"

    def __init__(
        self,
        code: str = "platform_rejected",
        message: str = "",
        *,
        retryable: bool = False,
        raw_excerpt: str = "",
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable
        self.raw_excerpt = raw_excerpt


class OutcomeUnknown(SocialPublisherError):
    """The request may have succeeded. Reconcile; never retry the upload blindly."""

    code = "outcome_unknown"

    def __init__(self, message: str = "The platform outcome is unknown") -> None:
        super().__init__(message)
        self.publication: Any = None
