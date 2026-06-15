import json
import os
from datetime import datetime

from core.config import get_bot_config
from logger import log_decision


class AuditTrail:
    """Append-only decision audit log at logs/decisions.jsonl."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()

    @property
    def enabled(self) -> bool:
        return bool(self.config.raw.get("observability", {}).get("decisions_audit", True))

    def record(
        self,
        coin: dict,
        analysis,
        trade_result=None,
        price: float = 0.0,
        risk_message: str = "",
    ):
        if not self.enabled or analysis is None:
            return

        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": analysis.symbol,
            "timeframe": analysis.timeframe,
            "price": price,
            "action": analysis.action,
            "normalized_action": analysis.normalized_action,
            "confidence": analysis.confidence,
            "sources": list(analysis.sources or []),
            "rationale": analysis.rationale,
            "rsi": analysis.rsi,
            "vol_multiplier": analysis.vol_multiplier,
            "atr_pct": getattr(analysis, "atr_pct", 0.0),
            "volatility_tier": getattr(analysis, "volatility_tier", ""),
            "strategy_profile": getattr(analysis, "strategy_profile", ""),
            "shadow_action": getattr(analysis, "shadow_action", ""),
            "trading_mode": self.config.trading_mode,
            "executed": bool(trade_result.executed) if trade_result else False,
            "order_type": trade_result.order_type if trade_result else None,
            "trade_message": trade_result.message if trade_result else "",
            "risk_outcome": "executed" if trade_result and trade_result.executed else (
                "rejected" if trade_result and trade_result.message else "hold"
            ),
            "risk_message": risk_message or (trade_result.message if trade_result else ""),
        }
        log_decision(entry)