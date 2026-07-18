"""Portfolio-level DCA: rank opportunities and fund via rotation sells."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.actions import BUY_DCA, SELL_FULL
from core.config import get_bot_config
from core.models import MarketContext, TradeOrder
from strategies.dca import DCACandidate, evaluate_dca_addon

from strategies.positions import get_key, get_position, position_notional_usdt
from strategies.registry import resolve_coin_config, resolve_strategy_params
from strategies.sell_rotation_policy import (
    can_rotation_evict,
    evaluate_ladder_terminal,
    evaluate_tail_idle_close,
    rotation_config,
    rotation_gain_pct,
)


@dataclass
class DCATarget:
    symbol: str
    timeframe: str
    source: str
    candidate: DCACandidate
    priority: float
    usdt_needed: float
    loss_pct: float
    score: int


@dataclass
class FundingSell:
    symbol: str
    timeframe: str
    source: str
    expected_usdt: float
    gain_pct: float
    rationale: str
    priority: int


@dataclass
class PortfolioDCAPlan:
    buy: DCATarget | None = None
    funding_sell: FundingSell | None = None
    shadow_only: bool = False
    audit: dict = field(default_factory=dict)


def portfolio_config(dca_cfg: dict | None, *, config_raw: dict | None = None) -> dict:
    defaults = {
        "enabled": False,
        "mode": "shadow",
        "max_buys_per_cycle": 1,
        "max_funding_sells_per_cycle": 1,
        "min_dca_score": 6,
        "min_priority_score": 0.0,
        "cash_buffer_usdt": 300.0,
        "stale_winner_min_hours": 36.0,
        "stale_winner_max_gain_pct": 8.0,
        "stale_winner_min_notional_usdt": 200.0,
    }
    if config_raw is None:
        try:
            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}

    global_port = dict(
        ((config_raw.get("volatile_altcoin") or {}).get("dca") or {}).get("portfolio") or {}
    )
    coin_port = dict((dca_cfg or {}).get("portfolio") or {})
    return {**defaults, **global_port, **coin_port}


def portfolio_enabled(strategy_params: dict | None, *, config_raw: dict | None = None) -> bool:
    dca = (strategy_params or {}).get("dca") if "dca" in (strategy_params or {}) else None
    return bool(portfolio_config(dca, config_raw=config_raw).get("enabled"))


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (datetime.now() - last_ts).total_seconds() / 3600.0


def _target_priority(candidate: DCACandidate, loss_pct: float) -> float:
    score = float(candidate.score or 0)
    loss_urgency = min(3.0, abs(min(0.0, loss_pct)) / 5.0)
    return score * 2.0 + loss_urgency


def _build_market(symbol: str, tf: str, price: float, position: dict, strategy_params: dict) -> MarketContext:
    from services.market_service import MarketService

    market_svc = MarketService()
    indicators = market_svc.fetch_indicators(symbol, tf, price)

    funding_rate_pct = None
    btc_underperf_ratio = None
    dca_cfg = dict((strategy_params or {}).get("dca") or {})
    scoring_cfg = dict(dca_cfg.get("scoring") or {})
    if dca_cfg.get("enabled") and scoring_cfg.get("enabled"):
        funding_rate_pct = market_svc.fetch_funding_rate(symbol)
        lookback = float(scoring_cfg.get("btc_lookback_hours", 8))
        btc_underperf_ratio = market_svc.btc_underperformance_ratio(
            symbol, tf, lookback_hours=lookback
        )

    return MarketContext(
        symbol=symbol,
        timeframe=tf,
        current_price=price,
        rsi=float(indicators.get("rsi", 50)),
        lower_bb=float(indicators.get("lower_bb", price)),
        atr_pct=float(indicators.get("atr_pct", 3.0)),
        funding_rate_pct=funding_rate_pct,
        btc_underperf_ratio=btc_underperf_ratio,
        has_position=True,
        average_entry=float(position.get("average_entry", 0) or 0),
        open_positions=1,
        strategy_params=strategy_params,
    )


def collect_dca_targets(
    coins: list[dict],
    price_map: dict[str, float],
    *,
    config_raw: dict | None = None,
) -> list[DCATarget]:
    targets: list[DCATarget] = []

    for coin in coins:
        try:
            symbol = coin.get("symbol", "")
            price = float(price_map.get(symbol, 0) or 0)
            if not symbol or price <= 0:
                continue
            coin_cfg = resolve_coin_config(coin)
            tf = coin_cfg.get("timeframe", "4h")
            pos = get_position(symbol, tf)
            if float(pos.get("amount", 0) or 0) <= 0:
                continue

            strategy_params = coin_cfg.get("strategy_params") or {}
            cfg_root = config_raw if config_raw is not None else get_bot_config().raw
            try:
                strategy_params = resolve_strategy_params(
                    coin_cfg,
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
            except Exception:
                pass

            dca_cfg = dict(strategy_params.get("dca") or {})
            port_cfg = portfolio_config(dca_cfg, config_raw=cfg_root)
            if not port_cfg.get("enabled"):
                continue
            market = _build_market(symbol, tf, price, pos, strategy_params)

            cand = evaluate_dca_addon(market, pos, strategy_params)
            if not cand:
                continue
            loss_pct = (price / float(pos.get("average_entry", price) or price) - 1.0) * 100.0
            if (cand.score or 0) < int(port_cfg.get("min_dca_score", 6)):
                continue
            priority = _target_priority(cand, loss_pct)
            if priority < float(port_cfg.get("min_priority_score", 0)):
                continue
            targets.append(
                DCATarget(
                    symbol=symbol,
                    timeframe=tf,
                    source="dca",
                    candidate=cand,
                    priority=priority,
                    usdt_needed=float(cand.usdt_amount or 0),
                    loss_pct=loss_pct,
                    score=int(cand.score or 0),
                )
            )
        except Exception as e:
            from logger import log

            log(
                f"Portfolio DCA target skip {coin.get('symbol', '?')}: {e}",
                "WARNING",
            )
            continue

    targets.sort(key=lambda t: t.priority, reverse=True)
    return targets


def _stale_winner_candidate(
    symbol: str,
    tf: str,
    market: MarketContext,
    position: dict,
    cfg: dict,
    port_cfg: dict,
    *,
    exclude_symbol: str,
) -> FundingSell | None:
    if symbol == exclude_symbol:
        return None
    gain = rotation_gain_pct(market)
    max_gain = float(port_cfg.get("stale_winner_max_gain_pct", 8.0))
    if gain < 0 or gain > max_gain:
        return None
    sold = float(position.get("sold_percent", 0) or 0)
    if sold >= 0.35:
        return None
    age_h = _hours_since(position.get("first_buy_at") or position.get("entry_at"))
    if age_h is None or age_h < float(port_cfg.get("stale_winner_min_hours", 36.0)):
        return None
    notional = position_notional_usdt(position)
    if notional < float(port_cfg.get("stale_winner_min_notional_usdt", 200.0)):
        return None
    if not can_rotation_evict(market, position, cfg):
        return None
    return FundingSell(
        symbol=symbol,
        timeframe=tf,
        source="stale_winner",
        expected_usdt=notional,
        gain_pct=gain,
        rationale=f"Stale winner {gain:.1f}% age {age_h:.0f}h → fund DCA",
        priority=2,
    )


def find_funding_sell(
    target: DCATarget,
    coins: list[dict],
    price_map: dict[str, float],
    *,
    cash_available: float,
    cash_needed: float,
    config_raw: dict | None = None,
) -> FundingSell | None:
    cfg_root = get_bot_config().raw if config_raw is None else config_raw
    port_cfg = portfolio_config({}, config_raw=cfg_root)
    shortfall = max(0.0, cash_needed - max(0.0, cash_available - float(port_cfg.get("cash_buffer_usdt", 300.0))))
    if shortfall <= 0:
        return None

    rot_cfg = rotation_config(cfg_root)
    candidates: list[FundingSell] = []

    for coin in coins:
        symbol = coin.get("symbol", "")
        if symbol == target.symbol:
            continue
        price = float(price_map.get(symbol, 0) or 0)
        if not symbol or price <= 0:
            continue
        coin_cfg = resolve_coin_config(coin)
        tf = coin_cfg.get("timeframe", "4h")
        pos = get_position(symbol, tf)
        if float(pos.get("amount", 0) or 0) <= 0:
            continue
        strategy_params = coin_cfg.get("strategy_params") or {}
        try:
            strategy_params = resolve_strategy_params(
                coin_cfg,
                has_position=True,
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            pass
        market = _build_market(symbol, tf, price, pos, strategy_params)

        tail = evaluate_tail_idle_close(market, pos, rot_cfg)
        if tail and can_rotation_evict(market, pos, rot_cfg):
            notional = position_notional_usdt(pos)
            candidates.append(
                FundingSell(
                    symbol=symbol,
                    timeframe=tf,
                    source=tail.source,
                    expected_usdt=notional,
                    gain_pct=rotation_gain_pct(market),
                    rationale=tail.rationale,
                    priority=5,
                )
            )
            continue

        ladder = evaluate_ladder_terminal(market, pos, strategy_params, rot_cfg)
        if ladder and can_rotation_evict(market, pos, rot_cfg):
            notional = position_notional_usdt(pos)
            candidates.append(
                FundingSell(
                    symbol=symbol,
                    timeframe=tf,
                    source=ladder.source,
                    expected_usdt=notional,
                    gain_pct=rotation_gain_pct(market),
                    rationale=ladder.rationale,
                    priority=4,
                )
            )
            continue

        stale = _stale_winner_candidate(
            symbol, tf, market, pos, rot_cfg, port_cfg, exclude_symbol=target.symbol,
        )
        if stale:
            candidates.append(stale)

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c.priority, c.expected_usdt), reverse=True)
    for cand in candidates:
        if cand.expected_usdt >= shortfall * 0.5:
            return cand
    return candidates[0]


def build_portfolio_dca_plan(
    coins: list[dict],
    price_map: dict[str, float],
    *,
    cash_available: float,
    config_raw: dict | None = None,
) -> PortfolioDCAPlan:
    cfg_root = get_bot_config().raw if config_raw is None else config_raw
    port_cfg = portfolio_config({}, config_raw=cfg_root)
    plan = PortfolioDCAPlan(audit={"targets": 0, "cash_available": cash_available})

    targets = collect_dca_targets(coins, price_map, config_raw=cfg_root)
    plan.audit["targets"] = len(targets)
    if not targets:
        return plan

    top = targets[0]
    plan.buy = top
    plan.shadow_only = bool(top.candidate.shadow_only)

    buffer = float(port_cfg.get("cash_buffer_usdt", 300.0))
    need = top.usdt_needed
    if cash_available - buffer < need:
        funding = find_funding_sell(
            top, coins, price_map,
            cash_available=cash_available,
            cash_needed=need,
            config_raw=cfg_root,
        )
        plan.funding_sell = funding
        if funding:
            plan.audit["funding_source"] = funding.source
            plan.audit["funding_usdt"] = funding.expected_usdt
            plan.audit["shortfall"] = max(0.0, need - (cash_available - buffer))

    plan.audit["priority"] = round(top.priority, 2)
    plan.audit["target"] = top.symbol
    plan.audit["usdt"] = need
    plan.audit["score"] = top.score
    return plan


def should_defer_per_coin_dca(strategy_params: dict | None, config_raw: dict | None = None) -> bool:
    """Defer only when this coin's merged strategy_params enable portfolio DCA live."""
    cfg_root = get_bot_config().raw if config_raw is None else config_raw
    dca = dict((strategy_params or {}).get("dca") or {})
    port = portfolio_config(dca, config_raw=cfg_root)
    return bool(port.get("enabled")) and str(port.get("mode", "shadow")).lower() == "live"