from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.test import override_settings

from apps.content.models import ContentEvent, ContentItem, Script
from apps.content.services import InvalidTransition, create_item, transition
from apps.production import pipeline
from apps.production.models import MediaAsset, RenderJob, Voiceover
from apps.production.originality import similarity
from apps.production.timing import build_srt, scene_durations
from providers.base import Scene, ScriptDraft, Source
from providers.fake import FakeLLM
from tests.factories import ContentItemFactory, NicheFactory, UserFactory
from tests.helpers import generate_ready_item, run_pipeline

pytestmark = pytest.mark.django_db
Status = ContentItem.Status


def _item():
    return ContentItemFactory()


def test_full_pipeline_reaches_review_with_events_and_licensed_clips(fake_render):
    item = _item()
    run_pipeline(item)
    item.refresh_from_db()
    assert item.status == Status.AWAITING_REVIEW
    path = [e.to_status for e in item.events.order_by("created_at")]
    assert path == ["scripting", "voicing", "sourcing", "rendering", "awaiting_review"]
    clips = item.assets.filter(kind=MediaAsset.Kind.CLIP)
    assert clips.count() == len(item.script.scenes)
    assert all(clip.licence and clip.source_url for clip in clips)
    assert item.voiceover.duration_seconds == pytest.approx(6.0)
    assert item.render_jobs.get().status == RenderJob.Status.SUCCEEDED


@pytest.mark.parametrize(
    "steps",
    [
        ["step_script"],
        ["step_script", "step_voiceover"],
        ["step_script", "step_voiceover", "step_footage"],
        ["step_script", "step_voiceover", "step_footage", "step_render"],
    ],
)
def test_each_step_is_safe_to_run_twice(fake_render, steps):
    item = _item()
    for name in steps:
        step = getattr(pipeline, name)
        item.refresh_from_db()
        step(item)
    item.refresh_from_db()
    before = {
        "scripts": Script.objects.filter(item=item).count(),
        "voiceovers": Voiceover.objects.filter(item=item).count(),
        "assets": MediaAsset.objects.filter(item=item).count(),
        "jobs": RenderJob.objects.filter(item=item).count(),
        "events": ContentEvent.objects.filter(item=item).count(),
        "status": item.status,
    }
    last = getattr(pipeline, steps[-1])
    with mock.patch.object(FakeLLM, "generate_script", side_effect=AssertionError("paid twice")):
        last(item)
    item.refresh_from_db()
    after = {
        "scripts": Script.objects.filter(item=item).count(),
        "voiceovers": Voiceover.objects.filter(item=item).count(),
        "assets": MediaAsset.objects.filter(item=item).count(),
        "jobs": RenderJob.objects.filter(item=item).count(),
        "events": ContentEvent.objects.filter(item=item).count(),
        "status": item.status,
    }
    assert after == before


def test_earlier_step_after_later_stage_is_a_noop(fake_render):
    item = _item()
    run_pipeline(item)
    item.refresh_from_db()
    with mock.patch.object(FakeLLM, "generate_script", side_effect=AssertionError("paid again")):
        pipeline.step_script(item)
    assert item.status == Status.AWAITING_REVIEW


def test_invalid_transition_is_refused_and_writes_nothing():
    item = _item()
    with pytest.raises(InvalidTransition):
        transition(item, Status.PUBLISHED)
    assert not item.events.filter(to_status="published").exists()


def _draft(text="unique words here", hook="unique hook", sources=()):
    return ScriptDraft(
        title="T",
        description="D",
        hook=hook,
        scenes=(Scene(text, "query"),),
        sources=sources,
        provider="test",
        cost_usd=Decimal("0.01"),
    )


def test_similar_script_is_retried_then_fails_the_item():
    user = UserFactory()
    niche = NicheFactory(owner=user, is_factual=False)
    first = ContentItemFactory(owner=user, niche=niche)
    text = "the quick brown fox jumps over the lazy dog near the quiet river bank"
    Script.objects.create(item=first, title="x", text=text, provider="t", scenes=[])
    item = ContentItemFactory(owner=user, niche=niche)
    llm = mock.Mock()
    llm.generate_script.return_value = _draft(text=text)
    with (
        mock.patch("apps.production.pipeline.get_llm", return_value=llm),
        pytest.raises(pipeline.PipelineError) as caught,
    ):
        pipeline.step_script(item)
    assert caught.value.code == "too_similar"
    assert llm.generate_script.call_count == 3
    assert llm.generate_script.call_args.kwargs["feedback"] == "too_similar"


def test_similarity_only_compares_against_the_same_users_scripts():
    other = ContentItemFactory()
    text = "the quick brown fox jumps over the lazy dog near the quiet river bank"
    Script.objects.create(item=other, title="x", text=text, provider="t", scenes=[])
    niche = NicheFactory(is_factual=False)
    item = ContentItemFactory(owner=niche.owner, niche=niche)
    llm = mock.Mock()
    llm.generate_script.return_value = _draft(text=text)
    with mock.patch("apps.production.pipeline.get_llm", return_value=llm):
        pipeline.step_script(item)
    assert item.scripts.count() == 1


def test_reused_hook_is_rejected():
    user = UserFactory()
    niche = NicheFactory(owner=user, is_factual=False)
    first = ContentItemFactory(owner=user, niche=niche)
    Script.objects.create(
        item=first,
        title="x",
        text="alpha",
        hook="You will not believe this",
        provider="t",
        scenes=[],
    )
    item = ContentItemFactory(owner=user, niche=niche)
    llm = mock.Mock()
    llm.generate_script.return_value = _draft(hook="You will not believe this!")
    with (
        mock.patch("apps.production.pipeline.get_llm", return_value=llm),
        pytest.raises(pipeline.PipelineError) as caught,
    ):
        pipeline.step_script(item)
    assert caught.value.code == "hook_reused"


def test_factual_niche_requires_sources():
    item = ContentItemFactory(niche=NicheFactory(is_factual=True))
    item = ContentItem.objects.get(pk=item.pk)
    llm = mock.Mock()
    llm.generate_script.return_value = _draft(sources=())
    with (
        mock.patch("apps.production.pipeline.get_llm", return_value=llm),
        pytest.raises(pipeline.PipelineError) as caught,
    ):
        pipeline.step_script(item)
    assert caught.value.code == "sources_required"


def test_rejected_attempts_are_counted_in_the_saved_scripts_cost():
    item = ContentItemFactory(niche=NicheFactory(is_factual=True))
    llm = mock.Mock()
    source = Source("NASA", "https://science.nasa.gov/")
    llm.generate_script.side_effect = [_draft(sources=()), _draft(sources=(source,))]
    with mock.patch("apps.production.pipeline.get_llm", return_value=llm):
        pipeline.step_script(item)
    assert item.scripts.get().cost_usd == Decimal("0.02")


def test_spend_is_kept_when_every_attempt_is_rejected():
    item = ContentItemFactory(niche=NicheFactory(is_factual=True))
    llm = mock.Mock()
    llm.generate_script.return_value = _draft(sources=())
    with (
        mock.patch("apps.production.pipeline.get_llm", return_value=llm),
        pytest.raises(pipeline.PipelineError),
    ):
        pipeline.step_script(item)
    item.refresh_from_db()
    assert item.discarded_cost_usd == Decimal("0.03")


def test_stock_clip_is_not_reused_across_a_users_recent_videos(fake_render):
    user = UserFactory()
    niche = NicheFactory(owner=user)
    first = generate_ready_item(user, niche, "First topic about comets")
    second = generate_ready_item(user, niche, "Second topic about planets")
    assert first.status == second.status == Status.AWAITING_REVIEW
    first_ids = set(first.assets.filter(kind="clip").values_list("source_id", flat=True))
    second_ids = set(second.assets.filter(kind="clip").values_list("source_id", flat=True))
    assert first_ids
    assert not first_ids & second_ids


def test_no_usable_footage_fails_the_item():
    item = _item()
    pipeline.step_script(item)
    pipeline.step_voiceover(item)
    stock = mock.Mock()
    stock.search.return_value = []
    with mock.patch("apps.production.pipeline.get_stock", return_value=stock):
        from apps.production import tasks

        tasks.source_footage.apply(args=[str(item.pk)])
    item.refresh_from_db()
    assert item.status == Status.FAILED
    assert item.status_detail == "no_footage"


def test_retryable_provider_error_is_retried_then_recorded():
    from apps.production import tasks
    from providers import ProviderError

    item = _item()
    llm = mock.Mock()
    llm.generate_script.side_effect = ProviderError("llm_http_500", "down", retryable=True)
    with mock.patch("apps.production.pipeline.get_llm", return_value=llm):
        tasks.generate_script.apply(args=[str(item.pk)])
    item.refresh_from_db()
    assert item.status == Status.FAILED
    assert item.status_detail == "llm_http_500"
    assert llm.generate_script.call_count == 4


def test_failed_render_records_error_and_command_log(monkeypatch, tmp_path):
    from apps.production import render as renderer
    from apps.production import tasks

    def explode(workdir, clips, durations, voice, srt, music, log):
        log.lines.append("ffmpeg -i x")
        raise renderer.RenderError("boom")

    monkeypatch.setattr(renderer, "render_video", explode)
    item = _item()
    run_pipeline(item)
    item.refresh_from_db()
    assert item.status == Status.FAILED
    assert item.status_detail == "render_failed"
    job = item.render_jobs.get()
    assert job.status == RenderJob.Status.FAILED
    assert job.error == "boom"
    assert "ffmpeg" in job.command_log
    assert tasks.render_video.name == "production.render"


@override_settings(GENERATION_DAILY_CAP=2)
def test_daily_cap_is_enforced():
    from apps.content.services import DailyCapExceeded

    niche = NicheFactory()
    create_item(niche.owner, niche, "one")
    create_item(niche.owner, niche, "two")
    with pytest.raises(DailyCapExceeded):
        create_item(niche.owner, niche, "three")


def test_scene_durations_sum_to_total_and_follow_word_counts():
    durations = scene_durations(["one two", "one two three four", "one two"], 8.0)
    assert sum(durations) == pytest.approx(8.0)
    assert durations[1] == pytest.approx(4.0)


def test_srt_format_and_timing_end_at_total():
    srt = build_srt(["alpha beta gamma delta epsilon", "zeta"], [5.0, 1.0])
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\nalpha beta gamma delta\n")
    assert "00:00:05,000 --> 00:00:06,000\nzeta" in srt


def test_similarity_scores():
    assert similarity("hello world", "hello world") == 1.0
    assert similarity("hello world", "Hello, world!") == 1.0
    assert similarity("hello world", "completely different text") < 0.2
    assert similarity("", "x") == 0.0


def test_provider_interfaces_produce_real_files(fake_render):
    item = generate_ready_item(UserFactory(), NicheFactory())
    from django.core.files.storage import default_storage

    for asset in item.assets.all():
        assert default_storage.exists(asset.storage_key)
    assert Path(item.voiceover.asset.storage_key).suffix == ".wav"
