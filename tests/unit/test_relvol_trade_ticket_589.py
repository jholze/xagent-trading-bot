"""#589: RelVol trade tickets skip the shadow $500 cap; post-mult < $1000 is size_too_small.

No live HTTP. Existing test_size_usdt_participation (missing mode → 500) stays frozen.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.gainer_signal.bot_http import process_gainer_signal
from services.gainer_universe.relvol_shadow import (
    size_usdt_for_signal,
    try_execute_relvol_buy,
)
from strategies.entry_sensor_15m import ENTRY_SENSOR_SOURCE


TRUST_MULT = 0.298
DEXE_MULT = 0.637
RELVOL_FLOOR = 1000.0
SHADOW_TICKET = 500.0


def _size_cfg(**over) -> dict:
    cfg = {
        "participation": 0.02,
        "max_ticket_usdt": 500,
        "min_ticket_usdt": 50,
        "max_pct_of_vol_24h": 0.02,
    }
    cfg.update(over)
    return cfg


def _risk_cfg(*, max_usdt: float = 4500.0, min_trade: float = 100.0) -> BotConfig:
    raw = {
        "max_usdt_per_trade": max_usdt,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "update_interval": 120,
        "paper": {"initial_capital_usdt": 100_000},
        "aggression": {"max_position_multiplier": 2.0},
        "risk": {
            "min_trade_usdt": min_trade,
            "min_size_multiplier": 0.1,
            "drawdown_throttle_pct": 10.0,
            "drawdown_size_multiplier": 0.5,
            "max_daily_loss_pct": 0,
            "cash_policy": {"enabled": False},
            "position_capacity": {"enabled": False},
            "moderate_deploy": {"enabled": False},
            "venue_quality": {"enabled": False},
            "slot_eviction": {"enabled": False},
        },
        "architecture": {},
        "entry_sensor_15m": {
            "ignore_aggression_boost": True,
            "max_usdt_absolute": 1000,
        },
    }
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


def _buy(
    *,
    symbol: str = "AAA/USDT",
    usdt: float = 1000.0,
    source: str = "gainer_relvol",
    signal: str = "GAINER_RELVOL",
    price: float = 1.0,
) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source,
    )


@contextmanager
def _eval_env(rm: RiskManager, *, sized: float, multiplier: float = 1.0):
    hist = {
        "virtual_balance": 80_000.0,
        "peak_equity": 100_000.0,
        "trades": [],
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"active": False, "apply_size_mult": False, "block_buys": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_size_bias", return_value=1.0))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(
            patch(
                "intelligence.macro.snapshot.get_risk_multipliers",
                return_value={
                    "calendar_mult": 1.0,
                    "session_mult": 1.0,
                    "pm_mult": 1.0,
                },
            )
        )
        stack.enter_context(patch("risk.moderate_deploy.size_boost_for_regime", return_value=1.0))
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=80_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=80_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_daily_buys_count", return_value=0))
        stack.enter_context(
            patch.object(rm.market, "fetch_indicators", return_value={"atr_pct": 3.0})
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(
            patch("risk.risk_manager.get_position", return_value={"amount": 0, "sold_percent": 0})
        )
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_partial_sell_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch("strategies.position_lock.auto_sell_blocked", return_value=(False, ""))
        )
        stack.enter_context(patch("risk.risk_manager.load_trade_history", return_value=hist))
        stack.enter_context(patch("risk.risk_manager.load_live_trade_history", return_value=hist))
        stack.enter_context(
            patch(
                "services.correlated_tier.api.correlated_tier_selloff_active",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch.object(
                rm,
                "_dynamic_size",
                return_value=(float(sized), {"total_multiplier": float(multiplier)}),
            )
        )
        yield


def _relvol_trade_config(**over) -> dict:
    block = {
        "enabled": True,
        "mode": "trade",
        "max_open": 4,
        "max_buys_per_day": 8,
        "max_ticket_usdt": 500,
        "min_ticket_usdt": 50,
        "participation": 0.02,
        "require_de_confirm": False,
        "max_pct_24h": 40,
        "max_pct_of_vol_24h": 0.02,
    }
    block.update(over)
    return {
        "gainer_relvol_shadow": block,
        "gainer_entry": {"enabled": False},
        "max_usdt_per_trade": 4500,
    }


class TestSizeUsdtTradeVsShadow:
    def test_trade_mode_ignores_500_cap_still_volume_and_ticket_capped(self):
        cfg = _size_cfg(mode="trade")
        u = size_usdt_for_signal(
            qvol_1h=50_000, abs_vol_24h=1_000_000, cfg=cfg, max_usdt_per_trade=4500
        )
        assert u == 1000.0
        assert u > SHADOW_TICKET

    def test_shadow_and_missing_mode_keep_500_cap(self):
        missing = _size_cfg()
        u_missing = size_usdt_for_signal(
            qvol_1h=50_000, abs_vol_24h=1_000_000, cfg=missing, max_usdt_per_trade=4500
        )
        assert u_missing == SHADOW_TICKET
        u_shadow = size_usdt_for_signal(
            qvol_1h=50_000,
            abs_vol_24h=1_000_000,
            cfg=_size_cfg(mode="shadow"),
            max_usdt_per_trade=4500,
        )
        assert u_shadow == SHADOW_TICKET


class TestRelvolRiskFloor:
    def test_relvol_120_after_multipliers_is_size_too_small_not_rounded_up(self):
        rm = RiskManager(_risk_cfg())
        with _eval_env(rm, sized=120.0, multiplier=DEXE_MULT):
            dec = rm.evaluate(
                _buy(usdt=189.0),
                "1h",
                source="gainer_relvol",
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is False
        assert dec.code == "size_too_small"
        assert dec.order is None
        assert "120" in dec.message
        assert "$1000" in dec.message
        assert "1000.00" not in (dec.message or "")

    def test_relvol_at_1000_or_more_approved_not_500_times_multiplier(self):
        rm = RiskManager(_risk_cfg())
        sized = 1341.0  # e.g. 4500 * 0.298, not 500 * 0.298 = 149
        with _eval_env(rm, sized=sized, multiplier=TRUST_MULT):
            dec = rm.evaluate(
                _buy(usdt=4500.0),
                "1h",
                source="gainer_relvol",
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is True, dec.message
        assert dec.order is not None
        assert dec.order.usdt_amount == pytest.approx(sized)
        assert dec.order.usdt_amount != pytest.approx(SHADOW_TICKET * TRUST_MULT)
        assert dec.order.usdt_amount >= RELVOL_FLOOR

    def test_trust_shaped_no_longer_fills_at_149(self):
        cfg = _size_cfg(mode="trade")
        pre = size_usdt_for_signal(
            qvol_1h=50_000, abs_vol_24h=1_000_000, cfg=cfg, max_usdt_per_trade=4500
        )
        assert pre == 1000.0
        old_fill = round(SHADOW_TICKET * TRUST_MULT, 2)
        assert old_fill == pytest.approx(149.0)
        post = round(pre * TRUST_MULT, 2)
        assert post != old_fill
        rm = RiskManager(_risk_cfg())
        with _eval_env(rm, sized=post, multiplier=TRUST_MULT):
            dec = rm.evaluate(
                _buy(symbol="TRUST/USDT", usdt=pre),
                "1h",
                source="gainer_relvol",
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is False
        assert dec.code == "size_too_small"
        assert dec.order is None

    def test_dexe_shaped_1h_participation_rejected_not_filled_at_120(self):
        cfg = _size_cfg(mode="trade")
        qvol_1h = 189.0 / 0.02  # ~9450 → 2% ≈ $189, under the old $500 cap
        pre = size_usdt_for_signal(
            qvol_1h=qvol_1h, abs_vol_24h=1_000_000, cfg=cfg, max_usdt_per_trade=4500
        )
        assert pre == pytest.approx(189.0)
        post = round(pre * DEXE_MULT, 2)
        assert post == pytest.approx(120.39, abs=0.02)
        rm = RiskManager(_risk_cfg())
        with _eval_env(rm, sized=post, multiplier=DEXE_MULT):
            dec = rm.evaluate(
                _buy(symbol="DEXE/USDT", usdt=pre),
                "1h",
                source="gainer_relvol",
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is False
        assert dec.code == "size_too_small"
        assert dec.order is None
        assert post < RELVOL_FLOOR

    def test_sensor_buy_multiplier_1_still_fills_at_1000(self):
        rm = RiskManager(_risk_cfg())
        with _eval_env(rm, sized=9999.0, multiplier=0.2):
            dec = rm.evaluate(
                _buy(
                    symbol="SEN/USDT",
                    usdt=1000.0,
                    source=ENTRY_SENSOR_SOURCE,
                    signal="BUY",
                ),
                "15m",
                source=ENTRY_SENSOR_SOURCE,
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is True, dec.message
        assert dec.order is not None
        assert dec.order.usdt_amount == pytest.approx(1000.0)
        assert dec.size_multiplier == pytest.approx(1.0)

    def test_non_relvol_buy_at_100_floor_unchanged(self):
        rm = RiskManager(_risk_cfg(min_trade=100.0))
        with _eval_env(rm, sized=100.0, multiplier=1.0):
            dec = rm.evaluate(
                _buy(usdt=100.0, source="auto", signal="BUY"),
                "4h",
                source="auto",
                indicators={"atr_pct": 3.0},
            )
        assert dec.approved is True, dec.message
        assert dec.order is not None
        assert dec.order.usdt_amount == pytest.approx(100.0)
        assert dec.code != "size_too_small"


class TestShadowDoesNotBuy:
    def test_try_execute_relvol_buy_mode_shadow_logs_and_does_not_buy(self):
        out = try_execute_relvol_buy(
            {"symbol": "IGN/USDT", "price": 1.0, "qvol": 80_000, "close": 1.0},
            config={"gainer_relvol_shadow": {"enabled": True, "mode": "shadow"}},
        )
        assert out.get("executed") is False
        assert out.get("message") == "not_trade_mode"

    def test_process_gainer_signal_mode_shadow_does_not_buy(self):
        exec_fn = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "IGN/USDT",
                "last": 1.0,
                "quote_vol": 50_000,
                "qvol_1h": 80_000,
                "pct_24h": 12,
                "source": "gainer_relvol",
                "trigger": "relvol_ws",
            },
            config={
                "gainer_relvol_shadow": {"enabled": True, "mode": "shadow"},
                "max_usdt_per_trade": 4500,
            },
            positions=[],
            gainer_buys_today=0,
            execute_buy=exec_fn,
        )
        assert status == 409
        assert body["message"] == "relvol_disabled"
        assert body.get("executed") is False
        exec_fn.assert_not_called()


class TestHttpTradeSizing:
    def test_trade_mode_http_requests_uncapped_size_not_500(self):
        result = MagicMock(executed=True, message="ok", order_id="rv589")
        exec_fn = MagicMock(return_value=result)
        body, status = process_gainer_signal(
            {
                "symbol": "TRUST/USDT",
                "last": 1.0,
                "quote_vol": 1_000_000,
                "qvol_1h": 50_000,
                "pct_24h": 12,
                "source": "gainer_relvol",
                "trigger": "relvol_ws",
                "factor": 11.0,
            },
            config=_relvol_trade_config(),
            positions=[],
            gainer_buys_today=0,
            execute_buy=exec_fn,
        )
        assert status == 200, body
        exec_fn.assert_called_once()
        usdt = float(exec_fn.call_args.kwargs.get("usdt") or 0)
        assert usdt == pytest.approx(1000.0)
        assert usdt != pytest.approx(SHADOW_TICKET)
        assert usdt != pytest.approx(round(SHADOW_TICKET * TRUST_MULT, 2))
