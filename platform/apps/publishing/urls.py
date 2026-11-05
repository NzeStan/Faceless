from django.urls import path

from .views import PublishingStatusView

urlpatterns = [path("status/", PublishingStatusView.as_view(), name="publishing-status")]
