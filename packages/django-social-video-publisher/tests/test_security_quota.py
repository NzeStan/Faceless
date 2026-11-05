import logging
import threading
from datetime import UTC, datetime

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, connections
from django.test import override_settings

from social_video_publisher import conf, crypto, quota, services
from social_video_publisher.adapters import get_adapter, register_adapter
from social_video_publisher.adapters.youtube import UPLOAD_URL
from social_video_publisher.errors import QuotaExhausted, TokenDecryptionError
from social_video_publisher.models import OAuthState, OAuthToken, Publication, QuotaLedger
from social_video_publisher.types import AccountInfo, QuotaCharge, TokenBundle
from tests.helpers import ACCESS, REFRESH, SESSION_URI, fixture, make_account, media_spec, metadata

pytestmark = pytest.mark.django_db
CHARGE = [QuotaCharge("uploads", 1, 3)]


def test_token_values_are_not_in_any_repr_or_str():
    bundle = TokenBundle(ACCESS, REFRESH)
    record = OAuthToken.objects.get(account=make_account())
    for text in (repr(bundle), str(bundle), repr(record), str(record), repr(record.account)):
        assert ACCESS not in text
        assert REFRESH not in text
        assert record.access_token_encrypted not in text


@respx.mock
def test_tokens_and_session_never_reach_logs(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": SESSION_URI}))
    respx.put(SESSION_URI).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": "bytes=0-262143"}),
            httpx.Response(201, json=fixture("video_inserted")),
        ]
    )
    services.publish(make_account(), media_spec(), metadata(), "key-1")
    assert caplog.records, "expected httpx to log requests"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert ACCESS not in logged
    assert REFRESH not in logged
    assert "SESSION123" not in logged


def test_key_rotation_reencrypts_everything():
    old, new = (
        settings.SOCIAL_VIDEO_PUBLISHER["TOKEN_ENCRYPTION_KEYS"][0],
        Fernet.generate_key().decode(),
    )
    account = make_account()
    OAuthState.objects.create(
        state="s",
        code_verifier_encrypted=crypto.encrypt("verifier"),
        owner_content_type=account.owner_content_type,
        owner_object_id=account.owner_object_id,
        platform="youtube",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    Publication.objects.create(
        account=account,
        idempotency_key="k",
        marker="m",
        request_fingerprint="f",
        upload_session_encrypted=crypto.encrypt(SESSION_URI),
    )
    with override_settings(
        SOCIAL_VIDEO_PUBLISHER={
            **settings.SOCIAL_VIDEO_PUBLISHER,
            "TOKEN_ENCRYPTION_KEYS": [new, old],
        }
    ):
        call_command("social_video_publisher_rotate_keys", verbosity=0)
    with override_settings(
        SOCIAL_VIDEO_PUBLISHER={**settings.SOCIAL_VIDEO_PUBLISHER, "TOKEN_ENCRYPTION_KEYS": [new]}
    ):
        assert OAuthToken.objects.get().get_bundle().access_token == ACCESS
        assert OAuthToken.objects.get().get_bundle().refresh_token == REFRESH
        assert crypto.decrypt(OAuthState.objects.get().code_verifier_encrypted) == "verifier"
        assert crypto.decrypt(Publication.objects.get().upload_session_encrypted) == SESSION_URI


def test_wrong_key_raises_typed_error():
    value = crypto.encrypt("x")
    with (
        override_settings(
            SOCIAL_VIDEO_PUBLISHER={
                **settings.SOCIAL_VIDEO_PUBLISHER,
                "TOKEN_ENCRYPTION_KEYS": [Fernet.generate_key().decode()],
            }
        ),
        pytest.raises(TokenDecryptionError),
    ):
        crypto.decrypt(value)


def test_ledger_stops_at_limit():
    for _ in range(3):
        quota.reserve("youtube", CHARGE)
    with pytest.raises(QuotaExhausted):
        quota.reserve("youtube", CHARGE)
    assert quota.units_used("youtube", "uploads") == 3


def test_all_or_nothing_across_buckets():
    charges = [QuotaCharge("a", 1, 5), QuotaCharge("b", 6, 5)]
    with pytest.raises(QuotaExhausted):
        quota.reserve("youtube", charges)
    assert quota.units_used("youtube", "a") == 0


def test_pacific_day_boundary_and_reset_across_dst():
    # 07:59 UTC on 2 Mar 2026 is 23:59 on 1 Mar in Los Angeles (PST, UTC-8).
    before = datetime(2026, 3, 2, 7, 59, tzinfo=UTC)
    after = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    assert quota.pacific_day(before).isoformat() == "2026-03-01"
    assert quota.pacific_day(after).isoformat() == "2026-03-02"
    # Clocks change on 8 Mar 2026, so the following midnight is at UTC-7.
    assert quota.next_reset(datetime(2026, 3, 8, 12, tzinfo=UTC)) == datetime(
        2026, 3, 9, 7, tzinfo=UTC
    )
    assert quota.next_reset(datetime(2026, 3, 1, 12, tzinfo=UTC)) == datetime(
        2026, 3, 2, 8, tzinfo=UTC
    )


def test_release_does_not_go_negative():
    day = quota.reserve("youtube", CHARGE)
    quota.release("youtube", CHARGE, day)
    quota.release("youtube", CHARGE, day)
    assert quota.units_used("youtube", "uploads") == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    "sqlite" in settings.DATABASES["default"]["ENGINE"],
    reason="Row locks need a real database; run with TEST_POSTGRES_HOST set.",
)
def test_ledger_is_correct_under_concurrent_publishes():
    limit = 5
    workers = 20
    successes = []
    failures = []
    barrier = threading.Barrier(workers)

    def work():
        try:
            barrier.wait()
            quota.reserve("youtube", [QuotaCharge("uploads", 1, limit)])
            successes.append(1)
        except QuotaExhausted:
            failures.append(1)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert connection.vendor == "postgresql"
    assert len(successes) == limit
    assert len(failures) == workers - limit
    assert QuotaLedger.objects.get().units_used == limit


class _Custom:
    key = "custom"

    def __init__(self, config):
        self.config = config

    def capabilities(self, account):
        return "custom-capabilities"


def test_host_can_register_its_own_adapter():
    register_adapter("custom", _Custom)
    platforms = {**settings.SOCIAL_VIDEO_PUBLISHER["PLATFORMS"], "custom": {"X": 1}}
    with override_settings(
        SOCIAL_VIDEO_PUBLISHER={**settings.SOCIAL_VIDEO_PUBLISHER, "PLATFORMS": platforms}
    ):
        adapter = get_adapter("custom")
        assert adapter.config == {"X": 1}
        assert adapter.capabilities(AccountInfo("a", "b")) == "custom-capabilities"


@pytest.mark.parametrize(
    "bad",
    [
        {"TOKEN_ENCRYPTION_KEYS": []},
        {"TOKEN_ENCRYPTION_KEYS": ["not-a-key"]},
        {"TOKEN_ENCRYPTION_KEYS": "a-string"},
        {"MEDIA_OPENER": ""},
        {"MEDIA_OPENER": "no.such.module"},
        {"OWNER_MODEL": "nodot"},
        {"TYPO": 1},
        {"RECONCILE_GRACE_SECONDS": 0},
    ],
)
def test_invalid_settings_are_rejected(bad):
    with override_settings(SOCIAL_VIDEO_PUBLISHER={**settings.SOCIAL_VIDEO_PUBLISHER, **bad}):
        with pytest.raises(ImproperlyConfigured) as caught:
            conf.get_settings()
        assert "not-a-key" not in str(caught.value)
    conf.get_settings()


def test_owner_must_be_the_configured_model():
    with pytest.raises(TypeError):
        services.start_connection(make_account(), "youtube")
