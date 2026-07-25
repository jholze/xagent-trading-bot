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
from services.watchlist_quality.enforce import apply_enforce_tiers, buy_allowed
from services.watchlist_quality.universe import (
    cmc_only_buy_allowed,
    rank_cmc_candidates_by_wqe,
    sensor_universe,
)
from services.watchlist_quality.soak import compute_ai_agreement_metrics, format_soak_report
from services.watchlist_quality.runtime import apply_wqe_to_watchlist
from services.watchlist_quality.metrics import snapshot as wqe_metrics_snapshot
from services.watchlist_quality.universe import get_sensor_watch_coins
from services.watchlist_quality.policy import filter_for_grid
from services.watchlist_quality.venue_batch import attach_quote_volumes

__all__ = [
    "MemoryWqeInput",
    "get_memory_wqe_input",
    "CoinQualityScore",
    "score_coin",
    "score_watchlist",
    "run_shadow_score",
    "maybe_run_shadow_after_watchlist_load",
    "apply_soft_to_effective_candidates",
    "apply_wqe_to_watchlist",
    "RagPack",
    "build_rag_pack",
    "AiCriticResult",
    "run_ai_critic",
    "parse_critic_payload",
    "fuse_quality",
    "apply_soft_watchlist",
    "apply_enforce_tiers",
    "buy_allowed",
    "sensor_universe",
    "cmc_only_buy_allowed",
    "rank_cmc_candidates_by_wqe",
    "compute_ai_agreement_metrics",
    "format_soak_report",
    "wqe_metrics_snapshot",
    "get_sensor_watch_coins",
    "filter_for_grid",
    "attach_quote_volumes",
]
