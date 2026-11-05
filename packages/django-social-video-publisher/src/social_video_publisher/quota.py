"""Project-wide daily quota ledger, keyed by Pacific-time day."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .errors import QuotaExhausted
from .models import QuotaLedger
from .types import QuotaCharge

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_day(now: datetime | None = None) -> date:
    return (now or timezone.now()).astimezone(PACIFIC).date()


def next_reset(now: datetime | None = None) -> datetime:
    """Return the next Pacific midnight in UTC. Correct across daylight-saving changes."""
    tomorrow = pacific_day(now) + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=PACIFIC).astimezone(UTC)


def reserve(platform: str, charges: Iterable[QuotaCharge], *, now: datetime | None = None) -> date:
    """Reserve all charges or none. Raises QuotaExhausted without changing anything."""
    day = pacific_day(now)
    # A stable bucket order keeps concurrent multi-bucket reservations from deadlocking.
    with transaction.atomic():
        for charge in sorted(charges, key=lambda item: item.bucket):
            ledger, _ = QuotaLedger.objects.select_for_update().get_or_create(
                platform=platform, bucket=charge.bucket, day=day
            )
            if ledger.units_used + charge.units > charge.limit:
                raise QuotaExhausted(charge.bucket, next_reset(now))
            QuotaLedger.objects.filter(pk=ledger.pk).update(
                units_used=F("units_used") + charge.units
            )
    return day


def release(platform: str, charges: Iterable[QuotaCharge], day: date) -> None:
    with transaction.atomic():
        for charge in sorted(charges, key=lambda item: item.bucket):
            ledger = (
                QuotaLedger.objects.select_for_update()
                .filter(platform=platform, bucket=charge.bucket, day=day)
                .first()
            )
            if ledger is not None:
                ledger.units_used = max(0, ledger.units_used - charge.units)
                ledger.save(update_fields=["units_used"])


def units_used(platform: str, bucket: str, *, now: datetime | None = None) -> int:
    ledger = QuotaLedger.objects.filter(
        platform=platform, bucket=bucket, day=pacific_day(now)
    ).first()
    return ledger.units_used if ledger else 0
