"""Dead helpers #701 / #702 / #703.

Given/When/Then cases for the unused copies only. Live notional pricing,
replay, and sensor fractions stay as they are. The spec file named in the
review (`spec_bundle_dead_helpers_v2.md`) is not in this repo; these tests
cover the issue acceptance criteria and the named review conditions
(removal, replay once, BUY/manual false, dca_recovery, dca_scheduled,
the two notional precedence cases).
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from core.sim_ledger_replay import _is_dca_order as replay_is_dca_order
from core.sim_ledger_replay import replay_simulated_ledger
from services import ledger_sync
from services.ledger_sync import (
    _build_positions_snapshot_from_orders,
    rebuild_positions_from_orders,
)
from services.order_service import OrderService
from storage import order_ledger_v2
from storage.order_ledger_v2 import order_notional_usdt
from strategies.positions import get_key
from strategies.sensor_entry_policy import resolve_sensor_size_hint
from strategies.trading_modes import (
    MODE_DEFENSIVE,
    MODE_GRID,
    MODE_HYBRID,
    MODE_MOMENTUM,
    entry_sensor_buy_usdt_frac,
    mode_allows_grid_sells,
    mode_allows_new_grid_buys,
)

# Split so a whole-tree search for the deleted symbol stays empty.
_DEAD_NOTIONAL = "_order_" + "notional_usdt"
_DEAD_FULL_SIZE = "mode_allows_" + "entry_sensor_full_size"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}
_TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".txt", ".toml", ".ini"}

# Deployed entry_sensor_15m.size_hint_by_mode. Frozen snapshot; config.json
# is not edited by this change.
_DEPLOYED_SIZE_HINTS = {
    "GRID": {"stable": 0.22, "volatile": 0.35},
    "HYBRID": {"stable": 0.4, "volatile": 0.55},
    "MOMENTUM": {"default": 0.3, "max": 0.35},
}

_SYMBOL = "SNIP/USDT"
_TF = "4h"


def _tree_hits(needle: str) -> list[str]:
    hits: list[str] = []
    for dirpath, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            path = Path(dirpath) / name
            if path.suffix not in _TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if needle in text:
                hits.append(str(path.relative_to(_REPO_ROOT)))
    return hits


def _filled_buy(
    order_id: str,
    *,
    signal: str,
    source: str,
    ts: str,
    price: float = 10.0,
    amount: float = 1.0,
) -> dict:
    return {
        "id": order_id,
        "status": "filled",
        "side": "buy",
        "symbol": _SYMBOL,
        "timeframe": _TF,
        "signal": signal,
        "source": source,
        "execution": {"price": price, "amount": amount, "usdt": price * amount},
        "timestamps": {"filled": ts},
    }


def _position_after(orders: list[dict]) -> dict:
    replayed = replay_simulated_ledger(orders, initial=5000.0)
    return replayed["positions"][get_key(_SYMBOL, _TF)]


# --- #701 -----------------------------------------------------------------


def test_701_order_service_notional_helper_is_gone():
    """Given the unused OrderService notional helper,
    When the tree and OrderService are inspected,
    Then the helper is gone and no shorter-field wrapper replaced it.
    """
    assert not hasattr(OrderService, _DEAD_NOTIONAL)
    assert _DEAD_NOTIONAL not in inspect.getsource(OrderService)
    assert _tree_hits(_DEAD_NOTIONAL) == []


def test_execution_usdt_beats_request_usdt():
    """Given execution.usdt and a different request.usdt,
    When the live notional helper prices the order,
    Then execution.usdt wins.
    """
    order = {"execution": {"usdt": 10}, "request": {"usdt": 99}}
    assert order_notional_usdt(order) == pytest.approx(10.0)


def test_exec_price_amount_beats_usdt_amount():
    """Given an execution price times amount and a top-level usdt_amount,
    When the live notional helper prices the order,
    Then the execution price times amount wins.
    """
    order = {
        "execution": {"price": 2.0, "amount": 5},
        "usdt_amount": 125.5,
    }
    assert order_notional_usdt(order) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "order, expected",
    [
        ({"execution": {"usdt": 10}}, 10.0),
        ({"request": {"usdt": 7}}, 7.0),
        ({"usdt_amount": 125.5}, 125.5),
        ({"usdt_received": 80}, 80.0),
        ({"price": 2.0, "amount": 5}, 10.0),
        ({"usdt_amount": 125.5, "usdt_received": 80}, 125.5),
        ({"execution": {"usdt": 10}, "usdt_amount": 125.5}, 10.0),
        ({"execution": {"usdt": 0}, "request": {"usdt": 4}}, 4.0),
        ({}, 0.0),
        ({"execution": {"usdt": "nope"}}, 0.0),
        ({"price": -2.0, "amount": 5}, 0.0),
    ],
    ids=[
        "execution-usdt",
        "request-usdt",
        "usdt-amount",
        "usdt-received",
        "top-level-price-amount",
        "usdt-amount-beats-usdt-received",
        "execution-usdt-beats-usdt-amount",
        "zero-execution-usdt-falls-through",
        "empty",
        "non-numeric",
        "negative-price",
    ],
)
def test_701_live_notional_fields_stay(order, expected):
    """Given an order notional shape the live helper already prices,
    When order_ledger_v2.order_notional_usdt runs,
    Then the existing field order is unchanged.
    """
    assert order_notional_usdt(order) == pytest.approx(expected)


def test_701_missing_pnl_and_stats_still_call_live_notional():
    """Given the live missing-pnl charge and filled-order stats,
    When their source is read,
    Then both still call order_ledger_v2.order_notional_usdt.
    """
    missing = inspect.getsource(order_ledger_v2._missing_pnl_charge)
    stats = inspect.getsource(order_ledger_v2.stats_from_filled_orders)
    assert "order_notional_usdt(" in missing
    assert "order_notional_usdt(" in stats
    delegated = inspect.getsource(OrderService.stats_from_filled_orders)
    assert "stats_from_filled_orders" in delegated


# --- #702 -----------------------------------------------------------------


def test_702_ledger_sync_dca_helper_is_gone_and_replay_keeps_its_own():
    """Given the unused ledger_sync DCA predicate,
    When ledger_sync and the replay module are inspected,
    Then ledger_sync no longer defines it and replay still has its own copy.
    """
    assert not hasattr(ledger_sync, "_is_dca_order")
    assert "def _is_dca_order" not in inspect.getsource(ledger_sync)
    replay_src = inspect.getsource(replay_is_dca_order)
    assert "dca_sniper" in replay_src
    module_src = inspect.getsource(inspect.getmodule(replay_is_dca_order))
    assert "ledger_sync" not in module_src


def test_702_partial_sell_helper_stays():
    """Given the separate uncalled partial-sell helper,
    When it is left in ledger_sync,
    Then its existing signal check is unchanged.
    """
    assert ledger_sync._is_partial_sell_signal("SELL_PARTIAL") is True
    assert ledger_sync._is_partial_sell_signal("SELL_30") is True
    assert ledger_sync._is_partial_sell_signal("SELL_20") is True
    assert ledger_sync._is_partial_sell_signal("SELL_10") is True
    assert ledger_sync._is_partial_sell_signal("SELL") is False
    assert ledger_sync._is_partial_sell_signal("") is False


@pytest.mark.parametrize(
    "signal, source, expected",
    [
        ("BUY", "dca_sniper", True),
        ("BUY_DCA", "dca_sniper", True),
        ("BUY", "manual", False),
        ("BUY", "dca_recovery", True),
        ("BUY", "dca_scheduled", True),
        ("BUY", "dca", True),
        ("BUY_DCA", "manual", True),
        ("buy", "DCA_SNIPER", True),
        ("SELL", "manual", False),
        ("", "", False),
    ],
    ids=[
        "buy-dca-sniper",
        "buy-dca-signal-and-sniper",
        "buy-manual",
        "dca-recovery",
        "dca-scheduled",
        "dca",
        "buy-dca-signal-manual-source",
        "case-insensitive",
        "sell-manual",
        "empty",
    ],
)
def test_702_replay_dca_predicate(signal, source, expected):
    """Given a signal and source,
    When the live replay DCA predicate runs,
    Then dca_sniper counts, and BUY/manual does not.
    """
    assert replay_is_dca_order({"signal": signal, "source": source}) is expected


def test_702b_order0_opening_buy_dca_sniper_is_a_new_cycle():
    """Given only the opening fill (order 0) with signal BUY and source dca_sniper,
    When the ledger is replayed,
    Then it opens a cycle and does not count as a DCA add.
    """
    pos = _position_after(
        [_filled_buy("order0", signal="BUY", source="dca_sniper", ts="2026-01-01T00:00:00")]
    )
    assert pos["amount"] == pytest.approx(1.0)
    assert int(pos.get("dca_rounds") or 0) == 0
    assert pos["last_action"] == "BUY"


def test_702c_buy_dca_sniper_add_increments_dca_rounds():
    """Given an open lot and a later signal BUY with source dca_sniper,
    When the ledger is replayed,
    Then that add counts as DCA.
    """
    pos = _position_after(
        [
            _filled_buy("order0", signal="BUY", source="manual", ts="2026-01-01T00:00:00"),
            _filled_buy("order1", signal="BUY", source="dca_sniper", ts="2026-01-01T01:00:00"),
        ]
    )
    assert pos["amount"] == pytest.approx(2.0)
    assert int(pos.get("dca_rounds") or 0) == 1
    assert pos["last_action"] == "BUY_DCA"


def test_702d_recovery_and_scheduled_add_manual_does_not():
    """Given an open lot,
    When the next fill is dca_recovery, dca_scheduled, or BUY/manual,
    Then recovery and scheduled count as DCA and BUY/manual does not.
    """
    opening = _filled_buy("order0", signal="BUY", source="manual", ts="2026-01-01T00:00:00")
    for source in ("dca_recovery", "dca_scheduled"):
        pos = _position_after(
            [
                opening,
                _filled_buy(
                    "order1",
                    signal="BUY",
                    source=source,
                    ts="2026-01-01T01:00:00",
                ),
            ]
        )
        assert int(pos.get("dca_rounds") or 0) == 1, source
        assert pos["last_action"] == "BUY_DCA"
    manual = _position_after(
        [
            opening,
            _filled_buy("order1", signal="BUY", source="manual", ts="2026-01-01T01:00:00"),
        ]
    )
    assert int(manual.get("dca_rounds") or 0) == 0
    assert manual["last_action"] == "BUY"


def test_702_rebuild_path_calls_replay_exactly_once(monkeypatch):
    """Given executed and non-executed orders for a scope,
    When the snapshot helper that rebuild_positions_from_orders uses runs,
    Then replay_simulated_ledger is called exactly once with the executed orders.
    """
    filled = {"id": "filled-1", "status": "filled", "symbol": "A/USDT"}
    blocked = {"id": "blocked-1", "status": "blocked", "symbol": "A/USDT"}
    monkeypatch.setattr(
        "data_manager.load_orders",
        lambda scope, tenant_id=None: {"orders": [filled, blocked]},
    )
    monkeypatch.setattr("data_manager.get_config", lambda tenant_id=None: {"marker": True})
    monkeypatch.setattr(
        "core.portfolio_baseline.initial_capital",
        lambda scope, config=None, history=None, trading_mode=None: 4321.0,
    )
    calls = []

    def _replay(orders, initial, config=None, tenant_id=None):
        calls.append(
            {
                "orders": orders,
                "initial": initial,
                "config": config,
                "tenant_id": tenant_id,
            }
        )
        return {"positions": {}}

    monkeypatch.setattr("core.sim_ledger_replay.replay_simulated_ledger", _replay)
    snapshot = _build_positions_snapshot_from_orders("paper", tenant_id="tenant-a")
    assert snapshot == {}
    assert len(calls) == 1
    assert calls[0]["orders"] == [filled]
    assert calls[0]["initial"] == pytest.approx(4321.0)
    assert calls[0]["tenant_id"] == "tenant-a"
    assert calls[0]["config"] == {"marker": True}

    builder_src = inspect.getsource(_build_positions_snapshot_from_orders)
    rebuild_src = inspect.getsource(rebuild_positions_from_orders)
    assert builder_src.count("replay_simulated_ledger(") == 1
    assert rebuild_src.count("_build_positions_snapshot_from_orders(") == 1


def test_ac_002d_writer_lease_blocks_rebuild_without_replay(monkeypatch):
    """Given the writer lease is enabled and this process does not hold it,
    When rebuild_positions_from_orders runs,
    Then it returns 0 and replay_simulated_ledger is not called.
    """
    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: True)
    monkeypatch.setattr("bus.writer_lease.writer_lease_held", lambda: False)
    replay_calls: list[tuple] = []

    def _replay(*args, **kwargs):
        replay_calls.append((args, kwargs))
        return {"positions": {}}

    monkeypatch.setattr("core.sim_ledger_replay.replay_simulated_ledger", _replay)
    # Present only so a bypassed lease check reaches the replay spy.
    monkeypatch.setattr(
        "data_manager.load_orders",
        lambda scope, tenant_id=None: {
            "orders": [
                {
                    "id": "filled-1",
                    "status": "filled",
                    "symbol": "A/USDT",
                    "timeframe": "4h",
                    "side": "buy",
                    "execution": {"price": 10.0, "amount": 1.0, "usdt": 10.0},
                    "timestamps": {"filled": "2026-01-01T00:00:00"},
                }
            ]
        },
    )
    monkeypatch.setattr("data_manager.get_config", lambda tenant_id=None: {})
    monkeypatch.setattr(
        "core.portfolio_baseline.initial_capital",
        lambda scope=None, config=None, history=None, trading_mode=None: 5000.0,
    )
    monkeypatch.setattr(
        "data_manager.load_positions_document",
        lambda scope, tenant_id=None: {"positions": {}},
    )
    monkeypatch.setattr("strategies.positions.flush_positions", lambda *a, **k: None)
    monkeypatch.setattr(
        "strategies.positions.apply_positions_snapshot", lambda *a, **k: None
    )

    result = rebuild_positions_from_orders("paper", tenant_id="tenant-a")
    assert replay_calls == []
    assert result == 0


# --- #703 -----------------------------------------------------------------


def test_703_full_size_symbol_is_gone_from_the_tree():
    """Given the unused full-size sensor helper,
    When the tree is searched,
    Then that symbol is gone.
    """
    assert _tree_hits(_DEAD_FULL_SIZE) == []
    trading_modes_src = inspect.getsource(
        inspect.getmodule(entry_sensor_buy_usdt_frac)
    )
    assert _DEAD_FULL_SIZE not in trading_modes_src


@pytest.mark.parametrize(
    "mode, tier, expected",
    [
        (MODE_GRID, "stable", 0.22),
        (MODE_GRID, "volatile", 0.35),
        (MODE_HYBRID, "stable", 0.40),
        (MODE_HYBRID, "volatile", 0.55),
        (MODE_MOMENTUM, "volatile", 0.30),
        (MODE_MOMENTUM, "stable", 0.30),
        (MODE_MOMENTUM, "", 0.30),
        (MODE_DEFENSIVE, "stable", 0.0),
        (MODE_DEFENSIVE, "volatile", 0.0),
    ],
    ids=[
        "GRID-stable",
        "GRID-volatile",
        "HYBRID-stable",
        "HYBRID-volatile",
        "MOMENTUM-volatile",
        "MOMENTUM-stable",
        "MOMENTUM-default",
        "DEFENSIVE-stable",
        "DEFENSIVE-volatile",
    ],
)
def test_703c_entry_sensor_buy_usdt_frac(mode, tier, expected):
    """Given a trading mode and volatility tier,
    When entry_sensor_buy_usdt_frac runs,
    Then GRID is 0.22/0.35, HYBRID is 0.40/0.55, MOMENTUM is 0.30, and DEFENSIVE is 0.0.
    """
    assert entry_sensor_buy_usdt_frac(mode, volatility_tier=tier) == pytest.approx(expected)


def test_ac_003c_per_tier_config_beats_fallback():
    """Given non-default per-tier size hints,
    When resolve_sensor_size_hint runs,
    Then each tier is exactly that configured value, not the fraction fallback.
    """
    hints = {
        "GRID": {"stable": 0.11, "volatile": 0.33},
        "HYBRID": {"stable": 0.44, "volatile": 0.25},
    }
    cfg = {"size_hint_by_mode": hints}
    cases = (
        (MODE_GRID, "stable", 0.11),
        (MODE_GRID, "volatile", 0.33),
        (MODE_HYBRID, "stable", 0.44),
        (MODE_HYBRID, "volatile", 0.25),
    )
    for mode, tier, expected in cases:
        got = resolve_sensor_size_hint(mode, volatility_tier=tier, cfg=cfg)
        fallback = entry_sensor_buy_usdt_frac(mode, volatility_tier=tier)
        assert got == hints[mode][tier]
        assert got == expected
        assert got != fallback


def test_703_deployed_size_hints_unchanged_and_resolve_matches_fractions():
    """Given the deployed size_hint_by_mode,
    When resolve_sensor_size_hint runs with that map,
    Then it returns the same fractions as entry_sensor_buy_usdt_frac.
    """
    raw = json.loads((_REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    hints = raw["entry_sensor_15m"]["size_hint_by_mode"]
    assert hints == _DEPLOYED_SIZE_HINTS
    cfg = {"size_hint_by_mode": hints}
    cases = [
        (MODE_GRID, "stable", 0.22),
        (MODE_GRID, "volatile", 0.35),
        (MODE_HYBRID, "stable", 0.40),
        (MODE_HYBRID, "volatile", 0.55),
        (MODE_MOMENTUM, "volatile", 0.30),
        (MODE_MOMENTUM, "stable", 0.30),
        (MODE_DEFENSIVE, "volatile", 0.0),
    ]
    for mode, tier, expected in cases:
        got = resolve_sensor_size_hint(mode, volatility_tier=tier, cfg=cfg)
        assert got == pytest.approx(expected)
        assert got == pytest.approx(
            entry_sensor_buy_usdt_frac(mode, volatility_tier=tier)
        )


def test_703_grid_buy_and_sell_helpers_stay():
    """Given the grid buy and sell mode helpers,
    When they are called for each mode,
    Then they are still present and unchanged.
    """
    assert mode_allows_new_grid_buys(MODE_GRID) is True
    assert mode_allows_new_grid_buys(MODE_HYBRID) is True
    assert mode_allows_new_grid_buys(MODE_MOMENTUM) is False
    assert mode_allows_new_grid_buys(MODE_DEFENSIVE) is False
    assert mode_allows_grid_sells(MODE_GRID) is True
    assert mode_allows_grid_sells(MODE_HYBRID) is True
    assert mode_allows_grid_sells(MODE_DEFENSIVE) is True
    assert mode_allows_grid_sells(MODE_MOMENTUM) is False
