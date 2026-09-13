"""load/save_live_trade_history must share one scope (#351)."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import (
    _live_trade_history_scope,
    load_live_trade_history,
    record_live_trade,
    record_trade,
    save_live_trade_history,
)


def _cfg(*, trading_mode: str, dry_run: bool = True) -> dict:
    return {
        "trading_mode": trading_mode,
        "live": {
            "dry_run": dry_run,
            "dry_run_enhanced": True,
            "simulated_balance_usdt": 100_000.0,
        },
        "architecture": {"ledger_backend": "local"},
    }


def _empty_history():
    return {
        "trades": [],
        "virtual_balance": 0.0,
        "realized_pnl": 0.0,
        "total_pnl": 0.0,
    }


_FILL = {
    "type": "BUY",
    "symbol": "XPL/USDT",
    "usdt_amount": 100.0,
    "timestamp": "2026-07-15T08:43:51",
}


class _FakeLedger:
    """In-memory trade_history documents keyed by scope (never touches data/)."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.load_scopes: list[str] = []
        self.save_scopes: list[str] = []

    def load(self, scope, *a, **k):
        self.load_scopes.append(scope)
        doc = self.docs.get(scope) or _empty_history()
        return {**doc, "trades": list(doc.get("trades", []))}

    def save(self, data, scope, *a, **k):
        self.save_scopes.append(scope)
        self.docs[scope] = {**data, "trades": list(data.get("trades", []))}
        return True

    def trades(self, scope):
        return (self.docs.get(scope) or {}).get("trades", [])


def test_a_demo_live_dry_run_load_and_save_live(monkeypatch):
    """Railway pin: DEMO_MODE=1, trading_mode=live, live.dry_run=true -> live."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="live", dry_run=True)
    load_scopes: list[str] = []
    save_scopes: list[str] = []

    def fake_load(scope, *a, **k):
        load_scopes.append(scope)
        return _empty_history()

    def fake_save(data, scope, *a, **k):
        save_scopes.append(scope)
        return True

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=fake_load),
        patch("data_manager.save_trade_history_document", side_effect=fake_save),
    ):
        load_live_trade_history()
        save_live_trade_history({"trades": []})

    assert load_scopes == ["live"]
    assert save_scopes == ["live"]


def test_b_demo_paper_load_and_save_demo(monkeypatch):
    """DEMO_MODE=1 + trading_mode=paper: both load and save use demo (was save=live)."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="paper")
    load_scopes: list[str] = []
    save_scopes: list[str] = []

    def fake_load(scope, *a, **k):
        load_scopes.append(scope)
        return _empty_history()

    def fake_save(data, scope, *a, **k):
        save_scopes.append(scope)
        return True

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=fake_load),
        patch("data_manager.save_trade_history_document", side_effect=fake_save),
    ):
        load_live_trade_history()
        save_live_trade_history({"trades": []})

    assert load_scopes == ["demo"]
    assert save_scopes == ["demo"]


def test_c_non_demo_live_scope_is_live(monkeypatch):
    """DEMO_MODE=0 + trading_mode=live: helper and save stay on live (non-demo path)."""
    monkeypatch.setenv("DEMO_MODE", "0")
    cfg = _cfg(trading_mode="live", dry_run=True)
    save_scopes: list[str] = []

    def fake_save(data, scope, *a, **k):
        save_scopes.append(scope)
        return True

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.save_trade_history_document", side_effect=fake_save),
    ):
        assert _live_trade_history_scope() == "live"
        save_live_trade_history({"trades": []})

    assert save_scopes == ["live"]


def test_d_record_live_trade_demo_paper_does_not_double_book(monkeypatch):
    """B1: demo+paper -> record_trade is the writer; record_live_trade adds no row."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="paper")
    ledger = _FakeLedger()

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=ledger.load),
        patch("data_manager.save_trade_history_document", side_effect=ledger.save),
    ):
        record_live_trade(dict(_FILL))

    assert ledger.save_scopes == []
    assert ledger.trades("demo") == []
    assert ledger.trades("live") == []


def test_e_one_fill_demo_paper_books_exactly_one_demo_row(monkeypatch):
    """B1: gate_adapter path under DEMO_MODE=1 + paper fires record_trade
    (sync_virtual, uses_exchange_ledger("paper") is False) AND record_live_trade
    on one fill -> exactly one row in demo trades, none in live."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="paper")
    ledger = _FakeLedger()

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=ledger.load),
        patch("data_manager.save_trade_history_document", side_effect=ledger.save),
    ):
        record_trade(dict(_FILL))
        record_live_trade(dict(_FILL))

    assert len(ledger.trades("demo")) == 1
    assert ledger.trades("live") == []
    assert ledger.save_scopes == ["demo"]


def test_f_demo_live_dry_run_record_live_trade_still_writes_live(monkeypatch):
    """B1 guard: Railway shadow (demo + live + dry_run) -> scopes differ
    (demo vs live), record_trade is not fired, record_live_trade writes live."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="live", dry_run=True)
    ledger = _FakeLedger()

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=ledger.load),
        patch("data_manager.save_trade_history_document", side_effect=ledger.save),
    ):
        record_live_trade(dict(_FILL))

    assert len(ledger.trades("live")) == 1
    assert ledger.trades("demo") == []
    assert ledger.save_scopes == ["live"]


def test_g_non_demo_live_record_live_trade_still_writes_live(monkeypatch):
    """B1 guard: non-demo live has equal scopes ("live" == "live") but
    record_trade is NOT called there (uses_exchange_ledger) -> record_live_trade
    must remain the writer. Skipping here would drop real live rows."""
    monkeypatch.setenv("DEMO_MODE", "0")
    cfg = _cfg(trading_mode="live", dry_run=False)
    ledger = _FakeLedger()

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=ledger.load),
        patch("data_manager.save_trade_history_document", side_effect=ledger.save),
        patch("data_manager._ledger_reads_mongo", return_value=False),
        patch("data_manager._load_live_trade_history_json", return_value=_empty_history()),
        patch("data_manager._reconcile_live_trade_sources", side_effect=lambda h: (h, False)),
        patch("data_manager._ensure_live_virtual_balance", side_effect=lambda h, *a, **k: h),
    ):
        record_live_trade(dict(_FILL))

    assert len(ledger.trades("live")) == 1
    assert ledger.save_scopes == ["live"]


def test_h_demo_live_dry_run_false_stays_live(monkeypatch):
    """B2: DEMO_MODE=1 + trading_mode=live + live.dry_run=false is the cutover
    the operator decides -> helper, load and save all stay "live"."""
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = _cfg(trading_mode="live", dry_run=False)
    ledger = _FakeLedger()

    with (
        patch("data_manager.get_config", return_value=cfg),
        patch("data_manager.load_trade_history_document", side_effect=ledger.load),
        patch("data_manager.save_trade_history_document", side_effect=ledger.save),
    ):
        assert _live_trade_history_scope(cfg) == "live"
        load_live_trade_history()
        save_live_trade_history({"trades": []})

    assert ledger.load_scopes == ["live"]
    assert ledger.save_scopes == ["live"]


def test_i_helper_matrix(monkeypatch):
    """B2 truth table for _live_trade_history_scope."""
    cases = [
        ("1", "live", True, "live"),   # Railway shadow
        ("1", "live", False, "live"),  # cutover, untouched
        ("1", "paper", True, "demo"),  # the load/save split (#351)
        ("1", "paper", False, "demo"),
        ("0", "live", True, "live"),
        ("0", "live", False, "live"),
        ("0", "paper", True, "live"),
    ]
    for demo, mode, dry_run, expected in cases:
        monkeypatch.setenv("DEMO_MODE", demo)
        assert _live_trade_history_scope(_cfg(trading_mode=mode, dry_run=dry_run)) == expected, (
            demo, mode, dry_run,
        )
