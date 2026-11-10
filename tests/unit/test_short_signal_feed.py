"""D-EXH Rev 1: raw exhaustion features. No threshold, no signal, no order.

Draft id D-EXH is not a GitHub issue. Open definitions are pending owner freeze
(specs/short-exhaustion-v220/04-requirements.md).
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.short_signal_feed import dataset_sha256, load_jsonl, measure_exhaustion
from services.short_signal_feed.schema import (
    HISTORICAL_CLIMAX_NO_GO,
    HOLDOUT_START,
    RSI_PERIOD,
    SCHEMA_ID,
    SCHEMA_REVISION,
    TRIAL_ID,
)
from services.short_signal_feed.trace import tape_row
from strategies.buy_decision_tape import ROW_KEYS
from tools.hypothesis_protocol import SLIPPAGE_PCT_PER_SIDE, TAKER_FEE_PCT_PER_SIDE

COMMIT = "a" * 40
PACKAGE = Path("services/short_signal_feed")

FORBIDDEN_KEYS = {
    "accepted",
    "divergence",
    "failed",
    "grace_hours",
    "lookback",
    "min_rsi",
    "min_return",
    "passed",
    "rejected",
    "vol_mult_min",
}


def _paper(enabled=True, *, mode="paper", allow_live=False, climax=None):
    shorts = {"allow_live": allow_live}
    if enabled is not None:
        shorts["exhaustion_v220"] = {"enabled": enabled}
    if climax is not None:
        shorts["climax_fade"] = {"enabled": climax}
    return {"execution": {"mode": mode}, "shorts": shorts}


def _signal(symbol, as_of, **extra):
    row = {
        "schema_id": SCHEMA_ID,
        "schema_revision": SCHEMA_REVISION,
        "source_commit": COMMIT,
        "venue": "fixture-venue",
        "market_type": "perpetual",
        "symbol": symbol,
        "as_of": as_of,
        "timeframe": "4h",
    }
    row.update(extra)
    return row


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bars(symbol, last_open: datetime, closes, volumes):
    start = last_open - timedelta(hours=4 * (len(closes) - 1))
    rows = []
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        rows.append(
            {
                "symbol": symbol,
                "timeframe": "4h",
                "open_time": _stamp(start + timedelta(hours=4 * index)),
                "close": close,
                "volume": volume,
            }
        )
    return rows


def _walk_keys(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)


def _by_symbol(result):
    return {row["symbol"]: row for row in result["observations"]}


def test_ac_exh_1_source_freshness_dedup(tmp_path):
    """Dedup, bad inputs, and age without a grace cutoff."""
    fresh_as_of = "2026-10-02T04:00:00Z"
    old_as_of = "2026-09-30T04:00:00Z"
    now = "2026-10-02T04:30:00Z"
    fresh = _signal("AAA/USDT", fresh_as_of)
    old = _signal("BBB/USDT", old_as_of)
    future = _signal("CCC/USDT", "2026-10-03T00:00:00Z")
    holdout = _signal("DDD/USDT", "2026-11-05T23:00:00Z")
    mismatch = _signal("EEE/USDT", fresh_as_of, schema_id="other-schema")
    path = tmp_path / "signals.jsonl"
    lines = [
        json.dumps(fresh),
        json.dumps(old),
        json.dumps(fresh),
        "{not-json",
        json.dumps(mismatch),
        json.dumps(future),
        json.dumps(holdout),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = load_jsonl(path)
    result = measure_exhaustion(
        config=_paper(),
        signals=loaded,
        bars=[],
        funding=[],
        now=now,
        tenant="tenant-a",
    )

    assert result["reason"] == "measured"
    assert result["trading_effect"] == "none"
    assert result["order_intents"] == []
    assert result["venue_calls"] == 0
    assert result["duplicate_dropped"] == 1
    assert HOLDOUT_START.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") == (
        "2026-11-05T23:00:00Z"
    )

    rows = _by_symbol(result)
    assert rows["AAA/USDT"]["status"] == "observed"
    assert rows["BBB/USDT"]["status"] == "observed"
    assert len(rows["AAA/USDT"]["signal_key"]) == 64
    assert rows["AAA/USDT"]["grace_applied"] is False
    assert rows["BBB/USDT"]["grace_applied"] is False
    assert rows["AAA/USDT"]["features"]["age"]["seconds"] == "1800"
    assert rows["BBB/USDT"]["features"]["age"]["seconds"] == "174600"
    assert rows["AAA/USDT"]["decision"] is None
    assert rows["BBB/USDT"]["order_intent"] is None

    assert rows["CCC/USDT"]["status"] == "not_measurable"
    assert rows["CCC/USDT"]["status_reason"] == "future_as_of"
    assert rows["CCC/USDT"]["features"] is None

    assert rows["DDD/USDT"]["status"] == "not_measurable"
    assert rows["DDD/USDT"]["status_reason"] == "holdout_excluded"
    assert rows["DDD/USDT"]["features"] is None

    assert rows["EEE/USDT"]["status"] == "not_measurable"
    assert rows["EEE/USDT"]["status_reason"] == "schema_mismatch"
    assert rows["EEE/USDT"]["features"] is None

    malformed = [row for row in result["observations"] if row["symbol"] is None]
    assert len(malformed) == 1
    assert malformed[0]["status"] == "not_measurable"
    assert malformed[0]["status_reason"] == "malformed"
    assert malformed[0]["detail"] == "json"
    assert malformed[0]["features"] is None

    symbols = [row["symbol"] for row in result["observations"]]
    assert symbols.index("BBB/USDT") > symbols.index("AAA/USDT")
    assert symbols.count("AAA/USDT") == 1


def test_ac_exh_2_raw_features_no_threshold():
    """Funding, RSI(14), and volume ratios are logged. Nothing is a gate."""
    assert RSI_PERIOD == 14
    last_open = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
    as_of = "2026-10-01T04:00:00Z"
    rising = [str(100 + index) for index in range(15)]
    falling = [str(200 - index) for index in range(15)]
    volumes = ["10"] * 14 + ["100"]
    short_closes = ["50"] * 10
    bad_closes = ["10"] * 14 + [1.25]

    bars = []
    bars += _bars("HIGH/USDT", last_open, rising, volumes)
    bars += _bars("LOW/USDT", last_open, falling, ["5"] * 15)
    bars += _bars("SHORT/USDT", last_open, short_closes, ["3"] * 10)
    bars += _bars("BAD/USDT", last_open, bad_closes, ["4"] * 15)
    bars += _bars("OTHER/USDT", last_open, rising, volumes)

    funding = [
        {
            "symbol": "HIGH/USDT",
            "settled_at": "2026-09-30T00:00:00Z",
            "rate": "0.00010000",
            "provenance": "observed_settlement",
        },
        {
            "symbol": "HIGH/USDT",
            "settled_at": "2026-10-01T00:00:00Z",
            "rate": "0.00020000",
            "interval_hours": "4",
            "provenance": "observed_settlement",
            "quote": "USDT",
        },
        {
            "symbol": "HIGH/USDT",
            "settled_at": "2026-10-01T08:00:00Z",
            "rate": "0.09000000",
            "interval_hours": "8",
            "provenance": "observed_settlement",
        },
    ]
    signals = [
        _signal("HIGH/USDT", as_of),
        _signal("LOW/USDT", as_of),
        _signal("SHORT/USDT", as_of),
        _signal("BAD/USDT", as_of),
        _signal("NONE/USDT", as_of),
        _signal(
            "TOK/USDT",
            as_of,
            instrument_kind="tokenized_equity",
            listed_perp=None,
            calendar_known=None,
            depth_known=None,
        ),
    ]
    result = measure_exhaustion(
        config=_paper(climax=True),
        signals=signals,
        bars=bars,
        funding=funding,
        now="2026-10-01T05:00:00Z",
        tenant="tenant-a",
    )
    rows = _by_symbol(result)
    high = rows["HIGH/USDT"]
    low = rows["LOW/USDT"]
    assert high["status"] == "observed"
    assert low["status"] == "observed"
    assert high["decision"] is None and low["decision"] is None
    assert high["order_intent"] is None and low["order_intent"] is None
    assert high["thresholds_applied"] is False
    assert high["features"]["rsi"]["period"] == 14
    assert "timeperiod=14" in high["features"]["rsi"]["period_source"]
    assert "MarketService._bars_to_dataframe" in high["features"]["rsi"]["period_source"]
    assert high["features"]["rsi"]["threshold"] is None
    assert high["features"]["rsi"]["value"] == "100.0000000000"
    assert low["features"]["rsi"]["value"] == "0.0000000000"
    assert high["features"]["rsi"]["reason"] is None

    history = high["features"]["funding"]["history"]
    assert [item["rate"] for item in history] == ["0.00010000", "0.00020000"]
    assert high["features"]["funding"]["excluded_after_as_of"] == 1
    assert high["features"]["funding"]["last"]["rate"] == "0.00020000"
    assert high["features"]["funding"]["last"]["interval_hours"] == "4"
    assert high["features"]["funding"]["last"]["interval_known"] is True
    assert high["features"]["funding"]["last"]["net_cost_usdt"] is None
    assert history[0]["interval_known"] is False
    assert history[0]["interval_hours"] is None
    assert "0.09000000" not in json.dumps(history)

    ratios = [item["ratio"] for item in high["features"]["volume"]["consecutive_ratios"]]
    assert ratios == ["1"] * 13 + ["10"]
    assert all(item["reason"] is None for item in high["features"]["volume"]["consecutive_ratios"])
    assert len(high["features"]["volume"]["bars"]) == 15

    short = rows["SHORT/USDT"]
    assert short["status"] == "observed"
    assert short["features"]["rsi"]["value"] is None
    assert short["features"]["rsi"]["reason"] == "insufficient_closes"
    assert short["features"]["rsi"]["period"] == 14

    missing = rows["NONE/USDT"]
    assert missing["status"] == "observed"
    assert missing["features"]["funding"]["history"] == []
    assert missing["features"]["funding"]["last"] is None
    assert missing["features"]["funding"]["reason"] == "no_settlement_at_or_before_as_of"
    assert missing["features"]["funding"]["last"] != 0

    bad = rows["BAD/USDT"]
    assert bad["features"]["rsi"]["value"] is None
    assert bad["features"]["rsi"]["reason"] == "non_decimal_close"

    found: set[str] = set()
    _walk_keys(result["observations"], found)
    assert FORBIDDEN_KEYS.isdisjoint(found)

    text = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
    for token in ("vol_mult_4_20", "climax_entry_signal", "is_excluded_symbol", "grace_hours"):
        assert token not in text


def test_ac_exh_3_default_off_and_live_denied():
    """The new path stays off, and climax_fade cannot switch it on."""
    signals = [_signal("AAA/USDT", "2026-10-01T04:00:00Z")]
    kwargs = {
        "signals": signals,
        "bars": [],
        "funding": [],
        "now": "2026-10-01T05:00:00Z",
        "tenant": "tenant-a",
    }

    off_cases = [
        None,
        {},
        {"shorts": {}},
        _paper(enabled=False),
        _paper(enabled=None),
        {"execution": {"mode": "paper"}, "shorts": {"exhaustion_v220": {"enabled": "true"}}},
        {"execution": {"mode": "paper"}, "shorts": {"exhaustion_v220": {"enabled": 1}}},
        _paper(enabled=False, climax=True),
        {"execution": {"mode": "live"}, "shorts": {"climax_fade": {"enabled": True}}},
    ]
    for config in off_cases:
        result = measure_exhaustion(config=config, **kwargs)
        assert result["reason"] == "feature_off"
        assert result["observations"] == []
        assert result["log_lines"] == []
        assert result["order_intents"] == []
        assert result["venue_calls"] == 0
        assert result["tape_rows"] == []
        assert result["trading_effect"] == "none"

    live_cases = [
        _paper(mode="live"),
        _paper(mode="mainnet"),
        _paper(mode="real"),
        _paper(allow_live=True),
    ]
    for config in live_cases:
        result = measure_exhaustion(config=config, **kwargs)
        assert result["reason"] == "live_denied"
        assert result["observations"] == []
        assert result["order_intents"] == []
        assert result["venue_calls"] == 0

    for mode in (None, "shadow", "live-paper"):
        config = {"shorts": {"exhaustion_v220": {"enabled": True}, "allow_live": False}}
        if mode is not None:
            config["execution"] = {"mode": mode}
        result = measure_exhaustion(config=config, **kwargs)
        assert result["reason"] == "mode_not_sandbox"
        assert result["order_intents"] == []
        assert result["observations"] == []

    enabled = measure_exhaustion(config=_paper(climax=False), **kwargs)
    enabled_climax = measure_exhaustion(config=_paper(climax=True), **kwargs)
    assert enabled["reason"] == "measured"
    assert enabled_climax["reason"] == "measured"
    assert [row["signal_key"] for row in enabled["observations"]] == [
        row["signal_key"] for row in enabled_climax["observations"]
    ]
    assert enabled["order_intents"] == []
    assert enabled["venue_calls"] == 0
    assert enabled["observations"][0]["decision"] is None

    testnet = measure_exhaustion(config=_paper(mode="futures_testnet"), **kwargs)
    assert testnet["reason"] == "measured"
    assert testnet["order_intents"] == []
    assert testnet["venue_calls"] == 0

    modules = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
    for name in modules:
        assert not name.startswith(
            (
                "ccxt",
                "execution",
                "risk",
                "services.trading_service",
                "strategies.decision_engine",
            )
        )
        assert "get_bot_config" not in name


def test_ac_exh_4_replay_costs_no_lookahead():
    """No backtest result. Inputs are hashed first. Later prints are not features."""
    as_of = "2026-10-01T04:00:00Z"
    signals = [_signal("AAA/USDT", as_of)]
    bars = _bars(
        "AAA/USDT",
        datetime(2026, 10, 1, 0, tzinfo=timezone.utc),
        ["100"] * 15,
        ["10"] * 15,
    )
    funding = [
        {
            "symbol": "AAA/USDT",
            "settled_at": "2026-10-01T00:00:00Z",
            "rate": "0.00020000",
            "provenance": "observed_settlement",
        },
        {
            "symbol": "AAA/USDT",
            "settled_at": "2026-10-01T08:00:00Z",
            "rate": "0.09000000",
            "provenance": "observed_settlement",
        },
    ]
    now = "2026-10-01T06:00:00Z"
    first = measure_exhaustion(
        config=_paper(),
        signals=signals,
        bars=bars,
        funding=funding,
        now=now,
        tenant="tenant-a",
    )
    second = measure_exhaustion(
        config=_paper(),
        signals=signals,
        bars=bars,
        funding=funding,
        now=now,
        tenant="tenant-a",
    )
    expected_hash = dataset_sha256(signals, bars, funding, now)
    assert first["report"]["dataset_sha256"] == expected_hash
    assert second["report"]["dataset_sha256"] == expected_hash
    assert first["report"]["pre_feature_hash"] is True

    changed = json.loads(json.dumps(funding))
    changed[1]["rate"] = "0.50000000"
    changed_hash = dataset_sha256(signals, bars, changed, now)
    assert changed_hash != expected_hash
    changed_run = measure_exhaustion(
        config=_paper(),
        signals=signals,
        bars=bars,
        funding=changed,
        now=now,
        tenant="tenant-a",
    )
    assert changed_run["observations"][0]["features"]["funding"]["history"] == (
        first["observations"][0]["features"]["funding"]["history"]
    )

    report = first["report"]
    assert report["trial_id"] == TRIAL_ID
    assert report["status"] == "insufficient_data"
    assert report["pending_owner_freeze"] is True
    assert report["open_definitions"] == {
        "thresholds": "UNCLEAR",
        "lookback": "UNCLEAR",
        "bearish_volume_divergence": "UNCLEAR",
        "freshness_grace": "undefined",
        "freeze": "pending owner freeze",
    }
    assert report["profitability_claim"] is False
    assert report["profit_factor"] is None
    assert report["net_expectancy"] is None
    assert report["max_drawdown"] is None
    assert report["mfe"] is None
    assert report["mae"] is None
    assert report["n_orders"] == 0
    assert report["uncertainty"] == "not_estimated"
    assert report["holdout"] == "not_accessed"
    assert report["historical_climax_nogo"] == HISTORICAL_CLIMAX_NO_GO
    assert report["historical_climax_nogo"]["profit_factor"] == "0.9160"
    assert report["historical_climax_nogo"]["net_usdt"] == "-3500.42"
    assert report["historical_climax_nogo"]["n"] == 1016
    assert report["historical_climax_nogo"]["kind"] == "spot_proxy"
    assert report["historical_climax_nogo"]["verdict"] == "NO-GO"
    assert report["historical_climax_nogo"]["recomputed"] is False
    assert report["cost_protocol"]["applied_to_pnl"] is False
    assert report["cost_protocol"]["taker_fee_pct_per_side"] == TAKER_FEE_PCT_PER_SIDE
    assert report["cost_protocol"]["slippage_pct_per_side"] == dict(SLIPPAGE_PCT_PER_SIDE)
    assert report["cost_protocol"]["spread"] == "unknown"
    assert report["cost_protocol"]["funding_net_cost"] == "unknown"
    for name in ("old_climax", "unconditional_top_gainer_fade", "no_trade"):
        assert report["baselines"][name]["status"] == "not_run"
    assert report["trial_register"]["scenario_trials_py"] == "not_modified"
    assert report["trial_register"]["status"] == "planned_not_run"
    assert "D-EXH" not in Path("backtest/harness/trials.py").read_text(encoding="utf-8")

    holdout = measure_exhaustion(
        config=_paper(),
        signals=[_signal("AAA/USDT", "2026-11-05T23:00:00Z")],
        bars=bars,
        funding=funding,
        now="2026-12-01T00:00:00Z",
        tenant="tenant-a",
    )
    assert holdout["observations"][0]["status_reason"] == "holdout_excluded"
    assert holdout["observations"][0]["features"] is None
    assert holdout["report"]["status"] == "insufficient_data"
    assert holdout["report"]["profit_factor"] is None


def test_ac_exh_5_trace_reuses_existing_storage(monkeypatch):
    """Tape shape is reused. Nothing is written. Unknown equity fields are not a ban."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("feature log must not append a second store")

    monkeypatch.setattr("services.observability_store.append_jsonl", _boom)
    as_of = "2026-10-01T04:00:00Z"
    signals = [
        _signal("AAA/USDT", as_of),
        _signal(
            "TOK/USDT",
            as_of,
            instrument_kind="tokenized_equity",
            listed_perp=None,
            calendar_known=None,
            depth_known=None,
        ),
    ]
    result = measure_exhaustion(
        config=_paper(),
        signals=signals,
        bars=[],
        funding=[],
        now="2026-10-01T06:00:00Z",
        tenant="tenant-a",
    )
    assert len(result["tape_rows"]) == 2
    assert {row["symbol"] for row in result["tape_rows"]} == {"AAA/USDT", "TOK/USDT"}
    for row in result["tape_rows"]:
        assert tuple(row) == ROW_KEYS
        assert row["filter_codes"] == []
        assert row["outcome"] == "observed"
        assert row["tenant"] == "tenant-a"
        assert row["signal"]["name"] == TRIAL_ID
        assert row["signal"]["source"] == row["correlation_id"]
        assert row["book"]["order_intent"] is None
        assert row["book"]["decision"] is None
        assert row["book"]["thresholds_applied"] is False
        assert row["book"]["grace_applied"] is False
        assert row["macro_stress"] is None
        assert row["eligibility"]["entry_snapshot_ref"] is None

    token = next(row for row in result["observations"] if row["symbol"] == "TOK/USDT")
    assert token["status"] == "observed"
    assert token["inputs"]["instrument_kind"] == "tokenized_equity"
    assert token["inputs"]["listed_perp"] is None
    assert token["inputs"]["calendar_known"] is None
    assert token["inputs"]["depth_known"] is None
    assert token["decision"] is None

    projected = tape_row(
        result["observations"][0],
        tenant="tenant-a",
        entry_snapshot_ref="fill-1",
        entry_snapshot={"rsi": "SENTINEL_PRICE", "not_a_snapshot_key": "SECRET_BODY"},
    )
    assert projected["eligibility"]["entry_snapshot_ref"] == "fill-1"
    assert projected["eligibility"]["entry_snapshot_rejected_keys"] == ["not_a_snapshot_key"]
    serialized = json.dumps(projected)
    assert "SENTINEL_PRICE" not in serialized
    assert "SECRET_BODY" not in serialized
    assert "entry_snapshot" not in projected["eligibility"]

    package_text = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
    assert "fbr" not in package_text.lower()
    assert "#694" not in package_text
    assert result["order_intents"] == []
    assert result["log_lines"]
    assert "gate=none" in result["log_lines"][0]
    assert "order_intent=none" in result["log_lines"][0]


@pytest.mark.parametrize("enabled", [True, False])
def test_dataset_hash_does_not_require_a_measurement_pass(enabled):
    """The pre-run hash exists even when the flag is off. Not an AC gate."""
    signals = [_signal("AAA/USDT", "2026-10-01T04:00:00Z")]
    result = measure_exhaustion(
        config=_paper(enabled=enabled),
        signals=signals,
        bars=[],
        funding=[],
        now="2026-10-01T05:00:00Z",
    )
    assert result["report"]["dataset_sha256"] == dataset_sha256(
        signals, [], [], "2026-10-01T05:00:00Z"
    )
    assert result["report"]["profit_factor"] is None
