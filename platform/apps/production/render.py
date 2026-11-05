"""FFmpeg render pipeline. Runs only on render workers."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

WIDTH = 1080
HEIGHT = 1920
FPS = 30
SUBTITLE_STYLE = (
    "FontName=DejaVu Sans,FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=70"
)


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    width: int
    height: int
    has_audio: bool
    has_video: bool
    size_bytes: int


class CommandLog:
    """Collects the commands a render ran. They contain only local temporary paths."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def run(self, args: list[str], cwd: Path) -> None:
        self.lines.append(shlex.join(args))
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=settings.RENDER_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise RenderError(f"{args[0]} exited with {result.returncode}: {result.stderr[-1500:]}")

    def text(self) -> str:
        return "\n".join(self.lines)


def normalise_clip_args(source: str, output: str, duration: float) -> list[str]:
    """Loop, cut, scale and crop one clip to exact scene length with no audio."""
    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p"
    )
    return [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        source,
        "-t",
        f"{duration:.3f}",
        "-an",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        output,
    ]


def concat_args(list_file: str, output: str) -> list[str]:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output]


def final_mix_args(
    video: str, voice: str, subtitles: str, output: str, duration: float, music: str | None = None
) -> list[str]:
    """Burn subtitles and mix audio. ``subtitles`` must be a plain file name in the cwd."""
    args = ["ffmpeg", "-y", "-i", video, "-i", voice]
    graph = f"[0:v]subtitles={subtitles}:force_style='{SUBTITLE_STYLE}'[v]"
    if music:
        args += ["-stream_loop", "-1", "-i", music]
        graph += ";[2:a]volume=0.12[m];[1:a][m]amix=inputs=2:duration=first[a]"
        audio_map = "[a]"
    else:
        audio_map = "1:a"
    return [
        *args,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        audio_map,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-r",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        output,
    ]


def thumbnail_args(video: str, output: str, at_seconds: float = 1.0) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{at_seconds:.3f}",
        "-i",
        video,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        output,
    ]


def probe(path: Path) -> ProbeResult:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RenderError(f"ffprobe failed: {result.stderr[-500:]}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    return ProbeResult(
        duration=float(data.get("format", {}).get("duration", 0)),
        width=int(video["width"]) if video else 0,
        height=int(video["height"]) if video else 0,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        has_video=video is not None,
        size_bytes=path.stat().st_size,
    )


def validate_output(result: ProbeResult, expected_duration: float) -> list[str]:
    """Return every reason the rendered file should not go to review."""
    problems = []
    if not result.has_video:
        problems.append("no video stream")
    if (result.width, result.height) != (WIDTH, HEIGHT):
        problems.append(f"resolution is {result.width}x{result.height}")
    if not result.has_audio:
        problems.append("no audio stream")
    if abs(result.duration - expected_duration) > 1.0:
        problems.append(f"duration {result.duration:.2f}s, expected {expected_duration:.2f}s")
    if result.duration > settings.SHORT_MAX_DURATION_SECONDS:
        problems.append(f"longer than {settings.SHORT_MAX_DURATION_SECONDS}s")
    if result.size_bytes <= 0:
        problems.append("file is empty")
    if result.size_bytes > settings.MAX_RENDER_FILE_BYTES:
        problems.append(f"file is {result.size_bytes} bytes, over the limit")
    return problems


def render_video(
    workdir: Path,
    clips: list[Path],
    durations: list[float],
    voice: Path,
    srt_text: str,
    music: Path | None,
    log: CommandLog,
) -> tuple[Path, Path, ProbeResult]:
    """Render final.mp4 and thumb.jpg inside workdir, and validate the result."""
    parts = []
    for index, (clip, duration) in enumerate(zip(clips, durations, strict=True)):
        part = f"scene_{index:03}.mp4"
        log.run(normalise_clip_args(str(clip), part, duration), workdir)
        parts.append(part)
    (workdir / "scenes.txt").write_text("".join(f"file '{part}'\n" for part in parts))
    log.run(concat_args("scenes.txt", "video.mp4"), workdir)
    (workdir / "subs.srt").write_text(srt_text, encoding="utf-8")
    total = sum(durations)
    log.run(
        final_mix_args(
            "video.mp4", str(voice), "subs.srt", "final.mp4", total, str(music) if music else None
        ),
        workdir,
    )
    log.run(thumbnail_args("final.mp4", "thumb.jpg", min(1.0, total / 2)), workdir)
    final = workdir / "final.mp4"
    result = probe(final)
    problems = validate_output(result, total)
    if problems:
        raise RenderError("Output failed validation: " + "; ".join(problems))
    return final, workdir / "thumb.jpg", result
