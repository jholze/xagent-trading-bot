"""#662 small-ticket readiness, Spec v1.5.

Overlays only. config.json keeps today's values. No coin names in logic;
symbols here are fixture labels.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.costs import CostModel
from core.models import TradeOrder
from execution.gate_adapter import (
    set_venue_limits_override,
    venue_limits_for,
)
from risk.cash_policy import compute_dual_spendable
from risk.risk_manager import RiskManager
from risk.slot_eviction_runtime import resolve_spendable_ok_for_entry
from services.gainer_universe.relvol_shadow import size_usdt_for_signal
from services.portfolio_service import PortfolioService
from strategies.dca import _partial_stop_ratio_from_config
from strategies.positions import (
    BELOW_VENUE_MINIMUM,
    VENUE_MIN_MERGE_LOSS_EXCEEDED,
    brake_open_pnl,
    clear_positions_memory,
    count_open_full_slots,
    count_open_positions,
    count_open_tail_slots,
    get_position,
    is_below_venue_minimum,
    is_open_position,
    mark_exchange_min_reject,
    refresh_venue_min_mark,
    update_position,
)

TF = "1h"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_book():
    clear_positions_memory()
    yield
    clear_positions_memory()


def _raw(*, risk=None, **over) -> dict:
    block = {
        "min_trade_usdt": 15,
        "cash_floor_pct": 0,
        "max_daily_loss_pct": 0,
        "max_daily_sells": 0,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "slot_eviction": {"enabled": True, "mode": "off"},
        "stop_gap_buffer_pct": 6.21,
        "venue_min_hysteresis": 0.25,
        "venue_min_max_pct_of_ticket": 0.20,
        "venue_step_max_pct_of_ticket": 0.02,
        "longs": {
            "volatile": {"market_cap_min_usd": 0},
            "stable": {"market_cap_min_usd": 0},
        },
    }
    if risk:
        block.update(risk)
    raw = {
        "trading_mode": "paper",
        "max_usdt_per_trade": 25,
        "max_position_percent": 100,
        "max_open_positions": 36,
        "stop_loss_pct": 20.0,
        "initial_capital_usdt": 1000,
        "paper": {"initial_capital_usdt": 1000},
        "risk": block,
        "sell_policy": {
            "rotation": {
                "tail_exempt_notional_usdt": 500,
                "tail_exempt_sold_pct": 0.25,
            }
        },
        "costs": {
            "fee_source": "config",
            "gate": {
                "spot": {
                    "fee_taker_pct": 0.0,
                    "fee_maker_pct": 0.0,
                    "slippage_pct": 0.0,
                    "fee_side_buy": "base",
                    "fee_side_sell": "quote",
                }
            },
        },
        "strategies": [],
    }
    raw.update(over)
    return raw


def _cfg(*, risk=None, **over) -> BotConfig:
    return BotConfig(_raw(risk=risk, **over))


def _buy(symbol="SYM/USDT", price=15.0, usdt=None, signal="BUY", source="manual"):
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source,
    )


def _sell(symbol, price, amount, signal="SELL_PARTIAL"):
    return TradeOrder(
        type="SELL",
        symbol=symbol,
        price=price,
        amount=amount,
        signal=signal,
        source="auto",
    )


def _limits(**kw):
    base = {
        "known": True,
        "min_cost": 3.0,
        "amount_step": 1e-8,
        "price_places": 4,
    }
    base.update(kw)
    set_venue_limits_override(base)


def _plant(symbol, entry, amount):
    update_position(symbol, TF, "BUY", entry, amount)
    return get_position(symbol, TF)


def _below(symbol, entry, amount, mark):
    _plant(symbol, entry, amount)
    assert refresh_venue_min_mark(symbol, TF, mark=mark, mark_fresh=True) == "set"
    pos = get_position(symbol, TF)
    assert is_below_venue_minimum(pos)
    return pos


@contextmanager
def _quiet(rm, *, real_capacity=False, open_slots=0, max_eff=100, dynamic=None, universe=False):
    cap = SimpleNamespace(
        max_open_eff=max_eff,
        enabled=False,
        rationale="",
        factors={},
        free_slots=max(0, max_eff - open_slots),
        regime=None,
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_loss_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_sells_count", return_value=0))
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_for_sizing", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_buy_lock_decision", return_value=None))
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(
            patch("strategies.position_lock.auto_sell_blocked", return_value=(False, ""))
        )
        stack.enter_context(
            patch(
                "strategies.position_lock.attach_lock_from_ledger",
                side_effect=lambda pos, *a, **k: pos,
            )
        )
        stack.enter_context(
            patch(
                "services.correlated_tier.api.correlated_tier_selloff_active",
                return_value=False,
            )
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
                return_value={"block_buys": False, "apply_size_mult": False, "active": False, "size_mult": 1.0},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        if universe:
            stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=True))
            stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=False))
        else:
            stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
            stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        if not real_capacity:
            stack.enter_context(
                patch("risk.risk_manager.count_open_full_slots", return_value=open_slots)
            )
            stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
        if dynamic is not None:
            stack.enter_context(patch.object(rm, "_dynamic_size", side_effect=dynamic))
        yield


@contextmanager
def _logs():
    lines: list[str] = []

    def _capture(message, level="INFO"):
        lines.append(str(message))

    with ExitStack() as stack:
        stack.enter_context(patch("logger.log", side_effect=_capture))
        stack.enter_context(patch("execution.gate_adapter.log", side_effect=_capture))
        yield lines


def _mult(factor):
    def _inner(base, *args, **kwargs):
        return float(base) * factor, {
            "total_multiplier": factor,
            "drawdown_pct": 0.0,
            "atr_factor": 1.0,
            "trust_factor": 1.0,
        }

    return _inner


def test_t1_min_trade_passes_and_buffer_zero_frees_tickets():
    _limits(min_cost=1e-6, amount_step=1e-8)
    rm = RiskManager(_cfg())
    with _quiet(rm):
        ok = rm.evaluate(_buy(usdt=15, price=10), TF, source="manual")
        small = rm.evaluate(_buy(usdt=14, price=10), TF, source="manual")
    assert ok.approved is True
    assert ok.order.usdt_amount == pytest.approx(15)
    assert small.approved is False
    assert small.code == "size_too_small"

    floor = 1000 * 0.12
    freed, _, _ = compute_dual_spendable(
        cash_total=1000,
        floor_abs=floor,
        equity=1000,
        dca_buffer_usdt=0,
        dca_buffer_pct_equity=0,
    )
    blocked, _, _ = compute_dual_spendable(
        cash_total=1000,
        floor_abs=floor,
        equity=1000,
        dca_buffer_usdt=800,
        dca_buffer_pct_equity=1.5,
    )
    assert freed / 25 > 10
    assert blocked / 25 < 4


def test_t2_relvol_ticket_and_explicit_zero():
    sized = size_usdt_for_signal(
        qvol_1h=1000,
        abs_vol_24h=0,
        cfg={"participation": 0.02, "min_ticket_usdt": 15, "mode": "trade", "max_pct_of_vol_24h": 1},
        max_usdt_per_trade=25,
    )
    assert sized > 0
    explicit_zero = size_usdt_for_signal(
        qvol_1h=100,
        abs_vol_24h=0,
        cfg={"participation": 0.02, "min_ticket_usdt": 0, "mode": "trade", "max_pct_of_vol_24h": 1},
        max_usdt_per_trade=25,
    )
    assert explicit_zero == pytest.approx(2.0)
    missing = size_usdt_for_signal(
        qvol_1h=100,
        abs_vol_24h=0,
        cfg={"participation": 0.02, "mode": "trade", "max_pct_of_vol_24h": 1},
        max_usdt_per_trade=25,
    )
    assert missing == 0.0

    _limits(min_cost=1e-6, amount_step=1e-8)
    rm = RiskManager(_cfg(risk={"relvol_min_trade_usdt": 0, "min_trade_usdt": 1}))
    with _quiet(rm):
        dec = rm.evaluate(
            _buy(price=10, usdt=10, source="gainer_relvol"),
            TF,
            source="gainer_relvol",
        )
    assert dec.approved is True


def test_t3_absent_keys_match_today_and_overlay_is_used():
    absent = RiskManager(_cfg(risk={
        "dust_sweep_max_position_usdt": None,
        "dust_sweep_min_remainder_usdt": None,
        "relvol_min_trade_usdt": None,
    }))
    # None is present, so _cfg_number keeps it only when the key is absent.
    raw = _raw()
    raw["risk"].pop("relvol_min_trade_usdt", None)
    raw["risk"].pop("dust_sweep_max_position_usdt", None)
    raw["risk"].pop("dust_sweep_min_remainder_usdt", None)
    absent = RiskManager(BotConfig(raw))
    limits = absent._partial_sell_limits("SYM/USDT", TF)
    assert limits["dust_sweep_max_position_usdt"] == pytest.approx(15)
    assert limits["dust_sweep_min_remainder_usdt"] == pytest.approx(10)
    assert absent._cfg_number("relvol_min_trade_usdt", 1000.0) == pytest.approx(1000)

    overlay = RiskManager(_cfg(risk={
        "dust_sweep_max_position_usdt": 500,
        "dust_sweep_min_remainder_usdt": 100,
        "relvol_min_trade_usdt": 25,
    }))
    used = overlay._partial_sell_limits("SYM/USDT", TF)
    assert used["dust_sweep_max_position_usdt"] == pytest.approx(500)
    assert used["dust_sweep_min_remainder_usdt"] == pytest.approx(100)

    _limits(min_cost=1e-6, amount_step=1e-8)
    _plant("SWEEP/USDT", 9, 40)  # value 400 at price 10, in profit
    with _quiet(overlay):
        swept = overlay.evaluate(_sell("SWEEP/USDT", 10, 1), TF, source="auto")
    assert swept.approved is True
    assert swept.order.signal == "SELL_FULL"

    tight = RiskManager(_cfg(risk={
        "dust_sweep_max_position_usdt": 6,
        "dust_sweep_min_remainder_usdt": 6,
        "min_sell_notional_usdt": 1,
        "min_position_usdt_for_partial_sell": 1,
        "block_partial_sell_if_sold_percent_above": 0.99,
    }))
    clear_positions_memory()
    _plant("SWEEP/USDT", 9, 40)
    with _quiet(tight):
        held = tight.evaluate(_sell("SWEEP/USDT", 10, 1), TF, source="auto")
    assert held.order is None or held.order.signal != "SELL_FULL"

    _limits(min_cost=1e-6, amount_step=1e-8)
    floor = RiskManager(_cfg(risk={"relvol_min_trade_usdt": 1000, "min_trade_usdt": 1}))
    with _quiet(floor):
        blocked = floor.evaluate(
            _buy(price=10, usdt=30, source="gainer_relvol"),
            TF,
            source="gainer_relvol",
        )
    assert blocked.code == "size_too_small"
    low = RiskManager(_cfg(risk={"relvol_min_trade_usdt": 20, "min_trade_usdt": 1}))
    with _quiet(low):
        passed = low.evaluate(
            _buy(price=10, usdt=30, source="gainer_relvol"),
            TF,
            source="gainer_relvol",
        )
    assert passed.approved is True


def test_t3_partial_stop_ratio_and_slot_min_explicit_zero(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_bot_config",
        lambda: BotConfig({"trading_mode": "paper", "risk": {"partial_stop_ratio": 0.4}}),
    )
    assert _partial_stop_ratio_from_config() == pytest.approx(0.4)
    monkeypatch.setattr(
        "core.config.get_bot_config",
        lambda: BotConfig({"trading_mode": "paper", "risk": {}}),
    )
    assert _partial_stop_ratio_from_config() == pytest.approx(0.67)

    class _Rm:
        def _portfolio_equity(self, price, sym):
            return 0.0

        def _spendable_usdt(self, eq, is_dca=False):
            return 0.0

    order = SimpleNamespace(usdt_amount=0, price=1, symbol="SYM/USDT")
    assert resolve_spendable_ok_for_entry(
        order=order,
        risk_manager=_Rm(),
        risk_config={"min_trade_usdt": 0},
    ) is True


def test_t4_partial_below_min_skipped_and_missing_data_blocks_buy_not_stop():
    _limits(min_cost=3, amount_step=1e-8)
    rm = RiskManager(_cfg(risk={
        "dust_sweep_max_position_usdt": 1,
        "dust_sweep_min_remainder_usdt": 0,
        "min_sell_notional_usdt": 1,
        "min_position_usdt_for_partial_sell": 1,
    }))
    _plant("PART/USDT", 10, 2.5)  # mark value 25 at price 10
    with _logs() as lines, _quiet(rm):
        skipped = rm.evaluate(_sell("PART/USDT", 10, 0.2), TF, source="auto")
    assert skipped.approved is False
    assert skipped.code == "venue_min_partial_skipped"
    assert any("partial skipped" in line for line in lines)

    set_venue_limits_override(None)
    from execution import gate_adapter

    gate_adapter._VENUE_CACHE.pop("PART/USDT", None)
    with _quiet(rm):
        missing_buy = rm.evaluate(_buy("PART/USDT", price=10, usdt=20), TF, source="manual")
        stop = rm.evaluate(
            _sell("PART/USDT", 10, 2.5, signal="SELL_STOP_FULL"),
            TF,
            source="auto",
        )
    assert missing_buy.approved is False
    assert missing_buy.code == "venue_min_missing"
    assert stop.approved is True
    assert stop.order.signal == "SELL_STOP_FULL"


def test_t5_remainder_below_min_sells_whole_lot_and_stop_executes():
    _limits(min_cost=3, amount_step=1e-8)
    rm = RiskManager(_cfg(risk={
        "dust_sweep_max_position_usdt": 1,
        "dust_sweep_min_remainder_usdt": 0,
        "min_sell_notional_usdt": 1,
        "min_position_usdt_for_partial_sell": 1,
    }))
    # Underwater so dust-sweep rotation does not steal the venue rule.
    _plant("LOT/USDT", 20, 2.5)  # mark 10 → value 25
    with _quiet(rm):
        whole = rm.evaluate(_sell("LOT/USDT", 10, 2.3), TF, source="auto")
        stop = rm.evaluate(
            _sell("LOT/USDT", 10, 2.5, signal="SELL_STOP_FULL"),
            TF,
            source="auto",
        )
    assert whole.approved is True
    assert whole.order.signal == "SELL_FULL"
    assert whole.order.amount == pytest.approx(2.5)
    assert stop.approved is True
    assert stop.order.signal == "SELL_STOP_FULL"


def test_t6_pair_rule_blocks_wide_step_and_high_minimum():
    rm = RiskManager(_cfg())
    _limits(min_cost=3, amount_step=0.1, price_places=4)
    with _logs() as wide_logs, _quiet(rm):
        wide = rm.evaluate(_buy("STEP/USDT", price=10, usdt=25), TF, source="manual")
        wide_again = rm.evaluate(_buy("STEP/USDT", price=10, usdt=25), TF, source="manual")
    assert wide.code == "venue_pair_blocked"
    assert wide_again.code == "venue_pair_blocked"
    assert sum("venue_pair_blocked" in line for line in wide_logs) == 1

    _limits(min_cost=6, amount_step=1e-8)
    with _quiet(rm):
        high = rm.evaluate(_buy("MINP/USDT", price=10, usdt=25), TF, source="manual")
    assert high.code == "venue_pair_blocked"

    _limits(min_cost=3, amount_step=1e-8)
    with _quiet(rm):
        normal = rm.evaluate(_buy("NORM/USDT", price=10, usdt=25), TF, source="manual")
    assert normal.approved is True


def test_t7_state_hysteresis_one_log_and_no_sell():
    _limits(min_cost=3, amount_step=1e-8)
    rm = RiskManager(_cfg())
    _plant("HYST/USDT", 2.0, 1.0)
    with _logs() as lines:
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=2.9, mark_fresh=True) == "set"
        for _ in range(3):
            assert refresh_venue_min_mark("HYST/USDT", TF, mark=2.0, mark_fresh=True) == "unchanged"
        with _quiet(rm):
            for _ in range(3):
                sell = rm.evaluate(_sell("HYST/USDT", 2.0, 1.0, signal="SELL_PARTIAL"), TF, source="auto")
                assert sell.approved is False
                assert sell.code == BELOW_VENUE_MINIMUM
    assert sum(line.startswith(BELOW_VENUE_MINIMUM) or f" {BELOW_VENUE_MINIMUM} " in f" {line} " or line.split()[0] == BELOW_VENUE_MINIMUM for line in lines) == 1
    assert is_below_venue_minimum(get_position("HYST/USDT", TF))
    assert is_open_position(get_position("HYST/USDT", TF)) is False
    assert count_open_positions() == 0
    assert count_open_full_slots(rm.config.raw) == 0

    clear_positions_memory()
    _plant("HYST/USDT", 4.0, 1.0)
    with _logs() as transitions:
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=2.9, mark_fresh=True) == "set"
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=3.5, mark_fresh=True) == "unchanged"
        assert is_below_venue_minimum(get_position("HYST/USDT", TF))
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=3.75, mark_fresh=True) == "lifted"
        assert is_below_venue_minimum(get_position("HYST/USDT", TF)) is False
        assert is_open_position(get_position("HYST/USDT", TF))
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=3.5, mark_fresh=True) == "unchanged"
        assert is_below_venue_minimum(get_position("HYST/USDT", TF)) is False
        assert refresh_venue_min_mark("HYST/USDT", TF, mark=2.9, mark_fresh=True) == "set"
    set_lines = [ln for ln in transitions if BELOW_VENUE_MINIMUM in ln and "lifted" not in ln]
    lift_lines = [ln for ln in transitions if "venue_min_lifted" in ln]
    assert len(set_lines) == 2
    assert len(lift_lines) == 1

    assert refresh_venue_min_mark("HYST/USDT", TF, mark=10, mark_fresh=False) == "unchanged"
    assert is_below_venue_minimum(get_position("HYST/USDT", TF))
    assert refresh_venue_min_mark("HYST/USDT", TF, mark=None, mark_fresh=True) == "unchanged"
    assert is_below_venue_minimum(get_position("HYST/USDT", TF))


def test_t7_k1_real_capacity_eviction_off_no_force_sell():
    _limits(min_cost=3, amount_step=1e-8)
    rm = RiskManager(_cfg(
        max_open_positions=2,
        risk={
            "position_capacity": {
                "enabled": True,
                "base": 2,
                "min_floor": 2,
                "max_ceiling": 2,
                "link_fusion_size_mult": False,
            },
            "slot_eviction": {"enabled": True, "mode": "off"},
        },
    ))
    _plant("SLOT1/USDT", 1000, 1)
    _plant("SLOT2/USDT", 1000, 1)
    _plant("BELOW/USDT", 2.0, 1.0)
    assert refresh_venue_min_mark("BELOW/USDT", TF, mark=2.0, mark_fresh=True) == "set"
    before = count_open_positions()
    full_before = count_open_full_slots(rm.config.raw)
    cap = rm._resolve_position_capacity(full_slots=full_before)
    assert cap.max_open_eff == full_before
    assert refresh_venue_min_mark("BELOW/USDT", TF, mark=3.75, mark_fresh=True) == "lifted"
    assert count_open_positions() == before + 1
    assert count_open_full_slots(rm.config.raw) == full_before
    assert float(get_position("SLOT1/USDT", TF)["amount"]) == pytest.approx(1)
    assert float(get_position("SLOT2/USDT", TF)["amount"]) == pytest.approx(1)
    assert float(get_position("BELOW/USDT", TF)["amount"]) == pytest.approx(1)
    with _quiet(rm, real_capacity=True):
        refused = rm.evaluate(_buy("NEW/USDT", price=10, usdt=25), TF, source="manual")
    assert refused.approved is False
    assert refused.code == "max_open_positions"
    assert float(get_position("SLOT1/USDT", TF)["amount"]) == pytest.approx(1)
    assert float(get_position("BELOW/USDT", TF)["amount"]) == pytest.approx(1)


def test_t7_exchange_reject_sets_state_once_and_dust_threshold():
    _limits(min_cost=3, amount_step=1e-8)
    _plant("REJ/USDT", 4.0, 1.0)
    with _logs() as lines:
        from execution.gate_adapter import GateExecutionAdapter

        adapter = GateExecutionAdapter(config=_cfg(), mode="shadow")
        order = _sell("REJ/USDT", 4.0, 1.0, signal="SELL_STOP_FULL")
        adapter._reject_sell_below_venue_min(order, TF, "Order value below Gate minimum ($3.00)")
        adapter._reject_sell_below_venue_min(order, TF, "Order value below Gate minimum ($3.00)")
    assert is_below_venue_minimum(get_position("REJ/USDT", TF))
    assert get_position("REJ/USDT", TF)["venue_min_open_pnl"] == pytest.approx(-4.0)
    assert sum(BELOW_VENUE_MINIMUM in line for line in lines) == 1
    assert mark_exchange_min_reject("REJ/USDT", TF) is False

    clear_positions_memory()
    _limits(min_cost=1e-6, amount_step=1e-8)
    _plant("DUST/USDT", 1.0, 0.5)
    _plant("OPEN/USDT", 2.0, 1.0)
    assert is_open_position(get_position("DUST/USDT", TF)) is False
    assert is_open_position(get_position("OPEN/USDT", TF)) is True


def test_t7_buy_on_below_min_lot_is_a_new_entry():
    _limits(min_cost=3, amount_step=1e-8)
    rm = RiskManager(_cfg())
    _below("SYM/USDT", 2.0, 1.0, 2.0)
    with _quiet(rm, universe=True):
        dec = rm.evaluate(_buy("SYM/USDT", price=2.0, usdt=25), TF, source="manual")
    assert dec.approved is False
    assert dec.code == "universe_trade_cap"


def test_t8_config_blocks_stay_at_today_and_buffer_is_copied():
    disk = json.loads((ROOT / "config.json").read_text())
    risk = disk["risk"]
    dry = disk["dry_run_defaults"]
    shared = (
        "min_trade_usdt",
        "min_sell_notional_usdt",
        "min_position_usdt_for_partial_sell",
        "block_partial_sell_if_sold_percent_above",
        "dust_sweep_max_position_usdt",
        "dust_sweep_min_remainder_usdt",
        "relvol_min_trade_usdt",
        "partial_stop_ratio",
        "venue_min_hysteresis",
        "venue_min_max_pct_of_ticket",
        "venue_step_max_pct_of_ticket",
        "stop_gap_buffer_pct",
    )
    for key in shared:
        assert risk[key] == dry[key]
    assert risk["stop_gap_buffer_pct"] == pytest.approx(6.21)
    assert disk["max_usdt_per_trade"] == 4500
    assert disk["live"]["max_usdt_per_trade"] == 4500
    assert disk["paper"]["initial_capital_usdt"] == disk["live"]["simulated_balance_usdt"]
    assert disk["initial_capital_usdt"] == disk["paper"]["initial_capital_usdt"]
    rotation = disk["sell_policy"]["rotation"]
    assert rotation["tail_exempt_notional_usdt"] == 500
    assert rotation["tail_exempt_sold_pct"] == pytest.approx(0.25)
    blob = json.dumps(disk)
    assert "gap_buffer_pct" not in blob.replace("stop_gap_buffer_pct", "")
    assert "count_all_open_as_slots" not in blob
    assert dry["max_daily_buys"] != risk["max_daily_buys"]
    assert dry["max_daily_sells"] != risk["max_daily_sells"]


def _merge_buy(rm, symbol, price, *, source="manual", dynamic=None, usdt=None):
    order = _buy(symbol, price=price, usdt=usdt, source=source)
    with _quiet(rm, dynamic=dynamic):
        return rm.evaluate(order, TF, source=source, indicators={})


def test_t9_merge_keeps_loss_once_and_lifts():
    _limits()
    rm = RiskManager(_cfg())
    _below("SYM/USDT", 20, 0.1, 15)
    assert brake_open_pnl() == pytest.approx(-0.5)
    dec = _merge_buy(rm, "SYM/USDT", 15)
    assert dec.approved is True
    assert dec.order.usdt_amount == pytest.approx(23)
    PortfolioService(rm.config).execute_buy(
        "SYM/USDT", TF, 15, usdt_amount=dec.order.usdt_amount, sync_virtual_ledger=False
    )
    pos = get_position("SYM/USDT", TF)
    amount = float(pos["amount"])
    assert amount == pytest.approx(1.633333333, rel=1e-6)
    assert float(pos["average_entry"]) == pytest.approx(15.306122, rel=1e-5)
    assert amount * float(pos["average_entry"]) == pytest.approx(25)
    assert brake_open_pnl() == pytest.approx(-0.5)
    assert is_below_venue_minimum(pos) is False
    assert is_open_position(pos)
    assert count_open_positions() == 1
    assert count_open_tail_slots(rm.config.raw) == 1
    assert count_open_full_slots(rm.config.raw) == 0
    sold = PortfolioService(rm.config).execute_sell(
        "SYM/USDT",
        TF,
        15,
        "SELL_FULL",
        amount=amount,
        sync_virtual_ledger=False,
    )
    assert sold.pnl == pytest.approx(-0.5)
    assert brake_open_pnl() == pytest.approx(0)
    assert sold.pnl + brake_open_pnl() == pytest.approx(-0.5)


def test_t9_full_slots_and_universe_refuse_before_merge():
    _limits()
    rm = RiskManager(_cfg())
    _below("SYM/USDT", 20, 0.1, 15)
    with _quiet(rm, open_slots=36, max_eff=36):
        full = rm.evaluate(_buy("SYM/USDT", price=15), TF, source="manual")
    assert full.code == "max_open_positions"
    with _quiet(rm, universe=True):
        outside = rm.evaluate(_buy("SYM/USDT", price=15), TF, source="manual")
    assert outside.code == "universe_trade_cap"
    assert float(get_position("SYM/USDT", TF)["amount"]) == pytest.approx(0.1)


def test_t9b_and_t9c_loss_above_bound_logs_once():
    _limits()
    rm = RiskManager(_cfg())
    _below("WIDE/USDT", 25, 1, 2.5)
    assert brake_open_pnl() == pytest.approx(-22.5)
    with _logs() as lines, _quiet(rm):
        first = rm.evaluate(_buy("WIDE/USDT", price=2.5), TF, source="manual")
        second = rm.evaluate(_buy("WIDE/USDT", price=2.5), TF, source="manual")
        third = rm.evaluate(_buy("WIDE/USDT", price=2.5), TF, source="manual")
    assert first.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert second.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert third.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert first.order is None
    assert float(get_position("WIDE/USDT", TF)["amount"]) == pytest.approx(1)
    assert brake_open_pnl() == pytest.approx(-22.5)
    assert sum(VENUE_MIN_MERGE_LOSS_EXCEEDED in line for line in lines) == 1

    clear_positions_memory()
    _below("MID/USDT", 8, 1, 2.5)
    dec = _merge_buy(rm, "MID/USDT", 2.5)
    assert dec.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert float(get_position("MID/USDT", TF)["amount"]) == pytest.approx(1)
    assert brake_open_pnl() == pytest.approx(-5.5)


def test_t9d_boundary_merges_on_equal_loss_and_rejects_a_cent_over():
    _limits()
    rm = RiskManager(_cfg())
    _below("EDGE/USDT", 5.4475, 1, 2.0)
    dec = _merge_buy(rm, "EDGE/USDT", 2.0)
    assert dec.approved is True
    assert dec.order.usdt_amount == pytest.approx(19.5525)
    PortfolioService(rm.config).execute_buy(
        "EDGE/USDT", TF, 2.0, usdt_amount=dec.order.usdt_amount, sync_virtual_ledger=False
    )
    pos = get_position("EDGE/USDT", TF)
    cost = float(pos["amount"]) * float(pos["average_entry"])
    assert cost == pytest.approx(25)
    assert brake_open_pnl() == pytest.approx(-3.4475)
    assert abs(brake_open_pnl()) / cost * 100 == pytest.approx(13.79, abs=0.01)
    assert abs(brake_open_pnl()) / cost * 100 < 20

    clear_positions_memory()
    _below("EDGE/USDT", 5.4475, 1, 1.99)
    rejected = _merge_buy(rm, "EDGE/USDT", 1.99)
    assert rejected.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert float(get_position("EDGE/USDT", TF)["amount"]) == pytest.approx(1)


def test_t9d2_per_symbol_stop_12_5():
    _limits()
    rm = RiskManager(_cfg(
        stop_loss_pct=20.0,
        strategies=[{
            "symbol": "SYM/USDT",
            "timeframe": TF,
            "stop_loss_pct": 12.5,
        }],
    ))
    _below("SYM/USDT", 3.57, 1, 2.0)  # loss 1.57
    merged = _merge_buy(rm, "SYM/USDT", 2.0)
    assert merged.approved is True
    assert merged.order.usdt_amount == pytest.approx(21.43)

    clear_positions_memory()
    _below("SYM/USDT", 3.58, 1, 2.0)  # loss 1.58 > bound 1.5725
    rejected = _merge_buy(rm, "SYM/USDT", 2.0)
    assert rejected.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert rejected.details["stop_pct"] == pytest.approx(12.5)
    assert rejected.details["bound"] == pytest.approx(1.5725, abs=1e-4)


def test_t9d3_size_mult_0_8_rejects_on_the_bound_code():
    _limits()
    rm = RiskManager(_cfg())
    _below("SYM/USDT", 5.4475, 1, 2.0)
    dec = _merge_buy(rm, "SYM/USDT", 2.0, source="auto", dynamic=_mult(0.8))
    assert dec.approved is False
    assert dec.code == "venue_min_merge_loss_exceeded"
    assert dec.details["reason"] == "loss_bound"
    assert dec.details["final_buy"] == pytest.approx(15.642, abs=1e-4)
    assert dec.details["basis"] == pytest.approx(21.0895, abs=1e-4)
    assert dec.details["bound"] == pytest.approx(2.9082, abs=1e-4)
    assert dec.details["loss"] == pytest.approx(3.4475, abs=1e-6)
    assert float(get_position("SYM/USDT", TF)["amount"]) == pytest.approx(1)


def test_t9d3b_size_mult_0_5_is_size_too_small():
    _limits()
    rm = RiskManager(_cfg())
    _below("SYM/USDT", 5.4475, 1, 2.0)
    dec = _merge_buy(rm, "SYM/USDT", 2.0, source="auto", dynamic=_mult(0.5))
    assert dec.approved is False
    assert dec.code == "size_too_small"
    assert dec.code != VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert float(get_position("SYM/USDT", TF)["amount"]) == pytest.approx(1)


def test_t9d4_stop_at_or_below_buffer_never_merges_and_missing_key_fails_closed():
    _limits()
    equal = RiskManager(_cfg(stop_loss_pct=6.21))
    _below("SYM/USDT", 2.0, 1, 2.0)
    dec = _merge_buy(equal, "SYM/USDT", 2.0)
    assert dec.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert dec.details["reason"] == "stop_not_above_buffer"

    clear_positions_memory()
    under = RiskManager(_cfg(stop_loss_pct=6.0))
    _below("SYM/USDT", 2.0, 1, 2.5)  # negative loss still refused
    dec_under = _merge_buy(under, "SYM/USDT", 2.5)
    assert dec_under.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert dec_under.details["reason"] == "stop_not_above_buffer"

    clear_positions_memory()
    raw = _raw()
    raw["risk"].pop("stop_gap_buffer_pct")
    missing = RiskManager(BotConfig(raw))
    _below("SYM/USDT", 5.4475, 1, 2.0)
    refused = _merge_buy(missing, "SYM/USDT", 2.0)
    assert refused.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert refused.details["reason"] == "missing_key"


def test_t9e_fee_stays_inside_the_ticket_and_is_counted_once():
    _limits()
    raw = _raw()
    raw["costs"]["gate"]["spot"].update({
        "fee_taker_pct": 0.2,
        "slippage_pct": 0.0,
        "fee_side_buy": "base",
        "fee_side_sell": "quote",
    })
    rm = RiskManager(BotConfig(raw))
    _below("SYM/USDT", 20, 0.1, 15)
    before = brake_open_pnl()
    assert before == pytest.approx(-0.5)
    dec = _merge_buy(rm, "SYM/USDT", 15)
    assert dec.approved is True
    cm = CostModel.from_config(rm.config, symbol="SYM/USDT")
    buy = cm.simulate_buy(15, usdt=dec.order.usdt_amount)
    PortfolioService(rm.config).execute_buy(
        "SYM/USDT", TF, 15, usdt_amount=dec.order.usdt_amount, sync_virtual_ledger=False
    )
    pos = get_position("SYM/USDT", TF)
    merged_cost = float(pos["amount"]) * float(pos["average_entry"])
    assert merged_cost == pytest.approx(25)
    assert buy.quote_net == pytest.approx(23)
    assert brake_open_pnl() == pytest.approx(before - buy.fee_usdt)
    amount = float(pos["amount"])
    sold = PortfolioService(rm.config).execute_sell(
        "SYM/USDT", TF, 15, "SELL_FULL", amount=amount, sync_virtual_ledger=False
    )
    sell = cm.simulate_sell(15, amount)
    open_before_sell = before - buy.fee_usdt
    assert sold.pnl == pytest.approx(open_before_sell - sell.fee_usdt)
    assert brake_open_pnl() == pytest.approx(0)
