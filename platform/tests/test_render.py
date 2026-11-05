"""Render tests. The ffmpeg-marked ones run in the render image and in CI."""

import shutil
import wave

import pytest
from django.test import override_settings

from apps.content.models import ContentItem
from apps.production import render as renderer
from apps.production.timing import build_srt, scene_durations

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are not on PATH",
)


def _good(**overrides):
    values = {
        "duration": 30.0,
        "width": 1080,
        "height": 1920,
        "has_audio": True,
        "has_video": True,
        "size_bytes": 1000,
    }
    return renderer.ProbeResult(**{**values, **overrides})


def test_validate_accepts_good_output():
    assert renderer.validate_output(_good(), 30.0) == []


@pytest.mark.parametrize(
    ("overrides", "expected", "text"),
    [
        ({"width": 720}, 30.0, "resolution"),
        ({"has_audio": False}, 30.0, "no audio"),
        ({"has_video": False}, 30.0, "no video"),
        ({"duration": 25.0}, 30.0, "expected"),
        ({"duration": 200.0}, 200.0, "longer than"),
        ({"size_bytes": 0}, 30.0, "empty"),
    ],
)
def test_validate_catches_each_problem(overrides, expected, text):
    assert any(
        text in problem for problem in renderer.validate_output(_good(**overrides), expected)
    )


@override_settings(MAX_RENDER_FILE_BYTES=10)
def test_validate_catches_oversized_file():
    assert any("over the limit" in p for p in renderer.validate_output(_good(), 30.0))


def test_final_mix_without_music_maps_voice_directly():
    args = renderer.final_mix_args("v.mp4", "a.mp3", "subs.srt", "o.mp4", 10.0)
    assert "1:a" in args
    assert "amix" not in " ".join(args)
    assert "subtitles=subs.srt" in " ".join(args)


def test_final_mix_with_music_ducks_and_mixes():
    args = renderer.final_mix_args("v.mp4", "a.mp3", "subs.srt", "o.mp4", 10.0, music="m.mp3")
    joined = " ".join(args)
    assert "amix=inputs=2:duration=first" in joined
    assert "volume=0.12" in joined
    assert "[a]" in args


@needs_ffmpeg
def test_real_render_produces_valid_short(tmp_path):
    import subprocess

    voice = tmp_path / "voice.wav"
    with wave.open(str(voice), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 8000 * 4)
    clips = []
    # A landscape clip and a short clip prove the crop and the looping both work.
    for index, size in enumerate(("1280x720", "720x1280")):
        clip = tmp_path / f"clip{index}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={size}:rate=24:duration=1",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        clips.append(clip)
    texts = ["first scene has some words", "second scene with more words here"]
    durations = scene_durations(texts, 4.0)
    log = renderer.CommandLog()
    final, thumb, result = renderer.render_video(
        tmp_path, clips, durations, voice, build_srt(texts, durations), None, log
    )
    assert final.exists()
    assert thumb.stat().st_size > 0
    assert (result.width, result.height) == (1080, 1920)
    assert result.has_audio
    assert result.duration == pytest.approx(4.0, abs=0.5)
    assert "subtitles=subs.srt" in log.text()


@needs_ffmpeg
@pytest.mark.django_db
def test_real_pipeline_end_to_end_with_fake_providers():
    from tests.factories import NicheFactory, UserFactory
    from tests.helpers import generate_ready_item

    user = UserFactory()
    item = generate_ready_item(user, NicheFactory(owner=user))
    assert item.status == ContentItem.Status.AWAITING_REVIEW
    job = item.render_jobs.get()
    assert job.status == "succeeded"
    assert job.output_asset.width == 1080
    assert job.output_asset.height == 1920
    assert job.output_asset.size_bytes > 0
    assert job.thumbnail_asset.size_bytes > 0
