"""Optional DRF API, scoped to the owner returned by ``DRF_OWNER_RESOLVER``.

There is deliberately no browser-redirect callback view. The redirect target should be the
host's frontend, which posts ``state`` and ``code`` here with the signed-in user's credentials.
That ties completion to the owner who started the flow and blocks account-linking CSRF.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model, QuerySet
from django.utils.module_loading import import_string
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from social_video_publisher import services
from social_video_publisher.conf import get_settings
from social_video_publisher.errors import (
    AuthExpired,
    CapabilityUnavailable,
    OAuthStateInvalid,
    PlatformRejected,
    QuotaExhausted,
    RateLimited,
    SocialVideoPublisherError,
    ValidationFailed,
)
from social_video_publisher.models import ConnectedAccount, Publication

from .serializers import (
    CompleteConnectionSerializer,
    ConnectedAccountSerializer,
    DisconnectResultSerializer,
    PublicationSerializer,
    StartConnectionSerializer,
)

_STATUS = {
    OAuthStateInvalid: status.HTTP_400_BAD_REQUEST,
    ValidationFailed: status.HTTP_400_BAD_REQUEST,
    AuthExpired: status.HTTP_409_CONFLICT,
    CapabilityUnavailable: status.HTTP_409_CONFLICT,
    QuotaExhausted: status.HTTP_429_TOO_MANY_REQUESTS,
    RateLimited: status.HTTP_429_TOO_MANY_REQUESTS,
    PlatformRejected: status.HTTP_502_BAD_GATEWAY,
}


def error_response(exc: SocialVideoPublisherError) -> Response:
    """Convert a package error to a response that is safe to show the client."""
    body: dict[str, Any] = {"code": exc.code, "detail": str(exc)}
    headers: dict[str, str] = {}
    if isinstance(exc, QuotaExhausted):
        body["reset_at"] = exc.reset_at.isoformat()
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after)
    code = next(
        (value for kind, value in _STATUS.items() if isinstance(exc, kind)),
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    return Response(body, status=code, headers=headers)


def resolve_owner(request: Request) -> Model:
    path = get_settings().drf_owner_resolver
    if path:
        return import_string(path)(request)  # type: ignore[no-any-return]
    if not request.user.is_authenticated:
        raise ImproperlyConfigured("These views need an authenticated user or an owner resolver.")
    return request.user


class _ErrorHandling:
    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, SocialVideoPublisherError):
            return error_response(exc)
        return super().handle_exception(exc)  # type: ignore[misc, no-any-return]


class StartConnectionView(_ErrorHandling, APIView):
    serializer_class = StartConnectionSerializer

    def post(self, request: Request) -> Response:
        serializer = StartConnectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        platform = serializer.validated_data["platform"]
        if platform not in get_settings().platforms:
            return Response(
                {"code": "unknown_platform", "detail": "This platform is not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        url = services.start_connection(resolve_owner(request), platform)
        return Response({"authorization_url": url}, status=status.HTTP_201_CREATED)


class CompleteConnectionView(_ErrorHandling, APIView):
    serializer_class = CompleteConnectionSerializer

    def post(self, request: Request) -> Response:
        serializer = CompleteConnectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = services.complete_connection(
            serializer.validated_data["state"],
            serializer.validated_data["code"],
            owner=resolve_owner(request),
        )
        return Response(ConnectedAccountSerializer(account).data, status=status.HTTP_201_CREATED)


class _OwnedAccounts:
    def get_queryset(self) -> QuerySet[ConnectedAccount]:
        if getattr(self, "swagger_fake_view", False):
            return ConnectedAccount.objects.none()
        return services.accounts_for_owner(resolve_owner(self.request)).order_by(  # type: ignore[attr-defined]
            "-created_at"
        )


class _OwnedPublications:
    def get_queryset(self) -> QuerySet[Publication]:
        if getattr(self, "swagger_fake_view", False):
            return Publication.objects.none()
        return services.publications_for_owner(resolve_owner(self.request)).order_by(  # type: ignore[attr-defined]
            "-created_at"
        )


class AccountListView(_OwnedAccounts, generics.ListAPIView[ConnectedAccount]):
    serializer_class = ConnectedAccountSerializer


class AccountDetailView(_OwnedAccounts, generics.RetrieveAPIView[ConnectedAccount]):
    serializer_class = ConnectedAccountSerializer


class AccountCapabilitiesView(
    _ErrorHandling, _OwnedAccounts, generics.GenericAPIView[ConnectedAccount]
):
    serializer_class = ConnectedAccountSerializer

    def get(self, request: Request, pk: str) -> Response:
        capabilities = services.get_capabilities(self.get_object())
        return Response(
            {
                "upload": asdict(capabilities.upload),
                "public_visibility": asdict(capabilities.public_visibility),
                "ai_disclosure": asdict(capabilities.ai_disclosure),
                "media_kinds": sorted(capabilities.media_kinds),
                "max_duration_seconds": capabilities.max_duration_seconds,
                "allowed_aspect_ratios": list(capabilities.allowed_aspect_ratios),
                "daily_limits": dict(capabilities.daily_limits),
            }
        )


class AccountDisconnectView(
    _ErrorHandling, _OwnedAccounts, generics.GenericAPIView[ConnectedAccount]
):
    serializer_class = DisconnectResultSerializer

    def post(self, request: Request, pk: str) -> Response:
        return Response({"remote_revoked": services.disconnect(self.get_object())})


class PublicationListView(_OwnedPublications, generics.ListAPIView[Publication]):
    serializer_class = PublicationSerializer


class PublicationDetailView(_OwnedPublications, generics.RetrieveAPIView[Publication]):
    serializer_class = PublicationSerializer
