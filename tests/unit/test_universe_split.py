"""Observe vs trade universe split (option C)."""

from __future__ import annotations

from services.universe.split import (
    apply_observe_cap,
    select_trade_universe,
    universe_split_config,
)
from services.universe.split import rank_key_for_coin


def _coin(sym: str, **extra):
    c = {"symbol": sym, "active": True, "ticker": sym.split("/")[0]}
    c.update(extra)
    return c


def test_universe_split_config_defaults():
    cfg = universe_split_config({})
    assert cfg["split_enabled"] is False
    assert cfg["observe_max_coins"] >= 80
    assert cfg["trade_max_coins"] >= 20


def test_universe_split_config_merge():
    cfg = universe_split_config(
        {"universe": {"split_enabled": True, "trade_max_coins": 35, "observe_max_coins": 90}}
    )
    assert cfg["split_enabled"] is True
    assert cfg["trade_max_coins"] == 35
    assert cfg["observe_max_coins"] == 90


def test_select_trade_always_includes_open_positions():
    observe = [_coin(f"C{i}/USDT", trending_rank=i) for i in range(1, 30)]
    open_syms = {"C1/USDT", "C2/USDT", "OPEN/USDT"}
    # OPEN not in observe — still injected as stub? only from observe for now
    observe.append(_coin("OPEN/USDT", source="position"))
    trade = select_trade_universe(
        observe,
        open_symbols=open_syms,
        base_symbols=set(),
        trade_max_coins=5,
        include_open_positions=True,
        include_base=False,
        rank_by="trending_rank",
    )
    syms = {c["symbol"] for c in trade}
    assert "C1/USDT" in syms
    assert "C2/USDT" in syms
    assert "OPEN/USDT" in syms
    # open always kept even if trade_max small
    assert len([c for c in trade if c["symbol"] in open_syms]) == 3


def test_select_trade_caps_discovery_not_open():
    observe = [_coin(f"D{i}/USDT", trending_rank=i) for i in range(1, 21)]
    open_syms = {f"P{i}/USDT" for i in range(1, 6)}
    for s in open_syms:
        observe.append(_coin(s, source="held"))
    trade = select_trade_universe(
        observe,
        open_symbols=open_syms,
        base_symbols=set(),
        trade_max_coins=8,  # 5 open + 3 discovery
        include_open_positions=True,
        include_base=False,
        rank_by="trending_rank",
    )
    syms = [c["symbol"] for c in trade]
    for s in open_syms:
        assert s in syms
    discovery = [s for s in syms if s not in open_syms]
    assert len(discovery) <= 3
    assert len(trade) <= 8 or len(open_syms) > 8  # open may exceed max


def test_select_trade_includes_base():
    observe = [
        _coin("BASE/USDT", source="base"),
        _coin("T1/USDT", trending_rank=1),
        _coin("T2/USDT", trending_rank=2),
    ]
    trade = select_trade_universe(
        observe,
        open_symbols=set(),
        base_symbols={"BASE/USDT"},
        trade_max_coins=2,
        include_open_positions=True,
        include_base=True,
        rank_by="trending_rank",
    )
    syms = {c["symbol"] for c in trade}
    assert "BASE/USDT" in syms


def test_select_trade_rank_by_quality_score():
    observe = [
        _coin("LOW/USDT", quality_score=0.1),
        _coin("HIGH/USDT", quality_score=0.9),
        _coin("MID/USDT", quality_score=0.5),
    ]
    trade = select_trade_universe(
        observe,
        open_symbols=set(),
        base_symbols=set(),
        trade_max_coins=2,
        include_open_positions=False,
        include_base=False,
        rank_by="quality_score",
    )
    syms = [c["symbol"] for c in trade]
    assert syms[0] == "HIGH/USDT"
    assert "LOW/USDT" not in syms


def test_apply_observe_cap_prefers_forced():
    coins = [_coin(f"X{i}/USDT") for i in range(20)]
    forced = {"X0/USDT", "X1/USDT"}
    out = apply_observe_cap(coins, max_coins=5, forced_symbols=forced)
    assert len(out) == 5
    syms = {c["symbol"] for c in out}
    assert forced <= syms


def test_rank_key_quality_and_trending():
    assert rank_key_for_coin(_coin("A/USDT", quality_score=0.8), "quality_score") < 0
    # lower rank number = better for trending
    assert rank_key_for_coin(_coin("A/USDT", trending_rank=1), "trending_rank") < rank_key_for_coin(
        _coin("B/USDT", trending_rank=9), "trending_rank"
    )


def test_split_disabled_helpers_are_identity():
    coins = [_coin("A/USDT"), _coin("B/USDT")]
    # when max 0 or negative treat as unlimited
    assert apply_observe_cap(coins, max_coins=0, forced_symbols=set()) == coins


def test_load_trade_watchlist_respects_split_flag(monkeypatch):
    from data_manager import load_trade_watchlist

    observe = [_coin(f"Z{i}/USDT", quality_score=i / 10) for i in range(15)]

    monkeypatch.setattr(
        "data_manager.load_config",
        lambda tenant_id=None: {
            "universe": {
                "split_enabled": True,
                "trade_max_coins": 5,
                "trade_include_open_positions": True,
                "trade_include_base": False,
                "trade_rank_by": "quality_score",
            }
        },
    )
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    monkeypatch.setattr(
        "services.universe.split._open_symbols_live",
        lambda: {"Z14/USDT"},
    )
    monkeypatch.setattr(
        "services.universe.split._quality_lookup",
        lambda tenant_id="default": {},
    )
    trade = load_trade_watchlist(observe_coins=observe, open_positions=[{"symbol": "Z14/USDT"}])
    assert any(c["symbol"] == "Z14/USDT" for c in trade)
    assert len(trade) <= 5


def test_load_trade_watchlist_split_off_returns_observe(monkeypatch):
    from data_manager import load_trade_watchlist

    observe = [_coin("A/USDT"), _coin("B/USDT")]
    monkeypatch.setattr(
        "data_manager.load_config",
        lambda tenant_id=None: {"universe": {"split_enabled": False}},
    )
    trade = load_trade_watchlist(observe_coins=observe)
    assert len(trade) == 2

