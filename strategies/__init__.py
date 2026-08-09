"""Strategies package — avoid eager DecisionEngine import (pandas).

Submodules (``positions``, ``registry``, …) load on first attribute access.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "DecisionEngine",
    "get_strategy",
    "list_registered_strategies",
    "resolve_coin_config",
]


def __getattr__(name: str) -> Any:
    if name == "DecisionEngine":
        from strategies.decision_engine import DecisionEngine

        return DecisionEngine
    if name in ("get_strategy", "list_registered_strategies", "resolve_coin_config"):
        from strategies import registry

        return getattr(registry, name)
    # Lazy submodule load so ``strategies.positions`` / patch paths keep working
    try:
        return importlib.import_module(f"strategies.{name}")
    except ModuleNotFoundError as e:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from e
