"""Macro calendar, session clock, Polymarket context (Epic #53).

Hot path: get_macro_snapshot() only — no FRED/Polymarket/Weaviate per coin.
Hermes: sync_macro_context() writes memory_* events + snapshot.
"""

from intelligence.macro.session_clock import session_status
from intelligence.macro.snapshot import get_macro_snapshot, get_risk_multipliers
from intelligence.macro.sync import sync_macro_context

__all__ = [
    "session_status",
    "get_macro_snapshot",
    "get_risk_multipliers",
    "sync_macro_context",
]
