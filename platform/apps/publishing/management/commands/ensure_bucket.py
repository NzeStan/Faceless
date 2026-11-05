"""Create the media bucket if it does not exist. Used by the local Docker stack."""

import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Wait for the S3 endpoint, then create the media bucket if it is missing."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=60, help="Seconds to wait for S3.")

    def handle(self, *args, timeout, **options):
        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1",
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
                break
            except (BotoCoreError, ClientError) as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"S3 was not reachable after {timeout}s: {exc}") from exc
                time.sleep(1)
        if settings.S3_BUCKET in existing:
            self.stdout.write(f"Bucket {settings.S3_BUCKET} already exists.")
            return
        client.create_bucket(Bucket=settings.S3_BUCKET)
        self.stdout.write(f"Created bucket {settings.S3_BUCKET}.")
