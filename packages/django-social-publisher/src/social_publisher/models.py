"""Concrete models. Only ``social_publisher.services`` should change their state."""

from __future__ import annotations

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from . import crypto
from .types import TokenBundle


class ConnectedAccount(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_REAUTH = "needs_reauth", "Needs reauthorisation"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    owner_object_id = models.CharField(max_length=64)
    owner = GenericForeignKey("owner_content_type", "owner_object_id")
    platform = models.CharField(max_length=32)
    external_account_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    scopes = models.JSONField(default=list, blank=True)
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_content_type", "owner_object_id", "platform", "external_account_id"],
                name="social_publisher_unique_owner_account",
            )
        ]
        indexes = [models.Index(fields=["owner_content_type", "owner_object_id"])]

    def __str__(self) -> str:
        return f"{self.platform}:{self.display_name or self.external_account_id}"


class OAuthToken(models.Model):
    """Encrypted tokens. Never add this model to a serializer or to the admin."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        ConnectedAccount, on_delete=models.CASCADE, related_name="oauth_token"
    )
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"OAuthToken(account={self.account_id})"

    def __repr__(self) -> str:
        return f"<OAuthToken account={self.account_id}>"

    def get_bundle(self) -> TokenBundle:
        refresh = self.refresh_token_encrypted
        return TokenBundle(
            access_token=crypto.decrypt(self.access_token_encrypted),
            refresh_token=crypto.decrypt(refresh) if refresh else None,
            expires_at=self.expires_at,
        )

    def set_bundle(self, tokens: TokenBundle) -> None:
        self.access_token_encrypted = crypto.encrypt(tokens.access_token)
        if tokens.refresh_token:
            self.refresh_token_encrypted = crypto.encrypt(tokens.refresh_token)
        self.expires_at = tokens.expires_at


class OAuthState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=128, unique=True)
    code_verifier_encrypted = models.TextField(blank=True)
    owner_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    owner_object_id = models.CharField(max_length=64)
    owner = GenericForeignKey("owner_content_type", "owner_object_id")
    platform = models.CharField(max_length=32)
    redirect_target = models.CharField(max_length=2048, blank=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OAuthState({self.platform}, {self.id})"

    def __repr__(self) -> str:
        return f"<OAuthState {self.id}>"


class Publication(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        UPLOADING = "uploading", "Uploading"
        OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown"
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        ConnectedAccount, on_delete=models.CASCADE, related_name="publications"
    )
    idempotency_key = models.CharField(max_length=255)
    marker = models.CharField(max_length=32, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    platform_media_id = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1024, blank=True)
    requested_visibility = models.CharField(max_length=16, blank=True)
    visibility = models.CharField(max_length=16, blank=True)
    visibility_reason = models.CharField(max_length=512, blank=True)
    error_code = models.CharField(max_length=128, blank=True)
    raw_response_excerpt = models.TextField(blank=True)
    upload_session_encrypted = models.TextField(blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    quota_reserved_day = models.DateField(null=True, blank=True, editable=False)
    outcome_unknown_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                name="social_publisher_unique_idempotency_key",
            )
        ]
        indexes = [models.Index(fields=["status", "updated_at"])]

    def __str__(self) -> str:
        return f"Publication({self.id}, {self.status})"


class QuotaLedger(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=32)
    bucket = models.CharField(max_length=64)
    day = models.DateField(help_text="Pacific-time day the units count against.")
    units_used = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "bucket", "day"], name="social_publisher_unique_quota_day"
            )
        ]

    def __str__(self) -> str:
        return f"{self.platform}/{self.bucket} {self.day}: {self.units_used}"
