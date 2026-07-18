"""Lightweight embeddings without heavy deps — deterministic hash vectors.

Optional MiniLM via MEMORY_EMBEDDING_BACKEND=minilm or MEMORY_USE_MINILM=1.
Default remains hash (no torch) for bot/hot paths and unit tests.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable


_DIM = 64
_MINILM_DIM = 384
_minilm_model = None
_minilm_failed = False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$]{2,}", (text or "").lower())


def embedding_backend() -> str:
    env = (os.environ.get("MEMORY_EMBEDDING_BACKEND") or "").strip().lower()
    if env in ("hash", "minilm"):
        return env
    if os.environ.get("MEMORY_USE_MINILM", "").strip().lower() in ("1", "true", "yes", "on"):
        return "minilm"
    return "hash"


def embed_text_hash(text: str, dim: int = _DIM) -> list[float]:
    """Bag-of-hash embedding in R^dim (unit-ish L2). Stable across processes."""
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:2], "big") % dim
        sign = 1.0 if h[2] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _embed_minilm(text: str) -> list[float] | None:
    global _minilm_model, _minilm_failed
    if _minilm_failed:
        return None
    try:
        if _minilm_model is None:
            from sentence_transformers import SentenceTransformer

            _minilm_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        vec = _minilm_model.encode(text or "", normalize_embeddings=True)
        return [float(x) for x in list(vec)]
    except Exception:
        _minilm_failed = True
        return None


def embed_text(text: str, dim: int = _DIM) -> list[float]:
    """Embed text: MiniLM if configured and available, else hash fallback."""
    if embedding_backend() == "minilm":
        m = _embed_minilm(text)
        if m is not None:
            return m
    return embed_text_hash(text, dim=dim)


def embed_for_rag(text: str) -> list[float]:
    """Fixed 384-d vector for MemoryRagChunk (Weaviate C5) + Mongo dual-write.

    Prefer MiniLM when available; otherwise hash at dim=384 (stable, no torch).
    """
    if embedding_backend() == "minilm":
        m = _embed_minilm(text)
        if m is not None:
            if len(m) == _MINILM_DIM:
                return m
            # pad/truncate unexpected sizes
            if len(m) < _MINILM_DIM:
                return m + [0.0] * (_MINILM_DIM - len(m))
            return m[:_MINILM_DIM]
    return embed_text_hash(text, dim=_MINILM_DIM)


def rag_embedding_dim() -> int:
    return _MINILM_DIM


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
