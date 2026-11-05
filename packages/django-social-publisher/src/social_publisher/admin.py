"""Read-only admin. OAuthToken and OAuthState are deliberately not registered."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import ConnectedAccount, Publication, QuotaLedger


class _ReadOnly(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ConnectedAccount)
class ConnectedAccountAdmin(_ReadOnly):
    list_display = ("display_name", "platform", "status", "last_health_check_at")
    list_filter = ("platform", "status")


@admin.register(Publication)
class PublicationAdmin(_ReadOnly):
    list_display = ("id", "account", "status", "visibility", "error_code", "created_at")
    list_filter = ("status",)
    exclude = ("upload_session_encrypted",)


@admin.register(QuotaLedger)
class QuotaLedgerAdmin(_ReadOnly):
    list_display = ("platform", "bucket", "day", "units_used")
