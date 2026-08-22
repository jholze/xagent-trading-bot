"""Desk snapshot assembler. v0: injectable, fail-open live wiring, no Mongo in tests."""

from __future__ import annotations

from typing import Any

from services.desk.hud import build_hud, next_edge as compose_next_edge

_DEFAULT_TENANTS = ("default", "henry")
_DEFAULT_RELVOL_MAX = 8
_DEFAULT_DCA_MAX_ROUNDS = 2
_DEFAULT_SIZE_MULT_DEPLOY = 0.80
_BADGE_EMPTY = "—"
_CONFLICT_SOCIAL_MEMORY = "SOCIAL ARMED · MEMORY BLOCK"
# Live FactFlags names only (intelligence.memory.coin_facts.FactFlags) — never invented.
_FLAG_PRIORITY = (
    "structure_risk",
    "hard_negative",
    "unlock",
    "flow_only",
    "profit_taking",
)


def _fail(error: str) -> dict:
    return {"ok": False, "error": error}


def _desk_section(config_raw: dict | None) -> dict | None:
    if not isinstance(config_raw, dict):
        return None
    desk = config_raw.get("desk")
    return desk if isinstance(desk, dict) else None


def _allowed_tenants(desk: dict) -> list[str]:
    raw = desk.get("tenants")
    if raw is None or not isinstance(raw, (list, tuple, set)):
        return list(_DEFAULT_TENANTS)
    return [str(t) for t in raw]


def _load_config(tenant_id: str) -> dict:
    try:
        from core.config import get_bot_config

        raw = get_bot_config(tenant_id=tenant_id).raw
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _load_lots(tenant_id: str) -> list[dict]:
    try:
        from core.tenant_context import tenant_context
        from strategies.positions import list_active_positions

        with tenant_context(tenant_id):
            lots = list_active_positions(tenant_id=tenant_id)
        if not isinstance(lots, list):
            return []
        return [lot for lot in lots if isinstance(lot, dict)]
    except Exception:
        return []


def _load_fusion(config_raw: dict | None) -> dict:
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(config_raw)
        return dict(bias) if isinstance(bias, dict) else {}
    except Exception:
        return {}


def _load_cash_mode(fusion: dict | None, config_raw: dict | None) -> str | None:
    try:
        from risk.cash_policy import cash_policy_section, resolve_cash_mode

        size_mult = 1.0
        block_buys = False
        if isinstance(fusion, dict):
            if fusion.get("size_mult") is not None:
                size_mult = float(fusion["size_mult"])
            block_buys = bool(
                fusion.get("block_buys") or fusion.get("block_new_entries")
            )
        risk = (config_raw or {}).get("risk") if isinstance(config_raw, dict) else {}
        cp = cash_policy_section(risk if isinstance(risk, dict) else {})
        deploy = _DEFAULT_SIZE_MULT_DEPLOY
        if isinstance(cp, dict) and cp.get("size_mult_deploy") is not None:
            deploy = float(cp["size_mult_deploy"])
        return resolve_cash_mode(
            size_mult=size_mult,
            block_buys=block_buys,
            size_mult_deploy=deploy,
        )
    except Exception:
        return None


def _load_relvol_open(lots: list[dict] | None) -> int | None:
    try:
        from services.gainer_signal.pure import count_open_gainer_positions

        return int(count_open_gainer_positions(lots, source_exact="gainer_relvol"))
    except Exception:
        return None


def _load_relvol_max(config_raw: dict | None) -> int:
    try:
        cfg = config_raw if isinstance(config_raw, dict) else {}
        block = None
        gu = cfg.get("gainer_universe")
        if isinstance(gu, dict):
            nested = gu.get("gainer_relvol_shadow")
            if isinstance(nested, dict):
                block = nested
        if not isinstance(block, dict):
            top = cfg.get("gainer_relvol_shadow")
            block = top if isinstance(top, dict) else {}
        if block.get("max_open") is not None:
            return int(block["max_open"])
    except Exception:
        pass
    return _DEFAULT_RELVOL_MAX


def _coin_base(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    return s.split("/")[0].strip()


def _sig_get(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _worst_memory_flag(flags: Any) -> str | None:
    if flags is None:
        return None
    for name in _FLAG_PRIORITY:
        try:
            on = flags.get(name) if isinstance(flags, dict) else getattr(flags, name, False)
        except Exception:
            on = False
        if on:
            return name
    return None


def _load_memory_facts(symbol: str, tenant_id: str, config_raw: dict | None) -> dict:
    out: dict = {}
    try:
        from intelligence.memory.cache import get_coin_profile

        prof = get_coin_profile(symbol, tenant_id=tenant_id, config=config_raw)
        if prof is not None:
            bias = getattr(prof, "entry_bias", None)
            if bias:
                out["memory_bias"] = str(bias)
            lesson = getattr(prof, "rationale", None)
            if lesson:
                out["memory_lesson"] = str(lesson)
    except Exception:
        pass
    try:
        from intelligence.memory.coin_facts import summarize_facts_for_symbol

        flags = summarize_facts_for_symbol(symbol, config_raw=config_raw)
        flag = _worst_memory_flag(flags)
        if flag:
            out["memory_flag"] = flag
    except Exception:
        pass
    return out


def _load_social_facts(symbol: str) -> dict:
    """Latest CMC/LC from in-memory eval snapshot if present; else empty → HUD IDLE."""
    try:
        from services.eval_queue_runtime import _latest_signals
    except Exception:
        return {}
    try:
        base = _coin_base(symbol)
        out: dict = {}
        for sig in _latest_signals.get("cmc") or []:
            coin = _sig_get(sig, "coin", "symbol")
            if _coin_base(str(coin or "")) != base:
                continue
            conf = _sig_get(sig, "confidence")
            if conf is not None:
                out["cmc_confidence"] = float(conf)
            trust = _sig_get(sig, "trust_score", "cmc_trust")
            if trust is not None:
                out["cmc_trust"] = float(trust)
            qf = _sig_get(sig, "quotes_fallback")
            if qf is not None:
                out["cmc_quotes_fallback"] = bool(qf)
            break
        for sig in _latest_signals.get("lc") or []:
            coin = _sig_get(sig, "coin", "symbol")
            if _coin_base(str(coin or "")) != base:
                continue
            action = _sig_get(sig, "action")
            out["lc_action"] = action
            break
        return out
    except Exception:
        return {}


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _selected_lot(lots: list[dict] | None, symbol: str) -> dict | None:
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        if str(lot.get("symbol") or "") == symbol:
            return lot
    return None


def _config_dca_max_rounds(config_raw: dict | None) -> int:
    if not isinstance(config_raw, dict):
        return _DEFAULT_DCA_MAX_ROUNDS
    dca = config_raw.get("dca")
    if not isinstance(dca, dict):
        return _DEFAULT_DCA_MAX_ROUNDS
    mx = _int_or_none(dca.get("max_rounds"))
    return mx if mx is not None else _DEFAULT_DCA_MAX_ROUNDS


def _lot_dca_max_rounds(lot: dict, config_raw: dict | None) -> int:
    mx = _int_or_none(lot.get("dca_max_rounds"))
    if mx is not None:
        return mx
    return _config_dca_max_rounds(config_raw)


def _lot_partial_stop_paused(
    lot: dict, dca_rounds: int, dca_max: int, config_raw: dict | None
) -> bool:
    if lot.get("partial_stop_paused") is not None:
        return bool(lot.get("partial_stop_paused"))
    try:
        from strategies.dca import should_pause_partial_stop

        return bool(
            should_pause_partial_stop(
                {"dca_rounds": dca_rounds, "dca_max_rounds": dca_max},
                strategy_params=config_raw,
            )
        )
    except Exception:
        return False


def _compose_base_facts(
    *,
    symbol: str,
    lots: list[dict] | None,
    fusion: dict | None,
    relvol_open: int | None,
    relvol_max: int | None,
    config_raw: dict | None,
) -> dict:
    """Pure facts from already-resolved lots / fusion / relvol. No Mongo."""
    facts: dict = {}
    if isinstance(fusion, dict):
        regime = fusion.get("regime") or fusion.get("state")
        if regime:
            facts["fusion_regime"] = regime
    if relvol_open is not None:
        facts["relvol_open"] = relvol_open
    if relvol_max is not None:
        facts["relvol_max"] = relvol_max

    lot = _selected_lot(lots, symbol)
    if lot is None:
        return facts

    used = _int_or_none(lot.get("dca_rounds"))
    if used is None:
        used = 0
    mx = _lot_dca_max_rounds(lot, config_raw)
    facts["dca_rounds"] = used
    facts["dca_max_rounds"] = mx
    facts["partial_stop_paused"] = _lot_partial_stop_paused(lot, used, mx, config_raw)
    return facts


def _merge_facts(
    *,
    symbol: str,
    tenant_id: str,
    config_raw: dict | None,
    fusion: dict | None,
    relvol_open: int | None,
    relvol_max: int | None,
    lots: list[dict] | None,
    overlay: dict | None,
) -> dict:
    """Base (lots/fusion/relvol) + optional live memory/social + caller overlay."""
    overlay = overlay if isinstance(overlay, dict) else {}
    facts = _compose_base_facts(
        symbol=symbol,
        lots=lots,
        fusion=fusion,
        relvol_open=relvol_open,
        relvol_max=relvol_max,
        config_raw=config_raw,
    )
    if "memory_bias" not in overlay and "memory_flag" not in overlay:
        facts.update(_load_memory_facts(symbol, tenant_id, config_raw))
    if "cmc_confidence" not in overlay:
        facts.update(_load_social_facts(symbol))
    facts.update(overlay)
    return facts


def _fusion_badge(fusion: dict | None) -> str:
    if not isinstance(fusion, dict):
        return _BADGE_EMPTY
    regime = fusion.get("regime") or fusion.get("state")
    if regime is None or regime == "":
        return _BADGE_EMPTY
    return str(regime)


def _size_mult_badge(fusion: dict | None) -> float | str:
    if not isinstance(fusion, dict) or fusion.get("size_mult") is None:
        return _BADGE_EMPTY
    try:
        return float(fusion["size_mult"])
    except (TypeError, ValueError):
        return _BADGE_EMPTY


def _cash_badge(cash_mode: str | None) -> str:
    if cash_mode is None or cash_mode == "":
        return _BADGE_EMPTY
    return str(cash_mode)


def _relvol_badge(open_n: int | None, max_n: int | None) -> str:
    if open_n is None or max_n is None:
        return _BADGE_EMPTY
    try:
        return f"{int(open_n)} / {int(max_n)}"
    except (TypeError, ValueError):
        return _BADGE_EMPTY


def _conflict(hud: dict | None) -> str | None:
    hud = hud or {}
    social = hud.get("social") or {}
    memory = hud.get("memory") or {}
    if social.get("stance") == "ARMED" and memory.get("stance") == "BLOCK":
        return _CONFLICT_SOCIAL_MEMORY
    return None


def build_snapshot(
    *,
    tenant_id: str,
    symbol: str,
    config_raw: dict | None = None,
    lots: list[dict] | None = None,
    fusion: dict | None = None,
    cash_mode: str | None = None,
    relvol_open: int | None = None,
    relvol_max: int | None = None,
    facts: dict | None = None,
) -> dict:
    """Assemble a read-only desk snapshot.

    Keyword-injectable: tests pass lots/fusion/cash/relvol/facts and never hit Mongo.
    ``facts=`` overlays a base composed from lots/fusion/relvol (caller wins).
    Live memory/social I/O runs only when those keys are missing from the overlay.
    Enabled + tenant allowlist are checked before any live load.
    """
    if config_raw is None:
        config_raw = _load_config(tenant_id)

    desk = _desk_section(config_raw)
    # Non-dict desk section → fail closed (do not call .get on a bool/str).
    if desk is None or not bool(desk.get("enabled")):
        return _fail("desk_disabled")

    if str(tenant_id or "") not in _allowed_tenants(desk):
        return _fail("tenant_not_allowed")

    if lots is None:
        lots = _load_lots(tenant_id)
    elif not isinstance(lots, list):
        lots = []

    if fusion is None:
        fusion = _load_fusion(config_raw)
    elif not isinstance(fusion, dict):
        fusion = {}

    if relvol_open is None:
        relvol_open = _load_relvol_open(lots)
    if relvol_max is None:
        relvol_max = _load_relvol_max(config_raw)

    if cash_mode is None:
        cash_mode = _load_cash_mode(fusion, config_raw)

    facts = _merge_facts(
        symbol=symbol,
        tenant_id=tenant_id,
        config_raw=config_raw,
        fusion=fusion,
        relvol_open=relvol_open,
        relvol_max=relvol_max,
        lots=lots,
        overlay=facts,
    )

    hud = build_hud(facts)
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "symbol": symbol,
        "badges": {
            "fusion": _fusion_badge(fusion),
            "size_mult": _size_mult_badge(fusion),
            "cash": _cash_badge(cash_mode),
            "relvol": _relvol_badge(relvol_open, relvol_max),
        },
        "lots": list(lots),
        "hud": hud,
        "conflict": _conflict(hud),
        "next_edge": compose_next_edge(facts, hud),
        "partial_stop_paused": bool(facts.get("partial_stop_paused")),
    }
