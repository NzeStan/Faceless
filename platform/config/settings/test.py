import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Test-only key, used by nothing else: base64 of a fixed 32-byte string.
os.environ.setdefault(
    "SOCIAL_TOKEN_ENCRYPTION_KEYS", "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
)
os.environ.setdefault("YOUTUBE_CLIENT_ID", "client-id")
os.environ.setdefault("YOUTUBE_CLIENT_SECRET", "client-secret")

from .base import *

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_BROKER_URL = "memory://"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = tempfile.mkdtemp(prefix="faceless-test-")
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": MEDIA_ROOT, "base_url": "/media/"},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
LLM_PROVIDER = "providers.fake.FakeLLM"
TTS_PROVIDER = "providers.fake.FakeTTS"
STOCK_PROVIDER = "providers.fake.FakeStock"
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {"auth": "1000/min", "generation": "1000/min"},
}
SOCIAL_PUBLISHER = {
    **SOCIAL_PUBLISHER,
    "PLATFORMS": {
        "youtube": {
            **SOCIAL_PUBLISHER["PLATFORMS"]["youtube"],
            "CHUNK_SIZE_BYTES": 256 * 1024,
            "RETRY_BACKOFF_SECONDS": 0,
            "DAILY_UPLOAD_LIMIT": 3,
        }
    },
}
