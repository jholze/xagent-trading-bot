import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hermes"


@pytest.fixture(autouse=True)
def demo_mode_env(monkeypatch):
    """Unit tests run with isolated demo JSON paths when touching data files."""
    monkeypatch.setenv("DEMO_MODE", "1")


@pytest.fixture(autouse=True)
def isolate_demo_ledger_files(tmp_path, monkeypatch):
    """Keep unit tests from mutating operator orders.demo.json (XRVM etc.)."""
    import data_manager
    import storage.ledger_router as ledger_router

    orders_path = str(tmp_path / "orders.demo.json")
    positions_path = str(tmp_path / "positions.demo.json")
    history_path = str(tmp_path / "live_trade_history.demo.json")

    for src, dst in (
        (data_manager.ORDERS_SCOPE_FILES.get("demo"), orders_path),
        (data_manager.POSITIONS_SCOPE_FILES.get("demo"), positions_path),
        (data_manager.get_data_file(data_manager.LIVE_TRADE_HISTORY_FILE), history_path),
    ):
        if src and os.path.exists(src):
            shutil.copy2(src, dst)
        elif "orders" in dst:
            Path(dst).write_text(
                json.dumps({"ledger_scope": "demo", "orders": [], "migrated_from_trades": False}),
                encoding="utf-8",
            )
        elif "positions" in dst:
            Path(dst).write_text(
                json.dumps({"ledger_scope": "demo", "positions": {}}),
                encoding="utf-8",
            )
        else:
            Path(dst).write_text(json.dumps({"trades": []}), encoding="utf-8")

    orders_files = dict(data_manager.ORDERS_SCOPE_FILES)
    orders_files["demo"] = orders_path
    positions_files = dict(data_manager.POSITIONS_SCOPE_FILES)
    positions_files["demo"] = positions_path

    monkeypatch.setattr(data_manager, "ORDERS_SCOPE_FILES", orders_files)
    monkeypatch.setattr(data_manager, "POSITIONS_SCOPE_FILES", positions_files)
    monkeypatch.setattr(ledger_router, "ORDERS_SCOPE_FILES", orders_files)
    monkeypatch.setattr(ledger_router, "POSITIONS_SCOPE_FILES", positions_files)
    yield


@pytest.fixture(autouse=True)
def reset_positions_memory(isolate_demo_ledger_files):
    """Prevent in-memory positions dict from leaking across unit tests."""
    from data_manager import resolve_ledger_scope
    from strategies.positions import clear_positions_memory, load_positions

    clear_positions_memory()
    try:
        load_positions(resolve_ledger_scope())
    except Exception:
        pass
    yield
    clear_positions_memory()


@pytest.fixture(autouse=True)
def clear_ledger_caches():
    """Prevent OrderService / resolve_store caches from leaking across tests."""
    from services import order_service
    from storage import ledger_router

    order_service._ORDERS_READ_CACHE.clear()
    ledger_router._store_cache.clear()
    yield
    order_service._ORDERS_READ_CACHE.clear()
    ledger_router._store_cache.clear()


@pytest.fixture(autouse=True)
def normalize_unit_test_config(monkeypatch):
    """Keep unit tests independent of operator-scaled production config.json."""
    import copy

    import data_manager
    from data_manager import load_config

    cfg = copy.deepcopy(load_config())
    cfg["trading_mode"] = "paper"
    cfg["virtual_trading"] = True
    risk = cfg.setdefault("risk", {})
    risk["min_trade_usdt"] = 5.0
    risk["min_sell_notional_usdt"] = 15
    risk["min_position_usdt_for_partial_sell"] = 25
    risk["dca_reserve_pct"] = 0
    risk["dust_sweep_max_position_usdt"] = 15
    cmc = cfg.setdefault("cmc", {})
    cmc["min_sell_notional_usdt"] = 15
    cmc["min_position_usdt_for_social_sell"] = 50
    cfg["initial_capital_usdt"] = 5000
    cfg["max_usdt_per_trade"] = 200
    cfg.setdefault("live", {})["max_usdt_per_trade"] = 200
    cfg.setdefault("paper", {})["initial_capital_usdt"] = 5000
    cfg["paper"]["backend"] = "local"
    cfg.setdefault("aggression", {})["max_position_multiplier"] = 2.0
    arch = cfg.setdefault("architecture", {})
    arch["ledger_backend"] = "local"
    arch["ledger_dual_write"] = False

    def _disable_exit_ladders(node):
        if isinstance(node, dict):
            ladder = node.get("exit_ladder")
            if isinstance(ladder, dict):
                ladder["enabled"] = False
            for value in node.values():
                _disable_exit_ladders(value)
        elif isinstance(node, list):
            for item in node:
                _disable_exit_ladders(item)

    _disable_exit_ladders(cfg)
    cfg.setdefault("volatile_altcoin", {})["mode"] = "active"
    cfg["entry_sensor_15m"] = {
        "enabled": True,
        "mode": "active",
        "timeframe": "15m",
        "poll_interval_sec": 20,
        "vol_spike_mult": 2.0,
        "vol_avg_period": 20,
        "ema_period": 9,
        "require_ema_breakout": False,
        "block_buy_if_rsi_4h_above": 75,
        "fakeout_min_body_atr_ratio": 0.3,
        "cooldown_after_reject_hours": 2,
        "max_watched_coins": 15,
        "min_poll_gap_sec_per_coin": 20,
        "setup_modes": ["buy_signal", "setup_zone", "trending"],
        "watch_ttl_hours": 24,
    }
    data_manager._config_cache = cfg
    orig_get_config = data_manager.get_config
    orig_reload_config = data_manager.reload_config

    def _get_config():
        return cfg

    def _reload_config():
        return cfg

    monkeypatch.setattr(data_manager, "get_config", _get_config)
    monkeypatch.setattr(data_manager, "_config_cache", cfg)
    monkeypatch.setattr(data_manager, "reload_config", _reload_config)

    def _save_config(updated):
        nonlocal cfg
        cfg = copy.deepcopy(updated)
        data_manager._config_cache = cfg
        return True

    monkeypatch.setattr(data_manager, "save_config", _save_config)

    project_root = str(Path(__file__).resolve().parent.parent)
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", "") or ""
        if project_root not in mod_file:
            continue
        if mod.__dict__.get("get_config") is orig_get_config:
            monkeypatch.setattr(mod, "get_config", _get_config)
        if mod.__dict__.get("reload_config") is orig_reload_config:
            monkeypatch.setattr(mod, "reload_config", _reload_config)

    def _bot_config():
        from core.config import BotConfig

        return BotConfig(raw=copy.deepcopy(cfg))

    monkeypatch.setattr("core.config.get_bot_config", _bot_config)
    monkeypatch.setattr("strategies.decision_engine.get_bot_config", _bot_config)


@pytest.fixture(autouse=True)
def telegram_credentials(monkeypatch):
    """Keep Telegram send paths testable after other tests clear env vars."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


@pytest.fixture(autouse=True)
def isolate_bot_logs(tmp_path, monkeypatch):
    """Keep test runs from appending to logs/aria_log.txt while the bot is live."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("logger.LOG_DIR", str(log_dir))
    monkeypatch.setattr("logger.LOG_FILE", str(log_dir / "aria_log.txt"))
    monkeypatch.setattr("logger.JSON_LOG_FILE", str(log_dir / "aria_log.jsonl"))
    monkeypatch.setattr("logger.DECISIONS_LOG_FILE", str(log_dir / "decisions.jsonl"))


@pytest.fixture
def hermes_memory_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    from hermes.memory import store

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def sample_live_trade_history():
    with open(FIXTURES / "live_trade_history.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_positions_live():
    with open(FIXTURES / "positions.live.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_orders_live():
    with open(FIXTURES / "orders.live.sample.json", encoding="utf-8") as f:
        return json.load(f)