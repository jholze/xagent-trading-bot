"""Watchlist Quality Engine (WQE) — Epic #124.

Authority: hard floors > risk > det score > memory > RAG > LLM soft adjust.
Modes: off | shadow | soft | enforce.
"""

from services.watchlist_quality.memory_bias import MemoryWqeInput, get_memory_wqe_input
from services.watchlist_quality.engine import (
    apply_soft_to_effective_candidates,
    maybe_run_shadow_after_watchlist_load,
    run_shadow_score,
    score_watchlist,
)
from services.watchlist_quality.scoring import CoinQualityScore, score_coin
from services.watchlist_quality.rag_pack import RagPack, build_rag_pack
from services.watchlist_quality.ai_critic import (
    AiCriticResult,
    fuse_quality,
    parse_critic_payload,
    run_ai_critic,
)
from services.watchlist_quality.soft import apply_soft_watchlist

__all__ = [
    "MemoryWqeInput",
    "get_memory_wqe_input",
    "CoinQualityScore",
    "score_coin",
    "score_watchlist",
    "run_shadow_score",
    "maybe_run_shadow_after_watchlist_load",
    "apply_soft_to_effective_candidates",
    "RagPack",
    "build_rag_pack",
    "AiCriticResult",
    "run_ai_critic",
    "parse_critic_payload",
    "fuse_quality",
    "apply_soft_watchlist",
]
