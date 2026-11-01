"""Config denylist for every BUY and add.

Symbol and source lists live under ``risk`` in the effective config
(config.json, then the tenant overlay). This module does not name coins.
A missing key is an empty list so partial test configs keep today's
behavior. A present value that is not a list of non-empty strings is
malformed and fail-closes every BUY.
"""

from __future__ import annotations

from dataclasses import dataclass

CODE_SYMBOL = "symbol_denylist"
CODE_SOURCE = "source_denylist"
CODE_CONFIG = "denylist_config_invalid"

_SYMBOL_KEY = "symbol_denylist"
_SOURCE_KEY = "source_denylist"


@dataclass(frozen=True)
class DenylistVerdict:
    malformed: bool
    codes: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.malformed or bool(self.codes)


def _parse_list(value: object, *, upper: bool) -> frozenset[str] | None:
    if not isinstance(value, list):
        return None
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text:
            return None
        parsed.append(text.upper() if upper else text.strip().lower())
    return frozenset(parsed)


def evaluate_denylist(
    symbol: str,
    sources: list[str] | tuple[str, ...],
    risk: object,
) -> DenylistVerdict:
    """One decision for a BUY. ``sources`` is the evaluate source and the order source."""
    if risk is None:
        return DenylistVerdict(False, ())
    if not isinstance(risk, dict):
        return DenylistVerdict(True, (CODE_CONFIG,))

    if _SYMBOL_KEY not in risk:
        symbols: frozenset[str] = frozenset()
    else:
        parsed_symbols = _parse_list(risk.get(_SYMBOL_KEY), upper=True)
        if parsed_symbols is None:
            return DenylistVerdict(True, (CODE_CONFIG,))
        symbols = parsed_symbols

    if _SOURCE_KEY not in risk:
        blocked_sources: frozenset[str] = frozenset()
    else:
        parsed_sources = _parse_list(risk.get(_SOURCE_KEY), upper=False)
        if parsed_sources is None:
            return DenylistVerdict(True, (CODE_CONFIG,))
        blocked_sources = parsed_sources

    from core.stablecoins import symbol_base

    codes: list[str] = []
    base = symbol_base(symbol)
    if base and base in symbols:
        codes.append(CODE_SYMBOL)
    seen: set[str] = set()
    for raw in sources:
        key = str(raw or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if key in blocked_sources:
            codes.append(CODE_SOURCE)
            break
    return DenylistVerdict(False, tuple(codes))
