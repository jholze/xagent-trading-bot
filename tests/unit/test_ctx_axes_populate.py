"""#345 slice 2: populate ctx axes on auto-cycle TradeOrders (additive, write-only)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.config import BotConfig
from core.models import SignalAnalysis, TradeOrder, TradeResult
from risk.risk_manager import RiskManager
from services.signal_orchestrator import SignalOrchestrator
from strategies.ctx_axes import compute_volume_rel, compute_volume_rel_window, read_oracle_state
from strategies.positions import update_position

_CTX = {
    "ctx_oracle_state": "RISK_ON",
    "ctx_coin_regime": "RANGING",
    "ctx_volume_rel": 1.37,
}


def _vol_frame(n: int, last_k: int, last_vol: float, rest_vol: float) -> pd.DataFrame:
    vols = [rest_vol] * (n - last_k) + [last_vol] * last_k
    return pd.DataFrame({"volume": vols})


# ---------------------------------------------------------------------------
# a. compute_volume_rel
# ---------------------------------------------------------------------------


def test_compute_volume_rel_4h_30d_exact_ratio():
    # 4h → 6 bars/day × 30 = 180. Last 6 (24h) vol=20, rest vol=10.
    # mean_24h=20, mean_30d=(174*10 + 6*20)/180 = 10.333… → 1.9355
    df = _vol_frame(180, 6, 20.0, 10.0)
    assert compute_volume_rel(df, "4h") == 1.9355


def test_compute_volume_rel_1h_30d_same_ratio_as_4h():
    # 1h → 24 bars/day × 30 = 720. Same 24h-vs-30d volume pattern as 4h.
    df = _vol_frame(720, 24, 20.0, 10.0)
    assert compute_volume_rel(df, "1h") == 1.9355


def test_compute_volume_rel_7d_uses_whole_frame_denominator():
    # 4h × 7 days = 42 bars (< 30d, ≥ 7d). last 6=20, rest=10
    # 20 / ((36*10 + 6*20)/42) = 20 / (480/42) = 1.75
    df = _vol_frame(42, 6, 20.0, 10.0)
    assert compute_volume_rel(df, "4h") == 1.75


def test_compute_volume_rel_under_7_days_is_none():
    df = _vol_frame(41, 6, 20.0, 10.0)  # 4h: 41/6 < 7 days
    assert compute_volume_rel(df, "4h") is None


def test_compute_volume_rel_none_and_empty_df():
    assert compute_volume_rel(None, "4h") is None
    assert compute_volume_rel(pd.DataFrame(), "4h") is None
    assert compute_volume_rel(pd.DataFrame({"volume": []}), "4h") is None


def test_compute_volume_rel_missing_volume_column():
    df = pd.DataFrame({"close": [1.0] * 180})
    assert compute_volume_rel(df, "4h") is None


def test_compute_volume_rel_zero_denominator():
    df = pd.DataFrame({"volume": [0.0] * 180})
    assert compute_volume_rel(df, "4h") is None


def test_compute_volume_rel_nan_denominator():
    df = pd.DataFrame({"volume": [float("nan")] * 180})
    assert compute_volume_rel(df, "4h") is None


def test_compute_volume_rel_1h_vs_4h_bars_per_day():
    # 42 bars = 7 days on 4h, 1.75 days on 1h.
    df = _vol_frame(42, 6, 20.0, 10.0)
    assert compute_volume_rel(df, "4h") == 1.75
    assert compute_volume_rel(df, "1h") is None


def test_compute_volume_rel_unknown_timeframe_none():
    df = _vol_frame(180, 6, 20.0, 10.0)
    assert compute_volume_rel(df, "5m") is None


def test_compute_volume_rel_2h_now_supported():
    # 2h was missing from ctx_axes._BARS_PER_DAY; _24H_BARS has 12 bars/day.
    # 7d = 84 bars. last 12=20, rest=10 → 20 / ((72*10 + 12*20)/84) = 1.75
    df = _vol_frame(84, 12, 20.0, 10.0)
    assert compute_volume_rel(df, "2h") == 1.75
    assert compute_volume_rel(df, "5m") is None


def test_compute_volume_rel_window_4h_30d_reports_30():
    df = _vol_frame(180, 6, 20.0, 10.0)
    rel, days = compute_volume_rel_window(df, "4h")
    assert rel == 1.9355
    assert days == 30.0


def test_compute_volume_rel_window_1h_300_bars_is_12_5d():
    # Live 300-bar 1h frame: 300/24 = 12.5d whole-frame fallback (< 30d).
    df = _vol_frame(300, 24, 20.0, 10.0)
    rel, days = compute_volume_rel_window(df, "1h")
    assert rel is not None
    assert days == 12.5


def test_compute_volume_rel_window_4h_42_bars_is_7d():
    df = _vol_frame(42, 6, 20.0, 10.0)
    rel, days = compute_volume_rel_window(df, "4h")
    assert rel == 1.75
    assert days == 7.0


def test_compute_volume_rel_window_under_7_days_is_none_none():
    df = _vol_frame(41, 6, 20.0, 10.0)
    assert compute_volume_rel_window(df, "4h") == (None, None)


def test_compute_volume_rel_matches_window_tuple_first_element():
    frames = [
        (_vol_frame(180, 6, 20.0, 10.0), "4h"),
        (_vol_frame(720, 24, 20.0, 10.0), "1h"),
        (_vol_frame(42, 6, 20.0, 10.0), "4h"),
        (_vol_frame(41, 6, 20.0, 10.0), "4h"),
        (_vol_frame(300, 24, 20.0, 10.0), "1h"),
        (_vol_frame(84, 12, 20.0, 10.0), "2h"),
        (pd.DataFrame(), "4h"),
    ]
    for df, tf in frames:
        assert compute_volume_rel(df, tf) == compute_volume_rel_window(df, tf)[0]
    assert compute_volume_rel(None, "4h") == compute_volume_rel_window(None, "4h")[0]


# ---------------------------------------------------------------------------
# b. read_oracle_state
# ---------------------------------------------------------------------------


def test_read_oracle_state_uppercases_state():
    with patch(
        "services.market_oracle.store.get_latest_snapshot",
        return_value={"state": "risk_off"},
    ):
        assert read_oracle_state() == "RISK_OFF"


def test_read_oracle_state_regime_fallback():
    with patch(
        "services.market_oracle.store.get_latest_snapshot",
        return_value={"regime": "crash"},
    ):
        assert read_oracle_state() == "CRASH"


def test_read_oracle_state_none_snapshot():
    with patch(
        "services.market_oracle.store.get_latest_snapshot",
        return_value=None,
    ):
        assert read_oracle_state() is None


def test_read_oracle_state_empty_state_is_none():
    with patch(
        "services.market_oracle.store.get_latest_snapshot",
        return_value={"state": "", "regime": ""},
    ):
        assert read_oracle_state() is None


def test_read_oracle_state_raising_returns_none():
    with patch(
        "services.market_oracle.store.get_latest_snapshot",
        side_effect=RuntimeError("redis down"),
    ):
        assert read_oracle_state() is None


# ---------------------------------------------------------------------------
# c. risk_manager passthrough
# ---------------------------------------------------------------------------


def _risk_cfg() -> BotConfig:
    raw = {
        "max_usdt_per_trade": 1000,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "update_interval": 120,
        "paper": {"initial_capital_usdt": 100_000},
        "aggression": {"max_position_multiplier": 2.0},
        "risk": {
            "min_trade_usdt": 1,
            "min_size_multiplier": 0.25,
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
    }
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


@contextmanager
def _size_env(rm: RiskManager):
    hist = {
        "virtual_balance": 80_000.0,
        "peak_equity": 100_000.0,
        "trades": [],
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"active": False, "apply_size_mult": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_size_bias", return_value=1.0))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
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
        stack.enter_context(patch.object(rm.market, "fetch_indicators", return_value={"atr_pct": 3.0}))
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(
            patch("risk.risk_manager.get_position", return_value={"amount": 0, "sold_percent": 0})
        )
        stack.enter_context(patch("risk.risk_manager.find_open_position_for_symbol", return_value=None))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch("risk.risk_manager.load_trade_history", return_value=hist))
        stack.enter_context(patch("risk.risk_manager.load_live_trade_history", return_value=hist))
        yield


def _buy_with_ctx(**extra) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100.0,
        signal="BUY",
        source="auto",
        timestamp="2026-01-01T00:00:00",
        ctx_oracle_state=_CTX["ctx_oracle_state"],
        ctx_coin_regime=_CTX["ctx_coin_regime"],
        ctx_volume_rel=_CTX["ctx_volume_rel"],
        **extra,
    )


def test_risk_evaluate_approved_buy_copies_ctx_fields():
    rm = RiskManager(_risk_cfg())
    order = _buy_with_ctx()
    with _size_env(rm):
        decision = rm.evaluate(
            order, "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
        )
    assert decision.approved, decision.message
    assert decision.order is not order
    assert decision.order.ctx_oracle_state == "RISK_ON"
    assert decision.order.ctx_coin_regime == "RANGING"
    assert decision.order.ctx_volume_rel == pytest.approx(1.37)


def test_risk_evaluate_approved_buy_copies_ctx_volume_window_days():
    rm = RiskManager(_risk_cfg())
    order = _buy_with_ctx(ctx_volume_window_days=12.5)
    with _size_env(rm):
        decision = rm.evaluate(
            order, "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
        )
    assert decision.approved, decision.message
    assert decision.order is not order
    assert decision.order.ctx_oracle_state == "RISK_ON"
    assert decision.order.ctx_coin_regime == "RANGING"
    assert decision.order.ctx_volume_rel == pytest.approx(1.37)
    assert decision.order.ctx_volume_window_days == pytest.approx(12.5)


def test_fill_sell_amount_from_open_lot_copies_ctx_fields():
    rm = RiskManager(_risk_cfg())
    order = TradeOrder(
        type="SELL",
        symbol="NEAR/USDT",
        price=1.0,
        amount=0,
        signal="SELL_PARTIAL_20",
        source="auto",
        ctx_oracle_state="NEUTRAL",
        ctx_coin_regime="CHOPPY_HIGH_VOL",
        ctx_volume_rel=0.42,
    )
    pos = {"amount": 100.0, "side": "long"}
    with patch(
        "risk.risk_manager.find_open_position_for_symbol",
        return_value=("4h", pos),
    ), patch("risk.risk_manager.sell_fraction_for_signal", return_value=0.5):
        out = rm._fill_sell_amount_from_open_lot(order, "4h")
    assert out is not order
    assert out.amount == pytest.approx(50.0)
    assert out.ctx_oracle_state == "NEUTRAL"
    assert out.ctx_coin_regime == "CHOPPY_HIGH_VOL"
    assert out.ctx_volume_rel == pytest.approx(0.42)


def test_resolve_sell_order_dust_sweep_copies_ctx_fields():
    rm = RiskManager(_risk_cfg())
    order = TradeOrder(
        type="SELL",
        symbol="HIGH/USDT",
        price=0.05,
        amount=60.0,
        signal="SELL_PARTIAL_20",
        ctx_oracle_state="RISK_OFF",
        ctx_coin_regime="STRONG_DOWNTREND",
        ctx_volume_rel=2.5,
    )
    with patch(
        "risk.risk_manager.get_position",
        return_value={"amount": 100.0, "average_entry": 0.05, "sold_percent": 0.97},
    ), patch(
        "strategies.sell_rotation_policy.can_rotation_evict",
        return_value=True,
    ):
        resolved = rm._resolve_sell_order(order, "4h", source="auto")
    assert resolved.signal == "SELL_FULL"
    assert resolved.amount == 100.0
    assert resolved is not order
    assert resolved.ctx_oracle_state == "RISK_OFF"
    assert resolved.ctx_coin_regime == "STRONG_DOWNTREND"
    assert resolved.ctx_volume_rel == pytest.approx(2.5)


def test_cover_evaluate_copies_ctx_fields():
    update_position("EEE/USDT", "4h", "SHORT", 1.0, 8, leverage=2)
    rm = RiskManager(_risk_cfg())
    disabled = {"shorts": {"enabled": False, "allow_live": False}}
    order = TradeOrder(
        type="COVER",
        symbol="EEE/USDT",
        price=0.9,
        amount=8,
        ctx_oracle_state="CRASH",
        ctx_coin_regime="TRANSITION",
        ctx_volume_rel=0.11,
    )
    with patch.object(rm.config, "_raw", disabled), patch(
        "core.simulated_trading.is_real_live_trading", return_value=False
    ):
        dec = rm.evaluate(order, "4h", source="manual")
    assert dec.approved, dec.message
    assert dec.order is not order
    assert dec.order.ctx_oracle_state == "CRASH"
    assert dec.order.ctx_coin_regime == "TRANSITION"
    assert dec.order.ctx_volume_rel == pytest.approx(0.11)


def test_short_evaluate_copies_ctx_fields():
    rm = RiskManager(_risk_cfg())
    enabled = {
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "leverage_default": 2,
            "leverage_cap": 5,
            "max_open": 6,
            "max_margin_pct": 80,
            "volatile": {"market_cap_min_usd": 0},
        }
    }
    order = TradeOrder(
        type="SHORT",
        symbol="SHCTX/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100,
        ctx_oracle_state="RISK_ON",
        ctx_coin_regime="RANGING",
        ctx_volume_rel=1.37,
        ctx_volume_window_days=12.5,
    )
    with patch.object(rm.config, "_raw", enabled), patch(
        "core.simulated_trading.is_real_live_trading", return_value=False
    ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
        rm, "_portfolio_equity", return_value=10_000
    ):
        dec = rm.evaluate(order, "4h", source="manual")
    assert dec.approved, dec.message
    assert dec.order is not order
    assert dec.order.type == "SHORT"
    assert dec.order.ctx_oracle_state == "RISK_ON"
    assert dec.order.ctx_coin_regime == "RANGING"
    assert dec.order.ctx_volume_rel == pytest.approx(1.37)
    assert dec.order.ctx_volume_window_days == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# d. signal_orchestrator stamps ctx from analysis
# ---------------------------------------------------------------------------


def _buy_analysis() -> SignalAnalysis:
    return SignalAnalysis(
        action="BUY",
        symbol="CTX/USDT",
        timeframe="4h",
        rsi=40.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical"],
        normalized_action="BUY",
        recommended=True,
        regime="RANGING",
        ctx_oracle_state="RISK_ON",
        ctx_volume_rel=1.37,
    )


def test_orchestrator_buy_order_carries_ctx_from_analysis():
    trading = MagicMock()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "BUY", order.symbol, amount=1, price=1.0)

    trading.execute_order.side_effect = _exec
    trading.refresh = MagicMock()
    orch = SignalOrchestrator()
    orch.trading = trading

    with patch(
        "services.signal_orchestrator.get_position",
        return_value={"amount": 0},
    ), patch(
        "services.signal_orchestrator.resolve_coin_config",
        return_value={"strategy_params": {}},
    ):
        orch.execute_if_needed(
            _buy_analysis(),
            coin={"symbol": "CTX/USDT", "timeframe": "4h"},
            current_price=1.0,
        )

    order = captured.get("order")
    assert order is not None
    assert order.type == "BUY"
    assert order.ctx_oracle_state == "RISK_ON"
    assert order.ctx_coin_regime == "RANGING"
    assert order.ctx_volume_rel == pytest.approx(1.37)


def test_orchestrator_sell_order_carries_ctx_from_analysis():
    trading = MagicMock()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "SELL", order.symbol, amount=1, price=1.0)

    trading.execute_order.side_effect = _exec
    trading.refresh = MagicMock()
    orch = SignalOrchestrator()
    orch.trading = trading
    analysis = SignalAnalysis(
        action="SELL_30",
        symbol="LAB/USDT",
        timeframe="4h",
        rsi=70.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["time_profit_exit", "technical"],
        normalized_action="SELL_PARTIAL_50",
        rationale="Time->profit exit",
        sell_source="time_profit_exit",
        recommended=True,
        regime="RANGING",
        ctx_oracle_state="RISK_ON",
        ctx_volume_rel=1.37,
    )
    with patch(
        "services.signal_orchestrator.find_open_position_for_symbol",
        return_value=("4h", {"amount": 100.0}),
    ), patch(
        "services.signal_orchestrator.get_position",
        return_value={"amount": 100.0, "side": "long"},
    ), patch(
        "services.signal_orchestrator.resolve_coin_config",
        return_value={"strategy_params": {}},
    ), patch(
        "strategies.positions.sell_fraction_for_signal",
        return_value=0.5,
    ):
        orch.execute_if_needed(
            analysis,
            coin={"symbol": "LAB/USDT", "timeframe": "4h"},
            current_price=0.15,
        )

    order = captured.get("order")
    assert order is not None
    assert order.type == "SELL"
    assert order.ctx_oracle_state == "RISK_ON"
    assert order.ctx_coin_regime == "RANGING"
    assert order.ctx_volume_rel == pytest.approx(1.37)


# ---------------------------------------------------------------------------
# e. ctx fields are not a risk decision input
# ---------------------------------------------------------------------------


def test_ctx_fields_do_not_change_risk_decision():
    """Two otherwise-identical orders differing only in ctx fields → same decision.

    Picked this behavioral check over a source-grep: sizing/approve/code/message
    must match even when the diagnostic axes differ.
    """
    rm = RiskManager(_risk_cfg())
    plain = TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100.0,
        signal="BUY",
        source="auto",
        timestamp="2026-01-01T00:00:00",
    )
    tagged = _buy_with_ctx()
    with _size_env(rm):
        a = rm.evaluate(plain, "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0})
        b = rm.evaluate(tagged, "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0})
    assert a.approved and b.approved
    assert a.message == b.message
    assert a.code == b.code
    assert a.order.usdt_amount == pytest.approx(b.order.usdt_amount)
    assert a.size_multiplier == pytest.approx(b.size_multiplier)
    assert b.order.ctx_oracle_state == "RISK_ON"
    assert a.order.ctx_oracle_state is None
