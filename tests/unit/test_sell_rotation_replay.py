"""Unit tests for hermes.sell_rotation_replay."""

from __future__ import annotations

from datetime import datetime, timedelta

from hermes.sell_rotation_replay import (
    POLICIES,
    build_cycles,
    compare_policies,
    compute_recovery_eligibility,
    compute_tail_slot_snapshot,
    forward_open_analysis,
    infer_sell_category,
    is_partial_signal,
    open_cycles_now,
    simulate_policy,
    validate_plan_gate1,
)


def _ts(offset_hours: float = 0) -> str:
    return (datetime(2026, 7, 1, 12, 0, 0) + timedelta(hours=offset_hours)).isoformat()


def _buy(symbol="TEST/USDT", usdt=1000.0, price=1.0, amount=1000.0, h=0.0):
    return {
        "status": "filled",
        "side": "buy",
        "symbol": symbol,
        "timeframe": "4h",
        "signal": "BUY",
        "timestamps": {"filled": _ts(h)},
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "pnl": 0,
    }


def _sell(symbol="TEST/USDT", usdt=300.0, price=1.1, amount=272.7, signal="SELL_PARTIAL_30", pnl=30.0, h=1.0):
    return {
        "status": "filled",
        "side": "sell",
        "symbol": symbol,
        "timeframe": "4h",
        "signal": signal,
        "timestamps": {"filled": _ts(h)},
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "pnl": pnl,
    }


class TestSellRotationReplay:
    def test_infer_sell_category_structure(self):
        assert infer_sell_category("SELL_PARTIAL_30", "bb_upper") == "structure"

    def test_infer_sell_category_trail(self):
        assert infer_sell_category("SELL_PARTIAL_30", "trailing_take_profit") == "trail"

    def test_is_partial_signal(self):
        assert is_partial_signal("SELL_PARTIAL_30")
        assert not is_partial_signal("SELL_FULL")

    def test_build_cycles_single_buy_partial_open(self):
        orders = [_buy(), _sell()]
        cycles = build_cycles(orders)
        assert len(cycles) == 1
        assert cycles[0].still_open
        assert len(cycles[0].sells) == 1
        assert cycles[0].sells[0].gain_pct > 0

    def test_build_cycles_full_close(self):
        orders = [
            _buy(),
            _sell(h=1, amount=300, usdt=330),
            _sell(h=2, amount=700, usdt=770, signal="SELL_FULL", pnl=70),
        ]
        cycles = build_cycles(orders)
        assert len(cycles) == 1
        assert cycles[0].close_ts is not None
        assert cycles[0].amount <= 1e-6

    def test_policy_b_blocks_early_partial(self):
        orders = [_buy(), _sell(price=1.05, pnl=5.0, signal="SELL_PARTIAL_30")]
        cycles = build_cycles(orders, hints={("TEST/USDT", datetime(2026, 7, 1, 12, 1, 0)): "bb_upper"})
        res = simulate_policy(cycles, POLICIES["B"], as_of=datetime(2026, 7, 5))
        assert res.blocked_sells >= 1
        assert res.executed_sells == 0

    def test_policy_c_converts_to_full_close(self):
        orders = [_buy(), _sell(price=1.15, pnl=50.0, amount=400, usdt=460)]
        cycles = build_cycles(orders)
        res_a = simulate_policy(cycles, POLICIES["A"], as_of=datetime(2026, 7, 5))
        res_c = simulate_policy(cycles, POLICIES["C"], as_of=datetime(2026, 7, 5))
        assert res_c.full_close_conversions >= 1
        assert res_c.cycles_closed >= res_a.cycles_closed

    def test_compare_policies_returns_all_variants(self):
        orders = [_buy(), _sell()]
        report = compare_policies(orders, decisions_path=None, max_open_slots=40)
        assert set(report["policies"].keys()) == {"A", "B", "C", "D", "D_prime"}
        assert report["baseline"].filled_orders == 2
        assert "recovery" in report
        assert "tail_slots" in report
        assert "validation" in report

    def test_forward_open_tail_exempt_and_idle_close(self):
        orders = [_buy(), _sell(h=1, amount=600, usdt=660, price=1.1)]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        as_of = datetime(2026, 7, 3, 12, 0, 0)
        fwd = forward_open_analysis(open_now, POLICIES["D"], as_of=as_of, max_open_slots=40)
        assert fwd.tail_exempt >= 1 or fwd.would_close_now >= 0

    def test_d_prime_blocks_loser_idle_close(self):
        orders = [
            _buy(price=1.0, amount=1000, usdt=1000),
            _sell(h=1, price=0.85, amount=600, usdt=510, pnl=-90, signal="SELL_STOP_LOSS"),
        ]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        as_of = datetime(2026, 7, 5, 12, 0, 0)
        fwd_d = forward_open_analysis(open_now, POLICIES["D"], as_of=as_of, max_open_slots=40)
        fwd_dp = forward_open_analysis(open_now, POLICIES["D_prime"], as_of=as_of, max_open_slots=40)
        assert fwd_d.would_close_now >= 1
        assert fwd_dp.would_close_now == 0
        assert fwd_dp.would_close_losers == 0

    def test_d_prime_closes_profitable_tail_on_idle(self):
        orders = [
            _buy(price=1.0, amount=1000, usdt=1000),
            _sell(h=1, price=1.2, amount=600, usdt=720, pnl=120),
        ]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        as_of = datetime(2026, 7, 5, 12, 0, 0)
        fwd_dp = forward_open_analysis(open_now, POLICIES["D_prime"], as_of=as_of, max_open_slots=40)
        assert fwd_dp.would_close_now >= 1
        assert fwd_dp.would_close_losers == 0

    def test_d_prime_replay_skips_loser_tail_auto_close(self):
        orders = [
            _buy(price=1.0, amount=1000, usdt=1000),
            _sell(h=1, price=0.85, amount=600, usdt=510, pnl=-90, signal="SELL_STOP_LOSS"),
        ]
        cycles = build_cycles(orders)
        as_of = datetime(2026, 7, 5, 12, 0, 0)
        res_d = simulate_policy(cycles, POLICIES["D"], as_of=as_of)
        res_dp = simulate_policy(cycles, POLICIES["D_prime"], as_of=as_of)
        assert res_d.tail_auto_closes >= 1
        assert res_dp.tail_auto_closes == 0
        assert res_dp.open_cycles >= 1

    def test_recovery_eligibility_counts_minus_tail(self):
        orders = [
            _buy(symbol="LOSS/USDT", price=1.0, amount=1000, usdt=1000),
            _sell(symbol="LOSS/USDT", h=1, price=0.90, amount=300, usdt=270, pnl=-30),
        ]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        rec = compute_recovery_eligibility(open_now)
        assert rec.minus_tails == 1
        assert rec.eligible == 1
        assert rec.blocked == 0

    def test_recovery_blocks_outside_loss_band(self):
        orders = [
            _buy(symbol="DEEP/USDT", price=1.0, amount=1000, usdt=1000),
            _sell(symbol="DEEP/USDT", h=1, price=0.50, amount=300, usdt=150, pnl=-150),
        ]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        rec = compute_recovery_eligibility(open_now)
        assert rec.minus_tails == 1
        assert rec.eligible == 0
        assert rec.blocked == 1

    def test_tail_slot_snapshot_exempts_large_sold_tails(self):
        orders = [
            _buy(price=1.0, amount=1000, usdt=1000),
            _sell(h=1, price=1.1, amount=600, usdt=660, pnl=60),
        ]
        cycles = build_cycles(orders)
        open_now = open_cycles_now(cycles)
        snap = compute_tail_slot_snapshot(open_now, POLICIES["D_prime"], max_open_slots=40)
        assert snap.open_total == 1
        assert snap.open_tail_exempt == 1
        assert snap.open_full_slots == 0
        assert snap.free_buy_slots == 40

    def test_validate_plan_gate1_pass_when_criteria_met(self):
        report = {
            "forward_open": {
                "A": type("F", (), {"free_slots": 0, "would_close_losers": 0})(),
                "D_prime": type("F", (), {
                    "free_slots": 10,
                    "would_close_now": 2,
                    "would_close_losers": 0,
                    "tail_exempt": 5,
                })(),
            },
            "recovery": type("R", (), {"eligible": 10, "minus_tails": 10})(),
            "open_policies": {
                "A": type("P", (), {"realized_pnl": 1000.0})(),
                "D_prime": type("P", (), {"realized_pnl": 980.0})(),
            },
        }
        result = validate_plan_gate1(report)
        assert result["go"] is True
        assert result["gates"]["no_loser_eviction"]["pass"] is True

    def test_validate_plan_gate1_fail_on_loser_eviction(self):
        report = {
            "forward_open": {
                "A": type("F", (), {"free_slots": 0, "would_close_losers": 0})(),
                "D_prime": type("F", (), {
                    "free_slots": 10,
                    "would_close_now": 3,
                    "would_close_losers": 2,
                    "tail_exempt": 5,
                })(),
            },
            "recovery": type("R", (), {"eligible": 10, "minus_tails": 10})(),
            "open_policies": {
                "A": type("P", (), {"realized_pnl": 1000.0})(),
                "D_prime": type("P", (), {"realized_pnl": 980.0})(),
            },
        }
        result = validate_plan_gate1(report)
        assert result["go"] is False
        assert result["gates"]["no_loser_eviction"]["pass"] is False