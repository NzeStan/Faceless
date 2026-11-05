"""Storage glue for media files."""

from __future__ import annotations

import boto3
from botocore.config import Config
from django.conf import settings
from django.core.files.storage import default_storage


def open_media(ref: str):
    """MEDIA_OPENER for django-social-publisher. Returns a seekable binary stream."""
    return default_storage.open(ref, "rb")


def signed_url(key: str) -> str:
    """Return an expiring URL a browser can open.

    With S3 the signature covers the host, so it is signed for the public endpoint. Inside
    Docker that differs from the address workers use to reach MinIO.
    """
    if not settings.STORAGES["default"]["BACKEND"].endswith("S3Storage"):
        return default_storage.url(key)
    client = boto3.client(
        "s3",
        endpoint_url=settings.S3_PUBLIC_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.STORAGES["default"]["OPTIONS"]["region_name"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=settings.SIGNED_URL_TTL_SECONDS,
    )
