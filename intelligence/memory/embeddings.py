"""Lightweight embeddings without heavy deps — deterministic hash vectors.

Optional upgrade: sentence-transformers when MEMORY_USE_MINILM=1.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable


_DIM = 64


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$]{2,}", (text or "").lower())


def embed_text(text: str, dim: int = _DIM) -> list[float]:
    """Bag-of-hash embedding in R^dim (unit-ish L2). Stable across processes."""
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:2], "big") % dim
        sign = 1.0 if h[2] % 2 == 0 else -1.0
        vec[idx] += sign
    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    aa = list(a)
    bb = list(b)
    if not aa or not bb or len(aa) != len(bb):
        return 0.0
    return sum(x * y for x, y in zip(aa, bb))


def embed_event(title: str, description: str = "", event_type: str = "") -> list[float]:
    return embed_text(f"{event_type} {title} {description}")


def embed_profile_summary(symbol: str, rationale: str, features: dict | None = None) -> list[float]:
    feat = " ".join(f"{k}:{v}" for k, v in sorted((features or {}).items())[:12])
    return embed_text(f"{symbol} {rationale} {feat}")
