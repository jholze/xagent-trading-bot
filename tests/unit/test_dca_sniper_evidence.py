"""Evidence pack: news/facts from memory, path stats, wallet adapter."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.dca_sniper.evidence import (
    EvidencePack,
    NewsItem,
    apply_evidence_size_adjust,
    apply_evidence_to_candidate,
    evaluate_wallet_soft,
    events_to_news_items,
    gather_evidence,
    news_is_hard,
    wallet_evidence,
)


@dataclass
class _Ev:
    event_type: str
    impact_score: float
    description: str
    source: str = "test"
    created_at: str = ""


class TestNewsItems(unittest.TestCase):
    def test_hard_news_detect(self):
        items = [
            NewsItem("unlock", -0.5, "token unlock", age_hours=2),
            NewsItem("partnership", 0.3, "partner", age_hours=10),
        ]
        self.assertTrue(news_is_hard(items))
        self.assertFalse(news_is_hard([NewsItem("partnership", 0.2, "x", age_hours=1)]))

    def test_events_to_news_age(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=5)).isoformat()
        items, freshest = events_to_news_items(
            [_Ev("catalyst", 0.4, "launch", created_at=old)],
            now=now,
        )
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(freshest or 0, 5.0, delta=0.1)


class TestWalletAdapter(unittest.TestCase):
    def test_unavailable_without_provider(self):
        w = wallet_evidence("X/USDT", provider=None)
        self.assertFalse(w["available"])
        self.assertEqual(w["status"], "unavailable")
        mult, codes = evaluate_wallet_soft(w)
        self.assertEqual(mult, 1.0)
        self.assertEqual(codes, [])

    def test_inflow_soft_mult(self):
        class P:
            def fetch_flows(self, symbol):
                return {"exchange_inflow": 2.5, "net_flow": -1.0}

        w = wallet_evidence("X/USDT", provider=P())
        self.assertTrue(w["available"])
        mult, codes = evaluate_wallet_soft(w)
        self.assertLess(mult, 1.0)
        self.assertTrue(any("inflow" in c for c in codes))


class TestEvidenceSize(unittest.TestCase):
    def test_hard_news_demotes_heavy(self):
        pack = EvidencePack(
            symbol="X/USDT",
            news=[NewsItem("hack", -0.9, "exploit", age_hours=1)],
            hard_news=True,
            facts_fresh=True,
        )
        usdt, reason, extra = apply_evidence_size_adjust(
            2000,
            "DCA_HEAVY",
            pack,
            cfg={"small_dca_usdt": 500, "min_meaningful_usdt": 200, "deep_hard_news_blocks_heavy": True},
        )
        self.assertLessEqual(usdt, 500)
        self.assertIn("hard_news", reason)

    def test_path_caution_trims(self):
        pack = EvidencePack(
            symbol="X/USDT",
            path_stats={"available": True, "hint": "high_giveback_caution"},
        )
        usdt, reason, _ = apply_evidence_size_adjust(1000, "DCA_SMALL", pack, cfg={})
        self.assertAlmostEqual(usdt, 850.0)


class TestGatherEvidence(unittest.TestCase):
    def test_gather_with_injected_events(self):
        now = datetime.now(timezone.utc)
        ev = _Ev(
            "unlock",
            -0.6,
            "large unlock",
            created_at=(now - timedelta(hours=3)).isoformat(),
        )
        with patch(
            "services.dca_sniper.evidence.load_path_stats_brief",
            return_value={"available": False, "reason": "no_data"},
        ):
            pack = gather_evidence(
                "BLESS/USDT",
                events=[ev],
                lookback_hours=72,
            )
        self.assertTrue(pack.facts_fresh)
        self.assertTrue(pack.hard_news)
        self.assertGreaterEqual(len(pack.news), 1)
        cand = apply_evidence_to_candidate({"symbol": "BLESS/USDT"}, pack)
        self.assertTrue(cand.get("unlock_risk") or cand.get("hard_news"))
        self.assertIn("news_brief", cand)


if __name__ == "__main__":
    unittest.main()
