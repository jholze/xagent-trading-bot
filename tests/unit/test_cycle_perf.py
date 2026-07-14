import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import SignalAnalysis
from services.audit_trail import AuditTrail
from services.market_service import MarketService
from strategies.decision_engine import DecisionEngine


class TestMarketServiceBtcCache(unittest.TestCase):
    def test_prefetch_reuses_btc_ohlcv_within_cycle(self):
        svc = MarketService()
        frame = pd.DataFrame({"close": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0]})
        with patch.object(svc, "_fetch_ohlcv", return_value=frame) as fetch:
            svc.begin_cycle()
            svc.prefetch_btc_ohlcv("4h", 20)
            first = svc._get_btc_ohlcv("4h", 20)
            second = svc._get_btc_ohlcv("4h", 20)
        self.assertIs(first, second)
        fetch.assert_called_once_with("BTC/USDT", "4h", 20)


class TestDecisionEngineCycle(unittest.TestCase):
    def test_begin_tenant_cycle_reuses_regime_detector(self):
        market = MagicMock()
        market.begin_cycle = MagicMock()
        market.prefetch_btc_ohlcv = MagicMock()
        engine = DecisionEngine(market_service=market)

        class _Cfg:
            raw = {
                "regime_detector": {"enabled": True},
                "strategy_allocator": {"enabled": True},
            }

            def refresh(self):
                return None

            @property
            def regime_detector_config(self):
                return {"tech_weight": 0.62, "sentiment_weight": 0.38}

        engine.config = _Cfg()
        engine.begin_tenant_cycle()
        first = engine._tenant_regime_detector
        engine.begin_tenant_cycle()
        second = engine._tenant_regime_detector
        self.assertIsNotNone(first)
        self.assertIsNotNone(engine._tenant_strategy_allocator)
        self.assertIsNot(first, second)


class TestAuditTrailPerf(unittest.TestCase):
    def test_hold_skips_position_metrics(self):
        analysis = SignalAnalysis(
            action="HOLD",
            symbol="TST/USDT",
            timeframe="4h",
            rsi=50.0,
            lower_bb=1.0,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="neutral",
            normalized_action="HOLD",
            rationale="test",
        )

        class _Cfg:
            raw = {"observability": {"decisions_audit": True}}
            trading_mode = "demo"

        with patch("strategies.registry.resolve_strategy_params") as resolve_params, \
             patch("strategies.positions.get_position", return_value={"amount": 1.0, "average_entry": 1.0}), \
             patch("services.observability_store.persist_decision"), \
             patch("services.audit_trail.log_decision") as log_dec:
            AuditTrail(config=_Cfg()).record(
                {"symbol": "TST/USDT", "timeframe": "4h"},
                analysis,
                price=1.0,
            )
        resolve_params.assert_not_called()
        log_dec.assert_called_once()
        entry = log_dec.call_args[0][0]
        self.assertNotIn("gain_pct", entry)


class TestDecisionLogRotation(unittest.TestCase):
    def test_rotate_when_max_bytes_exceeded(self):
        from logger import DECISIONS_LOG_FILE, LOG_DIR, log_decision

        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            log_path = log_dir / "decisions.jsonl"
            with patch("logger.LOG_DIR", str(log_dir)), \
                 patch("logger.DECISIONS_LOG_FILE", str(log_path)), \
                 patch("logger._observability_cfg", return_value={"decisions_log_max_bytes": 10, "decisions_log_rotate_keep": 2}):
                log_path.write_text("x" * 20 + "\n", encoding="utf-8")
                log_decision({"symbol": "A/USDT", "action": "HOLD"})
            self.assertTrue(log_path.exists())
            archives = list(log_dir.glob("decisions.*.jsonl"))
            self.assertEqual(len(archives), 1)
            self.assertTrue(json.loads(log_path.read_text(encoding="utf-8").strip())["symbol"] == "A/USDT")


class TestPositionSnapshotTenantCounter(unittest.TestCase):
    def test_counters_are_per_tenant(self):
        import services.position_tracking as pt

        pt._cycle_counters.clear()
        cfg = {"observability": {"position_snapshots_enabled": True, "position_snapshots_every_n_cycles": 2}}

        with patch("core.tenant_context.resolve_tenant_id", side_effect=["default", "henry", "default"]), \
             patch("services.position_tracking.snapshot_all_open_positions", return_value=None) as snap:
            pt.maybe_snapshot_after_cycle(config_raw=cfg)
            pt.maybe_snapshot_after_cycle(config_raw=cfg)
            pt.maybe_snapshot_after_cycle(config_raw=cfg)

        self.assertEqual(snap.call_count, 1)


class TestCycleShared(unittest.TestCase):
    def test_union_watchlists_dedupes_symbols(self):
        from services.cycle_shared import union_tenant_watchlists

        coins_a = [{"symbol": "BTC/USDT", "active": True}, {"symbol": "ETH/USDT", "active": True}]
        coins_b = [{"symbol": "BTC/USDT", "active": True}, {"symbol": "SOL/USDT", "active": True}]

        with patch("core.tenant_routing.iter_price_cycle_tenants", return_value=["default", "henry"]), \
             patch("core.tenant_routing.tenant_cycle_context"), \
             patch("data_manager.load_effective_watchlist", side_effect=[coins_a, coins_b]):
            merged = union_tenant_watchlists()
        symbols = {c["symbol"] for c in merged}
        self.assertEqual(symbols, {"BTC/USDT", "ETH/USDT", "SOL/USDT"})


if __name__ == "__main__":
    unittest.main()