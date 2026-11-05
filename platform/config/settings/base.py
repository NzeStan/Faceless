"""Settings shared by every environment. Secrets come from environment variables only."""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Set the {name} environment variable.")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = [host for host in env("DJANGO_ALLOWED_HOSTS", "").split(",") if host]
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
STATIC_URL = "static/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "social_publisher",
    "apps.accounts",
    "apps.content",
    "apps.production",
    "apps.publishing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DATABASES = {
    "default": dj_database_url.parse(env("DATABASE_URL"), conn_max_age=60, conn_health_checks=True)
}

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_RATES": {
        "auth": env("THROTTLE_AUTH", "10/min"),
        "generation": env("THROTTLE_GENERATION", "30/hour"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Faceless Platform API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "ENUM_NAME_OVERRIDES": {
        "ContentStatusEnum": "apps.content.models.ContentItem.Status",
        "PublicationStatusEnum": "social_publisher.models.Publication.Status",
        "AccountStatusEnum": "social_publisher.models.ConnectedAccount.Status",
    },
}

# Celery -------------------------------------------------------------------------------------
QUEUE_AI = "ai"
QUEUE_RENDER = "render"
QUEUE_PUBLISH = "publish"
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = QUEUE_AI
CELERY_TASK_ROUTES = {
    "production.script": {"queue": QUEUE_AI},
    "production.voiceover": {"queue": QUEUE_AI},
    "production.footage": {"queue": QUEUE_AI},
    "production.render": {"queue": QUEUE_RENDER},
    "publishing.*": {"queue": QUEUE_PUBLISH},
}
# Longer than the longest render, so Redis does not redeliver a task that is still running.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 4 * 60 * 60}
CELERY_BEAT_SCHEDULE = {
    "publishing-sweep": {"task": "publishing.sweep", "schedule": timedelta(seconds=120)},
}

# Storage ------------------------------------------------------------------------------------
S3_BUCKET = env("S3_BUCKET", "faceless-media")
S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", "http://localhost:9000")
S3_PUBLIC_ENDPOINT_URL = env("S3_PUBLIC_ENDPOINT_URL", S3_ENDPOINT_URL)
S3_ACCESS_KEY = env("S3_ACCESS_KEY", "")
S3_SECRET_KEY = env("S3_SECRET_KEY", "")
SIGNED_URL_TTL_SECONDS = int(env("SIGNED_URL_TTL_SECONDS", "900"))
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": S3_BUCKET,
            "endpoint_url": S3_ENDPOINT_URL,
            "access_key": S3_ACCESS_KEY,
            "secret_key": S3_SECRET_KEY,
            "region_name": env("S3_REGION", "auto"),
            "signature_version": "s3v4",
            "addressing_style": "path",
            "default_acl": None,
            "file_overwrite": False,
            "querystring_auth": True,
            "querystring_expire": SIGNED_URL_TTL_SECONDS,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Providers ----------------------------------------------------------------------------------
LLM_PROVIDER = env("LLM_PROVIDER", "providers.anthropic_llm.AnthropicLLM")
TTS_PROVIDER = env("TTS_PROVIDER", "providers.elevenlabs_tts.ElevenLabsTTS")
STOCK_PROVIDER = env("STOCK_PROVIDER", "providers.pexels_stock.PexelsStock")

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-opus-5-5")
# US dollars per million tokens: (input, output). Check current prices before relying on them.
ANTHROPIC_PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = env("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL_ID = env("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_USD_PER_1K_CHARS = float(env("ELEVENLABS_USD_PER_1K_CHARS", "0.30"))
PEXELS_API_KEY = env("PEXELS_API_KEY", "")

# Product limits -----------------------------------------------------------------------------
GENERATION_DAILY_CAP = int(env("GENERATION_DAILY_CAP", "10"))
SHORT_TARGET_SECONDS = int(env("SHORT_TARGET_SECONDS", "45"))
SHORT_MAX_DURATION_SECONDS = int(env("SHORT_MAX_DURATION_SECONDS", "180"))
MAX_RENDER_FILE_BYTES = int(env("MAX_RENDER_FILE_BYTES", str(512 * 1024 * 1024)))
RENDER_TIMEOUT_SECONDS = int(env("RENDER_TIMEOUT_SECONDS", "1200"))
SIMILARITY_WINDOW = 50
SIMILARITY_THRESHOLD = float(env("SIMILARITY_THRESHOLD", "0.6"))
SCRIPT_GENERATION_ATTEMPTS = 3
CLIP_REUSE_WINDOW = 20

# YouTube ------------------------------------------------------------------------------------
YOUTUBE_PROJECT_AUDITED = env_bool("YOUTUBE_PROJECT_AUDITED", False)
YOUTUBE_FORCE_PRIVATE_WHEN_UNAUDITED = env_bool("YOUTUBE_FORCE_PRIVATE_WHEN_UNAUDITED", True)
YOUTUBE_DAILY_UPLOAD_LIMIT = int(env("YOUTUBE_DAILY_UPLOAD_LIMIT", "100"))
SOCIAL_PUBLISHER = {
    "TOKEN_ENCRYPTION_KEYS": [
        key.strip() for key in env("SOCIAL_TOKEN_ENCRYPTION_KEYS").split(",") if key.strip()
    ],
    "MEDIA_OPENER": "apps.publishing.media.open_media",
    "PLATFORMS": {
        "youtube": {
            "CLIENT_ID": env("YOUTUBE_CLIENT_ID", "unset"),
            "CLIENT_SECRET": env("YOUTUBE_CLIENT_SECRET", "unset"),
            "REDIRECT_URI": env("YOUTUBE_REDIRECT_URI", "http://localhost:8765/oauth/callback"),
            "PROJECT_AUDITED": YOUTUBE_PROJECT_AUDITED,
            "FORCE_PRIVATE_WHEN_UNAUDITED": YOUTUBE_FORCE_PRIVATE_WHEN_UNAUDITED,
            "DAILY_UPLOAD_LIMIT": YOUTUBE_DAILY_UPLOAD_LIMIT,
            "SHORTS_MAX_DURATION_SECONDS": SHORT_MAX_DURATION_SECONDS,
        },
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        # Their debug output can include request details; keep it out of application logs.
        "anthropic": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "boto3": {"level": "WARNING"},
        "botocore": {"level": "WARNING"},
    },
}
