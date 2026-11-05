"""Deterministic providers for tests and local development. They call no external service."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import wave
from decimal import Decimal
from pathlib import Path

from .base import Scene, ScriptDraft, Source, SpeechResult, StockClip

WORDS = (
    "river mountain copper lantern orbit harvest signal meadow engine violet "
    "harbor crystal thunder marble forest compass ember glacier falcon canyon "
    "saffron quartz willow tundra beacon"
).split()


def _unique_words(seed: str, count: int) -> str:
    digest = hashlib.sha256(seed.encode()).digest() * (count // 32 + 1)
    return " ".join(WORDS[byte % len(WORDS)] for byte in digest[:count])


class FakeLLM:
    def generate_script(self, *, niche, topic, avoid_hooks, avoid_scene_counts, feedback):
        seed = f"{topic}:{feedback}"
        scenes = tuple(
            Scene(f"{topic}. {_unique_words(f'{seed}:{index}', 14)}.", f"query {index}")
            for index in range(3)
        )
        return ScriptDraft(
            title=f"About {topic}"[:100],
            description=f"A short video about {topic}.",
            hook=_unique_words(f"hook:{seed}", 8),
            scenes=scenes,
            sources=(Source("Example source", "https://example.com/source"),),
            tags=("example",),
            provider="fake",
            model="fake-1",
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal("0.001000"),
        )


class FakeTTS:
    SECONDS = 6

    def synthesize(self, text, *, language):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 8000 * self.SECONDS)
        return SpeechResult(
            audio=buffer.getvalue(),
            mime_type="audio/wav",
            extension=".wav",
            provider="fake",
            voice_id="fake-voice",
            cost_usd=Decimal("0.002000"),
        )


class FakeStock:
    def search(self, query):
        digest = hashlib.sha256(query.encode()).hexdigest()
        return [
            StockClip(
                source_id=f"fake-{digest[:8]}-{index}",
                download_url=f"https://stock.example/{digest[:8]}/{index}.mp4",
                source_url=f"https://stock.example/video/{digest[:8]}-{index}",
                licence="Test licence",
                attribution="Fake Stock",
                duration_seconds=5.0,
                width=1080,
                height=1920,
                provider="fake",
            )
            for index in range(60)
        ]

    def download(self, clip, destination: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            destination.write_bytes(b"not a real video")
            return
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=720x1280:rate=24:duration=3",
                "-pix_fmt",
                "yuv420p",
                str(destination),
            ],
            check=True,
            capture_output=True,
        )
