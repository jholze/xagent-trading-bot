"""#429 — WQE BUY gate, watchlist save, trade-universe ranking honour tenant context.

Before the fix every path collapsed to the ``default`` score file:
``current_tenant_id`` never existed in ``core.tenant_context`` (permanent
ImportError → ``tid = "default"``), ``build_merged_watchlist_coins`` /
``load_trade_universe`` used ``tenant_id or "default"`` instead of
``resolve_tenant_id``, and the store fell back to the default file when a
tenant file was missing. Each test here fails under that collapse.

All WQE files are redirected to ``tmp_path`` via ``WQE_DATA_DIR``; nothing is
written into ``data/`` or ``logs/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.tenant_context import tenant_context
from services.watchlist_quality.runtime import clear_runtime_cache
from services.watchlist_quality.store import (
    load_quality_scores,
    save_quality_scores,
    scores_path,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def wqe_dir(tmp_path, monkeypatch):
    d = tmp_path / "wqe"
    d.mkdir()
    monkeypatch.setenv("WQE_DATA_DIR", str(d))
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.delenv("WATCHLIST_QUALITY_MODE", raising=False)
    clear_runtime_cache()
    yield d
    clear_runtime_cache()


def _row(symbol: str, score: float, tier: str) -> dict:
    return {
        "symbol": symbol,
        "quality_score": score,
        "quality_shadow_ai": score,
        "tier_hint": tier,
        "flags": [],
    }


def _save(tenant_id: str, coins: list[dict]) -> str:
    assert save_quality_scores(
        {"updated_at": "2026-09-15T00:00:00Z", "mode": "enforce", "coins": coins},
        tenant_id=tenant_id,
    )
    return scores_path(tenant_id)


# --------------------------------------------------------------------------- #
# 1. dead import is gone
# --------------------------------------------------------------------------- #


def test_no_dead_current_tenant_id_import_left():
    """``current_tenant_id`` never existed; the import must not be reintroduced."""
    import core.tenant_context as tc

    assert not hasattr(tc, "current_tenant_id")
    for rel in ("risk/risk_manager.py", "services/watchlist_quality/soak_log.py"):
        src = (_REPO / rel).read_text(encoding="utf-8")
        assert "current_tenant_id" not in src, rel
        assert 'tid = "default"' not in src, rel


# --------------------------------------------------------------------------- #
# 2. store: missing tenant file → empty, never default's scores
# --------------------------------------------------------------------------- #


def test_store_missing_tenant_file_returns_empty_not_default(wqe_dir):
    default_path = _save("default", [_row("X/USDT", 0.1, "T3")])
    assert os.path.exists(default_path)
    assert not os.path.exists(scores_path("henry"))

    data = load_quality_scores(tenant_id="henry")
    assert data["coins"] == []
    assert data["tenant_id"] == "henry"
    # default file itself still loads its own row
    assert load_quality_scores(tenant_id="default")["coins"][0]["symbol"] == "X/USDT"


# --------------------------------------------------------------------------- #
# 3. Risk BUY gate reads the context tenant's scores
# --------------------------------------------------------------------------- #


def _thin_risk_manager():
    from risk.risk_manager import RiskManager

    rm = RiskManager(config=MagicMock())
    rm.config.raw = {
        "watchlist_quality": {"mode": "enforce", "min_buy_score": 0.4},
        "risk": {"venue_quality": {"enabled": False}, "cash_floor_pct": 0},
    }
    rm.config.max_open_positions = 50
    rm.config.max_position_percent = 100
    rm.config.max_usdt_per_trade = 500
    rm.config.risk_config = {"min_trade_usdt": 1, "min_size_multiplier": 0.1}
    rm.config.aggression_config = {}
    rm.config.entry_sensor_15m_config = {}
    return rm


def _evaluate_buy(rm, symbol: str):
    from core.models import TradeOrder

    order = TradeOrder(
        type="BUY", symbol=symbol, price=1.0, amount=0, usdt_amount=100,
        signal="BUY", source="ta",
    )
    with patch("risk.risk_manager.get_position", return_value={"amount": 0}), patch(
        "risk.risk_manager.find_open_position_for_symbol", return_value=None
    ), patch(
        "risk.risk_manager.count_open_full_slots", return_value=0
    ), patch(
        "intelligence.memory.cache.get_entry_bias", return_value=None
    ), patch.object(rm, "_cash_floor_blocked", return_value=None), patch.object(
        rm, "_daily_buy_limit_blocked", return_value=None
    ), patch.object(rm, "_daily_loss_limit_blocked", return_value=None), patch.object(
        rm, "_trade_cooldown_blocked", return_value=(False, "")
    ), patch.object(rm, "_is_dca_buy", return_value=False), patch.object(
        rm, "_dynamic_size", return_value=(100.0, {"total_multiplier": 1.0})
    ), patch.object(rm, "_portfolio_equity", return_value=100_000), patch.object(
        rm, "_available_usdt", return_value=50_000
    ):
        return rm.evaluate(order, "4h", source="ta")


def test_risk_buy_gate_default_t3_row_does_not_block_henry(wqe_dir):
    """Default has X/USDT as T3 (would block); henry scores it T1 → henry BUY passes."""
    _save("default", [_row("X/USDT", 0.1, "T3")])
    _save("henry", [_row("X/USDT", 0.9, "T1")])
    rm = _thin_risk_manager()

    # sanity: without tenant context the default T3 row blocks (gate is reached)
    dec_default = _evaluate_buy(rm, "X/USDT")
    assert dec_default.approved is False
    assert dec_default.code == "watchlist_quality"

    with tenant_context("henry"):
        dec_henry = _evaluate_buy(rm, "X/USDT")
    assert dec_henry.code != "watchlist_quality", dec_henry.message


def test_risk_buy_gate_default_min_buy_score_row_does_not_block_henry(wqe_dir):
    """Default row is T2 but below min_buy_score; henry row is above → not blocked."""
    _save("default", [_row("Y/USDT", 0.2, "T2")])
    _save("henry", [_row("Y/USDT", 0.8, "T2")])
    rm = _thin_risk_manager()

    dec_default = _evaluate_buy(rm, "Y/USDT")
    assert dec_default.code == "watchlist_quality"
    assert "min_buy_score" in dec_default.message

    with tenant_context("henry"):
        dec_henry = _evaluate_buy(rm, "Y/USDT")
    assert dec_henry.code != "watchlist_quality", dec_henry.message


# --------------------------------------------------------------------------- #
# 4. watchlist WQE apply persists to the context tenant's file
# --------------------------------------------------------------------------- #


def test_load_effective_watchlist_persists_to_henry_file(wqe_dir, monkeypatch):
    import data_manager

    default_path = _save("default", [_row("KEEP/USDT", 0.5, "T2")])
    default_bytes = Path(default_path).read_bytes()

    cfg = {"watchlist_quality": {"mode": "soft"}, "universe": {"split_enabled": False}}
    monkeypatch.setattr(data_manager, "load_config", lambda tenant_id=None: cfg)
    monkeypatch.setattr(
        data_manager,
        "load_watchlist",
        lambda tenant_id=None: [{"symbol": "AAA/USDT", "active": True}],
    )
    monkeypatch.setattr(data_manager, "uses_watchlist_expansion", lambda c: False)
    monkeypatch.setattr(data_manager, "is_dry_run_enhanced", lambda c: False)
    monkeypatch.setattr(data_manager, "trending_watchlist_live_enabled", lambda c: False)
    # Gate prune would call the exchange; identity keeps the test offline.
    monkeypatch.setattr(
        data_manager, "prune_watchlist_coins_gate_only", lambda coins, **kw: (list(coins), [])
    )
    monkeypatch.setattr("core.coin_eligibility.filter_watchlist_coins", lambda coins, c: coins)
    monkeypatch.setattr(
        "services.watchlist_quality.runtime.attach_quote_volumes",
        lambda coins, config=None: coins,
    )
    monkeypatch.setattr(
        "services.watchlist_quality.runtime.apply_soft_watchlist",
        lambda coins, **kw: list(coins),
    )
    monkeypatch.setattr(
        "services.watchlist_quality.runtime._open_symbols", lambda *a, **k: set()
    )

    with tenant_context("henry"):
        out = data_manager.load_effective_watchlist()

    assert [c["symbol"] for c in out] == ["AAA/USDT"]
    henry_path = scores_path("henry")
    assert henry_path != default_path
    assert os.path.exists(henry_path), "henry scores not persisted"
    henry = json.loads(Path(henry_path).read_text(encoding="utf-8"))
    assert henry["tenant_id"] == "henry"
    assert [c["symbol"] for c in henry["coins"]] == ["AAA/USDT"]
    assert Path(default_path).read_bytes() == default_bytes, "default file was overwritten"


# --------------------------------------------------------------------------- #
# 5. trade universe ranks from the context tenant's lookup
# --------------------------------------------------------------------------- #


def test_load_trade_universe_ranks_from_context_tenant(wqe_dir, monkeypatch):
    from services.universe.split import load_trade_universe

    _save("default", [_row("A/USDT", 0.1, "T3"), _row("B/USDT", 0.9, "T1")])
    _save("henry", [_row("A/USDT", 0.9, "T1"), _row("B/USDT", 0.1, "T3")])
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])

    cfg = {
        "universe": {
            "split_enabled": True,
            "trade_max_coins": 1,
            "trade_include_open_positions": True,
            "trade_include_base": False,
            "trade_rank_by": "quality_score",
        }
    }
    observe = [{"symbol": "A/USDT"}, {"symbol": "B/USDT"}]

    trade_default = load_trade_universe(
        config=cfg, observe_coins=list(observe), open_symbols=set()
    )
    assert [c["symbol"] for c in trade_default] == ["B/USDT"]

    with tenant_context("henry"):
        trade_henry = load_trade_universe(
            config=cfg, observe_coins=list(observe), open_symbols=set()
        )
    assert [c["symbol"] for c in trade_henry] == ["A/USDT"]


# --------------------------------------------------------------------------- #
# 6. soak log risk_reject resolves tenant from context
# --------------------------------------------------------------------------- #


def test_soak_log_risk_reject_uses_context_tenant(wqe_dir, tmp_path, monkeypatch):
    import services.watchlist_quality.soak_log as soak_log

    rpath = tmp_path / "logs" / "risk_rejects.jsonl"
    monkeypatch.setattr(soak_log, "RISK_REJECTS_LOG", str(rpath))
    monkeypatch.setattr(soak_log, "LOG_DIR", str(tmp_path / "logs"))
    cfg = {"watchlist_quality": {"mode": "enforce", "risk_reject_log": True}}

    with tenant_context("henry"):
        soak_log.log_risk_reject(
            symbol="Z/USDT",
            code="watchlist_quality",
            quality_score=0.1,
            quality_shadow_ai=0.1,
            config=cfg,
            wqe_mode_value="enforce",
        )
    rec = json.loads(rpath.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["tenant_id"] == "henry"
