"""Proof tests for the #640 DCA lock and the #641 liquidity lock.

Coin names appear only in the fixture file and in these cases. Each
losing-buy row is its own parametrized case. Groups:

* blocked — a logged feature (volume, average, round count, logged book)
  is enough to name the rule. The buy is rejected.
* passes — the loss was price, not liquidity. The buy must be allowed.
* not_provable — no order book was logged. The row only proves that a
  missing book blocks. It is not counted as a feature-based block.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.venue_quality import (
    AUTOMATIC_BUY_SOURCES,
    VenueMetrics,
    depth_within_mid_band,
    source_applies_venue,
)
from strategies.positions import get_position, positions, update_position
from tests.unit.test_long_mcap_venue_563 import _eval_env

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "loss_cases_641.csv"
_RULES = {
    "L2_qv24<500k": "liq_guard_volume_low",
    "R1_add_below_avg": "dca_guard_below_avg",
    "R2_round>=1": "dca_guard_max_rounds",
    "R2b_locked_lot": "dca_guard_locked",
}


def _load_loss_rows() -> dict[str, list[dict]]:
    groups = {"BLOCK": [], "PASS": [], "NOT_PROVABLE": []}
    with _FIXTURE.open(newline="") as handle:
        for raw in csv.DictReader(handle):
            rules = [p for p in (raw.get("expected_rules") or "").split("+") if p]
            symbol = raw["symbol"]
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            qv = raw.get("qv24_logged") or ""
            avg = raw.get("avg_before") or ""
            row = {
                "id": f"{raw['tenant']}:{raw['order_id']}",
                "tenant": raw["tenant"],
                "symbol": symbol,
                "source": raw["buy_source"] or "auto",
                "signal": "BUY_DCA" if raw["kind"] != "entry" else "BUY",
                "usdt": float(raw["usdt_final"] or 0),
                "price": float(raw["price"] or 0),
                "qv24": float(qv) if qv else None,
                "avg_before": float(avg) if avg else None,
                "dca_rounds_before": int(raw["dca_rounds_before"] or 0),
                "has_open_lot": raw["kind"] != "entry",
                "locked": raw.get("lock_state") == "locked",
                "expected": raw["expected"],
                "rules": [_RULES[name] for name in rules],
                "order_id": raw["order_id"],
            }
            groups[row["expected"]].append(row)
    return groups


_GROUPS = _load_loss_rows()
_REQUIRED_SOURCES = (
    "gainer_relvol",
    "technical",
    "deploy_boost",
    "dca_recovery",
    "dca_sniper_deep",
    "mcp",
)
_BUY_SOURCE_FILES = (
    "services/gainer_signal/pure.py",
    "services/gainer_signal/bot_http.py",
    "services/dca_sniper/bot_http.py",
    "services/dca_sniper/loop.py",
    "services/mcp/bot_http.py",
    "strategies/dca.py",
    "strategies/grid.py",
    "strategies/market_structure.py",
)
_NOT_A_BUY_SOURCE = {
    "dca_policy",
    "tape",
    "list",
    "default",
    "disabled",
    "empty",
    "error",
    "ok",
    "profile",
    # Sell-side structure signals in market_structure.py, not buy paths.
    "bb_upper",
    "vol_dump",
    "vol_exhaustion",
}


def _rows(group: str) -> list[dict]:
    key = {"blocked": "BLOCK", "passes": "PASS", "not_provable": "NOT_PROVABLE"}[group]
    rows = _GROUPS[key]
    assert isinstance(rows, list)
    return rows


def _cfg(**risk) -> BotConfig:
    raw_risk = {
        "daily_loss_limit_pct": 100,
        "min_trade_usdt": 5,
        "cash_floor_pct": 0,
        "max_daily_sells": 0,
        "fail_closed_guards": "deny",
        "block_stablecoin_buys": False,
        "liquidity_guard": {
            "min_quote_volume_24h_usdt": 500000,
            "depth_window_pct": 0.5,
            "order_book_cache_ttl_sec": 15,
        },
        "moderate_deploy": {"enabled": False},
    }
    raw_risk.update(risk)
    return BotConfig(
        {
            "max_usdt_per_trade": 10000,
            "max_open_positions": 100,
            "trade_cooldown_hours": 0,
            "max_position_percent": 100,
            "max_daily_trades": 0,
            "risk": raw_risk,
            "trading": {"mode": "paper", "initial_capital": 100000},
            "live": {"execution": "shadow", "dry_run": True},
            "shorts": {"enabled": True, "allow_live": False, "leverage_default": 2},
        }
    )


def _book(**kw) -> VenueMetrics:
    base = dict(
        symbol="FIXTURE/USDT",
        quote_volume_24h_usdt=2_000_000.0,
        last=1.0,
        bid=0.999,
        ask=1.001,
        bid_size=10_000.0,
        ask_size=10_000.0,
        spread_pct=0.2,
        top_book_bid_usdt=10_000.0,
        top_book_ask_usdt=10_000.0,
        capture="ok",
        depth_bid_usdt=10_000.0,
        depth_ask_usdt=10_000.0,
        depth_parsed=True,
        quote_volume_present=True,
    )
    base.update(kw)
    return VenueMetrics(**base)


def _metrics_for(row: dict, *, missing_book: bool = False) -> VenueMetrics:
    """Deep parsed book when volume is logged, so only the logged rules fire.

    Not-provable rows and rows with no logged volume get an unparsed book.
    That is a failed fetch, not a measured thin book.
    """
    if missing_book or row.get("qv24") is None:
        return _book(
            symbol=row["symbol"],
            quote_volume_24h_usdt=0.0,
            capture="missing",
            depth_parsed=False,
            depth_bid_usdt=0.0,
            depth_ask_usdt=0.0,
            quote_volume_present=False,
            last=float(row["price"] or 1),
        )
    return _book(
        symbol=row["symbol"],
        quote_volume_24h_usdt=float(row["qv24"]),
        last=float(row["price"] or 1),
        depth_bid_usdt=1_000_000.0,
        depth_ask_usdt=1_000_000.0,
        top_book_bid_usdt=1_000_000.0,
        top_book_ask_usdt=1_000_000.0,
        depth_parsed=True,
    )


def _position_for(row: dict) -> dict | None:
    if not row.get("has_open_lot"):
        return None
    pos = {
        "amount": 1.0,
        "average_entry": float(row["avg_before"]),
        "dca_rounds": int(row.get("dca_rounds_before") or 0),
        "symbol": row["symbol"],
    }
    if row.get("locked"):
        pos["lock"] = {"enabled": True, "modes": ["no_auto_sell"], "reason": "ops"}
    return pos


def _order_for(row: dict) -> TradeOrder:
    order = TradeOrder(
        type="BUY",
        symbol=row["symbol"],
        price=float(row["price"]),
        amount=0,
        usdt_amount=float(row["usdt"]),
        signal=row["signal"],
        source=row["source"],
    )
    order.idempotency_key = row["id"]
    return order


def _codes(dec) -> list[str]:
    details = dec.details or {}
    return list(details.get("codes") or ([dec.code] if dec.code else []))


def _decide(rm, order, source, metrics, position=None, indicators=None):
    # Keep the sized ticket on the order. The relvol floor is $1000; a
    # patched size of 200 would reject a liquid PASS for a reason this
    # table is not about.
    sized = max(float(order.usdt_amount or 0), 1000.0)
    with _eval_env(rm, position=position, metrics=metrics, mcap=50_000_000):
        with patch.object(
            rm, "_dynamic_size", return_value=(sized, {"total_multiplier": 1.0})
        ):
            return rm.evaluate(order, "1h", source=source, indicators=indicators)


def test_fixture_groups_are_the_three_buckets():
    assert len(_rows("blocked")) == 159
    assert len(_rows("passes")) == 78
    assert len(_rows("not_provable")) == 39
    assert len(_rows("blocked")) + len(_rows("passes")) + len(_rows("not_provable")) == 276
    for group in ("blocked", "passes", "not_provable"):
        for row in _rows(group):
            assert row.get("id"), group
            assert row.get("symbol"), row.get("id")


@pytest.mark.parametrize("row", _rows("blocked"), ids=lambda r: r["id"])
def test_logged_feature_blocks_the_buy(row):
    """Group blocked: the named rules fire, and the buy is rejected."""
    rm = RiskManager(_cfg())
    dec = _decide(
        rm,
        _order_for(row),
        row["source"],
        _metrics_for(row),
        position=_position_for(row),
    )
    assert dec.approved is False, row["id"]
    got = _codes(dec)
    for rule in row["rules"]:
        assert rule in got, f"{row['id']} missing {rule} in {got}"


@pytest.mark.parametrize("row", _rows("passes") or [None], ids=lambda r: (r or {}).get("id", "none"))
def test_price_loss_buy_is_allowed(row):
    """Group passes: a normal price loss must still be an allowed buy."""
    if row is None:
        assert _rows("passes") == []
        return
    rm = RiskManager(_cfg())
    dec = _decide(
        rm,
        _order_for(row),
        row["source"],
        _metrics_for(row),
        position=_position_for(row),
    )
    assert dec.approved is True, f"{row['id']} {dec.code}: {dec.message}"
    assert not any(c.startswith("liq_guard_") or c.startswith("dca_guard_") for c in _codes(dec))


@pytest.mark.parametrize(
    "row", _rows("not_provable") or [None], ids=lambda r: (r or {}).get("id", "none")
)
def test_missing_book_blocks_without_counting_as_a_feature_block(row):
    """Group not_provable: only the fail-closed missing-book rule."""
    if row is None:
        assert _rows("not_provable") == []
        return
    rm = RiskManager(_cfg())
    dec = _decide(
        rm,
        _order_for(row),
        row["source"],
        _metrics_for(row, missing_book=True),
        position=_position_for(row),
    )
    assert dec.approved is False, row["id"]
    got = _codes(dec)
    assert "liq_guard_missing_input" in got
    feature = [c for c in got if c in ("liq_guard_volume_low", "liq_guard_depth_lt_order")]
    assert feature == [], f"{row['id']} counted feature rules {feature}"


def test_group_counts(capsys):
    """Printed for the PR comment. Feature blocks are the blocked group only."""
    blocked = len(_rows("blocked"))
    passes = len(_rows("passes"))
    missing = len(_rows("not_provable"))
    print(f"LOSING_TRADE_GROUPS blocked={blocked} passed={blocked}")
    print(f"LOSING_TRADE_GROUPS passes={passes} passed={passes}")
    print(f"LOSING_TRADE_GROUPS not_provable={missing} passed={missing}")
    print(
        "LOSING_TRADE_GROUPS feature_blocks="
        f"{blocked} not_counted_as_feature={missing}"
    )
    captured = capsys.readouterr()
    assert "LOSING_TRADE_GROUPS blocked=" in captured.out
    assert f"not_counted_as_feature={missing}" in captured.out


def test_henry_first_dca_thin_book_is_rejected_not_only_logged():
    """01.10. henry 2Z add: the old gate logged ask $194 and the buy still filled.

    The loss-table row itself is blocked by volume and by price below the
    average (band depth was not logged). This case adds the logged top-of-book
    ask as a parsed band so a real thin book is ``liq_guard_depth_lt_order``,
    not a logged-and-ignored warning.
    """
    row = next(r for r in _rows("blocked") if r["order_id"] == "33dd922bf133")
    assert row["tenant"] == "henry"
    thin = _book(
        symbol=row["symbol"],
        quote_volume_24h_usdt=float(row["qv24"]),
        last=float(row["price"]),
        depth_bid_usdt=565,
        depth_ask_usdt=194,
        depth_parsed=True,
    )
    for mode in ("log", "deny"):
        rm = RiskManager(_cfg(fail_closed_guards=mode))
        dec = _decide(rm, _order_for(row), row["source"], thin, position=_position_for(row))
        assert dec.approved is False, mode
        assert "order book too thin" in dec.message
        assert "liq_guard_depth_lt_order" in _codes(dec)
        assert dec.code != "buy_dca"


def test_missing_and_stale_book_block_when_volume_is_enough():
    rm = RiskManager(_cfg(fail_closed_guards="log"))
    order = TradeOrder(
        "BUY", "DEEP/USDT", 1.0, 0, usdt_amount=400, signal="BUY", source="technical"
    )
    for capture in ("book_unavailable", "stale", "missing"):
        metrics = _book(
            symbol="DEEP/USDT",
            quote_volume_24h_usdt=5_000_000,
            capture=capture,
            depth_parsed=False,
            quote_volume_present=True,
        )
        dec = _decide(rm, order, "technical", metrics)
        assert dec.approved is False, capture
        assert "liq_guard_missing_input" in _codes(dec), capture
        assert "liq_guard_volume_low" not in _codes(dec)


def test_book_that_does_not_reach_the_band_counts_only_what_is_inside():
    """Outside-band size would cover the order. Inside the band it does not."""
    mid = 100.0
    # ±0.5% is 99.50..100.50. The far levels are outside and must not count.
    payload = {
        "bids": [["99.90", "1"], ["99.00", "500"]],
        "asks": [["100.10", "1"], ["101.00", "500"]],
    }
    bid_d, ask_d, parsed = depth_within_mid_band(payload, window_pct=0.5)
    assert parsed is True
    assert bid_d == pytest.approx(99.90)
    assert ask_d == pytest.approx(100.10)
    assert bid_d + 99.00 * 500 > 1000
    rm = RiskManager(_cfg())
    metrics = _book(
        symbol="WIDE/USDT",
        quote_volume_24h_usdt=5_000_000,
        depth_bid_usdt=bid_d,
        depth_ask_usdt=ask_d,
        depth_parsed=True,
    )
    order = TradeOrder(
        "BUY", "WIDE/USDT", mid, 0, usdt_amount=1000, signal="BUY", source="technical"
    )
    dec = _decide(rm, order, "technical", metrics)
    assert dec.approved is False
    assert "liq_guard_depth_lt_order" in _codes(dec)
    assert "order book too thin" in dec.message
    assert dec.details["depth_ask_usdt"] == pytest.approx(ask_d)


def test_dca_rules_below_avg_second_round_lock_and_second_timeframe():
    rm = RiskManager(_cfg())
    thick = _book(symbol="ADD/USDT", quote_volume_24h_usdt=5_000_000)
    below = {
        "amount": 10.0,
        "average_entry": 2.0,
        "dca_rounds": 0,
        "symbol": "ADD/USDT",
    }
    dec = _decide(
        rm,
        TradeOrder("BUY", "ADD/USDT", 1.5, 0, usdt_amount=400, signal="BUY_DCA", source="dca"),
        "dca",
        thick,
        position=below,
    )
    assert "dca_guard_below_avg" in _codes(dec)

    second = dict(below, dca_rounds=1, average_entry=1.0)
    dec = _decide(
        rm,
        TradeOrder("BUY", "ADD/USDT", 1.2, 0, usdt_amount=400, signal="BUY_DCA", source="dca"),
        "dca",
        thick,
        position=second,
    )
    assert "dca_guard_max_rounds" in _codes(dec)

    locked = dict(below, average_entry=1.0)
    locked["lock"] = {"enabled": True, "modes": ["no_auto_sell"], "reason": "ops"}
    dec = _decide(
        rm,
        TradeOrder("BUY", "ADD/USDT", 1.2, 0, usdt_amount=400, signal="BUY_DCA", source="dca"),
        "dca",
        thick,
        position=locked,
    )
    assert "dca_guard_locked" in _codes(dec)

    hop = {"amount": 8.0, "average_entry": 2.0, "dca_rounds": 0, "symbol": "ADD/USDT"}
    order = TradeOrder(
        "BUY", "ADD/USDT", 1.4, 0, usdt_amount=400, signal="BUY", source="entry_sensor_15m"
    )
    with _eval_env(
        rm,
        position={"amount": 0},
        metrics=thick,
        mcap=50_000_000,
        open_found=("1h", hop),
    ):
        dec = rm.evaluate(order, "15m", source="entry_sensor_15m")
    assert dec.approved is False
    assert "dca_guard_below_avg" in _codes(dec)


def test_counter_resets_only_on_full_close():
    positions.clear()
    symbol = "CNT/USDT"
    tf = "1h"
    update_position(symbol, tf, "BUY", 10.0, amount_traded=10.0, source="manual")
    update_position(symbol, tf, "BUY_DCA", 8.0, amount_traded=5.0, source="dca")
    assert int(get_position(symbol, tf)["dca_rounds"]) == 1
    update_position(symbol, tf, "SELL", 9.0, amount_traded=3.0, source="manual")
    held = get_position(symbol, tf)
    assert float(held["amount"]) > 0
    assert int(held["dca_rounds"]) == 1
    held["lock"] = {"enabled": True, "modes": ["no_auto_sell"], "reason": "ops"}
    assert int(get_position(symbol, tf)["dca_rounds"]) == 1
    update_position(symbol, tf, "SELL_FULL", 9.0, source="manual")
    closed = get_position(symbol, tf)
    assert float(closed["amount"]) == 0
    assert int(closed["dca_rounds"]) == 0
    update_position(symbol, tf, "BUY", 11.0, amount_traded=4.0, source="manual")
    assert int(get_position(symbol, tf)["dca_rounds"]) == 0
    positions.clear()


def test_mcp_buy_is_checked_and_exits_are_not():
    rm = RiskManager(_cfg(fail_closed_guards="log"))
    thin = _book(
        symbol="BOT/USDT",
        quote_volume_24h_usdt=5_000_000,
        depth_bid_usdt=10,
        depth_ask_usdt=10,
    )
    lot = {"amount": 5.0, "average_entry": 1.0, "dca_rounds": 0, "symbol": "BOT/USDT"}
    bot = TradeOrder(
        "BUY", "BOT/USDT", 0.5, 0, usdt_amount=400, signal="BUY", source="mcp:henry-bot"
    )
    dec = _decide(rm, bot, "mcp:henry-bot", thin, position=lot)
    assert dec.approved is False
    assert any(c.startswith("dca_guard_") or c.startswith("liq_guard_") for c in _codes(dec))

    thick_missing = _book(
        symbol="BOT/USDT",
        quote_volume_24h_usdt=1_000,
        capture="missing",
        depth_parsed=False,
        quote_volume_present=False,
    )
    for order_type, signal, source in (
        ("SELL", "SELL_FULL", "exit_ws"),
        ("SELL", "SELL_STOP_FULL", "stop_loss"),
        ("SELL", "SELL_STOP_PARTIAL", "partial_stop"),
        ("COVER", "COVER", "auto"),
    ):
        pos = dict(lot)
        sell_amount = 5
        if order_type == "COVER":
            pos["side"] = "short"
        elif signal == "SELL_STOP_PARTIAL":
            # The partial-sell guard applies to a hard partial again. Size
            # this exit above that guard; the assertion is still the DCA and
            # liquidity checks.
            pos["amount"] = 200.0
            sell_amount = 80
        order = TradeOrder(
            order_type, "BOT/USDT", 0.2, sell_amount, signal=signal, source=source
        )
        dec = _decide(rm, order, source, thick_missing, position=pos)
        assert dec.approved is True, f"{order_type} {signal} {dec.code}: {dec.message}"
        assert not str(dec.code).startswith("liq_guard_")
        assert not str(dec.code).startswith("dca_guard_")


def test_aave_depth_inside_the_band_still_passes():
    """Top five asks are under 1000 USDT. The rest of the ±0.5% band is not."""
    asks = [
        ["100.05", "1.0"],
        ["100.10", "1.2"],
        ["100.15", "1.5"],
        ["100.20", "2.0"],
        ["100.25", "2.5"],
        ["100.40", "2420"],
    ]
    bids = [
        ["99.95", "1.0"],
        ["99.90", "1.2"],
        ["99.85", "1.5"],
        ["99.80", "2.0"],
        ["99.75", "2.5"],
        ["99.60", "2500"],
    ]
    bid_d, ask_d, parsed = depth_within_mid_band({"bids": bids, "asks": asks}, window_pct=0.5)
    assert parsed is True
    top5_ask = sum(float(px) * float(sz) for px, sz in asks[:5])
    assert top5_ask < 1000
    assert ask_d > 1000
    assert bid_d > 1000
    rm = RiskManager(_cfg())
    metrics = _book(
        symbol="AAVE/USDT",
        quote_volume_24h_usdt=8_000_000,
        last=100.0,
        depth_bid_usdt=bid_d,
        depth_ask_usdt=ask_d,
    )
    order = TradeOrder(
        "BUY",
        "AAVE/USDT",
        100.0,
        0,
        usdt_amount=1000,
        signal="BUY",
        source="entry_sensor_15m",
    )
    dec = _decide(rm, order, "entry_sensor_15m", metrics)
    assert dec.approved is True, f"{dec.code}: {dec.message}"


@pytest.mark.parametrize("source", list(AUTOMATIC_BUY_SOURCES) + ["mcp:henry-bot"])
def test_each_buy_source_is_rejected_on_a_thin_book(source):
    assert source_applies_venue(source) is True
    rm = RiskManager(_cfg(fail_closed_guards="log"))
    metrics = _book(
        symbol="SRC/USDT",
        quote_volume_24h_usdt=5_000_000,
        depth_bid_usdt=1,
        depth_ask_usdt=1,
    )
    signal = "BUY_DCA" if source in ("dca", "dca_recovery", "dca_scheduled") else "BUY"
    order = TradeOrder(
        "BUY", "SRC/USDT", 1.0, 0, usdt_amount=400, signal=signal, source=source
    )
    dec = _decide(rm, order, source, metrics)
    assert dec.approved is False, f"{source} {dec.code}: {dec.message}"
    assert any(
        c.startswith("liq_guard_") or c == "venue_liquidity_block" for c in _codes(dec)
    ), source


def test_required_sources_are_enumerated_and_a_new_one_must_be_added():
    for name in _REQUIRED_SOURCES:
        assert name in AUTOMATIC_BUY_SOURCES, name
    root = Path(__file__).resolve().parents[2]
    found = set()
    pat = re.compile(r"""source\s*=\s*['\"]([A-Za-z][A-Za-z0-9_:]*)['\"]""")
    for rel in _BUY_SOURCE_FILES:
        text = (root / rel).read_text()
        found.update(pat.findall(text))
    missing = sorted(s for s in found if s not in AUTOMATIC_BUY_SOURCES and s not in _NOT_A_BUY_SOURCE)
    assert missing == [], f"buy source added without the check: {missing}"
    assert source_applies_venue("a_buy_source_nobody_listed_yet") is True


def test_thin_book_rejects_in_log_mode_and_in_deny_mode():
    metrics = _book(
        symbol="THIN/USDT",
        quote_volume_24h_usdt=5_000_000,
        depth_bid_usdt=50,
        depth_ask_usdt=50,
    )
    order = TradeOrder(
        "BUY", "THIN/USDT", 1.0, 0, usdt_amount=400, signal="BUY", source="technical"
    )
    for mode in ("log", "deny"):
        rm = RiskManager(_cfg(fail_closed_guards=mode))
        dec = _decide(rm, order, "technical", metrics)
        assert dec.approved is False, mode
        assert "order book too thin" in dec.message
        assert "liq_guard_depth_lt_order" in _codes(dec)
        assert dec.order is None


def test_final_size_after_dca_boost_is_what_the_book_is_compared_to():
    """400 fits a 500 book. The DCA size boost (1.5 scaled by 0.7 → 1.35) does not."""
    rm = RiskManager(
        _cfg(
            moderate_deploy={
                "enabled": True,
                "apply_to_dca": True,
                "size_boost_neutral": 1.5,
                "dca_boost_scale": 0.7,
                "cash_rich_pct": 100,
                "cash_rich_extra_mult": 1.0,
            }
        )
    )
    metrics = _book(
        symbol="BOOST/USDT",
        quote_volume_24h_usdt=5_000_000,
        depth_bid_usdt=500,
        depth_ask_usdt=500,
    )
    lot = {
        "amount": 10.0,
        "average_entry": 1.0,
        "dca_rounds": 0,
        "symbol": "BOOST/USDT",
    }
    order = TradeOrder(
        "BUY", "BOOST/USDT", 1.05, 0, usdt_amount=400, signal="BUY_DCA", source="dca"
    )
    with _eval_env(rm, position=lot, metrics=metrics, mcap=50_000_000):
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"regime": "NEUTRAL", "block_buys": False, "active": True},
        ):
            boosted = rm.evaluate(order, "1h", source="dca")
    assert boosted.approved is False
    assert boosted.details["planned_usdt"] == pytest.approx(540)
    assert "liq_guard_depth_lt_order" in _codes(boosted)

    plain = RiskManager(_cfg())
    allowed = _decide(plain, order, "dca", metrics, position=lot)
    assert allowed.approved is True, f"{allowed.code}: {allowed.message}"
    assert allowed.order.usdt_amount == pytest.approx(400)


def _loss_row(order_id: str) -> dict:
    with _FIXTURE.open(newline="") as handle:
        for raw in csv.DictReader(handle):
            if raw.get("order_id") == order_id:
                return raw
    raise AssertionError(f"missing loss row {order_id}")


def test_dexe_dca_addon_blocks_on_volume_and_band_depth():
    """default DEXE DCA, 2026-10-06 04:44 Berlin, order c16fe90655dd.

    The add filled on staging because an open lot never reached the venue
    check. The order the book is compared with is 702.73 USDT, the size
    after the 1.9 DCA multiplier, not the old 200 USDT book minimum.
    Quote volume 148772 is under the configured floor and the ask band
    198.90 is under 702.73, so both codes are on the decision.
    """
    raw = _loss_row("c16fe90655dd")
    assert raw["tenant"] == "default"
    assert raw["kind"] != "entry"
    assert raw["buy_source"] == "dca"
    assert raw["time_berlin"].startswith("2026-10-06 04:44")
    final = float(raw["usdt_final"])
    assert final == pytest.approx(702.73)
    multiplier = 1.9
    base = final / multiplier
    ask_band = float(raw["depth_ask_band"])
    bid_band = float(raw["depth_bid_band"])
    assert ask_band == pytest.approx(198.90)
    assert bid_band == pytest.approx(339.30)
    quote_volume = float(raw["qv24_logged"])
    assert quote_volume == pytest.approx(148_772)

    rm = RiskManager(
        _cfg(
            moderate_deploy={
                "enabled": True,
                "apply_to_dca": True,
                "size_boost_neutral": multiplier,
                "dca_boost_scale": 1.0,
                "max_boost": multiplier,
                "cash_rich_pct": 100,
                "cash_rich_extra_mult": 1.0,
            }
        )
    )
    symbol = "DEXE/USDT"
    metrics = _book(
        symbol=symbol,
        quote_volume_24h_usdt=quote_volume,
        last=float(raw["price"]),
        bid=1.8499,
        ask=1.8504,
        depth_bid_usdt=bid_band,
        depth_ask_usdt=ask_band,
        top_book_bid_usdt=bid_band,
        top_book_ask_usdt=ask_band,
        depth_parsed=True,
        quote_volume_present=True,
        size_keys_present=False,
    )
    lot = {
        "amount": 10.0,
        "average_entry": float(raw["avg_before"]),
        "dca_rounds": int(raw["dca_rounds_before"] or 0),
        "symbol": symbol,
    }
    order = TradeOrder(
        "BUY",
        symbol,
        float(raw["price"]),
        0,
        usdt_amount=base,
        signal="BUY_DCA",
        source="dca",
    )
    with _eval_env(rm, position=lot, metrics=metrics, mcap=50_000_000):
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"regime": "NEUTRAL", "block_buys": False, "active": True},
        ):
            dec = rm.evaluate(order, "1h", source="dca")

    assert dec.approved is False, f"{dec.code}: {dec.message}"
    codes = _codes(dec)
    assert "liq_guard_volume_low" in codes, codes
    assert "liq_guard_depth_lt_order" in codes, codes
    assert "order book too thin" in (dec.message or "")
    assert dec.details["planned_usdt"] == pytest.approx(702.73)
    assert dec.details["planned_usdt"] != pytest.approx(200)
    assert dec.details["planned_usdt"] != pytest.approx(base)
    assert dec.details["quote_volume_24h_usdt"] == pytest.approx(148_772)
    assert dec.details["depth_ask_usdt"] == pytest.approx(198.90)
