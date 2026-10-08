"""#651: trailing stop must not arm on a stale pre-DCA peak.

Paper fixtures only. Coin names are test data, never production branches.
"""

from __future__ import annotations

import os
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

_NULL_CTX = nullcontext()

from core.models import MarketContext
from core.sim_ledger_replay import replay_simulated_ledger
from core.tenant_context import DEFAULT_TENANT
from services.exit_realtime.shadow_eval import evaluate_would_sells
from strategies.dca import (
    _total_dca_rounds,
    effective_stop_loss_thresholds,
    lot_has_dca_rounds,
    recent_high_reached_after_dca,
    trail_exits_paused_after_dca,
)
from strategies.positions import (
    CYCLE_FIELDS,
    _active_store,
    _merged_dca_round,
    apply_positions_snapshot,
    clear_positions_memory,
    derive_positions_from_orders_and_cache,
    get_key,
    get_position,
    has_position_amount,
    is_open_position,
    k3_cycle_status,
    load_positions,
    positions,
    reset_all_position_stores_for_tests,
    update_market_snapshot,
    update_position,
)
from strategies.technical_rsi_bb import TechnicalRSIStrategy
from strategies.trailing_stop import evaluate_trailing_stop
from strategies.trailing_take_profit import evaluate_trailing_take_profit


def _params(**trail):
    base = {
        "trailing_stop": {
            "enabled": True,
            "mode": "live",
            "activation_gain_pct": 5.0,
            "min_trail_pct": 8.0,
            "max_trail_pct": 25.0,
            "atr_multiplier": 1.0,
            "arm_on_peak": True,
            "floor_at_entry": True,
            "floor_breach_pct": 1.0,
            "be_buffer_pct": 0.0,
        },
        "trailing_take_profit": {
            "enabled": True,
            "mode": "live",
            "arm_gain_pct": 5.0,
            "min_gain_pct": 1.0,
            "min_gain_pct_floor": 1.0,
            "trail_pct": 8.0,
            "dynamic_trail": False,
            "max_steps": 1,
            "cooldown_hours": 0,
            "trail_above_zero_after_arm": True,
        },
        "dca": {
            "pause_trail_exits_after_dca": True,
            "trail_grace_hours_after_dca": 12,
            "reanchor_peak_on_dca": True,
        },
        "stop_loss_pct": 50,
        "take_profit_tiers": [],
    }
    base["trailing_stop"].update(trail)
    return base


def _mkt(symbol, price, entry, atr=5.0, params=None):
    return MarketContext(
        symbol=symbol,
        timeframe="1h",
        current_price=price,
        has_position=True,
        average_entry=entry,
        atr_pct=atr,
        strategy_params=params or _params(),
    )


def _buy(symbol, tf, price, amount, ts, *, source="manual", signal="BUY", created=None):
    usdt = float(price) * float(amount)
    return {
        "status": "filled",
        "side": "buy",
        "symbol": symbol,
        "timeframe": tf,
        "source": source,
        "signal": signal,
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "request": {"price": price, "amount": amount, "usdt": usdt},
        "timestamps": {"created": created or ts, "filled": ts},
    }


def _sell(symbol, tf, price, amount, ts):
    return {
        "status": "filled",
        "side": "sell",
        "symbol": symbol,
        "timeframe": tf,
        "source": "trailing_stop",
        "signal": "SELL_FULL",
        "execution": {"price": price, "amount": amount, "usdt": price * amount},
        "request": {"price": price, "amount": amount},
        "timestamps": {"created": ts, "filled": ts},
        "pnl": 0,
    }


Q_ENTRY = 0.034257
Q_PEAK = 0.038628
Q_AVG = 0.0226077
Q_PX = 0.022394
Q_FIRST = "2026-09-27T16:58:30"
Q_DCA = "2026-10-02T05:31:46"


def _q_legacy():
    return {
        "amount": 1000.0,
        "peak_amount": 1000.0,
        "sold_percent": 0.0,
        "average_entry": Q_AVG,
        "last_buy_price": Q_AVG,
        "recent_high": Q_PEAK,
        "dca_rounds": 4,
        "last_dca_at": Q_DCA,
        "first_buy_at": Q_FIRST,
        "entry_at": Q_FIRST,
        "side": "long",
    }


class TestIssue651(unittest.TestCase):
    def setUp(self):
        reset_all_position_stores_for_tests()

    def tearDown(self):
        reset_all_position_stores_for_tests()

    def test_t1_q_legacy_no_trailing_sell(self):
        pos = _q_legacy()
        now = datetime(2026, 10, 6, 10, 58, 7)
        cand = evaluate_trailing_stop(
            _mkt("Q/USDT", Q_PX, Q_AVG), pos, _params(), now=now
        )
        self.assertIsNone(cand)

    def test_t1b_exit_ws_no_sell(self):
        events = evaluate_would_sells(
            symbol="Q/USDT",
            timeframe="1h",
            price=Q_PX,
            position=_q_legacy(),
            strategy_params=_params(),
        )
        self.assertFalse(any(ev.get("source") == "trailing_stop" for ev in events))

    def test_t5_no_dca_unchanged(self):
        pos = {"average_entry": 1.0, "recent_high": 1.2, "dca_rounds": 0}
        cand = evaluate_trailing_stop(_mkt("Y/USDT", 1.08, 1.0), pos, _params())
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_stop")

    def test_t6_six_dca_cases_do_not_fire(self):
        """Pattern of Lena's six DCA rows. Names are fixtures, not code branches."""
        now = datetime(2026, 10, 6, 12, 0, 0)
        rows = [
            ("TAKE/USDT", 1.0, 1.6, 0.9),
            ("BEAT/USDT", 2.2, 2.8, 2.0),
            ("RAVE/USDT", 0.5, 0.8, 0.48),
            ("FORM/USDT", 1.1, 1.7, 1.0),
            ("KAT/USDT", 0.4, 0.7, 0.38),
            ("Q/USDT", Q_AVG, Q_PEAK, Q_PX),
        ]
        for symbol, avg, peak, px in rows:
            pos = {
                "average_entry": avg,
                "recent_high": peak,
                "dca_rounds": 2,
                "last_dca_at": "2026-09-01T00:00:00",
            }
            cand = evaluate_trailing_stop(
                _mkt(symbol, px, avg), pos, _params(), now=now
            )
            self.assertIsNone(cand, symbol)

    def test_t8_grace_expired_v2_no_fire(self):
        pos = _q_legacy()
        now = datetime(2026, 10, 6, 12, 0, 0)
        paused, _ = trail_exits_paused_after_dca(pos, _params(), now=now)
        self.assertFalse(paused)
        self.assertFalse(recent_high_reached_after_dca(pos))
        self.assertIsNone(
            evaluate_trailing_stop(_mkt("Q/USDT", Q_PX, Q_AVG), pos, _params(), now=now)
        )

    def test_t10_stop_loss_still_fires(self):
        pos = _q_legacy()
        price = Q_AVG * 0.4
        self.assertIsNone(
            evaluate_trailing_stop(_mkt("Q/USDT", price, Q_AVG), pos, _params())
        )
        strategy = TechnicalRSIStrategy()
        market = MarketContext(
            symbol="Q/USDT",
            timeframe="1h",
            current_price=price,
            rsi=40.0,
            lower_bb=1.0,
            has_position=True,
            average_entry=Q_AVG,
            strategy_params=_params(),
            sim_state=dict(pos),
        )
        result = strategy.analyze(
            {"symbol": "Q/USDT", "timeframe": "1h", "strategy_params": _params()},
            market,
        )
        self.assertIn("stop_loss", result.sources)

    def test_t13_check_raises_no_arm_stop_loss_still_fires(self):
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.4,
            "dca_rounds": 1,
            "last_dca_at": "2026-09-01T00:00:00",
            "peak_epoch_high": 1.0,
            "peak_epoch_at": "2026-09-01T00:00:00",
        }
        with patch(
            "strategies.dca.recent_high_reached_after_dca",
            side_effect=RuntimeError("boom"),
        ), patch("logger.log") as logged:
            cand = evaluate_trailing_stop(
                _mkt("ZZ/USDT", 1.2, 1.0), pos, _params()
            )
        self.assertIsNone(cand)
        warnings = [
            c for c in logged.call_args_list if c.args and c.args[-1] == "WARNING"
            or (len(c.args) > 1 and c.args[1] == "WARNING")
        ]
        self.assertTrue(warnings, logged.call_args_list)
        self.assertTrue(any("boom" in str(c) for c in warnings))
        strategy = TechnicalRSIStrategy()
        market = MarketContext(
            symbol="ZZ/USDT",
            timeframe="1h",
            current_price=0.4,
            rsi=40.0,
            lower_bb=1.0,
            has_position=True,
            average_entry=1.0,
            strategy_params=_params(),
            sim_state={"dca_rounds": 0},
        )
        result = strategy.analyze(
            {"symbol": "ZZ/USDT", "timeframe": "1h", "strategy_params": _params()},
            market,
        )
        self.assertIn("stop_loss", result.sources)

    def test_t4_real_post_dca_peak_fires(self):
        dca = datetime(2026, 10, 1, 0, 0, 0)
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.2,
            "dca_rounds": 1,
            "last_dca_at": dca.isoformat(),
            "peak_epoch_high": 1.0,
            "peak_epoch_at": dca.isoformat(),
            "peak_at": (dca + timedelta(hours=5)).isoformat(),
        }
        now = dca + timedelta(hours=30)
        cand = evaluate_trailing_stop(
            _mkt("HH/USDT", 1.05, 1.0), pos, _params(), now=now
        )
        self.assertIsNotNone(cand)
        self.assertIn("proof=peak_at", cand.rationale)
        self.assertIn("peak_epoch_at=", cand.rationale)
        self.assertIn("latest_dca=", cand.rationale)
        self.assertIn("v3=", cand.rationale)
        self.assertIn("first_buy_at=", cand.rationale)

    def test_t14_epoch_branch_rules(self):
        dca = datetime(2026, 10, 2, 5, 31, 46)
        older = dca - timedelta(hours=3)
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.3,
            "peak_epoch_high": 1.05,
            "peak_epoch_at": older.isoformat(),
            "dca_rounds": 2,
            "last_dca_at": dca.isoformat(),
            "peak_at": older.isoformat(),
        }
        self.assertFalse(recent_high_reached_after_dca(pos))
        snap = {"SYM_USDT_1h": dict(pos)}
        apply_positions_snapshot(snap, scope="demo")
        loaded = get_position("SYM/USDT", "1h")
        self.assertFalse(recent_high_reached_after_dca(loaded))

        equal = dict(pos)
        equal["peak_epoch_at"] = dca.isoformat()
        equal["recent_high"] = 1.3
        self.assertTrue(recent_high_reached_after_dca(equal))
        events = evaluate_would_sells(
            symbol="SYM/USDT",
            timeframe="1h",
            price=1.35,
            position=equal,
            strategy_params=_params(),
        )
        bumped = dict(equal)
        bumped["recent_high"] = 1.35
        self.assertTrue(recent_high_reached_after_dca(bumped))

        recovery = dict(pos)
        recovery["last_dca_at"] = older.isoformat()
        recovery["last_dca_recovery_at"] = dca.isoformat()
        recovery["peak_epoch_at"] = older.isoformat()
        recovery["dca_recovery_rounds"] = 1
        self.assertFalse(recent_high_reached_after_dca(recovery))
        recovery["peak_epoch_at"] = dca.isoformat()
        recovery["recent_high"] = 1.4
        self.assertTrue(recent_high_reached_after_dca(recovery))

    def test_t17_ttp_after_grace(self):
        now = datetime(2026, 10, 6, 12, 0, 0)
        stale = {
            "average_entry": 1.0,
            "recent_high": 1.3,
            "dca_rounds": 1,
            "last_dca_at": "2026-09-01T00:00:00",
            "trail_tp_steps": 0,
        }
        self.assertIsNone(
            evaluate_trailing_take_profit(
                _mkt("TTP/USDT", 1.15, 1.0), stale, _params(), now=now
            )
        )
        fresh = dict(stale)
        fresh["peak_at"] = "2026-10-05T00:00:00"
        fresh["peak_epoch_at"] = "2026-09-01T00:00:00"
        fresh["peak_epoch_high"] = 1.0
        cand = evaluate_trailing_take_profit(
            _mkt("TTP/USDT", 1.15, 1.0), fresh, _params(), now=now
        )
        self.assertIsNotNone(cand)
        self.assertIn("proof=", cand.rationale)

    def test_t2_sources_reset_peak_and_survive_reload(self):
        cases = [
            ("dca", "BUY_DCA"),
            ("dca_sniper", "BUY_DCA"),
            ("dca_recovery", "BUY"),
            ("dca_scheduled", "BUY"),
            ("dca", "BUY"),
            ("manual", "BUY"),
        ]
        for source, signal in cases:
            reset_all_position_stores_for_tests()
            symbol = f"SRC{source[:3].upper()}/USDT"
            update_position(symbol, "1h", "BUY", 2.0, 10)
            pos = get_position(symbol, "1h")
            pos["recent_high"] = 9.0
            pos["v3"] = True
            update_position(symbol, "1h", signal, 1.0, 10, source=source)
            got = get_position(symbol, "1h")
            self.assertLess(float(got["recent_high"]), 9.0, source)
            self.assertFalse(bool(got.get("v3")), source)
            self.assertTrue(got.get("peak_epoch_at"), source)
            with patch("strategies.positions.save_positions_document", return_value=True), patch(
                "services.ledger_sync._build_positions_snapshot_from_orders",
                return_value={},
            ), patch(
                "data_manager.load_positions_document",
                return_value={"positions": {}},
            ):
                # Persist via serialize then round-trip the live lot through deserialize.
                from strategies.positions import _deserialize_position, _serialize_positions

                blob = _serialize_positions()
                key = get_key(symbol, "1h")
                again = _deserialize_position(blob["positions"][key])
            self.assertEqual(again.get("peak_epoch_at"), got.get("peak_epoch_at"))
            self.assertAlmostEqual(float(again["recent_high"]), float(got["recent_high"]))

    def test_t2_replay_source_resets_from_fill_time(self):
        orders = [
            _buy("RP/USDT", "1h", 2.0, 10, "2026-10-01T00:00:00"),
            _buy(
                "RP/USDT",
                "1h",
                1.0,
                10,
                "2026-10-02T01:02:03",
                source="dca",
                signal="BUY",
            ),
        ]
        snap = replay_simulated_ledger(orders, initial=100000)["positions"]
        pos = snap[get_key("RP/USDT", "1h")]
        self.assertEqual(pos["peak_epoch_at"], "2026-10-02T01:02:03")
        self.assertLess(float(pos["recent_high"]), 2.0)
        self.assertFalse(pos.get("v3"))

    def test_t11_dust_reentry(self):
        symbol = "MARSCOIN/USDT"
        update_position(symbol, "1h", "BUY", 2.0, 10)
        update_position(symbol, "1h", "BUY_DCA", 1.5, 10, source="dca")
        pos = get_position(symbol, "1h")
        # Shrink to dust without clearing the old peak / DCA state.
        pos["amount"] = Decimal("0.1")
        pos["average_entry"] = 1.5
        pos["recent_high"] = 9.0
        pos["v3"] = True
        self.assertTrue(has_position_amount(pos))
        self.assertFalse(is_open_position(pos))
        update_position(symbol, "1h", "BUY", 1.2, 10)
        got = get_position(symbol, "1h")
        self.assertEqual(int(got.get("dca_rounds") or 0), 0)
        self.assertIsNone(got.get("last_dca_at"))
        self.assertAlmostEqual(float(got["recent_high"]), 1.2)
        self.assertFalse(bool(got.get("v3")))
        # Dust stays in the average: 0.1*1.5 + 10*1.2 / 10.1
        expect = (0.1 * 1.5 + 10 * 1.2) / 10.1
        self.assertAlmostEqual(float(got["average_entry"]), expect, places=6)

    def test_t11b_replay_keeps_weighted_average(self):
        dust_amt = 0.2
        dust_px = 1.5
        buy_amt = 10.0
        buy_px = 1.2
        orders = [
            _buy("XTZ/USDT", "1h", dust_px, dust_amt, "2026-09-20T00:00:00"),
            _buy("XTZ/USDT", "1h", buy_px, buy_amt, "2026-09-25T00:00:00"),
        ]
        # First order is dust-sized by itself (notional 0.3), so the second
        # buy is a dust re-entry and must weight the remainder in.
        snap = replay_simulated_ledger(orders, initial=100000)["positions"]
        pos = snap[get_key("XTZ/USDT", "1h")]
        expect = (dust_amt * dust_px + buy_amt * buy_px) / (dust_amt + buy_amt)
        self.assertAlmostEqual(pos["average_entry"], expect, places=6)
        self.assertEqual(int(pos["dca_rounds"]), 0)
        self.assertIsNone(pos.get("last_dca_at"))
        self.assertAlmostEqual(pos["recent_high"], buy_px)
        again = replay_simulated_ledger(orders, initial=100000)["positions"]
        self.assertAlmostEqual(
            again[get_key("XTZ/USDT", "1h")]["average_entry"], pos["average_entry"]
        )

    def test_t11c_second_reentry_no_inherited_dca(self):
        orders = [
            _buy("XTZ/USDT", "1h", 1.0, 0.2, "2026-09-20T00:00:00"),
            _buy("XTZ/USDT", "1h", 1.1, 10, "2026-09-25T00:00:00", source="dca", signal="BUY_DCA"),
            _sell("XTZ/USDT", "1h", 1.1, 10.1, "2026-09-25T06:00:00"),
            _buy("XTZ/USDT", "1h", 1.0, 0.2, "2026-09-25T07:00:00"),
            _buy("XTZ/USDT", "1h", 1.3, 8, "2026-09-26T00:00:00"),
        ]
        # The DCA buy is into an open (not dust) lot only if the first buy is
        # open. Force the middle add to be a real DCA by sizing the first buy
        # above the dust line, then sell down, then re-enter.
        orders = [
            _buy("XTZ/USDT", "1h", 1.0, 5, "2026-09-20T00:00:00"),
            _buy("XTZ/USDT", "1h", 0.8, 5, "2026-09-21T00:00:00", source="dca", signal="BUY_DCA"),
            _sell("XTZ/USDT", "1h", 0.8, 9.7, "2026-09-25T06:00:00"),
            _buy("XTZ/USDT", "1h", 1.2, 6, "2026-09-26T00:00:00"),
        ]
        snap = replay_simulated_ledger(orders, initial=100000)["positions"]
        pos = snap[get_key("XTZ/USDT", "1h")]
        self.assertEqual(int(pos["dca_rounds"]), 0)
        self.assertIsNone(pos.get("last_dca_at"))
        self.assertAlmostEqual(float(pos["recent_high"]), 1.2)

    def test_t11d_open_lot_stays_addon(self):
        symbol = "OPEN/USDT"
        update_position(symbol, "1h", "BUY", 2.0, 2)  # notional 4 >= 1
        self.assertTrue(is_open_position(get_position(symbol, "1h")))
        update_position(symbol, "1h", "BUY_DCA", 1.0, 2, source="dca")
        got = get_position(symbol, "1h")
        self.assertEqual(int(got["dca_rounds"]), 1)
        self.assertIsNotNone(got.get("last_dca_at"))

    def test_t11e_new_position_allows_dca_without_old_pause(self):
        symbol = "FRESH/USDT"
        update_position(symbol, "1h", "BUY", 2.0, 10)
        pos = get_position(symbol, "1h")
        pos["amount"] = Decimal("0.1")
        pos["average_entry"] = 2.0
        pos["dca_rounds"] = 4
        pos["last_dca_at"] = "2026-01-01T00:00:00"
        update_position(symbol, "1h", "BUY", 1.0, 10)
        got = get_position(symbol, "1h")
        paused, _ = trail_exits_paused_after_dca(got, _params())
        self.assertFalse(paused)
        self.assertFalse(lot_has_dca_rounds(got))
        update_position(symbol, "1h", "BUY_DCA", 0.9, 4, source="dca")
        self.assertEqual(int(get_position(symbol, "1h")["dca_rounds"]), 1)

    def test_t12_rebuild_twice_then_one_new_fill(self):
        orders = [
            _buy("RB/USDT", "1h", 2.0, 10, "2026-10-01T00:00:00"),
            _buy("RB/USDT", "1h", 1.0, 10, "2026-10-02T00:00:00", source="dca", signal="BUY_DCA"),
        ]
        a = replay_simulated_ledger(orders, initial=100000)["positions"]
        b = replay_simulated_ledger(list(orders), initial=100000)["positions"]
        key = get_key("RB/USDT", "1h")
        self.assertEqual(a[key]["peak_epoch_at"], b[key]["peak_epoch_at"])
        self.assertEqual(a[key]["recent_high"], b[key]["recent_high"])
        self.assertEqual(a[key]["peak_at"], b[key]["peak_at"])
        orders.append(
            _buy("RB/USDT", "1h", 0.5, 10, "2026-10-03T04:05:06", source="dca", signal="BUY_DCA")
        )
        c = replay_simulated_ledger(orders, initial=100000)["positions"]
        self.assertEqual(c[key]["peak_epoch_at"], "2026-10-03T04:05:06")
        self.assertAlmostEqual(c[key]["peak_epoch_high"], c[key]["average_entry"])

    def test_t7_fields_survive_load(self):
        raw = {
            "amount": 5.0,
            "peak_amount": 5.0,
            "sold_percent": 0.0,
            "average_entry": 1.0,
            "recent_high": 1.2,
            "peak_epoch_high": 1.1,
            "peak_epoch_at": "2026-10-02T05:31:46",
            "peak_at": "2026-10-03T00:00:00",
            "v3": True,
            "first_buy_at": "2026-10-01T00:00:00",
            "dca_rounds": 1,
            "last_dca_at": "2026-10-02T05:31:46",
        }
        key = get_key("KEEP/USDT", "1h")
        snap = {
            key: {
                **raw,
                "amount": 5.0,
                "average_entry": 1.0,
                "cycle_open_created": "2026-10-01T00:00:00",
                "cycle_open_filled": "2026-10-01T00:00:01",
            }
        }
        cache = {"positions": {key: dict(raw)}}
        merged = derive_positions_from_orders_and_cache(snap, cache, tenant_id="henry")
        self.assertEqual(merged[key]["peak_epoch_at"], raw["peak_epoch_at"])
        self.assertTrue(merged[key]["v3"])
        apply_positions_snapshot({key: merged[key]}, scope="demo")
        # Switch into henry and load through the real loader.
        with patch(
            "services.ledger_sync._build_positions_snapshot_from_orders",
            return_value=snap,
        ), patch(
            "data_manager.load_positions_document",
            return_value=cache,
        ):
            loaded = load_positions(scope="demo", tenant_id="henry")
        got = loaded[key]
        self.assertEqual(got.get("peak_epoch_at"), raw["peak_epoch_at"])
        self.assertEqual(got.get("peak_at"), raw["peak_at"])
        self.assertTrue(got.get("v3"))
        self.assertAlmostEqual(float(got["peak_epoch_high"]), 1.1)

    def test_c1_dust_reentry_live_and_replay_match_cycle_fields(self):
        """Both F7 resets write CYCLE_FIELDS. Dust re-entry must not keep stale entry fields."""
        symbol = "DUSTEQ/USDT"
        fill = "2026-10-08T09:00:00"
        stale = {
            "entry_source": "stale-source",
            "entry_at": "2020-01-01T00:00:00",
            "entry_15m_vol_ratio": 9.9,
            "strategy_tier": "volatile",
            "exit_source": "old-exit",
            "dca_rounds": 4,
            "dca_recovery_rounds": 2,
            "last_dca_at": "2020-06-01T00:00:00",
            "v3": True,
            "recent_high": 50.0,
            "side": "long",
            "leverage": 3,
            "entry_snapshot": {"fill_price": 9},
        }

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 10, 8, 9, 0, 0)
                if tz is not None:
                    return base.replace(tzinfo=tz)
                return base

        # #600 attaches entry_snapshot after the reset on the live path only.
        # The shared reset is what both paths must match, so that post-step stays off.
        with patch("strategies.positions.flush_positions", lambda *a, **k: None), patch(
            "strategies.positions._attach_and_persist_entry_snapshot", lambda *a, **k: None
        ), patch(
            "strategies.positions.datetime", _FixedDateTime
        ):
            update_position(
                symbol,
                "1h",
                "BUY",
                2.0,
                10,
                entry_source="entry_sensor_15m",
                entry_15m_vol_ratio=4.5,
            )
            dust = get_position(symbol, "1h")
            dust["amount"] = Decimal("0.1")
            dust["average_entry"] = 2.0
            dust.update(stale)
            self.assertFalse(is_open_position(dust))
            update_position(symbol, "1h", "BUY", 1.2, 8)
            live = get_position(symbol, "1h")

        orders = [
            _buy(symbol, "1h", 2.0, 0.1, "2020-01-01T00:00:00", source="entry_sensor_15m"),
            _buy(symbol, "1h", 1.2, 8, fill),
        ]
        replayed = replay_simulated_ledger(orders, initial=100000)["positions"]
        replay_pos = replayed[get_key(symbol, "1h")]

        def _norm(value):
            if isinstance(value, Decimal):
                value = float(value)
            if isinstance(value, float):
                return round(value, 8)
            return value

        for name in CYCLE_FIELDS:
            self.assertEqual(
                _norm(live.get(name)),
                _norm(replay_pos.get(name)),
                name,
            )
        self.assertIsNone(live.get("entry_source"))
        self.assertEqual(live.get("entry_at"), fill)
        self.assertIsNone(live.get("entry_15m_vol_ratio"))
        self.assertNotEqual(live.get("entry_source"), stale["entry_source"])
        self.assertIsNone(replay_pos.get("entry_source"))
        self.assertEqual(replay_pos.get("entry_at"), fill)
        self.assertIsNone(replay_pos.get("entry_15m_vol_ratio"))

    def test_recovery_fill_counts_once_live_and_replay(self):
        symbol = "RECONCE/USDT"
        widen = 4.0
        stop = 10.0
        params = {
            "stop_loss_pct": stop,
            "dca": {"stop_loss_widen_pct_per_round": widen, "interval_hours": 12},
        }
        with patch("strategies.positions.flush_positions", lambda *a, **k: None), patch(
            "strategies.registry.resolve_strategy_params", return_value=_params()
        ):
            update_position(symbol, "1h", "BUY", 2.0, 10)
            update_position(symbol, "1h", "BUY_DCA", 1.5, 5, source="dca_recovery")
            live = get_position(symbol, "1h")
        self.assertEqual(_total_dca_rounds(live), 1)
        self.assertEqual(int(live.get("dca_recovery_rounds") or 0), 0)
        full, _, _ = effective_stop_loss_thresholds(live, params, stop)
        self.assertEqual(full, stop + 1 * widen)

        orders = [
            _buy(symbol, "1h", 2.0, 10, "2026-10-01T00:00:00"),
            _buy(
                symbol,
                "1h",
                1.5,
                5,
                "2026-10-02T00:00:00",
                source="dca_recovery",
                signal="BUY_DCA",
            ),
        ]
        replay_pos = replay_simulated_ledger(orders, initial=100000)["positions"][
            get_key(symbol, "1h")
        ]
        self.assertEqual(_total_dca_rounds(replay_pos), 1)
        self.assertEqual(int(replay_pos.get("dca_rounds") or 0), 1)
        self.assertEqual(int(replay_pos.get("dca_recovery_rounds") or 0), 0)
        full_replay, _, _ = effective_stop_loss_thresholds(replay_pos, params, stop)
        self.assertEqual(full_replay, stop + 1 * widen)

    def test_conservative_atr_is_the_tightest_configured(self):
        from services.ledger_sync import _conservative_atr_pct, _v3_would_sell

        class _Cfg:
            raw = {
                "risk": {"atr_reference_pct": 9.0, "atr_pct": 6.0},
                "exit_realtime": {"default_atr_pct": 2.5},
            }

        params = _params()
        params["atr_pct"] = 4.0
        with patch("core.config.get_bot_config", return_value=_Cfg()):
            self.assertEqual(_conservative_atr_pct(params), 2.5)
            self.assertEqual(_conservative_atr_pct({}), 2.5)
        with patch("core.config.get_bot_config", side_effect=RuntimeError("no cfg")):
            self.assertEqual(_conservative_atr_pct({}), 3.0)

        seen = {}

        def _stop(market, position, strategy_params, **kwargs):
            seen["atr"] = market.atr_pct
            return None

        pos = {
            "average_entry": 100.0,
            "last_buy_price": 100.0,
            "recent_high": 100.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-09-01T00:00:00",
        }
        with patch("core.config.get_bot_config", return_value=_Cfg()), patch(
            "strategies.registry.resolve_strategy_params", return_value=params
        ), patch("strategies.trailing_stop.evaluate_trailing_stop", side_effect=_stop), patch(
            "strategies.trailing_take_profit.evaluate_trailing_take_profit", return_value=None
        ):
            sold = _v3_would_sell(
                pos,
                {"recent_high": 101.0, "peak_epoch_at": "2026-09-02T00:00:00"},
                price=100.5,
                symbol="ATR/USDT",
                timeframe="1h",
                tenant_id="henry",
            )
        self.assertFalse(sold)
        self.assertEqual(seen["atr"], 2.5)

    def test_v3_check_fail_closed_names_the_lot(self):
        from datetime import datetime as real_dt

        from services.ledger_sync import _reanchor_one_legacy_lot

        symbol = "FAILC/USDT"
        pos = {
            "amount": 10.0,
            "peak_amount": 10.0,
            "sold_percent": 0.0,
            "average_entry": 100.0,
            "last_buy_price": 100.0,
            "recent_high": 100.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-09-01T00:00:00",
            "first_buy_at": "2026-08-01T00:00:00",
        }

        class _Svc:
            def infer_ohlcv_peak_price(self, symbol, timeframe, since, **kwargs):
                return {
                    "covered": True,
                    "price": 101.0,
                    "candle_open": real_dt(2026, 9, 2),
                    "reason": None,
                }

        def _run(resolve):
            notes = []

            def _log(message, level="INFO"):
                notes.append((level, message))

            with patch("services.ledger_sync.log", side_effect=_log), patch(
                "strategies.registry.resolve_strategy_params", side_effect=resolve
            ), patch(
                "services.ledger_sync._latest_buy_fill_iso",
                return_value="2026-09-01T00:00:00",
            ):
                out = _reanchor_one_legacy_lot(
                    pos,
                    symbol=symbol,
                    timeframe="1h",
                    scope="demo",
                    tenant_id="henry",
                    price=100.5,
                    market_svc=_Svc(),
                )
            return out, notes

        for resolve in (RuntimeError("no params"), lambda *a, **k: None, lambda *a, **k: {}):
            out, notes = _run(resolve)
            self.assertIsNotNone(out)
            self.assertTrue(out["fields"]["v3"])
            self.assertAlmostEqual(float(out["fields"]["recent_high"]), 100.5)
            warnings = [msg for level, msg in notes if level == "WARNING"]
            self.assertTrue(any(symbol in msg and "henry" in msg for msg in warnings), warnings)

        def _boom(*args, **kwargs):
            raise RuntimeError("eval blew up")

        notes = []

        def _log(message, level="INFO"):
            notes.append((level, message))

        with patch("services.ledger_sync.log", side_effect=_log), patch(
            "strategies.registry.resolve_strategy_params", return_value=_params()
        ), patch(
            "strategies.trailing_stop.evaluate_trailing_stop", side_effect=_boom
        ), patch(
            "services.ledger_sync._latest_buy_fill_iso",
            return_value="2026-09-01T00:00:00",
        ):
            out = _reanchor_one_legacy_lot(
                pos,
                symbol=symbol,
                timeframe="1h",
                scope="demo",
                tenant_id="henry",
                price=100.5,
                market_svc=_Svc(),
            )
        self.assertTrue(out["fields"]["v3"])
        self.assertTrue(
            any(level == "WARNING" and symbol in msg and "eval blew up" in msg for level, msg in notes)
        )


class TestIssue651Startup(unittest.TestCase):
    def setUp(self):
        reset_all_position_stores_for_tests()
        self.docs = {}
        self.orders = {}
        self.logs = []
        self.saves = []

    def tearDown(self):
        reset_all_position_stores_for_tests()

    def _log(self, message, level="INFO"):
        self.logs.append((level, str(message)))

    def _in(self, tenant):
        from core.tenant_context import tenant_context

        return tenant_context(tenant, scope="demo", headless=True)

    def _tenant(self, tid):
        return {
            "tenant_id": tid,
            "status": "active",
            "telegram": {"owner_chat_id": "100", "headless": True},
            "defaults": {"ledger_scope": "demo"},
        }

    def _patched(self, tenants, *, prices=None, candles=None, infer=None, params=None):
        from contextlib import contextmanager

        prices = prices or {}
        candles = candles or {
            "covered": True,
            "price": None,
            "candle_open": None,
            "reason": "no_candle_after",
        }

        def load_orders(scope, tenant_id=None):
            tid = tenant_id or DEFAULT_TENANT
            return {"orders": list(self.orders.get((tid, scope), []))}

        def load_doc(scope, tenant_id=None):
            tid = tenant_id or DEFAULT_TENANT
            return {"positions": dict(self.docs.get((tid, scope), {}).get("positions", {}))}

        def save_doc(payload, scope, tenant_id=None):
            tid = tenant_id or DEFAULT_TENANT
            self.docs[(tid, scope)] = payload
            self.saves.append((tid, scope, payload))
            return True

        if infer is not None:
            infer_patch = patch(
                "services.market_service.MarketService.infer_ohlcv_peak_price",
                side_effect=infer,
            )
        else:
            infer_patch = patch(
                "services.market_service.MarketService.infer_ohlcv_peak_price",
                return_value=candles,
            )

        @contextmanager
        def _ctx():
            with patch(
                "storage.tenant_registry.get_tenant",
                side_effect=lambda tid, test=False: self._tenant(tid),
            ), patch(
                "core.tenant_routing.iter_price_cycle_tenants", return_value=list(tenants)
            ), patch("data_manager.load_orders", side_effect=load_orders), patch(
                "data_manager.load_positions_document", side_effect=load_doc
            ), patch("data_manager.save_positions_document", side_effect=save_doc), patch(
                "strategies.positions.load_positions_document", side_effect=load_doc
            ), patch(
                "strategies.positions.save_positions_document", side_effect=save_doc
            ), patch(
                "data_manager.resolve_ledger_scope", return_value="demo"
            ), patch(
                "data_manager.get_config", return_value={"trading_mode": "paper"}
            ), patch("services.ledger_sync.migrate_legacy_positions", lambda: None), patch(
                "price_fetcher.get_prices_batch", return_value=dict(prices)
            ), infer_patch, patch("logger.log", side_effect=self._log), patch(
                "services.ledger_sync.log", side_effect=self._log
            ), patch("strategies.positions.log", side_effect=self._log), patch(
                "core.portfolio_baseline.initial_capital", return_value=1_000_000.0
            ), patch("bus.writer_lease.lease_enabled", return_value=False), patch(
                "bus.locks.ledger_lock", return_value=_NULL_CTX
            ), patch(
                "strategies.registry.resolve_strategy_params",
                return_value=params if params is not None else _params(),
            ):
                yield

        return _ctx()

    def _run(self, tenants, *, reanchor=True, prices=None, candles=None, infer=None, params=None):
        from services.ledger_sync import run_per_tenant_startup

        with self._patched(
            tenants, prices=prices, candles=candles, infer=infer, params=params
        ):
            run_per_tenant_startup("demo", include_legacy_reanchor=reanchor)

    def test_t1a_restart_does_not_sell(self):
        key = get_key("Q/USDT", "1h")
        self.docs[("henry", "demo")] = {"positions": {key: _q_legacy()}}
        self.orders[("henry", "demo")] = [
            _buy("Q/USDT", "1h", Q_ENTRY, 100, Q_FIRST, created=Q_FIRST),
            _buy("Q/USDT", "1h", Q_AVG, 100, Q_DCA, source="dca", signal="BUY_DCA", created=Q_DCA),
        ]
        old_candle = datetime(2026, 9, 28, 6, 0, 0)
        self._run(
            ["henry"],
            prices={"Q/USDT": Q_PX},
            candles={
                "covered": True,
                "price": Q_AVG,
                "candle_open": datetime(2026, 10, 3, 0, 0, 0),
                "reason": None,
            },
        )
        from strategies.positions import _activate, _resolve_store_key

        _activate(_resolve_store_key("demo", "henry"))
        pos = get_position("Q/USDT", "1h")
        cand = evaluate_trailing_stop(
            _mkt("Q/USDT", Q_PX, float(pos.get("average_entry") or Q_AVG)),
            pos,
            _params(),
            now=datetime(2026, 10, 6, 10, 58, 7),
        )
        self.assertIsNone(cand)
        self.assertNotEqual(float(pos.get("recent_high") or 0), Q_PEAK)
        self.assertTrue(all(old_candle.isoformat() not in msg for _lvl, msg in self.logs))

    def test_t18_same_key_two_tenants_and_restore(self):
        key = get_key("BTC/USDT", "4h")
        base = {
            "amount": 1.0,
            "peak_amount": 1.5,
            "sold_percent": 0.2,
            "average_entry": 100.0,
            "last_buy_price": 90.0,
            "recent_high": 150.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-01T00:00:00",
            "first_buy_at": "2026-09-01T00:00:00",
            "side": "long",
        }
        for tid, peak_amount in (("default", 1.5), ("henry", 9.0)):
            row = dict(base)
            row["peak_amount"] = peak_amount
            row["sold_percent"] = 0.2 if tid == "default" else 0.8
            self.docs[(tid if tid != "default" else DEFAULT_TENANT, "demo")] = {
                "positions": {key: row}
            }
            self.orders[(tid if tid != "default" else DEFAULT_TENANT, "demo")] = [
                _buy("BTC/USDT", "4h", 100.0, 1.0, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
                _buy(
                    "BTC/USDT",
                    "4h",
                    90.0,
                    1.0,
                    "2026-10-01T00:00:00",
                    source="dca",
                    signal="BUY_DCA",
                    created="2026-10-01T00:00:00",
                ),
            ]
        self._run(
            ["henry", DEFAULT_TENANT],
            prices={"BTC/USDT": 95.0},
            candles={"covered": True, "price": 96.0, "candle_open": datetime(2026, 10, 2), "reason": None},
        )
        v4 = [msg for lvl, msg in self.logs if "F6 V4" in msg]
        self.assertGreaterEqual(len(v4), 2)
        self.assertTrue(_active_store() is positions)
        # Default peak_amount comes from the order replay (sum of buys), not henry.
        self.assertNotEqual(float(positions[key].get("sold_percent") or 0), 0.8)
        self.assertNotIn("henry-only", positions)

    def test_t18_natural_order_restores_default(self):
        from strategies.positions import _activate, _resolve_store_key

        _activate(_resolve_store_key("demo", DEFAULT_TENANT))
        self._run([DEFAULT_TENANT, "henry"], prices={}, candles={"covered": True, "price": None, "candle_open": None, "reason": "no_candle_after"})
        self.assertTrue(_active_store() is positions)

    def test_t18_exception_restores_previous_tenant(self):
        from core.tenant_routing import tenant_cycle_context
        from strategies.positions import _activate, _resolve_store_key

        _activate(_resolve_store_key("demo", "henry"))
        with patch("storage.tenant_registry.get_tenant", return_value=self._tenant("ctexp")), patch(
            "strategies.positions.bootstrap_positions", lambda *a, **k: None
        ):
            with self.assertRaises(RuntimeError):
                with tenant_cycle_context("ctexp"):
                    raise RuntimeError("boom")
        self.assertEqual(_active_store() is positions, False)
        from strategies import positions as pmod

        self.assertEqual(pmod._active_key[0], "henry")

    def test_t15_second_restart_no_change(self):
        key = get_key("LEG/USDT", "1h")
        row = {
            "amount": 10.0,
            "peak_amount": 10.0,
            "sold_percent": 0.0,
            "average_entry": 1.0,
            "last_buy_price": 1.0,
            "recent_high": 3.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-01T00:00:00",
            "first_buy_at": "2026-09-01T00:00:00",
        }
        self.docs[("henry", "demo")] = {"positions": {key: row}}
        self.orders[("henry", "demo")] = [
            _buy("LEG/USDT", "1h", 2.0, 5, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
            _buy("LEG/USDT", "1h", 1.0, 5, "2026-10-01T00:00:00", source="dca", signal="BUY_DCA", created="2026-10-01T00:00:00"),
        ]
        candle = {"covered": True, "price": 1.2, "candle_open": datetime(2026, 10, 2, 0, 0), "reason": None}
        self._run(["henry"], prices={"LEG/USDT": 1.05}, candles=candle)
        first = [msg for lvl, msg in self.logs if "F6 V4" in msg and "LEG" in msg]
        self.assertTrue(first)
        self.logs.clear()
        # Saved doc now has peak_epoch_at, so the second pass must not re-anchor.
        self._run(["henry"], prices={"LEG/USDT": 1.05}, candles=candle)
        second = [msg for lvl, msg in self.logs if "F6 V4" in msg and "LEG" in msg]
        self.assertEqual(second, [])

    def test_t19_dust_reanchored_not_open(self):
        key = get_key("DUST/USDT", "1h")
        row = {
            "amount": 0.1,
            "peak_amount": 0.1,
            "sold_percent": 0.0,
            "average_entry": 0.5,
            "last_buy_price": 0.5,
            "recent_high": 5.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-01T00:00:00",
            "first_buy_at": "2026-09-01T00:00:00",
        }
        self.assertTrue(has_position_amount(row))
        self.assertFalse(is_open_position(row))
        self.docs[("henry", "demo")] = {"positions": {key: row}}
        self.orders[("henry", "demo")] = [
            _buy("DUST/USDT", "1h", 1.0, 10, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
            _buy(
                "DUST/USDT",
                "1h",
                0.5,
                10,
                "2026-10-01T00:00:00",
                source="dca",
                signal="BUY_DCA",
                created="2026-10-01T00:00:00",
            ),
            _sell("DUST/USDT", "1h", 0.4, 19.9, "2026-10-02T00:00:00"),
        ]
        self._run(
            ["henry"],
            prices={"DUST/USDT": 0.4},
            candles={"covered": True, "price": 0.55, "candle_open": datetime(2026, 10, 2), "reason": None},
        )
        self.assertTrue(any("dust=yes" in msg for _lvl, msg in self.logs))
        from strategies.positions import count_open_positions

        with self._in("henry"):
            pos = get_position("DUST/USDT", "1h")
            self.assertFalse(is_open_position(pos))
            self.assertTrue(pos.get("peak_epoch_at"))
            self.assertEqual(count_open_positions(), 0)

    def test_t23_dust_without_price_is_v2(self):
        key = get_key("NOPX/USDT", "1h")
        row = {
            "amount": 0.1,
            "average_entry": 0.5,
            "last_buy_price": 0.5,
            "recent_high": 4.0,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-01T00:00:00",
            "first_buy_at": "2026-09-01T00:00:00",
            "peak_amount": 0.1,
            "sold_percent": 0.0,
        }
        self.docs[("henry", "demo")] = {"positions": {key: row}}
        self.orders[("henry", "demo")] = [
            _buy("NOPX/USDT", "1h", 0.5, 0.1, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
        ]
        self._run(["henry"], prices={}, candles={"covered": True, "price": 1.0, "candle_open": datetime(2026, 10, 2), "reason": None})
        self.assertTrue(any("no_boot_price" in msg and lvl == "WARNING" for lvl, msg in self.logs))
        self.assertFalse(any("F6 V4" in msg and "NOPX" in msg for _lvl, msg in self.logs))


class TestIssue651K3(unittest.TestCase):
    def test_t26_cycle_change_drops_cache_peak(self):
        key = "RE/USDT_1h"
        snap = {
            key: {
                "amount": 8.0,
                "average_entry": 1.2,
                "recent_high": 1.2,
                "peak_epoch_high": 1.2,
                "peak_epoch_at": "2026-10-05T00:00:00",
                "peak_at": "2026-10-05T00:00:00",
                "v3": False,
                "first_buy_at": "2026-10-05T00:00:00",
                "dca_rounds": 0,
                "last_dca_at": None,
                "last_dca_recovery_at": None,
                "exit_ladder_step": 0,
                "cycle_open_created": "2026-10-05T00:00:00",
                "cycle_open_filled": "2026-10-05T00:00:02",
            }
        }
        cache = {
            "positions": {
                key: {
                    "amount": 0.1,
                    "average_entry": 1.0,
                    "recent_high": 9.0,
                    "v3": True,
                    "first_buy_at": "2026-09-01T00:00:00",
                    "last_dca_at": "2026-08-01T00:00:00",
                    "last_dca_recovery_at": "2026-08-02T00:00:00",
                    "exit_ladder_step": 3,
                    "dca_rounds": 4,
                    "peak_epoch_high": 9.0,
                    "peak_epoch_at": "2026-08-01T00:00:00",
                }
            }
        }
        merged = derive_positions_from_orders_and_cache(snap, cache, tenant_id="henry")
        pos = merged[key]
        self.assertAlmostEqual(float(pos["amount"]), 8.0)
        self.assertAlmostEqual(float(pos["recent_high"]), 1.2)
        self.assertFalse(bool(pos.get("v3")))
        self.assertIsNone(pos.get("last_dca_at"))
        self.assertIsNone(pos.get("last_dca_recovery_at"))
        self.assertEqual(int(pos.get("dca_rounds") or 0), 0)
        self.assertNotEqual(int(pos.get("exit_ladder_step") or 0), 3)

    def test_t26b_same_cycle_keeps_fields(self):
        key = "SAME/USDT_1h"
        created = "2026-10-05T12:00:00"
        filled = "2026-10-05T12:00:01"
        cache_fb = "2026-10-05T12:00:00.500000"
        snap = {
            key: {
                "amount": 5.0,
                "average_entry": 1.0,
                "recent_high": 1.0,
                "first_buy_at": filled,
                "dca_rounds": 0,
                "last_dca_at": None,
                "v3": False,
                "exit_ladder_step": 0,
                "trail_tp_steps": 0,
                "cycle_open_created": created,
                "cycle_open_filled": filled,
            }
        }
        cached_row = {
            "amount": 5.0,
            "average_entry": 1.0,
            "recent_high": 1.7,
            "peak_epoch_high": 1.1,
            "peak_epoch_at": "2026-10-05T13:00:00",
            "peak_at": "2026-10-05T13:00:00",
            "v3": True,
            "first_buy_at": cache_fb,
            "exit_ladder_step": 2,
            "trail_tp_steps": 1,
            "profit_max_lifetime_done": True,
            "time_profit_exit_done": True,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-05T12:30:00",
        }
        merged = derive_positions_from_orders_and_cache(
            snap, {"positions": {key: cached_row}}, tenant_id="henry"
        )
        pos = merged[key]
        self.assertAlmostEqual(float(pos["recent_high"]), 1.7)
        self.assertTrue(pos["v3"])
        self.assertEqual(int(pos["exit_ladder_step"]), 2)
        self.assertEqual(int(pos["trail_tp_steps"]), 1)
        self.assertTrue(pos["profit_max_lifetime_done"])

    def test_t26c_cache_newer_keeps_overlay_and_warns(self):
        key = "NEWER/USDT_1h"
        snap = {
            key: {
                "amount": 9.0,
                "average_entry": 1.0,
                "recent_high": 1.0,
                "first_buy_at": "2026-10-01T00:00:00",
                "v3": False,
                "dca_rounds": 0,
                "cycle_open_created": "2026-10-01T00:00:00",
                "cycle_open_filled": "2026-10-01T00:00:01",
            }
        }
        cached_row = {
            "amount": 2.0,
            "average_entry": 1.0,
            "recent_high": 4.0,
            "v3": True,
            "first_buy_at": "2026-10-06T00:00:00",
            "peak_epoch_high": 4.0,
            "peak_epoch_at": "2026-10-06T01:00:00",
            "peak_at": "2026-10-06T01:00:00",
            "exit_ladder_step": 2,
        }
        with patch("strategies.positions.log") as logged:
            merged = derive_positions_from_orders_and_cache(
                snap, {"positions": {key: cached_row}}, tenant_id="henry"
            )
        pos = merged[key]
        self.assertAlmostEqual(float(pos["recent_high"]), 4.0)
        self.assertTrue(pos["v3"])
        self.assertEqual(int(pos["exit_ladder_step"]), 2)
        warns = [c for c in logged.call_args_list if "WARNING" in c.args]
        self.assertEqual(len(warns), 1)
        text = warns[0].args[0]
        self.assertIn("henry", text)
        self.assertIn("NEWER", text)
        self.assertIn("9.0", text)
        self.assertIn("2.0", text)

    def test_t26d_old_dca_time_and_rounds(self):
        key = "OLD/USDT_1h"
        created = "2026-10-05T00:00:00"
        snap = {
            key: {
                "amount": 4.0,
                "average_entry": 1.0,
                "first_buy_at": created,
                "dca_rounds": 0,
                "dca_recovery_rounds": 0,
                "last_dca_at": None,
                "last_dca_recovery_at": None,
                "recent_high": 1.0,
                "cycle_open_created": created,
                "cycle_open_filled": "2026-10-05T00:00:01",
            }
        }
        cache = {
            "positions": {
                key: {
                    "amount": 4.0,
                    "average_entry": 1.0,
                    "first_buy_at": "2026-10-05T00:00:00.200000",
                    "dca_rounds": 3,
                    "dca_recovery_rounds": 3,
                    "last_dca_at": "2026-09-01T00:00:00",
                    "last_dca_recovery_at": "2026-09-02T00:00:00",
                    "recent_high": 1.4,
                }
            }
        }
        merged = derive_positions_from_orders_and_cache(snap, cache)
        pos = merged[key]
        self.assertIsNone(pos.get("last_dca_at"))
        self.assertIsNone(pos.get("last_dca_recovery_at"))
        self.assertEqual(int(pos.get("dca_rounds") or 0), 0)
        self.assertEqual(int(pos.get("dca_recovery_rounds") or 0), 0)
        self.assertFalse(lot_has_dca_rounds(pos))

        # (iii) cycle change, cached rounds 4, orders 0.
        snap2 = dict(snap)
        snap2[key] = dict(snap[key])
        snap2[key]["cycle_open_created"] = "2026-10-08T00:00:00"
        snap2[key]["first_buy_at"] = "2026-10-08T00:00:00"
        cache2 = {"positions": {key: dict(cache["positions"][key], dca_rounds=4)}}
        merged2 = derive_positions_from_orders_and_cache(snap2, cache2)
        self.assertEqual(int(merged2[key].get("dca_rounds") or 0), 0)

        # (iv) same instant, two spellings, is not a cycle change.
        a = _merged_dca_round(
            0,
            3,
            {"first_buy_at": "2026-10-05T12:00:00", "cycle_open_created": "2026-10-05T12:00:00", "cycle_open_filled": "2026-10-05T12:00:01"},
            {"first_buy_at": "2026-10-05 12:00:00", "last_dca_at": "2026-10-05T12:30:00"},
        )
        b = _merged_dca_round(
            0,
            3,
            {"first_buy_at": "2026-10-05 12:00:00.000000", "cycle_open_created": "2026-10-05 12:00:00.000000", "cycle_open_filled": "2026-10-05T12:00:01"},
            {"first_buy_at": "2026-10-05T12:00:00", "last_dca_at": "2026-10-05T12:30:00"},
        )
        self.assertEqual(a, b)
        self.assertEqual(a, 3)

    def test_t26e_short_timezones_summer_and_winter(self):
        os.environ["TZ"] = "Europe/Berlin"

        def window(day, hhmm, *, before):
            # day is a naive local stamp of the opening created time.
            created = datetime.fromisoformat(day)
            cache_local = created - timedelta(minutes=30) if before else created + timedelta(minutes=10)
            filled = created + timedelta(minutes=20)
            # Short first_buy_at is aware UTC.
            from core.time_utils import process_local_tz

            cache_utc = cache_local.replace(tzinfo=process_local_tz()).astimezone(timezone.utc)
            snap = {
                "cycle_open_created": created.isoformat(sep=" "),
                "cycle_open_filled": filled.isoformat(sep=" "),
                "first_buy_at": created.isoformat(sep=" "),
                "amount": 1.0,
            }
            cached = {
                "first_buy_at": cache_utc.isoformat(),
                "amount": 1.0,
                "side": "short",
            }
            changed, warn = k3_cycle_status(snap, cached, lot_key="SHRT/USDT_1h", tenant_id="henry")
            return changed, warn

        summer_before, _ = window("2026-10-06 12:00:00", "12:00", before=True)
        summer_inside, _ = window("2026-10-06 12:00:00", "12:00", before=False)
        winter_before, _ = window("2026-11-02 12:00:00", "12:00", before=True)
        winter_inside, _ = window("2026-11-02 12:00:00", "12:00", before=False)
        self.assertTrue(summer_before)
        self.assertFalse(summer_inside)
        self.assertTrue(winter_before)
        self.assertFalse(winter_inside)

        # A fixed +2h reading of the naive created stamp misses the winter case.
        # Berlin that day is UTC+1, so 11:30 local is 10:30 UTC, which is not
        # before 12:00 mis-tagged as UTC+2 (10:00 UTC).
        from core.time_utils import process_local_tz

        created = datetime.fromisoformat("2026-11-02 12:00:00")
        cache_local = created - timedelta(minutes=30)
        real_cache = cache_local.replace(tzinfo=process_local_tz()).astimezone(timezone.utc)
        created_as_plus2 = created.replace(tzinfo=timezone(timedelta(hours=2))).astimezone(
            timezone.utc
        )
        self.assertFalse(real_cache < created_as_plus2)

    def test_t26b_reload_and_satellite_keep_same_cycle(self):
        """Restart load and a satellite reload both keep the in-window cache."""
        key = "SAME/USDT_1h"
        created = "2026-10-05T12:00:00"
        filled = "2026-10-05T12:00:01"
        cache_fb = "2026-10-05T12:00:00.500000"
        snap = {
            key: {
                "amount": 5.0,
                "average_entry": 1.0,
                "recent_high": 1.0,
                "first_buy_at": filled,
                "dca_rounds": 0,
                "last_dca_at": None,
                "v3": False,
                "exit_ladder_step": 0,
                "trail_tp_steps": 0,
                "cycle_open_created": created,
                "cycle_open_filled": filled,
            }
        }
        cached_row = {
            "amount": 5.0,
            "average_entry": 1.0,
            "recent_high": 1.7,
            "peak_epoch_high": 1.1,
            "peak_epoch_at": "2026-10-05T13:00:00",
            "peak_at": "2026-10-05T13:00:00",
            "v3": True,
            "first_buy_at": cache_fb,
            "exit_ladder_step": 2,
            "trail_tp_steps": 1,
            "profit_max_lifetime_done": True,
            "time_profit_exit_done": True,
            "dca_rounds": 1,
            "last_dca_at": "2026-10-05T12:30:00",
        }
        reset_all_position_stores_for_tests()
        cache_doc = {"positions": {key: dict(cached_row)}}
        with patch(
            "services.ledger_sync._build_positions_snapshot_from_orders",
            return_value=snap,
        ), patch(
            "data_manager.load_positions_document", return_value=cache_doc
        ), patch(
            "strategies.positions.load_positions_document", return_value=cache_doc
        ):
            loaded = load_positions(scope="demo", tenant_id="henry")
            from strategies.positions import (
                _resolve_store_key,
                _store_for_key,
                activate_tenant_positions,
            )

            activate_tenant_positions(scope="demo", tenant_id="henry")
            sat = dict(_store_for_key(_resolve_store_key("demo", "henry"))["SAME/USDT_1h"])
        self.assertTrue(loaded[key]["v3"])
        self.assertAlmostEqual(float(loaded[key]["recent_high"]), 1.7)
        self.assertEqual(int(loaded[key]["exit_ladder_step"]), 2)
        self.assertTrue(sat["v3"])
        self.assertAlmostEqual(float(sat["recent_high"]), 1.7)
        self.assertEqual(int(sat["trail_tp_steps"]), 1)
        self.assertTrue(sat["profit_max_lifetime_done"])


class TestIssue651Remainder(unittest.TestCase):
    """New proofs. Helpers come from the startup harness without re-collecting its tests."""

    setUp = TestIssue651Startup.setUp
    tearDown = TestIssue651Startup.tearDown
    _log = TestIssue651Startup._log
    _in = TestIssue651Startup._in
    _tenant = TestIssue651Startup._tenant
    _patched = TestIssue651Startup._patched
    _run = TestIssue651Startup._run
    def _seed_dca(self, tenant, symbol, tf, row, orders):
        key = get_key(symbol, tf)
        self.docs[(tenant, "demo")] = {"positions": {key: row}}
        self.orders[(tenant, "demo")] = orders
        return key

    def _henry(self, symbol, tf="1h"):
        with self._in("henry"):
            return dict(get_position(symbol, tf))

    def test_t3_restart_does_not_raise_above_post_epoch_highs(self):
        epoch = "2026-10-02T05:00:00"
        key = self._seed_dca(
            "henry",
            "POST/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 1.0,
                "last_buy_price": 1.0,
                "recent_high": 1.02,
                "dca_rounds": 1,
                "last_dca_at": epoch,
                "first_buy_at": "2026-09-01T00:00:00",
                "peak_epoch_high": 1.02,
                "peak_epoch_at": epoch,
                "peak_at": epoch,
                "v3": False,
            },
            [
                _buy("POST/USDT", "1h", 1.0, 5, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
                _buy("POST/USDT", "1h", 1.0, 5, epoch, source="dca", signal="BUY_DCA", created=epoch),
            ],
        )
        seen = []

        def infer(symbol, timeframe, since=None, **kwargs):
            seen.append(str(since or ""))
            if since and epoch[:13] in str(since):
                return {
                    "covered": True,
                    "price": 1.08,
                    "candle_open": datetime(2026, 10, 3, 0, 0, 0),
                    "reason": None,
                }
            return {
                "covered": True,
                "price": 9.0,
                "candle_open": datetime(2026, 9, 28, 6, 0, 0),
                "reason": None,
            }

        self._run(["henry"], prices={"POST/USDT": 1.03}, infer=infer)
        pos = self._henry("POST/USDT")
        self.assertLess(float(pos["recent_high"]), 1.09)
        self.assertNotAlmostEqual(float(pos["recent_high"]), 9.0)
        self.assertTrue(seen)
        self.assertTrue(all(epoch[:13] in s or "2026-10-02" in s for s in seen))
        self.assertIn(key, self.docs[("henry", "demo")]["positions"])

    def test_t4b_new_peak_below_old_stored_peak_fires(self):
        dca = "2026-09-01T00:00:00"
        self._seed_dca(
            "henry",
            "LOW/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 1.0,
                "last_buy_price": 1.0,
                "recent_high": 5.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
            },
            [
                _buy("LOW/USDT", "1h", 1.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy("LOW/USDT", "1h", 1.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(
            ["henry"],
            prices={"LOW/USDT": 1.25},
            candles={
                "covered": True,
                "price": 1.30,
                "candle_open": datetime(2026, 9, 2, 0, 0, 0),
                "reason": None,
            },
        )
        with self._in("henry"):
            pos = get_position("LOW/USDT", "1h")
            self.assertLess(float(pos["recent_high"]), 5.0)
            self.assertFalse(bool(pos.get("v3")))
            with patch("strategies.positions.flush_positions", lambda *a, **k: None):
                update_market_snapshot("LOW/USDT", "1h", 1.40)
            pos = get_position("LOW/USDT", "1h")
        self.assertLess(float(pos["recent_high"]), 5.0)
        cand = evaluate_trailing_stop(
            _mkt("LOW/USDT", 1.20, float(pos["average_entry"])),
            pos,
            _params(),
            now=datetime(2026, 10, 7, 12, 0, 0),
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_stop")

    def test_t4c_fallen_boot_price_is_v3_and_does_not_sell(self):
        dca = "2026-09-01T00:00:00"

        def infer(symbol, timeframe, since=None, **kwargs):
            if since and "2026-09" in str(since):
                return {
                    "covered": True,
                    "price": 120.0,
                    "candle_open": datetime(2026, 9, 2),
                    "reason": None,
                }
            return {"covered": True, "price": None, "candle_open": None, "reason": "no_candle_after"}

        self._seed_dca(
            "henry",
            "FALL/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 100.0,
                "last_buy_price": 100.0,
                "recent_high": 400.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
            },
            [
                _buy("FALL/USDT", "1h", 100.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy("FALL/USDT", "1h", 100.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(["henry"], prices={"FALL/USDT": 99.5}, infer=infer)
        with self._in("henry"):
            pos = get_position("FALL/USDT", "1h")
        self.assertTrue(bool(pos.get("v3")))
        self.assertAlmostEqual(float(pos["recent_high"]), 100.0)
        now = datetime(2026, 10, 7, 12, 0, 0)
        self.assertIsNone(
            evaluate_trailing_stop(_mkt("FALL/USDT", 99.5, 100.0), pos, _params(), now=now)
        )
        self.assertIsNone(
            evaluate_trailing_take_profit(_mkt("FALL/USDT", 99.5, 100.0), pos, _params(), now=now)
        )

    def test_t7_fields_survive_f6a_then_load(self):
        epoch = "2026-10-02T05:31:46"
        key = self._seed_dca(
            "henry",
            "KEEP/USDT",
            "1h",
            {
                "amount": 5.0,
                "peak_amount": 5.0,
                "sold_percent": 0.0,
                "average_entry": 1.0,
                "recent_high": 1.2,
                "peak_epoch_high": 1.1,
                "peak_epoch_at": epoch,
                "peak_at": "2026-10-03T00:00:00",
                "v3": True,
                "first_buy_at": "2026-10-01T00:00:00",
                "dca_rounds": 1,
                "last_dca_at": epoch,
            },
            [
                _buy("KEEP/USDT", "1h", 1.0, 5, "2026-10-01T00:00:00", created="2026-10-01T00:00:00"),
                _buy("KEEP/USDT", "1h", 1.0, 5, epoch, source="dca", signal="BUY_DCA", created=epoch),
            ],
        )
        from services.ledger_sync import run_per_tenant_startup

        with self._patched(["henry"], prices={"KEEP/USDT": 1.05}):
            run_per_tenant_startup("demo", include_legacy_reanchor=True)
            loaded = load_positions(scope="demo", tenant_id="henry")
        got = loaded[key]
        self.assertEqual(got.get("peak_epoch_at"), epoch)
        self.assertTrue(got.get("v3"))
        self.assertAlmostEqual(float(got["peak_epoch_high"]), 1.1)
        self.assertTrue(got.get("peak_at"))

    def test_t9_old_candle_and_missing_epoch_give_no_hint(self):
        from services.ledger_sync import _f2_peak_hint
        from services.market_service import MarketService

        svc = unittest.mock.Mock()
        legacy = {
            "dca_rounds": 2,
            "last_dca_at": "2026-10-02T05:31:46",
            "first_buy_at": "2026-09-01T00:00:00",
            "recent_high": 9.0,
        }
        hint, when = _f2_peak_hint(svc, "Q/USDT", "1h", legacy)
        self.assertIsNone(hint)
        self.assertIsNone(when)
        svc.infer_ohlcv_peak_price.assert_not_called()

        epoch = "2026-10-02T05:00:00"
        stamped = dict(legacy, peak_epoch_at=epoch, peak_epoch_high=1.0, peak_at=epoch)
        svc.infer_ohlcv_peak_price.return_value = {
            "covered": True,
            "price": 1.1,
            "candle_open": datetime(2026, 10, 3),
            "reason": None,
        }
        hint, when = _f2_peak_hint(svc, "Q/USDT", "1h", stamped)
        called_since = svc.infer_ohlcv_peak_price.call_args.args[2]
        self.assertEqual(called_since, epoch)
        self.assertNotEqual(called_since, legacy["first_buy_at"])
        self.assertEqual(hint, 1.1)

        # The candle that opens on the fill itself is not a post-fill high.
        fill = datetime(2026, 10, 2, 5, 0, 0)
        fill_ms = fill.timestamp() * 1000
        bar = 3600 * 1000
        import pandas as pd

        df_fill_only = pd.DataFrame(
            [
                {"ts": fill_ms - bar, "high": 0.5, "open": 0.5, "low": 0.5, "close": 0.5, "volume": 1},
                {"ts": fill_ms, "high": 9.0, "open": 9.0, "low": 9.0, "close": 9.0, "volume": 1},
            ]
        )
        market = MarketService()
        with patch.object(market, "_fetch_ohlcv", return_value=df_fill_only):
            meta = market.infer_ohlcv_peak_price(
                "Q/USDT", "1h", fill.isoformat(sep=" "), with_meta=True, page_to_since=True
            )
        self.assertTrue(meta["covered"])
        self.assertIsNone(meta["price"])
        self.assertFalse(recent_high_reached_after_dca(stamped))

    def test_t14a_save_then_load_positions(self):
        dca = datetime(2026, 10, 2, 5, 31, 46)
        older = dca - timedelta(hours=3)
        key = get_key("SYM/USDT", "1h")
        pos = {
            "amount": 5.0,
            "average_entry": 1.0,
            "recent_high": 1.3,
            "peak_epoch_high": 1.05,
            "peak_epoch_at": older.isoformat(),
            "dca_rounds": 2,
            "last_dca_at": dca.isoformat(),
            "peak_at": older.isoformat(),
            "first_buy_at": "2026-10-01T00:00:00",
        }
        snap = {
            key: {
                **pos,
                "amount": 5.0,
                "cycle_open_created": "2026-10-01T00:00:00",
                "cycle_open_filled": "2026-10-01T00:00:01",
            }
        }
        with patch(
            "services.ledger_sync._build_positions_snapshot_from_orders",
            return_value=snap,
        ), patch(
            "data_manager.load_positions_document",
            return_value={"positions": {key: dict(pos)}},
        ), patch("data_manager.save_positions_document", return_value=True), patch(
            "bus.locks.ledger_lock", return_value=_NULL_CTX
        ):
            apply_positions_snapshot({key: pos}, scope="demo")
            from strategies.positions import flush_positions

            flush_positions(scope="demo", force=True)
            loaded = load_positions(scope="demo", tenant_id="henry")
        self.assertFalse(recent_high_reached_after_dca(loaded[key]))

    def test_t15_v3_survives_second_restart(self):
        dca = "2026-09-01T00:00:00"

        def infer(symbol, timeframe, since=None, **kwargs):
            if since and "2026-09" in str(since):
                return {
                    "covered": True,
                    "price": 120.0,
                    "candle_open": datetime(2026, 9, 2),
                    "reason": None,
                }
            return {"covered": True, "price": None, "candle_open": None, "reason": "no_candle_after"}

        self._seed_dca(
            "henry",
            "V3/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 100.0,
                "last_buy_price": 100.0,
                "recent_high": 400.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
            },
            [
                _buy("V3/USDT", "1h", 100.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy("V3/USDT", "1h", 100.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(["henry"], prices={"V3/USDT": 99.5}, infer=infer)
        self.assertTrue(any("F6 V4" in msg and "v3=True" in msg for _lvl, msg in self.logs))
        self.logs.clear()
        self._run(["henry"], prices={"V3/USDT": 99.5}, infer=infer)
        pos = self._henry("V3/USDT")
        self.assertTrue(bool(pos.get("v3")))
        self.assertFalse(any("F6 V4" in msg for _lvl, msg in self.logs))
        self.assertIsNone(
            evaluate_trailing_stop(
                _mkt("V3/USDT", 99.5, 100.0), pos, _params(), now=datetime(2026, 10, 7)
            )
        )

    def test_t16_v2_missing_time_and_candles(self):
        self._seed_dca(
            "henry",
            "NODCA/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 1.0,
                "last_buy_price": 1.0,
                "recent_high": 4.0,
                "dca_rounds": 2,
                "first_buy_at": "2026-10-01T00:00:00.200000",
            },
            [
                _buy("NODCA/USDT", "1h", 1.0, 10, "2026-10-01T00:00:01", created="2026-10-01T00:00:00"),
            ],
        )
        self._run(
            ["henry"],
            prices={"NODCA/USDT": 1.1},
            candles={"covered": True, "price": 3.0, "candle_open": datetime(2026, 10, 2), "reason": None},
        )
        pos = self._henry("NODCA/USDT")
        self.assertIsNone(pos.get("peak_epoch_at"))
        self.assertAlmostEqual(float(pos["recent_high"]), 4.0)
        self.assertTrue(any("missing_dca_time" in msg and lvl == "WARNING" for lvl, msg in self.logs))
        self.assertIsNone(
            evaluate_trailing_stop(_mkt("NODCA/USDT", 1.1, 1.0), pos, _params(), now=datetime(2026, 10, 7))
        )

        self.logs.clear()
        self.docs.clear()
        self.orders.clear()
        dca = "2026-09-01T00:00:00"
        self._seed_dca(
            "henry",
            "NOCAN/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 1.0,
                "last_buy_price": 1.0,
                "recent_high": 4.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
            },
            [
                _buy("NOCAN/USDT", "1h", 1.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy("NOCAN/USDT", "1h", 1.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(
            ["henry"],
            prices={"NOCAN/USDT": 1.1},
            candles={"covered": False, "price": None, "candle_open": None, "reason": "not_covered"},
        )
        pos = self._henry("NOCAN/USDT")
        self.assertIsNone(pos.get("peak_epoch_at"))
        self.assertAlmostEqual(float(pos["recent_high"]), 4.0)
        self.assertFalse(any(msg.startswith("2026-10-07") and "peak_epoch" in msg for _lvl, msg in self.logs))
        self.assertTrue(any("not_covered" in msg and lvl == "WARNING" for lvl, msg in self.logs))
        self.assertIsNone(
            evaluate_trailing_stop(_mkt("NOCAN/USDT", 1.1, 1.0), pos, _params(), now=datetime(2026, 10, 7))
        )

    def test_t18_satellite_does_not_change_default_lots(self):
        key = get_key("BTC/USDT", "4h")
        only = get_key("ONLYDEF/USDT", "4h")
        positions[key] = {
            "amount": 1.0,
            "peak_amount": 1.5,
            "sold_percent": 0.2,
            "average_entry": 100.0,
            "recent_high": 110.0,
        }
        positions[only] = {
            "amount": 2.0,
            "peak_amount": 2.0,
            "sold_percent": 0.1,
            "average_entry": 3.0,
        }
        before = set(positions)
        self._seed_dca(
            "henry",
            "BTC/USDT",
            "4h",
            {
                "amount": 1.0,
                "peak_amount": 9.0,
                "sold_percent": 0.8,
                "average_entry": 100.0,
                "last_buy_price": 90.0,
                "recent_high": 150.0,
                "dca_rounds": 1,
                "last_dca_at": "2026-10-01T00:00:00",
                "first_buy_at": "2026-09-01T00:00:00",
            },
            [
                _buy("BTC/USDT", "4h", 100.0, 1.0, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
                _buy(
                    "BTC/USDT",
                    "4h",
                    90.0,
                    1.0,
                    "2026-10-01T00:00:00",
                    source="dca",
                    signal="BUY_DCA",
                    created="2026-10-01T00:00:00",
                ),
            ],
        )
        self._run(
            ["henry"],
            prices={"BTC/USDT": 95.0},
            candles={"covered": True, "price": 96.0, "candle_open": datetime(2026, 10, 2), "reason": None},
        )
        self.assertEqual(float(positions[key]["peak_amount"]), 1.5)
        self.assertEqual(float(positions[key]["sold_percent"]), 0.2)
        self.assertEqual(set(positions), before)
        self.assertTrue(_active_store() is positions)
        henry_saves = [tid for tid, _scope, _payload in self.saves]
        self.assertNotIn(DEFAULT_TENANT, henry_saves)

    def test_t18_natural_flush_writes_default_only(self):
        from strategies.positions import _activate, _resolve_store_key, flush_positions

        _activate(_resolve_store_key("demo", DEFAULT_TENANT))
        for tid, sold in ((DEFAULT_TENANT, 0.2), ("henry", 0.8)):
            self._seed_dca(
                tid,
                "BTC/USDT",
                "4h",
                {
                    "amount": 1.0,
                    "peak_amount": 1.5 if tid == DEFAULT_TENANT else 9.0,
                    "sold_percent": sold,
                    "average_entry": 100.0,
                    "last_buy_price": 90.0,
                    "recent_high": 150.0,
                    "dca_rounds": 1,
                    "last_dca_at": "2026-10-01T00:00:00",
                    "first_buy_at": "2026-09-01T00:00:00",
                },
                [
                    _buy("BTC/USDT", "4h", 100.0, 1, "2026-09-01T00:00:00", created="2026-09-01T00:00:00"),
                    _buy(
                        "BTC/USDT",
                        "4h",
                        90.0,
                        1,
                        "2026-10-01T00:00:00",
                        source="dca",
                        signal="BUY_DCA",
                        created="2026-10-01T00:00:00",
                    ),
                ],
            )
        candles = {"covered": True, "price": 96.0, "candle_open": datetime(2026, 10, 2), "reason": None}
        self._run([DEFAULT_TENANT, "henry"], prices={"BTC/USDT": 95.0}, candles=candles)
        self.assertTrue(_active_store() is positions)
        self.saves.clear()
        with self._patched([DEFAULT_TENANT, "henry"], prices={"BTC/USDT": 95.0}, candles=candles):
            flush_positions(scope="demo", force=True)
        self.assertTrue(self.saves)
        tid, _scope, payload = self.saves[-1]
        self.assertEqual(tid, DEFAULT_TENANT)
        row = payload["positions"][get_key("BTC/USDT", "4h")]
        self.assertNotEqual(float(row.get("sold_percent") or 0), 0.8)

    def test_t20_pages_past_200_and_fail_closed(self):
        import pandas as pd
        from services.market_service import MarketService

        market = MarketService()
        start = datetime(2026, 1, 1, 0, 0, 0)
        start_ms = start.timestamp() * 1000
        bar = 3600 * 1000
        calls = []

        def fetch(symbol, timeframe, limit, since_ms=None):
            calls.append(since_ms)
            if since_ms is None:
                tail = start_ms + 250 * bar
                return pd.DataFrame(
                    [{"ts": tail + i * bar, "high": 1.0, "open": 1, "low": 1, "close": 1, "volume": 1} for i in range(200)]
                )
            rows = []
            for i in range(30):
                ts = float(since_ms) + i * bar
                high = 7.5 if i == 3 else 1.0
                rows.append({"ts": ts, "high": high, "open": 1, "low": 1, "close": 1, "volume": 1})
            return pd.DataFrame(rows)

        with patch.object(market, "_fetch_ohlcv", side_effect=fetch):
            meta = market.infer_ohlcv_peak_price(
                "OLD/USDT", "1h", start.isoformat(sep=" "), with_meta=True, page_to_since=True
            )
        self.assertTrue(meta["covered"])
        self.assertAlmostEqual(meta["price"], 7.5)
        self.assertTrue(any(c is not None for c in calls))

        def short_page(symbol, timeframe, limit, since_ms=None):
            late = start_ms + 400 * bar
            return pd.DataFrame(
                [{"ts": late, "high": 3.0, "open": 1, "low": 1, "close": 1, "volume": 1}]
            )

        with patch.object(market, "_fetch_ohlcv", side_effect=short_page):
            uncovered = market.infer_ohlcv_peak_price(
                "OLD/USDT", "1h", start.isoformat(sep=" "), with_meta=True, page_to_since=True
            )
        self.assertFalse(uncovered["covered"])
        self.assertEqual(uncovered["reason"], "not_covered")
        self.assertIsNone(uncovered["price"])

        def boom(symbol, timeframe, limit, since_ms=None):
            raise AssertionError("full window searched")

        with patch.object(market, "_fetch_ohlcv", side_effect=boom):
            bad = market.infer_ohlcv_peak_price(
                "OLD/USDT", "1h", "not-a-timestamp", with_meta=True, page_to_since=True
            )
        self.assertFalse(bad["covered"])
        self.assertEqual(bad["reason"], "unparseable_since")
        self.assertIsNone(bad["price"])

    def test_t21_search_start_latest_buy(self):
        from core.time_utils import ledger_datetime_utc
        from services.ledger_sync import _f6_search_start

        dca = "2026-09-01T00:00:00"
        later = "2026-10-03T04:00:00"
        pos = {
            "dca_rounds": 1,
            "last_dca_at": dca,
            "first_buy_at": "2026-08-01T00:00:00",
        }
        orders = [
            _buy("ZRO/USDT", "1h", 1.0, 1, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
            _buy("ZRO/USDT", "1h", 0.8, 1, dca, source="dca", signal="BUY_DCA", created=dca),
            _buy("ZRO/USDT", "1h", 0.9, 1, later, created=later),
        ]
        with patch("data_manager.load_orders", return_value={"orders": orders}):
            start, source = _f6_search_start(
                pos, symbol="ZRO/USDT", timeframe="1h", scope="demo", tenant_id="henry"
            )
        self.assertEqual(source, "buy")
        self.assertEqual(ledger_datetime_utc(start), ledger_datetime_utc(later))

        # ZRO pattern: last_dca_at sits before this cycle's first buy.
        first = "2026-09-20T08:00:00"
        pos2 = {
            "dca_rounds": 1,
            "last_dca_at": "2026-08-01T00:00:00",
            "first_buy_at": first,
        }
        orders2 = [
            _buy("ZRO/USDT", "1h", 1.2, 2, first, created=first),
        ]
        with patch("data_manager.load_orders", return_value={"orders": orders2}):
            start2, source2 = _f6_search_start(
                pos2, symbol="ZRO/USDT", timeframe="1h", scope="demo", tenant_id="henry"
            )
        self.assertEqual(ledger_datetime_utc(start2), ledger_datetime_utc(first))
        self.assertNotEqual(ledger_datetime_utc(start2), ledger_datetime_utc("2026-08-01T00:00:00"))
        self.assertIn(source2, ("buy", "first_buy"))

    def _boot_v3(self, symbol):
        dca = "2026-09-01T00:00:00"

        def infer(sym, timeframe, since=None, **kwargs):
            if since and "2026-09" in str(since):
                return {
                    "covered": True,
                    "price": 120.0,
                    "candle_open": datetime(2026, 9, 2),
                    "reason": None,
                }
            return {"covered": True, "price": None, "candle_open": None, "reason": "no_candle_after"}

        self._seed_dca(
            "henry",
            symbol,
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 100.0,
                "last_buy_price": 100.0,
                "recent_high": 400.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
            },
            [
                _buy(symbol, "1h", 100.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy(symbol, "1h", 100.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(["henry"], prices={symbol: 99.5}, infer=infer)
        return self._henry(symbol)

    def test_t22_v3_lifecycle(self):
        symbol = "WSFIX/USDT"
        self.assertNotIn(get_key(symbol, "1h"), positions)
        pos = self._boot_v3(symbol)
        self.assertTrue(bool(pos.get("v3")))
        floor = float(pos["recent_high"])
        self.assertAlmostEqual(floor, 100.0)
        now = datetime(2026, 10, 7, 12, 0, 0)

        # (a) sideways ticks and candle highs at the floor: no trail, no TTP.
        with self._in("henry"):
            with patch("strategies.positions.flush_positions", lambda *a, **k: None):
                update_market_snapshot(symbol, "1h", floor, peak_hint=floor)
            pos = get_position(symbol, "1h")
        self.assertTrue(bool(pos.get("v3")))
        self.assertIsNone(evaluate_trailing_stop(_mkt(symbol, floor, 100.0), pos, _params(), now=now))
        self.assertIsNone(
            evaluate_trailing_take_profit(_mkt(symbol, floor, 100.0), pos, _params(), now=now)
        )
        events = evaluate_would_sells(
            symbol=symbol, timeframe="1h", price=floor, position=dict(pos), strategy_params=_params()
        )
        self.assertFalse(events)
        with self._in("henry"):
            self.assertTrue(get_position(symbol, "1h").get("v3"))

        # (c) restart through F6a + load keeps v3 and does not arm.
        from services.ledger_sync import run_per_tenant_startup

        with self._patched(["henry"], prices={symbol: 99.5}):
            run_per_tenant_startup("demo", include_legacy_reanchor=True)
            loaded = load_positions(scope="demo", tenant_id="henry")
        key = get_key(symbol, "1h")
        self.assertTrue(loaded[key].get("v3"))
        self.assertIsNone(
            evaluate_trailing_stop(_mkt(symbol, 99.5, 100.0), loaded[key], _params(), now=now)
        )

        # (d) above the floor, below activation: v3 clears, no sell.
        before = len([msg for _lvl, msg in self.logs if "v3 cleared" in msg])
        with self._in("henry"), patch(
            "strategies.positions.flush_positions", lambda *a, **k: None
        ), patch("strategies.positions.log", side_effect=self._log):
            before_pos = dict(get_position(symbol, "1h"))
            update_market_snapshot(symbol, "1h", 102.0)
            pos = get_position(symbol, "1h")
        self.assertFalse(bool(pos.get("v3")))
        self.assertGreater(
            len([msg for _lvl, msg in self.logs if "v3 cleared" in msg]), before
        )
        self.assertIsNone(evaluate_trailing_stop(_mkt(symbol, 102.0, 100.0), pos, _params(), now=now))

        # (b) a fresh V3 lot: high at activation, then a trail-distance fall sells.
        self.docs.clear()
        self.orders.clear()
        self.logs.clear()
        pos = self._boot_v3(symbol)
        self.assertTrue(pos.get("v3"))
        with self._in("henry"), patch(
            "strategies.positions.flush_positions", lambda *a, **k: None
        ), patch("strategies.positions.log", side_effect=self._log):
            update_market_snapshot(symbol, "1h", 120.0)
            pos = get_position(symbol, "1h")
        self.assertFalse(bool(pos.get("v3")))
        self.assertTrue(any("v3 cleared" in msg and symbol in msg for _lvl, msg in self.logs))
        cand = evaluate_trailing_stop(_mkt(symbol, 110.0, 100.0), pos, _params(), now=now)
        self.assertIsNotNone(cand)

        # (e) a DCA fill clears v3.
        self.docs.clear()
        self.orders.clear()
        pos = self._boot_v3(symbol)
        with self._in("henry"), patch(
            "strategies.positions.flush_positions", lambda *a, **k: None
        ), patch(
            "strategies.registry.resolve_strategy_params", return_value=_params()
        ), patch(
            "data_manager.get_config", return_value={"trading_mode": "paper"}
        ), patch(
            "data_manager._load_tenant_config_body", return_value={}
        ):
            update_position(symbol, "1h", "BUY_DCA", 90.0, amount_traded=1, source="dca")
            pos = get_position(symbol, "1h")
        self.assertFalse(bool(pos.get("v3")))

        # (f) WS-only spike arms the copy; the store keeps v3.
        self.docs.clear()
        self.orders.clear()
        pos = self._boot_v3(symbol)
        store_before = dict(pos)
        copy = dict(store_before)
        if 130.0 > float(copy.get("recent_high") or 0):
            copy["recent_high"] = 130.0
        self.assertTrue(recent_high_reached_after_dca(copy))
        fallen = dict(copy)
        fallen_cand = evaluate_trailing_stop(_mkt(symbol, 110.0, 100.0), fallen, _params(), now=now)
        self.assertIsNotNone(fallen_cand)
        with self._in("henry"):
            store_after = get_position(symbol, "1h")
        self.assertTrue(bool(store_after.get("v3")))
        self.assertAlmostEqual(float(store_after["recent_high"]), float(store_before["recent_high"]))
        self.assertNotIn(get_key(symbol, "1h"), positions)

        # (g) hint that does not beat the price stamps now, not the candle.
        candle = datetime(2026, 1, 1, 0, 0, 0)
        with self._in("henry"), patch(
            "strategies.positions.flush_positions", lambda *a, **k: None
        ):
            live = get_position(symbol, "1h")
            live["recent_high"] = 1.0
            update_market_snapshot(symbol, "1h", 1.5, peak_hint=1.2, high_at=candle)
            stamped = get_position(symbol, "1h")
        self.assertAlmostEqual(float(stamped["recent_high"]), 1.5)
        self.assertNotIn("2026-01-01", str(stamped.get("peak_at")))

    def test_t24_sidecar_never_reanchors_main_bot_does(self):
        from services.exit_radar.sidecar.__main__ import _sync_ledger
        from aria_bot import _run_ledger_startup_sync, reset_ledger_startup_sync_for_tests

        with patch("storage.mongo_client.ping_database", return_value=True), patch(
            "storage.mongo_client.resolve_database_name", return_value="xagent_test"
        ), patch("data_manager.resolve_ledger_scope", return_value="demo"), patch(
            "services.ledger_sync.rebuild_positions_from_orders"
        ) as rebuild, patch("services.ledger_sync.sync_positions_on_startup") as sync, patch(
            "bus.writer_lease.lease_enabled", return_value=False
        ), patch("bus.writer_lease.writer_lease_held", return_value=False), patch(
            "services.ledger_sync.reanchor_legacy_dca_peaks"
        ) as reanchor:
            _sync_ledger()
        sync.assert_called_once()
        self.assertFalse(sync.call_args.kwargs.get("include_legacy_reanchor", True))
        reanchor.assert_not_called()

        sync.reset_mock()
        with patch("storage.mongo_client.ping_database", return_value=True), patch(
            "storage.mongo_client.resolve_database_name", return_value="xagent_test"
        ), patch("data_manager.resolve_ledger_scope", return_value="demo"), patch(
            "services.ledger_sync.rebuild_positions_from_orders"
        ), patch("services.ledger_sync.sync_positions_on_startup") as sync, patch(
            "bus.writer_lease.lease_enabled", return_value=True
        ), patch("bus.writer_lease.writer_lease_held", return_value=False):
            _sync_ledger()
        sync.assert_not_called()

        reset_ledger_startup_sync_for_tests()
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), patch(
            "bus.writer_lease.writer_lease_held", return_value=True
        ), patch("data_manager.resolve_ledger_scope", return_value="demo"), patch(
            "data_manager.reconcile_demo_trade_history_on_startup"
        ), patch("strategies.positions.flush_positions"), patch(
            "services.ledger_sync.run_per_tenant_startup"
        ) as per_tenant:
            _run_ledger_startup_sync()
        per_tenant.assert_called_once()
        self.assertTrue(per_tenant.call_args.kwargs.get("include_legacy_reanchor"))

    def test_t25_ttp_only_boot_candidate_is_v3(self):
        dca = "2026-09-01T00:00:00"
        params = _params()
        params["trailing_stop"] = dict(params["trailing_stop"], enabled=False)

        def infer(symbol, timeframe, since=None, **kwargs):
            if since and "2026-09" in str(since):
                return {
                    "covered": True,
                    "price": 120.0,
                    "candle_open": datetime(2026, 9, 2),
                    "reason": None,
                }
            return {"covered": True, "price": None, "candle_open": None, "reason": "no_candle_after"}

        self._seed_dca(
            "henry",
            "TTPONLY/USDT",
            "1h",
            {
                "amount": 10.0,
                "peak_amount": 10.0,
                "sold_percent": 0.0,
                "average_entry": 100.0,
                "last_buy_price": 100.0,
                "recent_high": 400.0,
                "dca_rounds": 1,
                "last_dca_at": dca,
                "first_buy_at": "2026-08-01T00:00:00",
                "trail_tp_steps": 0,
            },
            [
                _buy("TTPONLY/USDT", "1h", 100.0, 10, "2026-08-01T00:00:00", created="2026-08-01T00:00:00"),
                _buy("TTPONLY/USDT", "1h", 100.0, 10, dca, source="dca", signal="BUY_DCA", created=dca),
            ],
        )
        self._run(["henry"], prices={"TTPONLY/USDT": 110.0}, infer=infer, params=params)
        pos = self._henry("TTPONLY/USDT")
        self.assertTrue(bool(pos.get("v3")))
        self.assertAlmostEqual(float(pos["recent_high"]), 110.0)
        self.assertTrue(any("v3=True" in msg and "TTPONLY" in msg for _lvl, msg in self.logs))


if __name__ == "__main__":
    unittest.main()
