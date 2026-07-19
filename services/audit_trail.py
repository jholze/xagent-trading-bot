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

    @staticmethod
    def _needs_position_metrics(analysis, trade_result) -> bool:
        if trade_result and trade_result.executed:
            return True
        norm = str(getattr(analysis, "normalized_action", "") or "").upper()
        if norm.startswith(("BUY", "SELL")) and norm != "HOLD":
            return True
        audit = getattr(analysis, "sell_policy_audit", None) or {}
        if audit.get("would_sell"):
            return True
        return False

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

        from core.tenant_context import resolve_tenant_id, resolve_tenant_scope
        from services.observability_store import persist_decision, runtime_context
        from services.position_metrics import position_metrics
        from strategies.positions import get_position

        entry = {
            "timestamp": datetime.now().isoformat(),
            "tenant_id": resolve_tenant_id(),
            "ledger_scope": resolve_tenant_scope(),
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
        if has_position and price > 0 and self._needs_position_metrics(analysis, trade_result):
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
        # P5: shadow memory hits on decision audit (never changes action)
        try:
            self._attach_memory_shadow(entry, analysis)
        except Exception:
            pass
        log_decision(entry)
        persist_decision(entry)

    def _attach_memory_shadow(self, entry: dict, analysis) -> None:
        """Top-k RAG snippets for observability. Fail-open; shadow only."""
        try:
            from intelligence.memory.rag_config import rag_config, rag_enabled

            cfg = rag_config(self.config.raw if hasattr(self.config, "raw") else None)
            if not rag_enabled(self.config.raw if hasattr(self.config, "raw") else None):
                return
            if not cfg.get("enrich_decision_audit", True):
                return
        except Exception:
            return

        symbol = str(getattr(analysis, "symbol", "") or entry.get("symbol") or "")
        action = str(getattr(analysis, "normalized_action", "") or entry.get("action") or "")
        query = (
            f"{symbol} {action} "
            f"{getattr(analysis, 'rationale', '') or ''} "
            f"trade memory lesson risk"
        ).strip()
        top_k = min(5, int(cfg.get("top_k") or 5))
        try:
            from hermes.memory.rag_retriever import RagRetriever

            hits = RagRetriever(
                config=self.config.raw if hasattr(self.config, "raw") else None
            ).retrieve(
                query,
                top_k=top_k,
                filters={"symbol": symbol} if symbol else None,
            )
            if not hits and symbol:
                hits = RagRetriever(
                    config=self.config.raw if hasattr(self.config, "raw") else None
                ).retrieve(query, top_k=top_k, filters=None)
        except Exception:
            hits = []

        shadow = []
        for h in hits or []:
            md = h.metadata if isinstance(getattr(h, "metadata", None), dict) else {}
            shadow.append(
                {
                    "score": round(float(getattr(h, "score", 0) or 0), 4),
                    "type": md.get("type") or "",
                    "symbol": md.get("symbol") or "",
                    "text": str(getattr(h, "text", "") or "")[:180],
                    "chunk_id": str(getattr(h, "chunk_id", "") or "")[:40],
                }
            )
        entry["memory_shadow"] = {
            "enabled": True,
            "query": query[:200],
            "hit_count": len(shadow),
            "hits": shadow,
        }
        # Profile snapshot (soft_block / size_bias) — no action change
        try:
            from intelligence.memory.cache import get_coin_profile

            prof = get_coin_profile(symbol) if symbol else None
            if prof:
                entry["memory_shadow"]["profile"] = {
                    "entry_bias": prof.entry_bias,
                    "size_bias": float(prof.size_bias or 1.0),
                    "risk_score": float(getattr(prof, "risk_score", 0.5) or 0.5),
                }
        except Exception:
            pass