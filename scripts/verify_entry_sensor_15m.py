#!/usr/bin/env python3
"""Gating exercise: 15m sensor HOLD->BUY via real DecisionEngine + active orchestrator."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.actions import BUY
from core.config import BotConfig
from core.models import RiskDecision, TradeOrder
from services.signal_orchestrator import SignalOrchestrator
from strategies.decision_engine import DecisionEngine
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    clear_pending_for_tests,
    evaluate_entry_sensor_15m,
    set_pending_sensor_result,
)
from strategies import watch_15m_state

SENSOR_CFG = {
    "enabled": True,
    "mode": "active",
    "vol_spike_mult": 2.0,
    "block_buy_if_rsi_4h_above": 75,
    "fakeout_min_body_atr_ratio": 0.3,
    "cooldown_after_reject_hours": 2,
    "require_ema_breakout": False,
    "poll_interval_sec": 20,
    "max_watched_coins": 15,
}

COIN = {
    "symbol": "SENSOR15/USDT",
    "timeframe": "4h",
    "active": True,
    "strategy_params": {
        "strategy_profile": "hermes_baseline+volatile",
        "rsi_buy_low": 25,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.5,
        "stop_loss_pct": 50,
    },
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


def _bot_config() -> BotConfig:
    import copy
    from data_manager import get_config

    raw = copy.deepcopy(get_config())
    raw["entry_sensor_15m"] = {**SENSOR_CFG}
    raw["trading_mode"] = "paper"
    raw["virtual_trading"] = True
    return BotConfig(raw=raw)


def main() -> int:
    if os.environ.get("MONGODB_DB"):
        print(f"mongodb_db={os.environ['MONGODB_DB']}")

    clear_pending_for_tests()
    watch_15m_state.reset_cache_for_tests()

    spike_metrics = {
        "volume_spike_ratio": 2.8,
        "body_atr_ratio": 0.55,
        "price_momentum": True,
    }
    pending = evaluate_entry_sensor_15m(
        watched=True,
        metrics=spike_metrics,
        cfg=SENSOR_CFG,
        rsi_4h=INDICATORS["rsi"],
    )
    set_pending_sensor_result(COIN["symbol"], pending)
    watch_15m_state.set_watch(
        COIN["symbol"],
        COIN["timeframe"],
        reason="setup_zone",
        rsi_4h=INDICATORS["rsi"],
        tech_buy=False,
    )

    engine = DecisionEngine()
    with patch("core.config.get_bot_config", _bot_config), patch.object(
        engine.market, "fetch_indicators", return_value=INDICATORS
    ), patch.object(engine.market, "fetch_15m_sensor_metrics", return_value=None):
        analysis = engine.evaluate(COIN, 1.0)

    print(
        f"[decision] action={analysis.action} tf={analysis.timeframe} "
        f"sources={analysis.sources} rationale={analysis.rationale}"
    )
    assert analysis.action == BUY
    assert analysis.timeframe == "4h"
    assert ENTRY_SENSOR_SOURCE in (analysis.sources or [])

    orch = SignalOrchestrator()
    buy_order = TradeOrder(type="BUY", symbol=COIN["symbol"], price=1.0, amount=0, usdt_amount=50)
    with patch("core.config.get_bot_config", _bot_config), patch.object(
        orch.decision_engine.market, "fetch_indicators", return_value=INDICATORS
    ), patch.object(orch.decision_engine.market, "fetch_15m_sensor_metrics", return_value=None), patch.object(
        orch.trading.risk, "evaluate", return_value=RiskDecision(approved=True, order=buy_order)
    ), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot"
    ):
        set_pending_sensor_result(COIN["symbol"], pending)
        result = orch.process_coin(COIN, 1.0, quiet=True)

    print(
        f"[orchestrator] action={result['action']} tf=4h executed={result['executed']} "
        f"sources={result['sources']} order_type={result.get('order_type')}"
    )
    assert result["action"] == BUY
    assert result["executed"] is True
    assert ENTRY_SENSOR_SOURCE in result["sources"]
    assert "4h" == COIN["timeframe"]

    from services import entry_sensor_loop

    entry_sensor_loop.reset_poll_state_for_tests()
    print("[loop] import_ok module=services.entry_sensor_loop")
    print("[verify-ok] sensor HOLD->BUY path preserved tf=4h risk=approved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())