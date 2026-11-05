"""External provider interfaces. Application code imports only from this module."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import LLMProvider, ProviderError, StockProvider, TTSProvider

__all__ = ["ProviderError", "get_llm", "get_stock", "get_tts"]


def get_llm() -> LLMProvider:
    return import_string(settings.LLM_PROVIDER)()


def get_tts() -> TTSProvider:
    return import_string(settings.TTS_PROVIDER)()


def get_stock() -> StockProvider:
    return import_string(settings.STOCK_PROVIDER)()
