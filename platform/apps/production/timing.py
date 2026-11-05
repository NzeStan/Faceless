"""Scene timings and subtitles derived from the measured voiceover duration."""

from __future__ import annotations


def scene_durations(scene_texts: list[str], total_seconds: float) -> list[float]:
    """Split the voiceover duration across scenes in proportion to word count."""
    counts = [max(1, len(text.split())) for text in scene_texts]
    total_words = sum(counts)
    durations = [round(total_seconds * count / total_words, 3) for count in counts]
    durations[-1] = round(total_seconds - sum(durations[:-1]), 3)
    return durations


def _timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def build_srt(scene_texts: list[str], durations: list[float], words_per_cue: int = 4) -> str:
    """Build SRT text with short cues, each timed by its share of the scene's words."""
    cues: list[str] = []
    start = 0.0
    number = 1
    for text, duration in zip(scene_texts, durations, strict=True):
        words = text.split()
        if not words:
            start += duration
            continue
        per_word = duration / len(words)
        for offset in range(0, len(words), words_per_cue):
            chunk = words[offset : offset + words_per_cue]
            end = start + per_word * len(chunk)
            cues.append(f"{number}\n{_timestamp(start)} --> {_timestamp(end)}\n{' '.join(chunk)}\n")
            number += 1
            start = end
    return "\n".join(cues)
