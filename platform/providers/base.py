"""Provider interfaces and value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol


class ProviderError(Exception):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Scene:
    text: str
    search_query: str


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True)
class ScriptDraft:
    title: str
    description: str
    hook: str
    scenes: tuple[Scene, ...]
    sources: tuple[Source, ...] = ()
    tags: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)

    @property
    def text(self) -> str:
        return " ".join(scene.text for scene in self.scenes)


@dataclass(frozen=True)
class SpeechResult:
    audio: bytes = field(repr=False)
    mime_type: str
    extension: str
    provider: str
    voice_id: str = ""
    cost_usd: Decimal = Decimal(0)


@dataclass(frozen=True)
class StockClip:
    source_id: str
    download_url: str
    source_url: str
    licence: str
    attribution: str
    duration_seconds: float
    width: int
    height: int
    provider: str
    mime_type: str = "video/mp4"


class LLMProvider(Protocol):
    def generate_script(
        self,
        *,
        niche: Any,
        topic: str,
        avoid_hooks: list[str],
        avoid_scene_counts: list[int],
        feedback: str | None,
    ) -> ScriptDraft: ...


class TTSProvider(Protocol):
    def synthesize(self, text: str, *, language: str) -> SpeechResult: ...


class StockProvider(Protocol):
    def search(self, query: str) -> list[StockClip]: ...
    def download(self, clip: StockClip, destination: Path) -> None: ...
