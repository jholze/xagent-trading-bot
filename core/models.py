from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())