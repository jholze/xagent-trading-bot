#!/usr/bin/env python3
"""Bot entry exercise: import aria_bot + one sensor poll cycle with observables."""

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

from core.config import BotConfig
from strategies.entry_sensor_15m import ENTRY_SENSOR_SOURCE, clear_pending_for_tests
from strategies import watch_15m_state

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

INDICATORS = {
    "rsi": 42.0,
    "lower_bb": 0.95,
    "middle_bb": 1.0,
    "upper_bb": 1.05,
    "vol_multiplier": 1.0,
    "atr": 0.03,
    "atr_pct": 3.0,
}


def _install_harness():
    import data_manager

    cfg = copy.deepcopy(data_manager.load_config())
    cfg.setdefault("entry_sensor_15m", {})
    cfg["entry_sensor_15m"]["enabled"] = True
    cfg["entry_sensor_15m"]["mode"] = "active"
    cfg["entry_sensor_15m"]["fakeout_min_body_atr_ratio"] = 0.01
    data_manager._config_cache = cfg
    data_manager.get_config = lambda: cfg
    import core.config as core_config

    core_config.get_bot_config = lambda: BotConfig(raw=copy.deepcopy(cfg))
    return cfg


def main() -> int:
    os.environ["DEMO_MODE"] = "1"
    os.environ["MONGODB_DB"] = "xagent_test"
    tmp = Path(tempfile.mkdtemp(prefix="verify-bot-cycle-"))
    orders = tmp / "orders.demo.json"
    positions = tmp / "positions.demo.json"
    orders.write_text(
        json.dumps({"ledger_scope": "demo", "orders": [], "migrated_from_trades": False}),
        encoding="utf-8",
    )
    positions.write_text(json.dumps({"ledger_scope": "demo", "positions": {}}), encoding="utf-8")

    import data_manager
    import storage.ledger_router as ledger_router

    orders_files = dict(data_manager.ORDERS_SCOPE_FILES)
    orders_files["demo"] = str(orders)
    positions_files = dict(data_manager.POSITIONS_SCOPE_FILES)
    positions_files["demo"] = str(positions)
    data_manager.ORDERS_SCOPE_FILES = orders_files
    data_manager.POSITIONS_SCOPE_FILES = positions_files
    ledger_router.ORDERS_SCOPE_FILES = orders_files
    ledger_router.POSITIONS_SCOPE_FILES = positions_files

    cfg = _install_harness()
    os.environ["MONGODB_DB"] = "xagent_test"
    import aria_bot  # noqa: F401

    print(f"aria_bot import clean, demo={os.environ.get('DEMO_MODE')}")
    print(f"config loaded keys={list(cfg.get('entry_sensor_15m', {}).keys())}")

    from services.signal_orchestrator import SignalOrchestrator
    from services import entry_sensor_loop

    clear_pending_for_tests()
    watch_path = str(tmp / "watch_15m_state.demo.json")
    watch_15m_state._state_path = lambda: watch_path  # type: ignore[attr-defined]
    watch_15m_state.reset_cache_for_tests()
    entry_sensor_loop.reset_poll_state_for_tests()
    watch_15m_state.set_watch(COIN["symbol"], COIN["timeframe"], rsi_4h=42.0)

    class CycleMarket:
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

    orch = SignalOrchestrator()
    cycle_market = CycleMarket()
    orch.market = cycle_market
    orch.decision_engine.market = cycle_market

    poll_result = {}
    real_process = orch.process_coin

    def _capture_process(coin, price, **kwargs):
        out = real_process(coin, price, **kwargs)
        poll_result.update(out)
        return out

    orch.process_coin = _capture_process

    with patch.object(entry_sensor_loop, "get_prices_batch", lambda symbols: {COIN["symbol"]: 1.0}), patch.object(
        entry_sensor_loop, "_coin_by_symbol", lambda symbol: dict(COIN)
    ), patch.object(orch.trading.risk.market, "fetch_indicators", return_value=INDICATORS), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot"
    ):
        print("[cycle] entry_sensor_loop._poll_once")
        entry_sensor_loop._poll_once(orch)

    print(
        f"[cycle] action={poll_result.get('action')} executed={poll_result.get('executed')} "
        f"sources={poll_result.get('sources')} rationale={poll_result.get('rationale', '')[:80]}"
    )
    assert poll_result.get("executed") is True
    assert ENTRY_SENSOR_SOURCE in (poll_result.get("sources") or [])
    print("orchestrator+decision+loop cycle ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())