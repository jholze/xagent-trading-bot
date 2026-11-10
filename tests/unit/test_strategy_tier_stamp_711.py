"""#711: strategy_tier is stamped at buy time and the long mcap floor uses it.

No network. The mcap oracle and indicators are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from risk.risk_manager import RiskManager
from strategies.entry_tier import (
    classify_entry_tier,
    entry_tier_for_order,
    freeze_on_entry_enabled,
    normalize_tier,
    resolve_entry_tier_live,
)
from strategies.positions import get_position, positions, update_position
from tests.unit.test_long_mcap_venue_563 import _buy, _cfg, _eval_env

_VA = {"enabled": True, "freeze_tier_on_entry": True}


def _va_cfg(**over):
    va = {**_VA, **over}
    return SimpleNamespace(volatile_altcoin_config=va, raw={"volatile_altcoin": va})


@pytest.fixture
def clean_store():
    positions.clear()
    yield
    positions.clear()


# --- entry_tier helpers -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,want",
    [("stable", "stable"), ("VOLATILE", "volatile"), ("", None), (None, None), ("mid", None)],
)
def test_normalize_tier(raw, want):
    assert normalize_tier(raw) == want


def test_classify_without_atr_only_answers_class_cases():
    cfg = _va_cfg()
    assert classify_entry_tier("BTC/USDT", config=cfg) == "stable"
    assert classify_entry_tier("DOGE/USDT", config=cfg) == "volatile"
    assert (
        classify_entry_tier("XYZ/USDT", coin={"market_cap_tier": "micro"}, config=cfg)
        == "volatile"
    )
    # A mid cap needs an ATR. Unknown stays None; it is not guessed as volatile.
    assert classify_entry_tier("XYZ/USDT", config=cfg) is None


def test_classify_with_atr_uses_cycle_classifier():
    cfg = _va_cfg()
    with patch(
        "intelligence.volatility_classifier.volatility_tier", return_value="stable"
    ) as vt:
        assert classify_entry_tier("XYZ/USDT", atr_pct=1.2, config=cfg) == "stable"
    args, kwargs = vt.call_args
    assert args[0]["symbol"] == "XYZ/USDT"
    assert args[1] == 1.2


def test_order_hint_wins_then_class_shortcut():
    cfg = _va_cfg()
    hinted = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, strategy_tier="volatile")
    assert entry_tier_for_order(hinted, config=cfg) == "volatile"
    plain = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0)
    assert entry_tier_for_order(plain, config=cfg) == "stable"
    unknown = TradeOrder(type="BUY", symbol="XYZ/USDT", price=1.0)
    assert entry_tier_for_order(unknown, config=cfg) is None


def test_live_resolve_uses_indicators_and_never_raises():
    cfg = _va_cfg()
    market = SimpleNamespace(
        fetch_ohlcv_and_indicators=lambda s, tf, p: (None, {"atr_pct": 9.0, "change_24h_pct": 30.0})
    )
    with patch("intelligence.volatility_classifier.volatility_tier", return_value="volatile"):
        assert resolve_entry_tier_live("XYZ/USDT", "1h", 1.0, market=market, config=cfg) == "volatile"

    def boom(*a, **k):
        raise RuntimeError("rest down")

    bad = SimpleNamespace(fetch_ohlcv_and_indicators=boom)
    assert resolve_entry_tier_live("XYZ/USDT", "1h", 1.0, market=bad, config=cfg) is None
    no_atr = SimpleNamespace(fetch_ohlcv_and_indicators=lambda s, tf, p: (None, {}))
    assert resolve_entry_tier_live("XYZ/USDT", "1h", 1.0, market=no_atr, config=cfg) is None


def test_freeze_switch_default_on():
    assert freeze_on_entry_enabled(_va_cfg()) is True
    assert freeze_on_entry_enabled(_va_cfg(freeze_tier_on_entry=False)) is False
    assert freeze_on_entry_enabled(SimpleNamespace(volatile_altcoin_config={})) is True


# --- stamp at first fill ------------------------------------------------------


def test_first_fill_stamps_tier_and_dca_does_not_overwrite(clean_store):
    update_position("XYZ/USDT", "1h", "BUY", 1.0, 10, strategy_tier="stable")
    assert get_position("XYZ/USDT", "1h")["strategy_tier"] == "stable"
    update_position(
        "XYZ/USDT", "1h", "BUY_DCA", 0.9, 10, source="dca", strategy_tier="volatile"
    )
    assert get_position("XYZ/USDT", "1h")["strategy_tier"] == "stable"


def test_tier_source_survives_serialize_roundtrip(clean_store):
    from strategies.positions import _deserialize_position, _serialize_positions, get_key

    update_position("RTP/USDT", "1h", "BUY", 1.0, 10, strategy_tier="stable")
    key = get_key("RTP/USDT", "1h")
    raw = _serialize_positions(store={key: positions[key]}, scope="demo")["positions"][key]
    assert raw["strategy_tier_source"] == "entry"
    back = _deserialize_position(raw)
    assert back["strategy_tier"] == "stable" and back["strategy_tier_source"] == "entry"


def test_invalid_or_missing_tier_leaves_lazy_lock_path(clean_store):
    update_position("XYZ/USDT", "1h", "BUY", 1.0, 10, strategy_tier="mid")
    assert not get_position("XYZ/USDT", "1h").get("strategy_tier")
    positions.clear()
    update_position("XYZ/USDT", "1h", "BUY", 1.0, 10)
    assert not get_position("XYZ/USDT", "1h").get("strategy_tier")


def test_new_cycle_after_close_restamps(clean_store):
    update_position("XYZ/USDT", "1h", "BUY", 1.0, 10, strategy_tier="stable")
    update_position("XYZ/USDT", "1h", "SELL", 1.1, 10)
    update_position("XYZ/USDT", "1h", "BUY", 1.0, 10, strategy_tier="volatile")
    assert get_position("XYZ/USDT", "1h")["strategy_tier"] == "volatile"


def test_portfolio_execute_buy_passes_tier_and_honours_freeze_switch(clean_store):
    from services.portfolio_service import PortfolioService, _stampable_tier

    assert _stampable_tier("stable", _va_cfg()) == "stable"
    assert _stampable_tier("stable", _va_cfg(freeze_tier_on_entry=False)) is None
    assert _stampable_tier("bogus", _va_cfg()) is None

    svc = PortfolioService(_cfg())
    with patch("services.portfolio_service.record_trade"), patch(
        "services.portfolio_service.refresh_venue_min_mark"
    ):
        res = svc.execute_buy(
            "XYZ/USDT", "1h", 1.0, 50.0, source="cmc", strategy_tier="stable"
        )
    assert res.executed
    assert get_position("XYZ/USDT", "1h")["strategy_tier"] == "stable"


def test_portfolio_execute_order_forwards_order_tier(clean_store):
    from services.portfolio_service import PortfolioService

    svc = PortfolioService(_cfg())
    order = TradeOrder(
        type="BUY", symbol="XYZ/USDT", price=1.0, usdt_amount=50.0, strategy_tier="volatile"
    )
    with patch("services.portfolio_service.record_trade"), patch(
        "services.portfolio_service.refresh_venue_min_mark"
    ):
        svc.execute_order(order, "1h")
    assert get_position("XYZ/USDT", "1h")["strategy_tier"] == "volatile"


# --- cycle order carries the analysis tier -----------------------------------


def test_analysis_tier_is_read_from_strategy_params():
    from services.signal_orchestrator import _entry_tier_from_analysis

    a = SimpleNamespace(strategy_params={"volatility_tier": "stable"})
    assert _entry_tier_from_analysis(a) == "stable"
    b = SimpleNamespace(strategy_params={}, volatility_tier="volatile")
    assert _entry_tier_from_analysis(b) == "volatile"
    c = SimpleNamespace(strategy_params=None)
    assert _entry_tier_from_analysis(c) is None


# --- long mcap floor ----------------------------------------------------------


def _hinted(tier, symbol="L3/USDT"):
    o = _buy(symbol, source="cmc")
    o.strategy_tier = tier
    return o


def test_stable_entry_gets_20m_floor():
    """Bug repro: before #711 a stable coin at 6M passed the 5M volatile floor."""
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=6_000_000):
        dec = rm.evaluate(_hinted("stable"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert "20000000" in dec.message


def test_volatile_entry_keeps_5m_floor_and_tier_survives_approval():
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=6_000_000):
        dec = rm.evaluate(_hinted("volatile"), "4h", source="cmc")
    assert dec.code != "long_mcap"
    if dec.approved:
        assert dec.order.strategy_tier == "volatile"


def test_unknown_tier_keeps_legacy_volatile_floor():
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=4_900_000):
        dec = rm.evaluate(_hinted(None), "4h", source="cmc")
    assert dec.code == "long_mcap"
    assert "5000000" in dec.message


def test_large_cap_without_hint_uses_class_shortcut():
    rm = RiskManager(_cfg())
    order = _buy("SOL/USDT", source="cmc")
    with _eval_env(rm, mcap=10_000_000):
        dec = rm.evaluate(order, "4h", source="cmc")
    assert dec.code == "long_mcap"
    assert "20000000" in dec.message
    assert order.strategy_tier == "stable"


def test_closed_lot_tier_does_not_override_the_fresh_hint():
    """A closed lot keeps its old tier. The new entry uses the order hint."""
    rm = RiskManager(_cfg())
    closed = {"amount": 0, "open": False, "strategy_tier": "stable"}
    order = _hinted("volatile")
    with _eval_env(rm, position=closed, mcap=6_000_000):
        dec = rm.evaluate(order, "4h", source="cmc")
    assert dec.code != "long_mcap"
    assert order.strategy_tier == "volatile"


def test_closed_lot_tier_is_not_stamped_onto_an_unhinted_reentry():
    rm = RiskManager(_cfg())
    closed = {"amount": 0, "strategy_tier": "stable"}
    order = _buy("XYZ/USDT", source="cmc")
    with _eval_env(rm, position=closed, mcap=6_000_000):
        dec = rm.evaluate(order, "4h", source="cmc")
    assert dec.code != "long_mcap"
    assert normalize_tier(order.strategy_tier) is None


def test_fresh_stable_hint_beats_a_closed_volatile_lot():
    rm = RiskManager(_cfg())
    closed = {"amount": 0, "strategy_tier": "volatile"}
    order = _hinted("stable")
    with _eval_env(rm, position=closed, mcap=6_000_000):
        dec = rm.evaluate(order, "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert "20000000" in dec.message


def test_adapter_fill_carries_strategy_tier(clean_store, monkeypatch):
    from tests.unit.test_gate_adapter_shadow import SYMBOL, _shadow_adapter

    adapter, _captured = _shadow_adapter(monkeypatch)
    order = TradeOrder(
        "BUY", SYMBOL, 100.0, 0, usdt_amount=100.0, strategy_tier="stable"
    )
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    pos = get_position(SYMBOL, "4h")
    assert pos["strategy_tier"] == "stable"
    snap = pos.get("entry_snapshot") or {}
    assert snap.get("strategy_tier") == "stable"


def test_cycle_buy_order_carries_the_analysis_tier():
    from services.signal_orchestrator import SignalOrchestrator

    trading = SimpleNamespace()
    captured: dict = {}

    def _exec(order, *_a, **_k):
        captured["order"] = order
        return SimpleNamespace(executed=True)

    trading.execute_order = _exec
    trading.refresh = lambda: None
    orch = SignalOrchestrator()
    orch.trading = trading
    analysis = SimpleNamespace(
        action="BUY",
        symbol="XYZ/USDT",
        timeframe="4h",
        sources=["technical"],
        normalized_action="BUY",
        x_confidence=None,
        confidence=0.5,
        strategy_params={"volatility_tier": "stable"},
        dca_usdt=50.0,
        exposure_multiplier=None,
        allocation=None,
        ctx_oracle_state=None,
        regime="",
        ctx_volume_rel=None,
        ctx_volume_window_days=None,
    )
    with patch("services.signal_orchestrator.get_position", return_value={}), patch(
        "services.signal_orchestrator.resolve_coin_config",
        return_value={"strategy_params": {}},
    ):
        orch.execute_if_needed(analysis, {"symbol": "XYZ/USDT"}, 1.0)
    assert captured["order"].strategy_tier == "stable"


def test_relvol_buy_sets_tier_from_the_live_classifier(monkeypatch):
    from services.gainer_signal.bot_http import process_gainer_signal

    captured: dict = {}

    def _exec(self, order, *_a, **_k):
        captured["order"] = order
        return SimpleNamespace(executed=True, message="filled")

    monkeypatch.setattr(
        "services.trading_service.TradingService.execute_order", _exec
    )
    monkeypatch.setattr(
        "strategies.entry_tier.resolve_entry_tier_live", lambda *_a, **_k: "volatile"
    )
    body, status = process_gainer_signal(
        {
            "symbol": "AAA/USDT",
            "last": 1.0,
            "quote_vol": 2_000_000,
            "qvol_1h": 50_000,
            "pct_24h": 5,
            "source": "gainer_relvol",
            "eligible": True,
        },
        config={
            "max_usdt_per_trade": 200,
            "gainer_entry": {"enabled": True, "require_de_confirm": False},
            "gainer_relvol_shadow": {
                "enabled": True,
                "mode": "trade",
                "max_open": 4,
                "max_buys_per_day": 8,
                "max_pct_24h": 40,
            },
            "memory": {"enabled": False},
        },
        positions=[],
        gainer_buys_today=0,
    )
    assert status == 200, body
    assert captured["order"].strategy_tier == "volatile"
    assert captured["order"].source == "gainer_relvol"
