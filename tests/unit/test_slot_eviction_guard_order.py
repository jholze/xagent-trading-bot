"""#334 Slot eviction must not sell before remaining buy guards can still reject."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk.risk_manager import RiskManager


def _cfg(**risk_over) -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["initial_capital_usdt"] = 100_000
    raw["max_open_positions"] = 24
    raw["max_usdt_per_trade"] = 500
    raw["max_position_percent"] = 80
    raw["max_daily_trades"] = 2
    live = dict(raw.get("live") or {})
    live["dry_run_enhanced"] = False
    raw["live"] = live
    base_risk = raw.get("risk") if isinstance(raw.get("risk"), dict) else {}
    risk = dict(base_risk)
    risk["cash_floor_pct"] = 18
    risk["cash_floor_basis"] = "initial"
    risk["dca_reserve_pct"] = 0
    risk["min_trade_usdt"] = 100.0
    risk["max_daily_buys"] = 2
    risk["max_daily_dca_buys"] = 0
    risk["max_daily_dca_usdt"] = 0
    risk["max_daily_loss_pct"] = 0
    risk["venue_quality"] = {"enabled": False}
    risk["position_capacity"] = {"enabled": False}
    risk["cash_policy"] = {"enabled": False}
    risk["slot_eviction"] = {
        "enabled": True,
        "mode": "live",
        "sources": ["entry_sensor_15m"],
    }
    risk.update(risk_over)
    raw["risk"] = risk
    uni = dict(raw.get("universe") or {})
    uni["split_enabled"] = False
    raw["universe"] = uni
    wqe = dict(raw.get("watchlist_quality") or {})
    wqe["mode"] = "off"
    raw["watchlist_quality"] = wqe
    return BotConfig(raw)


def _order() -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="BANK/USDT",
        price=1.0,
        amount=0,
        usdt_amount=500,
        signal="BUY",
        source="entry_sensor_15m",
        entry_15m_vol_ratio=5.5,
    )


@contextmanager
def _new_buy_at_cap(rm: RiskManager, extra=()):
    """Max-open new entry. Cash/daily/eviction are left to the caller."""
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value={"amount": 0}))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=24))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_daily_loss_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "size_mult": 1.0, "regime": "NEUTRAL"},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="prefer"))
        stack.enter_context(
            patch("services.correlated_tier.api.correlated_tier_selloff_active", return_value=False)
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch("services.venue_quality.venue_quality_config", return_value={"enabled": False})
        )
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        for p in extra:
            stack.enter_context(p)
        yield


def _successful_plan():
    return SimpleNamespace(sell_executed=True, veto_reason="")


class TestEvictionAfterBuyGuards:
    def test_daily_buy_limit_rejects_without_eviction(self):
        rm = RiskManager(_cfg())
        hook = MagicMock(return_value=(_successful_plan(), " · evicted"))
        extra = (
            patch.object(rm, "_available_usdt", return_value=50_000.0),
            patch.object(rm, "_daily_buys_count", return_value=2),
            patch(
                "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
                hook,
            ),
        )
        with _new_buy_at_cap(rm, extra=extra):
            decision = rm.evaluate(_order(), "4h", source="entry_sensor_15m")
        assert not decision.approved
        assert decision.code == "max_daily_trades"
        assert "Daily buy limit" in (decision.message or "")
        hook.assert_not_called()

    def test_cash_floor_rejects_without_eviction(self):
        rm = RiskManager(_cfg())
        hook = MagicMock(return_value=(_successful_plan(), " · evicted"))
        extra = (
            patch.object(rm, "_available_usdt", return_value=0.0),
            patch.object(rm, "_daily_buys_count", return_value=0),
            patch(
                "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
                hook,
            ),
        )
        with _new_buy_at_cap(rm, extra=extra):
            decision = rm.evaluate(_order(), "4h", source="entry_sensor_15m")
        assert not decision.approved
        assert decision.code == "cash_floor"
        hook.assert_not_called()

    def test_max_open_attempts_eviction_and_approves_on_success(self):
        rm = RiskManager(_cfg())
        hook = MagicMock(return_value=(_successful_plan(), " · evicted"))
        extra = (
            patch("risk.risk_manager.count_open_full_slots", side_effect=[24, 23]),
            patch.object(rm, "_available_usdt", return_value=50_000.0),
            patch.object(rm, "_daily_buys_count", return_value=0),
            patch(
                "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
                hook,
            ),
        )
        with _new_buy_at_cap(rm, extra=extra):
            decision = rm.evaluate(_order(), "4h", source="entry_sensor_15m")
        assert decision.approved, f"{decision.code}: {decision.message}"
        hook.assert_called_once()

    def test_eviction_no_plan_keeps_max_open_rejection(self):
        rm = RiskManager(_cfg())
        hook = MagicMock(return_value=(None, ""))
        extra = (
            patch.object(rm, "_available_usdt", return_value=50_000.0),
            patch.object(rm, "_daily_buys_count", return_value=0),
            patch(
                "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
                hook,
            ),
        )
        with _new_buy_at_cap(rm, extra=extra):
            decision = rm.evaluate(_order(), "4h", source="entry_sensor_15m")
        assert not decision.approved
        assert decision.code == "max_open_positions"
        hook.assert_called_once()
