"""Deterministic 3D layout from lobe centers + embedding (no UMAP required)."""

from __future__ import annotations

import hashlib
import math
from typing import Sequence

# Lobe cluster centers in world space
LOBE_CENTERS: dict[str, tuple[float, float, float]] = {
    "coin_facts": (1.2, 0.3, 0.1),
    "trades": (-0.95, 0.85, 0.2),
    "lessons": (-0.35, 1.1, -0.5),
    "events": (0.05, -1.15, 0.85),
    "social": (0.65, 0.9, 1.05),
    "other": (-1.15, -0.9, -0.9),
}


def _unit3(x: float, y: float, z: float) -> tuple[float, float, float]:
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    return x / n, y / n, z / n


def position_for(
    *,
    node_id: str,
    lobe: str,
    embedding: Sequence[float] | None = None,
    radius: float = 0.55,
) -> list[float]:
    """Place a node near its lobe center with embedding-driven offset."""
    cx, cy, cz = LOBE_CENTERS.get(lobe) or LOBE_CENTERS["other"]
    emb = list(embedding or [])
    if len(emb) >= 3:
        # use first 3 components (already ~unit from hash embed)
        ox, oy, oz = emb[0], emb[1], emb[2]
        # mix with id hash for stability when emb dims correlated
        h = hashlib.sha256(str(node_id).encode("utf-8")).digest()
        jx = (h[0] / 255.0 - 0.5) * 0.25
        jy = (h[1] / 255.0 - 0.5) * 0.25
        jz = (h[2] / 255.0 - 0.5) * 0.25
        ox, oy, oz = ox + jx, oy + jy, oz + jz
    else:
        h = hashlib.sha256(str(node_id).encode("utf-8")).digest()
        ox = h[0] / 255.0 * 2 - 1
        oy = h[1] / 255.0 * 2 - 1
        oz = h[2] / 255.0 * 2 - 1
    ux, uy, uz = _unit3(ox, oy, oz)
    return [
        cx + ux * radius,
        cy + uy * radius,
        cz + uz * radius,
    ]
