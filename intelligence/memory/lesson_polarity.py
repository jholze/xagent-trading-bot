"""Direction of a memory lesson or RAG hit for DCA scoring.

A lesson such as "Avoid chasing DCA on this pair" is a warning. Counting it as
evidence *for* a DCA add-on is a sign error (see the DCA sniper checklist).
This module classifies a lesson (or RAG hit) as negative, positive, or
neutral, from its tags first and its text second. Negative wins over
positive when both match, so an ambiguous warning never adds score.

Pure function, no IO.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

NEGATIVE = -1
NEUTRAL = 0
POSITIVE = 1

# Tags written by intelligence/memory/reflector.py and dca_reflector.py.
_NEGATIVE_TAGS = frozenset(
    {
        "skip",
        "outcome_loss",
        "harvest",
        "risk_off",
        "weak_history",
        "soft_block",
        "gross_loss",
        "hard_negative",
        "loss",
        "stop",
    }
)
_POSITIVE_TAGS = frozenset({"outcome_win", "strong_history"})

_NEGATIVE_TEXT = re.compile(
    r"\b(avoid|skip(?:ped|s)?|prefer no|prefer smaller|lost|loss(?:es)?|losing|weak|"
    r"soft_block|stop(?:ped)?[ _-]?loss|hurt|do not|don't|never|block(?:ed)?|drawdown|"
    r"bleed|dump(?:ed)?)\b",
    re.IGNORECASE,
)
_POSITIVE_TEXT = re.compile(
    r"\b(often green|can work|solid|strong history|win(?:s|ner)?|profitable|recovered)\b",
    re.IGNORECASE,
)


def _norm_tags(tags: Iterable[Any] | None) -> set[str]:
    out: set[str] = set()
    for t in tags or ():
        s = str(t or "").strip().lower()
        if s:
            out.add(s)
    return out


def lesson_polarity(text: Any = "", tags: Iterable[Any] | None = None) -> int:
    """Return NEGATIVE (-1), NEUTRAL (0) or POSITIVE (+1).

    Tags decide when present; text is the fallback. Negative beats positive.
    """
    tg = _norm_tags(tags)
    if tg & _NEGATIVE_TAGS:
        return NEGATIVE
    if tg & _POSITIVE_TAGS:
        return POSITIVE
    s = str(text or "")
    if _NEGATIVE_TEXT.search(s):
        return NEGATIVE
    if _POSITIVE_TEXT.search(s):
        return POSITIVE
    return NEUTRAL


def item_polarity(item: Any) -> int:
    """Polarity of a Lesson, MarketEvent, RagHit, or dict with text/tags."""
    if isinstance(item, dict):
        text = item.get("text") or item.get("description") or ""
        tags = item.get("tags")
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    else:
        text = getattr(item, "text", None) or getattr(item, "description", None) or ""
        tags = getattr(item, "tags", None)
        meta = getattr(item, "metadata", None)
        meta = meta if isinstance(meta, dict) else {}
    if not tags and meta:
        tags = meta.get("tags")
    if isinstance(tags, str):
        tags = [tags]  # a single tag stored as a bare string
    return lesson_polarity(text, tags if isinstance(tags, (list, tuple, set)) else None)


def count_polarity(items: Iterable[Any] | None) -> tuple[int, int, int]:
    """Return (negative, neutral, positive) counts."""
    neg = neu = pos = 0
    for it in items or ():
        p = item_polarity(it)
        if p < 0:
            neg += 1
        elif p > 0:
            pos += 1
        else:
            neu += 1
    return neg, neu, pos
