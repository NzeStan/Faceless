from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx
import pytest
import respx
from django.test import override_settings

from providers.anthropic_llm import AnthropicLLM, ScriptOut, _cost
from providers.base import ProviderError
from providers.elevenlabs_tts import ElevenLabsTTS
from providers.pexels_stock import SEARCH_URL, PexelsStock

NICHE = SimpleNamespace(
    name="Space",
    audience="Adults",
    tone="Warm",
    language="en",
    keywords=["orbit"],
    excluded_topics=["war"],
    is_factual=True,
)
PEXELS_VIDEO = {
    "id": 42,
    "url": "https://www.pexels.com/video/42/",
    "duration": 12,
    "user": {"name": "Ada"},
    "video_files": [
        {"file_type": "video/mp4", "width": 720, "height": 1280, "link": "https://v/720.mp4"},
        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://v/1080.mp4"},
        {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://v/4k.mp4"},
        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://v/land.mp4"},
    ],
}


def _parsed_response(stop_reason="end_turn", parsed=None):
    parsed = parsed or ScriptOut(
        title="Saturn <rings>",
        description="About Saturn.",
        hook="Saturn's rings are young.",
        scenes=[{"text": "Saturn's rings are young.", "search_query": "saturn planet"}],
        sources=[{"title": "NASA", "url": "https://science.nasa.gov/saturn/"}],
        tags=["space"],
    )
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


def _generate(client):
    with (
        mock.patch("providers.anthropic_llm.anthropic.Anthropic", return_value=client),
        override_settings(ANTHROPIC_API_KEY="test-key"),
    ):
        return AnthropicLLM().generate_script(
            niche=NICHE,
            topic="Saturn",
            avoid_hooks=["old hook"],
            avoid_scene_counts=[5],
            feedback=None,
        )


def test_llm_maps_response_and_tracks_cost():
    client = mock.Mock()
    client.beta.messages.parse.return_value = _parsed_response()
    draft = _generate(client)
    assert draft.title == "Saturn rings"
    assert draft.cost_usd == Decimal("0.014000")
    assert draft.sources[0].title == "NASA"
    kwargs = client.beta.messages.parse.call_args.kwargs
    assert kwargs["output_format"] is ScriptOut
    assert kwargs["fallbacks"] == "default"
    prompt = kwargs["messages"][0]["content"]
    assert "old hook" in prompt
    assert "war" in prompt


@pytest.mark.parametrize("reason", ["refusal", "max_tokens"])
def test_llm_incomplete_or_refused_is_a_permanent_error(reason):
    client = mock.Mock()
    client.beta.messages.parse.return_value = _parsed_response(stop_reason=reason)
    with pytest.raises(ProviderError) as caught:
        _generate(client)
    assert not caught.value.retryable


def test_llm_errors_distinguish_retryable_from_permanent():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [
        (
            anthropic.RateLimitError("x", response=httpx.Response(429, request=request), body=None),
            True,
        ),
        (
            anthropic.InternalServerError(
                "x", response=httpx.Response(500, request=request), body=None
            ),
            True,
        ),
        (anthropic.APIConnectionError(request=request), True),
        (
            anthropic.BadRequestError(
                "x", response=httpx.Response(400, request=request), body=None
            ),
            False,
        ),
        (
            anthropic.AuthenticationError(
                "x", response=httpx.Response(401, request=request), body=None
            ),
            False,
        ),
    ]
    for error, retryable in cases:
        client = mock.Mock()
        client.beta.messages.parse.side_effect = error
        with pytest.raises(ProviderError) as caught:
            _generate(client)
        assert caught.value.retryable is retryable, type(error)


def test_llm_needs_a_key():
    with override_settings(ANTHROPIC_API_KEY=""), pytest.raises(ProviderError):
        AnthropicLLM().generate_script(
            niche=NICHE, topic="x", avoid_hooks=[], avoid_scene_counts=[], feedback=None
        )


def test_unknown_model_records_zero_and_does_not_crash():
    assert _cost("some-new-model", 100, 100) == Decimal(0)


@respx.mock
@override_settings(
    ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v1", ELEVENLABS_USD_PER_1K_CHARS=0.5
)
def test_tts_returns_audio_and_cost():
    route = respx.post("https://api.elevenlabs.io/v1/text-to-speech/v1").mock(
        return_value=httpx.Response(200, content=b"mp3-bytes")
    )
    result = ElevenLabsTTS().synthesize("a" * 1000, language="en")
    assert result.audio == b"mp3-bytes"
    assert result.cost_usd == Decimal("0.500000")
    assert route.calls[0].request.headers["xi-api-key"] == "k"


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (401, False)])
@override_settings(ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v1")
def test_tts_errors(status, retryable):
    respx.post("https://api.elevenlabs.io/v1/text-to-speech/v1").mock(
        return_value=httpx.Response(status)
    )
    with pytest.raises(ProviderError) as caught:
        ElevenLabsTTS().synthesize("hello", language="en")
    assert caught.value.retryable is retryable


@respx.mock
@override_settings(PEXELS_API_KEY="k")
def test_stock_search_picks_smallest_sharp_portrait_file_and_keeps_licence():
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"videos": [PEXELS_VIDEO]})
    )
    clip = PexelsStock().search("saturn")[0]
    assert clip.download_url == "https://v/1080.mp4"
    assert clip.source_id == "pexels-42"
    assert clip.source_url == "https://www.pexels.com/video/42/"
    assert "Pexels" in clip.licence
    assert clip.attribution == "Ada via Pexels"
    assert route.calls[0].request.url.params["orientation"] == "portrait"
    assert route.calls[0].request.headers["Authorization"] == "k"


@respx.mock
@override_settings(PEXELS_API_KEY="k")
def test_stock_skips_videos_with_no_portrait_file():
    video = {**PEXELS_VIDEO, "video_files": [PEXELS_VIDEO["video_files"][3]]}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"videos": [video]}))
    assert PexelsStock().search("x") == []


@respx.mock
def test_stock_download_writes_file(tmp_path):
    respx.get("https://v/1.mp4").mock(return_value=httpx.Response(200, content=b"video"))
    clip = SimpleNamespace(download_url="https://v/1.mp4")
    destination = tmp_path / "c.mp4"
    PexelsStock().download(clip, destination)
    assert destination.read_bytes() == b"video"
