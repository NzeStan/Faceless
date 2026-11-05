"""Originality checks: script similarity and hook variety."""

from __future__ import annotations

import re

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACE.sub(" ", _NON_WORD.sub("", text.lower())).strip()


def trigrams(text: str) -> set[str]:
    value = f"  {_normalise(text)}  "
    return {value[index : index + 3] for index in range(len(value) - 2)}


def similarity(left: str, right: str) -> float:
    """Jaccard similarity of character trigrams, from 0.0 to 1.0."""
    first, second = trigrams(left), trigrams(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def too_similar(text: str, others: list[str], threshold: float) -> bool:
    return any(similarity(text, other) >= threshold for other in others)
