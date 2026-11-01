"""Fee and slippage model. Numbers come from ``tools.hypothesis_protocol`` (trial ledger s2)."""

from __future__ import annotations

from dataclasses import dataclass

from tools.hypothesis_protocol import TAKER_FEE_PCT_PER_SIDE, expected_slippage_pct

STABLE_BASES = frozenset({"BTC", "ETH"})


@dataclass(frozen=True)
class CostModel:
    tier: str = "base"  # base | stress | hard_stress
    fee_pct: float = TAKER_FEE_PCT_PER_SIDE

    def slippage_pct(self, symbol: str, bot_tier: str | None, *, small_cap: bool = False) -> float:
        base = symbol.split("/")[0].upper()
        if base in STABLE_BASES:
            asset_class = "stable"
        elif bot_tier == "volatile":
            asset_class = "volatile"
        else:
            asset_class = "other"
        return float(expected_slippage_pct(asset_class, self.tier, small_cap=small_cap))

    def buy_price(self, mid: float, slip_pct: float) -> float:
        return mid * (1.0 + slip_pct / 100.0)

    def sell_price(self, mid: float, slip_pct: float) -> float:
        return mid * (1.0 - slip_pct / 100.0)

    def fee(self, notional: float) -> float:
        return abs(notional) * self.fee_pct / 100.0
