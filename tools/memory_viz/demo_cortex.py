"""Synthetic cortex for demo / Railway without Mongo.

Embeddings use the same hash path as RAG (embed_for_rag) so queries like
"ARIA volume" rank real demo nodes via cosine — not hardcoded hit lists.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from tools.memory_viz.lobes import classify_lobe, lobe_color


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _embed(text: str) -> list[float]:
    from tools.memory_viz.hash_embed import resolve_embed_for_rag

    return list(resolve_embed_for_rag()(text or ""))


# (lobe hint via source/type, symbol, title, body, cluster center offset)
_DEMO_SEEDS: list[dict[str, Any]] = [
    {
        "source": "cmc_pro_quotes",
        "type": "volume_breakout",
        "symbol": "ARIA/USDT",
        "title": "ARIA volume breakout +29% 24h",
        "text": (
            "CMC Pro: ARIA +29.5% 24h with volume change +287% "
            "(vol $4.7M). volume_breakout signal on AriaAI."
        ),
        "cx": 1.2,
        "cy": 0.3,
        "cz": 0.1,
    },
    {
        "source": "cmc_pro_quotes",
        "type": "relative_strength",
        "symbol": "ARIA/USDT",
        "title": "ARIA outperforms BTC",
        "text": "CMC Pro: ARIA outperforms BTC by 12pp (coin +18% vs BTC +6%).",
        "cx": 1.1,
        "cy": 0.4,
        "cz": 0.2,
    },
    {
        "source": "cmc_ai_updates",
        "type": "ai_narrative",
        "symbol": "ARIA/USDT",
        "title": "AriaAI gaming narrative",
        "text": "AriaAI is a game development experiment with AI NPCs and $ARIA token utility.",
        "cx": 1.0,
        "cy": 0.2,
        "cz": -0.1,
    },
    {
        "source": "cmc_pro_quotes",
        "type": "volume_breakout",
        "symbol": "ZBT/USDT",
        "title": "ZBT volume spike",
        "text": "CMC Pro: ZBT +15% 24h with strong volume breakout confirmation.",
        "cx": 0.8,
        "cy": -0.5,
        "cz": 0.6,
    },
    {
        "source": "cmc_pro_quotes",
        "type": "structure_risk",
        "symbol": "TRX/USDT",
        "title": "TRX underperforms",
        "text": "CMC Pro: TRX underperforms BTC; structure_risk mild drawdown.",
        "cx": 0.5,
        "cy": -0.8,
        "cz": -0.4,
    },
    {
        "source": "trade_history",
        "type": "trade",
        "symbol": "ARIA/USDT",
        "title": "Trade ARIA sell pnl",
        "text": "Trade ARIA/USDT sell filled size 1200 pnl=-42.5 after volume spike entry.",
        "cx": -0.9,
        "cy": 0.8,
        "cz": 0.2,
    },
    {
        "source": "trade_history",
        "type": "trade",
        "symbol": "ZBT/USDT",
        "title": "Trade ZBT buy",
        "text": "Trade ZBT/USDT buy paper fill at 0.041 size 5000.",
        "cx": -1.0,
        "cy": 0.7,
        "cz": 0.3,
    },
    {
        "source": "dca_lesson",
        "type": "lesson",
        "symbol": "ARIA/USDT",
        "title": "DCA lesson ARIA size down",
        "text": "Lesson: after ARIA volume breakout without structure, reduce DCA size next cycle.",
        "cx": -0.4,
        "cy": 1.1,
        "cz": -0.5,
    },
    {
        "source": "reflector",
        "type": "dca_lesson",
        "symbol": "TRX/USDT",
        "title": "DCA lesson wait for confirmation",
        "text": "Lesson: TRX flow_only move — wait for volume confirmation before DCA add.",
        "cx": -0.3,
        "cy": 1.0,
        "cz": -0.6,
    },
    {
        "source": "fusion",
        "type": "market_context",
        "symbol": "",
        "title": "Fusion risk-on tilt",
        "text": "Market context fusion: risk-on breadth improved; funding neutral; regime event.",
        "cx": 0.1,
        "cy": -1.2,
        "cz": 0.8,
    },
    {
        "source": "macro",
        "type": "regime",
        "symbol": "",
        "title": "Regime shift alert",
        "text": "Regime change event: altcoin season index rising; event catalog update.",
        "cx": 0.0,
        "cy": -1.1,
        "cz": 0.9,
    },
    {
        "source": "social",
        "type": "community",
        "symbol": "ARIA/USDT",
        "title": "Social spike ARIA",
        "text": "Community social posts mention AriaAI listing and gaming airdrop buzz.",
        "cx": 0.6,
        "cy": 0.9,
        "cz": 1.0,
    },
    {
        "source": "community",
        "type": "social",
        "symbol": "ZBT/USDT",
        "title": "Social chatter ZBT",
        "text": "Twitter community noise on ZBT; treat as social not structure bias.",
        "cx": 0.7,
        "cy": 0.85,
        "cz": 1.1,
    },
    {
        "source": "notes",
        "type": "misc",
        "symbol": "BTC/USDT",
        "title": "Unclassified note",
        "text": "Generic operator note without trading taxonomy match.",
        "cx": -1.2,
        "cy": -0.9,
        "cz": -0.9,
    },
]


def _jitter(i: int, j: int, scale: float = 0.18) -> tuple[float, float, float]:
    """Deterministic offset so each seed expands into a small cluster."""
    a = math.sin(i * 12.9898 + j * 78.233) * 43758.5453
    b = math.sin(i * 93.989 + j * 12.13) * 43758.5453
    c = math.sin(i * 41.2 + j * 55.7) * 43758.5453
    return (
        (a - math.floor(a) - 0.5) * scale,
        (b - math.floor(b) - 0.5) * scale,
        (c - math.floor(c) - 0.5) * scale,
    )


def build_demo_cortex(
    *,
    variants_per_seed: int = 8,
    knn: int = 6,
) -> tuple[dict[str, Any], list[list[float]]]:
    """Return (cortex_json, vectors) ready for server + query.

    Cortex has no full embeddings (keep JSON light); vectors aligned by index.
    """
    nodes: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    texts: list[str] = []
    built = _utc_now()
    n_var = max(1, int(variants_per_seed))

    idx = 0
    for si, seed in enumerate(_DEMO_SEEDS):
        for j in range(n_var):
            suffix = "" if j == 0 else f" · v{j}"
            text = seed["text"] if j == 0 else f"{seed['text']} context variant {j}"
            title = f"{seed['title']}{suffix}"
            meta = {
                "source": seed["source"],
                "type": seed["type"],
                "symbol": seed["symbol"],
                "kind": "coin_fact" if str(seed["source"]).startswith("cmc_") else seed["type"],
            }
            lobe = classify_lobe(meta)
            jx, jy, jz = _jitter(si, j)
            pos = [
                float(seed["cx"]) + jx,
                float(seed["cy"]) + jy,
                float(seed["cz"]) + jz,
            ]
            # Bias embedding text with title for better query alignment
            emb_src = f"{title} {text}"
            vec = _embed(emb_src)
            node_id = f"demo_{idx:04d}"
            nodes.append(
                {
                    "i": idx,
                    "id": node_id,
                    "pos": pos,
                    "col": lobe_color(lobe),
                    "lobe": lobe,
                    "symbol": seed["symbol"] or "",
                    "source": seed["source"],
                    "type": seed["type"],
                    "title": title,
                    "preview": text[:160],
                    "created_at": built,
                    "nbs": [],
                }
            )
            vectors.append(vec)
            texts.append(text)
            idx += 1

    # KNN on positions (cheap visual synapses)
    k = max(1, min(int(knn), max(1, len(nodes) - 1)))
    for i, n in enumerate(nodes):
        px, py, pz = n["pos"]
        dists: list[tuple[float, int]] = []
        for j, m in enumerate(nodes):
            if i == j:
                continue
            qx, qy, qz = m["pos"]
            d = (px - qx) ** 2 + (py - qy) ** 2 + (pz - qz) ** 2
            dists.append((d, j))
        dists.sort(key=lambda t: t[0])
        n["nbs"] = [j for _, j in dists[:k]]

    dim = len(vectors[0]) if vectors else 0
    cortex = {
        "version": 1,
        "built_at": built,
        "embedding_backend": "hash",
        "embedding_dim": dim,
        "demo": True,
        "node_count": len(nodes),
        "nodes": nodes,
        "texts": texts,
    }
    return cortex, vectors
