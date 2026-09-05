from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


@dataclass
class MarketContext:
    symbol: str
    timeframe: str
    current_price: float
    rsi: float = 45.0
    lower_bb: float = 0.0
    middle_bb: float = 0.0
    upper_bb: float = 0.0
    atr_pct: float = 3.0
    vol_multiplier: float = 1.0
    funding_rate_pct: float | None = None
    btc_underperf_ratio: float | None = None
    has_position: bool = False
    average_entry: float = 0.0
    open_positions: int = 0
    strategy_params: dict = field(default_factory=dict)
    sim_state: dict | None = None  # isolated in-memory state for Hermes backtests
    regime: Optional["RegimeResult"] = None
    allocation: Optional["AllocationDecision"] = None
    # PR-P2c: OHLCV frame reused by regime (avoid second fetch with different limit)
    ohlcv_df: Any = None


@dataclass
class SignalAnalysis:
    action: str
    symbol: str
    timeframe: str
    rsi: float
    lower_bb: float
    vol_multiplier: float
    ampel_emoji: str
    ampel_text: str
    should_notify: bool = False
    notify_reason: str = ""
    x_confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    normalized_action: str = "HOLD"
    rationale: str = ""
    confidence: float = 0.0
    recommended: bool = False
    upper_bb: float = 0.0
    middle_bb: float = 0.0
    atr_pct: float = 0.0
    volatility_tier: str = ""
    strategy_profile: str = ""
    shadow_action: str = ""
    dca_usdt: float = 0.0
    sell_policy_audit: dict = field(default_factory=dict)
    # Strategy/module that won the sell merge (trailing_stop, grid, stop_loss, …)
    sell_source: str = ""
    regime: str = ""
    regime_confidence: float = 0.0
    sentiment_score: float = 0.0
    allocation: Optional[dict] = None


class OrderStatus(str, Enum):
    """Ledger order lifecycle. Values reuse legacy strings where they exist
    so the live Mongo ledger needs no data migration:

    * ``ACTIVE = "executing"``
    * ``EXECUTED = "filled"``
    * ``REJECTED = "rejected"``

    New values: ``queued``, ``partially_filled``, ``canceled``.

    ``pending_confirmation`` is a pre-submission Telegram confirm state
    **outside this enum** — do not pass it to ``from_legacy``. Ledger
    ``expired`` (pending TTL) is likewise housekeeping, not an exchange
    state. Legacy ``failed`` maps to ``REJECTED`` on read; ``cancelled``
    (British) maps to ``CANCELED``.
    """

    QUEUED = "queued"
    ACTIVE = "executing"
    PARTIALLY_FILLED = "partially_filled"
    EXECUTED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @classmethod
    def from_legacy(cls, value: object) -> "OrderStatus":
        """Map a stored ledger string (or Enum) to ``OrderStatus``.

        This is the only allowed place that mentions the legacy tokens
        ``filled`` / ``executing`` / ``failed`` as input.
        """
        if isinstance(value, cls):
            return value
        if value is None:
            raise ValueError("OrderStatus.from_legacy(None)")
        token = str(getattr(value, "value", value)).strip().lower()
        mapped = _LEGACY_STATUS.get(token)
        if mapped is not None:
            return mapped
        raise ValueError(f"unknown order status {value!r}")

    @classmethod
    def try_legacy(cls, value: object) -> Optional["OrderStatus"]:
        """``from_legacy`` that returns ``None`` for blank / outside-enum / unknown."""
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        token = str(getattr(value, "value", value)).strip().lower()
        if not token or token in _OUTSIDE_ENUM:
            return None
        try:
            return cls.from_legacy(value)
        except ValueError:
            return None


# from_legacy input table — keep in this helper, not at comparison sites.
_LEGACY_STATUS: dict[str, OrderStatus] = {
    "queued": OrderStatus.QUEUED,
    "executing": OrderStatus.ACTIVE,
    "active": OrderStatus.ACTIVE,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.EXECUTED,
    "executed": OrderStatus.EXECUTED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "failed": OrderStatus.REJECTED,
}
_OUTSIDE_ENUM = frozenset({"pending_confirmation", "expired"})


def stored_status(value: object) -> str:
    """Canonical ledger string for an enum or already-stored token."""
    if isinstance(value, OrderStatus):
        return value.value
    if value is None:
        return ""
    token = str(getattr(value, "value", value)).strip()
    return token


def is_executed_status(value: object) -> bool:
    """True when a stored ledger status is EXECUTED (legacy ``filled``)."""
    return OrderStatus.try_legacy(value) is OrderStatus.EXECUTED


@dataclass
class TradeResult:
    executed: bool
    order_type: str
    symbol: str
    amount: float = 0.0
    price: float = 0.0
    usdt_amount: float = 0.0
    pnl: float = 0.0
    message: str = ""
    order_id: str = ""
    exchange_order_id: str = ""
    fee: float = 0.0
    precision_unverified: bool = False
    pending: bool = False
    needs_reconcile: bool = False
    fee_unknown: bool = False
    order_status: OrderStatus | None = None
    order_exist_in_exchange: bool = False
    filled_qty: float = 0.0


@dataclass
class SocialSignal:
    account: str
    coin: str
    action: str
    confidence: int
    price_target: Optional[float] = None
    stop_loss: Optional[float] = None
    rationale: str = ""
    score: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Signal:
    action: str
    symbol: str
    timeframe: str = "4h"
    confidence: float = 0.0
    rsi: Optional[float] = None
    rationale: str = ""
    sources: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str
    symbol: str
    recommended: bool = False
    rationale: str = ""
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass
class TradeOrder:
    type: str
    symbol: str
    price: float
    qty: float = 0.0
    usdt_amount: float = 0.0
    signal: str = ""
    source: str = "auto"
    # Why we sold (strategy/module). Distinct from channel `source` (auto/grid/dca/manual).
    exit_source: str = ""
    exit_rationale: str = ""
    order_id: str = ""
    idempotency_key: str = ""
    entry_15m_vol_ratio: float | None = None
    leverage: float | None = None
    # Allocator de-risking factor (0–1). Applied in RiskManager._dynamic_size.
    exposure_multiplier: float | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    filled_qty: float = 0.0
    status: OrderStatus = OrderStatus.QUEUED
    reduce_only: bool = False
    client_order_id: str = ""
    exchange_order_id: str = ""
    order_exist_in_exchange: bool = False

    def __post_init__(self) -> None:
        if self.client_order_id and not self.idempotency_key:
            self.idempotency_key = self.client_order_id
        elif self.idempotency_key and not self.client_order_id:
            self.client_order_id = self.idempotency_key

    @property
    def amount(self) -> float:
        """Alias of ``qty`` for one release; new code uses ``qty`` / ``filled_qty``."""
        return self.qty

    @amount.setter
    def amount(self, value: float) -> None:
        self.qty = float(value)

    @property
    def remaining_qty(self) -> float:
        return max(0.0, float(self.qty or 0) - float(self.filled_qty or 0))


_TRADE_ORDER_INIT = TradeOrder.__init__


def _trade_order_init(self, *args, amount=None, **kwargs):
    """Accept legacy ``amount=`` as an alias of ``qty`` at construction."""
    if amount is not None and "qty" not in kwargs and len(args) < 4:
        kwargs["qty"] = amount
    _TRADE_ORDER_INIT(self, *args, **kwargs)


TradeOrder.__init__ = _trade_order_init  # type: ignore[method-assign]


@dataclass
class ApprovedOrder:
    order: TradeOrder
    usdt_amount: float
    size_multiplier: float = 1.0
    atr_factor: float = 1.0
    trust_factor: float = 1.0
    drawdown_pct: float = 0.0


@dataclass
class RiskDecision:
    approved: bool
    order: Optional[TradeOrder] = None
    message: str = ""
    code: str = ""
    size_multiplier: float = 1.0
    drawdown_pct: float = 0.0
    atr_factor: float = 1.0
    trust_factor: float = 1.0


@dataclass
class StrategyHypothesis:
    id: str
    name: str
    source_account: str
    status: str = "testing"
    timeframe: str = "4h"
    symbol: str = ""
    params: dict = field(default_factory=dict)
    rationale: str = ""
    source_tweet: str = ""
    source_post_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metrics: dict = field(default_factory=dict)


@dataclass
class SandboxMetrics:
    win_rate: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: int = 0
    realized_pnl: float = 0.0
    equity: float = 0.0
    trade_quality: float = 0.0
    opportunity_score: float = 0.0
    buy_signals: int = 0


@dataclass
class RegimeResult:
    """Result of the RegimeDetector (technical + sentiment fusion)."""
    primary_regime: str
    confidence: float
    weighted_score: float
    volatility_tier: str
    sentiment_score: float
    components: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)


@dataclass
class AllocationDecision:
    """StrategyAllocator output: weights, exposure, and parameter overrides."""
    strategy_weights: dict = field(default_factory=dict)
    exposure_multiplier: float = 1.0
    grid_params: dict = field(default_factory=dict)
    momentum_params_override: dict = field(default_factory=dict)
    defensive_mode: bool = False
    rationale: str = ""
