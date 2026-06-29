#!/usr/bin/env python3
"""Gating exercise: 15m sensor via real DecisionEngine + RiskManager (paper harness)."""

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
from strategies.decision_engine import DecisionEngine
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    clear_pending_for_tests,
    set_pending_sensor_metrics,
)
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
        "fakeout_min_body_atr_ratio": 0.3,
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-15m-"))
    _isolate_ledger(tmp)
    bot = _install_paper_harness()
    print(f"trading_mode={bot.trading_mode} virtual_trading={bot.virtual_trading} ledger_tmp={tmp}")

    clear_pending_for_tests()
    watch_15m_state.reset_cache_for_tests()
    set_pending_sensor_metrics(COIN["symbol"], SPIKE_METRICS)
    watch_15m_state.set_watch(
        COIN["symbol"],
        COIN["timeframe"],
        reason="setup_zone",
        rsi_4h=INDICATORS["rsi"],
        tech_buy=False,
    )

    engine = DecisionEngine()
    with patch.object(engine.market, "fetch_indicators", return_value=INDICATORS), patch.object(
        engine.market, "fetch_15m_sensor_metrics", return_value=None
    ):
        analysis = engine.evaluate(COIN, 1.0)

    print(
        f"[decision] action={analysis.action} tf={analysis.timeframe} "
        f"sources={analysis.sources} rationale={analysis.rationale}"
    )
    assert analysis.action == BUY
    assert analysis.timeframe == "4h"
    assert ENTRY_SENSOR_SOURCE in (analysis.sources or [])

    orch = SignalOrchestrator()
    set_pending_sensor_metrics(COIN["symbol"], SPIKE_METRICS)
    risk_outcomes = []
    real_risk_eval = orch.trading.risk.evaluate

    def _capture_risk(*args, **kwargs):
        decision = real_risk_eval(*args, **kwargs)
        risk_outcomes.append(decision)
        return decision

    with patch.object(orch.decision_engine.market, "fetch_indicators", return_value=INDICATORS), patch.object(
        orch.decision_engine.market, "fetch_15m_sensor_metrics", return_value=None
    ), patch.object(orch.trading.risk.market, "fetch_indicators", return_value=INDICATORS), patch.object(
        orch.trading.risk, "evaluate", side_effect=_capture_risk
    ), patch("notifications.telegram_commands.position_display.send_positions_snapshot"):
        result = orch.process_coin(COIN, 1.0, quiet=True)

    risk_decision = risk_outcomes[-1]
    print(
        f"[risk] approved={risk_decision.approved} code={risk_decision.code} "
        f"message={risk_decision.message}"
    )
    print(
        f"[orchestrator] action={result['action']} tf=4h executed={result['executed']} "
        f"sources={result['sources']} order_type={result.get('order_type')}"
    )
    assert risk_outcomes
    assert risk_decision.approved is True
    assert result["action"] == BUY
    assert result["executed"] is True
    assert ENTRY_SENSOR_SOURCE in result["sources"]

    from services import entry_sensor_loop

    entry_sensor_loop.reset_poll_state_for_tests()
    print("[loop] import_ok module=services.entry_sensor_loop")
    print("[verify-ok] sensor HOLD->BUY tf=4h real RiskManager approved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())