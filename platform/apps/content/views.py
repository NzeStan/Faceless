from django.db.models import Sum
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from social_video_publisher.errors import SocialVideoPublisherError
from social_video_publisher.services import accounts_for_owner

from apps.production.models import Voiceover
from apps.production.tasks import start_pipeline
from apps.publishing import services as publishing

from . import services
from .models import ContentItem, Niche, Script
from .serializers import (
    ApproveSerializer,
    ContentItemDetailSerializer,
    ContentItemSerializer,
    DisclosureSerializer,
    NicheSerializer,
    RejectSerializer,
)


class NicheViewSet(viewsets.ModelViewSet):
    serializer_class = NicheSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Niche.objects.none()
        return Niche.objects.filter(owner=self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class ContentItemViewSet(viewsets.ModelViewSet):
    http_method_names = ("get", "post", "head", "options")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ContentItem.objects.none()
        return ContentItem.objects.filter(owner=self.request.user).order_by("-created_at")

    def get_serializer_class(self):
        return (
            ContentItemSerializer
            if self.action in ("list", "create")
            else (ContentItemDetailSerializer)
        )

    def get_throttles(self):
        if self.action == "create":
            self.throttle_scope = "generation"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = services.create_item(
                request.user,
                serializer.validated_data["niche"],
                serializer.validated_data["topic"],
            )
        except services.DailyCapExceeded:
            return Response(
                {"code": "daily_cap_reached", "detail": "You reached today's generation limit."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        start_pipeline(item)
        return Response(ContentItemSerializer(item).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ApproveSerializer, responses=ContentItemDetailSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        item = self.get_object()
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = (
            accounts_for_owner(request.user).filter(pk=serializer.validated_data["account"]).first()
        )
        if account is None:
            return Response(
                {"code": "unknown_account", "detail": "Connect this account first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            item = publishing.approve(
                item,
                actor=request.user,
                account=account,
                visibility=serializer.validated_data["visibility"],
                made_for_kids=serializer.validated_data["made_for_kids"],
            )
        except services.InvalidTransition as exc:
            return _conflict(exc)
        except SocialVideoPublisherError as exc:
            return Response({"code": exc.code, "detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ContentItemDetailSerializer(item).data)

    @extend_schema(request=RejectSerializer, responses=ContentItemDetailSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        item = self.get_object()
        serializer = RejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = services.transition(
                item,
                ContentItem.Status.REJECTED,
                actor=request.user,
                note=serializer.validated_data.get("reason", ""),
            )
        except services.InvalidTransition as exc:
            return _conflict(exc)
        return Response(ContentItemDetailSerializer(item).data)

    @extend_schema(request=DisclosureSerializer, responses=ContentItemDetailSerializer)
    @action(detail=True, methods=["post"])
    def disclosure(self, request, pk=None):
        item = self.get_object()
        serializer = DisclosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = services.set_disclosure(
                item, serializer.validated_data["contains_synthetic_media"], actor=request.user
            )
        except services.InvalidTransition as exc:
            return _conflict(exc)
        return Response(ContentItemDetailSerializer(item).data)

    @extend_schema(request=None, responses=ContentItemDetailSerializer)
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        item = self.get_object()
        try:
            item = publishing.retry(item, actor=request.user)
        except services.InvalidTransition as exc:
            return _conflict(exc)
        return Response(ContentItemDetailSerializer(item).data)


def _conflict(exc):
    return Response({"code": "invalid_status", "detail": str(exc)}, status=status.HTTP_409_CONFLICT)


class UsageView(APIView):
    """AI spend per step for the signed-in user, and today's remaining generations."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(responses=dict)
    def get(self, request):
        scripts = Script.objects.filter(item__owner=request.user).aggregate(
            total=Sum("cost_usd"), input=Sum("input_tokens"), output=Sum("output_tokens")
        )
        voice = Voiceover.objects.filter(item__owner=request.user).aggregate(total=Sum("cost_usd"))
        discarded = ContentItem.objects.filter(owner=request.user).aggregate(
            total=Sum("discarded_cost_usd")
        )
        script_cost = (scripts["total"] or 0) + (discarded["total"] or 0)
        voice_cost = voice["total"] or 0
        return Response(
            {
                "cost_usd": {
                    "scripting": script_cost,
                    "voiceover": voice_cost,
                    "total": script_cost + voice_cost,
                },
                "tokens": {"input": scripts["input"] or 0, "output": scripts["output"] or 0},
                "generations_left_today": services.generations_left_today(request.user),
            }
        )
