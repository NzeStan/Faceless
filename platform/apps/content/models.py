import uuid

from django.conf import settings
from django.db import models


class Niche(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    audience = models.CharField(max_length=255, blank=True)
    tone = models.CharField(max_length=120, blank=True)
    language = models.CharField(max_length=32, default="en")
    keywords = models.JSONField(default=list, blank=True)
    excluded_topics = models.JSONField(default=list, blank=True)
    is_factual = models.BooleanField(
        default=True, help_text="Factual niches must list sources with every script."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class ContentItem(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCRIPTING = "scripting", "Scripting"
        VOICING = "voicing", "Voicing"
        SOURCING = "sourcing", "Sourcing"
        RENDERING = "rendering", "Rendering"
        AWAITING_REVIEW = "awaiting_review", "Awaiting review"
        APPROVED = "approved", "Approved"
        QUEUED = "queued", "Queued"
        UPLOADING = "uploading", "Uploading"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    niche = models.ForeignKey(Niche, on_delete=models.PROTECT)
    topic = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    status_detail = models.CharField(
        max_length=64, blank=True, help_text="Machine-readable reason, such as quota_exhausted."
    )
    status_message = models.CharField(max_length=512, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    contains_synthetic_media = models.BooleanField(default=True)
    made_for_kids = models.BooleanField(default=False)
    requested_visibility = models.CharField(max_length=16, default="private")
    music_asset = models.ForeignKey(
        "production.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    publish_account = models.ForeignKey(
        "social_publisher.ConnectedAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    publication = models.ForeignKey(
        "social_publisher.Publication",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    publish_attempt = models.PositiveIntegerField(default=0)
    discarded_cost_usd = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        default=0,
        help_text="AI spend on attempts that produced no saved script.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["status", "next_attempt_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.topic[:40]} ({self.status})"

    @property
    def script(self):
        return self.scripts.order_by("-version").first()


class ContentEvent(models.Model):
    """Append-only audit trail of status changes and sensitive decisions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=32, default="transition")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    note = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)

    def __str__(self) -> str:
        return f"{self.kind}: {self.from_status} -> {self.to_status}"


class Script(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="scripts")
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    hook = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    scenes = models.JSONField(default=list)
    sources = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    provider = models.CharField(max_length=64)
    model = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["item", "version"], name="unique_script_version")
        ]

    def __str__(self) -> str:
        return f"{self.title} v{self.version}"
