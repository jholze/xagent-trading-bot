from core.config import get_bot_config
from core.models import AllocationDecision, RegimeResult
from data_manager import is_dry_run_enhanced
from strategies.base import BaseStrategy

_STRATEGY_CLASSES = {}


def _load_registry():
    if _STRATEGY_CLASSES:
        return _STRATEGY_CLASSES
    from strategies.technical_rsi_bb import TechnicalRSIStrategy
    _STRATEGY_CLASSES["technical_rsi_bb"] = TechnicalRSIStrategy
    try:
        from strategies.grid import GridStrategy
        _STRATEGY_CLASSES["grid"] = GridStrategy
    except Exception:
        pass
    return _STRATEGY_CLASSES




_BUY_PARAM_KEYS = (
    "volume_multiplier",
    "reversal_volume_multiplier",
    "buy_regime",
    "rsi_buy_low",
    "rsi_buy_high",
    "reversal_rsi_cross_low",
    "reversal_rsi_cross_high",
)

_EXPLICIT_PRESERVE_KEYS = (
    "rsi_sell_mode",
    "rsi_sell_30",
    "rsi_sell_20",
    "rsi_sell_min_gain_pct",
    "stop_loss_pct",
    "rsi_buy_low",
    "rsi_buy_high",
    "volume_multiplier",
    "reversal_volume_multiplier",
    "buy_regime",
    "min_hours_between_buys",
    "min_hours_between_sells",
)

# Identity/meta only — never strategy params. Optional token_address / live_enabled
# may sit on an identity row without making it explicit.
STRATEGY_IDENTITY_META_KEYS = frozenset({
    "symbol",
    "timeframe",
    "strategy_class",
    "description",
    "auto_identity",
    "token_address",
    "live_enabled",
})

# Shared param-key set (Opus S2). One constant, not re-derived per call site.
# Presence of any of these wins over a stale auto_identity marker.
STRATEGY_PARAM_KEYS = frozenset(_EXPLICIT_PRESERVE_KEYS) | frozenset(_BUY_PARAM_KEYS) | frozenset({
    "dca",
    "take_profit_pct",
    "exit_ladder",
    "take_profit_tiers",
    "strategy_profile",
    "hermes_experiment_id",
    "hermes_updated_at",
    "sandbox_id",
    "source_account",
})


def is_identity_strategy_entry(entry) -> bool:
    """True only when ``auto_identity`` is true and no strategy param key is present.

    Param keys win: a preserve-key row with a stale marker is explicit.
    Absence of keys without the marker is not identity (do not ignore a broken ARIA row).
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("auto_identity") is not True:
        return False
    if any(key in entry for key in STRATEGY_PARAM_KEYS):
        return False
    return not (set(entry) - STRATEGY_IDENTITY_META_KEYS)


def _patch_has_param_keys(patch: dict | None) -> bool:
    if not patch:
        return False
    for key in patch:
        if key in STRATEGY_PARAM_KEYS:
            return True
        if key not in STRATEGY_IDENTITY_META_KEYS:
            return True
    return False


def _explicit_strategy_entry(symbol: str, tf: str) -> dict | None:
    cfg = get_bot_config()
    for entry in cfg.raw.get("strategies", []):
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            if is_identity_strategy_entry(entry):
                continue
            return entry
    return None


from strategies.sell_profile import apply_position_sell_overlay
from core.models import RegimeResult, AllocationDecision


def _hermes_memory_params(symbol: str, tf: str) -> dict | None:
    """Load per-coin params Hermes / personal entry renewal (survives restarts).

    Prefer personal_entry_v1 tags from entry_recipe renewal; otherwise Hermes
    baseline params. Buy keys are preserved after tier overlay in resolve.
    """
    try:
        from strategies.entry_recipe import (
            STRATEGY_PROFILE_PERSONAL,
            load_personal_params,
        )

        personal = load_personal_params(symbol, tf)
        # load_personal_params already excludes tier-fallback stamps
        if personal and personal.get("strategy_profile") == STRATEGY_PROFILE_PERSONAL:
            personal = dict(personal)
            personal.update(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "strategy_profile": STRATEGY_PROFILE_PERSONAL,
                }
            )
            return personal
    except Exception:
        pass

    try:
        from hermes.memory import store
        from strategies.entry_recipe import is_personal_fallback_profile

        profile = store.load_profile(symbol, tf)
    except Exception:
        return None

    params = dict(profile.get("params") or {})
    if not params:
        return None
    # Audit-only fallback profiles must not feed buy-key prefer path
    if is_personal_fallback_profile(params):
        return None

    profile_name = params.get("strategy_profile") or "hermes_baseline"
    params.update({
        "symbol": symbol,
        "timeframe": tf,
        "strategy_profile": profile_name,
        "hermes_baseline_updated_at": profile.get("updated_at"),
    })
    exp_id = profile.get("hermes_experiment_id") or params.get("hermes_experiment_id")
    if exp_id:
        params["hermes_experiment_id"] = exp_id
        if profile.get("hermes_updated_at"):
            params["hermes_updated_at"] = profile.get("hermes_updated_at")
    return params


def _resolve_volatility_tier(
    coin: dict,
    atr_pct: float,
    va_cfg: dict,
    frozen_tier: str | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
) -> str | None:
    if not va_cfg.get("enabled", False):
        return None
    from intelligence.volatility_classifier import volatility_tier

    return volatility_tier(
        coin,
        atr_pct,
        va_cfg,
        frozen_tier=frozen_tier,
        range_24h_pct=range_24h_pct,
        change_24h_pct=change_24h_pct,
    )


def _has_open_position(symbol: str, timeframe: str) -> bool:
    from strategies.positions import get_position, is_open_position

    return is_open_position(get_position(symbol, timeframe))


def resolve_effective_timeframe(
    coin: dict,
    atr_pct: float | None = None,
    frozen_tier: str | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
) -> str:
    """Pick analysis timeframe: open lots keep their TF; volatile coins use 1h."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    watchlist_tf = coin.get("timeframe", "4h")
    va_cfg = cfg.volatile_altcoin_config
    volatile_tf = str(va_cfg.get("timeframe") or "").strip()

    from intelligence.strategy_backtest import classify_coin
    from strategies.positions import find_open_position_for_symbol

    coin_class = classify_coin(symbol, coin.get("strategy_params"))
    if coin_class == "large_cap":
        return watchlist_tf

    # Any open lot for this symbol wins (not only candidate TFs) — avoids
    # volatile 1h entries being analysed/sold against an empty 4h key.
    open_lot = find_open_position_for_symbol(symbol, preferred_timeframe=watchlist_tf)
    if open_lot:
        return open_lot[0]

    if not va_cfg.get("enabled", False) or not volatile_tf:
        return watchlist_tf

    if coin_class == "meme":
        return volatile_tf
    if va_cfg.get("micro_cap_override", True) and coin.get("market_cap_tier") == "micro":
        return volatile_tf

    volatile_sources = ("cmc_trending", "dry_run_expansion")
    if coin.get("source") in volatile_sources:
        if atr_pct is None:
            return volatile_tf
        tier = _resolve_volatility_tier(
            coin, atr_pct, va_cfg, frozen_tier=frozen_tier,
            range_24h_pct=range_24h_pct, change_24h_pct=change_24h_pct,
        )
        if tier == "volatile":
            return volatile_tf
        return watchlist_tf

    if atr_pct is not None:
        tier = _resolve_volatility_tier(
            coin, atr_pct, va_cfg, frozen_tier=frozen_tier,
            range_24h_pct=range_24h_pct, change_24h_pct=change_24h_pct,
        )
        if tier == "volatile":
            return volatile_tf

    return watchlist_tf


def _buy_profile_source(tier: str, coin: dict, cfg) -> dict:
    from intelligence.strategy_backtest import classify_coin

    raw = cfg.raw
    if tier == "volatile":
        return cfg.volatile_altcoin_config
    coin_class = classify_coin(coin.get("symbol", ""), coin.get("strategy_params"))
    if coin_class == "large_cap":
        stable = raw.get("stable_altcoin", {})
        return stable if stable.get("enabled", True) else {}
    if coin_class == "meme":
        return cfg.volatile_altcoin_config
    mid = raw.get("mid_cap_defaults", {})
    return mid if mid else raw.get("altcoin_social", {})


def _buy_profile_overlay(base: dict, coin: dict, tier: str | None, cfg) -> dict:
    if not tier:
        return base
    source = _buy_profile_source(tier, coin, cfg)
    if not source:
        return base
    merged = dict(base)
    for key in _BUY_PARAM_KEYS:
        if key in source:
            merged[key] = source[key]
    if "dca" in source:
        merged["dca"] = source["dca"]
    merged["volatility_tier"] = tier
    return merged


def _pure_volatile_profile(va_cfg: dict, tier: str, symbol: str, tf: str, cfg) -> dict:
    profile = dict(va_cfg)
    if is_dry_run_enhanced():
        profile.update(cfg.dry_run_defaults)
    profile.update({
        "symbol": symbol,
        "timeframe": tf,
        "strategy_profile": "volatile_altcoin",
        "volatility_tier": tier,
    })
    if profile.get("take_profit_pct") is None:
        profile.pop("take_profit_pct", None)
    return profile


def _apply_path_stats_soft_bias(result: dict, symbol: str, has_position: bool, cfg) -> dict:
    """Soft-bias trail/arm from path-stats memory (positions only). Fail-open."""
    if not has_position or not result or not symbol:
        return result
    try:
        from intelligence.memory.path_stats_bias import apply_path_stats_soft_bias

        raw = getattr(cfg, "raw", None) if cfg is not None else None
        return apply_path_stats_soft_bias(result, symbol, config=raw)
    except Exception:
        return result


def resolve_strategy_params(
    coin: dict,
    has_position: bool = False,
    atr_pct: float = 3.0,
    frozen_tier: str | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
    regime_result: RegimeResult | None = None,
    allocation: AllocationDecision | None = None,
) -> dict:
    """
    Pick strategy params.
    Erweitert um regime_result + allocation (von RegimeDetector + StrategyAllocator).
    Bestehende volatility_tier Logik wird erweitert, nicht ersetzt.
    Open positions get optional path-stats soft bias on trail/arm knobs.
    """
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    tf = coin.get("timeframe", "4h")

    va_cfg = cfg.volatile_altcoin_config
    tier = _resolve_volatility_tier(
        coin, atr_pct, va_cfg, frozen_tier=frozen_tier,
        range_24h_pct=range_24h_pct, change_24h_pct=change_24h_pct,
    )
    stable_cfg = cfg.stable_altcoin_config

    regime_profile = {}
    if regime_result:
        regime_profile["regime"] = regime_result.primary_regime
        regime_profile["regime_confidence"] = regime_result.confidence
        regime_profile["sentiment_score"] = regime_result.sentiment_score

    if allocation:
        regime_profile["allocation"] = {
            "strategy_weights": getattr(allocation, "strategy_weights", {}),
            "exposure_multiplier": getattr(allocation, "exposure_multiplier", 1.0),
        }
        regime_profile.update(getattr(allocation, "momentum_params_override", {}) or {})
        if getattr(allocation, "grid_params", None):
            regime_profile["grid"] = allocation.grid_params

    def _out(result: dict) -> dict:
        result.update(regime_profile)
        result = _apply_path_stats_soft_bias(result, symbol, has_position, cfg)
        try:
            from strategies.correlated_tier_overlay import apply_correlated_tier_overlay

            raw = getattr(cfg, "raw", None) if cfg is not None else None
            result = apply_correlated_tier_overlay(
                result, symbol, raw if isinstance(raw, dict) else {}
            )
        except Exception:
            pass
        return result

    # Personal / Hermes buy keys always beat config.strategies[] and tier overlay.
    # Load before explicit short-circuit so ARIA/USDT-style config entries still
    # receive renewed personal_entry_v1 params on the live TF (often 4h).
    hermes_params = _hermes_memory_params(symbol, tf)
    from strategies.entry_recipe import preserve_buy_params

    def _prefer_personal(result: dict) -> dict:
        if not hermes_params:
            return result
        from strategies.entry_recipe import (
            STRATEGY_PROFILE_PERSONAL,
            is_personal_fallback_profile,
        )

        # Never clobber config.strategies with tier-default "personal" fallbacks
        if is_personal_fallback_profile(hermes_params):
            return result
        if hermes_params.get("strategy_profile") != STRATEGY_PROFILE_PERSONAL:
            return result
        preferred = {
            k: hermes_params[k]
            for k in _BUY_PARAM_KEYS
            if k in hermes_params and hermes_params[k] is not None
        }
        meta = {
            k: hermes_params[k]
            for k in (
                "strategy_profile",
                "personal_entry_renewed_at",
                "personal_entry_score",
                "hermes_baseline_updated_at",
            )
            if hermes_params.get(k) is not None
        }
        return preserve_buy_params(result, {**meta, **preferred})

    explicit = _explicit_strategy_entry(symbol, tf)
    if explicit:
        base = dict(explicit)
        preserved = {k: base[k] for k in _EXPLICIT_PRESERVE_KEYS if k in base}
        base = _buy_profile_overlay(base, coin, tier, cfg)
        result = apply_position_sell_overlay(
            base,
            tier=tier,
            has_position=has_position,
            symbol=symbol,
            tf=tf,
            volatile_cfg=va_cfg,
            stable_cfg=stable_cfg,
            cfg=cfg,
        )
        result.update(preserved)
        # Personal buy knobs win over stale config.strategies[] (skeptic bug).
        return _out(_prefer_personal(result))

    volatile_active = has_position and tier == "volatile"

    if hermes_params:
        # Tier overlay may rewrite buy knobs — personal/Hermes entry keys win.
        preferred_buy = {
            k: hermes_params[k]
            for k in _BUY_PARAM_KEYS
            if k in hermes_params
        }
        base = _buy_profile_overlay(hermes_params, coin, tier, cfg)
        base = preserve_buy_params(base, {**hermes_params, **preferred_buy})
        result = apply_position_sell_overlay(
            base,
            tier=tier,
            has_position=has_position,
            symbol=symbol,
            tf=tf,
            volatile_cfg=va_cfg,
            stable_cfg=stable_cfg,
            cfg=cfg,
        )
        # Restore buy knobs only — not strategy_profile, or +volatile overlay is clobbered.
        result = preserve_buy_params(result, preferred_buy)
        return _out(result)

    if volatile_active:
        result = _pure_volatile_profile(va_cfg, tier, symbol, tf, cfg)
        return _out(result)

    if coin.get("source") == "cmc_trending" and tier == "volatile":
        result = _pure_volatile_profile(va_cfg, tier, symbol, tf, cfg)
        return _out(result)

    if coin.get("source") == "cmc_trending" or coin.get("market_cap_tier") == "micro":
        profile = dict(cfg.altcoin_social_config)
        if is_dry_run_enhanced():
            profile.update(cfg.dry_run_defaults)
        profile.update({"symbol": symbol, "timeframe": tf})
        profile = _buy_profile_overlay(profile, coin, tier, cfg)
        result = apply_position_sell_overlay(
            profile,
            tier=tier,
            has_position=has_position,
            symbol=symbol,
            tf=tf,
            volatile_cfg=va_cfg,
            stable_cfg=stable_cfg,
            cfg=cfg,
        )
        return _out(result)

    params = cfg.strategy_params(symbol, tf)
    base = dict(params) if params else {}
    base = _buy_profile_overlay(base, coin, tier, cfg)
    result = apply_position_sell_overlay(
        base,
        tier=tier,
        has_position=has_position,
        symbol=symbol,
        tf=tf,
        volatile_cfg=va_cfg,
        stable_cfg=stable_cfg,
        cfg=cfg,
    )
    return _out(result)

def resolve_coin_config(coin: dict) -> dict:
    """Merge watchlist coin with matching config.strategies[] entry."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    tf = coin.get("timeframe", "4h")
    merged = dict(coin)

    for entry in cfg.raw.get("strategies", []):
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            # Identity must continue (Opus N2). Copy-then-break drops the
            # resolved key and signal_orchestrator.py:119 sees {}.
            if is_identity_strategy_entry(entry):
                continue
            if entry.get("live_enabled") is False and cfg.trading_mode == "live":
                continue
            merged["timeframe"] = entry.get("timeframe", tf)
            merged["strategy_class"] = entry.get("strategy_class", "technical_rsi_bb")
            merged["strategy_params"] = entry
            break
    else:
        merged.setdefault("strategy_class", "technical_rsi_bb")
        merged["strategy_params"] = resolve_strategy_params(coin, has_position=False)

    return merged


def list_registered_strategies() -> list:
    return list(_load_registry().keys())


def get_strategy(coin: dict) -> BaseStrategy:
    preset_params = coin.get("strategy_params") or {}
    coin = resolve_coin_config(coin)
    if preset_params.get("strategy_profile"):
        coin["strategy_params"] = preset_params

    registry = _load_registry()
    strategy_class = coin.get("strategy_class", "technical_rsi_bb")

    allocation = preset_params.get("allocation") or {}
    weights = allocation.get("strategy_weights", {})
    if weights.get("grid", 0) > weights.get("momentum", 0):
        strategy_class = "grid"

    cls = registry.get(strategy_class)
    if cls is None:
        if strategy_class == "grid":
            from strategies.grid import GridStrategy
            cls = GridStrategy
        else:
            from strategies.technical_rsi_bb import TechnicalRSIStrategy
            cls = TechnicalRSIStrategy
    return cls()


def _persisted_strategy_rows(tenant_id: str | None) -> list[dict] | None:
    """Load the tenant's stored strategies[] list, not the merged operator view.

    Arrays replace on patch_config (#456): henry must merge into *his* list,
    never freeze operator config.json strategies into the tenant body.
    """
    from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id
    from data_manager import (
        _load_default_config_from_disk,
        _load_tenant_config_body,
        get_config,
    )

    tid = resolve_tenant_id(tenant_id)
    if tid == DEFAULT_TENANT:
        cfg = get_config(tenant_id=tid) or {}
        rows = cfg.get("strategies") or []
        return [dict(e) for e in rows if isinstance(e, dict)]
    default_cfg = _load_default_config_from_disk()
    try:
        body = _load_tenant_config_body(tid, default_cfg) or {}
    except Exception:
        return None
    rows = body.get("strategies") or []
    return [dict(e) for e in rows if isinstance(e, dict)]


def _identity_strategy_row(symbol: str, tf: str, patch: dict) -> dict:
    row = {
        "symbol": symbol,
        "timeframe": tf,
        "strategy_class": patch.get("strategy_class") or "technical_rsi_bb",
        "description": patch.get("description") or f"Identity {symbol} {tf}",
        "auto_identity": True,
    }
    if "token_address" in patch:
        row["token_address"] = patch["token_address"]
    if "live_enabled" in patch:
        row["live_enabled"] = patch["live_enabled"]
    return row


def upsert_strategy_row(
    symbol: str,
    tf: str,
    patch: dict | None = None,
    tenant_id: str | None = None,
) -> tuple:
    """Append or patch one tenant ``strategies[]`` row via ``patch_config``.

    Identity create: meta + ``auto_identity: true``, no overlay keys.
    Same tenant+symbol+TF with no param change is a no-op.
    Param patch merges keys and clears ``auto_identity``.
    Writes the **full** tenant strategies list (arrays replace). Never
    ``save_config(get_config())``.
    """
    from core.tenant_context import resolve_tenant_id
    from data_manager import patch_config

    if not symbol:
        return False, "Missing symbol"
    patch = dict(patch or {})
    tf = tf or "4h"
    rows = _persisted_strategy_rows(tenant_id)
    if rows is None:
        return False, "Failed to load tenant strategies"

    idx = None
    for i, entry in enumerate(rows):
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            idx = i
            break

    has_params = _patch_has_param_keys(patch)
    if idx is None:
        if has_params:
            new_row = {k: v for k, v in patch.items() if k != "auto_identity"}
            new_row["symbol"] = symbol
            new_row["timeframe"] = tf
            new_row.setdefault("strategy_class", "technical_rsi_bb")
            if not new_row.get("description"):
                new_row["description"] = f"Hermes-tuned {symbol} {tf}"
        else:
            new_row = _identity_strategy_row(symbol, tf, patch)
        rows.append(new_row)
    else:
        existing = rows[idx]
        if not has_params:
            return True, "noop"
        merged = dict(existing)
        desc_before = merged.get("description")
        merged.update({k: v for k, v in patch.items() if k != "auto_identity"})
        if desc_before:
            merged["description"] = desc_before
        merged.pop("auto_identity", None)
        merged["symbol"] = symbol
        merged["timeframe"] = tf
        if merged == existing:
            return True, "noop"
        rows[idx] = merged

    tid = resolve_tenant_id(tenant_id)
    if patch_config({"strategies": rows}, tenant_id=tid):
        return True, f"upserted {symbol} {tf}"
    return False, "Failed to save config.json"


def sync_hermes_baseline_to_config(baseline: dict, experiment_id: str = "") -> tuple:
    """Patch config.strategies[] with Hermes baseline params for symbol/timeframe."""
    from data_manager import reload_config

    symbol = baseline.get("symbol")
    timeframe = baseline.get("timeframe", "4h")
    params = baseline.get("params") or {}
    if not symbol or not params:
        return False, "Baseline missing symbol or params"

    patch = dict(params)
    patch["hermes_experiment_id"] = experiment_id
    patch["hermes_updated_at"] = baseline.get("updated_at")
    patch.setdefault("strategy_class", "technical_rsi_bb")
    patch.setdefault("description", f"Hermes-tuned {symbol} {timeframe}")
    ok, msg = upsert_strategy_row(symbol, timeframe, patch)
    if ok:
        reload_config()
        return True, f"Hermes baseline synced to config.strategies for {symbol} {timeframe}"
    return False, msg


def promote_hypothesis_to_config(hypothesis: dict) -> tuple:
    """Promote a sandbox hypothesis into config.strategies[].

    Identity slots merge params and clear ``auto_identity`` (Opus N1) instead
    of refusing “already exists”.
    """
    symbol = hypothesis.get("symbol")
    if not symbol:
        return False, "Hypothesis has no symbol — assign one before promotion"

    tf = hypothesis.get("timeframe", "4h")
    rows = _persisted_strategy_rows(None)
    if rows is None:
        return False, "Failed to load tenant strategies"
    for entry in rows:
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            if entry.get("sandbox_id") == hypothesis.get("id"):
                return True, "Already promoted"
            if not is_identity_strategy_entry(entry):
                return False, f"Strategy already exists for {symbol} {tf}"
            break

    params = dict(hypothesis.get("params") or {})
    params.update({
        "strategy_class": "technical_rsi_bb",
        "description": f"Promoted from sandbox: {hypothesis.get('name', '')}",
        "sandbox_id": hypothesis.get("id"),
        "source_account": hypothesis.get("source_account"),
    })
    ok, msg = upsert_strategy_row(symbol, tf, params)
    if ok:
        return True, f"Added {symbol} ({tf}) to strategies"
    return False, msg