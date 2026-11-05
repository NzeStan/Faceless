"""Pexels stock video. https://www.pexels.com/api/documentation/"""

from __future__ import annotations

from pathlib import Path

import httpx
from django.conf import settings

from .base import ProviderError, StockClip

SEARCH_URL = "https://api.pexels.com/v1/videos/search"
LICENCE = "Pexels License (https://www.pexels.com/license/)"
MAX_CLIP_BYTES = 300 * 1024 * 1024


class PexelsStock:
    def search(self, query: str) -> list[StockClip]:
        if not settings.PEXELS_API_KEY:
            raise ProviderError("stock_not_configured", "Set the Pexels API key.")
        try:
            response = httpx.get(
                SEARCH_URL,
                params={
                    "query": query,
                    "orientation": "portrait",
                    "size": "medium",
                    "per_page": 15,
                },
                headers={"Authorization": settings.PEXELS_API_KEY},
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderError(
                "stock_transport_error", "Stock search unreachable.", retryable=True
            ) from exc
        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise ProviderError(
                f"stock_http_{response.status_code}", "Stock search failed.", retryable=retryable
            )
        return [clip for video in response.json().get("videos", []) if (clip := _to_clip(video))]

    def download(self, clip: StockClip, destination: Path) -> None:
        received = 0
        try:
            with (
                httpx.stream(
                    "GET", clip.download_url, timeout=120, follow_redirects=True
                ) as response,
                destination.open("wb") as target,
            ):
                if response.status_code != 200:
                    raise ProviderError(
                        f"stock_download_http_{response.status_code}",
                        "Clip download failed.",
                        retryable=response.status_code >= 500,
                    )
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > MAX_CLIP_BYTES:
                        raise ProviderError("stock_clip_too_large", "The clip is too large.")
                    target.write(chunk)
        except httpx.TransportError as exc:
            raise ProviderError(
                "stock_transport_error", "Clip download failed.", retryable=True
            ) from exc


def _to_clip(video: dict) -> StockClip | None:
    """Choose the smallest MP4 that is still at least 1080 wide or 1080 tall when portrait."""
    files = [
        item
        for item in video.get("video_files", [])
        if item.get("file_type") == "video/mp4" and item.get("link") and item.get("width")
    ]
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    sharp = [f for f in portrait if f["width"] >= 1080]
    candidates = sharp or sorted(portrait, key=lambda f: f["width"], reverse=True)[:1]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda f: f["width"])
    author = (video.get("user") or {}).get("name", "")
    return StockClip(
        source_id=f"pexels-{video['id']}",
        download_url=chosen["link"],
        source_url=video.get("url", ""),
        licence=LICENCE,
        attribution=f"{author} via Pexels" if author else "Pexels",
        duration_seconds=float(video.get("duration") or 0),
        width=chosen["width"],
        height=chosen.get("height") or 0,
        provider="pexels",
    )
