"""Standalone 384-d hash embeddings for demo/Railway (no intelligence package required).

Mirrors intelligence.memory.embeddings.embed_text_hash at dim=384 so demo
query ranking stays stable when the full monorepo is not on PYTHONPATH.
"""

from __future__ import annotations

import hashlib
import math
import re


_DIM = 384


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$]{2,}", (text or "").lower())


def embed_for_rag(text: str, dim: int = _DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:2], "big") % dim
        sign = 1.0 if h[2] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def resolve_embed_for_rag():
    """Prefer monorepo embed_for_rag when available (MiniLM/hash parity with Hermes)."""
    try:
        from intelligence.memory.embeddings import embed_for_rag as _fn

        return _fn
    except Exception:
        return embed_for_rag
