import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def client(user):
    api = APIClient()
    api.force_authenticate(user)
    return api


@pytest.fixture
def other_client(db):
    api = APIClient()
    api.force_authenticate(UserFactory())
    return api


@pytest.fixture
def fake_render(monkeypatch, tmp_path):
    """Replaces FFmpeg for tests that are about the pipeline and not about rendering."""
    from apps.production import render as renderer

    def _render(workdir, clips, durations, voice, srt_text, music, log):
        final = workdir / "final.mp4"
        thumb = workdir / "thumb.jpg"
        final.write_bytes(b"\x00" * (512 * 1024))
        thumb.write_bytes(b"jpg")
        log.lines.append("ffmpeg fake")
        result = renderer.ProbeResult(
            duration=sum(durations),
            width=1080,
            height=1920,
            has_audio=True,
            has_video=True,
            size_bytes=512 * 1024,
        )
        return final, thumb, result

    monkeypatch.setattr(renderer, "render_video", _render)
    return _render
