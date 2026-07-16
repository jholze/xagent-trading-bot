"""Phase C stub: optional exchange limit-order grid (not wired yet).

When live + grid.use_limit_orders, a future GridLimitExecutor can:
- place buy/sell limits at plan.levels
- cancel/replace on re-center
- map fills back into GridPlan.filled flags

Local dry-run / demo keeps market-slice execution via GridStrategy + TradingService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from strategies.grid_plan import GridPlan


@dataclass
class LimitOrderSpec:
    symbol: str
    side: str
    price: float
    usdt: float = 0.0
    amount: float = 0.0
    client_id: str = ""


class GridLimitExecutor(Protocol):
    def sync_plan(self, plan: GridPlan) -> list[LimitOrderSpec]:
        """Ensure open limits match plan; return desired specs."""
        ...

    def on_fill(self, client_id: str, fill_price: float, fill_amount: float) -> None:
        ...


def limit_orders_enabled(config_raw: dict | None) -> bool:
    grid = (config_raw or {}).get("grid") or {}
    return bool(grid.get("use_limit_orders", False)) and bool(grid.get("enabled", True))
