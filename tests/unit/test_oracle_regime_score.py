"""Continuous market-oracle regime score (#835).

The numeric anchors live in committed config.json. These tests pin that
draft: clipping, weight renormalisation, the EMA half-life step, the four
mapping anchors, monotonicity, stale/missing fallback, mode order, an off
path that does not read the score, a shadow decision that matches the fixed
filter, and an oracle poll that survives a score exception.
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from core.tenant_context import tenant_context
from risk import rule_switches as rs
from services.market_oracle import score as score_mod
from services.market_oracle import score_runtime
from services.market_oracle.score import (
    clamp_not_looser,
    components,
    ema_update,
    filter_params,
    raw_score,
    resolve_mode,
    score_is_fresh,
    smooth_score,
    weights_of,
)
from services.market_oracle.score_runtime import (
    apply_poll_score,
    committed_oracle_score,
    current_mode,
    reset_for_tests,
    scored_trend_decision,
)
from test_tenant_rule_switches import (
    FLAT_H,
    NOW,
    SYM,
    _daily,
    _evaluate,
    _hourly,
    _on,
    _order,
    _rm,
    _uptrend_daily,
    _Market,
)

ROOT = Path(__file__).resolve().parents[2]
REF_BASE = {
    "sma_days": 3,
    "max_extension_pct": 20,
    "max_pump_24h_pct": 20,
    "mcap_floor_usd": 5_000_000,
    "liq_floor_usdt": 50_000,
}
TREND = _on("trend_filter", sma_days=3, max_extension_pct=20, max_pump_24h_pct=20)


def _mapping():
    reset_for_tests()
    return score_runtime._mapping(committed_oracle_score())


def _score(s, ts, raw=None):
    return {"smoothed": s, "raw": s if raw is None else raw, "ts": ts, "components": {}}


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


def _one_component_block(**overrides):
    block = {
        "half_life_sec": 100,
        "max_gap_sec": 100,
        "history_min_interval_sec": 300,
        "history_retain_sec": 1000,
        "stale_after_sec": 1800,
        "components": [
            {"name": "a", "feature": "x", "weight": 1, "scale": 1, "center": 0, "sign": 1}
        ],
    }
    block.update(overrides)
    return block


# --- pure math -----------------------------------------------------------------------


def test_component_clip_and_sign():
    specs = [
        {"name": "up", "feature": "x", "scale": 1, "center": 0, "sign": 1, "weight": 1},
        {"name": "down", "feature": "y", "scale": 0.05, "center": 0, "sign": -1, "weight": 1},
    ]
    parts = components({"x": 50, "y": 1, "z": 3}, specs)
    assert parts["up"] == 1.0
    assert parts["down"] == -1.0
    assert "z" not in parts
    low = components({"x": -50, "y": -1}, specs)
    assert low["up"] == -1.0
    assert low["down"] == 1.0


def test_missing_component_renormalises_weights():
    weights = {"a": 0.25, "b": 0.75}
    assert raw_score({"a": 1.0}, weights) == pytest.approx(1.0)
    assert raw_score({"a": 1.0, "b": -1.0}, weights) == pytest.approx(-0.5)
    assert raw_score({}, weights) is None
    specs = committed_oracle_score()["components"]
    features = {spec["feature"]: spec.get("center", 0) for spec in specs}
    parts = components(features, specs)
    assert set(parts) == {spec["name"] for spec in specs}
    assert raw_score(parts, weights_of(specs)) == pytest.approx(0.0)
    dropped = dict(features)
    del dropped[specs[-1]["feature"]]
    parts_dropped = components(dropped, specs)
    assert specs[-1]["name"] not in parts_dropped
    assert raw_score(parts_dropped, weights_of(specs)) == pytest.approx(0.0)


def test_shipped_components_clip_at_the_configured_scales():
    specs = committed_oracle_score()["components"]
    features = {spec["feature"]: spec.get("center", 0) for spec in specs}
    features["btc_ret_24h_pct"] = 1000
    features["btc_funding_rate_pct"] = 1
    parts = components(features, specs)
    assert parts["ret_24h_a"] == 1.0
    assert parts["funding"] == -1.0


def test_ema_half_life_is_a_half_step():
    half = committed_oracle_score()["half_life_sec"]
    assert ema_update(0, 1, half, half) == pytest.approx(0.5)
    assert ema_update(None, 0.4, half, half) == pytest.approx(0.4)
    held, reset = smooth_score(0.2, 0.0, None, half, half_life_sec=half, max_gap_sec=half)
    assert held == pytest.approx(0.2) and reset is False


def test_ema_gap_longer_than_configured_max_resets_to_raw():
    half = 100
    smoothed, reset = smooth_score(0.0, 0.0, 1.0, half, half_life_sec=half, max_gap_sec=half)
    assert reset is False and smoothed == pytest.approx(0.5)
    jumped, did_reset = smooth_score(0.0, 0.0, 1.0, half + 1, half_life_sec=half, max_gap_sec=half)
    assert did_reset is True and jumped == pytest.approx(1.0)


def test_mapping_anchors_match_the_committed_draft():
    mapping = _mapping()
    expected = {
        -1: dict(sma_days=50, max_extension_pct=10, max_pump_24h_pct=10,
                 mcap_floor_usd=20_000_000, liq_floor_usdt=200_000, size_round_up_usdt=0),
        0: dict(sma_days=3, max_extension_pct=20, max_pump_24h_pct=20,
                mcap_floor_usd=5_000_000, liq_floor_usdt=50_000, size_round_up_usdt=0),
        0.6: dict(sma_days=3, max_extension_pct=30, max_pump_24h_pct=25,
                  mcap_floor_usd=3_000_000, liq_floor_usdt=30_000, size_round_up_usdt=0),
        0.8: dict(sma_days=2, max_extension_pct=40, max_pump_24h_pct=35,
                  mcap_floor_usd=2_000_000, liq_floor_usdt=20_000, size_round_up_usdt=50),
    }
    for s, want in expected.items():
        got = filter_params(s, REF_BASE, mapping)
        assert got["s"] == pytest.approx(s)
        for key, value in want.items():
            assert got[key] == pytest.approx(value), (s, key, got[key])
    # Past the last anchor the loosest row is held. s=+0.8 is sma 2, not 1.
    top = filter_params(1, REF_BASE, mapping)
    assert top["sma_days"] == 2
    assert top["max_extension_pct"] == pytest.approx(40)


def test_higher_score_is_never_stricter():
    mapping = _mapping()
    previous = None
    for step in range(0, 41):
        got = filter_params(-1 + step * 0.05, REF_BASE, mapping)
        if previous is not None:
            assert got["sma_days"] <= previous["sma_days"]
            assert got["max_extension_pct"] >= previous["max_extension_pct"] - 1e-9
            assert got["max_pump_24h_pct"] >= previous["max_pump_24h_pct"] - 1e-9
            assert got["mcap_floor_usd"] <= previous["mcap_floor_usd"] + 1e-6
            assert got["liq_floor_usdt"] <= previous["liq_floor_usdt"] + 1e-6
            assert got["size_round_up_usdt"] >= previous["size_round_up_usdt"] - 1e-9
        previous = got


def test_other_tenant_base_scales_around_its_own_s0():
    mapping = _mapping()
    base = dict(REF_BASE)
    base["sma_days"] = 6
    assert filter_params(0, base, mapping)["sma_days"] == 6
    assert filter_params(-1, base, mapping)["sma_days"] == 100
    assert filter_params(0.8, base, mapping)["sma_days"] == 4


def test_negative_score_cannot_map_looser_than_the_tenant_base():
    bad = {
        "reference": {"sma_days": 3, "max_extension_pct": 20},
        "anchors": [
            {"s": -1, "sma_days": 1, "max_extension_pct": 100},
            {"s": 0, "sma_days": 3, "max_extension_pct": 20},
        ],
    }
    base = {"sma_days": 3, "max_extension_pct": 20}
    mapped = filter_params(-1, base, bad)
    assert mapped["sma_days"] == 1
    clamped = clamp_not_looser(mapped, base, -1)
    assert clamped["sma_days"] == 3
    assert clamped["max_extension_pct"] == pytest.approx(20)
    loose = clamp_not_looser({"sma_days": 2, "max_extension_pct": 40}, base, 0.8)
    assert loose["sma_days"] == 2
    assert loose["max_extension_pct"] == pytest.approx(40)


def test_mode_resolution_order():
    committed = {"mode": "off", "tenant_modes": {"henry": "shadow"}}
    assert resolve_mode({"mode": "enforce"}, committed, "henry") == "enforce"
    assert resolve_mode({"mode": ""}, committed, "henry") == "shadow"
    assert resolve_mode({"mode": None}, committed, "henry") == "shadow"
    assert resolve_mode({}, committed, "henry") == "shadow"
    assert resolve_mode(None, committed, "default") == "off"
    assert resolve_mode({"mode": "banana"}, {"mode": "enforce"}, "henry") == "off"
    assert resolve_mode({"mode": True}, {"mode": "off"}, "henry") == "enforce"
    assert resolve_mode(None, {}, None) == "off"
    reset_for_tests()
    assert current_mode("default") == "off"
    assert current_mode("henry") == "shadow"
    assert current_mode("other") == "off"


def test_tenant_body_mode_wins(monkeypatch):
    monkeypatch.setattr(
        score_runtime, "read_tenant_oracle_block", lambda tenant_id: {"mode": "enforce"}
    )
    assert current_mode("henry") == "enforce"


def test_committed_block_leaves_henry_trend_filter_untouched():
    reset_for_tests()
    block = committed_oracle_score()
    assert block["mode"] == "off"
    assert block["tenant_modes"] == {"henry": "shadow"}
    assert abs(sum(row["weight"] for row in block["components"]) - 1.0) < 1e-9
    raw = __import__("json").loads((ROOT / "config.json").read_text())
    assert "trend_filter" not in raw["rule_switches"]
    for rel in (
        "services/market_oracle/score.py",
        "services/market_oracle/score_runtime.py",
    ):
        text = (ROOT / rel).read_text()
        assert "BTC" not in text and "ETH" not in text


def test_score_freshness_uses_the_configured_limit():
    limit = committed_oracle_score()["stale_after_sec"]
    assert score_is_fresh(1_000, 1_000 + limit, limit) is True
    assert score_is_fresh(1_000, 1_000 + limit + 1, limit) is False
    assert score_is_fresh(None, 1_000, limit) is False


# --- poll persistence -----------------------------------------------------------------


def test_poll_continues_ema_and_resets_after_a_gap():
    block = _one_component_block()
    state = {
        "oracle_score_memory_loaded": True,
        "oracle_score_memory": {"smoothed": 0.0, "ts": 0.0},
    }
    published = apply_poll_score({"x": 5}, state, now=100, block=block, sink=lambda *_: None)
    assert published["raw"] == pytest.approx(1.0)
    assert published["smoothed"] == pytest.approx(0.5)
    assert state["oracle_score_memory"]["smoothed"] == pytest.approx(0.5)
    restarted = {
        "oracle_score_memory_loaded": True,
        "oracle_score_memory": {"smoothed": 0.0, "ts": 0.0},
    }
    reset = apply_poll_score({"x": 5}, restarted, now=101, block=block, sink=lambda *_: None)
    assert reset["smoothed"] == pytest.approx(1.0)


def test_history_is_downsampled_and_carries_a_retention_cutoff():
    block = _one_component_block()
    state = {"oracle_score_memory_loaded": True, "oracle_score_memory": {}}
    ops: list[tuple] = []
    for now in (0, 299, 300):
        apply_poll_score(
            {"x": 5}, state, now=now, block=block, sink=lambda op, payload: ops.append((op, payload))
        )
    history = [payload for op, payload in ops if op == "history"]
    assert len(history) == 2
    assert history[0]["row"]["raw"] == pytest.approx(1.0)
    assert history[0]["row"]["smoothed"] == pytest.approx(1.0)
    assert history[0]["row"]["components"]["a"] == pytest.approx(1.0)
    assert history[0]["cutoff"] == pytest.approx(-1000)
    assert history[1]["cutoff"] == pytest.approx(300 - 1000)
    assert [op for op, _ in ops].count("state") == 3


def test_poll_swallows_a_score_exception():
    block = _one_component_block()

    def boom(*_a, **_k):
        raise RuntimeError("score boom")

    with patch.object(score_runtime, "compute_poll", boom):
        assert apply_poll_score({"x": 1}, {}, now=1, block=block) is None


# --- trend filter ---------------------------------------------------------------------


def _closed_hourly(closes=None):
    return rs.closed_bars(_hourly(closes or FLAT_H), NOW, rs.HOUR)


def _fetch(daily, seen):
    def fetch(limit):
        seen.append(limit)
        return daily
    return fetch


def test_shadow_keeps_the_fixed_pass_and_records_the_counterfactual():
    seen: list[int] = []
    logs: list[str] = []
    rows: list[tuple] = []
    daily = _daily([120.0] * 40 + [100.0] * 9 + [105.0, 105.0])
    check = scored_trend_decision(
        trend_cfg=TREND["trend_filter"],
        hourly=_closed_hourly(),
        fetch_daily=_fetch(daily, seen),
        now=NOW,
        symbol=SYM,
        tenant_id="henry",
        config_raw={"risk": {"venue_quality": {"min_quote_volume_24h_usdt": 50_000}}},
        score_block=_score(-1, NOW, raw=-0.4),
        mode="shadow",
    )
    assert check.block_code is None
    assert seen == [60]
    # The logger patch has to wrap the call. Re-run with patches.
    seen.clear()

    def capture_log(message, level="INFO"):
        logs.append(str(message))

    def capture_row(collection, row):
        rows.append((collection, row))

    with patch("logger.log", side_effect=capture_log), patch(
        "services.observe_streams.mirror_record", side_effect=capture_row
    ):
        check = scored_trend_decision(
            trend_cfg=TREND["trend_filter"],
            hourly=_closed_hourly(),
            fetch_daily=_fetch(daily, seen),
            now=NOW,
            symbol=SYM,
            tenant_id="henry",
            config_raw={"risk": {"venue_quality": {"min_quote_volume_24h_usdt": 50_000}}},
            score_block=_score(-1, NOW, raw=-0.4),
            mode="shadow",
        )
    assert check.block_code is None
    lines = [line for line in logs if line.startswith("oracle_score_shadow ")]
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["tenant"] == "henry"
    assert fields["symbol"] == SYM
    assert fields["s"] == "-1"
    assert fields["raw"] == "-0.4"
    assert fields["age_s"] == "0"
    assert fields["sma_days_fixed"] == "3"
    assert fields["sma_days_score"] == "50"
    assert fields["max_ext_fixed"] == "20"
    assert fields["max_ext_score"] == "10"
    assert fields["would_pass_fixed"] == "true"
    assert fields["would_pass_score"] == "false"
    assert rows[0][0] == "obs_oracle_score_shadow"
    row = rows[0][1]
    assert row["mode"] == "shadow"
    assert row["tenant_id"] == "henry"
    assert row["symbol"] == SYM
    assert row["would_pass_fixed"] is True
    assert row["would_pass_score"] is False
    assert row["sma_days_score"] == 50
    assert row["applied"] is False


def test_enforce_uses_score_sma_when_fresh_and_fixed_values_when_stale_or_missing():
    daily = _daily([120.0, 90.0, 100.0, 100.0])
    fresh = scored_trend_decision(
        trend_cfg=TREND["trend_filter"],
        hourly=_closed_hourly(),
        fetch_daily=lambda _limit: daily,
        now=NOW,
        symbol=SYM,
        tenant_id="henry",
        config_raw={},
        score_block=_score(0.8, NOW),
        mode="enforce",
    )
    assert fresh.block_code is None
    stale = scored_trend_decision(
        trend_cfg=TREND["trend_filter"],
        hourly=_closed_hourly(),
        fetch_daily=lambda _limit: daily,
        now=NOW,
        symbol=SYM,
        tenant_id="henry",
        config_raw={},
        score_block=_score(0.8, NOW - 1801),
        mode="enforce",
    )
    assert stale.block_code == "trend_filter_below_sma"
    with patch("services.market_oracle.score_runtime.read_published_score", return_value=None):
        missing = scored_trend_decision(
            trend_cfg=TREND["trend_filter"],
            hourly=_closed_hourly(),
            fetch_daily=lambda _limit: daily,
            now=NOW,
            symbol=SYM,
            tenant_id="henry",
            config_raw={},
            mode="enforce",
        )
    assert missing.block_code == "trend_filter_below_sma"


def test_enforce_does_not_replace_the_pump_cap():
    hourly = _closed_hourly([100.0] * 70 + [125.0, 125.0])
    daily = _daily(_uptrend_daily(105))
    check = scored_trend_decision(
        trend_cfg=TREND["trend_filter"],
        hourly=hourly,
        fetch_daily=lambda _limit: daily,
        now=NOW,
        symbol=SYM,
        tenant_id="henry",
        config_raw={},
        score_block=_score(0.8, NOW),
        mode="enforce",
    )
    assert check.block_code == "trend_filter_pump_24h"


def test_off_buy_is_byte_identical_with_a_published_score():
    reads = {"n": 0}

    def reader():
        reads["n"] += 1
        return _score(-1, NOW)

    left = _Market(daily=_daily([120.0] * 40 + [100.0] * 9 + [105.0, 105.0]))
    right = _Market(daily=_daily([120.0] * 40 + [100.0] * 9 + [105.0, 105.0]))
    plain = _evaluate(_rm(TREND, left), _order())
    with patch("services.market_oracle.score_runtime.read_published_score", side_effect=reader):
        scored = _evaluate(_rm(TREND, right), _order())
    assert plain.approved and scored.approved
    assert plain.order.usdt_amount == pytest.approx(scored.order.usdt_amount)
    assert reads["n"] == 0
    assert [call for call in left.calls if call[1] == "1d"] == [(SYM, "1d", 13)]
    assert [call for call in right.calls if call[1] == "1d"] == [(SYM, "1d", 13)]


def test_henry_shadow_buy_matches_the_fixed_decision():
    logs: list[str] = []
    rows: list[tuple] = []
    market = _Market(daily=_daily([120.0] * 40 + [100.0] * 9 + [105.0, 105.0]))
    with tenant_context("henry"), patch(
        "services.market_oracle.score_runtime.read_published_score",
        return_value=_score(-1, NOW, raw=-0.4),
    ), patch("logger.log", side_effect=lambda message, level="INFO": logs.append(str(message))), patch(
        "services.observe_streams.mirror_record",
        side_effect=lambda collection, row: rows.append((collection, row)),
    ):
        decision = _evaluate(_rm(TREND, market), _order())
    assert decision.approved
    assert [call for call in market.calls if call[1] == "1d"] == [(SYM, "1d", 60)]
    lines = [line for line in logs if line.startswith("oracle_score_shadow ")]
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["would_pass_fixed"] == "true"
    assert fields["would_pass_score"] == "false"
    assert fields["sma_days_fixed"] == "3"
    assert fields["sma_days_score"] == "50"
    assert rows and rows[0][0] == "obs_oracle_score_shadow"


def test_enforce_buy_and_fallback_go_through_the_risk_manager():
    loosen = _daily([120.0, 90.0, 100.0, 100.0])
    blocked = _evaluate(_rm(TREND, _Market(daily=loosen)), _order())
    assert blocked.code == "trend_filter_below_sma"
    with patch("services.market_oracle.score_runtime.current_mode", return_value="enforce"), patch(
        "services.market_oracle.score_runtime.read_published_score",
        return_value=_score(0.8, NOW),
    ):
        opened = _evaluate(_rm(TREND, _Market(daily=loosen)), _order())
    assert opened.approved
    with patch("services.market_oracle.score_runtime.current_mode", return_value="enforce"), patch(
        "services.market_oracle.score_runtime.read_published_score",
        return_value=_score(0.8, NOW - 1801),
    ):
        stale = _evaluate(_rm(TREND, _Market(daily=loosen)), _order())
    assert stale.code == "trend_filter_below_sma"
    pumped = _Market(
        hourly=_hourly([100.0] * 70 + [125.0, 125.0]),
        daily=_daily(_uptrend_daily(105)),
    )
    with patch("services.market_oracle.score_runtime.current_mode", return_value="enforce"), patch(
        "services.market_oracle.score_runtime.read_published_score",
        return_value=_score(0.8, NOW),
    ):
        pump = _evaluate(_rm(TREND, pumped), _order())
    assert pump.code == "trend_filter_pump_24h"


def test_buy_path_survives_a_score_exception():
    def boom(**_kwargs):
        raise RuntimeError("score down")

    passing = _Market(daily=_daily([120.0] * 40 + [100.0] * 9 + [105.0, 105.0]))
    failing = _Market(daily=_daily([120.0, 90.0, 100.0, 100.0]))
    with patch("services.market_oracle.score_runtime.current_mode", return_value="shadow"), patch(
        "services.market_oracle.score_runtime.scored_trend_decision", side_effect=boom
    ):
        kept = _evaluate(_rm(TREND, passing), _order())
        rejected = _evaluate(_rm(TREND, failing), _order())
    assert kept.approved
    assert rejected.code == "trend_filter_below_sma"


# --- oracle loop ----------------------------------------------------------------------


class _Client:
    def fetch_features(self, breadth_top_n=40):
        return {
            "btc_ret_24h_pct": 0.2,
            "eth_ret_24h_pct": 0.1,
            "btc_ret_4h_pct": 0.0,
            "btc_ret_1h_pct": 0.0,
            "btc_trend_4h": 0.0,
            "breadth_pct_green": 0.5,
            "btc_funding_rate_pct": 0.01,
        }


def _oracle_cfg():
    from services.market_oracle.config import load_config

    cfg = load_config()
    cfg["dry_run"] = True
    cfg["bot_ingest_url"] = ""
    cfg["bot_ingest_token"] = ""
    return cfg


def _reset_oracle_state():
    from services.market_oracle import __main__ as oracle_main

    saved = copy.deepcopy(oracle_main._STATE)
    oracle_main._STATE.clear()
    oracle_main._STATE.update(
        {
            "last_snapshot": None,
            "last_push_at": 0.0,
            "last_error": "",
            "polls": 0,
            "pushes": 0,
            "prev_pushed": None,
        }
    )
    return oracle_main, saved


def test_run_once_publishes_a_score_and_survives_a_score_exception():
    from services.market_oracle.regime import StateHysteresis

    oracle_main, saved = _reset_oracle_state()
    try:
        snap = oracle_main.run_once(_oracle_cfg(), _Client(), StateHysteresis(2))
        assert "state" in snap
        assert "oracle_score" in snap
        assert -1.0 <= snap["oracle_score"]["smoothed"] <= 1.0
        assert "raw" in snap["oracle_score"]
        assert "components" in snap["oracle_score"]

        def boom(*_a, **_k):
            raise RuntimeError("score exploded")

        oracle_main._STATE["oracle_score_memory_loaded"] = False
        with patch("services.market_oracle.score_runtime.apply_poll_score", side_effect=boom):
            survived = oracle_main.run_once(_oracle_cfg(), _Client(), StateHysteresis(2))
        assert "state" in survived
        assert "oracle_score" not in survived
    finally:
        oracle_main._STATE.clear()
        oracle_main._STATE.update(saved)
