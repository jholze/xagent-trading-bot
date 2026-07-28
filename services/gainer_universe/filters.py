"""Pure filters for Gate spot USDT movers (testable, no I/O)."""

from __future__ import annotations

_STABLES = frozenset(
    {
        "USDT",
        "USDC",
        "USD",
        "DAI",
        "BUSD",
        "FDUSD",
        "TUSD",
        "USDD",
        "USDE",
        "EUR",
        "EURT",
        "PYUSD",
    }
)


def normalize_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if not s:
        return ""
    if ":" in s:
        s = s.split(":", 1)[0]
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def base_of(sym: str) -> str:
    s = normalize_symbol(sym)
    if "/" in s:
        return s.split("/", 1)[0]
    return s


def is_leverage_token(base: str, suffixes: list[str] | None = None) -> bool:
    b = (base or "").upper()
    sfx = suffixes or ["3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR"]
    return any(b.endswith(x.upper()) for x in sfx)


def passes_spot_usdt_filter(
    symbol: str,
    *,
    blacklist_suffixes: list[str] | None = None,
    blacklist_bases: list[str] | None = None,
    blacklist_name_keywords: list[str] | None = None,
    name: str | None = None,
) -> bool:
    sym = normalize_symbol(symbol)
    if not sym.endswith("/USDT"):
        return False
    base = base_of(sym)
    if not base or base in _STABLES:
        return False
    if is_leverage_token(base, blacklist_suffixes):
        return False
    blocked = {str(x).upper() for x in (blacklist_bases or [])}
    if base in blocked:
        return False
    # stock tokens allowed unless keywords configured
    kws = [str(k).lower() for k in (blacklist_name_keywords or []) if k]
    if kws and name:
        n = str(name).lower()
        if any(k in n for k in kws):
            return False
    return True
