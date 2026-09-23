"""#563: venue gate is default-deny; new long BUYs have a mcap floor.

No network. Mcap oracle is mocked. Nothing is written under data/.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from notifications.user_explain import explain_risk
from risk.risk_manager import RiskManager
from services.venue_quality import (
    VenueMetrics,
    source_applies_venue,
    venue_quality_config,
)
from strategies.position_lock import build_lock
from strategies.short_policy import resolve_long_market_cap_min_usd, resolve_short_params

_VENUE = {
    "enabled": True,
    "min_quote_volume_24h_usdt": 50_000,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200,
    "min_volume_to_order_multiple": 20,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "on_fetch_error": "block_sensor",
}

_EMPTY_BOOK = VenueMetrics(
    symbol="L3/USDT",
    quote_volume_24h_usdt=615_000.0,
    last=0.00350,
    bid=0.00349,
    ask=0.00351,
    bid_size=0.0,
    ask_size=0.0,
    spread_pct=0.31,
    top_book_bid_usdt=0.0,
    top_book_ask_usdt=0.0,
    capture="ok",
)

_HEALTHY = VenueMetrics(
    symbol="L3/USDT",
    quote_volume_24h_usdt=5_000_000.0,
    last=1.0,
    bid=0.999,
    ask=1.001,
    bid_size=10_000.0,
    ask_size=10_000.0,
    spread_pct=0.2,
    top_book_bid_usdt=10_000.0,
    top_book_ask_usdt=10_000.0,
    capture="ok",
)

_SHORTS = {
    "enabled": True,
    "allow_live": False,
    "leverage_default": 2,
    "leverage_cap": 2,
    "max_open": 6,
    "max_margin_pct": 80,
    "volatile": {"market_cap_min_usd": 50_000_000},
    "stable": {"market_cap_min_usd": 100_000_000},
}


def _cfg(**risk_over) -> BotConfig:
    risk = {
        "min_trade_usdt": 1,
        "cash_floor_pct": 0,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "moderate_deploy": {"enabled": False},
        "slot_eviction": {"enabled": False},
        "max_daily_loss_pct": 0,
        "venue_quality": dict(_VENUE),
    }
    risk.update(risk_over)
    raw = {
        "max_usdt_per_trade": 500,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "paper": {"initial_capital_usdt": 100_000},
        "risk": risk,
        "shorts": dict(_SHORTS),
        "architecture": {},
    }
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


def _buy(
    symbol: str = "L3/USDT",
    *,
    source: str = "cmc",
    signal: str = "BUY",
    usdt: float = 200.0,
) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=1.0,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source,
    )


@contextmanager
def _eval_env(rm: RiskManager, *, position: dict | None = None, metrics=None, mcap=6_000_000):
    pos = {"amount": 0} if position is None else position
    cap = SimpleNamespace(
        max_open_eff=100,
        enabled=False,
        rationale="",
        factors={},
        free_slots=100,
        regime=None,
    )
    mcap_patch = (
        patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=mcap)
        if not isinstance(mcap, BaseException)
        else patch("data.cmc_market_cap.resolve_market_cap_usd", side_effect=mcap)
    )
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch.object(rm, "_dynamic_size", return_value=(200.0, {"total_multiplier": 1.0}))
        )
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
        stack.enter_context(patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None))
        stack.enter_context(
            patch("services.correlated_tier.api.correlated_tier_selloff_active", return_value=False)
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "apply_size_mult": False, "active": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch("services.venue_quality.get_venue_metrics", return_value=metrics or _HEALTHY)
        )
        stack.enter_context(mcap_patch)
        yield


def test_default_floors_and_apply_to_is_not_an_allowlist():
    assert resolve_long_market_cap_min_usd(None, {}) == 5_000_000
    assert resolve_long_market_cap_min_usd("stable", {}) == 20_000_000
    overridden = resolve_long_market_cap_min_usd(
        None,
        {"risk": {"longs": {"volatile": {"market_cap_min_usd": 7_000_000}}}},
    )
    assert overridden == 7_000_000
    cfg = venue_quality_config({"risk": {"venue_quality": dict(_VENUE)}})
    assert cfg["exempt_sources"] == ["manual"]
    assert source_applies_venue("manual", cfg) is False
    assert source_applies_venue("manual_sell_not_listed", cfg) is True
    assert source_applies_venue("cmc", cfg) is True
    assert source_applies_venue("not_a_real_source", cfg) is True
    params = resolve_short_params(tier=None, config_raw={"shorts": _SHORTS})
    stable = resolve_short_params(tier="stable", config_raw={"shorts": _SHORTS})
    assert params["market_cap_min_usd"] == 50_000_000
    assert stable["market_cap_min_usd"] == 100_000_000


def test_fixture_mcap_below_volatile_floor_is_long_mcap_no_fill():
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=4_900_000):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert dec.order is None
    assert "long mcap" in dec.message
    assert "5000000" in dec.message


def test_real_l3_book_zero_is_venue_block_not_long_mcap():
    rm = RiskManager(_cfg())
    with _eval_env(rm, metrics=_EMPTY_BOOK, mcap=5_194_841) as _env:
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"
    assert dec.code != "long_mcap"
    assert dec.order is None


def test_short_of_l3_mcap_stays_short_mcap():
    rm = RiskManager(_cfg())
    with patch("core.simulated_trading.is_real_live_trading", return_value=False), patch(
        "data.cmc_market_cap.resolve_market_cap_usd", return_value=5_194_841
    ), patch.object(rm, "_available_usdt", return_value=10_000):
        dec = rm.evaluate(
            TradeOrder(
                type="SHORT",
                symbol="L3/USDT",
                price=1.0,
                amount=0,
                usdt_amount=100,
            ),
            "4h",
            source="auto",
        )
    assert dec.approved is False
    assert dec.code == "short_mcap"
    assert "50000000" in dec.message


def test_unknown_source_healthy_book_above_floor_is_allowed():
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=6_000_000):
        dec = rm.evaluate(_buy(source="not_a_real_source"), "4h", source="not_a_real_source")
    assert dec.approved is True, f"{dec.code}: {dec.message}"
    assert dec.code not in ("venue_liquidity_block", "long_mcap")
    assert dec.order is not None
    assert dec.order.type == "BUY"


def test_unknown_source_empty_book_is_venue_block():
    rm = RiskManager(_cfg())
    with _eval_env(rm, metrics=_EMPTY_BOOK, mcap=6_000_000):
        dec = rm.evaluate(_buy(source="not_a_real_source"), "4h", source="not_a_real_source")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"
    assert dec.order is None


def test_manual_exempt_from_venue_and_long_mcap():
    rm = RiskManager(_cfg())
    with _eval_env(rm, metrics=_EMPTY_BOOK, mcap=None):
        dec = rm.evaluate(_buy(source="manual"), "4h", source="manual")
    assert dec.approved is True, f"{dec.code}: {dec.message}"
    assert dec.code not in ("venue_liquidity_block", "long_mcap")
    assert dec.order is not None
    assert dec.order.type == "BUY"


@pytest.mark.parametrize("mcap", [None, 0, -5])
def test_missing_zero_or_negative_mcap_is_long_mcap(mcap):
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=mcap):
        dec = rm.evaluate(_buy(source="auto"), "4h", source="auto")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert dec.order is None
    assert dec.message.startswith("long mcap ")


def test_mcap_lookup_exception_is_long_mcap():
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=RuntimeError("cmc down")):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert "None" in dec.message


def test_stable_tier_uses_20m_floor_without_a_lot_amount():
    rm = RiskManager(_cfg())
    pos = {"amount": 0, "strategy_tier": "stable"}
    with _eval_env(rm, position=pos, mcap=6_000_000):
        denied = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert denied.code == "long_mcap"
    assert "20000000" in denied.message
    with _eval_env(rm, position=pos, mcap=21_000_000):
        allowed = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert allowed.approved is True, f"{allowed.code}: {allowed.message}"


@pytest.mark.parametrize(
    "position,source,signal",
    [
        ({"amount": 2.0, "average_entry": 1.0, "strategy_tier": "volatile"}, "dca", "BUY_DCA"),
        ({"amount": 2.0, "average_entry": 1.0}, "dca", "BUY"),
        ({"amount": 2.0, "average_entry": 1.0}, "auto", "BUY_DCA"),
    ],
)
def test_open_lot_dca_skips_venue_and_long_mcap(position, source, signal):
    rm = RiskManager(_cfg())
    with _eval_env(rm, position=position, metrics=_EMPTY_BOOK, mcap=None):
        dec = rm.evaluate(_buy(source=source, signal=signal), "4h", source=source)
    assert dec.code not in ("venue_liquidity_block", "long_mcap")
    assert dec.approved is True, f"{dec.code}: {dec.message}"
    assert dec.order.type == "BUY"


def test_locked_coin_sell_is_not_created():
    rm = RiskManager(_cfg(position_locks={"enabled": True}))
    locked = {
        "amount": 100.0,
        "average_entry": 1.0,
        "lock": build_lock(reason="hold"),
    }
    order = TradeOrder(
        type="SELL",
        symbol="L3/USDT",
        price=1.0,
        amount=100.0,
        signal="SELL_FULL",
        source="exit_ws",
    )
    with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
        "risk.risk_manager.get_position", return_value=locked
    ), patch("strategies.position_lock.log_lock_block"), patch.object(
        rm, "_resolve_sell_order", side_effect=AssertionError("sell created")
    ):
        dec = rm.evaluate(order, "4h", source="exit_ws")
    assert dec.approved is False
    assert dec.code == "position_locked"
    assert dec.order is None
    assert locked["amount"] == 100.0


def test_long_mcap_german_line():
    text = explain_risk("long mcap 1 < min 5000000", code="long_mcap")
    assert "Long" in text
    assert text != "long mcap 1 < min 5000000"
