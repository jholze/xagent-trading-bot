"""#641: a $0 book from a failed Gate fetch is not a thin book.

The historical "bid book $0" stamp came from the bulk ticker, which omits
``highest_size`` / ``lowest_size``. These cases use saved Gate bodies:

* ``gate_bulk_ticker_btc.json`` — live ``GET /spot/tickers`` row, no size keys
* ``gate_order_book_aave.json`` — live ``GET /spot/order_book`` body
* ``gate_error_invalid_pair.json`` — live HTTP 400 ``{label, message}``

A live HTTP 429 was not returned (a 220-call burst stayed 200). The rate-limit
case sends status 429 through ``requests.get`` with that same ``{label, message}``
object, so ``gate_public_rest_get`` handles it as a failed fetch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.venue_quality import (
    VenueMetrics,
    _book_cache_max_age,
    check_venue_for_buy,
    depth_within_mid_band,
    fetch_gate_venue_metrics,
    gate_public_rest_get,
    liquidity_guard_config,
    metrics_from_gate_ticker_row,
    reset_venue_cache_for_tests,
)
from tests.unit.test_liquidity_guard import _cfg
from tests.unit.test_long_mcap_venue_563 import _eval_env

_FIX = Path(__file__).resolve().parents[1] / "fixtures"
_BULK_BTC = json.loads((_FIX / "gate_bulk_ticker_btc.json").read_text())
_BOOK_AAVE = json.loads((_FIX / "gate_order_book_aave.json").read_text())
_ERR_400 = json.loads((_FIX / "gate_error_invalid_pair.json").read_text())


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _codes(result) -> list[str]:
    extra = list(getattr(result, "guard_codes", None) or [])
    if result.code and result.code not in extra:
        extra = [result.code] + extra
    return extra


def _decision_codes(dec) -> list[str]:
    details = dec.details or {}
    codes = list(details.get("codes") or [])
    if dec.code and dec.code not in codes:
        codes.insert(0, dec.code)
    return codes


@pytest.fixture(autouse=True)
def _clear_venue_cache():
    reset_venue_cache_for_tests()
    yield
    reset_venue_cache_for_tests()


def test_bulk_ticker_without_sizes_is_missing_input_not_a_thin_book():
    """Live bulk BTC row has quote volume and no size keys. That 0 is not depth."""
    assert "highest_size" not in _BULK_BTC
    assert "lowest_size" not in _BULK_BTC
    metrics = metrics_from_gate_ticker_row("BTC/USDT", _BULK_BTC)
    assert metrics.capture == "book_unavailable"
    assert metrics.size_keys_present is False
    assert metrics.top_book_bid_usdt == 0.0
    assert metrics.quote_volume_24h_usdt > 500_000
    assert metrics.depth_parsed is False

    result = check_venue_for_buy(
        "BTC/USDT",
        source="entry_sensor_15m",
        planned_usdt=1000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    codes = _codes(result)
    assert result.ok is False
    assert "liq_guard_missing_input" in codes
    assert "liq_guard_depth_lt_order" not in codes
    joined = "; ".join(result.reasons)
    assert "bid book $0" not in joined
    assert "order book too thin" not in joined


def test_real_aave_book_is_nonzero_logs_levels_and_a_normal_buy_passes():
    bid_d, ask_d, parsed = depth_within_mid_band(_BOOK_AAVE, window_pct=0.5)
    assert parsed is True
    assert bid_d > 1000
    assert ask_d > 1000
    assert bid_d != 0 and ask_d != 0

    logged: list[str] = []

    def _log(msg, level="INFO"):
        logged.append(str(msg))

    bulk = {
        "currency_pair": "AAVE_USDT",
        "last": "181.62",
        "highest_bid": "181.59",
        "lowest_ask": "181.66",
        "quote_volume": "8000000",
    }

    def fake(path, params=None, *, timeout=12.0):
        if path == "/spot/tickers" and not (params or {}).get("currency_pair"):
            return [bulk]
        if path == "/spot/order_book":
            assert (params or {}).get("currency_pair") == "AAVE_USDT"
            assert int((params or {}).get("limit") or 0) == 100
            return _BOOK_AAVE
        raise AssertionError(path)

    with patch("services.venue_quality.gate_public_rest_get", fake), patch(
        "services.venue_quality.log", _log
    ):
        got = fetch_gate_venue_metrics(
            ["AAVE/USDT"], config_raw=_cfg().raw, fetch_depth=True
        )
    metrics = got["AAVE/USDT"]
    assert metrics.depth_parsed is True
    assert metrics.depth_bid_usdt == pytest.approx(bid_d)
    assert metrics.depth_ask_usdt == pytest.approx(ask_d)
    prices = [price for _side, price, _sz in metrics.band_levels]
    assert 181.59 in prices
    assert 181.66 in prices
    assert any("venue_band_levels" in line and "bid@181.59" in line for line in logged)
    assert any("ask@181.66" in line for line in logged)
    stamp = metrics.to_stamp()
    assert {"side": "bid", "price": 181.59, "size": 1.358} in stamp["band_levels"]

    passed = check_venue_for_buy(
        "AAVE/USDT",
        source="technical",
        planned_usdt=1000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    assert passed.ok is True, passed.reasons

    thin = check_venue_for_buy(
        "AAVE/USDT",
        source="technical",
        planned_usdt=5000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    assert thin.ok is False
    assert "liq_guard_depth_lt_order" in _codes(thin)
    assert "liq_guard_missing_input" not in _codes(thin)
    assert any(str(r).startswith("order book too thin") for r in thin.reasons)


def test_explicit_zero_size_book_is_thin_not_a_failed_fetch():
    payload = {"bids": [["181.59", "0"]], "asks": [["181.66", "0"]]}
    bid_d, ask_d, parsed = depth_within_mid_band(payload, window_pct=0.5)
    assert parsed is True
    assert bid_d == 0.0 and ask_d == 0.0
    metrics = VenueMetrics(
        symbol="AAVE/USDT",
        quote_volume_24h_usdt=8_000_000,
        last=181.62,
        bid=181.59,
        ask=181.66,
        capture="ok",
        depth_bid_usdt=0.0,
        depth_ask_usdt=0.0,
        depth_parsed=True,
        quote_volume_present=True,
        size_keys_present=True,
    )
    result = check_venue_for_buy(
        "AAVE/USDT",
        source="technical",
        planned_usdt=1000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    assert "liq_guard_depth_lt_order" in _codes(result)
    assert "liq_guard_missing_input" not in _codes(result)
    assert any("order book too thin" in str(r) for r in result.reasons)


def test_unreadable_levels_are_a_failed_parse_not_a_thin_book():
    payload = {"bids": [{"p": "181.59", "s": "1"}], "asks": [{"p": "181.66", "s": "1"}]}
    bid_d, ask_d, parsed = depth_within_mid_band(payload, window_pct=0.5)
    assert parsed is False
    assert bid_d == 0.0 and ask_d == 0.0
    metrics = metrics_from_gate_ticker_row("BTC/USDT", _BULK_BTC)
    result = check_venue_for_buy(
        "BTC/USDT",
        source="technical",
        planned_usdt=1000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    assert "liq_guard_missing_input" in _codes(result)
    assert "liq_guard_depth_lt_order" not in _codes(result)


def test_live_400_body_raises_and_is_not_parsed_as_a_book():
    body = {"label": _ERR_400["label"], "message": _ERR_400["message"]}

    def fake_get(url, params=None, timeout=12.0):
        assert "/spot/order_book" in url
        return _Resp(400, body)

    with patch("requests.get", fake_get):
        with pytest.raises(RuntimeError, match="INVALID_CURRENCY_PAIR"):
            gate_public_rest_get(
                "/spot/order_book",
                {"currency_pair": "BTC/USDT", "limit": 100},
            )


def _rate_limit_router():
    calls: list[str] = []

    def fake_get(url, params=None, timeout=12.0):
        calls.append(url)
        if url.endswith("/spot/tickers") and not (params or {}).get("currency_pair"):
            return _Resp(200, [_BULK_BTC])
        return _Resp(
            429,
            {
                "label": "TOO_MANY_REQUESTS",
                "message": "Request Rate limit exceeded",
            },
        )

    return fake_get, calls


def test_rate_limit_blocks_the_buy_and_the_cycle_still_sells():
    """429 uses Gate's {label, message} object. The buy blocks; the sell still runs."""
    fake_get, calls = _rate_limit_router()
    rm = RiskManager(_cfg(fail_closed_guards="log"))
    buy = TradeOrder(
        "BUY", "BTC/USDT", 85000.0, 0, usdt_amount=1000, signal="BUY", source="technical"
    )
    sell = TradeOrder(
        "SELL", "BTC/USDT", 85000.0, 0.01, signal="SELL_FULL", source="exit_ws"
    )
    held = {"amount": 0.01, "average_entry": 80000.0, "dca_rounds": 0}
    decisions = []

    def live_metrics(symbol, *args, config_raw=None, force=False, fetch_depth=True, **kwargs):
        return fetch_gate_venue_metrics(
            [symbol], config_raw=config_raw, force=force, fetch_depth=fetch_depth
        ).get(symbol) or VenueMetrics(symbol=str(symbol), capture="missing")

    cycle = (
        (buy, "technical", {"amount": 0}),
        (sell, "exit_ws", held),
    )
    with patch("requests.get", fake_get):
        for order, source, position in cycle:
            with _eval_env(rm, position=position, mcap=1_000_000_000_000):
                with patch(
                    "services.venue_quality.get_venue_metrics",
                    side_effect=live_metrics,
                ):
                    decisions.append(rm.evaluate(order, "1h", source=source))

    assert len(decisions) == 2
    blocked, sold = decisions
    assert blocked.approved is False
    assert "liq_guard_missing_input" in _decision_codes(blocked)
    assert "liq_guard_depth_lt_order" not in _decision_codes(blocked)
    assert "order book too thin" not in (blocked.message or "")
    assert "bid book $0" not in (blocked.message or "")
    assert sold.approved is True, f"{sold.code}: {sold.message}"
    assert not str(sold.code or "").startswith("liq_guard_")
    assert any("order_book" in url for url in calls)


def test_cached_book_expires_at_the_configured_max_age():
    """Book age comes from config. A missing age does not fall back to 15s."""
    configured = {
        "risk": {
            "liquidity_guard": {
                "min_quote_volume_24h_usdt": 500000,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 15,
            }
        }
    }
    assert _book_cache_max_age(configured) == 15.0
    longer = {
        "risk": {
            "liquidity_guard": {
                "min_quote_volume_24h_usdt": 500000,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 10_000,
            }
        }
    }
    assert _book_cache_max_age(longer) == 10_000.0
    assert _book_cache_max_age({"risk": {}}) is None
    cfg = _cfg(
        venue_quality={
            "enabled": True,
            "cache_ttl_sec": 90,
            "order_book_limit": 100,
            "min_quote_volume_24h_usdt": 50_000,
            "min_top_book_usdt_per_side": 200,
            "book_unavailable_policy": "volume_ok",
        }
    ).raw
    fresh_levels = (("bid", 181.59, 10.0), ("ask", 181.66, 10.0))
    cached = VenueMetrics(
        symbol="AAVE/USDT",
        quote_volume_24h_usdt=8_000_000,
        last=181.62,
        bid=181.59,
        ask=181.66,
        capture="ok",
        depth_bid_usdt=50_000,
        depth_ask_usdt=50_000,
        depth_parsed=True,
        band_levels=fresh_levels,
        quote_volume_present=True,
        size_keys_present=True,
    )
    import services.venue_quality as vq

    vq._cache["AAVE/USDT"] = (time.time(), cached)
    vq._book_cache["AAVE_USDT"] = (
        time.time() - 30,
        (50_000.0, 50_000.0, True, fresh_levels),
    )

    def fail_book(path, params=None, *, timeout=12.0):
        if path == "/spot/order_book":
            raise RuntimeError("gate /spot/order_book HTTP 429 TOO_MANY_REQUESTS")
        if path == "/spot/tickers":
            return [
                {
                    "currency_pair": "AAVE_USDT",
                    "last": "181.62",
                    "highest_bid": "181.59",
                    "lowest_ask": "181.66",
                    "quote_volume": "8000000",
                }
            ]
        raise AssertionError(path)

    with patch("services.venue_quality.gate_public_rest_get", fail_book):
        stale = fetch_gate_venue_metrics(
            ["AAVE/USDT"], config_raw=cfg, fetch_depth=True
        )["AAVE/USDT"]
    assert stale.depth_parsed is False
    assert stale.depth_bid_usdt == 0.0
    assert stale.band_levels == ()
    blocked = check_venue_for_buy(
        "AAVE/USDT",
        source="technical",
        planned_usdt=1000,
        config_raw=cfg,
        metrics=stale,
    )
    assert "liq_guard_missing_input" in _codes(blocked)
    assert "liq_guard_depth_lt_order" not in _codes(blocked)

    reset_venue_cache_for_tests()
    vq._cache["AAVE/USDT"] = (time.time(), cached)
    vq._book_cache["AAVE_USDT"] = (
        time.time(),
        (50_000.0, 50_000.0, True, fresh_levels),
    )
    calls: list[str] = []

    def unexpected(path, params=None, *, timeout=12.0):
        calls.append(path)
        raise AssertionError(f"fresh book must not refetch {path}")

    with patch("services.venue_quality.gate_public_rest_get", unexpected):
        kept = fetch_gate_venue_metrics(
            ["AAVE/USDT"], config_raw=cfg, fetch_depth=True
        )["AAVE/USDT"]
    assert kept.depth_parsed is True
    assert kept.depth_bid_usdt == pytest.approx(50_000)
    assert calls == []


@pytest.mark.parametrize(
    "risk_over",
    [
        {"venue_quality": {"enabled": False, "book_unavailable_policy": "volume_ok", "on_fetch_error": "allow"}},
        {"fail_closed_guards": "log"},
        {
            "liquidity_guard": {
                "mode": "log",
                "enabled": False,
                "log_only": True,
                "min_quote_volume_24h_usdt": 1,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 15,
            }
        },
        {"venue_quality": {"enabled": True, "book_unavailable_policy": "volume_ok", "on_fetch_error": "fail_open"}},
    ],
)
def test_no_config_switch_makes_the_lock_log_only(risk_over):
    """The lock blocks on staging. enabled/policy/log/mode cannot pass a thin book."""
    cfg = liquidity_guard_config({"risk": {"liquidity_guard": {"mode": "log", "enabled": False}}})
    assert "mode" not in cfg
    assert "enabled" not in cfg
    assert cfg["min_quote_volume_24h_usdt"] is None
    assert cfg["complete"] is False

    thin = VenueMetrics(
        symbol="L3/USDT",
        quote_volume_24h_usdt=5_000_000,
        last=1.0,
        bid=0.999,
        ask=1.001,
        capture="ok",
        depth_bid_usdt=10.0,
        depth_ask_usdt=10.0,
        depth_parsed=True,
        quote_volume_present=True,
        size_keys_present=True,
    )
    rm = RiskManager(_cfg(**risk_over))
    order = TradeOrder(
        "BUY", "L3/USDT", 1.0, 0, usdt_amount=1000, signal="BUY", source="technical"
    )
    with _eval_env(rm, mcap=50_000_000):
        with patch("services.venue_quality.get_venue_metrics", return_value=thin):
            dec = rm.evaluate(order, "1h", source="technical")
    assert dec.approved is False
    assert "liq_guard_depth_lt_order" in _decision_codes(dec)
    assert "order book too thin" in (dec.message or "")
