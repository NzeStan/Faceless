"""ElevenLabs text to speech. https://elevenlabs.io/docs/api-reference/text-to-speech/convert"""

from __future__ import annotations

from decimal import Decimal

import httpx
from django.conf import settings

from .base import ProviderError, SpeechResult

BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"


class ElevenLabsTTS:
    def synthesize(self, text: str, *, language: str) -> SpeechResult:
        if not settings.ELEVENLABS_API_KEY or not settings.ELEVENLABS_VOICE_ID:
            raise ProviderError("tts_not_configured", "Set the ElevenLabs API key and voice ID.")
        voice = settings.ELEVENLABS_VOICE_ID
        try:
            response = httpx.post(
                f"{BASE_URL}/{voice}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
                json={"text": text, "model_id": settings.ELEVENLABS_MODEL_ID},
                timeout=120,
            )
        except httpx.TransportError as exc:
            raise ProviderError("tts_transport_error", "TTS unreachable.", retryable=True) from exc
        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise ProviderError(
                f"tts_http_{response.status_code}", "TTS request failed.", retryable=retryable
            )
        cost = Decimal(str(settings.ELEVENLABS_USD_PER_1K_CHARS)) * len(text) / 1000
        return SpeechResult(
            audio=response.content,
            mime_type="audio/mpeg",
            extension=".mp3",
            provider="elevenlabs",
            voice_id=voice,
            cost_usd=cost.quantize(Decimal("0.000001")),
        )
