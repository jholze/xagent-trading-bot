"""Unit tests for gainer universe (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.gainer_universe.filters import passes_spot_usdt_filter
from services.gainer_universe.inject import (
    expand_candidates_for_trade,
    merge_expand_into_trade,
    merge_gainers_into_observe,
)
from services.watchlist_quality.memory_bias import MemoryWqeInput
from services.gainer_universe.scanner import (
    build_eligible,
    compute_streaks,
    filter_and_rank_live,
)
from services.exit_rotation import (
    apply_exit_section_overlay,
    exit_rotation_config,
)


def test_filter_drops_leverage_keeps_stock():
    assert passes_spot_usdt_filter("BTC/USDT")
    assert not passes_spot_usdt_filter("BTC3L/USDT")
    assert not passes_spot_usdt_filter("ETH3S/USDT")
    assert passes_spot_usdt_filter("NVDAX/USDT")  # stock token ok
    assert not passes_spot_usdt_filter("USDC/USDT")


def test_filter_blacklist_base():
    assert not passes_spot_usdt_filter(
        "SCAM/USDT", blacklist_bases=["SCAM"]
    )


def test_live_rank_mock_tickers():
    tickers = {
        "AAA/USDT": {"last": 1, "percentage": 50, "quoteVolume": 2_000_000},
        "BBB/USDT": {"last": 1, "percentage": 10, "quoteVolume": 2_000_000},
        "BTC3L/USDT": {"last": 1, "percentage": 99, "quoteVolume": 9_000_000},
        "LOW/USDT": {"last": 1, "percentage": 80, "quoteVolume": 100},
    }
    cfg = {
        "min_volume_usdt_24h": 500_000,
        "live_top_n": 10,
        "blacklist_suffixes": ["3L", "3S"],
    }
    ranked = filter_and_rank_live(tickers, cfg)
    syms = [r["symbol"] for r in ranked]
    assert syms[0] == "AAA/USDT"
    assert "BTC3L/USDT" not in syms
    assert "LOW/USDT" not in syms


def test_streaks_and_eligible_prev_day():
    today = datetime.now(timezone.utc).date()
    d0 = (today - timedelta(days=2)).isoformat()
    d1 = (today - timedelta(days=1)).isoformat()
    hist = {
        d0: [
            {"symbol": "HOT/USDT", "day_ret_pct": 20, "rank": 1},
            {"symbol": "X/USDT", "day_ret_pct": 15, "rank": 2},
        ],
        d1: [
            {"symbol": "HOT/USDT", "day_ret_pct": 12, "rank": 1},
            {"symbol": "Y/USDT", "day_ret_pct": 10, "rank": 2},
        ],
    }
    cfg = {
        "streak_min_days_in_top20": 2,
        "streak_lookback_days": 3,
        "enable_continuation": True,
        "continuation_max_chase_pct_today": 15,
        "expand_inject_max": 40,
        "prev_top_ttl_hours": 36,
    }
    streaks = compute_streaks(hist, cfg)
    assert any(s["symbol"] == "HOT/USDT" for s in streaks)

    live = [{"symbol": "HOT/USDT", "pct_24h": 5, "rank": 1}]
    elig = build_eligible(hist, live, streaks, cfg)
    # prev day tops include HOT and Y
    sources = {e["symbol"]: e["source"] for e in elig}
    assert sources.get("Y/USDT") == "gate_prev_top"
    assert "HOT/USDT" in sources


def test_ttl_expiry_skips_inject():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "OLD/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": past,
            }
        ]
    }
    coins = expand_candidates_for_trade(state, {"expand_inject_max": 10, "enabled": True, "mode": "trade_expand"})
    assert coins == []


def test_shadow_no_trade_merge():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    state = {
        "eligible": [
            {
                "symbol": "HOT/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            }
        ]
    }
    root = {"gainer_universe": {"enabled": True, "mode": "shadow", "expand_inject_max": 40, "trade_max_with_expand": 80}}
    out = merge_expand_into_trade(trade, state, root_config=root)
    assert len(out) == 1
    assert out[0]["symbol"] == "BTC/USDT"


def test_trade_expand_injects():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "HOT/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": fut,
            }
        ]
    }
    root = {
        "gainer_universe": {
            "enabled": True,
            "mode": "trade_expand",
            "expand_inject_max": 40,
            "trade_max_with_expand": 80,
        }
    }
    out = merge_expand_into_trade(trade, state, root_config=root)
    syms = {c["symbol"] for c in out}
    assert "BTC/USDT" in syms
    assert "HOT/USDT" in syms


def test_observe_merge_adds_live():
    observe = [{"symbol": "BTC/USDT", "active": True}]
    state = {
        "live_top": [{"symbol": "AAA/USDT", "pct_24h": 30, "rank": 1}],
        "eligible": [],
    }
    out = merge_gainers_into_observe(observe, state, {"enabled": True, "mode": "shadow"})
    syms = {c["symbol"] for c in out}
    assert "BTC/USDT" in syms
    assert "AAA/USDT" in syms


def test_rot_mid_overlay():
    root = {"exit_rotation": {"enabled": True, "profile": "rot_mid"}}
    ttp = apply_exit_section_overlay(
        {"arm_gain_pct": 15, "min_gain_pct": 10, "trail_pct": 6},
        "trailing_take_profit",
        root_config=root,
    )
    assert ttp["arm_gain_pct"] == 10
    assert ttp["min_gain_pct"] == 6
    assert ttp["trail_pct"] == 6  # untouched

    pml = apply_exit_section_overlay(
        {"max_hours": 96, "arm_gain_pct": 3},
        "profit_max_lifetime",
        root_config=root,
    )
    assert pml["max_hours"] == 48


def test_rot_base_identity():
    root = {"exit_rotation": {"enabled": False, "profile": "rot_mid"}}
    ttp = apply_exit_section_overlay(
        {"arm_gain_pct": 15},
        "trailing_take_profit",
        root_config=root,
    )
    assert ttp["arm_gain_pct"] == 15


def test_exit_rotation_config_invalid_profile():
    er = exit_rotation_config({"exit_rotation": {"enabled": True, "profile": "nope"}})
    assert er["profile"] == "base"


def test_live_heat_in_eligible_band():
    today = datetime.now(timezone.utc).date()
    yest = (today - timedelta(days=1)).isoformat()
    hist = {
        yest: [{"symbol": "OLD/USDT", "day_ret_pct": 10, "rank": 1}],
    }
    live = [
        {"symbol": "AEON/USDT", "pct_24h": 18.0, "rank": 1, "quote_volume": 1e6},
        {"symbol": "CHASE/USDT", "pct_24h": 90.0, "rank": 2, "quote_volume": 1e6},
        {"symbol": "WEAK/USDT", "pct_24h": 3.0, "rank": 3, "quote_volume": 1e6},
        {"symbol": "EUL/USDT", "pct_24h": 12.0, "rank": 4, "quote_volume": 1e6},
    ]
    cfg = {
        "live_heat_trade": True,
        "live_heat_min_pct": 8,
        "live_heat_max_pct": 35,
        "live_heat_ttl_hours": 10,
        "enable_continuation": False,
        "expand_inject_max": 40,
        "prev_top_ttl_hours": 36,
    }
    elig = build_eligible(hist, live, [], cfg)
    by = {e["symbol"]: e for e in elig}
    assert by["AEON/USDT"]["source"] == "gainer_live_heat"
    assert by["EUL/USDT"]["source"] == "gainer_live_heat"
    assert "CHASE/USDT" not in by  # above max band
    assert "WEAK/USDT" not in by
    assert by["OLD/USDT"]["source"] == "gate_prev_top"


def test_scan_prefer_gainer_order():
    from core.cycle_order import order_watchlist_positions_first

    coins = [
        {"symbol": "ZZZ/USDT", "source": "discovery", "active": True},
        {"symbol": "AEON/USDT", "source": "gainer_live_heat", "gainer_day_ret": 18, "active": True},
        {"symbol": "BTC/USDT", "source": "base", "active": True},
        {"symbol": "ON/USDT", "source": "gate_prev_top", "gainer_day_ret": 80, "active": True},
    ]
    open_pos = [{"symbol": "BTC/USDT", "timeframe": "1h"}]
    ordered = order_watchlist_positions_first(coins, open_pos, prefer_gainer=True)
    syms = [c["symbol"] for c in ordered]
    assert syms[0] == "BTC/USDT"  # positions first
    # gainers before plain discovery
    assert syms.index("ON/USDT") < syms.index("ZZZ/USDT")
    assert syms.index("AEON/USDT") < syms.index("ZZZ/USDT")


def test_chase_guard_blocks_extended_prev_top(monkeypatch):
    from services.gainer_universe import chase_guard as cg

    state = {
        "eligible": [
            {
                "symbol": "ON/USDT",
                "source": "gate_prev_top",
                "day": "2026-07-28",
                "day_ret": 84,
            }
        ]
    }
    monkeypatch.setattr(cg, "_prev_day_close", lambda sym, day: 1.0)
    root = {
        "gainer_universe": {
            "enabled": True,
            "mode": "trade_expand",
            "chase_guard_enabled": True,
            "chase_max_gain_from_prev_close_pct": 18,
            "chase_guard_sources": ["gate_prev_top"],
        }
    }
    # +50% vs prev close → block
    blocked, msg = cg.check_gainer_chase_guard(
        "ON/USDT", 1.50, config=root, state=state
    )
    assert blocked is True
    assert "gainer_chase_guard" in msg
    # +5% → ok
    blocked2, _ = cg.check_gainer_chase_guard(
        "ON/USDT", 1.05, config=root, state=state
    )
    assert blocked2 is False


def _chase_state(symbol: str, source: str = "gate_prev_top") -> dict:
    return {
        "eligible": [
            {"symbol": symbol, "source": source, "day": "2026-07-28", "day_ret": 84}
        ]
    }


_CHASE_ROOT = {
    "gainer_universe": {
        "enabled": True,
        "mode": "trade_expand",
        "chase_guard_enabled": True,
        "chase_max_gain_from_prev_close_pct": 18,
        "chase_guard_sources": ["gate_prev_top"],
    }
}


def _boom(*_a, **_k):
    raise RuntimeError("ohlcv-down")


def test_chase_guard_ohlcv_throw_fails_closed(monkeypatch):
    """#423: OHLCV failure with no cached close must block, not (False, "")."""
    from services.gainer_universe import chase_guard as cg

    monkeypatch.setattr(cg, "_LAST_CLOSE", {})
    monkeypatch.setattr("historical_prices._fetch_ohlcv_range", _boom)
    blocked, msg = cg.check_gainer_chase_guard(
        "ON/USDT", 1.05, config=_CHASE_ROOT, state=_chase_state("ON/USDT")
    )
    assert blocked is True
    assert "gainer_chase_guard" in msg
    assert "unavailable" in msg


def test_chase_guard_ohlcv_empty_bars_fails_closed(monkeypatch):
    """#423: no bar for prev_day (fetch ok, close missing) also blocks."""
    from services.gainer_universe import chase_guard as cg

    monkeypatch.setattr(cg, "_LAST_CLOSE", {})
    monkeypatch.setattr("historical_prices._fetch_ohlcv_range", lambda *a, **k: [])
    blocked, msg = cg.check_gainer_chase_guard(
        "ON/USDT", 1.05, config=_CHASE_ROOT, state=_chase_state("ON/USDT")
    )
    assert blocked is True
    assert "unavailable" in msg


def test_chase_guard_ohlcv_throw_uses_cached_close(monkeypatch):
    """#423: a previously fetched close is reused when OHLCV later fails."""
    from services.gainer_universe import chase_guard as cg

    monkeypatch.setattr(cg, "_LAST_CLOSE", {})
    day_ms = int(
        datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp() * 1000
    )
    bars = [[day_ms, 1.0, 1.1, 0.9, 1.0, 1000.0]]
    monkeypatch.setattr("historical_prices._fetch_ohlcv_range", lambda *a, **k: bars)
    blocked, _ = cg.check_gainer_chase_guard(
        "ON/USDT", 1.05, config=_CHASE_ROOT, state=_chase_state("ON/USDT")
    )
    assert blocked is False
    assert cg._LAST_CLOSE[("ON/USDT", "2026-07-28")] == 1.0

    monkeypatch.setattr("historical_prices._fetch_ohlcv_range", _boom)
    # cached close 1.0 → +5% passes, +50% blocks on the real threshold
    blocked_ok, _ = cg.check_gainer_chase_guard(
        "ON/USDT", 1.05, config=_CHASE_ROOT, state=_chase_state("ON/USDT")
    )
    assert blocked_ok is False
    blocked_hi, msg = cg.check_gainer_chase_guard(
        "ON/USDT", 1.50, config=_CHASE_ROOT, state=_chase_state("ON/USDT")
    )
    assert blocked_hi is True
    assert "+50.0%" in msg


def test_chase_guard_ohlcv_throw_non_guarded_source_stays_open(monkeypatch):
    """#423: fail-closed applies to chase_guard_sources only."""
    from services.gainer_universe import chase_guard as cg

    monkeypatch.setattr(cg, "_LAST_CLOSE", {}, raising=False)
    monkeypatch.setattr("historical_prices._fetch_ohlcv_range", _boom)
    blocked, msg = cg.check_gainer_chase_guard(
        "ON/USDT",
        1.05,
        config=_CHASE_ROOT,
        state=_chase_state("ON/USDT", source="cmc_trending"),
    )
    assert blocked is False
    assert msg == ""


def _mem_input(symbol: str, *, hard: bool, tenant_id: str = "default") -> MemoryWqeInput:
    return MemoryWqeInput(
        symbol=symbol,
        entry_bias="soft_block" if hard else "neutral",
        size_bias=1.0,
        memory_score=0.1 if hard else 0.5,
        hard_exclude_new_add=hard,
        ttl_active=hard,
        scope="sensor_only",
        rationale="test",
        source="profile" if hard else "default",
    )


def _expand_root(**extra):
    root = {
        "watchlist_quality": {
            "mode": "soft",
            "honor_memory_soft_block": True,
            "memory": {"enabled": True, "exclude_new_adds_on_soft_block": True},
        },
        "gainer_universe": {
            "enabled": True,
            "mode": "trade_expand",
            "expand_inject_max": 40,
            "trade_max_with_expand": 80,
            "blacklist_bases": [],
        },
    }
    root.update(extra)
    return root


def test_trade_expand_skips_memory_soft_block_for_tenant():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "BDX/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": fut,
            },
            {
                "symbol": "HOT/USDT",
                "source": "gate_prev_top",
                "rank": 2,
                "day_ret": 18,
                "eligible_until": fut,
            },
        ]
    }
    seen_tenants: list[str] = []

    def fake_mem(sym, *, tenant_id="default", **kw):
        seen_tenants.append(tenant_id)
        return _mem_input(sym, hard=(tenant_id == "henry" and sym == "BDX/USDT"))

    root = _expand_root()
    with patch(
        "services.watchlist_quality.memory_bias.get_memory_wqe_input",
        side_effect=fake_mem,
    ):
        henry = merge_expand_into_trade(
            trade, state, root_config=root, tenant_id="henry"
        )
        default = merge_expand_into_trade(
            list(trade), state, root_config=root, tenant_id="default"
        )
    henry_syms = {c["symbol"] for c in henry}
    default_syms = {c["symbol"] for c in default}
    assert "BTC/USDT" in henry_syms
    assert "HOT/USDT" in henry_syms
    assert "BDX/USDT" not in henry_syms
    assert "BDX/USDT" in default_syms
    assert "henry" in seen_tenants
    assert "default" in seen_tenants


def test_observe_merge_skips_memory_soft_block_keeps_existing():
    observe = [
        {"symbol": "BTC/USDT", "active": True},
        {"symbol": "BDX/USDT", "active": True, "source": "watchlist"},
    ]
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    state = {
        "live_top": [{"symbol": "LOSER/USDT", "pct_24h": 30, "rank": 1}],
        "eligible": [
            {
                "symbol": "LOSER/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": fut,
            }
        ],
    }

    def fake_mem(sym, *, tenant_id="default", **kw):
        return _mem_input(sym, hard=(sym == "LOSER/USDT" or sym == "BDX/USDT"))

    with patch(
        "services.watchlist_quality.memory_bias.get_memory_wqe_input",
        side_effect=fake_mem,
    ):
        out = merge_gainers_into_observe(
            observe,
            state,
            {"enabled": True, "mode": "shadow", "blacklist_bases": []},
            tenant_id="henry",
            root_config=_expand_root(),
        )
    syms = {c["symbol"] for c in out}
    assert "BTC/USDT" in syms
    assert "BDX/USDT" in syms  # existing membership kept
    assert "LOSER/USDT" not in syms


def _eligible_state(*symbols: str) -> dict:
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    return {
        "eligible": [
            {
                "symbol": s,
                "source": "gate_prev_top",
                "rank": i + 1,
                "day_ret": 20,
                "eligible_until": fut,
            }
            for i, s in enumerate(symbols)
        ]
    }


def test_gainer_inject_fail_open_missing_profile():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    state = _eligible_state("NEW/USDT")
    with patch(
        "intelligence.memory.cache.get_coin_profile", return_value=None
    ), patch(
        "intelligence.memory.store.memory_enabled", return_value=True
    ):
        out = merge_expand_into_trade(
            trade, state, root_config=_expand_root(), tenant_id="henry"
        )
    assert {c["symbol"] for c in out} >= {"BTC/USDT", "NEW/USDT"}


def test_gainer_inject_fail_open_on_memory_error():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    state = _eligible_state("NEW/USDT")
    with patch(
        "services.watchlist_quality.memory_bias.get_memory_wqe_input",
        side_effect=RuntimeError("mongo down"),
    ):
        out = merge_expand_into_trade(
            trade, state, root_config=_expand_root(), tenant_id="henry"
        )
    assert {c["symbol"] for c in out} >= {"BTC/USDT", "NEW/USDT"}


def test_gainer_inject_ttl_expired_eligible_again():
    from types import SimpleNamespace

    trade = [{"symbol": "BTC/USDT", "active": True}]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "OLD/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": fut,
            }
        ]
    }
    prof = SimpleNamespace(
        symbol="OLD/USDT",
        entry_bias="soft_block",
        size_bias=1.0,
        rationale="expired",
        features={"soft_block_scope": "sensor_only", "soft_block_until": past},
    )
    with patch(
        "intelligence.memory.cache.get_coin_profile", return_value=prof
    ), patch(
        "intelligence.memory.store.memory_enabled", return_value=True
    ):
        out = merge_expand_into_trade(
            trade, state, root_config=_expand_root(), tenant_id="henry"
        )
    assert {c["symbol"] for c in out} >= {"BTC/USDT", "OLD/USDT"}
