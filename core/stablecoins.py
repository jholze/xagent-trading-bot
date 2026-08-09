"""Stablecoin detection for buy guards (not volatility_tier \"stable\").

Blocks USD/EUR-pegged bases (GUSD, USDP, USDC, …). Kill-switch via
risk.block_stablecoin_buys (default true).
"""

from __future__ import annotations

import re
from typing import Any

# Explicit bases that are pegged / cash-like (spot markets often */USDT)
STABLECOIN_BASES: frozenset[str] = frozenset(
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
        "USDP",
        "GUSD",
        "PYUSD",
        "EURC",
        "EURT",
        "EURS",
        "EUROC",
        "USD1",
        "RLUSD",
        "USDG",
        "XUSD",
        "CUSD",
        "SUSD",
        "FRAX",
        "LUSD",
        "USDX",
        "USDCASH",
        "USDJ",
        "USTC",
        "UST",
        "MIM",
        "DOLA",
        "CRVUSD",
        "GHO",
        "USDBC",
        "USDY",
        "OUSD",
        "ALUSD",
        "USDS",
        "USDB",
        "USD0",
        "USYC",
        "USDGLO",
        "AEUR",
        "AGEUR",
    }
)

# Bases that contain USD but are *not* stables
_USD_FALSE_POSITIVES: frozenset[str] = frozenset(
    {
        "SUSHI",
        "SUSH",
        "SOLUSD",  # unlikely as base
    }
)

# e.g. USD1, USDC… short pure-peg patterns
_PEG_RE = re.compile(r"^(USD[A-Z0-9]{0,4}|EUR[A-Z0-9]{0,3}|DAI|FRAX|GUSD|USDP)$")


def symbol_base(symbol: str | None) -> str:
    """Extract base from SYMBOL/USDT, SYMBOL_USDT_1h, or bare base."""
    s = str(symbol or "").strip().upper().replace("-", "/")
    if not s:
        return ""
    if "/" in s:
        return s.split("/", 1)[0].strip()
    # position keys: GUSD_USDT_1h
    if "_USDT" in s:
        return s.split("_USDT", 1)[0].strip()
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def is_stablecoin_base(base: str | None) -> bool:
    b = str(base or "").strip().upper()
    if not b or b in _USD_FALSE_POSITIVES:
        return False
    if b in STABLECOIN_BASES:
        return True
    # Heuristic: short USD*/EUR* peg tickers not in false-positive set
    if _PEG_RE.match(b) and b not in _USD_FALSE_POSITIVES:
        return True
    return False


def is_stablecoin_symbol(symbol: str | None) -> bool:
    return is_stablecoin_base(symbol_base(symbol))


def stablecoin_buys_blocked(config: dict | None = None) -> bool:
    """Master switch — default ON for permanent guard."""
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        risk = (config or {}).get("risk") if isinstance(config, dict) else {}
        if not isinstance(risk, dict):
            return True
        if "block_stablecoin_buys" in risk:
            return bool(risk.get("block_stablecoin_buys"))
    except Exception:
        pass
    return True


def stablecoin_block_reason(symbol: str | None) -> str:
    base = symbol_base(symbol) or "?"
    return f"stablecoin blocked ({base})"
