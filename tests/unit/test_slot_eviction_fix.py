"""#300 Slot eviction fixes — same-cycle approve, real prices, min fraction, ledger limits."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.models import TradeOrder, TradeResult
from risk.slot_eviction import (
    ACTION_FULL,
    ACTION_REDUCE_TAIL,
    EXIT_SOURCE_SLOT_EVICT,
    VictimCandidate,
    fraction_to_free_full_slot,
    plan_slot_eviction,
    score_entry_demand,
)
from risk.slot_eviction_runtime import (
    _hours_since,
    check_rate_limits,
    reset_rate_limits_for_tests,
    try_slot_eviction_on_max_open,
)


def _slot_cfg(**over) -> dict:
    base = {
        "enabled": True,
        "mode": "live",
        "min_entry_score": 4,
        "min_entry_score_shadow": 3,
        "min_victim_score": 0.15,
        "min_hold_hours": 3,
        "protect_peak_gain_pct": 12,
        "max_evict_notional_usdt": 8000,
        "prefer_reduce_to_tail": True,
        "allow_loss_full_evict": False,
        "require_sensor_source": True,
        "skip_if_warmup": True,
        "skip_if_block_buys": True,
        "skip_if_crash": True,
        "max_evictions_per_hour": 2,
        "max_evictions_per_day": 8,
        "symbol_cooldown_hours": 24,
        "memory": {
            "min_entry_keep_edge": 0.12,
            "prefer_keep_floor": 0.7,
            "prefer_is_hard_keep": True,
            "missing_profile_keep": 0.5,
            "min_samples_for_win_rate": 3,
        },
        "rag": {"mode": "off", "apply_to_plan": False},
        "weights": {"memory": 0.55, "idle": 0.2, "pnl_flat": 0.1, "tail_ready": 0.15},
        "sources": ["entry_sensor_15m", "vol_spike_15m"],
    }
    base.update(over)
    return {
        "slot_eviction": base,
        "min_trade_usdt": 5,
        "position_capacity": {"enabled": False, "restart_warmup_min": 0},
    }


def _cand(
    symbol: str,
    *,
    keep_profile: float,
    keep_rag: float | None = None,
    gain: float = 3.0,
    trail: bool = False,
    prefer: bool = False,
    age: float = 48.0,
    peak: float = 3.0,
    idle: float = 40.0,
    notional: float = 1000.0,
    sold: float = 0.0,
    rot: bool = True,
    price: float = 10.0,
    amount: float = 10.0,
) -> VictimCandidate:
    kr = keep_rag if keep_rag is not None else keep_profile
    return VictimCandidate(
        symbol=symbol,
        timeframe="4h",
        gain_pct=gain,
        peak_gain_pct=peak,
        idle_hours=idle,
        sold_percent=sold,
        notional_usdt=notional,
        amount=amount,
        price=price,
        keep_profile=keep_profile,
        keep_rag=kr,
        keep_final=kr,
        trail_armed=trail,
        rotation_eligible=rot,
        prefer=prefer,
        age_hours=age,
        class_name="A" if gain >= 0 else "B",
    )


def _demand(symbol: str = "BANK/USDT"):
    return score_entry_demand(
        symbol=symbol,
        source="entry_sensor_15m",
        free_full_slots=0,
        spike_multiple=5.0,
        risk_config=_slot_cfg(),
    )


def _weak_prof():
    return SimpleNamespace(
        entry_bias="neutral",
        size_bias=0.7,
        win_rate=0.2,
        sells_30d=10,
        risk_score=0.8,
        total_pnl_usdt=-200.0,
        features={},
    )


def _strong_prof():
    return SimpleNamespace(
        entry_bias="prefer",
        size_bias=1.1,
        win_rate=0.7,
        sells_30d=10,
        risk_score=0.3,
        total_pnl_usdt=200.0,
        features={},
    )


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class TestFractionMinAmount(unittest.TestCase):
    """Ticket table: assert the *minimum* sell amount (not the old max oversell)."""

    # Production rotation: tail_exempt_sold_pct=0.25, tail_exempt_notional_usdt=500
    TAIL_SOLD = 0.25
    TAIL_NOTIONAL = 500.0

    def _amount(self, notional: float, sold: float) -> float:
        frac, _action, already = fraction_to_free_full_slot(
            sold_percent=sold,
            notional_usdt=notional,
            tail_sold_pct=self.TAIL_SOLD,
            tail_notional_usdt=self.TAIL_NOTIONAL,
        )
        self.assertFalse(already)
        return notional * frac

    def test_2000_unsold_sells_500(self):
        self.assertAlmostEqual(self._amount(2000.0, 0.0), 500.0, places=0)

    def test_3000_20pct_sells_187(self):
        self.assertAlmostEqual(self._amount(3000.0, 0.20), 187.0, places=0)

    def test_5000_unsold_sells_1250(self):
        self.assertAlmostEqual(self._amount(5000.0, 0.0), 1250.0, places=0)


class TestHoursSinceFailClosed(unittest.TestCase):
    def test_garbage_returns_none(self):
        self.assertIsNone(_hours_since("not-a-timestamp"))
        self.assertIsNone(_hours_since(""))
        self.assertIsNone(_hours_since(None))

    def test_valid_iso_is_non_negative(self):
        h = _hours_since(_iso_hours_ago(5))
        self.assertIsNotNone(h)
        self.assertGreater(h, 4.0)
        self.assertLess(h, 6.0)

    def test_garbage_timestamp_excludes_candidate(self):
        from risk.slot_eviction_runtime import build_victim_candidates

        pos = {
            "symbol": "AAA/USDT",
            "timeframe": "4h",
            "amount": 10.0,
            "average_entry": 10.0,
            "sold_percent": 0.0,
            "last_trade_at": "garbage",
            "first_buy_at": _iso_hours_ago(48),
        }
        with patch(
            "strategies.positions.list_active_positions", return_value=[pos]
        ), patch(
            "strategies.sell_rotation_policy.is_tail_position", return_value=False
        ):
            cands = build_victim_candidates(
                config_raw={},
                risk_config=_slot_cfg(),
                entry_symbol="BANK/USDT",
                get_profile=lambda s: None,
                prices={"AAA/USDT": 11.0},
            )
        symbols = [c.symbol for c in cands if c.symbol != "BANK/USDT"]
        self.assertNotIn("AAA/USDT", symbols)


class TestLockPrefilterFailClosed(unittest.TestCase):
    def _plan(self, cands, **cfg):
        entry = _cand("BANK/USDT", keep_profile=0.7)
        entry = VictimCandidate(**{**entry.to_dict(), "veto": "entry_self"})
        return plan_slot_eviction(
            demand=_demand(),
            candidates=cands + [entry],
            risk_config=_slot_cfg(**cfg),
        )

    def test_ledger_lock_never_in_victim_pool(self):
        locked = _cand("LOCK/USDT", keep_profile=0.1, gain=4.0)
        other = _cand("BBB/USDT", keep_profile=0.3, gain=4.0)

        def _attach(pos, symbol, timeframe="4h", **_k):
            pos = dict(pos or {})
            if symbol == "LOCK/USDT":
                pos["lock"] = {"enabled": True, "reason": "ops", "modes": ["no_evict"]}
            return pos

        with patch(
            "strategies.positions.get_position",
            side_effect=lambda sym, tf: {"amount": 10.0, "symbol": sym},
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=_attach,
        ):
            plan = self._plan([locked, other])
        self.assertTrue(plan.ok, plan.veto_reason)
        self.assertEqual(plan.victim_symbol, "BBB/USDT")
        pool = [c["symbol"] for c in plan.candidates if not c.get("veto")]
        self.assertNotIn("LOCK/USDT", pool)

    def test_prefilter_exception_excludes(self):
        boom = _cand("BOOM/USDT", keep_profile=0.1, gain=4.0)
        other = _cand("BBB/USDT", keep_profile=0.3, gain=4.0)

        def _blocked(pos, **_k):
            if (pos or {}).get("symbol") == "BOOM/USDT":
                raise RuntimeError("lock check exploded")
            return False, ""

        with patch(
            "strategies.positions.get_position",
            side_effect=lambda sym, tf: {"amount": 10.0, "symbol": sym},
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=lambda pos, symbol, timeframe="4h", **k: pos,
        ), patch(
            "strategies.position_lock.eviction_blocked",
            side_effect=_blocked,
        ):
            plan = self._plan([boom, other])
        self.assertTrue(plan.ok, plan.veto_reason)
        self.assertEqual(plan.victim_symbol, "BBB/USDT")
        pool = [c["symbol"] for c in plan.candidates if not c.get("veto")]
        self.assertNotIn("BOOM/USDT", pool)


class TestAllowLossFullEvict(unittest.TestCase):
    def test_underwater_never_sell_full_when_disabled(self):
        underwater = _cand(
            "LOSS/USDT",
            keep_profile=0.2,
            gain=-8.0,
            rot=False,
            notional=70.0,
            sold=0.0,
            price=9.0,
        )
        entry = VictimCandidate(
            **{**_cand("BANK/USDT", keep_profile=0.75).to_dict(), "veto": "entry_self"}
        )
        raw = {
            "sell_policy": {
                "rotation": {
                    "tail_exempt_sold_pct": 0.55,
                    "tail_exempt_notional_usdt": 50.0,
                }
            }
        }
        with patch(
            "strategies.positions.get_position", return_value={"amount": 0}
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=lambda pos, *a, **k: pos,
        ), patch(
            "strategies.position_lock.eviction_blocked", return_value=(False, "")
        ):
            plan = plan_slot_eviction(
                demand=_demand(),
                candidates=[underwater, entry],
                risk_config=_slot_cfg(allow_loss_full_evict=False),
                config_raw=raw,
            )
        self.assertTrue(plan.ok, plan.veto_reason)
        self.assertEqual(plan.victim_symbol, "LOSS/USDT")
        self.assertNotEqual(plan.action, ACTION_FULL)
        self.assertEqual(plan.action, ACTION_REDUCE_TAIL)
        self.assertLess(plan.sell_fraction, 0.99)


class TestFusionMemoryFailClosed(unittest.TestCase):
    def _order(self):
        return TradeOrder(
            type="BUY",
            symbol="BANK/USDT",
            price=1.0,
            amount=0,
            usdt_amount=200,
            signal="BUY",
            entry_15m_vol_ratio=5.5,
        )

    def test_fusion_lookup_raises_no_eviction(self):
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            side_effect=RuntimeError("fusion down"),
        ), patch(
            "risk.slot_eviction_runtime.execute_eviction_sell"
        ) as sell:
            plan, suffix = try_slot_eviction_on_max_open(
                order=self._order(),
                source="entry_sensor_15m",
                free_full_slots=0,
                config=None,
                risk_config=_slot_cfg(),
                spendable_ok=True,
            )
        self.assertIsNone(plan)
        self.assertEqual(suffix, "")
        sell.assert_not_called()

    def test_memory_lookup_raises_no_eviction(self):
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "regime": "NEUTRAL"},
        ), patch(
            "intelligence.memory.cache.get_entry_bias",
            side_effect=RuntimeError("memory down"),
        ), patch(
            "risk.slot_eviction_runtime.execute_eviction_sell"
        ) as sell:
            plan, suffix = try_slot_eviction_on_max_open(
                order=self._order(),
                source="entry_sensor_15m",
                free_full_slots=0,
                config=None,
                risk_config=_slot_cfg(),
                spendable_ok=True,
            )
        self.assertIsNone(plan)
        self.assertEqual(suffix, "")
        sell.assert_not_called()

    def test_profile_lookup_raises_during_candidates_no_eviction(self):
        def _gp(sym, config=None):
            if "BANK" in (sym or ""):
                return _strong_prof()
            raise RuntimeError("gp down")

        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "regime": "NEUTRAL"},
        ), patch(
            "intelligence.memory.cache.get_entry_bias", return_value="prefer"
        ), patch(
            "intelligence.memory.cache.get_coin_profile", side_effect=_gp
        ), patch(
            "strategies.positions.list_active_positions",
            return_value=[_victim_pos()],
        ), patch(
            "strategies.sell_rotation_policy.is_tail_position", return_value=False
        ), patch(
            "price_fetcher.get_prices_batch", return_value={"VICTIM/USDT": 10.5}
        ), patch(
            "risk.slot_eviction_runtime.check_rate_limits", return_value=(False, "")
        ), patch(
            "risk.slot_eviction_runtime.execute_eviction_sell"
        ) as sell:
            plan, suffix = try_slot_eviction_on_max_open(
                order=self._order(),
                source="entry_sensor_15m",
                free_full_slots=0,
                config=None,
                risk_config=_slot_cfg(),
                spendable_ok=True,
            )
        self.assertIsNone(plan)
        self.assertEqual(suffix, "")
        sell.assert_not_called()


class TestLedgerRateLimits(unittest.TestCase):
    def test_ninth_eviction_denied_after_restart(self):
        from services.order_service import OrderService

        reset_rate_limits_for_tests()
        now = datetime.now().isoformat()
        svc = OrderService()
        orders = []
        for i in range(8):
            orders.append(
                {
                    "id": f"evict{i}",
                    "status": "filled",
                    "side": "sell",
                    "symbol": f"S{i}/USDT",
                    "exit_source": EXIT_SOURCE_SLOT_EVICT,
                    "ledger_scope": svc.scope,
                    "timestamps": {
                        "created": now,
                        "filled": now,
                        "updated": now,
                    },
                }
            )
        svc._save({"ledger_scope": svc.scope, "orders": orders})
        # Simulated restart: RAM counters gone, ledger still has 8 today.
        reset_rate_limits_for_tests()
        blocked, reason = check_rate_limits(
            _slot_cfg(max_evictions_per_hour=20, max_evictions_per_day=8)
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "max_evictions_per_day")


def _risk_manager_for_e2e(*, mode: str = "live"):
    from core.config import BotConfig
    from data_manager import get_config
    from risk.risk_manager import RiskManager

    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["max_open_positions"] = 36
    raw["max_position_percent"] = 100
    raw["max_usdt_per_trade"] = 500
    risk = raw.setdefault("risk", {})
    risk["cash_policy"] = {"enabled": False}
    risk["position_capacity"] = {"enabled": False, "restart_warmup_min": 0}
    risk["venue_quality"] = {"enabled": False}
    risk["min_trade_usdt"] = 5
    risk["slot_eviction"] = {
        "enabled": True,
        "mode": mode,
        "min_entry_score": 4,
        "min_victim_score": 0.15,
        "min_hold_hours": 3,
        "protect_peak_gain_pct": 40,
        "max_evict_notional_usdt": 8000,
        "prefer_reduce_to_tail": True,
        "allow_loss_full_evict": False,
        "require_sensor_source": True,
        "require_spendable_for_entry": True,
        "skip_if_warmup": True,
        "skip_if_block_buys": True,
        "skip_if_crash": True,
        "max_evictions_per_hour": 20,
        "max_evictions_per_day": 20,
        "sources": ["entry_sensor_15m"],
        "rag": {"mode": "off", "apply_to_plan": False},
        "memory": {
            "min_entry_keep_edge": 0.12,
            "prefer_is_hard_keep": True,
            "missing_profile_keep": 0.5,
        },
    }
    raw.setdefault("sell_policy", {}).setdefault("rotation", {})
    raw["sell_policy"]["rotation"]["tail_exempt_sold_pct"] = 0.25
    raw["sell_policy"]["rotation"]["tail_exempt_notional_usdt"] = 500
    rm = RiskManager(BotConfig(raw))
    return rm


def _victim_pos():
    return {
        "symbol": "VICTIM/USDT",
        "timeframe": "4h",
        "amount": 100.0,
        "average_entry": 10.0,
        "sold_percent": 0.0,
        "first_buy_at": _iso_hours_ago(48),
        "entry_at": _iso_hours_ago(48),
        "last_trade_at": _iso_hours_ago(24),
        "updated_at": _iso_hours_ago(24),
        "recent_high": 10.4,
    }


class TestSameCycleEvaluate(unittest.TestCase):
    def _order(self):
        return TradeOrder(
            type="BUY",
            symbol="BANK/USDT",
            price=1.0,
            amount=0,
            usdt_amount=200,
            signal="BUY",
            source="entry_sensor_15m",
            entry_15m_vol_ratio=5.5,
        )

    def _profile(self, sym, config=None):
        if "VICTIM" in (sym or ""):
            return _weak_prof()
        return _strong_prof()

    def test_evaluate_approves_after_victim_sold_at_real_price(self):
        from services.order_service import OrderService

        rm = _risk_manager_for_e2e(mode="live")
        order = self._order()
        victim = _victim_pos()
        freed = {"yes": False}
        captured = []

        def _count(*_a, **_k):
            return 35 if freed["yes"] else 36

        def _get_pos(symbol, timeframe="4h"):
            if symbol == "VICTIM/USDT":
                return dict(victim)
            return {"amount": 0}

        def _execute(trade_order, *a, **k):
            captured.append(trade_order)
            freed["yes"] = True
            svc = OrderService()
            rec = svc.create_from_request(trade_order, status="executing", timeframe="4h")
            result = TradeResult(
                executed=True,
                order_type="SELL",
                symbol=trade_order.symbol,
                amount=trade_order.amount,
                price=trade_order.price,
                usdt_amount=trade_order.usdt_amount,
                message="ok",
            )
            svc.link_execution_result(rec["id"], result, approved_order=trade_order)
            return result

        mock_svc = MagicMock()
        mock_svc.execute_order.side_effect = _execute
        mock_svc.market = None

        patches = [
            patch("risk.risk_manager.count_open_full_slots", side_effect=_count),
            patch("risk.risk_manager.get_position", side_effect=_get_pos),
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None),
            patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")),
            patch.object(rm, "_cash_floor_blocked", return_value=None),
            patch.object(rm, "_daily_buy_limit_blocked", return_value=None),
            patch.object(rm, "_portfolio_equity", return_value=100_000.0),
            patch.object(rm, "_spendable_usdt", return_value=50_000.0),
            patch.object(rm, "_available_usdt", return_value=50_000.0),
            patch.object(rm, "_equity_drawdown_pct", return_value=0.0),
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "size_mult": 1.0, "regime": "RISK_ON"},
            ),
            patch("intelligence.memory.cache.get_entry_bias", return_value="prefer"),
            patch("intelligence.memory.cache.get_coin_profile", side_effect=self._profile),
            patch("strategies.positions.list_active_positions", return_value=[victim]),
            patch("strategies.positions.get_position", side_effect=_get_pos),
            patch("strategies.sell_rotation_policy.is_tail_position", return_value=False),
            patch("price_fetcher.get_prices_batch", return_value={"VICTIM/USDT": 10.50}),
            patch("services.trading_service.TradingService", return_value=mock_svc),
            patch("services.market_oracle_store.process_uptime_sec", return_value=10_000.0),
            patch("risk.slot_eviction_runtime.check_rate_limits", return_value=(False, "")),
            patch("strategies.position_lock.eviction_blocked", return_value=(False, "")),
            patch(
                "strategies.position_lock.attach_lock_from_ledger",
                side_effect=lambda pos, *a, **k: pos,
            ),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            decision = rm.evaluate(order, "4h", source="entry_sensor_15m")

        self.assertTrue(decision.approved, f"{decision.code}: {decision.message}")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].type, "SELL")
        self.assertEqual(captured[0].exit_source, EXIT_SOURCE_SLOT_EVICT)
        self.assertAlmostEqual(float(captured[0].price), 10.50, places=2)
        from services.order_service import OrderService, ORDERS_LIST_HARD_CAP

        filled, _ = OrderService().list_orders(
            trade_book_only=True, per_page=ORDERS_LIST_HARD_CAP
        )
        evicts = [o for o in filled if o.get("exit_source") == EXIT_SOURCE_SLOT_EVICT]
        self.assertTrue(evicts, "ledger missing slot_evict_for_entry sell")

    def test_no_price_no_sell_distinct_code(self):
        rm = _risk_manager_for_e2e(mode="live")
        order = self._order()
        victim = _victim_pos()
        mock_svc = MagicMock()
        mock_svc.execute_order.side_effect = AssertionError("must not sell")
        mock_svc.market = None

        patches = [
            patch("risk.risk_manager.count_open_full_slots", return_value=36),
            patch("risk.risk_manager.get_position", return_value={"amount": 0}),
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None),
            patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")),
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "size_mult": 1.0, "regime": "RISK_ON"},
            ),
            patch("intelligence.memory.cache.get_entry_bias", return_value="prefer"),
            patch("intelligence.memory.cache.get_coin_profile", side_effect=self._profile),
            patch("strategies.positions.list_active_positions", return_value=[victim]),
            patch("strategies.positions.get_position", return_value=dict(victim)),
            patch("strategies.sell_rotation_policy.is_tail_position", return_value=False),
            patch("price_fetcher.get_prices_batch", return_value={}),
            patch("services.trading_service.TradingService", return_value=mock_svc),
            patch("services.market_oracle_store.process_uptime_sec", return_value=10_000.0),
            patch("risk.slot_eviction_runtime.check_rate_limits", return_value=(False, "")),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            decision = rm.evaluate(order, "4h", source="entry_sensor_15m")

        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "slot_eviction_no_price")
        mock_svc.execute_order.assert_not_called()
