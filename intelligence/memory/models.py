"""Memory entity models (Epic #30)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


@dataclass
class CoinProfile:
    symbol: str
    ledger_scope: str = "live"
    tenant_id: str = "default"
    as_of: str = ""
    version: int = 1
    trades_30d: int = 0
    sells_30d: int = 0
    buys_30d: int = 0
    win_rate: float = 0.0
    total_pnl_usdt: float = 0.0
    avg_pnl_usdt: float = 0.0
    dca_count_30d: int = 0
    size_bias: float = 1.0
    entry_bias: str = "neutral"  # neutral | soft_block | prefer
    risk_score: float = 0.5
    rationale: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.as_of:
            self.as_of = utc_now_iso()
        self.size_bias = max(0.5, min(1.2, _f(self.size_bias, 1.0)))
        self.entry_bias = (self.entry_bias or "neutral").lower()
        if self.entry_bias not in ("neutral", "soft_block", "prefer"):
            self.entry_bias = "neutral"

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = f"{self.tenant_id}|{self.ledger_scope}|{self.symbol}"
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> CoinProfile | None:
        if not doc:
            return None
        raw = {k: v for k, v in doc.items() if k != "_id" and k in cls.__dataclass_fields__}
        try:
            return cls(**raw)
        except Exception:
            return None


@dataclass
class MarketEvent:
    event_id: str
    timestamp: str
    event_type: str
    symbols: list[str] = field(default_factory=list)
    impact_score: float = 0.0
    description: str = ""
    source: str = ""
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    tenant_id: str = "default"

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = utc_now_iso()
        self.impact_score = max(-1.0, min(1.0, _f(self.impact_score)))
        self.symbols = [str(s).upper().replace("-", "/") for s in (self.symbols or [])]

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = self.event_id
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> MarketEvent | None:
        if not doc:
            return None
        raw = {k: v for k, v in doc.items() if k != "_id" and k in cls.__dataclass_fields__}
        try:
            return cls(**raw)
        except Exception:
            return None


@dataclass
class TradeMemory:
    trade_id: str
    symbol: str
    entry_time: str = ""
    exit_time: str = ""
    direction: str = ""  # buy | sell
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl_usdt: float | None = None
    pnl_pct: float | None = None
    source: str = ""
    outcome: str = ""  # win | loss | breakeven | open
    reason: str = ""
    related_event_ids: list[str] = field(default_factory=list)
    ledger_scope: str = "live"
    tenant_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = self.trade_id
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> TradeMemory | None:
        if not doc:
            return None
        raw = {k: v for k, v in doc.items() if k != "_id" and k in cls.__dataclass_fields__}
        try:
            return cls(**raw)
        except Exception:
            return None


@dataclass
class Lesson:
    lesson_id: str
    text: str
    confidence: float = 0.5
    tags: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    sample_n: int = 0
    validated: bool = False
    created_at: str = ""
    source: str = "reflector"
    embedding: list[float] = field(default_factory=list)
    tenant_id: str = "default"

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now_iso()
        self.confidence = max(0.0, min(1.0, _f(self.confidence, 0.5)))

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = self.lesson_id
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> Lesson | None:
        if not doc:
            return None
        raw = {k: v for k, v in doc.items() if k != "_id" and k in cls.__dataclass_fields__}
        try:
            return cls(**raw)
        except Exception:
            return None
