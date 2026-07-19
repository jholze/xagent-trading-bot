"""Build DcaContext for policy — fail-open I/O, no order/ledger writes."""

from __future__ import annotations

from typing import Any

from strategies.dca_policy import DcaContext


def build_dca_context(
    *,
    symbol: str,
    position: dict | None = None,
    market: Any = None,
    strategy_params: dict | None = None,
    score: int = 0,
    max_score: int = 10,
    loss_pct: float = 0.0,
    config_raw: dict | None = None,
) -> DcaContext:
    """Assemble context; any failure → safe defaults (fail-open)."""
    _ = market, strategy_params  # reserved for future tech flags
    sym = str(symbol or "").strip()
    if not sym and position:
        sym = str((position or {}).get("symbol") or "")
    if sym and "/" not in sym:
        sym = f"{sym}/USDT"

    ctx = DcaContext(
        symbol=sym,
        score=int(score or 0),
        max_score=int(max_score or 10),
        loss_pct=float(loss_pct or 0.0),
    )

    raw = config_raw
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}

    # Fusion / global bias
    fusion_missing = True
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(raw) or {}
        fusion_missing = False
        try:
            ctx.fusion_size_mult = float(bias.get("size_mult", 1.0) or 1.0)
        except (TypeError, ValueError):
            ctx.fusion_size_mult = 1.0
            fusion_missing = True
        ctx.block_buys = bool(bias.get("block_buys"))
    except Exception:
        ctx.fusion_size_mult = 1.0
        ctx.block_buys = False
        fusion_missing = True
    ctx.fusion_missing = fusion_missing

    # Cash mode from fusion; spendable via Risk when possible
    try:
        from risk.cash_policy import is_cash_policy_enabled, resolve_cash_mode

        ctx.cash_mode = resolve_cash_mode(
            size_mult=ctx.fusion_size_mult,
            block_buys=ctx.block_buys,
        )
        try:
            from core.config import get_bot_config
            from risk.risk_manager import RiskManager

            bot = get_bot_config()
            if is_cash_policy_enabled(bot.risk_config or {}):
                pol = RiskManager(bot)._evaluate_cash_policy()
                if pol is not None and pol.enabled:
                    ctx.cash_mode = pol.mode
                    ctx.spendable_dca = float(pol.spendable_dca)
                    ctx.drawdown_active = bool(pol.drawdown_active)
        except Exception:
            pass
    except Exception:
        ctx.cash_mode = ""

    # Coin profile
    try:
        from intelligence.memory.cache import get_coin_profile

        prof = get_coin_profile(sym)
        if prof is not None:
            try:
                ctx.size_bias = float(getattr(prof, "size_bias", 1.0) or 1.0)
            except (TypeError, ValueError):
                ctx.size_bias = 1.0
            ctx.entry_bias = str(getattr(prof, "entry_bias", "neutral") or "neutral")
    except Exception:
        pass

    # Macro calendar / session
    try:
        from intelligence.macro.snapshot import get_risk_multipliers

        mults = get_risk_multipliers(raw if isinstance(raw, dict) else None) or {}
        try:
            cal = float(mults.get("calendar_mult", 1.0) or 1.0)
            if cal < 0.75:
                ctx.calendar_high_impact = True
        except (TypeError, ValueError):
            pass
        try:
            sess = float(mults.get("session_mult", 1.0) or 1.0)
            if sess < 0.85:
                ctx.session_low_liquidity = True
        except (TypeError, ValueError):
            pass
    except Exception:
        pass

    # Optional RAG hit count (advisory only — never decides)
    try:
        from hermes.memory.rag_retriever import RagRetriever
        from intelligence.memory.rag_config import rag_enabled

        cfg = raw if isinstance(raw, dict) else None
        if rag_enabled(cfg):
            hits = RagRetriever(config=cfg).retrieve(
                f"{sym} trade dca loss",
                top_k=3,
                filters={"symbol": sym} if sym else None,
            )
            ctx.rag_hit_count = len(hits or [])
    except Exception:
        ctx.rag_hit_count = 0

    return ctx
