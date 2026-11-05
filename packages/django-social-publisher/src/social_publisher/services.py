"""The service layer: the only supported way for host code to change state."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from . import crypto, quota, signals
from .adapters import MARKER_REF_PREFIX, PlatformAdapter, get_adapter
from .conf import get_settings
from .errors import (
    AuthExpired,
    CapabilityUnavailable,
    OAuthStateInvalid,
    OutcomeUnknown,
    PlatformRejected,
    QuotaExhausted,
    RateLimited,
    SocialPublisherError,
    ValidationFailed,
)
from .models import ConnectedAccount, OAuthState, OAuthToken, Publication
from .types import (
    AccountInfo,
    Capabilities,
    MediaSpec,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    TokenBundle,
    ValidationIssue,
)

logger = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(minutes=2)
_SETTLED = frozenset(
    {
        Publication.Status.OUTCOME_UNKNOWN,
        Publication.Status.PROCESSING,
        Publication.Status.PUBLISHED,
        Publication.Status.FAILED,
    }
)


# Owners ------------------------------------------------------------------------------------


def _owner_fields(owner: models.Model) -> dict[str, Any]:
    expected = apps.get_model(get_settings().owner_model)
    if not isinstance(owner, expected):
        raise TypeError(f"Owner must be a {get_settings().owner_model} instance.")
    return {
        "owner_content_type": ContentType.objects.get_for_model(owner, for_concrete_model=True),
        "owner_object_id": str(owner.pk),
    }


def accounts_for_owner(owner: models.Model) -> models.QuerySet[ConnectedAccount]:
    """Return the connected accounts that belong to ``owner``."""
    return ConnectedAccount.objects.filter(**_owner_fields(owner))


def publications_for_owner(owner: models.Model) -> models.QuerySet[Publication]:
    """Return the publications made through accounts that belong to ``owner``."""
    fields = _owner_fields(owner)
    return Publication.objects.filter(
        account__owner_content_type=fields["owner_content_type"],
        account__owner_object_id=fields["owner_object_id"],
    )


# Connecting --------------------------------------------------------------------------------


def start_connection(owner: models.Model, platform: str, *, redirect_target: str = "") -> str:
    """Create single-use OAuth state and PKCE data, and return the authorization URL."""
    adapter = get_adapter(platform)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    OAuthState.objects.create(
        state=state,
        code_verifier_encrypted=crypto.encrypt(verifier),
        platform=platform,
        redirect_target=redirect_target,
        expires_at=timezone.now() + timedelta(seconds=get_settings().oauth_state_ttl_seconds),
        **_owner_fields(owner),
    )
    return adapter.authorization_url(state, verifier)


def complete_connection(
    request_state: str, code: str, *, owner: models.Model | None = None
) -> ConnectedAccount:
    """Finish OAuth and store the account.

    Pass ``owner`` whenever the current user is known. It prevents a victim from being
    tricked into linking their channel to a flow an attacker started.
    """
    owner_fields = _owner_fields(owner) if owner is not None else None
    with transaction.atomic():
        oauth_state = (
            OAuthState.objects.select_for_update()
            .select_related("owner_content_type")
            .filter(state=request_state)
            .first()
        )
        if oauth_state is None or oauth_state.consumed_at is not None:
            raise OAuthStateInvalid("OAuth state is unknown or was already used.")
        if oauth_state.expires_at <= timezone.now():
            raise OAuthStateInvalid("OAuth state has expired.")
        if owner_fields is not None and (
            oauth_state.owner_content_type_id != owner_fields["owner_content_type"].pk
            or oauth_state.owner_object_id != owner_fields["owner_object_id"]
        ):
            raise OAuthStateInvalid("OAuth state belongs to a different owner.")
        oauth_state.consumed_at = timezone.now()
        oauth_state.save(update_fields=["consumed_at"])

    adapter = get_adapter(oauth_state.platform)
    verifier = crypto.decrypt(oauth_state.code_verifier_encrypted)
    tokens = adapter.exchange_code(code, verifier)
    quota.reserve(adapter.key, adapter.quota_for("connect"))
    info = adapter.fetch_account(tokens)

    with transaction.atomic():
        account, _ = ConnectedAccount.objects.update_or_create(
            owner_content_type_id=oauth_state.owner_content_type_id,
            owner_object_id=oauth_state.owner_object_id,
            platform=oauth_state.platform,
            external_account_id=info.external_id,
            defaults={
                "display_name": info.display_name,
                "status": ConnectedAccount.Status.ACTIVE,
                "scopes": list(tokens.scopes or info.scopes),
                "last_health_check_at": timezone.now(),
            },
        )
        record = OAuthToken.objects.filter(account=account).first() or OAuthToken(account=account)
        record.set_bundle(tokens)
        record.save()
        transaction.on_commit(
            lambda: signals.account_connected.send(sender=ConnectedAccount, account=account)
        )
    return account


def disconnect(account: ConnectedAccount) -> bool:
    """Revoke at the platform and delete local tokens.

    Local tokens are always deleted. Returns True when the platform confirmed the revocation
    and False when it could not, so the host can tell the owner to revoke access themselves.
    """
    record = OAuthToken.objects.filter(account=account).first()
    confirmed = record is None
    if record is not None:
        try:
            get_adapter(account.platform).revoke(record.get_bundle())
            confirmed = True
        except SocialPublisherError as exc:
            logger.warning("Revocation failed for account %s: %s", account.pk, exc.code)
    with transaction.atomic():
        OAuthToken.objects.filter(account=account).delete()
        account.status = ConnectedAccount.Status.REVOKED
        account.save(update_fields=["status", "updated_at"])
        transaction.on_commit(
            lambda: signals.account_disconnected.send(sender=ConnectedAccount, account=account)
        )
    return confirmed


def get_capabilities(account: ConnectedAccount) -> Capabilities:
    """Return what this connected account can do right now."""
    return get_adapter(account.platform).capabilities(_account_info(account))


def check_account_health(account: ConnectedAccount) -> ConnectedAccount:
    """Call the platform with the stored tokens and record that it answered."""
    adapter = get_adapter(account.platform)
    quota.reserve(adapter.key, adapter.quota_for("health"))
    adapter.fetch_account(_fresh_tokens(account, adapter))
    account.last_health_check_at = timezone.now()
    account.save(update_fields=["last_health_check_at", "updated_at"])
    return account


def purge_expired_states() -> int:
    """Delete expired OAuth state rows. Call it from a periodic job."""
    deleted, _ = OAuthState.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted


# Publishing --------------------------------------------------------------------------------


def publish(
    account: ConnectedAccount,
    media: MediaSpec,
    metadata: PublishMetadata,
    idempotency_key: str,
) -> Publication:
    """Upload media at most once per ``(account, idempotency_key)``.

    A repeated key returns the existing publication, or resumes an interrupted upload.

    Raises typed errors. After ``OutcomeUnknown`` call :func:`reconcile`; do not publish again
    under a new key until reconciliation says the upload failed.
    """
    adapter = get_adapter(account.platform)
    fingerprint = _fingerprint(media, metadata)
    existing = Publication.objects.filter(account=account, idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ValidationFailed(
                [ValidationIssue("idempotency_key", "Key was already used for different content.")]
            )
        if existing.status in _SETTLED:
            return existing

    if account.status != ConnectedAccount.Status.ACTIVE:
        raise AuthExpired(f"Account status is {account.status}; the owner must reconnect.")
    capabilities = adapter.capabilities(_account_info(account))
    if not capabilities.upload.available:
        raise CapabilityUnavailable("upload", capabilities.upload.reason)
    if metadata.contains_synthetic_media and not capabilities.ai_disclosure.available:
        raise CapabilityUnavailable("ai_disclosure", capabilities.ai_disclosure.reason)
    issues = adapter.validate(media, capabilities) + adapter.validate_metadata(
        metadata, capabilities
    )
    if issues:
        raise ValidationFailed(issues)

    publication = existing or _create_publication(
        account, idempotency_key, fingerprint, metadata.visibility
    )
    if not _claim(publication, adapter):
        publication.refresh_from_db()
        return publication
    return _upload(publication, account, adapter, media, metadata, capabilities)


def reconcile(publication: Publication) -> Publication:
    """Ask the platform for the true state of a publication.

    Only a platform-confirmed response changes the status. Errors leave it unchanged.
    """
    if publication.status not in (
        Publication.Status.OUTCOME_UNKNOWN,
        Publication.Status.PROCESSING,
    ):
        return publication
    account = publication.account
    adapter = get_adapter(account.platform)
    ref = publication.platform_media_id or f"{MARKER_REF_PREFIX}{publication.marker}"
    quota.reserve(adapter.key, adapter.quota_for("reconcile"))
    result = _with_auth_retry(account, adapter, lambda tokens: adapter.reconcile(ref, tokens))
    return _apply_result(publication, result)


def rotate_tokens() -> int:
    """Re-encrypt every stored secret with the first configured key. Returns the row count."""
    count = 0
    with transaction.atomic():
        for token in OAuthToken.objects.select_for_update():
            token.access_token_encrypted = crypto.rotate(token.access_token_encrypted)
            if token.refresh_token_encrypted:
                token.refresh_token_encrypted = crypto.rotate(token.refresh_token_encrypted)
            token.save(update_fields=["access_token_encrypted", "refresh_token_encrypted"])
            count += 1
        for state in OAuthState.objects.select_for_update().exclude(code_verifier_encrypted=""):
            state.code_verifier_encrypted = crypto.rotate(state.code_verifier_encrypted)
            state.save(update_fields=["code_verifier_encrypted"])
            count += 1
        for publication in Publication.objects.select_for_update().exclude(
            upload_session_encrypted=""
        ):
            publication.upload_session_encrypted = crypto.rotate(
                publication.upload_session_encrypted
            )
            publication.save(update_fields=["upload_session_encrypted"])
            count += 1
    return count


# Internals ---------------------------------------------------------------------------------


def _account_info(account: ConnectedAccount) -> AccountInfo:
    return AccountInfo(
        external_id=account.external_account_id,
        display_name=account.display_name,
        scopes=tuple(account.scopes),
    )


def _fingerprint(media: MediaSpec, metadata: PublishMetadata) -> str:
    payload = json.dumps(
        {"media": asdict(media), "metadata": asdict(metadata)}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _create_publication(
    account: ConnectedAccount, key: str, fingerprint: str, requested_visibility: str
) -> Publication:
    marker = hashlib.sha256(f"{account.pk}:{key}".encode()).hexdigest()[:32]
    try:
        with transaction.atomic():
            return Publication.objects.create(
                account=account,
                idempotency_key=key,
                marker=marker,
                request_fingerprint=fingerprint,
                requested_visibility=requested_visibility,
            )
    except IntegrityError:
        return Publication.objects.get(account=account, idempotency_key=key)


def _claim(publication: Publication, adapter: PlatformAdapter) -> bool:
    """Take the upload lease and reserve quota. False means someone else owns the work."""
    now = timezone.now()
    with transaction.atomic():
        current = Publication.objects.select_for_update().get(pk=publication.pk)
        if current.status in _SETTLED:
            return False
        if current.lease_expires_at and current.lease_expires_at > now:
            return False
        if current.quota_reserved_day is None:
            current.quota_reserved_day = quota.reserve(adapter.key, adapter.quota_for("publish"))
        current.lease_expires_at = now + timedelta(seconds=get_settings().upload_lease_seconds)
        _set_status(
            current, Publication.Status.UPLOADING, ["lease_expires_at", "quota_reserved_day"]
        )
    publication.refresh_from_db()
    return True


def _upload(
    publication: Publication,
    account: ConnectedAccount,
    adapter: PlatformAdapter,
    media: MediaSpec,
    metadata: PublishMetadata,
    capabilities: Capabilities,
) -> Publication:
    def checkpoint(session: str) -> None:
        lease = timezone.now() + timedelta(seconds=get_settings().upload_lease_seconds)
        # Persisted before any media bytes are sent, so a restarted worker can resume.
        Publication.objects.filter(pk=publication.pk).update(
            upload_session_encrypted=crypto.encrypt(session),
            lease_expires_at=lease,
            updated_at=timezone.now(),
        )
        publication.upload_session_encrypted = "set"

    def attempt(tokens: TokenBundle) -> PublishResult:
        stored = Publication.objects.values_list("upload_session_encrypted", flat=True).get(
            pk=publication.pk
        )
        return adapter.publish(
            PublishRequest(
                tokens=tokens,
                media=media,
                metadata=metadata,
                capabilities=capabilities,
                open_stream=lambda: get_settings().media_opener(media.ref),
                marker=publication.marker,
                resume_token=crypto.decrypt(stored) if stored else None,
                on_checkpoint=checkpoint,
            )
        )

    try:
        result = _with_auth_retry(account, adapter, attempt)
    except OutcomeUnknown as exc:
        publication.refresh_from_db()
        publication.outcome_unknown_at = timezone.now()
        publication.lease_expires_at = None
        _set_status(
            publication,
            Publication.Status.OUTCOME_UNKNOWN,
            ["outcome_unknown_at", "lease_expires_at"],
        )
        exc.publication = publication
        raise
    except (QuotaExhausted, RateLimited, AuthExpired) as exc:
        _park(publication, adapter)
        logger.info("Upload %s deferred: %s", publication.pk, exc.code)
        raise
    except PlatformRejected as exc:
        if exc.retryable:
            _park(publication, adapter)
        else:
            _fail(publication, adapter, exc.code, exc.raw_excerpt)
        raise
    except ValidationFailed as exc:
        _fail(publication, adapter, exc.code, "")
        raise
    except Exception:
        # A host or programming error, such as a failing MEDIA_OPENER. Keep the session and
        # free the lease so that the upload can resume at once.
        Publication.objects.filter(pk=publication.pk).update(lease_expires_at=None)
        raise
    return _apply_result(publication, result)


def _with_auth_retry(account: ConnectedAccount, adapter: PlatformAdapter, call: Any) -> Any:
    """Run ``call(tokens)``. If the token is rejected, force one refresh and try again."""
    try:
        return call(_fresh_tokens(account, adapter))
    except AuthExpired:
        return call(_fresh_tokens(account, adapter, force=True))


def _fresh_tokens(
    account: ConnectedAccount, adapter: PlatformAdapter, *, force: bool = False
) -> TokenBundle:
    try:
        with transaction.atomic():
            # The row lock stops concurrent workers from refreshing the same token.
            record = OAuthToken.objects.select_for_update().filter(account=account).first()
            if record is None:
                raise AuthExpired("No tokens are stored for this account.")
            tokens = record.get_bundle()
            expiring = tokens.expires_at and tokens.expires_at <= timezone.now() + REFRESH_MARGIN
            if not force and not expiring:
                return tokens
            refreshed = adapter.refresh(tokens)
            record.set_bundle(refreshed)
            record.save()
            return refreshed
    except AuthExpired:
        ConnectedAccount.objects.filter(pk=account.pk).update(
            status=ConnectedAccount.Status.NEEDS_REAUTH, updated_at=timezone.now()
        )
        account.status = ConnectedAccount.Status.NEEDS_REAUTH
        raise


def _park(publication: Publication, adapter: PlatformAdapter) -> None:
    """Stop for now without losing progress. Keep the session and quota when a session exists."""
    publication.refresh_from_db()
    publication.lease_expires_at = None
    if publication.upload_session_encrypted:
        publication.save(update_fields=["lease_expires_at", "updated_at"])
        return
    _release_quota(publication, adapter)
    _set_status(publication, Publication.Status.PENDING, ["lease_expires_at", "quota_reserved_day"])


def _fail(publication: Publication, adapter: PlatformAdapter, code: str, raw_excerpt: str) -> None:
    publication.refresh_from_db()
    _release_quota(publication, adapter)
    publication.error_code = code
    publication.raw_response_excerpt = raw_excerpt
    publication.lease_expires_at = None
    publication.upload_session_encrypted = ""
    _set_status(
        publication,
        Publication.Status.FAILED,
        [
            "error_code",
            "raw_response_excerpt",
            "lease_expires_at",
            "upload_session_encrypted",
            "quota_reserved_day",
        ],
    )


def _release_quota(publication: Publication, adapter: PlatformAdapter) -> None:
    if publication.quota_reserved_day is not None:
        quota.release(adapter.key, adapter.quota_for("publish"), publication.quota_reserved_day)
        publication.quota_reserved_day = None


def _apply_result(publication: Publication, result: PublishResult) -> Publication:
    publication.refresh_from_db()
    if result.status == "outcome_unknown":
        # Not found yet. Give up only after the grace period.
        started = publication.outcome_unknown_at or publication.updated_at
        grace = timedelta(seconds=get_settings().reconcile_grace_seconds)
        if timezone.now() - started < grace:
            return publication
        publication.error_code = "reconcile_not_found"
        publication.upload_session_encrypted = ""
        _set_status(
            publication,
            Publication.Status.FAILED,
            ["error_code", "upload_session_encrypted"],
        )
        return publication

    fields = ["lease_expires_at", "upload_session_encrypted"]
    publication.lease_expires_at = None
    publication.upload_session_encrypted = ""
    for attribute, value in (
        ("platform_media_id", result.platform_media_id),
        ("url", result.url),
        ("visibility", result.visibility),
        ("raw_response_excerpt", result.raw_excerpt),
    ):
        if value:
            setattr(publication, attribute, value)
            fields.append(attribute)
    reason = result.visibility_reason
    if (
        not reason
        and not publication.visibility_reason
        and publication.visibility
        and publication.visibility != publication.requested_visibility
    ):
        reason = (
            f"The platform reports {publication.visibility}, "
            f"but {publication.requested_visibility} was requested."
        )
    if reason:
        publication.visibility_reason = reason[:512]
        fields.append("visibility_reason")
    if result.status == "failed":
        publication.error_code = result.error_code
        fields.append("error_code")
    if result.status == "published":
        publication.published_at = timezone.now()
        fields.append("published_at")
    _set_status(publication, Publication.Status(result.status), fields)
    return publication


def _set_status(publication: Publication, status: Publication.Status, fields: list[str]) -> None:
    previous = Publication.objects.values_list("status", flat=True).get(pk=publication.pk)
    publication.status = status
    publication.save(update_fields=[*fields, "status", "updated_at"])
    if previous != status:
        transaction.on_commit(
            lambda: signals.publication_status_changed.send(
                sender=Publication, publication=publication, previous_status=previous
            )
        )
