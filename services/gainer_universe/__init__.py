"""Gate top-mover scanner + trade-universe expand (feature-flagged).

Rollback: ``gainer_universe.enabled: false`` or ``mode: "off"``.
Does not place orders — only universe + logs. Existing signals unchanged.
"""

from services.gainer_universe.config import (
    gainer_trade_expand_enabled,
    gainer_universe_config,
    gainer_universe_enabled,
)
from services.gainer_universe.inject import (
    expand_candidates_for_trade,
    merge_expand_into_trade,
    merge_gainers_into_observe,
)
from services.gainer_universe.runtime import maybe_refresh_gainer_universe
from services.gainer_universe.store import load_gainer_state

__all__ = [
    "gainer_universe_config",
    "gainer_universe_enabled",
    "gainer_trade_expand_enabled",
    "expand_candidates_for_trade",
    "merge_expand_into_trade",
    "merge_gainers_into_observe",
    "maybe_refresh_gainer_universe",
    "load_gainer_state",
]
