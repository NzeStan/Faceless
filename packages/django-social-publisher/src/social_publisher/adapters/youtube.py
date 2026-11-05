"""YouTube Data API v3 adapter.

Platform facts were checked against Google's documentation on 4 Nov 2026:
https://developers.google.com/youtube/v3/docs/videos/insert
https://developers.google.com/youtube/v3/determine_quota_cost
https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any, BinaryIO, NoReturn
from urllib.parse import urlencode

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .. import quota
from ..errors import AuthExpired, OutcomeUnknown, PlatformRejected, QuotaExhausted, RateLimited
from ..types import (
    AccountInfo,
    Capabilities,
    Capability,
    MediaSpec,
    Operation,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    QuotaCharge,
    TokenBundle,
    ValidationIssue,
)
from . import MARKER_REF_PREFIX

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
REQUIRED_SCOPES = (SCOPE_UPLOAD, SCOPE_READONLY)

BUCKET_UPLOADS = "uploads"
BUCKET_DEFAULT = "default"

CHUNK_UNIT = 256 * 1024
DEFAULT_CHUNK_SIZE = 32 * CHUNK_UNIT  # 8 MiB
DESCRIPTION_MAX_BYTES = 5000
TITLE_MAX_CHARS = 100
TAGS_MAX_CHARS = 500
EXCERPT_MAX = 2000
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
MAX_CHUNK_RETRIES = 5
RECONCILE_PAGES = 3
PAGE_SIZE = 50
_RANGE = re.compile(r"bytes=0-(\d+)")


def _footer(marker: str) -> str:
    return f"\n\n[ref:{marker}]"


def _excerpt(response: httpx.Response) -> str:
    return response.text[:EXCERPT_MAX]


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _tags_length(tags: tuple[str, ...]) -> int:
    # YouTube counts commas between tags and implicit quotes around tags with spaces.
    per_tag = (len(tag) + (2 if " " in tag else 0) for tag in tags)
    return sum(per_tag) + max(0, len(tags) - 1)


class YouTubeAdapter:
    key = "youtube"

    def __init__(self, config: Mapping[str, Any]) -> None:
        missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI") if not config.get(k)]
        if missing:
            raise ImproperlyConfigured(f"YouTube platform settings are missing: {missing}")
        self.client_id: str = config["CLIENT_ID"]
        self.client_secret: str = config["CLIENT_SECRET"]
        self.redirect_uri: str = config["REDIRECT_URI"]
        self.project_audited = bool(config.get("PROJECT_AUDITED", False))
        self.force_private_when_unaudited = bool(config.get("FORCE_PRIVATE_WHEN_UNAUDITED", True))
        self.daily_upload_limit = int(config.get("DAILY_UPLOAD_LIMIT", 100))
        self.daily_unit_limit = int(config.get("DAILY_UNIT_LIMIT", 10_000))
        self.shorts_max_duration = int(config.get("SHORTS_MAX_DURATION_SECONDS", 180))
        self.chunk_size = int(config.get("CHUNK_SIZE_BYTES", DEFAULT_CHUNK_SIZE))
        self.retry_backoff = float(config.get("RETRY_BACKOFF_SECONDS", 1.0))
        if self.chunk_size <= 0 or self.chunk_size % CHUNK_UNIT:
            raise ImproperlyConfigured("CHUNK_SIZE_BYTES must be a multiple of 256 KiB.")

    # OAuth ---------------------------------------------------------------------------------

    def authorization_url(self, state: str, code_verifier: str | None) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(REQUIRED_SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        if code_verifier:
            params |= {"code_challenge": _challenge(code_verifier), "code_challenge_method": "S256"}
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str | None) -> TokenBundle:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        response = self._token_request(data)
        if response.status_code != 200:
            raise PlatformRejected(self._oauth_error(response) or "token_exchange_failed")
        return self._bundle(response.json())

    def refresh(self, tokens: TokenBundle) -> TokenBundle:
        if not tokens.refresh_token:
            raise AuthExpired("No refresh token is stored; the owner must reconnect.")
        response = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        if response.status_code != 200:
            error = self._oauth_error(response)
            if error == "invalid_grant":
                raise AuthExpired("Google rejected the refresh token (invalid_grant).")
            raise PlatformRejected(
                error or "token_refresh_failed", retryable=response.status_code >= 500
            )
        return self._bundle(response.json(), previous=tokens)

    def revoke(self, tokens: TokenBundle) -> None:
        token = tokens.refresh_token or tokens.access_token
        try:
            with self._client() as client:
                # Sent in the form body so that the token never appears in a logged URL.
                response = client.post(REVOKE_URL, data={"token": token})
        except httpx.TransportError as exc:
            raise PlatformRejected("revoke_transport_error", retryable=True) from exc
        if response.status_code == 200 or self._oauth_error(response) == "invalid_token":
            return
        raise PlatformRejected("revoke_failed", retryable=response.status_code >= 500)

    def fetch_account(self, tokens: TokenBundle) -> AccountInfo:
        body = self._api_get(tokens, "channels", {"part": "id,snippet", "mine": "true"})
        items = body.get("items") or []
        if not items:
            raise PlatformRejected("no_channel", "This Google account has no YouTube channel.")
        channel = items[0]
        return AccountInfo(
            external_id=channel["id"],
            display_name=channel.get("snippet", {}).get("title", ""),
            scopes=tokens.scopes,
        )

    # Capabilities and validation -----------------------------------------------------------

    def capabilities(self, account: AccountInfo) -> Capabilities:
        missing = [scope for scope in REQUIRED_SCOPES if scope not in account.scopes]
        upload = (
            Capability(False, f"Reconnect and grant the missing scopes: {', '.join(missing)}")
            if missing
            else Capability(True)
        )
        if self.project_audited or not self.force_private_when_unaudited:
            public = Capability(True)
        else:
            public = Capability(
                False,
                "Project not audited: this deployment keeps uploads private until "
                "PROJECT_AUDITED is enabled.",
            )
        return Capabilities(
            upload=upload,
            public_visibility=public,
            ai_disclosure=Capability(True),
            media_kinds=frozenset({"short", "video"}),
            max_duration_seconds=self.shorts_max_duration,
            allowed_aspect_ratios=("9:16", "1:1"),
            daily_limits=MappingProxyType(
                {BUCKET_UPLOADS: self.daily_upload_limit, BUCKET_DEFAULT: self.daily_unit_limit}
            ),
        )

    def validate(self, media: MediaSpec, capabilities: Capabilities) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if media.kind not in capabilities.media_kinds:
            issues.append(ValidationIssue("kind", f"Unsupported media kind {media.kind!r}."))
        if not media.mime_type.startswith("video/"):
            issues.append(ValidationIssue("mime_type", "Only video/* media can be uploaded."))
        if media.size_bytes <= 0:
            issues.append(ValidationIssue("size_bytes", "Media is empty."))
        if media.duration_seconds <= 0:
            issues.append(ValidationIssue("duration_seconds", "Duration must be positive."))
        if media.kind == "short":
            # YouTube has no Shorts flag. It classifies by shape and duration.
            if media.width <= 0 or media.height <= 0 or media.width > media.height:
                issues.append(
                    ValidationIssue("aspect_ratio", "A Short must be vertical or square.")
                )
            limit = capabilities.max_duration_seconds
            if limit is not None and media.duration_seconds > limit:
                issues.append(
                    ValidationIssue("duration_seconds", f"A Short can be at most {limit} seconds.")
                )
        return issues

    def validate_metadata(
        self, metadata: PublishMetadata, capabilities: Capabilities
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not metadata.title.strip():
            issues.append(ValidationIssue("title", "Title is required."))
        if len(metadata.title) > TITLE_MAX_CHARS:
            issues.append(ValidationIssue("title", f"Title exceeds {TITLE_MAX_CHARS} characters."))
        if set("<>") & set(metadata.title):
            issues.append(ValidationIssue("title", "Title cannot contain < or >."))
        if set("<>") & set(metadata.description):
            issues.append(ValidationIssue("description", "Description cannot contain < or >."))
        reserved = len(_footer("0" * 32).encode())
        if len(metadata.description.encode()) + reserved > DESCRIPTION_MAX_BYTES:
            issues.append(
                ValidationIssue(
                    "description", f"Description exceeds {DESCRIPTION_MAX_BYTES - reserved} bytes."
                )
            )
        if _tags_length(metadata.tags) > TAGS_MAX_CHARS:
            issues.append(ValidationIssue("tags", f"Tags exceed {TAGS_MAX_CHARS} characters."))
        if metadata.category is not None and not metadata.category.isdigit():
            issues.append(ValidationIssue("category", "Category must be a numeric category ID."))
        if metadata.visibility not in {"public", "unlisted", "private"}:
            issues.append(ValidationIssue("visibility", "Unknown visibility."))
        if metadata.made_for_kids is None:
            issues.append(ValidationIssue("made_for_kids", "Must be declared explicitly."))
        if metadata.contains_synthetic_media is None:
            issues.append(
                ValidationIssue("contains_synthetic_media", "Must be declared explicitly.")
            )
        return issues

    def quota_for(self, operation: Operation) -> list[QuotaCharge]:
        if operation == "publish":
            return [QuotaCharge(BUCKET_UPLOADS, 1, self.daily_upload_limit)]
        # Worst case: channels.list + up to RECONCILE_PAGES playlistItems.list + videos.list.
        units = {"connect": 1, "health": 1, "reconcile": 2 + RECONCILE_PAGES}[operation]
        return [QuotaCharge(BUCKET_DEFAULT, units, self.daily_unit_limit)]

    # Publishing ----------------------------------------------------------------------------

    def publish(self, request: PublishRequest) -> PublishResult:
        visibility, reason = self._effective_visibility(request)
        with self._client() as client:
            session = request.resume_token
            offset = 0
            if session:
                response = self._query_session(
                    client, request, session, retry_exhausted_unknown=False
                )
                if response.status_code in (200, 201):
                    return self._completed(response, visibility, reason)
                offset = self._next_offset(response)
            else:
                session = self._initiate(client, request, visibility)
                request.on_checkpoint(session)
            with request.open_stream() as stream:
                return self._send(client, request, session, stream, offset, visibility, reason)

    def reconcile(self, publication_ref: str, tokens: TokenBundle) -> PublishResult:
        video_id = publication_ref
        if publication_ref.startswith(MARKER_REF_PREFIX):
            marker = publication_ref.removeprefix(MARKER_REF_PREFIX)
            found = self._find_by_marker(tokens, marker)
            if found is None:
                return PublishResult(status="outcome_unknown")
            video_id = found
        return self._video_status(tokens, video_id)

    # Internals -----------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False)

    def _token_request(self, data: Mapping[str, str]) -> httpx.Response:
        try:
            with self._client() as client:
                return client.post(TOKEN_URL, data=data)
        except httpx.TransportError as exc:
            raise PlatformRejected("token_transport_error", retryable=True) from exc

    @staticmethod
    def _oauth_error(response: httpx.Response) -> str:
        try:
            error = response.json().get("error", "")
        except (ValueError, AttributeError):
            return ""
        return error if isinstance(error, str) else ""

    @staticmethod
    def _bundle(body: Mapping[str, Any], previous: TokenBundle | None = None) -> TokenBundle:
        expires_in = body.get("expires_in")
        scope = body.get("scope")
        return TokenBundle(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token")
            or (previous.refresh_token if previous else None),
            expires_at=timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None,
            scopes=tuple(scope.split()) if scope else (previous.scopes if previous else ()),
        )

    def _effective_visibility(self, request: PublishRequest) -> tuple[str, str]:
        requested = request.metadata.visibility
        public = request.capabilities.public_visibility
        if requested != "private" and not public.available:
            return "private", public.reason or "Public visibility is unavailable."
        return requested, ""

    def _initiate(self, client: httpx.Client, request: PublishRequest, visibility: str) -> str:
        metadata = request.metadata
        body = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description + _footer(request.marker),
                "tags": list(metadata.tags),
                "categoryId": metadata.category or "22",
            },
            "status": {
                # Always explicit: YouTube defaults an omitted privacyStatus to public.
                "privacyStatus": visibility,
                "selfDeclaredMadeForKids": metadata.made_for_kids,
                "containsSyntheticMedia": metadata.contains_synthetic_media,
            },
        }
        try:
            response = client.post(
                UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {request.tokens.access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(request.media.size_bytes),
                    "X-Upload-Content-Type": request.media.mime_type,
                },
                content=json.dumps(body),
            )
        except httpx.TransportError as exc:
            # No media bytes were sent and a session alone never creates a video.
            raise PlatformRejected("transport_error", str(exc), retryable=True) from exc
        location = response.headers.get("Location")
        if response.status_code == 200 and location:
            return str(location)
        self._raise_for_error(response)

    def _send(
        self,
        client: httpx.Client,
        request: PublishRequest,
        session: str,
        stream: BinaryIO,
        offset: int,
        visibility: str,
        reason: str,
    ) -> PublishResult:
        total = request.media.size_bytes
        retries = 0
        self._seek(stream, offset)
        while True:
            chunk = stream.read(self.chunk_size)
            if not chunk:
                raise PlatformRejected(
                    "media_truncated", "The media stream ended before the declared size."
                )
            last = offset + len(chunk) - 1
            try:
                response = client.put(
                    session,
                    headers={
                        "Authorization": f"Bearer {request.tokens.access_token}",
                        "Content-Type": request.media.mime_type,
                        "Content-Range": f"bytes {offset}-{last}/{total}",
                    },
                    content=chunk,
                )
            except httpx.TransportError as exc:
                raise OutcomeUnknown("The connection failed while sending media.") from exc

            status = response.status_code
            if status in (200, 201):
                return self._completed(response, visibility, reason)
            if status == 308:
                offset = self._next_offset(response)
                retries = 0
                request.on_checkpoint(session)
            elif status in RETRYABLE_STATUSES:
                retries += 1
                if retries > MAX_CHUNK_RETRIES:
                    raise OutcomeUnknown(f"The upload kept failing with HTTP {status}.")
                time.sleep(self.retry_backoff * 2 ** (retries - 1))
                query = self._query_session(client, request, session, retry_exhausted_unknown=True)
                if query.status_code in (200, 201):
                    return self._completed(query, visibility, reason)
                offset = self._next_offset(query)
            elif status == 404:
                raise OutcomeUnknown("The upload session is gone; the upload may have finished.")
            else:
                self._raise_for_error(response)
            self._seek(stream, offset)

    def _query_session(
        self,
        client: httpx.Client,
        request: PublishRequest,
        session: str,
        *,
        retry_exhausted_unknown: bool,
    ) -> httpx.Response:
        """Ask how much of the upload arrived. Returns a 200, 201 or 308 response."""
        try:
            response = client.put(
                session,
                headers={
                    "Authorization": f"Bearer {request.tokens.access_token}",
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{request.media.size_bytes}",
                },
            )
        except httpx.TransportError as exc:
            if retry_exhausted_unknown:
                raise OutcomeUnknown("The connection failed during an upload.") from exc
            # The session stays valid and is checked again on the next attempt.
            raise PlatformRejected("transport_error", str(exc), retryable=True) from exc
        status = response.status_code
        if status in (200, 201, 308):
            return response
        if status == 404:
            raise OutcomeUnknown(
                "The upload session is gone; the earlier upload may have finished."
            )
        if status in RETRYABLE_STATUSES and not retry_exhausted_unknown:
            raise PlatformRejected(f"http_{status}", retryable=True)
        if status in RETRYABLE_STATUSES:
            raise OutcomeUnknown(f"The upload status check failed with HTTP {status}.")
        self._raise_for_error(response)

    @staticmethod
    def _next_offset(response: httpx.Response) -> int:
        match = _RANGE.fullmatch(response.headers.get("Range", ""))
        return int(match.group(1)) + 1 if match else 0

    @staticmethod
    def _seek(stream: BinaryIO, offset: int) -> None:
        if stream.seekable():
            stream.seek(offset)
            return
        # Non-seekable streams only work from the start. A later rewind cannot happen, so
        # read forward when possible and otherwise refuse instead of sending wrong bytes.
        position = stream.tell() if hasattr(stream, "tell") else 0
        if offset < position:
            raise PlatformRejected(
                "media_not_seekable", "MEDIA_OPENER must return a seekable stream to resume."
            )
        remaining = offset - position
        while remaining:
            skipped = stream.read(min(remaining, DEFAULT_CHUNK_SIZE))
            if not skipped:
                raise PlatformRejected("media_truncated", "The media stream ended early.")
            remaining -= len(skipped)

    @staticmethod
    def _completed(response: httpx.Response, visibility: str, reason: str) -> PublishResult:
        try:
            body = response.json()
            video_id = str(body["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OutcomeUnknown("Upload finished without a readable video ID.") from exc
        return PublishResult(
            status="processing",
            platform_media_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            visibility=body.get("status", {}).get("privacyStatus", visibility),
            visibility_reason=reason,
            raw_excerpt=_excerpt(response),
        )

    def _api_get(self, tokens: TokenBundle, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(
                    f"{API_URL}/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {tokens.access_token}"},
                )
        except httpx.TransportError as exc:
            raise PlatformRejected("transport_error", str(exc), retryable=True) from exc
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            return body
        self._raise_for_error(response)

    def _find_by_marker(self, tokens: TokenBundle, marker: str) -> str | None:
        channels = self._api_get(tokens, "channels", {"part": "contentDetails", "mine": "true"})
        items = channels.get("items") or []
        if not items:
            raise PlatformRejected("no_channel", "This Google account has no YouTube channel.")
        playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        needle = _footer(marker).strip()
        page_token = ""
        for _ in range(RECONCILE_PAGES):
            params = {"part": "snippet", "playlistId": playlist, "maxResults": str(PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token
            body = self._api_get(tokens, "playlistItems", params)
            for item in body.get("items") or []:
                snippet = item.get("snippet", {})
                if needle in snippet.get("description", ""):
                    return str(snippet["resourceId"]["videoId"])
            page_token = body.get("nextPageToken", "")
            if not page_token:
                break
        return None

    def _video_status(self, tokens: TokenBundle, video_id: str) -> PublishResult:
        body = self._api_get(tokens, "videos", {"part": "status,processingDetails", "id": video_id})
        items = body.get("items") or []
        url = f"https://www.youtube.com/watch?v={video_id}"
        if not items:
            return PublishResult(
                status="failed", platform_media_id=video_id, url=url, error_code="video_not_found"
            )
        video = items[0]
        status = video.get("status", {})
        upload_status = status.get("uploadStatus", "")
        processing = video.get("processingDetails", {}).get("processingStatus", "")
        common: dict[str, Any] = {
            "platform_media_id": video_id,
            "url": url,
            "visibility": status.get("privacyStatus", ""),
            "raw_excerpt": json.dumps(video)[:EXCERPT_MAX],
        }
        if upload_status == "rejected":
            code = f"rejected:{status.get('rejectionReason', 'unknown')}"
            return PublishResult(status="failed", error_code=code, **common)
        if upload_status == "failed":
            code = f"failed:{status.get('failureReason', 'unknown')}"
            return PublishResult(status="failed", error_code=code, **common)
        if upload_status == "deleted":
            return PublishResult(status="failed", error_code="deleted", **common)
        if processing in ("failed", "terminated"):
            return PublishResult(status="failed", error_code=f"processing_{processing}", **common)
        if upload_status == "processed" or processing == "succeeded":
            return PublishResult(status="published", **common)
        return PublishResult(status="processing", **common)

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> NoReturn:
        status = response.status_code
        reason = ""
        message = ""
        try:
            error = response.json().get("error", {})
            if isinstance(error, dict):
                message = error.get("message", "")
                errors = error.get("errors") or []
                reason = errors[0].get("reason", "") if errors else ""
        except (ValueError, AttributeError, IndexError):
            pass
        if status == 401:
            raise AuthExpired(message or "The access token was rejected.")
        if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            header = response.headers.get("Retry-After", "")
            raise RateLimited(int(header) if header.isdigit() else 60)
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            raise QuotaExhausted(BUCKET_UPLOADS, quota.next_reset())
        raise PlatformRejected(
            reason or f"http_{status}",
            message,
            retryable=status in RETRYABLE_STATUSES,
            raw_excerpt=_excerpt(response),
        )
