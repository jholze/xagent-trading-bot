from unittest.mock import patch

import pytest

from hermes.memory import store
from hermes.symbol_pool import format_active_pool_line, resolve_active_symbols


@pytest.fixture
def hybrid_config(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig

    raw = BotConfig().raw
    raw["hermes"] = {
        **raw.get("hermes", {}),
        "enabled": True,
        "symbols_mode": "hybrid",
        "symbols_pin": ["ARIA/USDT"],
        "symbols_max": 8,
        "symbols_cmc_top_n": 2,
        "symbols_include_positions": True,
        "symbols_refresh_hours": 0,
        "symbols": ["ARIA/USDT", "H/USDT", "XPL/USDT"],
        "timeframes": ["4h"],
        "rotation_hours": 24,
    }
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda: cfg)
    return cfg


def test_static_mode_uses_config_symbols(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig

    raw = BotConfig().raw
    raw["hermes"]["symbols_mode"] = "static"
    raw["hermes"]["symbols"] = ["H/USDT", "XPL/USDT"]
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda: cfg)
    assert resolve_active_symbols(cfg, force_refresh=True) == ["H/USDT", "XPL/USDT"]


def test_hybrid_pool_includes_pin_positions_and_cmc(hybrid_config):
    with patch("hermes.symbol_pool._position_symbols", return_value=["STG/USDT", "H/USDT"]), \
         patch("hermes.symbol_pool._top_cmc_symbols", return_value=["XPL/USDT", "H/USDT"]), \
         patch("hermes.symbol_pool._watchlist_symbols", return_value=["STG/USDT", "H/USDT", "XPL/USDT"]):
        pool = resolve_active_symbols(hybrid_config, ohlcv_check=lambda s, tf: True, force_refresh=True)

    assert "ARIA/USDT" in pool
    assert "STG/USDT" in pool
    assert len(pool) <= 8
    assert pool[0] == "ARIA/USDT"


def test_hybrid_pool_respects_max_and_dedupes(hybrid_config, monkeypatch):
    with patch("hermes.symbol_pool._position_symbols", return_value=["H/USDT"]), \
         patch("hermes.symbol_pool._top_cmc_symbols", return_value=["H/USDT", "XPL/USDT"]), \
         patch("hermes.symbol_pool._watchlist_symbols", return_value=["H/USDT"]):
        hybrid_config.raw["hermes"]["symbols_max"] = 3
        pool = resolve_active_symbols(hybrid_config, ohlcv_check=lambda s, tf: True, force_refresh=True)
    assert pool.count("H/USDT") == 1
    assert len(pool) <= 3


def test_pool_cached_in_baseline_store(hybrid_config, monkeypatch):
    with patch("hermes.symbol_pool._position_symbols", return_value=[]), \
         patch("hermes.symbol_pool._top_cmc_symbols", return_value=[]), \
         patch("hermes.symbol_pool._watchlist_symbols", return_value=[]):
        hybrid_config.raw["hermes"]["symbols_refresh_hours"] = 24
        pool1 = resolve_active_symbols(hybrid_config, ohlcv_check=None, force_refresh=True)
        pool2 = resolve_active_symbols(hybrid_config, ohlcv_check=None, force_refresh=False)
    assert pool1 == pool2
    store_data = store.load_baseline_store()
    assert store_data.get("active_pool", {}).get("symbols") == pool1


def test_format_active_pool_line_static(hybrid_config):
    hybrid_config.raw["hermes"]["symbols_mode"] = "static"
    line = format_active_pool_line(hybrid_config)
    assert "static" in line
    assert "ARIA/USDT" in line