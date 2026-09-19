import copy
import unittest
from unittest.mock import MagicMock, patch

from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.registry import resolve_effective_timeframe, resolve_strategy_params
from strategies.technical_rsi_bb import TechnicalRSIStrategy


_HERMES_H_PARAMS = {
    "rsi_buy_low": 28,
    "rsi_buy_high": 48,
    "volume_multiplier": 1.3,
    "rsi_sell_30": 70,
    "rsi_sell_20": 85,
    "stop_loss_pct": 50.0,
    "strategy_profile": "hermes_baseline",
}


class TestVolatileProfile(unittest.TestCase):
    def test_effective_timeframe_volatile_meme_without_position(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        with patch(
            "strategies.registry.get_bot_config",
            return_value=MagicMock(
                volatile_altcoin_config={"enabled": True, "timeframe": "1h"}
            ),
        ), patch("strategies.registry._has_open_position", return_value=False):
            self.assertEqual(resolve_effective_timeframe(coin), "1h")

    def test_effective_timeframe_large_cap_stays_4h(self):
        coin = {"symbol": "BTC/USDT", "timeframe": "4h"}
        with patch(
            "strategies.registry.get_bot_config",
            return_value=MagicMock(
                volatile_altcoin_config={"enabled": True, "timeframe": "1h"}
            ),
        ):
            self.assertEqual(resolve_effective_timeframe(coin), "4h")

    def test_effective_timeframe_keeps_legacy_position_tf(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h"}
        with patch(
            "strategies.registry.get_bot_config",
            return_value=MagicMock(
                volatile_altcoin_config={"enabled": True, "timeframe": "1h"}
            ),
        ), patch("strategies.registry._has_open_position", side_effect=lambda s, tf: tf == "4h"):
            self.assertEqual(resolve_effective_timeframe(coin), "4h")

    def test_resolve_volatile_profile_for_h_position(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        with patch("strategies.registry._hermes_memory_params", return_value=dict(_HERMES_H_PARAMS)):
            params = resolve_strategy_params(coin, has_position=True, atr_pct=49.0, frozen_tier="volatile")
        self.assertEqual(params.get("strategy_profile"), "hermes_baseline+volatile")
        self.assertEqual(params.get("rsi_sell_30"), 62)
        self.assertEqual(params.get("rsi_sell_mode"), "level")
        self.assertEqual(params.get("stop_loss_pct"), 50.0)
        self.assertIsNone(params.get("take_profit_pct"))
        self.assertEqual(params.get("exit_ladder", {}).get("tiers"), [0.35, 0.35, 0.3])

    def test_hermes_memory_without_position(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h"}
        with patch("strategies.registry._hermes_memory_params", return_value=dict(_HERMES_H_PARAMS)):
            params = resolve_strategy_params(coin, has_position=False, atr_pct=49.0)
        self.assertEqual(params.get("strategy_profile"), "hermes_baseline")
        self.assertEqual(params.get("rsi_sell_30"), 70)
        self.assertEqual(params.get("volume_multiplier"), 1.3)
        self.assertEqual(params.get("buy_regime"), "both")
        self.assertEqual(params.get("volatility_tier"), "volatile")

    def test_stable_tier_sell_overlay_with_position(self):
        coin = {"symbol": "BTC/USDT", "timeframe": "4h"}
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry._explicit_strategy_entry", return_value=None):
            params = resolve_strategy_params(coin, has_position=True, atr_pct=1.5)
        self.assertEqual(params.get("volatility_tier"), "stable")
        self.assertEqual(params.get("strategy_profile"), "stable_altcoin")
        self.assertEqual(params.get("exit_ladder", {}).get("tiers"), [0.3, 0.3, 0.4])
        self.assertEqual(params.get("take_profit_tiers"), [60, 100, 150])

    def test_stable_tier_buy_overlay_without_position(self):
        coin = {"symbol": "BTC/USDT", "timeframe": "4h"}
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry._explicit_strategy_entry", return_value=None):
            params = resolve_strategy_params(coin, has_position=False, atr_pct=1.5)
        self.assertEqual(params.get("volatility_tier"), "stable")
        self.assertEqual(params.get("volume_multiplier"), 1.05)
        self.assertEqual(params.get("buy_regime"), "dip")
        self.assertEqual(params.get("rsi_buy_high"), 50)

    def test_mid_cap_buy_overlay_without_position(self):
        coin = {"symbol": "TRX/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry._explicit_strategy_entry", return_value=None):
            params = resolve_strategy_params(coin, has_position=False, atr_pct=4.0)
        self.assertEqual(params.get("volatility_tier"), "stable")
        self.assertEqual(params.get("volume_multiplier"), 0.95)
        self.assertEqual(params.get("buy_regime"), "both")

    def test_pure_volatile_when_no_hermes_memory(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        with patch("strategies.registry._hermes_memory_params", return_value=None):
            params = resolve_strategy_params(coin, has_position=True, atr_pct=49.0, frozen_tier="volatile")
        self.assertEqual(params.get("strategy_profile"), "volatile_altcoin")
        self.assertEqual(params.get("rsi_sell_30"), 62)

    def test_explicit_strategy_keeps_rsi_but_gets_exit_ladder(self):
        coin = {"symbol": "ETH/USDT", "timeframe": "4h"}
        with patch("strategies.registry._explicit_strategy_entry", return_value={"symbol": "ETH/USDT", "rsi_sell_30": 70}):
            params = resolve_strategy_params(coin, has_position=True, atr_pct=1.7, frozen_tier="volatile")
        self.assertEqual(params.get("rsi_sell_30"), 70)
        self.assertEqual(params.get("exit_ladder", {}).get("tiers"), [0.35, 0.35, 0.3])
        self.assertNotIn("take_profit_pct", params)

    def test_rsi_level_sell_with_min_gain(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "rsi_sell_mode": "level",
            "rsi_sell_30": 67,
            "rsi_sell_20": 75,
            "rsi_sell_min_gain_pct": 15,
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.58,
            rsi=68.0,
            lower_bb=0.5,
            has_position=True,
            average_entry=0.28,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 60},
        )
        coin = {"symbol": "H/USDT", "timeframe": "4h", "strategy_params": params}
        with patch("strategies.indicator_regime.overlay_active", return_value=False):
            result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "SELL_PARTIAL_30")

    def test_rsi_level_blocked_by_low_gain(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "rsi_sell_mode": "level",
            "rsi_sell_30": 67,
            "rsi_sell_min_gain_pct": 15,
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol="WLD/USDT",
            timeframe="4h",
            current_price=0.60,
            rsi=74.0,
            lower_bb=0.55,
            has_position=True,
            average_entry=0.595,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 60},
        )
        coin = {"symbol": "WLD/USDT", "timeframe": "4h", "strategy_params": params}
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "HOLD")

    def test_shadow_mode_blocks_hermes_volatile_overlay(self):
        engine = DecisionEngine(market_service=MagicMock())
        with patch.object(
            type(engine.config),
            "volatile_altcoin_config",
            property(lambda self: {"mode": "shadow"}),
        ):
            normalized, action, shadow = engine._apply_shadow_mode(
                "SELL_PARTIAL_30",
                "SELL_30",
                {"strategy_profile": "hermes_baseline+volatile"},
            )
        self.assertEqual(normalized, "HOLD")
        self.assertEqual(action, "HOLD")
        self.assertEqual(shadow, "SELL_30")

    def test_shadow_mode_blocks_execution(self):
        engine = DecisionEngine(market_service=MagicMock())
        technical = SignalAnalysis(
            action="HOLD",
            symbol="H/USDT",
            timeframe="4h",
            rsi=68.0,
            lower_bb=0.5,
            vol_multiplier=0.7,
            ampel_emoji="🟡",
            ampel_text="Neutral",
            sources=["technical"],
            normalized_action="HOLD",
        )
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.636,
            rsi=68.0,
            lower_bb=0.5,
            upper_bb=0.627,
            has_position=True,
            average_entry=0.28,
            strategy_params={"strategy_profile": "volatile_altcoin"},
        )
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.636}
        with patch.object(
            type(engine.config),
            "volatile_altcoin_config",
            property(lambda self: {"mode": "shadow"}),
        ):
            normalized, action, shadow = engine._apply_shadow_mode("SELL_PARTIAL_30", "SELL_30", market.strategy_params)
        self.assertEqual(normalized, "HOLD")
        self.assertEqual(action, "HOLD")
        self.assertEqual(shadow, "SELL_30")

    def test_identity_row_same_as_no_row_volatile(self):
        """Acceptance 1: identity-only resolve matches no-row `_pure_volatile_profile`."""
        from core.config import BotConfig
        from data_manager import get_config
        from strategies.registry import is_identity_strategy_entry

        coin = {"symbol": "SLOT/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        identity = {
            "symbol": "SLOT/USDT",
            "timeframe": "4h",
            "strategy_class": "technical_rsi_bb",
            "description": "identity slot",
            "auto_identity": True,
        }
        self.assertTrue(is_identity_strategy_entry(identity))
        raw = copy.deepcopy(get_config())
        curated = [e for e in raw.get("strategies", []) if e.get("symbol") != "SLOT/USDT"]
        cfg_none = BotConfig(raw={**raw, "strategies": curated})
        cfg_id = BotConfig(raw={**raw, "strategies": curated + [identity]})
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry.get_bot_config", return_value=cfg_none):
            no_row = resolve_strategy_params(
                coin, has_position=True, atr_pct=49.0, frozen_tier="volatile"
            )
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry.get_bot_config", return_value=cfg_id):
            ident = resolve_strategy_params(
                coin, has_position=True, atr_pct=49.0, frozen_tier="volatile"
            )
        self.assertEqual(ident.get("strategy_profile"), no_row.get("strategy_profile"))
        self.assertEqual(ident.get("rsi_sell_30"), no_row.get("rsi_sell_30"))
        self.assertEqual(ident.get("strategy_profile"), "volatile_altcoin")
        self.assertEqual(ident.get("rsi_sell_30"), 62)

    def test_stale_identity_marker_with_preserve_key_stays_explicit(self):
        """Acceptance 2: preserve-key row stays explicit even if auto_identity is stale."""
        from core.config import BotConfig
        from data_manager import get_config
        from strategies.registry import _explicit_strategy_entry, is_identity_strategy_entry

        coin = {"symbol": "ETH/USDT", "timeframe": "4h"}
        stale = {
            "symbol": "ETH/USDT",
            "timeframe": "4h",
            "rsi_sell_30": 70,
            "auto_identity": True,
        }
        self.assertFalse(is_identity_strategy_entry(stale))
        raw = copy.deepcopy(get_config())
        cfg = BotConfig(raw={**raw, "strategies": [stale]})
        with patch("strategies.registry.get_bot_config", return_value=cfg):
            entry = _explicit_strategy_entry("ETH/USDT", "4h")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.get("rsi_sell_30"), 70)
        with patch("strategies.registry._hermes_memory_params", return_value=None), \
             patch("strategies.registry.get_bot_config", return_value=cfg):
            params = resolve_strategy_params(
                coin, has_position=True, atr_pct=1.7, frozen_tier="volatile"
            )
        self.assertEqual(params.get("rsi_sell_30"), 70)
        self.assertEqual(params.get("exit_ladder", {}).get("tiers"), [0.35, 0.35, 0.3])