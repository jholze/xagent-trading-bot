#!/usr/bin/env python3
"""Gating exercise: 15m sensor via entry_sensor_loop._poll_once → real RiskManager."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.actions import BUY
from core.config import BotConfig
from services.signal_orchestrator import SignalOrchestrator
from strategies.entry_sensor_15m import ENTRY_SENSOR_SOURCE, clear_pending_for_tests
from strategies import watch_15m_state

SPIKE_METRICS = {
    "volume_spike_ratio": 2.8,
    "body_atr_ratio": 0.55,
    "price_momentum": True,
}

INDICATORS = {
    "rsi": 42.0,
    "lower_bb": 0.95,
    "middle_bb": 1.0,
    "upper_bb": 1.05,
    "vol_multiplier": 1.0,
    "atr": 0.03,
    "atr_pct": 3.0,
}

COIN = {
    "symbol": "XENTRY15/USDT",
    "timeframe": "4h",
    "active": True,
    "strategy_params": {
        "rsi_buy_low": 25,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.5,
        "stop_loss_pct": 50,
    },
}


def _isolate_ledger(tmp: Path):
    import data_manager
    import storage.ledger_router as ledger_router

    orders = str(tmp / "orders.demo.json")
    positions = str(tmp / "positions.demo.json")
    history = str(tmp / "live_trade_history.demo.json")
    for path, payload in (
        (orders, {"ledger_scope": "demo", "orders": [], "migrated_from_trades": False}),
        (positions, {"ledger_scope": "demo", "positions": {}}),
        (history, {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}),
    ):
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    orders_files = dict(data_manager.ORDERS_SCOPE_FILES)
    orders_files["demo"] = orders
    positions_files = dict(data_manager.POSITIONS_SCOPE_FILES)
    positions_files["demo"] = positions
    data_manager.ORDERS_SCOPE_FILES = orders_files
    data_manager.POSITIONS_SCOPE_FILES = positions_files
    ledger_router.ORDERS_SCOPE_FILES = orders_files
    ledger_router.POSITIONS_SCOPE_FILES = positions_files
    data_manager.get_data_file = lambda base: {
        "live_trade_history.demo.json": history,
    }.get(base, str(tmp / base))
    os.environ["DEMO_MODE"] = "1"


def _install_paper_harness():
    import data_manager

    cfg = copy.deepcopy(data_manager.load_config())
    cfg["trading_mode"] = "paper"
    cfg["virtual_trading"] = True
    cfg["initial_capital_usdt"] = 5000
    cfg["max_usdt_per_trade"] = 200
    cfg.setdefault("paper", {})["initial_capital_usdt"] = 5000
    cfg["paper"]["backend"] = "local"
    cfg.setdefault("architecture", {})["ledger_backend"] = "local"
    cfg.setdefault("architecture", {})["ledger_dual_write"] = False
    cfg.setdefault("volatile_altcoin", {})["mode"] = "active"
    cfg["entry_sensor_15m"] = {
        "enabled": True,
        "mode": "active",
        "vol_spike_mult": 2.0,
        "block_buy_if_rsi_4h_above": 75,
        "fakeout_min_body_atr_ratio": 0.01,
        "cooldown_after_reject_hours": 2,
        "require_ema_breakout": False,
        "poll_interval_sec": 20,
        "max_watched_coins": 15,
        "min_poll_gap_sec_per_coin": 20,
        "setup_modes": ["buy_signal", "setup_zone", "trending"],
        "watch_ttl_hours": 24,
    }
    data_manager._config_cache = cfg
    data_manager.get_config = lambda: cfg
    data_manager.reload_config = lambda: cfg

    import core.config as core_config

    core_config.get_bot_config = lambda: BotConfig(raw=copy.deepcopy(cfg))
    return BotConfig(raw=copy.deepcopy(cfg))


class _LoopMarket:
    """Deterministic 15m spike + live 4h indicators for loop exercise."""

    def fetch_ohlcv(self, symbol, timeframe, limit):
        from tests.unit.test_market_service_15m import _sample_15m_df

        return _sample_15m_df(30, spike_last=True)

    def compute_15m_sensor_metrics(self, df, **kwargs):
        from services.market_service import MarketService

        return MarketService.compute_15m_sensor_metrics(df, **kwargs)

    def fetch_indicators(self, symbol, timeframe, price):
        return INDICATORS

    def fetch_funding_rate(self, symbol):
        return None

    def fetch_15m_sensor_metrics(self, symbol, cfg):
        return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-15m-"))
    _isolate_ledger(tmp)
    bot = _install_paper_harness()
    print(f"trading_mode={bot.trading_mode} virtual_trading={bot.virtual_trading} ledger_tmp={tmp}")

    from services import entry_sensor_loop

    clear_pending_for_tests()
    watch_15m_state.reset_cache_for_tests()
    watch_path = str(tmp / "watch_15m_state.demo.json")
    watch_15m_state._state_path = lambda: watch_path  # type: ignore[attr-defined]
    watch_15m_state.reset_cache_for_tests()
    entry_sensor_loop.reset_poll_state_for_tests()
    watch_15m_state.set_watch(
        COIN["symbol"],
        COIN["timeframe"],
        reason="setup_zone",
        rsi_4h=42.0,
        tech_buy=False,
    )

    orch = SignalOrchestrator()
    loop_market = _LoopMarket()
    orch.market = loop_market
    orch.decision_engine.market = loop_market

    risk_outcomes = []
    process_outcomes = []
    real_risk_eval = orch.trading.risk.evaluate
    real_process = orch.process_coin

    def _capture_risk(*args, **kwargs):
        decision = real_risk_eval(*args, **kwargs)
        risk_outcomes.append(decision)
        return decision

    def _capture_process(coin, price, **kwargs):
        out = real_process(coin, price, **kwargs)
        process_outcomes.append(out)
        return out

    orch.process_coin = _capture_process

    with patch.object(entry_sensor_loop, "get_prices_batch", lambda symbols: {COIN["symbol"]: 1.0}), patch.object(
        entry_sensor_loop, "_coin_by_symbol", lambda symbol: dict(COIN)
    ), patch.object(orch.trading.risk.market, "fetch_indicators", return_value=INDICATORS), patch.object(
        orch.trading.risk, "evaluate", side_effect=_capture_risk
    ), patch("notifications.telegram_commands.position_display.send_positions_snapshot"):
        print("[loop] poll_once start watched=", watch_15m_state.list_watched())
        entry_sensor_loop._poll_once(orch)

    assert risk_outcomes, "loop._poll_once must reach real RiskManager.evaluate"
    assert process_outcomes, "loop._poll_once must call orchestrator.process_coin"
    risk_decision = risk_outcomes[-1]
    result = process_outcomes[-1]
    print(
        f"[risk] approved={risk_decision.approved} code={risk_decision.code} "
        f"message={risk_decision.message}"
    )
    print(
        f"[orchestrator] action={result['action']} executed={result['executed']} "
        f"sources={result['sources']} order_type={result.get('order_type')}"
    )
    assert risk_decision.approved is True
    assert result["action"] == BUY
    assert result["executed"] is True
    assert ENTRY_SENSOR_SOURCE in result["sources"]
    print("[loop-verify-ok] poll_once→HOLD lift→BUY tf=4h real RiskManager approved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())