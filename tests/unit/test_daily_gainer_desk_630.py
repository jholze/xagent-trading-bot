"""#630 Daily Gainer Desk (observe). Acceptance D1–D3.

D4 is a post-merge staging count and is not this file.

No live HTTP. New tests set DAILY_GAINER_DESK_UNDER_TEST=1 and write
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
from strategies.daily_gainer_desk import (
    ROW_KEYS,
    candidates_from_gainer_state,
    candidates_from_gis_leaders,
    classify_gainer_desk,
    desk_fire_enabled,
    emit_desk_from_decision,
    observe_gainer_desk,
    observe_gis_leaders,
    observe_scanner_state,
)
from tests.unit.test_long_mcap_venue_563 import _buy, _cfg, _eval_env

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_RELVOL = {"name": "GAINER_RELVOL", "source": "gainer_relvol"}


@pytest.fixture(autouse=True)
def _desk_under_test(monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK_UNDER_TEST", "1")
    from strategies.daily_gainer_desk import _reset_emit_cache_for_tests

    _reset_emit_cache_for_tests()


def _desk_file(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "daily_gainer_desk.jsonl"


def _rows(tmp_path: Path) -> list[dict]:
    path = _desk_file(tmp_path)
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


def _order_snapshot(order) -> dict:
    return {
        "type": order.type,
        "symbol": order.symbol,
        "qty": order.qty,
        "usdt_amount": order.usdt_amount,
        "signal": order.signal,
        "source": order.source,
        "idempotency_key": order.idempotency_key,
    }


def _assert_money_flags_untouched(cfg: BotConfig) -> None:
    raw = cfg._raw
    assert raw["shorts"]["allow_live"] is False
    assert "fire_enabled" not in raw
    assert "fire_enabled" not in (raw.get("risk") or {})
    assert "fire_enabled" not in (raw.get("macro_stress_observe") or {})
    assert desk_fire_enabled(raw) is False


def _relvol_order(symbol: str, *, key: str):
    order = _buy(symbol=symbol, source="gainer_relvol", signal="GAINER_RELVOL", usdt=1200.0)
    order.idempotency_key = key
    return order


def _allow_relvol_ticket(cfg: BotConfig) -> None:
    """RelVol rejects tickets under $1000. Raise the ceiling; do not touch risk gates."""
    cfg._raw["max_usdt_per_trade"] = 1500


def _eval_relvol(rm: RiskManager, order, *, mcap: float):
    with _eval_env(rm, mcap=mcap):
        with patch.object(
            rm,
            "_dynamic_size",
            return_value=(1200.0, {"total_multiplier": 1.0}),
        ):
            return rm.evaluate(order, "4h", source="gainer_relvol")


# --- D1 ---


def test_d1_desk_on_emits_rows_buy_skip_size_unchanged(tmp_path, monkeypatch):
    cfg = _cfg()
    _allow_relvol_ticket(cfg)
    rm = RiskManager(cfg)
    hit = _relvol_order("L3/USDT", key="hit-630")
    rej = _relvol_order("QNT/USDT", key="rej-630")

    monkeypatch.setenv("DAILY_GAINER_DESK", "0")
    baseline_hit = _eval_relvol(rm, hit, mcap=6_000_000)
    baseline_rej = _eval_relvol(rm, rej, mcap=4_900_000)
    assert baseline_hit.approved is True
    assert baseline_rej.approved is False
    assert baseline_rej.code == "long_mcap"
    assert not _desk_file(tmp_path).exists()

    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    hit_on = _relvol_order("L3/USDT", key="hit-630")
    rej_on = _relvol_order("QNT/USDT", key="rej-630")
    desk_hit = _eval_relvol(rm, hit_on, mcap=6_000_000)
    desk_rej = _eval_relvol(rm, rej_on, mcap=4_900_000)

    assert _money(desk_hit) == _money(baseline_hit)
    assert _money(desk_rej) == _money(baseline_rej)
    assert _order_snapshot(hit_on) == _order_snapshot(hit)
    assert _order_snapshot(rej_on) == _order_snapshot(rej)
    _assert_money_flags_untouched(cfg)

    missed = observe_gainer_desk(
        [{"symbol": "SOON/USDT", "source": "scanner", "signal": None}],
        [],
        config=cfg.raw,
    )
    assert len(missed) == 1
    assert missed[0]["bucket"] == "missed"

    rows = _rows(tmp_path)
    by_bucket = {row["bucket"]: row for row in rows}
    assert set(by_bucket) == {"hit", "rejected", "missed"}
    assert by_bucket["hit"]["symbol"] == "L3/USDT"
    assert by_bucket["hit"]["filter_codes"] == []
    assert by_bucket["hit"]["correlation_id"] == "hit-630"
    assert by_bucket["hit"]["source"] == "tape"
    assert by_bucket["rejected"]["symbol"] == "QNT/USDT"
    assert by_bucket["rejected"]["filter_codes"] == ["long_mcap"]
    assert by_bucket["rejected"]["correlation_id"] == "rej-630"
    assert by_bucket["missed"]["symbol"] == "SOON/USDT"
    assert by_bucket["missed"]["source"] == "scanner"
    assert by_bucket["missed"]["filter_codes"] is None
    assert by_bucket["missed"]["correlation_id"] is None


def test_d1_append_failure_does_not_change_decision(tmp_path, monkeypatch):
    cfg = _cfg()
    _allow_relvol_ticket(cfg)
    rm = RiskManager(cfg)
    monkeypatch.setenv("DAILY_GAINER_DESK", "0")
    baseline = _eval_relvol(rm, _relvol_order("L3/USDT", key="fail-630"), mcap=6_000_000)
    assert baseline.approved is True

    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    with patch(
        "services.observability_store.append_jsonl",
        side_effect=OSError("disk full"),
    ):
        taped = _eval_relvol(rm, _relvol_order("L3/USDT", key="fail-630"), mcap=6_000_000)

    assert _money(taped) == _money(baseline)
    assert taped.approved is True
    assert taped.size_multiplier == baseline.size_multiplier
    _assert_money_flags_untouched(cfg)
    assert not _desk_file(tmp_path).exists()


def test_d1_missed_observe_does_not_evaluate(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("evaluate must not run for a missed candidate")

    monkeypatch.setattr(RiskManager, "evaluate", _boom)
    rows = observe_gainer_desk(
        [{"symbol": "SOON/USDT", "source": "list"}],
        [],
        config={"daily_gainer_desk": {"enabled": True, "fire_enabled": True}},
    )
    assert len(rows) == 1
    assert rows[0]["bucket"] == "missed"
    assert rows[0]["source"] == "list"
    assert desk_fire_enabled({"daily_gainer_desk": {"fire_enabled": True}}) is False


def test_d1_fire_enabled_in_config_does_not_change_size(tmp_path, monkeypatch):
    cfg = _cfg()
    _allow_relvol_ticket(cfg)
    cfg._raw["daily_gainer_desk"] = {
        "enabled": True,
        "fire_enabled": True,
        "allow_live": True,
    }
    rm = RiskManager(cfg)
    monkeypatch.setenv("DAILY_GAINER_DESK", "0")
    baseline = _eval_relvol(rm, _relvol_order("L3/USDT", key="fire-630"), mcap=6_000_000)
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    taped = _eval_relvol(rm, _relvol_order("L3/USDT", key="fire-630"), mcap=6_000_000)
    assert _money(taped) == _money(baseline)
    assert taped.size_multiplier == baseline.size_multiplier
    assert desk_fire_enabled(cfg.raw) is False
    assert cfg.raw["shorts"]["allow_live"] is False
    assert cfg.raw["daily_gainer_desk"]["fire_enabled"] is True
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["bucket"] == "hit"
    assert "fire_enabled" not in rows[0]
    assert "allow_live" not in rows[0]


def test_d1_non_gainer_buy_does_not_emit(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=6_000_000):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is True
    assert not _desk_file(tmp_path).exists()


# --- D2 ---


def _assert_schema(row: dict) -> None:
    assert set(row.keys()) == set(ROW_KEYS)
    assert _TS_RE.match(row["ts"])
    assert row["symbol"]
    assert row["bucket"] in ("hit", "late", "missed", "rejected")
    assert row["source"] in ("tape", "scanner", "list")
    signal = row["signal"]
    if signal is not None:
        assert set(signal.keys()) == {"name", "source"}


def test_d2_hit_late_missed_rejected_schema():
    rows = classify_gainer_desk(
        [
            {"symbol": "SOON/USDT", "source": "scanner", "api_key": "sekret"},
            {
                "symbol": "ETN/USDT",
                "source": "list",
                "signal": {"name": "GAINER_SIGNAL"},
            },
        ],
        [
            {
                "ts": "2026-10-02T12:00:00Z",
                "symbol": "L3/USDT",
                "outcome": "accepted",
                "signal": {**_RELVOL, "api_key": "sekret"},
                "filter_codes": [],
                "correlation_id": "hit-corr",
                "api_key": "sekret",
            },
            {
                "symbol": "Q/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["gainer_chase_guard"],
                "correlation_id": "late-corr",
            },
            {
                "symbol": "RHEA/USDT",
                "outcome": "rejected",
                "signal": {"name": "GAINER_RELVOL", "source": "gainer_relvol"},
                "filter_codes": ["size_too_small", "venue_liquidity_block"],
                "correlation_id": "rej-corr",
            },
        ],
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"ETN/USDT", "L3/USDT", "Q/USDT", "RHEA/USDT", "SOON/USDT"}

    hit = by_symbol["L3/USDT"]
    _assert_schema(hit)
    assert hit["bucket"] == "hit"
    assert hit["signal"] == _RELVOL
    assert hit["filter_codes"] == []
    assert hit["source"] == "tape"
    assert hit["correlation_id"] == "hit-corr"
    assert "api_key" not in hit

    late = by_symbol["Q/USDT"]
    _assert_schema(late)
    assert late["bucket"] == "late"
    assert late["filter_codes"] == ["gainer_chase_guard"]
    assert late["source"] == "tape"
    assert late["correlation_id"] == "late-corr"

    missed = by_symbol["SOON/USDT"]
    _assert_schema(missed)
    assert missed["bucket"] == "missed"
    assert missed["signal"] is None
    assert missed["filter_codes"] is None
    assert missed["source"] == "scanner"
    assert missed["correlation_id"] is None

    listed = by_symbol["ETN/USDT"]
    _assert_schema(listed)
    assert listed["bucket"] == "missed"
    assert listed["source"] == "list"
    assert listed["signal"] == {"name": "GAINER_SIGNAL", "source": None}
    assert listed["filter_codes"] is None
    assert listed["correlation_id"] is None

    rejected = by_symbol["RHEA/USDT"]
    _assert_schema(rejected)
    assert rejected["bucket"] == "rejected"
    assert rejected["filter_codes"] == ["size_too_small", "venue_liquidity_block"]
    assert rejected["source"] == "tape"
    assert rejected["correlation_id"] == "rej-corr"


def test_d2_missing_optional_is_null_not_invented(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    rows = observe_gainer_desk(
        [{"symbol": "soon_usdt", "source": "scanner"}],
        [
            {
                "symbol": "QNT/USDT",
                "outcome": "rejected",
                "signal": {"name": "GAINER_RELVOL"},
                "filter_codes": ["long_mcap"],
            }
        ],
        config={"daily_gainer_desk": {"enabled": True}},
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["QNT/USDT"]["signal"] == {"name": "GAINER_RELVOL", "source": None}
    assert by_symbol["QNT/USDT"]["correlation_id"] is None
    assert by_symbol["QNT/USDT"]["bucket"] == "rejected"
    assert by_symbol["SOON/USDT"]["signal"] is None
    assert by_symbol["SOON/USDT"]["filter_codes"] is None
    assert by_symbol["SOON/USDT"]["correlation_id"] is None

    raw_lines = _desk_file(tmp_path).read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in raw_lines if line.strip()]
    soon = next(row for row in parsed if row["symbol"] == "SOON/USDT")
    assert soon["filter_codes"] is None
    assert soon["signal"] is None
    assert soon["correlation_id"] is None
    assert "api_key" not in json.dumps(parsed)


def test_d2_sell_and_empty_symbol_emit_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    order = _buy(source="gainer_relvol", signal="GAINER_RELVOL")
    order.type = "SELL"
    emit_desk_from_decision(order, type("D", (), {"approved": True, "code": ""})(), source="gainer_relvol")
    rows = classify_gainer_desk(
        [{"symbol": "  ", "source": "scanner"}, {"source": "list"}],
        [{"symbol": "", "outcome": "rejected", "signal": _RELVOL, "filter_codes": ["long_mcap"]}],
    )
    assert rows == []
    assert not _desk_file(tmp_path).exists()


# --- D3 ---


def test_d3_scanner_or_list_without_tape_is_missed_not_rejected():
    rows = classify_gainer_desk(
        [
            {"symbol": "soon/usdt", "source": "scanner"},
            {"symbol": "AT/USDT", "source": "list"},
        ],
        [
            {
                "symbol": "SOON/USDT",
                "signal": _RELVOL,
                "filter_codes": ["long_mcap"],
            }
        ],
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"SOON/USDT", "AT/USDT"}
    assert by_symbol["SOON/USDT"]["bucket"] == "missed"
    assert by_symbol["SOON/USDT"]["source"] == "scanner"
    assert by_symbol["SOON/USDT"]["bucket"] != "rejected"
    assert by_symbol["AT/USDT"]["bucket"] == "missed"
    assert by_symbol["AT/USDT"]["source"] == "list"
    assert by_symbol["AT/USDT"]["filter_codes"] is None


def test_d3_tape_reject_keeps_ist_codes_and_accepted_relvol_is_hit():
    rows = classify_gainer_desk(
        [
            {"symbol": "QNT/USDT", "source": "scanner"},
            {"symbol": "L3/USDT", "source": "list"},
        ],
        [
            {
                "ts": "2026-10-02T11:00:00Z",
                "symbol": "QNT/USDT",
                "outcome": "rejected",
                "signal": {"name": "BUY", "source": "cmc"},
                "filter_codes": ["venue_liquidity_block"],
                "correlation_id": "older",
            },
            {
                "ts": "2026-10-02T12:00:00Z",
                "symbol": "QNT/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["long_mcap"],
                "correlation_id": "newer",
            },
            {
                "ts": "2026-10-02T09:00:00Z",
                "symbol": "L3/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["size_too_small"],
                "correlation_id": "lost",
            },
            {
                "ts": "2026-10-02T10:00:00Z",
                "symbol": "L3/USDT",
                "outcome": "accepted",
                "signal": _RELVOL,
                "filter_codes": [],
                "correlation_id": "fill-1",
            },
        ],
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["QNT/USDT"]["bucket"] == "rejected"
    assert by_symbol["QNT/USDT"]["filter_codes"] == ["long_mcap"]
    assert by_symbol["QNT/USDT"]["correlation_id"] == "newer"
    assert by_symbol["QNT/USDT"]["source"] == "tape"
    assert by_symbol["QNT/USDT"]["signal"] == _RELVOL
    assert by_symbol["L3/USDT"]["bucket"] == "hit"
    assert by_symbol["L3/USDT"]["signal"] == _RELVOL
    assert by_symbol["L3/USDT"]["filter_codes"] == []
    assert by_symbol["L3/USDT"]["correlation_id"] == "fill-1"
    assert by_symbol["L3/USDT"]["source"] == "tape"


def test_d3_accepted_relvol_tape_without_candidate_is_hit():
    rows = classify_gainer_desk(
        [],
        [
            {
                "symbol": "L3/USDT",
                "outcome": "accepted",
                "signal": _RELVOL,
                "filter_codes": [],
                "correlation_id": "solo",
            }
        ],
    )
    assert len(rows) == 1
    assert rows[0]["bucket"] == "hit"
    assert rows[0]["source"] == "tape"
    assert rows[0]["signal"] == _RELVOL
    assert rows[0]["correlation_id"] == "solo"


def test_d3_non_gainer_tape_alone_is_not_a_desk_row():
    rows = classify_gainer_desk(
        [],
        [
            {
                "symbol": "BTC/USDT",
                "outcome": "rejected",
                "signal": {"name": "BUY", "source": "cmc"},
                "filter_codes": ["long_mcap"],
                "correlation_id": "cmc-1",
            }
        ],
    )
    assert rows == []


def test_d3_named_late_rule_only_gainer_chase_guard():
    late = classify_gainer_desk(
        [],
        [
            {
                "symbol": "Q/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["gainer_chase_guard", "size_too_small"],
                "correlation_id": "late-1",
            }
        ],
    )
    assert late[0]["bucket"] == "late"
    assert late[0]["filter_codes"] == ["gainer_chase_guard", "size_too_small"]

    not_late = classify_gainer_desk(
        [],
        [
            {
                "ts": "2026-10-02T08:00:00Z",
                "symbol": "Q/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["gainer_chase_guard"],
                "correlation_id": "old",
            },
            {
                "ts": "2026-10-02T09:00:00Z",
                "symbol": "Q/USDT",
                "outcome": "rejected",
                "signal": _RELVOL,
                "filter_codes": ["trade_cooldown"],
                "correlation_id": "new",
            },
        ],
    )
    assert not_late[0]["bucket"] == "rejected"
    assert not_late[0]["filter_codes"] == ["trade_cooldown"]


def test_d3_gainer_state_and_gis_list_join(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")
    state = {
        "live_top": [{"symbol": "SOON/USDT", "pct_24h": 40, "api_key": "sekret"}],
        "eligible": [
            {"symbol": "SOON/USDT", "source": "gate_prev_top"},
            {"symbol": "ATOS/USDT", "source": "gate_prev_top"},
        ],
        "streaks": [{"symbol": "RHEA/USDT"}],
    }
    cands = candidates_from_gainer_state(state)
    by_symbol = {row["symbol"]: row["source"] for row in cands}
    assert by_symbol == {
        "SOON/USDT": "scanner",
        "RHEA/USDT": "scanner",
        "ATOS/USDT": "list",
    }
    assert all("api_key" not in row for row in cands)

    leaders = candidates_from_gis_leaders(
        [{"symbol": "ACX/USDT", "pct_24h": 22, "quote_vol": 1}, {"symbol": "SOON/USDT"}]
    )
    assert [row["source"] for row in leaders] == ["list", "list"]

    tape = [
        {
            "symbol": "RHEA/USDT",
            "outcome": "rejected",
            "signal": _RELVOL,
            "filter_codes": ["venue_liquidity_block"],
            "correlation_id": "rhe-1",
        }
    ]
    monkeypatch.setattr(
        "strategies.daily_gainer_desk.read_buy_decision_tape",
        lambda *_args, **_kwargs: tape,
    )
    scanned = observe_scanner_state(state, config={"daily_gainer_desk": {"enabled": True}})
    got = {row["symbol"]: row for row in scanned}
    assert got["SOON/USDT"]["bucket"] == "missed"
    assert got["SOON/USDT"]["source"] == "scanner"
    assert got["ATOS/USDT"]["bucket"] == "missed"
    assert got["ATOS/USDT"]["source"] == "list"
    assert got["RHEA/USDT"]["bucket"] == "rejected"
    assert got["RHEA/USDT"]["filter_codes"] == ["venue_liquidity_block"]

    listed = observe_gis_leaders(
        [{"symbol": "ACX/USDT", "pct_24h": 22}],
        tape_rows=[],
        config={"daily_gainer_desk": {"enabled": True}},
    )
    assert len(listed) == 1
    assert listed[0]["bucket"] == "missed"
    assert listed[0]["source"] == "list"
    assert listed[0]["signal"] is None
    assert _rows(tmp_path)[-1]["symbol"] == "ACX/USDT"


def test_d3_scanner_observe_fail_open_when_tape_read_breaks(monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK", "1")

    def _boom(*_args, **_kwargs):
        raise OSError("tape unreadable")

    monkeypatch.setattr("strategies.daily_gainer_desk.read_buy_decision_tape", _boom)
    assert (
        observe_scanner_state(
            {"live_top": [{"symbol": "SOON/USDT"}]},
            config={"daily_gainer_desk": {"enabled": True}},
        )
        == []
    )
