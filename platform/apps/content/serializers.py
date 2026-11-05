from rest_framework import serializers
from social_video_publisher.contrib.drf.serializers import PublicationSerializer

from apps.production.models import MediaAsset, RenderJob, Voiceover
from apps.publishing.media import signed_url

from .models import ContentEvent, ContentItem, Niche, Script


class NicheSerializer(serializers.ModelSerializer):
    class Meta:
        model = Niche
        fields = (
            "id",
            "name",
            "audience",
            "tone",
            "language",
            "keywords",
            "excluded_topics",
            "is_factual",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_keywords(self, value):
        return _string_list(value)

    def validate_excluded_topics(self, value):
        return _string_list(value)


def _string_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise serializers.ValidationError("Must be a list of strings.")
    return value


class ScriptSerializer(serializers.ModelSerializer):
    sources_verified = serializers.SerializerMethodField(
        help_text="Always false. Sources were suggested by AI and nobody has checked them."
    )

    class Meta:
        model = Script
        fields = (
            "id",
            "version",
            "title",
            "description",
            "hook",
            "text",
            "scenes",
            "sources",
            "sources_verified",
            "tags",
            "provider",
            "model",
            "cost_usd",
            "created_at",
        )
        read_only_fields = fields

    def get_sources_verified(self, obj) -> bool:
        return False


class MediaAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = MediaAsset
        fields = (
            "id",
            "kind",
            "scene_index",
            "mime_type",
            "size_bytes",
            "duration_seconds",
            "provider",
            "source_url",
            "licence",
            "attribution",
        )
        read_only_fields = fields


class VoiceoverSerializer(serializers.ModelSerializer):
    class Meta:
        model = Voiceover
        fields = ("id", "provider", "voice_id", "duration_seconds", "cost_usd", "created_at")
        read_only_fields = fields


class RenderJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = RenderJob
        fields = ("id", "status", "progress", "error", "started_at", "finished_at")
        read_only_fields = fields


class ContentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentEvent
        fields = ("kind", "from_status", "to_status", "note", "created_at")
        read_only_fields = fields


class ContentItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentItem
        fields = (
            "id",
            "niche",
            "topic",
            "status",
            "status_detail",
            "status_message",
            "next_attempt_at",
            "contains_synthetic_media",
            "requested_visibility",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "status_detail",
            "status_message",
            "next_attempt_at",
            "contains_synthetic_media",
            "requested_visibility",
            "created_at",
            "updated_at",
        )

    def validate_niche(self, value):
        if value.owner_id != self.context["request"].user.pk:
            # The same message as an unknown ID, so other users' IDs cannot be probed.
            raise serializers.ValidationError("Object does not exist.")
        return value


class ContentItemDetailSerializer(ContentItemSerializer):
    script = ScriptSerializer(read_only=True)
    voiceover = VoiceoverSerializer(read_only=True)
    clips = serializers.SerializerMethodField()
    render = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    publication = PublicationSerializer(read_only=True)
    events = ContentEventSerializer(many=True, read_only=True)

    class Meta(ContentItemSerializer.Meta):
        fields = (
            *ContentItemSerializer.Meta.fields,
            "script",
            "voiceover",
            "clips",
            "render",
            "preview_url",
            "thumbnail_url",
            "publication",
            "events",
        )

    def _job(self, obj):
        return obj.render_jobs.order_by("-created_at").first()

    def get_clips(self, obj) -> list[dict]:
        clips = obj.assets.filter(kind=MediaAsset.Kind.CLIP).order_by("scene_index")
        return MediaAssetSerializer(clips, many=True).data

    def get_render(self, obj) -> dict | None:
        job = self._job(obj)
        return RenderJobSerializer(job).data if job else None

    def get_preview_url(self, obj) -> str | None:
        job = self._job(obj)
        return signed_url(job.output_asset.storage_key) if job and job.output_asset else None

    def get_thumbnail_url(self, obj) -> str | None:
        job = self._job(obj)
        if job and job.thumbnail_asset:
            return signed_url(job.thumbnail_asset.storage_key)
        return None


class ApproveSerializer(serializers.Serializer):
    account = serializers.UUIDField(help_text="Connected YouTube account to upload to.")
    visibility = serializers.ChoiceField(
        choices=["private", "unlisted", "public"], default="private"
    )
    made_for_kids = serializers.BooleanField(
        help_text="YouTube requires an explicit answer for every upload."
    )


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class DisclosureSerializer(serializers.Serializer):
    contains_synthetic_media = serializers.BooleanField()
    confirm = serializers.BooleanField(
        default=False, help_text="Must be true to switch the disclosure off."
    )

    def validate(self, attrs):
        if not attrs["contains_synthetic_media"] and not attrs["confirm"]:
            raise serializers.ValidationError(
                {"confirm": "Confirm that this video has no realistic altered or synthetic media."}
            )
        return attrs
