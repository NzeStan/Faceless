"""Explicit field allowlists. Tokens and upload sessions must never be added here."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from social_video_publisher.models import ConnectedAccount, Publication


class ConnectedAccountSerializer(serializers.ModelSerializer[ConnectedAccount]):
    class Meta:
        model = ConnectedAccount
        fields = (
            "id",
            "platform",
            "external_account_id",
            "display_name",
            "status",
            "scopes",
            "last_health_check_at",
            "created_at",
        )
        read_only_fields = fields


class PublicationSerializer(serializers.ModelSerializer[Publication]):
    class Meta:
        model = Publication
        fields = (
            "id",
            "account",
            "idempotency_key",
            "status",
            "platform_media_id",
            "url",
            "requested_visibility",
            "visibility",
            "visibility_reason",
            "error_code",
            "published_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class StartConnectionSerializer(serializers.Serializer[Any]):
    platform = serializers.CharField(max_length=32)


class CompleteConnectionSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(max_length=128)
    code = serializers.CharField(max_length=2048)


class AuthorizationUrlSerializer(serializers.Serializer[Any]):
    authorization_url = serializers.URLField()


class DisconnectResultSerializer(serializers.Serializer[Any]):
    remote_revoked = serializers.BooleanField()
