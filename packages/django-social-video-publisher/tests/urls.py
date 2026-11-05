from django.urls import include, path

urlpatterns = []
try:
    import rest_framework  # noqa: F401

    urlpatterns.append(path("social/", include("social_video_publisher.contrib.drf.urls")))
except ImportError:
    pass
