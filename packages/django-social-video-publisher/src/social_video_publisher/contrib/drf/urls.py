from django.urls import path

from . import views

app_name = "social_video_publisher"

urlpatterns = [
    path("connections/start/", views.StartConnectionView.as_view(), name="connection-start"),
    path(
        "connections/complete/", views.CompleteConnectionView.as_view(), name="connection-complete"
    ),
    path("accounts/", views.AccountListView.as_view(), name="account-list"),
    path("accounts/<uuid:pk>/", views.AccountDetailView.as_view(), name="account-detail"),
    path(
        "accounts/<uuid:pk>/capabilities/",
        views.AccountCapabilitiesView.as_view(),
        name="account-capabilities",
    ),
    path(
        "accounts/<uuid:pk>/disconnect/",
        views.AccountDisconnectView.as_view(),
        name="account-disconnect",
    ),
    path("publications/", views.PublicationListView.as_view(), name="publication-list"),
    path(
        "publications/<uuid:pk>/", views.PublicationDetailView.as_view(), name="publication-detail"
    ),
]
