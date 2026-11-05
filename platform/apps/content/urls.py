from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ContentItemViewSet, NicheViewSet, UsageView

router = DefaultRouter()
router.register("niches", NicheViewSet, basename="niche")
router.register("content", ContentItemViewSet, basename="content")

urlpatterns = [path("usage/", UsageView.as_view(), name="usage"), *router.urls]
