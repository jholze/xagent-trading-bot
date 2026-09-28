"""#601 Gate 90d core seed — membership, buckets, shadow vs enforce."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.universe.core_seed import (
    CORE_SEED_30,
    CORE_SEED_30_TICKERS,
    CoreSeedLoadError,
    PIN_TICKERS,
    finalize_trade_members,
    map_bucket_to_profile,
    merge_preserving_open_lots,
    prepare_watchlist_core_seed,
    ticker_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.json"
EXPANSION_PATH = REPO_ROOT / "data" / "watchlist.dry_run_expansion.json"

_EXCLUDED = frozenset({"DASH", "QNT", "Q", "SOON"})


def _seed_cfg(mode: str = "shadow", *, behavior_change: bool = False) -> dict:
    return {
        "universe_core_seed": {
            "mode": mode,
            "behavior_change": behavior_change,
            "quiet_profile": "stable_altcoin",
            "moving_profile": "mid_cap_defaults",
            "risk_profile": "volatile_altcoin",
        },
        "stable_altcoin": {"rsi_buy_low": 30, "rsi_buy_high": 50},
        "mid_cap_defaults": {"rsi_buy_low": 28, "rsi_buy_high": 52},
        "volatile_altcoin": {"rsi_buy_low": 28, "rsi_buy_high": 48},
    }


def _repo_watchlist_coins() -> list[dict]:
    data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return list(data.get("coins") or [])


def _repo_expansion_coins() -> list[dict]:
    data = json.loads(EXPANSION_PATH.read_text(encoding="utf-8"))
    return list(data.get("coins") or [])


def _by_ticker(coins: list[dict]) -> dict[str, dict]:
    return {ticker_of(c.get("symbol") or c.get("ticker") or ""): c for c in coins}


def test_seed_has_30_gate_usdt():
    coins = _repo_watchlist_coins()
    by_t = _by_ticker(coins)
    for tick in CORE_SEED_30_TICKERS:
        row = by_t.get(tick)
        assert row is not None, tick
        assert str(row.get("symbol") or "").endswith("/USDT")
        assert row.get("active") is True
        assert row.get("timeframe") == "4h"
        assert row.get("bucket") in {"quiet", "moving", "risk"}
    assert len(CORE_SEED_30) == 30


def test_pins_kept():
    by_t = _by_ticker(_repo_watchlist_coins())
    for tick in ("ARIA", "RAVE", "HIGH", "ZBT"):
        row = by_t.get(tick)
        assert row is None or row.get("active") is not True
    loaded = prepare_watchlist_core_seed(
        [
            {"symbol": "ARIA/USDT", "ticker": "ARIA", "timeframe": "4h", "active": True},
            {"symbol": "RAVE/USDT", "ticker": "RAVE", "timeframe": "4h", "active": True},
            {"symbol": "HIGH/USDT", "ticker": "HIGH", "timeframe": "4h", "active": True},
            {"symbol": "ZBT/USDT", "ticker": "ZBT", "timeframe": "4h", "active": True},
        ],
        _seed_cfg("shadow"),
    )
    assert {ticker_of(c["symbol"]) for c in loaded} == PIN_TICKERS


def test_no_duplicate_btc_sol():
    coins = _repo_watchlist_coins()
    btc = [c for c in coins if ticker_of(c.get("symbol") or "") == "BTC"]
    sol = [c for c in coins if ticker_of(c.get("symbol") or "") == "SOL"]
    assert len(btc) == 1
    assert len(sol) == 1
    assert btc[0].get("bucket") == "quiet"
    assert sol[0].get("bucket") == "quiet"


def test_bucket_required():
    cfg = _seed_cfg("shadow")
    pins_only = [
        {"symbol": "ARIA/USDT", "ticker": "ARIA", "timeframe": "4h", "active": True},
    ]
    assert prepare_watchlist_core_seed(pins_only, cfg)

    def _core_row(tick: str, bucket: str | None) -> dict:
        row = {
            "symbol": f"{tick}/USDT",
            "ticker": tick,
            "timeframe": "4h",
            "active": True,
        }
        if bucket:
            row["bucket"] = bucket
        return row

    bucket_for = {t: "quiet" for t in CORE_SEED_30_TICKERS}
    for t in ("HYPE", "AVAX", "ADA", "TAO", "SUI", "AAVE", "BCH", "ONDO", "INJ", "PEPE"):
        bucket_for[t] = "moving"
    for t in ("DOT", "FIL", "NEAR", "UNI", "WLD", "ENA", "ZEC", "ARB"):
        bucket_for[t] = "risk"

    seed_missing_near = pins_only + [
        _core_row(t, None if t == "NEAR" else bucket_for[t])
        for t in CORE_SEED_30_TICKERS
    ]
    with pytest.raises(CoreSeedLoadError, match="missing bucket"):
        prepare_watchlist_core_seed(seed_missing_near, cfg)

    all_core_no_buckets = pins_only + [
        _core_row(t, None) for t in CORE_SEED_30_TICKERS
    ]
    with pytest.raises(CoreSeedLoadError, match="missing bucket"):
        prepare_watchlist_core_seed(all_core_no_buckets, cfg)

    seed_ok = pins_only + [_core_row(t, bucket_for[t]) for t in CORE_SEED_30_TICKERS]
    loaded = prepare_watchlist_core_seed(seed_ok, cfg)
    assert any(c["symbol"] == "NEAR/USDT" for c in loaded)


def test_bucket_maps_profile():
    cfg = _seed_cfg("shadow")
    assert map_bucket_to_profile("quiet", cfg) == "stable_altcoin"
    assert map_bucket_to_profile("moving", cfg) == "mid_cap_defaults"
    assert map_bucket_to_profile("risk", cfg) == "volatile_altcoin"


def test_excluded_names_absent():
    tickers = {ticker_of(c.get("symbol") or "") for c in _repo_watchlist_coins()}
    assert not (_EXCLUDED & tickers)
    expansion_tickers = {
        ticker_of(c.get("symbol") or "") for c in _repo_expansion_coins()
    }
    assert not (_EXCLUDED & expansion_tickers)


def test_expansion_file_is_not_live_seed():
    base = _repo_watchlist_coins()
    base_syms = {c["symbol"] for c in base if c.get("symbol")}
    base_tickers = {ticker_of(s) for s in base_syms}
    only_expansion = [
        c
        for c in _repo_expansion_coins()
        if ticker_of(c.get("symbol") or "") not in base_tickers
    ]
    assert only_expansion
    # Expansion off: those names are not in the live seed at all.
    members = finalize_trade_members(
        list(base),
        open_symbols=set(),
        base_symbols=base_syms,
        config=_seed_cfg("shadow"),
    )
    member_syms = {c["symbol"] for c in members}
    for row in only_expansion:
        assert row["symbol"] not in member_syms
        assert row.get("source") != "base"

    # Even if they leak into a merge, they are not tagged source=base.
    leaked = finalize_trade_members(
        list(base) + only_expansion,
        open_symbols=set(),
        base_symbols=base_syms,
        config=_seed_cfg("shadow"),
    )
    by_sym = {c["symbol"]: c for c in leaked}
    for row in only_expansion:
        assert by_sym[row["symbol"]].get("source") != "base"


def test_shadow_logs_profile_but_does_not_apply(monkeypatch):
    from strategies.registry import resolve_strategy_params

    logged: list[str] = []
    monkeypatch.setattr(
        "services.universe.core_seed.log",
        lambda msg, level="INFO": logged.append(str(msg)),
    )
    near = {
        "symbol": "NEAR/USDT",
        "ticker": "NEAR",
        "timeframe": "4h",
        "active": True,
        "bucket": "risk",
    }
    pre = resolve_strategy_params(
        {"symbol": "NEAR/USDT", "timeframe": "4h", "active": True}
    )
    members = finalize_trade_members(
        [near],
        open_symbols=set(),
        base_symbols={"NEAR/USDT"},
        config=_seed_cfg("shadow"),
    )
    row = members[0]
    assert row["source"] == "base"
    assert row["lane"] == "trade"
    assert row["bucket"] == "risk"
    assert row["profile"] == "volatile_altcoin"
    assert row["profile_applied"] is False
    params = row.get("strategy_params") or {}
    assert params.get("strategy_profile") != "volatile_altcoin"
    assert "rsi_buy_low" not in params
    post = resolve_strategy_params(row)
    assert post.get("rsi_buy_low") == pre.get("rsi_buy_low")
    assert post.get("rsi_buy_high") == pre.get("rsi_buy_high")
    expected = (
        "universe_member symbol=NEAR/USDT lane=trade source=base "
        "bucket=risk profile=volatile_altcoin profile_applied=false"
    )
    assert expected in logged


def test_enforce_applies_profile():
    near = {
        "symbol": "NEAR/USDT",
        "ticker": "NEAR",
        "timeframe": "4h",
        "active": True,
        "bucket": "risk",
    }
    members = finalize_trade_members(
        [near],
        open_symbols=set(),
        base_symbols={"NEAR/USDT"},
        config=_seed_cfg("enforce"),
    )
    row = members[0]
    assert row["profile_applied"] is True
    assert row["profile"] == "volatile_altcoin"
    params = row.get("strategy_params") or {}
    assert params.get("strategy_profile") == "volatile_altcoin"
    assert params.get("rsi_buy_low") == 28
    assert params.get("rsi_buy_high") == 48
    assert "dca" not in params


def test_open_lot_not_evicted_by_larger_seed(monkeypatch):
    from data_manager import load_trade_watchlist

    def _row(tick: str, bucket: str | None = None, **extra) -> dict:
        coin = {
            "symbol": f"{tick}/USDT",
            "ticker": tick,
            "timeframe": "4h",
            "active": True,
            **extra,
        }
        if bucket:
            coin["bucket"] = bucket
        return coin

    seed = [_row(t) for t in sorted(PIN_TICKERS)]
    seed += [_row(t, "quiet") for t in CORE_SEED_30_TICKERS]
    trending = [
        _row(f"T{i}", source="cmc_trending") for i in range(20)
    ]
    observe = seed + trending
    base_syms = {c["symbol"] for c in seed}
    cfg = _seed_cfg("shadow")
    cfg["universe"] = {
        "split_enabled": True,
        "trade_max_coins": 8,
        "trade_include_open_positions": True,
        "trade_include_base": True,
        "trade_rank_by": "as_is",
    }

    monkeypatch.setattr("data_manager.load_config", lambda tenant_id=None: cfg)
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: seed)
    monkeypatch.setattr(
        "services.universe.split._open_symbols_live", lambda: {"LAB/USDT"}
    )
    monkeypatch.setattr(
        "services.universe.split._quality_lookup",
        lambda tenant_id="default", **kw: {},
    )

    trade = load_trade_watchlist(
        observe_coins=observe,
        open_positions=[{"symbol": "LAB/USDT"}],
    )
    by_sym = {c["symbol"]: c for c in trade}
    lab = by_sym["LAB/USDT"]
    assert lab["source"] == "position"
    assert lab["lane"] == "trade"
    assert "LAB/USDT" in by_sym
    # seed + trending exceeded the watch cap; the open lot was not dropped
    assert len(observe) > 8
    assert all(s in by_sym for s in base_syms)


def test_open_lot_keeps_existing_cmc_trending_source():
    from strategies.registry import resolve_strategy_params

    coin = {
        "symbol": "WIF/USDT",
        "ticker": "WIF",
        "timeframe": "4h",
        "active": True,
        "source": "cmc_trending",
    }
    before = resolve_strategy_params(dict(coin), has_position=True)
    members = finalize_trade_members(
        [coin],
        open_symbols={"WIF/USDT"},
        base_symbols=set(),
        config=_seed_cfg("shadow"),
    )
    row = members[0]
    assert row["source"] == "cmc_trending"
    assert row["lane"] == "trade"
    after = resolve_strategy_params(row, has_position=True)
    assert after == before


def test_open_expansion_seed_lot_keeps_pre_seed_rsi():
    from strategies.registry import resolve_strategy_params

    seed_near = next(
        c for c in _repo_watchlist_coins() if ticker_of(c.get("symbol") or "") == "NEAR"
    )
    exp_near = next(
        c for c in _repo_expansion_coins() if ticker_of(c.get("symbol") or "") == "NEAR"
    )
    assert exp_near.get("source") == "dry_run_expansion"
    assert seed_near.get("bucket") == "risk"

    base_row = dict(seed_near)
    base_row["source"] = "base"
    cfg = _seed_cfg("shadow")
    merged = merge_preserving_open_lots(
        [base_row],
        [[dict(exp_near)]],
        open_symbols={"NEAR/USDT"},
        config=cfg,
    )
    trade = finalize_trade_members(
        merged,
        open_symbols={"NEAR/USDT"},
        base_symbols={"NEAR/USDT"},
        config=cfg,
    )
    row = next(c for c in trade if c["symbol"] == "NEAR/USDT")
    assert row["source"] == "dry_run_expansion"
    expected = resolve_strategy_params(
        dict(exp_near), has_position=True, atr_pct=6.0
    )
    got = resolve_strategy_params(row, has_position=True, atr_pct=6.0)
    assert got == expected


def test_open_trending_seed_lot_survives_overlay_refresh():
    from strategies.registry import resolve_strategy_params

    seed_avax = next(
        c for c in _repo_watchlist_coins() if ticker_of(c.get("symbol") or "") == "AVAX"
    )
    trending = {
        "symbol": "AVAX/USDT",
        "ticker": "AVAX",
        "timeframe": "1h",
        "active": True,
        "source": "cmc_trending",
    }
    base_row = dict(seed_avax)
    base_row["source"] = "base"
    cfg = _seed_cfg("shadow")
    # Overlay after refresh no longer contains the seed-name trending row.
    merged = merge_preserving_open_lots(
        [base_row],
        [[]],
        open_symbols={"AVAX/USDT"},
        open_lot_rows={"AVAX/USDT": dict(trending)},
        config=cfg,
    )
    assert all(c.get("symbol") != "AVAX/USDT" or c.get("source") != "base" for c in merged)
    trade = finalize_trade_members(
        merged,
        open_symbols={"AVAX/USDT"},
        open_lot_rows={"AVAX/USDT": dict(trending)},
        base_symbols={"AVAX/USDT"},
        config=cfg,
    )
    row = next(c for c in trade if c["symbol"] == "AVAX/USDT")
    assert row["source"] == "cmc_trending"
    expected = resolve_strategy_params(
        dict(trending), has_position=True, atr_pct=6.0
    )
    got = resolve_strategy_params(row, has_position=True, atr_pct=6.0)
    assert got == expected


def test_unread_open_set_keeps_expansion_overlay(monkeypatch):
    from data_manager import build_merged_watchlist_coins

    seed_near = next(
        c for c in _repo_watchlist_coins() if ticker_of(c.get("symbol") or "") == "NEAR"
    )
    exp_near = next(
        c for c in _repo_expansion_coins() if ticker_of(c.get("symbol") or "") == "NEAR"
    )
    assert exp_near.get("source") == "dry_run_expansion"
    base_row = dict(seed_near)
    base_row["source"] = "base"
    cfg = _seed_cfg("shadow")

    def _boom():
        raise RuntimeError("ledger down")

    logs: list[tuple[str, str]] = []
    monkeypatch.setattr("services.universe.split._open_symbols_live", _boom)
    monkeypatch.setattr(
        "data_manager.log", lambda msg, level="INFO": logs.append((str(msg), str(level)))
    )
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [base_row])
    monkeypatch.setattr("data_manager.uses_watchlist_expansion", lambda config=None: True)
    monkeypatch.setattr(
        "data_manager.load_dry_run_expansion", lambda: {"coins": [dict(exp_near)]}
    )
    monkeypatch.setattr("data_manager.is_dry_run_enhanced", lambda config=None: False)
    monkeypatch.setattr(
        "data_manager.trending_watchlist_live_enabled", lambda config=None: False
    )
    monkeypatch.setattr(
        "data_manager.prune_watchlist_coins_gate_only",
        lambda coins, **kw: (list(coins), []),
    )
    monkeypatch.setattr(
        "core.coin_eligibility.filter_watchlist_coins",
        lambda coins, cfg=None, **kw: list(coins),
    )

    merged = build_merged_watchlist_coins(config=cfg, apply_wqe=False)
    row = next(c for c in merged if c["symbol"] == "NEAR/USDT")
    assert row["source"] == "dry_run_expansion"
    assert any(
        level == "WARNING" and "open-position set unread" in msg for msg, level in logs
    )


def test_open_avax_trending_survives_cmc_refresh(monkeypatch, tmp_path):
    from unittest.mock import MagicMock, patch

    from core.config import BotConfig
    from data_manager import build_merged_watchlist_coins
    from services.dry_run_watchlist import TrendingWatchlistSync
    from strategies.registry import resolve_strategy_params

    seed_avax = next(
        c for c in _repo_watchlist_coins() if ticker_of(c.get("symbol") or "") == "AVAX"
    )
    trending = {
        "symbol": "AVAX/USDT",
        "ticker": "AVAX",
        "timeframe": "1h",
        "active": True,
        "source": "cmc_trending",
        "trending_rank": 3,
    }
    overlay_path = tmp_path / "watchlist.dry_run_overlay.json"
    overlay_path.write_text(
        json.dumps(
            {
                "refreshed_at": "2020-01-01T00:00:00",
                "source": "trending/latest",
                "coins": [dict(trending)],
            }
        ),
        encoding="utf-8",
    )
    bot_cfg = BotConfig()
    bot_cfg._raw = {
        "trading_mode": "live",
        "live": {
            "dry_run": True,
            "dry_run_enhanced": True,
            "simulated_balance_usdt": 5000,
            "trending_watchlist": {
                "enabled": True,
                "max_coins": 15,
                "refresh_hours": 0,
                "gate_only": True,
                "exclude_symbols": ["USDT", "USDC"],
            },
        },
        "cmc": {"api_key_env": "CMC_API_KEY"},
        "dry_run_defaults": {},
        "volatile_altcoin": {"timeframe": "1h"},
        **_seed_cfg("shadow"),
    }
    provider = MagicMock()
    provider.fetch_trending_symbols.return_value = (["PEPE"], "trending/latest")
    base_row = dict(seed_avax)
    base_row["source"] = "base"
    monkeypatch.setattr(
        "services.universe.split._open_symbols_live", lambda: {"AVAX/USDT"}
    )

    with patch("data_manager.get_data_file", return_value=str(overlay_path)), patch(
        "data_manager.load_watchlist", return_value=[base_row]
    ), patch("data_manager.is_dry_run_enhanced", return_value=True), patch(
        "services.dry_run_watchlist.CMCTrendingProvider", return_value=provider
    ), patch(
        "services.dry_run_watchlist.get_gate_prices_batch",
        return_value={"PEPE/USDT": 0.1},
    ), patch(
        "data_manager.prune_non_gate_watchlist_sources"
    ), patch(
        "telegram_notifier.send_telegram_message"
    ):
        saved = TrendingWatchlistSync(bot_cfg).sync_if_needed(force=True)

    avax_saved = next(c for c in saved.get("coins") or [] if c["symbol"] == "AVAX/USDT")
    assert avax_saved["source"] == "cmc_trending"

    cfg = _seed_cfg("shadow")
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [base_row])
    monkeypatch.setattr("data_manager.uses_watchlist_expansion", lambda config=None: False)
    monkeypatch.setattr("data_manager.is_dry_run_enhanced", lambda config=None: True)
    monkeypatch.setattr(
        "data_manager.trending_watchlist_live_enabled", lambda config=None: False
    )
    monkeypatch.setattr("data_manager.load_dry_run_overlay", lambda: saved)
    monkeypatch.setattr(
        "data_manager.prune_watchlist_coins_gate_only",
        lambda coins, **kw: (list(coins), []),
    )
    monkeypatch.setattr(
        "core.coin_eligibility.filter_watchlist_coins",
        lambda coins, cfg=None, **kw: list(coins),
    )

    merged = build_merged_watchlist_coins(
        config=cfg, apply_wqe=False, open_symbols={"AVAX/USDT"}
    )
    trade = finalize_trade_members(
        merged,
        open_symbols={"AVAX/USDT"},
        base_symbols={"AVAX/USDT"},
        config=cfg,
    )
    row = next(c for c in trade if c["symbol"] == "AVAX/USDT")
    expected = resolve_strategy_params(dict(trending), has_position=True, atr_pct=6.0)
    got = resolve_strategy_params(row, has_position=True, atr_pct=6.0)
    assert got == expected
