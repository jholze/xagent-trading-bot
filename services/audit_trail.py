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

        from services.observability_store import persist_decision, runtime_context
        from services.position_metrics import position_metrics
        from strategies.positions import get_position

        entry = {
            "timestamp": datetime.now().isoformat(),
            **runtime_context(self.config.raw),
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
        pos = get_position(analysis.symbol, analysis.timeframe)
        has_position = float(pos.get("amount") or 0) > 0
        entry["has_position"] = has_position
        if has_position and price > 0:
            from core.models import MarketContext

            market = MarketContext(
                symbol=analysis.symbol,
                timeframe=analysis.timeframe,
                current_price=price,
                has_position=True,
                average_entry=float(pos.get("average_entry") or 0),
                atr_pct=getattr(analysis, "atr_pct", 0.0),
                strategy_params={"strategy_profile": getattr(analysis, "strategy_profile", "")},
            )
            params = None
            try:
                from strategies.registry import resolve_strategy_params

                params = resolve_strategy_params(
                    {"symbol": analysis.symbol, "timeframe": analysis.timeframe},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
                market.strategy_params = params
            except Exception:
                params = {}
            entry.update(position_metrics(market, pos, params))

        audit = getattr(analysis, "sell_policy_audit", None) or {}
        if audit:
            entry.update({
                "rotation_blocked": audit.get("rotation_blocked"),
                "tail_exempt": audit.get("tail_exempt"),
                "ladder_terminal_would_close": audit.get("ladder_terminal_would_close"),
                "tail_idle_would_close": audit.get("tail_idle_would_close"),
                "trail_exclusive_blocked": audit.get("trail_exclusive_blocked"),
                "would_sell": audit.get("would_sell"),
                "would_source": audit.get("would_source"),
            })
        log_decision(entry)
        persist_decision(entry)