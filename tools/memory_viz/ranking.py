"""Pure cosine top-k over embedding matrices (no I/O)."""

from __future__ import annotations

import math
from typing import Sequence


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    s = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return [float(x) / s for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    aa = list(a)
    bb = list(b)
    if not aa or not bb or len(aa) != len(bb):
        return 0.0
    return float(sum(x * y for x, y in zip(aa, bb)))


def top_k_cosine(
    query_vec: Sequence[float],
    matrix: Sequence[Sequence[float]],
    *,
    k: int = 40,
    normalize_query: bool = True,
) -> list[tuple[int, float]]:
    """Return (index, score) pairs sorted by cosine desc, length min(k, n).

    Empty matrix or k<=0 → []. Dim mismatch rows score 0.0 and still rank.
    """
    if not matrix or k <= 0:
        return []
    q = _l2_normalize(query_vec) if normalize_query else [float(x) for x in query_vec]
    scored: list[tuple[int, float]] = []
    for i, row in enumerate(matrix):
        scored.append((i, cosine(q, row)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[: min(int(k), len(scored))]
