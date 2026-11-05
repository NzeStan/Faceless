import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class MediaAsset(models.Model):
    class Kind(models.TextChoices):
        VOICEOVER = "voiceover", "Voiceover"
        CLIP = "clip", "Stock clip"
        MUSIC = "music", "Music"
        VIDEO = "video", "Rendered video"
        THUMBNAIL = "thumbnail", "Thumbnail"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    item = models.ForeignKey(
        "content.ContentItem",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="assets",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    scene_index = models.PositiveIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField(default=0)
    storage_key = models.CharField(max_length=512)
    provider = models.CharField(max_length=64, blank=True)
    source_id = models.CharField(max_length=128, blank=True)
    source_url = models.URLField(max_length=1024, blank=True)
    licence = models.CharField(max_length=255, blank=True)
    attribution = models.CharField(max_length=255, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item", "scene_index"],
                condition=Q(kind="clip"),
                name="unique_clip_per_scene",
            )
        ]
        indexes = [models.Index(fields=["owner", "kind", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.kind}:{self.storage_key}"


class Voiceover(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.OneToOneField(
        "content.ContentItem", on_delete=models.CASCADE, related_name="voiceover"
    )
    asset = models.OneToOneField(MediaAsset, on_delete=models.CASCADE, related_name="voiceover")
    provider = models.CharField(max_length=64)
    voice_id = models.CharField(max_length=128, blank=True)
    duration_seconds = models.FloatField()
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Voiceover({self.item_id})"


class RenderJob(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        "content.ContentItem", on_delete=models.CASCADE, related_name="render_jobs"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    inputs = models.JSONField(default=dict)
    command_log = models.TextField(blank=True)
    progress = models.PositiveSmallIntegerField(default=0)
    output_asset = models.ForeignKey(
        MediaAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    thumbnail_asset = models.ForeignKey(
        MediaAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"RenderJob({self.item_id}, {self.status})"
