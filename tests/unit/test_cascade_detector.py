"""#564 cascade detector: window math, sign split, floor, cold-start, fixture replay."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from services.exit_realtime.cascade_detector import CascadeDetector
from services.exit_realtime.cascade_state import CascadeState
from services.exit_realtime.config import CASCADE_DEFAULTS, cascade_config
from services.exit_realtime.liq_stream import (
    SIDE_LONG,
    SIDE_SHORT,
    parse_liq_batch,
    parse_liq_event,
)
from strategies.position_lock import is_manual_source
from strategies.sell_sources import LIQ_CASCADE_SOURCE, STOP_SOURCES

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "liq_cascade_2026_09_23.json"


def _cfg(**over):
    base = {
        "window_sec": 300,
        "baseline_sec": 3600,
        "min_baseline_samples_sec": 1800,
        "multiplier": 3.0,
        "min_notional_usd": 100000,
        "log_soft_signal": False,
    }
    base.update(over)
    return base


def _ev(ts_ms: int, usd, side: str = SIDE_LONG):
    from services.exit_realtime.liq_stream import ParsedLiq

    size = Decimal("1") if side == SIDE_LONG else Decimal("-1")
    return ParsedLiq(
        ts_ms=ts_ms,
        side=side,
        usd=Decimal(str(usd)),
        contract="BTC_USDT",
        size=size,
        price=Decimal("1"),
    )


def _quiet_baseline(det: CascadeDetector, now_ms: int, usd: float = 50000, side: str = SIDE_LONG):
    """12 five-minute prints filling the exclusive 60m baseline."""
    window_start = now_ms - 300_000
    baseline_start = window_start - 3_600_000
    for i in range(12):
        ts = baseline_start + i * 300_000
        det.ingest([_ev(ts, usd, side)])


class TestCascadeConfig:
    def test_defaults(self):
        cfg = cascade_config({})
        assert cfg["enabled"] is True
        assert cfg["fire_enabled"] is False
        assert cfg["min_notional_usd"] == 100000
        assert cfg["multiplier"] == 3.0
        assert cfg["calibrated"] is False
        assert cfg["ws_url"] == CASCADE_DEFAULTS["ws_url"]
        assert cfg["payload"] == "!all"

    def test_existing_exit_realtime_keys_untouched(self):
        raw = {
            "exit_realtime": {
                "enabled": True,
                "mode": "live",
                "owner": "bot",
                "sources": ["trailing_take_profit"],
                "cascade": {"fire_enabled": False, "min_notional_usd": 100000},
            }
        }
        from services.exit_realtime.config import exit_realtime_config

        block = exit_realtime_config(raw)
        assert block["enabled"] is True
        assert block["mode"] == "live"
        assert block["owner"] == "bot"
        assert block["sources"] == ["trailing_take_profit"]
        assert cascade_config(raw)["min_notional_usd"] == 100000

    def test_liq_cascade_not_stop_or_manual(self):
        assert LIQ_CASCADE_SOURCE not in STOP_SOURCES
        assert not is_manual_source(LIQ_CASCADE_SOURCE)


class TestLiqParser:
    def test_positive_size_is_long_dump(self):
        ev = parse_liq_event(
            {"contract": "BTC_USDT", "size": "10.5", "price": "87000", "time": 1_000_000},
            quanto_btc=0.0001,
        )
        assert ev is not None
        assert ev.side == SIDE_LONG
        assert ev.usd == Decimal("10.5") * Decimal("0.0001") * Decimal("87000")

    def test_negative_size_is_short_pump(self):
        ev = parse_liq_event(
            {"contract": "BTC_USDT", "size": "-3", "price": "87000", "time": 1_000_000},
            quanto_btc=0.0001,
        )
        assert ev is not None
        assert ev.side == SIDE_SHORT

    def test_decimal_string_and_zero_drop(self):
        assert parse_liq_event({"contract": "BTC_USDT", "size": "0", "price": "1", "time": 1}) is None
        batch = parse_liq_batch(
            {
                "event": "update",
                "result": [
                    {"contract": "BTC_USDT", "size": "1.25", "price": "100", "time": 50, "x": 1},
                    {"contract": "BTC_USDT", "size": "1.25", "price": "100", "time": 50, "x": 2},
                ],
            },
            quanto_btc=1,
        )
        assert len(batch) == 1

    def test_mixed_batch_no_netting(self):
        rows = parse_liq_batch(
            [
                {"contract": "BTC_USDT", "size": "2", "price": "100", "time": 10},
                {"contract": "BTC_USDT", "size": "-3", "price": "100", "time": 11},
            ],
            quanto_btc=1,
        )
        long_usd = sum((e.usd for e in rows if e.side == SIDE_LONG), Decimal("0"))
        short_usd = sum((e.usd for e in rows if e.side == SIDE_SHORT), Decimal("0"))
        assert long_usd == Decimal("200")
        assert short_usd == Decimal("300")

    def test_unknown_quanto_eth_usdt_dropped(self):
        row = {
            "contract": "ETH_USDT",
            "size": "100",
            "price": "3000",
            "time": 1_000_000,
        }
        assert parse_liq_event(row, quanto_btc=0.0001) is None
        rows = parse_liq_batch(
            [
                row,
                {"contract": "BTC_USDT", "size": "2", "price": "100000", "time": 10},
            ],
            quanto_btc=0.0001,
        )
        long_usd = sum((e.usd for e in rows if e.side == SIDE_LONG), Decimal("0"))
        short_usd = sum((e.usd for e in rows if e.side == SIDE_SHORT), Decimal("0"))
        assert all(e.contract == "BTC_USDT" for e in rows)
        assert long_usd == Decimal("2") * Decimal("0.0001") * Decimal("100000")
        assert short_usd == Decimal("0")
        det = CascadeDetector(_cfg())
        det.ingest(parse_liq_batch([row], quanto_btc=0.0001))
        snaps = det.evaluate(1_000_000)
        assert snaps[SIDE_LONG].window_usd == Decimal("0")
        assert snaps[SIDE_SHORT].window_usd == Decimal("0")


class TestCascadeDetector:
    def test_window_aggregation_and_3x_mean(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=50_000)
        det.ingest([_ev(now, 150_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.cold_start is False
        assert snap.baseline_mean_usd == Decimal("50000")
        assert snap.window_usd == Decimal("150000")
        assert snap.fire is True

    def test_3x_just_under_does_not_fire(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=50_000)
        det.ingest([_ev(now, 149_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.fire is False

    def test_floor_blocks_dust(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=1_000)
        det.ingest([_ev(now, 10_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.ratio >= 3.0
        assert snap.window_usd < Decimal("100000")
        assert snap.fire is False

    def test_cold_start_no_fire(self):
        now = 1_000_000
        det = CascadeDetector(_cfg())
        det.ingest([_ev(now, 5_000_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.cold_start is True
        assert snap.fire is False

    def test_long_short_split_no_netting(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=50_000, side=SIDE_LONG)
        _quiet_baseline(det, now, usd=50_000, side=SIDE_SHORT)
        det.ingest([_ev(now, 150_000, SIDE_LONG), _ev(now, 20_000, SIDE_SHORT)])
        snaps = det.evaluate(now)
        assert snaps[SIDE_LONG].fire is True
        assert snaps[SIDE_SHORT].fire is False
        assert snaps[SIDE_SHORT].window_usd == Decimal("20000")

    def test_baseline_exclusive_of_current_window(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=1_000)
        det.ingest([_ev(now, 52_900_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.fire is True
        assert snap.baseline_mean_usd == Decimal("1000")
        assert snap.window_usd == Decimal("52900000")
        # next 5m slot: burst has rolled into exclusive baseline, ratio collapses
        later = now + 300_000
        later_snap = det.evaluate(later)[SIDE_LONG]
        assert later_snap.window_usd == Decimal("0")
        assert later_snap.baseline_mean_usd > Decimal("1000")

    def test_soft_signal_not_fire(self):
        now = 10_000_000
        det = CascadeDetector(_cfg())
        _quiet_baseline(det, now, usd=20_000)
        det.ingest([_ev(now, 60_000)])
        snap = det.evaluate(now)[SIDE_LONG]
        assert snap.fire is False
        assert snap.soft is True


class TestCascadeStateRearm:
    def test_rearm_needs_cooldown_and_under_threshold(self):
        st = CascadeState(cooldown_sec=600)
        assert st.should_execute("long", is_fire=True, now_mono=0.0) is True
        st.note_fill("long", 0.0)
        assert st.should_execute("long", is_fire=True, now_mono=10.0) is False
        assert st.should_execute("long", is_fire=True, now_mono=600.0) is False
        assert st.should_execute("long", is_fire=False, now_mono=601.0) is False
        assert st.should_execute("long", is_fire=True, now_mono=602.0) is True

    def test_lock_only_does_not_start_timer(self):
        st = CascadeState(cooldown_sec=600)
        st.should_execute("long", is_fire=True, now_mono=0.0)
        assert st.last_fill_mono["long"] is None
        assert st.can_arm("long", 1.0) is True


class TestFixtureReplay:
    def test_fires_within_60s_of_cascade_start(self):
        data = json.loads(FIXTURE.read_text())
        start = int(data["cascade_start_ms"])
        det = CascadeDetector(_cfg(log_soft_signal=False))
        first_fire_ms = None
        for batch in data["batches"]:
            events = parse_liq_batch(batch, quanto_btc=data.get("quanto_btc") or 0.0001)
            det.ingest(events)
            now = max(ev.ts_ms for ev in events)
            snap = det.evaluate(now)[SIDE_LONG]
            if snap.fire and first_fire_ms is None:
                first_fire_ms = now
        assert first_fire_ms is not None
        assert first_fire_ms - start <= 60_000
        # dump path only
        last = data["batches"][-1]
        events = parse_liq_batch(last, quanto_btc=0.0001)
        det.ingest(events)
        short_snap = det.evaluate(int(last["result"][0]["time"]))[SIDE_SHORT]
        assert short_snap.fire is False
