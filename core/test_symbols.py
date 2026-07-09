"""Detect pytest / verification phantom symbols that must never hit live ledgers."""

from __future__ import annotations

_PHANTOM_BASES = frozenset({
    "SENSOR15",
    "XENTRY15",
    "XENTRY15M",
})

_PHANTOM_PREFIXES = ("TEST", "XRVM", "PHANTOM")


def phantom_symbol_base(symbol: str) -> str:
    return (symbol or "").split("/")[0].upper()


def is_phantom_test_symbol(symbol: str) -> bool:
    base = phantom_symbol_base(symbol)
    if not base:
        return False
    if base in _PHANTOM_BASES:
        return True
    return any(base.startswith(p) for p in _PHANTOM_PREFIXES)