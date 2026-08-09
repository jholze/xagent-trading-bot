"""Exit radar domain — pure eval + position loader + sniper status.

Canonical home for board/runtime exit proximity. GUI probe script and Flask
routes import from here (not the other way around).
"""

from __future__ import annotations

from services.exit_radar.eval import evaluate_position, hours_since, resolve_trail_pct
from services.exit_radar.positions import load_open_positions
from services.exit_radar.sniper_status import fetch_dca_sniper_status

__all__ = [
    "evaluate_position",
    "fetch_dca_sniper_status",
    "hours_since",
    "load_open_positions",
    "resolve_trail_pct",
]
