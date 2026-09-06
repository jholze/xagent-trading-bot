"""Fail-closed risk guards (#299 Tier 1a): log vs deny rollout."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.actions import BUY_DCA, HOLD, SELL_FULL
from core.config import BotConfig
from core.models import MarketContext, SignalAnalysis, TradeOrder
from data_manager import get_config
from risk.moderate_deploy import size_boost_for_regime
from risk.risk_manager import (
    RiskManager,
    _fail_closed_guards_mode,
    guard_failed,
)


def _risk_cfg(mode=None, **risk_over) -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["max_open_positions"] = 50
    raw["max_position_percent"] = 100
    raw["max_usdt_per_trade"] = 500
    risk = dict(raw.get("risk") or {})
    if mode is None:
        risk.pop("fail_closed_guards", None)
    else:
        risk["fail_closed_guards"] = mode
    risk["venue_quality"] = {"enabled": False}
    risk["cash_floor_pct"] = 0
    risk["cash_policy"] = {"enabled": False}
    risk["position_capacity"] = {"enabled": False}
    risk["slot_eviction"] = {"enabled": False}
    risk["min_trade_usdt"] = 1
    mem = dict(raw.get("memory") or {})
    gl = dict(mem.get("gross_loss") or {})
    gl.setdefault("size_bias_cap", 0.5)
    mem["gross_loss"] = gl
    raw["memory"] = mem
    risk.update(risk_over)
    raw["risk"] = risk
    return BotConfig(raw)


def _rm(mode=None, **risk_over) -> RiskManager:
    return RiskManager(_risk_cfg(mode, **risk_over))


def _buy_order() -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="NEAR/USDT",
        price=1.0,
        amount=0,
        usdt_amount=200.0,
        signal="BUY",
        source="grid",
        timestamp="2026-01-01T00:00:00",
    )


def _error_messages(mock_log) -> list[str]:
    out = []
    for args, kwargs in mock_log.call_args_list:
        level = kwargs.get("level")
        if level is None and len(args) >= 2:
            level = args[1]
        if str(level).upper() == "ERROR":
            out.append(str(args[0] if args else ""))
    return out


@contextmanager
def _buy_eval_env(rm: RiskManager, extra=()):
    cap = SimpleNamespace(
        max_open_eff=100,
        enabled=False,
        rationale="",
        factors={},
        free_slots=100,
        regime=None,
    )
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value={"amount": 0}))
        stack.enter_context(patch("risk.risk_manager.find_open_position_for_symbol", return_value=None))
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch.object(rm, "_dynamic_size", return_value=(200.0, {"total_multiplier": 1.0}))
        )
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
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
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch("services.venue_quality.venue_quality_config", return_value={"enabled": False})
        )
        for p in extra:
            stack.enter_context(p)
        yield


def _raise_patches(guard: str, rm: RiskManager):
    boom = RuntimeError("guard-boom")
    if guard == "sensor_reentry_cooloff":
        return [patch.object(rm, "_sensor_reentry_cooloff_blocked", side_effect=boom)]
    if guard == "venue_liquidity_block":
        return [
            patch("services.venue_quality.venue_quality_config", return_value={"enabled": True}),
            patch("services.venue_quality.source_applies_venue", return_value=True),
            patch("services.venue_quality.check_venue_for_buy", side_effect=boom),
        ]
    if guard == "universe_trade_cap":
        return [
            patch("services.universe.split.universe_split_enabled", return_value=True),
            patch("services.universe.split.is_trade_eligible", side_effect=boom),
        ]
    targets = {
        "stablecoin_blocked": "core.stablecoins.is_stablecoin_symbol",
        "correlated_tier_selloff": "services.correlated_tier.api.correlated_tier_selloff_active",
        "gainer_chase_guard": "services.gainer_universe.chase_guard.check_gainer_chase_guard",
        "market_block": "services.market_policy_fusion.get_global_market_bias",
        "coin_memory_soft_block": "intelligence.memory.cache.get_entry_bias",
        "watchlist_quality": "services.watchlist_quality.config.wqe_mode",
        "macro_calendar_block": "intelligence.macro.snapshot.get_risk_multipliers",
    }
    return [patch(targets[guard], side_effect=boom)]


A_RISK_GUARDS = (
    "stablecoin_blocked",
    "correlated_tier_selloff",
    "universe_trade_cap",
    "gainer_chase_guard",
    "market_block",
    "coin_memory_soft_block",
    "watchlist_quality",
    "sensor_reentry_cooloff",
    "venue_liquidity_block",
    "macro_calendar_block",
)


class TestFailClosedSwitch:
    def test_default_log_when_key_absent(self):
        assert _fail_closed_guards_mode(None) == "log"
        assert _fail_closed_guards_mode({}) == "log"
        assert _fail_closed_guards_mode({"risk": {}}) == "log"
        assert _fail_closed_guards_mode(_risk_cfg(None)) == "log"

    def test_guard_failed_log_returns_none_and_errors(self):
        cfg = _risk_cfg("log")
        with patch("logger.log") as mock_log:
            dec = guard_failed("stablecoin_blocked", RuntimeError("x"), _buy_order(), config=cfg)
        assert dec is None
        assert any("stablecoin_blocked" in m for m in _error_messages(mock_log))

    def test_guard_failed_deny_returns_decision(self):
        cfg = _risk_cfg("deny")
        with patch("logger.log") as mock_log:
            dec = guard_failed("stablecoin_blocked", RuntimeError("x"), _buy_order(), config=cfg)
        assert dec is not None
        assert dec.approved is False
        assert dec.code == "stablecoin_blocked_error"
        assert dec.size_multiplier == 0.0
        assert any("stablecoin_blocked" in m for m in _error_messages(mock_log))


@pytest.mark.parametrize("guard", A_RISK_GUARDS)
class TestASitesRiskEvaluate:
    def test_log_matches_prechange_and_errors(self, guard):
        rm_base = _rm("log")
        rm_log = _rm("log")
        order = _buy_order()
        with _buy_eval_env(rm_base):
            baseline = rm_base.evaluate(order, timeframe="4h", source="grid")
        with patch("logger.log") as mock_log, _buy_eval_env(
            rm_log, extra=_raise_patches(guard, rm_log)
        ):
            logged = rm_log.evaluate(order, timeframe="4h", source="grid")
        assert logged.approved == baseline.approved
        assert (logged.code or "") == (baseline.code or "")
        assert any(guard in m for m in _error_messages(mock_log))

    def test_deny_rejects_with_guard_error(self, guard):
        rm = _rm("deny")
        order = _buy_order()
        with patch("logger.log") as mock_log, _buy_eval_env(rm, extra=_raise_patches(guard, rm)):
            dec = rm.evaluate(order, timeframe="4h", source="grid")
        assert dec.approved is False
        assert dec.code == f"{guard}_error"
        assert dec.size_multiplier == 0.0
        assert any(guard in m for m in _error_messages(mock_log))


def _de_engine(mode: str):
    from strategies.decision_engine import DecisionEngine

    raw = {
        "risk": {"fail_closed_guards": mode, "position_locks": {"enabled": True}},
        "regime_detector": {"enabled": False},
        "strategy_allocator": {"enabled": False},
        "volatile_altcoin": {"mode": "live"},
        "sell_rotation": {"mode": "off"},
    }
    engine = DecisionEngine()
    engine.config = BotConfig(raw)
    return engine


def _de_market(*, has_position=True):
    return MarketContext(
        symbol="NEAR/USDT",
        timeframe="4h",
        current_price=1.0,
        rsi=50.0,
        lower_bb=0.9,
        has_position=has_position,
        average_entry=1.0,
        strategy_params={"strategy_profile": "grid", "volatility_tier": "normal"},
    )


def _de_technical():
    return SignalAnalysis(
        action="HOLD",
        symbol="NEAR/USDT",
        timeframe="4h",
        rsi=50.0,
        lower_bb=0.9,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical"],
    )


@contextmanager
def _de_common(engine, technical):
    strategy = MagicMock()
    strategy.analyze.return_value = technical
    with ExitStack() as stack:
        stack.enter_context(patch("strategies.decision_engine.get_strategy", return_value=strategy))
        stack.enter_context(
            patch("strategies.decision_engine.get_position", return_value={"amount": 1})
        )
        stack.enter_context(patch.object(engine, "_sync_watch_15m_state"))
        stack.enter_context(
            patch.object(
                engine,
                "_apply_shadow_mode",
                side_effect=lambda n, e, p, s=None: (n, e, ""),
            )
        )
        stack.enter_context(
            patch("strategies.decision_engine.policy_shadow_active", return_value=False)
        )
        yield


class TestASitesDecisionEngine:
    def test_position_lock_sell_log_lets_exit_through(self):
        engine = _de_engine("log")
        market = _de_market()
        technical = _de_technical()
        coin = {"symbol": "NEAR/USDT", "timeframe": "4h", "strategy_params": market.strategy_params}
        with _de_common(engine, technical), patch.object(
            engine,
            "_merge_sell",
            return_value=(SELL_FULL, ["technical"], 80.0, [], "technical", {}),
        ), patch(
            "strategies.position_lock.auto_sell_blocked",
            side_effect=RuntimeError("lock-boom"),
        ), patch("logger.log") as mock_log:
            analysis = engine.evaluate_with_market(coin, market)
        assert analysis.normalized_action == SELL_FULL
        assert "position_locked" not in analysis.sources
        assert any("position_lock" in m for m in _error_messages(mock_log))

    def test_position_lock_sell_deny_holds_exit(self):
        engine = _de_engine("deny")
        market = _de_market()
        technical = _de_technical()
        coin = {"symbol": "NEAR/USDT", "timeframe": "4h", "strategy_params": market.strategy_params}
        with _de_common(engine, technical), patch.object(
            engine,
            "_merge_sell",
            return_value=(SELL_FULL, ["technical"], 80.0, [], "technical", {}),
        ), patch(
            "strategies.position_lock.auto_sell_blocked",
            side_effect=RuntimeError("lock-boom"),
        ), patch("logger.log") as mock_log:
            analysis = engine.evaluate_with_market(coin, market)
        assert analysis.normalized_action == HOLD
        assert "position_locked" in analysis.sources
        assert any("position_lock" in m for m in _error_messages(mock_log))

    def test_position_lock_dca_log_lets_add_through(self):
        engine = _de_engine("log")
        market = _de_market()
        technical = _de_technical()
        coin = {"symbol": "NEAR/USDT", "timeframe": "4h", "strategy_params": market.strategy_params}
        dca = SimpleNamespace(
            shadow_only=False, source="dca", rationale="dip", usdt_amount=100.0
        )
        with _de_common(engine, technical), patch.object(
            engine,
            "_merge_sell",
            return_value=(HOLD, ["technical"], 0.0, [], "", {}),
        ), patch(
            "strategies.decision_engine.evaluate_dca_addon", return_value=dca
        ), patch(
            "services.dca_sniper.config.sniper_owns_cycle_dca", return_value=False
        ), patch(
            "strategies.dca_portfolio.should_defer_per_coin_dca", return_value=False
        ), patch(
            "strategies.position_lock.dca_blocked",
            side_effect=RuntimeError("dca-lock-boom"),
        ), patch("logger.log") as mock_log:
            analysis = engine.evaluate_with_market(coin, market)
        assert analysis.normalized_action == BUY_DCA
        assert "position_locked" not in analysis.sources
        assert any("position_lock_dca" in m for m in _error_messages(mock_log))

    def test_position_lock_dca_deny_holds_add(self):
        engine = _de_engine("deny")
        market = _de_market()
        technical = _de_technical()
        coin = {"symbol": "NEAR/USDT", "timeframe": "4h", "strategy_params": market.strategy_params}
        dca = SimpleNamespace(
            shadow_only=False, source="dca", rationale="dip", usdt_amount=100.0
        )
        with _de_common(engine, technical), patch.object(
            engine,
            "_merge_sell",
            return_value=(HOLD, ["technical"], 0.0, [], "", {}),
        ), patch(
            "strategies.decision_engine.evaluate_dca_addon", return_value=dca
        ), patch(
            "services.dca_sniper.config.sniper_owns_cycle_dca", return_value=False
        ), patch(
            "strategies.dca_portfolio.should_defer_per_coin_dca", return_value=False
        ), patch(
            "strategies.position_lock.dca_blocked",
            side_effect=RuntimeError("dca-lock-boom"),
        ), patch("logger.log") as mock_log:
            analysis = engine.evaluate_with_market(coin, market)
        assert analysis.normalized_action == HOLD
        assert "position_locked" in analysis.sources
        assert any("position_lock_dca" in m for m in _error_messages(mock_log))


class TestBSitesPermissiveDefaults:
    def test_unknown_regime_does_not_take_default_boost(self):
        cfg = {
            "risk": {
                "moderate_deploy": {
                    "enabled": True,
                    "size_boost_default": 1.35,
                    "size_boost_neutral": 2.0,
                    "max_boost": 3.0,
                }
            }
        }
        assert size_boost_for_regime(cfg, "UNKNOWN") == 1.0
        assert size_boost_for_regime(cfg, None) == pytest.approx(1.35)

    def test_dynamic_size_oracle_failure_log_keeps_default_boost(self):
        rm = _rm(
            "log",
            moderate_deploy={
                "enabled": True,
                "size_boost_default": 1.35,
                "size_boost_neutral": 1.5,
                "size_boost_risk_off": 1.0,
                "max_total_multiplier": 2.0,
                "max_boost": 1.75,
                "cash_rich_pct": 90,
            },
            min_size_multiplier=0.25,
        )
        order = _buy_order()
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            side_effect=RuntimeError("oracle-down"),
        ), patch("intelligence.memory.cache.get_size_bias", return_value=1.0), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ), patch.object(rm, "_equity_drawdown_pct", return_value=0.0), patch.object(
            rm, "_available_usdt", return_value=10_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch(
            "logger.log"
        ) as mock_log:
            sized, factors = rm._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        assert factors["moderate_deploy_mult"] == pytest.approx(1.35)
        assert factors["global_size_mult"] == pytest.approx(1.0)
        assert sized > 0
        assert any("global_market_bias" in m for m in _error_messages(mock_log))

    def test_dynamic_size_oracle_failure_deny_zeros_and_no_boost(self):
        rm = _rm(
            "deny",
            moderate_deploy={
                "enabled": True,
                "size_boost_default": 1.35,
                "size_boost_neutral": 1.5,
                "max_total_multiplier": 2.0,
                "max_boost": 1.75,
                "cash_rich_pct": 90,
            },
            min_size_multiplier=0.25,
        )
        order = _buy_order()
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            side_effect=RuntimeError("oracle-down"),
        ), patch("intelligence.memory.cache.get_size_bias", return_value=1.0), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ), patch.object(rm, "_equity_drawdown_pct", return_value=0.0), patch.object(
            rm, "_available_usdt", return_value=10_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch(
            "logger.log"
        ) as mock_log:
            sized, factors = rm._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        assert factors["global_size_mult"] == pytest.approx(0.0)
        assert factors["global_regime"] == "UNKNOWN"
        assert factors["moderate_deploy_mult"] == pytest.approx(1.0)
        assert sized == pytest.approx(0.0)
        assert any("global_market_bias" in m for m in _error_messages(mock_log))

    def test_market_bias_for_cash_log_stays_permissive(self):
        rm = _rm("log")
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            side_effect=RuntimeError("oracle-down"),
        ), patch("logger.log") as mock_log:
            bias = rm._market_bias_for_cash()
        assert bias["block_buys"] is False
        assert bias["size_mult"] == pytest.approx(1.0)
        assert any("market_bias_for_cash" in m for m in _error_messages(mock_log))

    def test_market_bias_for_cash_deny_blocks(self):
        rm = _rm("deny")
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            side_effect=RuntimeError("oracle-down"),
        ), patch("logger.log") as mock_log:
            bias = rm._market_bias_for_cash()
        assert bias["block_buys"] is True
        assert bias["size_mult"] == pytest.approx(0.0)
        assert any("market_bias_for_cash" in m for m in _error_messages(mock_log))

    def test_coin_bias_log_stays_one(self):
        rm = _rm("log", moderate_deploy={"enabled": False}, min_size_multiplier=0.25)
        order = _buy_order()
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"apply_size_mult": False, "active": False},
        ), patch(
            "intelligence.memory.cache.get_size_bias",
            side_effect=RuntimeError("memory-down"),
        ), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ), patch.object(rm, "_equity_drawdown_pct", return_value=0.0), patch.object(
            rm, "_available_usdt", return_value=10_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch(
            "logger.log"
        ) as mock_log:
            _sized, factors = rm._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        assert factors["coin_size_bias"] == pytest.approx(1.0)
        assert any("coin_memory_size_bias" in m for m in _error_messages(mock_log))

    def test_coin_bias_deny_uses_configured_min(self):
        rm = _rm("deny", moderate_deploy={"enabled": False}, min_size_multiplier=0.25)
        order = _buy_order()
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"apply_size_mult": False, "active": False},
        ), patch(
            "intelligence.memory.cache.get_size_bias",
            side_effect=RuntimeError("memory-down"),
        ), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ), patch.object(rm, "_equity_drawdown_pct", return_value=0.0), patch.object(
            rm, "_available_usdt", return_value=10_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch(
            "logger.log"
        ) as mock_log:
            _sized, factors = rm._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        assert factors["coin_size_bias"] == pytest.approx(0.5)
        assert any("coin_memory_size_bias" in m for m in _error_messages(mock_log))

    @pytest.mark.parametrize("mode", ("log", "deny"))
    def test_sell_fraction_error_never_full_sells(self, mode):
        rm = _rm(mode)
        order = TradeOrder(
            type="SELL",
            symbol="NEAR/USDT",
            price=1.0,
            amount=0,
            signal="SELL_PARTIAL_20",
            source="auto",
        )
        pos = {"amount": 100.0, "side": "long"}
        with patch(
            "risk.risk_manager.find_open_position_for_symbol",
            return_value=("4h", pos),
        ), patch("strategies.short_math.is_short", return_value=False), patch(
            "risk.risk_manager.sell_fraction_for_signal",
            side_effect=RuntimeError("fraction-boom"),
        ), patch("logger.log") as mock_log:
            out = rm._fill_sell_amount_from_open_lot(order, "4h")
        assert out.amount == 0
        assert out is order
        assert any("sell_fraction" in m for m in _error_messages(mock_log))

    def test_cooldown_unparsable_log_allows(self):
        rm = _rm("log")
        order = _buy_order()
        pos = {
            "amount": 1.0,
            "last_trade_at": "not-a-timestamp",
            "last_trade_type": "BUY",
        }
        with patch("risk.risk_manager.get_position", return_value=pos), patch(
            "logger.log"
        ) as mock_log:
            blocked, reason = rm._trade_cooldown_blocked(order, "4h", source="grid")
        assert blocked is False
        assert reason == ""
        assert any("trade_cooldown" in m for m in _error_messages(mock_log))

    def test_cooldown_unparsable_deny_blocks(self):
        rm = _rm("deny")
        order = _buy_order()
        pos = {
            "amount": 1.0,
            "last_trade_at": "not-a-timestamp",
            "last_trade_type": "BUY",
        }
        with patch("risk.risk_manager.get_position", return_value=pos), patch(
            "logger.log"
        ) as mock_log:
            blocked, reason = rm._trade_cooldown_blocked(order, "4h", source="grid")
        assert blocked is True
        assert reason == "cooldown_timestamp_unparsable"
        assert any("trade_cooldown" in m for m in _error_messages(mock_log))


class TestWqeNameError:
    def test_wqe_block_calls_log_buy_block(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_QUALITY_MODE", "enforce")
        rm = _rm("log")
        order = _buy_order()
        mock_log_block = MagicMock()
        extra = [
            patch("services.watchlist_quality.config.wqe_mode", return_value="enforce"),
            patch(
                "services.watchlist_quality.store.load_quality_scores",
                return_value={"coins": [{"symbol": "NEAR/USDT", "quality_score": 0.1}]},
            ),
            patch(
                "services.watchlist_quality.enforce.buy_allowed",
                return_value=(False, "min_buy_score"),
            ),
            patch("services.watchlist_quality.metrics.note_buy_blocked"),
            patch("services.watchlist_quality.event_log.log_buy_block", mock_log_block),
        ]
        with _buy_eval_env(rm, extra=extra):
            dec = rm.evaluate(order, timeframe="4h", source="grid")
        assert dec.approved is False
        assert dec.code == "watchlist_quality"
        assert mock_log_block.called
        kwargs = mock_log_block.call_args.kwargs
        assert kwargs.get("mode") == "enforce"
