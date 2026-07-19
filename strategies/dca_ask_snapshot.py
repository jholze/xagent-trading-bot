"""Live DCA policy snapshot for Telegram /ask (#99 D4b).

Read-only: reuses build_dca_context + evaluate_dca_policy.
No orders, no ledger writes. Fail-open empty string.
"""

from __future__ import annotations

from typing import Any


def _policy_cfg_for_symbol(symbol: str, config_raw: dict | None = None) -> dict[str, Any]:
    from strategies.dca import dca_config
    from strategies.dca_policy import dca_policy_config

    params: dict = {}
    try:
        from core.config import get_bot_config

        bot = get_bot_config()
        raw = config_raw if config_raw is not None else bot.raw
        # Prefer pair-level strategy params when available
        try:
            params = bot.strategy_params(symbol, "4h") or {}
        except Exception:
            params = (raw.get("strategies") or {}).get("default") or {}
            if not isinstance(params, dict):
                params = {}
    except Exception:
        params = {}
    return dca_policy_config(dca_config(params))


def format_live_dca_policy_snapshot(
    symbol: str | None,
    *,
    config_raw: dict | None = None,
    position: dict | None = None,
) -> str:
    """Return prompt block or '' if disabled / no symbol / error."""
    sym = str(symbol or "").strip()
    if not sym:
        return ""
    if "/" not in sym:
        sym = f"{sym}/USDT"

    try:
        pcfg = _policy_cfg_for_symbol(sym, config_raw)
        if not pcfg.get("enabled"):
            return ""
        if not pcfg.get("ask_snapshot", True):
            return ""

        from strategies.dca_context import build_dca_context
        from strategies.dca_policy import evaluate_dca_policy

        # include_rag=False: /ask already retrieves RAG separately (perf)
        ctx = build_dca_context(
            symbol=sym,
            position=position,
            config_raw=config_raw,
            include_rag=False,
        )
        result = evaluate_dca_policy(ctx, pcfg)
        shadow = bool(pcfg.get("shadow", True))
        codes = ", ".join(result.reason_codes) if result.reason_codes else "-"
        spd = ctx.spendable_dca
        spd_s = f"{spd:.0f}" if spd is not None else "n/a"
        action = "SKIP (no DCA)" if result.skip else "ALLOW DCA"
        mode = shadow and "shadow" or "live"

        fact_line = ""
        summary = str(getattr(ctx, "fact_summary", "") or "")
        if summary:
            fact_line = f"  facts: {summary[:200]}\n"
        elif int(getattr(ctx, "fact_event_count", 0) or 0) > 0:
            fact_line = f"  facts: n={ctx.fact_event_count}\n"

        return (
            "LIVE_DCA_POLICY (advisory only — no order from this block):\n"
            f"  symbol={sym}\n"
            f"  action={action}\n"
            f"  size_mult={result.size_mult}\n"
            f"  skip={result.skip}\n"
            f"  cash_mode={ctx.cash_mode or '-'}\n"
            f"  fusion_size_mult={ctx.fusion_size_mult:.3f}\n"
            f"  spendable_dca={spd_s}\n"
            f"  entry_bias={ctx.entry_bias} size_bias={ctx.size_bias:.2f}\n"
            f"{fact_line}"
            f"  reasons=[{codes}]\n"
            f"  policy_mode={mode} v{result.policy_version}\n"
            "  Use LIVE_DCA_POLICY together with RETRIEVED_MEMORY; "
            "if they conflict, prefer LIVE_DCA_POLICY for 'should we DCA now' "
            "and memory for past outcomes. Do not invent trades.\n"
        )
    except Exception:
        return ""
