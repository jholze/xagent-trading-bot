"""#581: missing Gate bulk ticker sizes are not a $0 book.

No live HTTP. Existing assertions in test_long_mcap_venue_563.py stay frozen.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import MarketContext, SignalAnalysis, TradeOrder
from risk.risk_manager import RiskManager
from services.venue_quality import (
    VenueMetrics,
    check_venue_for_buy,
    depth_from_gate_order_book,
    evaluate_venue_quality,
    fetch_gate_venue_metrics,
    get_venue_metrics,
    is_thin_venue_stamp,
    metrics_from_gate_ticker_row,
    reset_venue_cache_for_tests,
    stamp_venue_for_fill,
    venue_quality_config,
)

from services.watchlist_quality.venue_batch import attach_quote_volumes
from strategies.entry_sensor_15m import ENTRY_SENSOR_SOURCE, set_pending_sensor_metrics
from strategies import watch_15m_state

# Bound at import — the real bulk fetch, not the autouse offline stub in conftest.
_REAL_FETCH_GATE_VENUE_METRICS = fetch_gate_venue_metrics

_VENUE = {
    "enabled": True,
    "min_quote_volume_24h_usdt": 50_000,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200,
    "min_volume_to_order_multiple": 20,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "on_fetch_error": "block_sensor",
    "depth_levels": 5,
    "book_unavailable_policy": "volume_ok",
    "cache_ttl_sec": 90.0,
    "order_book_cache_ttl_sec": 15.0,
    "order_book_timeout_sec": 5.0,
}

_EMPTY_BOOK_OK = VenueMetrics(
    symbol="L3/USDT",
    quote_volume_24h_usdt=615_000.0,
    last=0.00350,
    bid=0.00349,
    ask=0.00351,
    bid_size=0.0,
    ask_size=0.0,
    spread_pct=0.31,
    top_book_bid_usdt=0.0,
    top_book_ask_usdt=0.0,
    capture="ok",
)

QNT_BULK = {
    "currency_pair": "QNT_USDT",
    "last": "99.92",
    "highest_bid": "99.90",
    "lowest_ask": "99.94",
    "quote_volume": "12000000",
    "base_volume": "120000",
}

# Best bid notional ~71; five-level sum > 200 on both sides.
QNT_BOOK = {
    "bids": [
        ["99.90", "0.711"],
        ["99.89", "0.50"],
        ["99.88", "0.40"],
        ["99.87", "0.30"],
        ["99.86", "0.25"],
        ["99.85", "10.0"],
    ],
    "asks": [
        ["99.94", "1.60"],
        ["99.95", "0.20"],
        ["99.96", "0.15"],
        ["99.97", "0.10"],
        ["99.98", "0.10"],
        ["99.99", "10.0"],
    ],
}

THIN_BOOK = {
    "bids": [["1.00", "10.0"], ["0.99", "10.0"], ["0.98", "10.0"], ["0.97", "10.0"], ["0.96", "10.0"]],
    "asks": [["1.01", "10.0"], ["1.02", "10.0"], ["1.03", "10.0"], ["1.04", "10.0"], ["1.05", "10.0"]],
}

EMPTY_BOOK = {"bids": [], "asks": []}

CFG_RAW = {"risk": {"venue_quality": dict(_VENUE)}}


@pytest.fixture(autouse=True)
def _reset_venue_cache():
    reset_venue_cache_for_tests()
    yield
    reset_venue_cache_for_tests()


def _cfg(**risk_over) -> BotConfig:
    risk = {
        "min_trade_usdt": 1,
        "cash_floor_pct": 0,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "moderate_deploy": {"enabled": False},
        "slot_eviction": {"enabled": False},
        "max_daily_loss_pct": 0,
        "venue_quality": dict(_VENUE),
        "fail_closed_guards": "deny",
    }
    risk.update(risk_over)
    raw = {
        "max_usdt_per_trade": 500,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "paper": {"initial_capital_usdt": 100_000},
        "risk": risk,
        "shorts": {},
        "architecture": {},
        "entry_sensor_15m": {"enabled": True, "mode": "active", "vol_spike_mult": 2.0},
    }
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


def _buy(
    symbol: str = "QNT/USDT",
    *,
    source: str = "gainer_relvol",
    signal: str = "GAINER_RELVOL",
    usdt: float = 500.0,
    price: float = 99.92,
) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source,
    )


@contextmanager
def _eval_env(
    rm: RiskManager,
    *,
    position: dict | None = None,
    mcap=2_000_000_000,
    extra=(),
    size: float = 200.0,
):
    pos = {"amount": 0} if position is None else position
    cap = SimpleNamespace(
        max_open_eff=100,
        enabled=False,
        rationale="",
        factors={},
        free_slots=100,
        regime=None,
    )
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch.object(rm, "_dynamic_size", return_value=(float(size), {"total_multiplier": 1.0}))
        )
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
        stack.enter_context(patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None))
        stack.enter_context(
            patch("services.correlated_tier.api.correlated_tier_selloff_active", return_value=False)
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "apply_size_mult": False, "active": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=mcap))
        for p in extra:
            stack.enter_context(p)
        yield


def _rest_router(bulk_rows, books=None, pair_rows=None, raise_on=None):
    books = books or {}
    pair_rows = pair_rows or {}
    raise_on = set(raise_on or ())
    calls: list[tuple[str, dict, float]] = []

    def fake(path, params=None, *, timeout=12.0):
        params = dict(params or {})
        calls.append((path, params, float(timeout)))
        if path in raise_on:
            raise RuntimeError(f"boom {path}")
        if path == "/spot/order_book":
            pair = params.get("currency_pair")
            if pair not in books:
                raise RuntimeError(f"no book for {pair}")
            return books[pair]
        if path == "/spot/tickers":
            pair = params.get("currency_pair")
            if pair:
                if pair not in pair_rows:
                    raise RuntimeError(f"no pair ticker for {pair}")
                return pair_rows[pair]
            return list(bulk_rows)
        raise RuntimeError(f"unexpected path {path}")

    return fake, calls


@contextmanager
def _http(fake):
    """Drive the real Gate fetch on top of conftest's offline get_venue_metrics stub."""

    def live_get(symbol, *args, config_raw=None, force=False, fetch_depth=True, **kwargs):
        return _REAL_FETCH_GATE_VENUE_METRICS(
            [symbol],
            config_raw=config_raw,
            force=force,
            fetch_depth=fetch_depth,
        ).get(symbol) or VenueMetrics(symbol=str(symbol), capture="missing")

    with patch("services.venue_quality.gate_public_rest_get", fake), patch(
        "services.venue_quality.get_venue_metrics",
        side_effect=live_get,
    ):
        yield


# --- 1. Bulk row without size keys ---


def test_bulk_row_missing_sizes_is_book_unavailable_not_zero_ok():
    m = metrics_from_gate_ticker_row("QNT/USDT", QNT_BULK)
    assert m.capture == "book_unavailable"
    assert m.size_keys_present is False
    assert m.quote_volume_24h_usdt == 12_000_000.0
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    joined = "; ".join(r.reasons)
    assert "bid book $0" not in joined
    assert "ask book $0" not in joined
    assert r.ok is True
    assert "book_unavailable_volume_ok" in r.reasons


def test_non_numeric_size_keys_are_book_unavailable():
    row = dict(QNT_BULK)
    row["highest_size"] = "n/a"
    row["lowest_size"] = "?"
    m = metrics_from_gate_ticker_row("QNT/USDT", row)
    assert m.capture == "book_unavailable"
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert "bid book $0" not in "; ".join(r.reasons)


def test_volume_ok_logs_warning(caplog):
    m = metrics_from_gate_ticker_row("QNT/USDT", QNT_BULK)
    import logging

    caplog.set_level(logging.WARNING)
    with patch("services.venue_quality.log") as mock_log:
        r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert r.ok is True
    messages = [str(c.args[0]) for c in mock_log.call_args_list]
    levels = [c.args[1] if len(c.args) > 1 else "" for c in mock_log.call_args_list]
    assert any("book_unavailable_volume_ok" in m for m in messages)
    assert any(lv == "WARNING" for lv in levels)


@pytest.mark.parametrize("policy", ["block", "volme_ok", "allow"])
def test_unrecognised_book_unavailable_policy_fail_closed(policy):
    """policy=block / typo / unknown never skips the book when volume is present."""
    cfg = dict(_VENUE)
    cfg["book_unavailable_policy"] = policy
    m = metrics_from_gate_ticker_row("QNT/USDT", QNT_BULK)
    assert m.capture == "book_unavailable"
    assert m.size_keys_present is False
    r = evaluate_venue_quality(m, cfg, planned_usdt=500)
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    assert "book_unavailable" in r.reasons
    assert "book_unavailable_volume_ok" not in r.reasons
    assert "bid book $0" not in "; ".join(r.reasons)

    raw = {"risk": {"venue_quality": dict(cfg)}}
    fake, _ = _rest_router([QNT_BULK], books={"QNT_USDT": EMPTY_BOOK})
    with _http(fake):
        r2 = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=raw,
        )
    assert r2.metrics is not None
    assert r2.metrics.size_keys_present is False
    assert r2.metrics.depth_parsed is False
    assert r2.metrics.capture == "book_unavailable"
    assert r2.metrics.quote_volume_24h_usdt >= 50_000
    assert r2.ok is False
    assert r2.code == "venue_liquidity_block"


def test_book_unavailable_volume_under_floor_is_liquidity_block():
    row = {
        "last": "1",
        "highest_bid": "1.0",
        "lowest_ask": "1.001",
        "quote_volume": "1000",
    }
    m = metrics_from_gate_ticker_row("JUNK/USDT", row)
    assert m.capture == "book_unavailable"
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    joined = "; ".join(r.reasons)
    assert "bid book $0" not in joined
    assert "quote_vol" in joined


def test_book_unavailable_no_volume_field_is_book_unavailable_code():
    row = {
        "last": "1",
        "highest_bid": "1.0",
        "lowest_ask": "1.001",
    }
    m = metrics_from_gate_ticker_row("X/USDT", row)
    assert m.capture == "book_unavailable"
    assert m.quote_volume_present is False
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert r.ok is False
    assert r.code == "book_unavailable"
    assert "book_unavailable" in r.reasons
    assert "bid book $0" not in "; ".join(r.reasons)


def test_sizes_present_missing_quote_volume_is_liquidity_block():
    row = {
        "last": "1",
        "highest_bid": "1.0",
        "lowest_ask": "1.001",
        "highest_size": "1000",
        "lowest_size": "1000",
    }
    m = metrics_from_gate_ticker_row("Y/USDT", row)
    assert m.size_keys_present is True
    assert m.quote_volume_present is False
    assert m.capture == "ok"
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    assert "quote_vol_24h unavailable" in r.reasons
    assert "book_unavailable" not in r.reasons


def test_missing_quote_volume_with_parsed_depth_above_floor_is_liquidity_block():
    bulk = {
        "currency_pair": "X_USDT",
        "last": "99.92",
        "highest_bid": "99.90",
        "lowest_ask": "99.94",
    }
    fake, _ = _rest_router([bulk], books={"X_USDT": QNT_BOOK})
    with _http(fake):
        r = check_venue_for_buy(
            "X/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.metrics is not None
    assert r.metrics.quote_volume_present is False
    assert r.metrics.size_keys_present is False
    assert r.metrics.depth_parsed is True
    assert r.metrics.depth_bid_usdt > 200
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    assert "quote_vol_24h unavailable" in r.reasons
    assert r.code != "book_unavailable"


def test_capture_missing_does_not_use_volume_ok():
    m = VenueMetrics(symbol="ZZZ/USDT", capture="missing", quote_volume_24h_usdt=12_000_000)
    r = evaluate_venue_quality(m, _VENUE, planned_usdt=500)
    assert r.ok is False
    assert r.reasons == ["venue_capture_missing"]
    r2 = check_venue_for_buy(
        "ZZZ/USDT",
        source="gainer_relvol",
        planned_usdt=500,
        config_raw=CFG_RAW,
        metrics=m,
    )
    # Passed-in missing metrics evaluate as capture-missing, not fetch-failed.
    assert r2.ok is False
    assert "venue_capture_missing" in r2.reasons


def test_frozen_empty_book_capture_ok_stays_thin():
    r = evaluate_venue_quality(_EMPTY_BOOK_OK, _VENUE, planned_usdt=200)
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    assert any("bid book $0" in x for x in r.reasons)


# --- 2. QNT replay ---


def test_qnt_replay_order_book_passes_gate_and_risk():
    fake, calls = _rest_router([QNT_BULK], books={"QNT_USDT": QNT_BOOK})
    rm = _cfg()
    rm._raw["max_usdt_per_trade"] = 1000
    manager = RiskManager(rm)
    with _http(fake), _eval_env(manager, size=1000.0):
        vres = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
        dec = manager.evaluate(_buy(), "4h", source="gainer_relvol")
    assert vres.ok is True, vres.reasons
    assert dec.approved is True, f"{dec.code}: {dec.message}"
    assert dec.code != "venue_liquidity_block"
    assert any(p == "/spot/order_book" for p, _, _ in calls)


# --- 3. Real thin book ---


def test_real_thin_book_and_low_volume_is_liquidity_block_with_measured_notional():
    bulk = {
        "currency_pair": "JUNK_USDT",
        "last": "1.00",
        "highest_bid": "1.00",
        "lowest_ask": "1.01",
        "quote_volume": "1000",
    }
    fake, _ = _rest_router([bulk], books={"JUNK_USDT": THIN_BOOK})
    with _http(fake):
        r = check_venue_for_buy(
            "JUNK/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    joined = "; ".join(r.reasons)
    assert "bid book $0" not in joined
    assert "ask book $0" not in joined
    assert "bid book $" in joined or "quote_vol" in joined
    m = r.metrics
    assert m is not None
    assert m.depth_parsed is True
    # ±0.5% of mid keeps only the touch on this book (~10 USDT), not the
    # top-five sum of ~49.
    assert m.depth_bid_usdt == pytest.approx(10.0, rel=0.05)


# --- 4. One touch under 200, depth over 200 ---


def test_one_touch_under_floor_depth_over_floor_passes():
    bulk = dict(QNT_BULK)
    bulk["highest_size"] = "0.711"  # ~71 USDT one-touch
    bulk["lowest_size"] = "0.50"
    fake, _ = _rest_router([bulk], books={"QNT_USDT": QNT_BOOK})
    with _http(fake):
        r = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.ok is True, r.reasons
    assert r.metrics is not None
    assert r.metrics.depth_parsed is True
    assert r.metrics.depth_bid_usdt > 200
    assert r.metrics.top_book_bid_usdt < 200


def test_depth_parser_sums_top_five_of_twenty():
    payload = {
        "bids": [[str(100 - i * 0.01), "1.0"] for i in range(20)],
        "asks": [[str(100 + i * 0.01), "1.0"] for i in range(20)],
    }
    bid_d, ask_d, parsed = depth_from_gate_order_book(payload, depth_levels=5)
    assert parsed is True
    assert bid_d == pytest.approx(sum(100 - i * 0.01 for i in range(5)))
    assert ask_d == pytest.approx(sum(100 + i * 0.01 for i in range(5)))
    assert bid_d < sum(100 - i * 0.01 for i in range(20))


def test_empty_order_book_lists_are_unparsed_volume_fallback_applies():
    bid_d, ask_d, parsed = depth_from_gate_order_book(EMPTY_BOOK, depth_levels=5)
    assert parsed is False
    assert bid_d == 0.0
    assert ask_d == 0.0
    fake, _ = _rest_router([QNT_BULK], books={"QNT_USDT": EMPTY_BOOK})
    with _http(fake):
        r = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.metrics is not None
    assert r.metrics.depth_parsed is False
    assert r.metrics.capture == "book_unavailable"
    # #641: a missing band is not a pass, even when 24h volume is fine.
    assert r.ok is False
    assert r.code == "liq_guard_missing_input"
    joined = "; ".join(r.reasons)
    assert "bid book $0" not in joined
    assert "ask book $0" not in joined


def test_present_zero_size_levels_are_parsed_zero_not_empty():
    payload = {"bids": [["1.00", "0"]], "asks": [["1.01", "0"]]}
    bid_d, ask_d, parsed = depth_from_gate_order_book(payload, depth_levels=5)
    assert parsed is True
    assert bid_d == 0.0
    assert ask_d == 0.0


# --- 5. Explicit zero size on present keys ---


def test_explicit_zero_size_with_empty_book_and_low_volume_is_thin():
    bulk = {
        "currency_pair": "Z_USDT",
        "last": "1.0",
        "highest_bid": "1.0",
        "lowest_ask": "1.001",
        "highest_size": "0",
        "lowest_size": "0",
        "quote_volume": "1000",
    }
    fake, _ = _rest_router([bulk], books={"Z_USDT": EMPTY_BOOK})
    m = metrics_from_gate_ticker_row("Z/USDT", bulk)
    assert m.capture == "ok"
    assert m.size_keys_present is True
    with _http(fake):
        r = check_venue_for_buy(
            "Z/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.ok is False
    assert r.code == "venue_liquidity_block"
    assert r.code != "book_unavailable"


# --- 6. HTTP / pair missing ---


def test_http_failure_is_fetch_failed_block_not_zero_book():
    fake, _ = _rest_router([], raise_on={"/spot/tickers"})
    with _http(fake):
        r = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.ok is False
    assert r.reasons == ["venue_fetch_failed_block"]
    assert "bid book $0" not in "; ".join(r.reasons)

    rm = RiskManager(_cfg())
    with _http(fake), _eval_env(rm):
        dec = rm.evaluate(_buy(), "4h", source="gainer_relvol")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"
    assert "venue_fetch_failed_block" in dec.message
    assert "bid book $0" not in dec.message


def test_pair_missing_from_bulk_is_fetch_failed_block():
    fake, _ = _rest_router([{"currency_pair": "BTC_USDT", "last": "1", "quote_volume": "9"}])
    with _http(fake):
        r = check_venue_for_buy(
            "QNT/USDT",
            source="gainer_relvol",
            planned_usdt=500,
            config_raw=CFG_RAW,
        )
    assert r.ok is False
    assert r.reasons == ["venue_fetch_failed_block"]


# --- 7. Cache ---


def test_missing_sizes_not_cached_as_zero_book_when_depth_resolves():
    fake, calls = _rest_router([QNT_BULK], books={"QNT_USDT": QNT_BOOK})
    with _http(fake):
        first = get_venue_metrics("QNT/USDT", config_raw=CFG_RAW, fetch_depth=True)
        book_calls_after_first = sum(1 for p, _, _ in calls if p == "/spot/order_book")
        second = get_venue_metrics("QNT/USDT", config_raw=CFG_RAW, fetch_depth=True)
        book_calls_after_second = sum(1 for p, _, _ in calls if p == "/spot/order_book")
    assert first.capture == "ok"
    assert first.depth_parsed is True
    assert first.depth_bid_usdt > 200
    assert second.depth_bid_usdt == first.depth_bid_usdt
    assert book_calls_after_first == 1
    assert book_calls_after_second == 1


# --- 8. Sells never call the gate ---


def test_sell_never_calls_check_venue_for_buy():
    rm = RiskManager(_cfg())
    order = TradeOrder(
        type="SELL",
        symbol="QNT/USDT",
        price=100.0,
        amount=1.0,
        signal="SELL_FULL",
        source="auto",
    )
    with patch("services.venue_quality.check_venue_for_buy") as spy, patch.object(
        rm, "_trade_cooldown_blocked", return_value=(False, "")
    ), patch.object(rm, "_resolve_sell_order", return_value=order), patch.object(
        rm, "_partial_sell_blocked", return_value=(False, "")
    ), patch.object(rm, "_effective_max_daily_sells", return_value=0), patch.object(
        rm, "_daily_sells_count", return_value=0
    ):
        dec = rm.evaluate(order, "1h", source="auto")
    assert spy.call_count == 0
    assert dec.approved is True
    assert dec.code != "venue_liquidity_block"


# --- 9. DCA and manual still exempt ---


def test_dca_and_manual_still_exempt():
    rm = RiskManager(_cfg())
    with _eval_env(rm, extra=[
        patch("services.venue_quality.get_venue_metrics", return_value=_EMPTY_BOOK_OK)
    ]):
        manual = rm.evaluate(_buy(source="manual", signal="BUY"), "4h", source="manual")
    assert manual.approved is True, f"{manual.code}: {manual.message}"
    assert manual.code not in ("venue_liquidity_block", "book_unavailable")

    dca_pos = {"amount": 2.0, "average_entry": 1.0, "dca_rounds": 0}
    with _eval_env(rm, position=dca_pos, extra=[
        patch("services.venue_quality.get_venue_metrics", return_value=_EMPTY_BOOK_OK)
    ]):
        dca = rm.evaluate(
            _buy(source="dca", signal="BUY_DCA", usdt=200, price=1.0),
            "4h",
            source="dca",
        )
    # Price equals average, so the below-avg rule passes. The empty book
    # does not: adds are no longer exempt from the liquidity lock.
    assert dca.approved is False
    assert dca.code in ("liq_guard_missing_input", "venue_liquidity_block")


def test_gainer_relvol_not_exempt_on_real_empty_book():
    rm = RiskManager(_cfg())
    with _eval_env(rm, extra=[
        patch("services.venue_quality.get_venue_metrics", return_value=_EMPTY_BOOK_OK)
    ]):
        dec = rm.evaluate(_buy(symbol="L3/USDT", usdt=200, price=1.0), "4h", source="gainer_relvol")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"


# --- 10. Fill stamp ---


def test_qnt_fill_stamp_is_not_ok_with_zeros_and_not_thin():
    fake, _ = _rest_router([QNT_BULK], books={"QNT_USDT": QNT_BOOK})
    with _http(fake):
        stamp = stamp_venue_for_fill("QNT/USDT", planned_usdt=500, config_raw=CFG_RAW)
    assert stamp.get("capture") != "missing"
    zeros = stamp.get("capture") == "ok" and float(stamp.get("top_book_bid_usdt") or 0) == 0 and float(
        stamp.get("depth_bid_usdt") or 0
    ) == 0
    assert zeros is False
    assert is_thin_venue_stamp(stamp, _VENUE) is False


def test_truly_thin_stamp_stays_thin():
    stamp = _EMPTY_BOOK_OK.to_stamp(planned_usdt=200, venue_ok=False, reasons=["bid book $0 < min $200"])
    assert is_thin_venue_stamp(stamp, _VENUE) is True
    high_vol_unavailable = {
        "capture": "book_unavailable",
        "quote_volume_24h_usdt": 12_000_000,
        "venue_ok": True,
        "top_book_bid_usdt": 0,
        "top_book_ask_usdt": 0,
        "planned_usdt": 500,
    }
    assert is_thin_venue_stamp(high_vol_unavailable, _VENUE) is False


# --- 11. Watchlist batch ---


def test_attach_quote_volumes_fills_from_bulk_without_size_and_skips_book_http():
    fake, calls = _rest_router([QNT_BULK], books={"QNT_USDT": QNT_BOOK})
    with _http(fake):
        defaulted = fetch_gate_venue_metrics(["QNT/USDT"], config_raw=CFG_RAW)
        out = attach_quote_volumes([{"symbol": "QNT/USDT"}], config=CFG_RAW)
    assert defaulted["QNT/USDT"].quote_volume_24h_usdt == 12_000_000.0
    assert defaulted["QNT/USDT"].capture == "book_unavailable"
    assert defaulted["QNT/USDT"].depth_parsed is False
    assert out[0]["quote_vol_24h"] == 12_000_000.0
    assert not any(p == "/spot/order_book" for p, _, _ in calls)
    ticker_calls = [c for c in calls if c[0] == "/spot/tickers"]
    assert ticker_calls
    assert all(not c[1].get("currency_pair") for c in ticker_calls)


# --- 12. Decision engine vs risk ---


def test_sensor_and_risk_agree_on_qnt_fixture():
    fake, _ = _rest_router([QNT_BULK], books={"QNT_USDT": QNT_BOOK})
    cfg = _cfg()
    cfg._raw["max_usdt_per_trade"] = 1000
    rm = RiskManager(cfg)
    with _http(fake), _eval_env(rm, size=1000.0):
        sensor = check_venue_for_buy(
            "QNT/USDT",
            source=ENTRY_SENSOR_SOURCE,
            planned_usdt=500 * 0.35,
            config_raw=CFG_RAW,
        )
        dec = rm.evaluate(_buy(), "4h", source="gainer_relvol")
    assert sensor.ok is True
    assert dec.approved is True, f"{dec.code}: {dec.message}"


def test_sensor_exception_fail_open_risk_exception_fail_closed():
    from strategies.decision_engine import DecisionEngine

    boom = RuntimeError("guard-boom")
    engine = DecisionEngine()
    engine.config = _cfg()
    watch_15m_state.set_watch("QNT/USDT", "4h", rsi_4h=40.0)
    set_pending_sensor_metrics(
        "QNT/USDT",
        {"volume_spike_ratio": 3.5, "body_atr_ratio": 0.5, "price_momentum": True},
    )
    market = MarketContext(
        symbol="QNT/USDT",
        timeframe="4h",
        current_price=99.92,
        rsi=40.0,
        lower_bb=90.0,
        has_position=False,
        strategy_params={"trading_mode": "HYBRID"},
    )
    technical = SignalAnalysis(
        action="HOLD",
        symbol="QNT/USDT",
        timeframe="4h",
        rsi=40.0,
        lower_bb=90.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        confidence=0.5,
    )
    with patch("services.venue_quality.check_venue_for_buy", side_effect=boom), patch(
        "data.cmc_market_cap.resolve_market_cap_usd", return_value=2_000_000_000
    ), patch("price_fetcher.is_gate_tradeable", return_value=True), patch(
        "strategies.decision_engine.get_bot_config", return_value=engine.config
    ):
        out = engine._apply_entry_sensor_buy("HOLD", [], 0.5, "QNT/USDT", market, technical)
    assert out is not None

    rm = RiskManager(_cfg(fail_closed_guards="deny"))
    with _eval_env(
        rm,
        extra=[patch("services.venue_quality.check_venue_for_buy", side_effect=boom)],
    ):
        dec = rm.evaluate(_buy(), "4h", source="gainer_relvol")
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"


def test_defaults_include_depth_and_volume_ok_policy():
    cfg = venue_quality_config({"risk": {"venue_quality": {}}})
    assert cfg["depth_levels"] == 5
    assert cfg["book_unavailable_policy"] == "volume_ok"
    assert cfg["min_quote_volume_24h_usdt"] == 50_000
    assert cfg["min_top_book_usdt_per_side"] == 200
    assert cfg["exempt_sources"] == ["manual"]


def test_book_unavailable_risk_code_when_volume_and_depth_absent():
    m = metrics_from_gate_ticker_row(
        "X/USDT",
        {"highest_bid": "1", "lowest_ask": "1.001"},
    )
    rm = RiskManager(_cfg())
    with _eval_env(
        rm,
        extra=[patch("services.venue_quality.get_venue_metrics", return_value=m)],
    ):
        dec = rm.evaluate(_buy(symbol="X/USDT", usdt=200, price=1.0), "4h", source="gainer_relvol")
    assert dec.approved is False
    assert dec.code == "book_unavailable"
    assert "bid book $0" not in dec.message
