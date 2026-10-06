"""#628 Buy-Decision-Tape (observe). Spec freeze T1–T3.

T4/T5 are post-merge measurement and are not this file.

No live HTTP. New tests set BUY_DECISION_TAPE_UNDER_TEST=1 and write
through isolate_bot_logs tmp_path. Nothing is written under data/.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import BotConfig
from risk.risk_manager import RiskManager
from strategies.buy_decision_tape import MACRO_STRESS_KEYS, ROW_KEYS
from tests.unit.test_long_mcap_venue_563 import (
    _EMPTY_BOOK,
    _buy,
    _cfg,
    _eval_env,
)

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_KNOWN_STRESS = {
    "reason": "macro_stress_observe",
    "regime": "RISK_OFF",
    "calendar_mult": 0.5,
    "session_mult": 1.0,
    "pm_mult": 0.8,
    "block_new_entries": True,
    "would_block": True,
    "would_cut": True,
    "observe_enabled": True,
    "fire_enabled": False,
    "sources_ok": True,
    "bias_error": False,
    "mults_error": False,
    "invented_field": "must_not_copy",
}


@pytest.fixture(autouse=True)
def _tape_under_test(monkeypatch):
    monkeypatch.setenv("BUY_DECISION_TAPE_UNDER_TEST", "1")


def _tape_file(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "buy_decision_tape.jsonl"


def _rows(tmp_path: Path) -> list[dict]:
    path = _tape_file(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _money(dec) -> dict:
    order = getattr(dec, "order", None)
    return {
        "approved": dec.approved,
        "code": dec.code,
        "size_multiplier": dec.size_multiplier,
        "qty": None if order is None else order.qty,
        "usdt_amount": None if order is None else order.usdt_amount,
    }


def _assert_money_flags_untouched(cfg: BotConfig) -> None:
    raw = cfg._raw
    assert "buy_decision_tape" not in raw
    assert "fire_enabled" not in raw
    assert "fire_enabled" not in (raw.get("macro_stress_observe") or {})
    assert "fire_enabled" not in (raw.get("risk") or {})


# --- T1 ---


def test_t1_tape_enabled_accepted_and_rejected_money_unchanged(tmp_path, monkeypatch):
    cfg = _cfg()
    rm = RiskManager(cfg)

    monkeypatch.setenv("BUY_DECISION_TAPE", "0")
    with _eval_env(rm, mcap=6_000_000):
        baseline_ok = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    with _eval_env(rm, mcap=4_900_000):
        baseline_rej = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert baseline_ok.approved is True
    assert baseline_rej.approved is False
    assert baseline_rej.code == "long_mcap"
    assert not _tape_file(tmp_path).exists()

    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    with _eval_env(rm, mcap=6_000_000):
        taped_ok = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    with _eval_env(rm, mcap=4_900_000):
        taped_rej = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")

    assert _money(taped_ok) == _money(baseline_ok)
    assert _money(taped_rej) == _money(baseline_rej)
    _assert_money_flags_untouched(cfg)

    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]["outcome"] == "accepted"
    assert rows[0]["filter_codes"] == []
    assert rows[1]["outcome"] == "rejected"
    assert "long_mcap" in rows[1]["filter_codes"]


def test_t1_append_jsonl_raise_does_not_alter_decision(tmp_path, monkeypatch):
    cfg = _cfg()
    rm = RiskManager(cfg)
    monkeypatch.setenv("BUY_DECISION_TAPE", "0")
    with _eval_env(rm, mcap=6_000_000):
        baseline = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")

    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    with patch(
        "services.observability_store.append_jsonl",
        side_effect=OSError("disk full"),
    ):
        with _eval_env(rm, mcap=6_000_000):
            taped = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")

    assert _money(taped) == _money(baseline)
    assert taped.approved is baseline.approved
    assert taped.size_multiplier == baseline.size_multiplier
    _assert_money_flags_untouched(cfg)


# --- T2 ---


def test_t2a_accepted_empty_book_row_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    rm = RiskManager(_cfg())
    order = _buy(source="cmc")
    order.idempotency_key = "tape-corr-628"
    with patch(
        "strategies.macro_stress_observe.observe_macro_stress",
        return_value=None,
    ):
        with _eval_env(rm, mcap=6_000_000):
            dec = rm.evaluate(order, "4h", source="cmc")
    assert dec.approved is True
    assert order.idempotency_key == "tape-corr-628"
    assert order.order_id == ""

    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == set(ROW_KEYS)
    assert _TS_RE.match(row["ts"])
    assert row["symbol"] == "L3/USDT"
    assert row["tenant"]
    assert row["outcome"] == "accepted"
    assert row["signal"] == {"name": "BUY", "source": "cmc"}
    assert row["filter_codes"] == []
    assert row["book"] == {"state": "empty", "dca": False}
    assert row["macro_stress"] is None
    assert row["correlation_id"] == "tape-corr-628"


def test_t2b_rejected_venue_liquidity_block_filter_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    rm = RiskManager(_cfg())
    with _eval_env(rm, metrics=_EMPTY_BOOK, mcap=6_000_000):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"

    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "rejected"
    assert row["filter_codes"][0] == "venue_liquidity_block"
    assert "liq_guard_missing_input" in row["filter_codes"]
    assert set(row.keys()) == set(ROW_KEYS)


def test_t2c_macro_stress_is_whitelisted_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    rm = RiskManager(_cfg())
    expected = {key: _KNOWN_STRESS[key] for key in MACRO_STRESS_KEYS}

    with patch(
        "strategies.macro_stress_observe.observe_macro_stress",
        return_value=dict(_KNOWN_STRESS),
    ):
        with _eval_env(rm, mcap=6_000_000):
            dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is True
    rows = _rows(tmp_path)
    assert len(rows) == 1
    stress = rows[0]["macro_stress"]
    assert stress == expected
    assert "invented_field" not in stress
    assert set(stress.keys()) == set(MACRO_STRESS_KEYS)

    _tape_file(tmp_path).unlink()
    incomplete = {"reason": "macro_stress_observe", "would_block": True}
    with patch(
        "strategies.macro_stress_observe.observe_macro_stress",
        return_value=incomplete,
    ):
        with _eval_env(rm, mcap=6_000_000):
            rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    copied = _rows(tmp_path)[0]["macro_stress"]
    assert copied["reason"] == "macro_stress_observe"
    assert copied["would_block"] is True
    for key in MACRO_STRESS_KEYS:
        if key not in incomplete:
            assert copied[key] is None

    _tape_file(tmp_path).unlink()
    with patch(
        "strategies.macro_stress_observe.observe_macro_stress",
        return_value=None,
    ):
        with _eval_env(rm, mcap=6_000_000):
            rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert _rows(tmp_path)[0]["macro_stress"] is None


# --- T3 ---


def test_t3_open_lot_buy_dca_has_position_no_first_buy_block(tmp_path, monkeypatch):
    cfg = _cfg()
    rm = RiskManager(cfg)
    position = {
        "amount": 2.0,
        "average_entry": 1.0,
        "dca_rounds": 0,
        "strategy_tier": "volatile",
    }

    monkeypatch.setenv("BUY_DECISION_TAPE", "0")
    with _eval_env(rm, position=position, mcap=None):
        baseline = rm.evaluate(_buy(source="dca", signal="BUY_DCA"), "4h", source="dca")
    assert not _tape_file(tmp_path).exists()

    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    with _eval_env(rm, position=position, mcap=None):
        taped = rm.evaluate(_buy(source="dca", signal="BUY_DCA"), "4h", source="dca")

    assert _money(taped) == _money(baseline)
    assert taped.approved is True
    assert taped.code not in ("venue_liquidity_block", "long_mcap")
    _assert_money_flags_untouched(cfg)

    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "accepted"
    assert row["book"] == {"state": "has_position", "dca": True}
    assert row["signal"]["name"] == "BUY_DCA"
    assert row["filter_codes"] == []
