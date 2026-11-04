"""DCA sniper memory layer: warning lessons must not raise the DCA score.

Bug: services/dca_sniper/checklist.py added +0.25 per DCA lesson and +0.4 per
RAG hit regardless of content. 76 of 129 exported lessons say "Avoid chasing
DCA on this pair", so the warning made a heavy DCA more likely.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from intelligence.memory.lesson_polarity import (  # noqa: E402
    NEGATIVE,
    NEUTRAL,
    POSITIVE,
    count_polarity,
    item_polarity,
    lesson_polarity,
)
from services.dca_sniper.checklist import analyze_candidate, build_layers_from_snapshot  # noqa: E402

# Real lesson texts from the 2026-10-31 export (memory_lessons.jsonl).
AVOID = (
    "GRAM/USDT: DCA policy skipped add-ons often (7 recent decisions). "
    "Avoid chasing DCA on this pair while harvest/block signals persist."
)
WEAK = "USELESS/USDT: weak live history (win_rate=25%, pnl=-118.1 USDT, n=4). Prefer smaller size and avoid chasing DCA."
LOST = "Q/USDT: after DCA-policy ALLOW, subsequent sells often lost (n=4, avg_pnl=-52.2 USDT within 72h). Prefer smaller mult or skip on weak structure."
HARVEST = "DCA policy: harvest_skip dominated recent decisions (5/300 events). In HARVEST/RISK_OFF prefer no DCA add-ons; wait for STEADY/DEPLOY before averaging down."
GREEN = "ENA/USDT: after DCA-policy ALLOW, later sells were often green (n=6, avg_pnl=24.9 USDT). Measured add-ons can work here."
SOLID = "BCH/USDT: solid live history (win_rate=100%, pnl=931.7 USDT, n=3)."
DEPLOY = "DCA policy: deploy_boost common (18/300). In DEPLOY/RISK_ON sized add-ons were favored; still respect hard gates."


def _row(**kw):
    base = {
        "symbol": "GRAM/USDT",
        "loss_pct": -30.0,
        "notional": 2000,
        "dca_rounds": 1,
        "max_rounds": 4,
        "rsi": 35,
        "atr_pct": 6,
        "funding_rate_pct": 0.001,
        "entry_bias": "neutral",
    }
    base.update(kw)
    return base


def _mem(**kw) -> dict:
    return build_layers_from_snapshot(_row(**kw), {"spendable_dca": 1000})["memory"]


class TestPolarity:
    def test_real_texts_without_tags(self):
        assert lesson_polarity(AVOID) == NEGATIVE
        assert lesson_polarity(WEAK) == NEGATIVE
        assert lesson_polarity(LOST) == NEGATIVE
        assert lesson_polarity(HARVEST) == NEGATIVE
        assert lesson_polarity(GREEN) == POSITIVE
        assert lesson_polarity(SOLID) == POSITIVE
        assert lesson_polarity(DEPLOY) == NEUTRAL
        assert lesson_polarity("") == NEUTRAL

    def test_tags_decide_before_text(self):
        assert lesson_polarity("whatever", ["dca_policy", "skip", "gram"]) == NEGATIVE
        assert lesson_polarity("whatever", ["dca_policy", "outcome_win", "ena"]) == POSITIVE
        assert lesson_polarity(GREEN, ["outcome_loss"]) == NEGATIVE
        assert lesson_polarity("x", ["fil", "history", "strong_history"]) == POSITIVE

    def test_negative_beats_positive_in_text(self):
        assert lesson_polarity("solid history but avoid DCA now") == NEGATIVE

    def test_item_polarity_shapes(self):
        lesson = SimpleNamespace(text=AVOID, tags=["dca_policy", "skip"])
        event = SimpleNamespace(description="dca add-on skipped, loss", tags=None, metadata={})
        hit = SimpleNamespace(text="trade closed green, profitable", metadata={})
        assert item_polarity(lesson) == NEGATIVE
        assert item_polarity(event) == NEGATIVE
        assert item_polarity(hit) == POSITIVE
        assert item_polarity({"text": GREEN}) == POSITIVE
        assert count_polarity([lesson, hit, {"text": DEPLOY}]) == (1, 1, 1)


class TestChecklistSign:
    def test_warning_lesson_lowers_score(self):
        base = float(_mem()["score"])
        warned = _mem(dca_lesson_count=1, dca_lesson_negative_count=1, dca_lesson_summary=AVOID)
        assert float(warned["score"]) < base
        assert "lessons_neg=1" in warned["reason"]

    def test_positive_lesson_raises_score(self):
        base = float(_mem()["score"])
        assert float(_mem(dca_lesson_count=1, dca_lesson_positive_count=1)["score"]) > base

    def test_unknown_direction_is_neutral(self):
        # Old snapshots carry only the counts. They must not add score any more.
        base = float(_mem()["score"])
        assert float(_mem(dca_lesson_count=3)["score"]) == base
        assert float(_mem(rag_hit_count=3)["score"]) == base

    def test_bug_repro_before_after(self):
        # Before the fix: 2.0 + 0.25 (lesson) + 0.4 (hit) = 2.65 for a pure warning.
        m = _mem(
            dca_lesson_count=1,
            dca_lesson_negative_count=1,
            rag_hit_count=1,
            rag_negative_count=1,
        )
        assert float(m["score"]) == 2.0 - 0.5 - 0.4

    def test_score_clamped(self):
        m = _mem(entry_bias="soft_block", dca_lesson_count=9, dca_lesson_negative_count=9, rag_hit_count=9, rag_negative_count=9)
        assert float(m["score"]) == 0.0
        m = _mem(entry_bias="prefer", dca_lesson_count=9, dca_lesson_positive_count=9, rag_hit_count=9, rag_positive_count=9)
        assert float(m["score"]) == 5.0

    def test_counts_cannot_exceed_total(self):
        a = _mem(dca_lesson_count=1, dca_lesson_negative_count=1, dca_lesson_positive_count=5)
        b = _mem(dca_lesson_count=1, dca_lesson_negative_count=1)
        assert float(a["score"]) == float(b["score"])
        c = _mem(dca_lesson_count=1, dca_lesson_negative_count=5)
        assert float(c["score"]) == float(b["score"])
        d = _mem(rag_hit_count=1, rag_positive_count=5)
        e = _mem(rag_hit_count=1, rag_positive_count=1)
        assert float(d["score"]) == float(e["score"])

    def test_total_checklist_score_lower_with_warning(self):
        cash = {"spendable_dca": 1000}
        plain = analyze_candidate(_row(), cash)["score"]
        warned = analyze_candidate(_row(dca_lesson_count=2, dca_lesson_negative_count=2), cash)["score"]
        assert warned < plain


@settings(max_examples=200, deadline=None)
@given(
    bias=st.sampled_from(["neutral", "prefer", "soft_block"]),
    n_neg=st.integers(0, 6),
    n_pos=st.integers(0, 6),
    r_neg=st.integers(0, 6),
    r_pos=st.integers(0, 6),
)
def test_property_negative_never_helps_positive_never_hurts(bias, n_neg, n_pos, r_neg, r_pos):
    def score(nn, np_, rn, rp):
        return float(
            _mem(
                entry_bias=bias,
                dca_lesson_count=nn + np_,
                dca_lesson_negative_count=nn,
                dca_lesson_positive_count=np_,
                rag_hit_count=rn + rp,
                rag_negative_count=rn,
                rag_positive_count=rp,
            )["score"]
        )

    s = score(n_neg, n_pos, r_neg, r_pos)
    assert 0.0 <= s <= 5.0
    assert score(n_neg + 1, n_pos, r_neg, r_pos) <= s
    assert score(n_neg, n_pos, r_neg + 1, r_pos) <= s
    assert score(n_neg, n_pos + 1, r_neg, r_pos) >= s
    assert score(n_neg, n_pos, r_neg, r_pos + 1) >= s


class TestContextWiring:
    def test_build_dca_context_counts_directions(self):
        from strategies import dca_context as mod

        lessons = [
            SimpleNamespace(text=AVOID, tags=["dca_policy", "skip", "gram"], symbols=["GRAM/USDT"]),
            SimpleNamespace(text=GREEN.replace("ENA", "GRAM"), tags=["dca_policy", "outcome_win", "gram"], symbols=["GRAM/USDT"]),
            SimpleNamespace(text=LOST.replace("Q/", "GRAM/"), tags=["dca_policy", "outcome_loss", "gram"], symbols=["GRAM/USDT"]),
        ]

        class FakeStore:
            def list_lessons(self, limit=50):
                return lessons

            def list_events(self, limit=80):
                return []

        with patch("intelligence.memory.store.MemoryStore", FakeStore), patch(
            "intelligence.memory.store.memory_enabled", return_value=True
        ):
            ctx = mod.build_dca_context(symbol="GRAM/USDT", config_raw={}, include_rag=False)
        assert ctx.dca_lesson_count == 3
        assert ctx.dca_lesson_negative_count == 2
        assert ctx.dca_lesson_positive_count == 1
        assert "Avoid" in ctx.dca_lesson_summary

    def test_enrich_maps_direction_counts(self):
        from services.dca_sniper.deep_analysis import enrich_candidate_from_context
        from strategies.dca_policy import DcaContext

        ctx = DcaContext(
            symbol="GRAM/USDT",
            dca_lesson_count=2,
            dca_lesson_negative_count=2,
            rag_hit_count=1,
            rag_negative_count=1,
        )
        out = enrich_candidate_from_context(_row(), ctx)
        assert out["dca_lesson_negative_count"] == 2
        assert out["rag_negative_count"] == 1
        assert out["dca_lesson_positive_count"] == 0
