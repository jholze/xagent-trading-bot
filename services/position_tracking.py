"""Portfolio-wide position snapshots for prod vs staging tracking."""

from __future__ import annotations

from datetime import datetime

_cycle_counter = 0

from core.actions import is_sell, normalize
from core.models import MarketContext
from logger import log
from services.observability_store import persist_position_snapshot, runtime_context
from services.position_metrics import position_metrics
from strategies.market_structure import evaluate_market_structure_sells
from strategies.positions import get_key, get_position, list_active_positions
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime
from strategies.registry import resolve_strategy_params
from strategies.sell_rotation_policy import (
    SellPolicyAudit,
    apply_rotation_sell_filters,
    audit_to_dict,
)
from strategies.time_profit_exit import evaluate_time_profit_exit
from strategies.trailing_stop import evaluate_trailing_stop
from strategies.trailing_take_profit import evaluate_trailing_take_profit


def _lite_exit_candidates(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    include_exit_sensor: bool,
    market_service=None,
    escfg: dict | None = None,
) -> tuple[list[tuple], dict]:
    candidates: list[tuple] = []
    for cand in evaluate_market_structure_sells(
        market, strategy_params, position, ta_bearish=False,
    ):
        candidates.append((cand.action, cand.priority, cand.source))

    if include_exit_sensor and escfg and escfg.get("enabled", True) and market_service:
        try:
            from strategies.exit_sensor import evaluate_exit_sensor_sells

            metrics_15m = market_service.fetch_exit_metrics_15m(market.symbol, escfg)
            metrics_1h = market_service.fetch_exit_metrics_1h(market.symbol)
            btc_delta = None
            bcfg = escfg.get("btc_rs") or {}
            if bcfg.get("enabled", True):
                btc_delta = market_service.btc_relative_return_delta(
                    market.symbol,
                    timeframe=str(bcfg.get("timeframe", "4h")),
                    periods=int(bcfg.get("periods", 1)),
                )
            for cand in evaluate_exit_sensor_sells(
                market,
                position,
                escfg,
                metrics_15m=metrics_15m,
                metrics_1h=metrics_1h,
                btc_rs_delta=btc_delta,
            ):
                candidates.append((cand.action, cand.priority, cand.source))
        except Exception as exc:
            log(f"position_tracking exit_sensor {market.symbol}: {exc}", "WARNING")

    for evaluator, args in (
        (evaluate_trailing_take_profit, (market, position, strategy_params)),
        (evaluate_profit_max_lifetime, (market, position, strategy_params)),
        (evaluate_trailing_stop, (market, position, strategy_params)),
        (evaluate_time_profit_exit, (market, position, strategy_params)),
    ):
        cand = evaluator(*args)
        if cand and not getattr(cand, "shadow_only", False):
            candidates.append((cand.action, cand.priority, cand.source))

    if not candidates:
        return [], audit_to_dict(SellPolicyAudit())

    from core.config import get_bot_config

    filtered, audit = apply_rotation_sell_filters(
        candidates,
        market,
        position,
        strategy_params,
        get_bot_config().raw,
    )
    return filtered, audit_to_dict(audit)


def build_position_row(
    symbol: str,
    timeframe: str,
    price: float,
    *,
    include_exit_sensor: bool = False,
    market_service=None,
    escfg: dict | None = None,
) -> dict | None:
    pos = get_position(symbol, timeframe)
    amount = float(pos.get("amount") or 0)
    if amount <= 0:
        return None
    entry = float(pos.get("average_entry") or 0)
    if entry <= 0 or price <= 0:
        return None

    params = resolve_strategy_params(
        {"symbol": symbol, "timeframe": timeframe},
        has_position=True,
        frozen_tier=pos.get("strategy_tier"),
    )
    market = MarketContext(
        symbol=symbol,
        timeframe=timeframe,
        current_price=price,
        has_position=True,
        average_entry=entry,
        atr_pct=5.0,
        strategy_params=params,
        rsi=float(pos.get("last_rsi") or 50),
        lower_bb=price * 0.95,
        upper_bb=price * 1.05,
        middle_bb=price,
        vol_multiplier=1.0,
    )
    metrics = position_metrics(market, pos, params)
    _, audit = _lite_exit_candidates(
        market,
        pos,
        params,
        include_exit_sensor=include_exit_sensor,
        market_service=market_service,
        escfg=escfg,
    )
    would = audit.get("would_sell") or ""
    would_norm = normalize(would) if would else "HOLD"
    return {
        "key": get_key(symbol, timeframe),
        "symbol": symbol,
        "timeframe": timeframe,
        **metrics,
        "would_action": would_norm,
        "would_source": audit.get("would_source") or "",
        "trail_exclusive_blocked": list(audit.get("trail_exclusive_blocked") or []),
        "rotation_blocked": bool(audit.get("rotation_blocked")),
    }


def snapshot_all_open_positions(
    price_map: dict[str, float] | None = None,
    *,
    config_raw: dict | None = None,
) -> dict | None:
    """Capture all open lots; returns snapshot dict or None if disabled."""
    try:
        from core.config import get_bot_config

        cfg = (config_raw or get_bot_config().raw).get("observability") or {}
    except Exception:
        cfg = {}
    if not cfg.get("position_snapshots_enabled", True):
        return None

    open_lots = list_active_positions()
    if not open_lots:
        return None

    prices = dict(price_map or {})
    missing = [p["symbol"] for p in open_lots if float(prices.get(p["symbol"], 0) or 0) <= 0]
    if missing:
        from price_fetcher import get_prices_batch

        prices.update(get_prices_batch(sorted(set(missing))))

    include_sensor = bool(cfg.get("position_snapshots_include_exit_sensor", False))
    market_service = None
    escfg = None
    if include_sensor:
        from core.config import get_bot_config
        from services.market_service import MarketService

        market_service = MarketService(config_raw)
        escfg = get_bot_config().exit_sensor_config()

    positions_out: list[dict] = []
    for lot in open_lots:
        sym = lot.get("symbol", "")
        tf = lot.get("timeframe", "4h")
        price = float(prices.get(sym, 0) or 0)
        if price <= 0:
            continue
        row = build_position_row(
            sym,
            tf,
            price,
            include_exit_sensor=include_sensor,
            market_service=market_service,
            escfg=escfg,
        )
        if row:
            positions_out.append(row)

    if not positions_out:
        return None

    ctx = runtime_context(config_raw)
    snapshot = {
        "ts": datetime.now().isoformat(),
        **ctx,
        "open_count": len(positions_out),
        "positions": positions_out,
    }
    persist_position_snapshot(snapshot)
    return snapshot


def maybe_snapshot_after_cycle(
    price_map: dict[str, float] | None = None,
    *,
    config_raw: dict | None = None,
) -> dict | None:
    global _cycle_counter
    _cycle_counter += 1
    try:
        from core.config import get_bot_config

        cfg = (config_raw or get_bot_config().raw).get("observability") or {}
    except Exception:
        cfg = {}
    every = max(1, int(cfg.get("position_snapshots_every_n_cycles", 1)))
    if _cycle_counter % every != 0:
        return None
    snap = snapshot_all_open_positions(price_map, config_raw=config_raw)
    if snap:
        log(
            f"position_snapshot: {snap['open_count']} open lots "
            f"stack={snap.get('bot_stack')} commit={snap.get('build_commit')}",
            "INFO",
        )
    return snap