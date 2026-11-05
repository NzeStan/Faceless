import os

SECRET_KEY = "test-only"
DEBUG = False
USE_TZ = True
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ROOT_URLCONF = "tests.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.admin",
    "django.contrib.messages",
    "social_video_publisher",
]
try:
    import rest_framework  # noqa: F401

    INSTALLED_APPS.append("rest_framework")
except ImportError:
    pass

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.request",
            ]
        },
    }
]

if os.environ.get("TEST_POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ["TEST_POSTGRES_HOST"],
            "PORT": os.environ.get("TEST_POSTGRES_PORT", "5432"),
            "NAME": "postgres",
            "USER": "postgres",
            "PASSWORD": os.environ.get("TEST_POSTGRES_PASSWORD", "postgres"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

SOCIAL_VIDEO_PUBLISHER = {
    # Test-only key, used by nothing else: base64 of a fixed 32-byte string.
    "TOKEN_ENCRYPTION_KEYS": ["MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="],
    "MEDIA_OPENER": "tests.helpers.open_media",
    "PLATFORMS": {
        "youtube": {
            "CLIENT_ID": "client-id",
            "CLIENT_SECRET": "client-secret",
            "REDIRECT_URI": "https://app.example/oauth/callback",
            "PROJECT_AUDITED": False,
            "DAILY_UPLOAD_LIMIT": 3,
            "CHUNK_SIZE_BYTES": 256 * 1024,
            "RETRY_BACKOFF_SECONDS": 0,
        },
    },
}
