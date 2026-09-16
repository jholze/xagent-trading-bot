"""#465: ``watchlist_quality.ai.sort_by`` / ``ai.enabled`` must actually flip the sort.

Before the fix every runtime call site hardcoded ``use_ai_score=True`` (and
``soft_scan_order`` had ``use_ai_sort_score(config) or True``), so the config
rollback switch was dead. These tests pin the contract on both paths:

- WQE path: ``apply_wqe_to_watchlist`` (runtime.py), the engine soft helper and
  ``load_observe_universe`` (whose order comes from the WQE soft transform)
- universe split: ``load_trade_universe`` (split.py rank key + score lookup)

AAA has the better deterministic ``quality_score``; BBB has the better AI-fused
``quality_shadow_ai``. AI order → BBB first; rollback → AAA first.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.universe.split import (
    _quality_lookup,
    load_observe_universe,
    load_trade_universe,
    rank_key_for_coin,
    select_trade_universe,
)
from services.watchlist_quality.config import use_ai_sort_score
from services.watchlist_quality.engine import apply_soft_to_effective_candidates
from services.watchlist_quality.runtime import apply_wqe_to_watchlist, clear_runtime_cache

AAA = "AAA/USDT"
BBB = "BBB/USDT"

# Opposite orderings: det score says AAA, AI score says BBB.
_SCORES = {
    AAA: {"quality_score": 0.9, "quality_shadow_ai": 0.2},
    BBB: {"quality_score": 0.3, "quality_shadow_ai": 0.8},
}

AI_LIVE = {"enabled": True, "sort_by": "quality_shadow_ai"}
AI_ROLLBACK_SORT_BY = {"enabled": True, "sort_by": ""}
AI_ROLLBACK_DISABLED = {"enabled": False, "sort_by": "quality_shadow_ai"}


def _wqe_cfg(ai: dict, **extra) -> dict:
    return {
        "watchlist_quality": {
            "mode": "soft",
            "vol_floors": {"t1_min_quote_vol_usd": 100},
            "ai": dict(ai),
        },
        **extra,
    }


def _coins(with_scores: bool = False) -> list[dict]:
    out = []
    for sym in (AAA, BBB):
        row = {"symbol": sym, "active": True, "quote_vol_24h": 5_000_000}
        if with_scores:
            row.update(_SCORES[sym])
        out.append(row)
    return out


def _fake_run_shadow_score(coins, **kw):
    """Stand-in for engine.run_shadow_score returning both scores per coin."""
    rows = []
    for c in coins:
        sym = c["symbol"]
        rows.append(
            {
                "symbol": sym,
                **_SCORES[sym],
                "tier_hint": "T2",
                "flags": [],
                "memory": {},
                "metrics": {"quote_vol_24h": c.get("quote_vol_24h")},
            }
        )
    return {"mode": "soft", "scored": len(rows), "coins": rows}


# --------------------------------------------------------------------------- #
# config switch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ai, expected",
    [
        (AI_LIVE, True),
        ({"enabled": True, "sort_by": "ai"}, True),
        (AI_ROLLBACK_SORT_BY, False),
        (AI_ROLLBACK_DISABLED, False),
        ({"enabled": False, "sort_by": ""}, False),
    ],
)
def test_use_ai_sort_score_switch(ai, expected):
    assert use_ai_sort_score(_wqe_cfg(ai)) is expected


# --------------------------------------------------------------------------- #
# WQE path: apply_wqe_to_watchlist (runtime.py)
# --------------------------------------------------------------------------- #


def _apply_wqe(cfg: dict) -> list[str]:
    clear_runtime_cache()  # cache key ignores config — must reset between variants
    with patch(
        "services.watchlist_quality.runtime.run_shadow_score",
        side_effect=_fake_run_shadow_score,
    ), patch("services.watchlist_quality.runtime._open_symbols", return_value=set()):
        out = apply_wqe_to_watchlist(_coins(), config=cfg, base_symbols=set(), attach_vol=False)
    return [c["symbol"] for c in out]


def test_apply_wqe_live_sort_by_keeps_ai_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _apply_wqe(_wqe_cfg(AI_LIVE)) == [BBB, AAA]


@pytest.mark.parametrize("ai", [AI_ROLLBACK_SORT_BY, AI_ROLLBACK_DISABLED])
def test_apply_wqe_rollback_orders_by_quality_score(tmp_path, monkeypatch, ai):
    monkeypatch.chdir(tmp_path)
    assert _apply_wqe(_wqe_cfg(ai)) == [AAA, BBB]


def test_apply_wqe_flipping_config_flips_order(tmp_path, monkeypatch):
    """Same coins, only the config changes → order flips (and back)."""
    monkeypatch.chdir(tmp_path)
    assert _apply_wqe(_wqe_cfg(AI_LIVE)) == [BBB, AAA]
    assert _apply_wqe(_wqe_cfg(AI_ROLLBACK_SORT_BY)) == [AAA, BBB]
    assert _apply_wqe(_wqe_cfg(AI_LIVE)) == [BBB, AAA]


# --------------------------------------------------------------------------- #
# WQE path: engine.apply_soft_to_effective_candidates
# --------------------------------------------------------------------------- #


def test_engine_soft_candidates_honour_sort_by():
    coins = _coins(with_scores=True)
    live = apply_soft_to_effective_candidates(coins, config=_wqe_cfg(AI_LIVE), open_symbols=set())
    assert [c["symbol"] for c in live] == [BBB, AAA]
    for ai in (AI_ROLLBACK_SORT_BY, AI_ROLLBACK_DISABLED):
        back = apply_soft_to_effective_candidates(coins, config=_wqe_cfg(ai), open_symbols=set())
        assert [c["symbol"] for c in back] == [AAA, BBB], ai


# --------------------------------------------------------------------------- #
# WQE path: load_observe_universe (order comes from the WQE soft transform)
# --------------------------------------------------------------------------- #


def _observe(cfg: dict) -> list[str]:
    # mirrors data_manager.build_merged_watchlist_coins: merged rows → WQE soft transform
    def build_merged(tenant_id=None, config=None):
        return [{"symbol": s, "active": True} for s in _apply_wqe(config)]

    out = load_observe_universe(config=cfg, build_merged_fn=build_merged)
    return [c["symbol"] for c in out]


def test_load_observe_universe_follows_sort_by(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    monkeypatch.setattr("services.universe.split._open_symbols_live", lambda: set())
    split = {"universe": {"split_enabled": True, "observe_max_coins": 50}}

    assert _observe(_wqe_cfg(AI_LIVE, **split)) == [BBB, AAA]
    assert _observe(_wqe_cfg(AI_ROLLBACK_SORT_BY, **split)) == [AAA, BBB]
    assert _observe(_wqe_cfg(AI_ROLLBACK_DISABLED, **split)) == [AAA, BBB]


# --------------------------------------------------------------------------- #
# universe split path: split.py
# --------------------------------------------------------------------------- #


def test_rank_key_for_coin_honours_use_ai_score():
    a, b = (dict(symbol=AAA, **_SCORES[AAA]), dict(symbol=BBB, **_SCORES[BBB]))
    # default (AI) → BBB ranks first
    assert rank_key_for_coin(b, "quality_score") < rank_key_for_coin(a, "quality_score")
    # rollback → AAA ranks first
    assert rank_key_for_coin(a, "quality_score", use_ai_score=False) < rank_key_for_coin(
        b, "quality_score", use_ai_score=False
    )


def test_select_trade_universe_honours_use_ai_score():
    observe = _coins(with_scores=True)
    kw = dict(
        open_symbols=set(),
        base_symbols=set(),
        trade_max_coins=1,
        include_open_positions=False,
        include_base=False,
        rank_by="quality_score",
    )
    assert [c["symbol"] for c in select_trade_universe(observe, **kw)] == [BBB]
    assert [c["symbol"] for c in select_trade_universe(observe, use_ai_score=True, **kw)] == [BBB]
    assert [c["symbol"] for c in select_trade_universe(observe, use_ai_score=False, **kw)] == [AAA]


def test_quality_lookup_honours_use_ai_score():
    store = {"coins": [{"symbol": s, **_SCORES[s]} for s in (AAA, BBB)]}
    with patch("services.watchlist_quality.store.load_quality_scores", return_value=store):
        ai = _quality_lookup("default")
        det = _quality_lookup("default", use_ai_score=False)
    assert ai == {AAA: 0.2, BBB: 0.8}
    assert det == {AAA: 0.9, BBB: 0.3}


def _trade(cfg: dict, observe: list[dict]) -> list[str]:
    out = load_trade_universe(config=cfg, observe_coins=list(observe), open_symbols=set())
    return [c["symbol"] for c in out]


_SPLIT_TRADE = {
    "universe": {
        "split_enabled": True,
        "trade_max_coins": 1,
        "trade_include_open_positions": True,
        "trade_include_base": False,
        "trade_rank_by": "quality_score",
    }
}


def test_load_trade_universe_follows_sort_by_with_row_scores(monkeypatch):
    """Scores present on the observe rows (WQE already ran)."""
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    monkeypatch.setattr(
        "services.universe.split._quality_lookup",
        lambda tenant_id="default", use_ai_score=True: {},
    )
    observe = _coins(with_scores=True)

    assert _trade(_wqe_cfg(AI_LIVE, **_SPLIT_TRADE), observe) == [BBB]
    assert _trade(_wqe_cfg(AI_ROLLBACK_SORT_BY, **_SPLIT_TRADE), observe) == [AAA]
    assert _trade(_wqe_cfg(AI_ROLLBACK_DISABLED, **_SPLIT_TRADE), observe) == [AAA]


def test_load_trade_universe_follows_sort_by_via_store_lookup(monkeypatch):
    """Scores only in the persisted store (observe rows unscored) — lookup must honour the flag."""
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    store = {"coins": [{"symbol": s, **_SCORES[s]} for s in (AAA, BBB)]}
    observe = _coins(with_scores=False)

    with patch("services.watchlist_quality.store.load_quality_scores", return_value=store):
        assert _trade(_wqe_cfg(AI_LIVE, **_SPLIT_TRADE), observe) == [BBB]
        assert _trade(_wqe_cfg(AI_ROLLBACK_SORT_BY, **_SPLIT_TRADE), observe) == [AAA]
        assert _trade(_wqe_cfg(AI_ROLLBACK_DISABLED, **_SPLIT_TRADE), observe) == [AAA]


# --------------------------------------------------------------------------- #
# soft_scan_order stays deleted
# --------------------------------------------------------------------------- #


def test_soft_scan_order_removed():
    import services.watchlist_quality.soft as soft

    assert not hasattr(soft, "soft_scan_order")
