from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class MarketContext:
    symbol: str
    timeframe: str
    current_price: float
    rsi: float = 45.0
    lower_bb: float = 0.0
    vol_multiplier: float = 1.0
    has_position: bool = False
    average_entry: float = 0.0
    open_positions: int = 0
    strategy_params: dict = field(default_factory=dict)


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
    amount: float
    usdt_amount: float = 0.0
    signal: str = ""
    source: str = "auto"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


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