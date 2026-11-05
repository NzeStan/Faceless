"""Pipeline steps. Each is idempotent: running one twice leaves the same result and cost."""

from __future__ import annotations

import io
import logging
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from mutagen import File as MutagenFile

from apps.content.models import ContentItem, Script
from apps.content.services import transition
from providers import ProviderError, get_llm, get_stock, get_tts
from providers.base import ScriptDraft

from . import render as renderer
from .models import MediaAsset, RenderJob, Voiceover
from .originality import similarity, too_similar
from .timing import build_srt, scene_durations

logger = logging.getLogger(__name__)
Status = ContentItem.Status

ORDER = [
    Status.DRAFT,
    Status.SCRIPTING,
    Status.VOICING,
    Status.SOURCING,
    Status.RENDERING,
    Status.AWAITING_REVIEW,
]


class PipelineError(Exception):
    """A permanent failure. The item moves to failed with this code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _enter(item: ContentItem, stage: str) -> bool:
    """Move into a stage if needed. False means the item has already passed it."""
    if item.status not in ORDER:
        return False
    current, wanted = ORDER.index(Status(item.status)), ORDER.index(Status(stage))
    if current > wanted:
        return False
    if current == wanted - 1:
        transition(item, stage)
    return item.status == stage


def _key(item: ContentItem, name: str) -> str:
    return f"users/{item.owner_id}/items/{item.pk}/{name}"


# Script -------------------------------------------------------------------------------------


def _recent_scripts(item: ContentItem) -> list[Script]:
    return list(
        Script.objects.filter(item__owner=item.owner)
        .exclude(item=item)
        .order_by("-created_at")[: settings.SIMILARITY_WINDOW]
    )


def _rejection_reason(draft: ScriptDraft, recent: list[Script], item: ContentItem) -> str | None:
    if item.niche.is_factual and not draft.sources:
        return "sources_required"
    if too_similar(draft.text, [s.text for s in recent], settings.SIMILARITY_THRESHOLD):
        return "too_similar"
    if any(similarity(draft.hook, s.hook) >= 0.8 for s in recent if s.hook):
        return "hook_reused"
    return None


def _record_discarded(item: ContentItem, cost: Decimal) -> None:
    if cost:
        ContentItem.objects.filter(pk=item.pk).update(
            discarded_cost_usd=F("discarded_cost_usd") + cost
        )


def step_script(item: ContentItem) -> None:
    if not _enter(item, Status.SCRIPTING) or item.scripts.exists():
        return
    recent = _recent_scripts(item)
    llm = get_llm()
    total_cost = Decimal(0)
    input_tokens = output_tokens = 0
    reason = None
    try:
        for _ in range(settings.SCRIPT_GENERATION_ATTEMPTS):
            draft = llm.generate_script(
                niche=item.niche,
                topic=item.topic,
                avoid_hooks=[s.hook for s in recent[:15] if s.hook],
                avoid_scene_counts=[len(s.scenes) for s in recent[:3]],
                feedback=reason,
            )
            total_cost += draft.cost_usd
            input_tokens += draft.input_tokens
            output_tokens += draft.output_tokens
            reason = _rejection_reason(draft, recent, item)
            if reason is None:
                break
    except ProviderError:
        _record_discarded(item, total_cost)
        raise
    if reason is not None:
        # Rejected attempts still cost money, so the spend stays on the books.
        _record_discarded(item, total_cost)
        raise PipelineError(reason, f"No acceptable script after several attempts: {reason}.")
    try:
        with transaction.atomic():
            Script.objects.create(
                item=item,
                version=1,
                title=draft.title,
                description=draft.description,
                hook=draft.hook,
                text=" ".join(scene.text for scene in draft.scenes),
                scenes=[
                    {"text": scene.text, "search_query": scene.search_query}
                    for scene in draft.scenes
                ],
                sources=[{"title": s.title, "url": s.url} for s in draft.sources],
                tags=list(draft.tags),
                provider=draft.provider,
                model=draft.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=total_cost,
            )
    except IntegrityError:
        logger.info("Item %s already has a script from a concurrent run.", item.pk)


# Voiceover ----------------------------------------------------------------------------------


def _measure_duration(data: bytes) -> float:
    audio = MutagenFile(io.BytesIO(data))
    if audio is None or not audio.info or audio.info.length <= 0:
        raise PipelineError("voice_unreadable", "The voiceover audio could not be measured.")
    return float(audio.info.length)


def step_voiceover(item: ContentItem) -> None:
    if not _enter(item, Status.VOICING) or Voiceover.objects.filter(item=item).exists():
        return
    script = item.script
    result = get_tts().synthesize(script.text, language=item.niche.language)
    duration = _measure_duration(result.audio)
    if duration > settings.SHORT_MAX_DURATION_SECONDS:
        raise PipelineError("voice_too_long", f"The voiceover runs {duration:.0f} seconds.")
    key = default_storage.save(_key(item, f"voice{result.extension}"), ContentFile(result.audio))
    try:
        with transaction.atomic():
            asset = MediaAsset.objects.create(
                owner=item.owner,
                item=item,
                kind=MediaAsset.Kind.VOICEOVER,
                mime_type=result.mime_type,
                size_bytes=len(result.audio),
                storage_key=key,
                provider=result.provider,
                duration_seconds=duration,
            )
            Voiceover.objects.create(
                item=item,
                asset=asset,
                provider=result.provider,
                voice_id=result.voice_id,
                duration_seconds=duration,
                cost_usd=result.cost_usd,
            )
    except IntegrityError:
        default_storage.delete(key)


# Footage ------------------------------------------------------------------------------------


def _recently_used_clip_ids(item: ContentItem) -> set[str]:
    recent_items = (
        ContentItem.objects.filter(owner=item.owner)
        .exclude(pk=item.pk)
        .order_by("-created_at")
        .values_list("pk", flat=True)[: settings.CLIP_REUSE_WINDOW]
    )
    used = MediaAsset.objects.filter(
        item__in=list(recent_items), kind=MediaAsset.Kind.CLIP
    ).values_list("source_id", flat=True)
    return set(used) | set(
        MediaAsset.objects.filter(item=item, kind=MediaAsset.Kind.CLIP).values_list(
            "source_id", flat=True
        )
    )


def step_footage(item: ContentItem) -> None:
    if not _enter(item, Status.SOURCING):
        return
    stock = get_stock()
    scenes = item.script.scenes
    existing = set(
        MediaAsset.objects.filter(item=item, kind=MediaAsset.Kind.CLIP).values_list(
            "scene_index", flat=True
        )
    )
    used = _recently_used_clip_ids(item)
    for index, scene in enumerate(scenes):
        if index in existing:
            continue
        candidates = [c for c in stock.search(scene["search_query"]) if c.source_id not in used]
        usable = [c for c in candidates if c.licence and c.source_url]
        if not usable:
            raise PipelineError(
                "no_footage", f"No unused, licensed clip was found for scene {index + 1}."
            )
        clip = usable[0]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.mp4"
            stock.download(clip, path)
            with path.open("rb") as handle:
                key = default_storage.save(_key(item, f"clip_{index:03}.mp4"), File(handle))
            size = path.stat().st_size
        try:
            with transaction.atomic():
                MediaAsset.objects.create(
                    owner=item.owner,
                    item=item,
                    kind=MediaAsset.Kind.CLIP,
                    scene_index=index,
                    mime_type=clip.mime_type,
                    size_bytes=size,
                    storage_key=key,
                    provider=clip.provider,
                    source_id=clip.source_id,
                    source_url=clip.source_url,
                    licence=clip.licence,
                    attribution=clip.attribution,
                    duration_seconds=clip.duration_seconds,
                    width=clip.width,
                    height=clip.height,
                )
        except IntegrityError:
            default_storage.delete(key)
        used.add(clip.source_id)


# Render -------------------------------------------------------------------------------------


def _fetch(key: str, destination: Path) -> Path:
    with default_storage.open(key, "rb") as source, destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
    return destination


def step_render(item: ContentItem) -> None:
    if not _enter(item, Status.RENDERING):
        return
    done = item.render_jobs.filter(status=RenderJob.Status.SUCCEEDED).exists()
    if done:
        transition(item, Status.AWAITING_REVIEW)
        return
    script = item.script
    voice = item.voiceover
    clips = list(item.assets.filter(kind=MediaAsset.Kind.CLIP).order_by("scene_index"))
    texts = [scene["text"] for scene in script.scenes]
    if len(clips) != len(texts):
        raise PipelineError("missing_clips", "Some scenes have no footage.")
    durations = scene_durations(texts, voice.duration_seconds)
    music_asset = item.music_asset
    if music_asset is not None and not music_asset.licence:
        raise PipelineError("music_unlicensed", "The selected music has no licence on record.")
    job = RenderJob.objects.create(
        item=item,
        started_at=timezone.now(),
        inputs={
            "voiceover": voice.asset.storage_key,
            "clips": [clip.storage_key for clip in clips],
            "durations": durations,
            "music": music_asset.storage_key if music_asset else None,
        },
    )
    log = renderer.CommandLog()
    try:
        with tempfile.TemporaryDirectory(prefix="render-") as temp:
            workdir = Path(temp)
            suffix = Path(voice.asset.storage_key).suffix
            voice_path = _fetch(voice.asset.storage_key, workdir / f"voice{suffix}")
            clip_paths = [
                _fetch(clip.storage_key, workdir / f"input_{index:03}.mp4")
                for index, clip in enumerate(clips)
            ]
            music_path = (
                _fetch(music_asset.storage_key, workdir / "music.audio") if music_asset else None
            )
            final, thumb, result = renderer.render_video(
                workdir,
                clip_paths,
                durations,
                voice_path,
                build_srt(texts, durations),
                music_path,
                log,
            )
            video_asset = _store_output(item, final, "final.mp4", "video/mp4", result)
            thumb_asset = _store_output(item, thumb, "thumb.jpg", "image/jpeg", None)
    except renderer.RenderError as exc:
        job.status = RenderJob.Status.FAILED
        job.error = str(exc)[:4000]
        job.command_log = log.text()
        job.finished_at = timezone.now()
        job.save()
        raise PipelineError("render_failed", "The video could not be rendered.") from exc
    job.status = RenderJob.Status.SUCCEEDED
    job.progress = 100
    job.command_log = log.text()
    job.output_asset = video_asset
    job.thumbnail_asset = thumb_asset
    job.finished_at = timezone.now()
    job.save()
    transition(item, Status.AWAITING_REVIEW)


def _store_output(item, path: Path, name: str, mime: str, result) -> MediaAsset:
    with path.open("rb") as handle:
        key = default_storage.save(_key(item, name), File(handle))
    kind = MediaAsset.Kind.VIDEO if mime.startswith("video/") else MediaAsset.Kind.THUMBNAIL
    return MediaAsset.objects.create(
        owner=item.owner,
        item=item,
        kind=kind,
        mime_type=mime,
        size_bytes=path.stat().st_size,
        storage_key=key,
        duration_seconds=result.duration if result else None,
        width=result.width if result else None,
        height=result.height if result else None,
    )


__all__ = [
    "PipelineError",
    "ProviderError",
    "step_footage",
    "step_render",
    "step_script",
    "step_voiceover",
]
