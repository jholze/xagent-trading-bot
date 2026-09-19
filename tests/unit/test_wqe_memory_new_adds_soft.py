"""#494: honor CoinProfile soft_block for new membership in WQE soft."""

from __future__ import annotations

from unittest.mock import patch

from services.universe.split import load_trade_universe, select_trade_universe
from services.watchlist_quality.enforce import filter_new_adds_memory
from services.watchlist_quality.memory_bias import MemoryWqeInput
from services.watchlist_quality.runtime import apply_wqe_to_watchlist, clear_runtime_cache


def _mem(
    symbol: str,
    *,
    hard: bool = False,
    entry_bias: str = "neutral",
    source: str = "profile",
) -> MemoryWqeInput:
    return MemoryWqeInput(
        symbol=symbol,
        entry_bias="soft_block" if hard else entry_bias,
        size_bias=1.0,
        memory_score=0.1 if hard else 0.5,
        hard_exclude_new_add=hard,
        ttl_active=hard,
        scope="sensor_only" if hard else "",
        rationale="test",
        source=source,
    )


def _soft_cfg(**extra):
    cfg = {
        "watchlist_quality": {
            "mode": "soft",
            "honor_memory_soft_block": True,
            "min_buy_score": 0.9,
            "drop_t3": True,
            "vol_floors": {"t1_min_quote_vol_usd": 750_000},
            "regime_caps": {"neutral": {"T1": 1, "T2": 0, "T3": 0}},
            "ai": {"enabled": False},
            "memory": {
                "enabled": True,
                "exclude_new_adds_on_soft_block": True,
            },
        },
        "gainer_universe": {
            "enabled": False,
            "mode": "off",
            "blacklist_bases": [],
        },
        "universe": {
            "split_enabled": True,
            "trade_max_coins": 40,
            "trade_include_open_positions": True,
            "trade_include_base": True,
        },
    }
    cfg.update(extra)
    return cfg


def test_soft_drops_hard_exclude_overlay_keeps_base():
    clear_runtime_cache()
    coins = [
        {
            "symbol": "BASE/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "watchlist",
        },
        {
            "symbol": "LOSER/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "dry_run_expansion",
        },
        {
            "symbol": "OKNEW/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "cmc_trending",
        },
    ]

    def mem(sym, **kw):
        return _mem(sym, hard=(sym in ("LOSER/USDT", "BASE/USDT")))

    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input", side_effect=mem
    ), patch(
        "services.watchlist_quality.runtime._open_symbols", return_value=set()
    ):
        out = apply_wqe_to_watchlist(
            coins,
            config=_soft_cfg(),
            base_symbols={"BASE/USDT"},
            tenant_id="henry",
            attach_vol=False,
        )
    syms = {c["symbol"] for c in out}
    assert "BASE/USDT" in syms
    assert "OKNEW/USDT" in syms
    assert "LOSER/USDT" not in syms
    assert all(c.get("tier") is None for c in out)


def test_soft_does_not_apply_tier_caps_or_min_buy_score():
    """Soft membership drop only — no enforce caps / min_buy_score."""
    clear_runtime_cache()
    coins = [
        {
            "symbol": f"T{i}/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "watchlist",
            "quality_score": 0.2,
        }
        for i in range(4)
    ]

    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: _mem(sym, hard=False),
    ), patch(
        "services.watchlist_quality.runtime._open_symbols", return_value=set()
    ):
        out = apply_wqe_to_watchlist(
            coins,
            config=_soft_cfg(),
            base_symbols={c["symbol"] for c in coins},
            tenant_id="default",
            attach_vol=False,
        )
    assert len(out) == 4


def test_open_soft_blocked_survives_when_open_symbols_raises():
    """R2: row is_open keep; _open_symbols exception path is empty set."""
    clear_runtime_cache()
    # Fillers occupy trade_max slots so OPENLOT only survives via open-force.
    fillers = [
        {
            "symbol": f"FILL{i:02d}/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "watchlist",
            "quality_score": 0.99,
        }
        for i in range(45)
    ]
    coins = [
        {
            "symbol": "OPENLOT/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "cmc_trending",
            "is_open": True,
            "quality_score": 0.01,
        },
        {
            "symbol": "NEWDROP/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "dry_run_expansion",
        },
        *fillers,
    ]

    def mem(sym, **kw):
        return _mem(sym, hard=(sym in ("OPENLOT/USDT", "NEWDROP/USDT")))

    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input", side_effect=mem
    ), patch(
        "strategies.positions.list_active_positions",
        side_effect=RuntimeError("mongo down"),
    ):
        out = apply_wqe_to_watchlist(
            coins,
            config=_soft_cfg(),
            base_symbols=set(),
            tenant_id="henry",
            attach_vol=False,
        )
    syms = {c["symbol"] for c in out}
    assert "OPENLOT/USDT" in syms
    assert "NEWDROP/USDT" not in syms

    row_open = {c["symbol"] for c in out if c.get("is_open") or c.get("_wqe_is_open")}
    trade = select_trade_universe(
        out,
        open_symbols=row_open,
        base_symbols=set(),
        trade_max_coins=5,
        include_open_positions=True,
        include_base=True,
    )
    trade_syms = {c["symbol"] for c in trade}
    assert "OPENLOT/USDT" in trade_syms

    with patch("data_manager.load_watchlist", return_value=[]):
        via_split = load_trade_universe(
            tenant_id="henry",
            observe_coins=out,
            open_symbols=row_open,
            config=_soft_cfg(universe={
                "split_enabled": True,
                "trade_max_coins": 5,
                "trade_include_open_positions": True,
                "trade_include_base": True,
            }),
        )
    assert "OPENLOT/USDT" in {c["symbol"] for c in via_split}


def test_filter_new_adds_keeps_row_open_flags_without_open_set():
    coins = [
        {
            "symbol": "OPENLOT/USDT",
            "hard_exclude_new_add": True,
            "is_open": True,
            "source": "cmc_trending",
        },
        {
            "symbol": "FLAGGED/USDT",
            "hard_exclude_new_add": True,
            "_wqe_is_open": True,
            "source": "dry_run_expansion",
        },
        {
            "symbol": "NEWDROP/USDT",
            "hard_exclude_new_add": True,
            "source": "cmc_trending",
        },
    ]
    out = filter_new_adds_memory(
        coins, base_symbols=set(), open_symbols=set()
    )
    syms = {c["symbol"] for c in out}
    assert "OPENLOT/USDT" in syms
    assert "FLAGGED/USDT" in syms
    assert "NEWDROP/USDT" not in syms


def test_soft_fail_open_missing_profile_keeps_new_add():
    clear_runtime_cache()
    coins = [
        {
            "symbol": "UNKNOWN/USDT",
            "quote_vol_24h": 5_000_000,
            "source": "cmc_trending",
        }
    ]
    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: _mem(sym, hard=False, source="default"),
    ), patch(
        "services.watchlist_quality.runtime._open_symbols", return_value=set()
    ):
        out = apply_wqe_to_watchlist(
            coins,
            config=_soft_cfg(),
            base_symbols=set(),
            tenant_id="henry",
            attach_vol=False,
        )
    assert [c["symbol"] for c in out] == ["UNKNOWN/USDT"]
