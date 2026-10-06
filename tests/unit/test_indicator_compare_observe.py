"""#636 observe-only indicator compare.

I2 ``test_disabled_or_empty_config_is_noop_money_path_unchanged``
I3 ``test_fixture_both_vs_dip_logs_diverge_row_no_order``
I4 ``test_diverge_does_not_change_accept_reject_size``
I5 ``test_logger_write_error_is_fail_open``

PYTEST_DB_SUFFIX=sIndCmp. Writes nothing into the checkout ``data/`` directory.
Coin names here are fixture data. The logger selects rows by characteristics.
"""

from __future__ import annotations

import ast
import copy
import json
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

os.environ.setdefault("PYTEST_DB_SUFFIX", "sIndCmp")

from core.actions import BUY, HOLD
from core.config import BotConfig
from core.models import MarketContext, SignalAnalysis
from core.tenant_context import current_tenant_context, tenant_context
from strategies.indicator_compare import (
    ROW_KEYS,
    VERDICT_BUY,
    VERDICT_HOLD,
    compare_log_path,
    in_compare_scope,
    indicator_compare_enabled,
    maybe_log_indicator_compare,
)
from strategies.technical_rsi_bb import TechnicalRSIStrategy
from tests.unit.test_long_mcap_venue_563 import _HEALTHY, _buy, _cfg, _eval_env

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "REV/USDT"
TENANT = "cmp_obs"
BAR_MS = 1_700_000_000_000

PARAMS = {
    "symbol": SYMBOL,
    "timeframe": "4h",
    "buy_regime": "both",
    "strategy_class": "technical_rsi_bb",
    "rsi_buy_low": 28,
    "rsi_buy_high": 48,
    "volume_multiplier": 1.2,
    "reversal_rsi_cross_low": 32,
    "reversal_rsi_cross_high": 38,
    "reversal_volume_multiplier": 1.3,
    "exchange": "gate",
    "market": "spot",
}


def _strategy_row(**overrides) -> dict:
    row = dict(PARAMS)
    row.update(overrides)
    return row


def _config(*, enabled: bool | None = True, strategies: list | None = None) -> dict:
    raw: dict = {
        "live": {"exchange": "gate", "dry_run": True},
        "max_open_positions": 36,
        "strategies": [dict(PARAMS)] if strategies is None else strategies,
    }
    if enabled is not None:
        raw["indicator_compare"] = {"enabled": enabled}
    return raw


def _market(*, price: float = 100.0, lower: float = 95.0, rsi: float = 40.0, last_rsi: float = 30.0) -> MarketContext:
    return MarketContext(
        symbol=SYMBOL,
        timeframe="4h",
        current_price=price,
        rsi=rsi,
        lower_bb=lower,
        vol_multiplier=1.5,
        has_position=False,
        open_positions=0,
        strategy_params=dict(PARAMS),
        sim_state={"last_rsi": last_rsi, "last_ampel": "🟡", "rsi_sell_tiers_done": {}},
        ohlcv_df=pd.DataFrame({"ts": [BAR_MS]}),
    )


def _coin() -> dict:
    return {"symbol": SYMBOL, "timeframe": "4h", "strategy_class": "technical_rsi_bb"}


def _enable_writes(monkeypatch, tmp_path: Path) -> Path:
    data = tmp_path / "cmp-data"
    data.mkdir()
    monkeypatch.setenv("INDICATOR_COMPARE_UNDER_TEST", "1")
    monkeypatch.setattr("data_manager.data_dir", lambda: str(data))
    return data


def _analyze(market: MarketContext | None = None):
    strategy = TechnicalRSIStrategy()
    market = market or _market()
    return strategy.analyze(_coin(), market), market


def _money_signal(analysis) -> dict:
    return {
        "action": analysis.action,
        "normalized": analysis.normalized_action,
        "confidence": analysis.confidence,
        "dca_usdt": float(getattr(analysis, "dca_usdt", 0) or 0),
        "shadow": analysis.shadow_action or "",
        "sources": list(analysis.sources),
    }


def _read_rows(tmp_path: Path) -> list[dict]:
    path = Path(compare_log_path(TENANT))
    assert path.is_relative_to(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _capture_log(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def _record(message, level="INFO"):
        seen.append((str(level), str(message)))

    monkeypatch.setattr("logger.log", _record)
    return seen


def test_code_default_false_and_staging_config_enables_without_fire():
    assert indicator_compare_enabled(None) is False
    assert indicator_compare_enabled({}) is False
    assert indicator_compare_enabled({"indicator_compare": {}}) is False
    assert indicator_compare_enabled({"indicator_compare": {"enabled": False}}) is False
    assert indicator_compare_enabled({"indicator_compare": {"enabled": "true"}}) is False
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert raw["indicator_compare"]["enabled"] is True
    assert raw["live"]["dry_run"] is True
    assert raw["shorts"]["allow_live"] is False
    assert "fire" not in raw["indicator_compare"]
    assert "fire_enabled" not in raw["indicator_compare"]


def test_scope_filters_by_characteristics_not_names():
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    matched = []
    for row in raw["strategies"]:
        market = MarketContext(
            symbol=row["symbol"],
            timeframe=row.get("timeframe") or "4h",
            current_price=1.0,
            strategy_params=row,
        )
        if in_compare_scope(
            coin=row,
            params=row,
            config=raw,
            market=market,
            strategy_name=row.get("strategy_class") or "technical_rsi_bb",
        ):
            matched.append((row["symbol"], row.get("timeframe")))
    assert set(matched) == {("ARIA/USDT", "4h"), ("RAVE/USDT", "4h"), ("HIGH/USDT", "4h")}

    synthetic = _config(strategies=[_strategy_row(symbol="SYN/USDT")])
    market = _market()
    market.symbol = "SYN/USDT"
    assert in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "4h"},
        params=synthetic["strategies"][0],
        config=synthetic,
        market=market,
        strategy_name="technical_rsi_bb",
    )
    dip = _config(strategies=[_strategy_row(symbol="SYN/USDT", buy_regime="dip")])
    assert not in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "4h"},
        params=dip["strategies"][0],
        config=dip,
        market=market,
        strategy_name="technical_rsi_bb",
    )
    hour = _config(strategies=[_strategy_row(symbol="SYN/USDT", timeframe="1h")])
    market.timeframe = "1h"
    assert not in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "1h"},
        params=hour["strategies"][0],
        config=hour,
        market=market,
        strategy_name="technical_rsi_bb",
    )
    market.timeframe = "4h"
    swap = _config(strategies=[_strategy_row(symbol="SYN/USDT", market="swap")])
    assert not in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "4h", "market": "swap"},
        params=swap["strategies"][0],
        config=swap,
        market=market,
        strategy_name="technical_rsi_bb",
    )
    other = _config(strategies=[_strategy_row(symbol="SYN/USDT", exchange="binance")])
    assert not in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "4h", "exchange": "binance"},
        params=other["strategies"][0],
        config=other,
        market=market,
        strategy_name="technical_rsi_bb",
    )
    grid = _config(
        strategies=[_strategy_row(symbol="SYN/USDT", strategy_class="grid")]
    )
    assert not in_compare_scope(
        coin={"symbol": "SYN/USDT", "timeframe": "4h", "strategy_class": "grid"},
        params=grid["strategies"][0],
        config=grid,
        market=market,
        strategy_name="grid",
    )
    module = (ROOT / "strategies" / "indicator_compare.py").read_text(encoding="utf-8")
    for banned in ("ARIA", "RAVE", "HIGH", "USDT"):
        assert banned not in module


def test_disabled_or_empty_config_is_noop_money_path_unchanged(tmp_path, monkeypatch):
    _enable_writes(monkeypatch, tmp_path)
    before, market = _analyze()
    assert before.action == "BUY"
    assert "reversal" in before.sources
    cases = [
        {},
        {"indicator_compare": {}},
        {"indicator_compare": {"enabled": False}, "strategies": [_strategy_row()]},
        _config(enabled=True, strategies=[]),
        _config(enabled=True, strategies=[_strategy_row(timeframe="1h")]),
        _config(enabled=True, strategies=[_strategy_row(buy_regime="dip")]),
    ]
    with patch("services.observability_store.append_jsonl") as append:
        for cfg in cases:
            assert maybe_log_indicator_compare(
                cfg,
                coin=_coin(),
                market=market,
                params=market.strategy_params,
                strategy_name="technical_rsi_bb",
            ) is None
        append.assert_not_called()
    after, _market_again = _analyze(market)
    assert _money_signal(after) == _money_signal(before)
    assert _read_rows(tmp_path) == []
    checkout = ROOT / "data"
    assert not (checkout / "default" / "indicator_compare.jsonl").exists()
    assert not (checkout / "tenant" / "indicator_compare.jsonl").exists()


def test_fixture_both_vs_dip_logs_diverge_row_no_order(tmp_path, monkeypatch):
    _enable_writes(monkeypatch, tmp_path)
    market = _market()
    analysis, _ = _analyze(market)
    assert analysis.action == "BUY"
    with tenant_context(TENANT, scope="paper"), patch(
        "services.order_service.OrderService", side_effect=AssertionError("order")
    ), patch(
        "services.trading_service.TradingService.execute_buy",
        side_effect=AssertionError("buy"),
    ):
        row = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert row is not None
    assert set(row) == set(ROW_KEYS)
    assert row["symbol"] == SYMBOL
    assert row["ist_read"] == VERDICT_BUY
    assert row["alt_read"] == VERDICT_HOLD
    assert row["diverge"] is True
    assert row["rsi"] == pytest.approx(40.0)
    assert row["last_rsi"] == pytest.approx(30.0)
    assert row["volume_factor"] == pytest.approx(1.5)
    assert row["dist_lower_bb"] == pytest.approx((100.0 - 95.0) / 95.0)
    expected_bar = datetime.fromtimestamp(BAR_MS / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert row["bar_time"] == expected_bar
    written = datetime.fromisoformat(row["written_at"].replace("Z", "+00:00"))
    assert written.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - written).total_seconds()) < 120
    assert row["written_at"] != row["bar_time"]
    assert row["rsi_buy_low"] == PARAMS["rsi_buy_low"]
    assert row["rsi_buy_high"] == PARAMS["rsi_buy_high"]
    assert row["volume_multiplier"] == PARAMS["volume_multiplier"]
    assert row["reversal_rsi_cross_low"] == float(PARAMS["reversal_rsi_cross_low"])
    assert row["reversal_rsi_cross_high"] == float(PARAMS["reversal_rsi_cross_high"])
    assert row["reversal_volume_multiplier"] == float(PARAMS["reversal_volume_multiplier"])
    for key in ("qty", "amount", "usdt_amount", "order", "size", "size_multiplier"):
        assert key not in row
    on_disk = _read_rows(tmp_path)
    assert on_disk == [row]
    assert analysis.action == "BUY"

    dip_market = _market(price=95.0, lower=95.0, rsi=40.0, last_rsi=40.0)
    dip_market.strategy_params = dict(PARAMS)
    with tenant_context(TENANT, scope="paper"):
        agreed = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=dip_market,
            params=dip_market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert agreed is not None
    assert agreed["ist_read"] == VERDICT_BUY
    assert agreed["alt_read"] == VERDICT_BUY
    assert agreed["diverge"] is False
    assert all(item["ist_read"] in (VERDICT_BUY, VERDICT_HOLD) for item in _read_rows(tmp_path))
    assert all(item["alt_read"] in (VERDICT_BUY, VERDICT_HOLD) for item in _read_rows(tmp_path))


def _risk_money(dec, order) -> dict:
    attached = getattr(dec, "order", None)
    return {
        "approved": dec.approved,
        "code": dec.code,
        "message": dec.message,
        "size_multiplier": dec.size_multiplier,
        "decision_usdt": None if attached is None else attached.usdt_amount,
        "decision_amount": None if attached is None else attached.amount,
        "request_usdt": order.usdt_amount,
        "request_amount": order.amount,
    }


def _risk_once(raw: dict):
    from risk.risk_manager import RiskManager

    cfg = BotConfig(copy.deepcopy(raw))
    rm = RiskManager(cfg)
    order = _buy(SYMBOL, usdt=200.0)
    with _eval_env(rm, metrics=_HEALTHY, mcap=6_000_000):
        dec = rm.evaluate(order, "4h", source="ta")
    return _risk_money(dec, order)


def test_diverge_does_not_change_accept_reject_size(tmp_path, monkeypatch):
    _enable_writes(monkeypatch, tmp_path)
    market = _market()
    with tenant_context(TENANT, scope="paper"):
        logged = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert logged is not None and logged["diverge"] is True

    off_signal, _ = _analyze(market)
    on_signal, _ = _analyze(market)
    assert off_signal.action == on_signal.action == "BUY"
    assert _money_signal(off_signal) == _money_signal(on_signal)

    poison = {
        "symbol": SYMBOL,
        "ist_read": "HOLD",
        "alt_read": "BUY",
        "diverge": True,
        "size_multiplier": 0,
        "marker": "POISON",
    }
    path = Path(compare_log_path(TENANT))
    path.write_text(json.dumps(poison) + "\n", encoding="utf-8")

    base = _cfg().raw
    off = _risk_once(base)
    on_raw = copy.deepcopy(base)
    on_raw["indicator_compare"] = {"enabled": True}
    on_raw["strategies"] = [_strategy_row()]
    on = _risk_once(on_raw)
    assert off == on
    blob = json.dumps(on)
    assert "POISON" not in blob
    assert "ist_read" not in blob
    assert "diverge" not in blob

    with tenant_context(TENANT, scope="paper"):
        engine_off = _run_engine(enabled=False)
        engine_on = _run_engine(enabled=True)
    assert _money_signal(engine_off) == _money_signal(engine_on)
    assert engine_on.normalized_action == BUY
    assert engine_on.action == "BUY"
    rows = _read_rows(tmp_path)
    assert any(row.get("diverge") is True and row.get("symbol") == SYMBOL for row in rows)


def test_logger_write_error_is_fail_open(tmp_path, monkeypatch):
    _enable_writes(monkeypatch, tmp_path)
    before, market = _analyze()
    assert before.action == "BUY"
    with tenant_context(TENANT, scope="paper"), patch(
        "services.observability_store.append_jsonl", side_effect=OSError("disk full")
    ):
        row = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert row is not None and row["diverge"] is True
    after, _ = _analyze(market)
    assert _money_signal(after) == _money_signal(before)
    with tenant_context(TENANT, scope="paper"):
        engine = _run_engine(enabled=True, write_error=True)
    assert engine.action == "BUY"
    assert engine.normalized_action == BUY
    assert float(getattr(engine, "dca_usdt", 0) or 0) == 0.0


def test_risk_and_buy_do_not_read_compare_results():
    forbidden_names = ("indicator_compare", "ist_read", "alt_read")
    for rel in (
        "risk/risk_manager.py",
        "strategies/technical_rsi_bb.py",
        "strategies/buy_decision_tape.py",
        "services/trading_service.py",
        "services/order_service.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for name in forbidden_names:
            assert name not in text, f"{rel} references {name}"
    for path in (ROOT / "risk").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in forbidden_names:
            assert name not in text, f"{path} references {name}"

    from strategies.decision_engine import DecisionEngine

    for fn in (DecisionEngine._merge_buy, DecisionEngine._apply_entry_sensor_buy):
        src = _function_source(fn)
        for name in forbidden_names:
            assert name not in src

    engine_src = (ROOT / "strategies" / "decision_engine.py").read_text(encoding="utf-8")
    assert "ist_read" not in engine_src
    assert "alt_read" not in engine_src
    calls = _discarded_calls(engine_src, "maybe_log_indicator_compare")
    assert len(calls) == 1
    keywords = {kw.arg for kw in calls[0].keywords}
    assert keywords == {"coin", "market", "params", "strategy_name"}
    assert "analysis" not in keywords

    def _poison(*_args, **_kwargs):
        return {
            "ist_read": VERDICT_HOLD,
            "alt_read": VERDICT_BUY,
            "diverge": True,
            "size_multiplier": 0.0,
            "marker": "POISON",
        }

    with patch(
        "strategies.indicator_compare.maybe_log_indicator_compare",
        side_effect=_poison,
    ):
        poisoned = _run_engine(enabled=True)
    plain = _run_engine(enabled=False)
    assert _money_signal(poisoned) == _money_signal(plain)
    assert poisoned.action == "BUY"
    assert "POISON" not in poisoned.rationale
    assert "ist_read" not in poisoned.rationale


def test_missing_tenant_context_does_not_write_default(tmp_path, monkeypatch):
    data = _enable_writes(monkeypatch, tmp_path)
    assert current_tenant_context() is None
    warnings = _capture_log(monkeypatch)
    market = _market()
    row = maybe_log_indicator_compare(
        _config(enabled=True),
        coin=_coin(),
        market=market,
        params=market.strategy_params,
        strategy_name="technical_rsi_bb",
    )
    assert row is not None and row["diverge"] is True
    assert list(data.rglob("indicator_compare.jsonl")) == []
    assert not (data / "default" / "indicator_compare.jsonl").exists()
    assert not (ROOT / "data" / "default" / "indicator_compare.jsonl").exists()
    assert any(level == "WARNING" and "tenant" in message.lower() for level, message in warnings)
    source = (ROOT / "strategies" / "indicator_compare.py").read_text(encoding="utf-8")
    assert "resolve_tenant_id" not in source


def test_missing_max_open_positions_skips_row(tmp_path, monkeypatch):
    data = _enable_writes(monkeypatch, tmp_path)
    warnings = _capture_log(monkeypatch)

    class _Missing:
        raw = {"live": {"dry_run": True}}

    monkeypatch.setattr("core.config.get_bot_config", lambda tenant_id=None: _Missing())
    market = _market()
    with tenant_context(TENANT, scope="paper"):
        skipped = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert skipped is None
    assert _read_rows(tmp_path) == []
    assert list(data.rglob("indicator_compare.jsonl")) == []
    assert not (data / "default" / "indicator_compare.jsonl").exists()
    assert any(
        level == "WARNING" and "max_open_positions" in message for level, message in warnings
    )
    source = (ROOT / "strategies" / "indicator_compare.py").read_text(encoding="utf-8")
    assert 'max_open_positions", 5' not in source
    assert "return 5" not in source

    class _One:
        raw = {"max_open_positions": 1}

    monkeypatch.setattr("core.config.get_bot_config", lambda tenant_id=None: _One())
    market.open_positions = 1
    with tenant_context(TENANT, scope="paper"):
        full = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert full is None
    assert _read_rows(tmp_path) == []

    market.open_positions = 0
    with tenant_context(TENANT, scope="paper"):
        opened = maybe_log_indicator_compare(
            _config(enabled=True),
            coin=_coin(),
            market=market,
            params=market.strategy_params,
            strategy_name="technical_rsi_bb",
        )
    assert opened is not None and opened["diverge"] is True
    assert len(_read_rows(tmp_path)) == 1


def _pinned_entry_action(market: MarketContext, params: dict, max_open: int) -> str:
    """Independent copy of the pre-refactor inline entry predicate."""
    rsi_buy_low = params.get("rsi_buy_low", 28)
    rsi_buy_high = params.get("rsi_buy_high", 48)
    volume_multiplier_min = params.get("volume_multiplier", 1.2)
    buy_regime = params.get("buy_regime", "dip")
    reversal_rsi_low = float(params.get("reversal_rsi_cross_low", 32))
    reversal_rsi_high = float(params.get("reversal_rsi_cross_high", 38))
    reversal_vol_min = float(params.get("reversal_volume_multiplier", 1.3))
    if not market.has_position and market.open_positions < max_open:
        dip_buy = (
            market.current_price <= market.lower_bb * 1.01
            and rsi_buy_low <= market.rsi <= rsi_buy_high
            and market.vol_multiplier >= volume_multiplier_min
        )
        last_rsi = float((market.sim_state or {}).get("last_rsi", market.rsi))
        reversal_buy = (
            last_rsi < reversal_rsi_low
            and market.rsi >= reversal_rsi_high
            and market.vol_multiplier >= reversal_vol_min
        )
        if buy_regime == "dip" and dip_buy:
            return "BUY"
        if buy_regime == "reversal" and reversal_buy:
            return "BUY"
        if buy_regime == "both" and (dip_buy or reversal_buy):
            return "BUY"
        return "HOLD"
    return "HOLD"


_BOUNDARY_CASES = (
    ("dip_price_on_band", 101.0, 40.0, 1.2, 40.0, 0, False, True),
    ("dip_price_above_band", 101.01, 40.0, 1.2, 40.0, 0, False, True),
    ("dip_rsi_at_low", 100.0, 28.0, 1.2, 40.0, 0, False, True),
    ("dip_rsi_below_low", 100.0, 27.999, 1.2, 40.0, 0, False, True),
    ("dip_rsi_at_high", 100.0, 48.0, 1.2, 40.0, 0, False, True),
    ("dip_rsi_above_high", 100.0, 48.001, 1.2, 40.0, 0, False, True),
    ("dip_vol_at_min", 100.0, 40.0, 1.2, 40.0, 0, False, True),
    ("dip_vol_below_min", 100.0, 40.0, 1.199, 40.0, 0, False, True),
    ("reversal_last_rsi_below", 110.0, 38.0, 1.3, 31.999, 0, False, True),
    ("reversal_last_rsi_equal_low", 110.0, 38.0, 1.3, 32.0, 0, False, True),
    ("reversal_rsi_at_high", 110.0, 38.0, 1.3, 31.0, 0, False, True),
    ("reversal_rsi_below_high", 110.0, 37.999, 1.3, 31.0, 0, False, True),
    ("reversal_vol_at_min", 110.0, 38.0, 1.3, 31.0, 0, False, True),
    ("reversal_vol_below_min", 110.0, 38.0, 1.299, 31.0, 0, False, True),
    ("both_signals", 100.0, 40.0, 1.5, 30.0, 0, False, True),
    ("neither", 110.0, 50.0, 1.0, 40.0, 0, False, True),
    ("defaults_only", 100.0, 28.0, 1.2, 40.0, 0, False, False),
    ("full_book", 100.0, 40.0, 1.5, 30.0, 10_000, False, True),
    ("has_position", 100.0, 40.0, 1.5, 30.0, 0, True, True),
)


@pytest.mark.parametrize("regime", ["dip", "reversal", "both"])
@pytest.mark.parametrize(
    ("name", "price", "rsi", "vol", "last_rsi", "open_positions", "has_position", "explicit_thresholds"),
    _BOUNDARY_CASES,
    ids=[case[0] for case in _BOUNDARY_CASES],
)
def test_entry_action_matches_pinned_pre_refactor_predicate(
    regime,
    name,
    price,
    rsi,
    vol,
    last_rsi,
    open_positions,
    has_position,
    explicit_thresholds,
):
    del name
    params = {
        "symbol": SYMBOL,
        "timeframe": "4h",
        "buy_regime": regime,
        "strategy_class": "technical_rsi_bb",
        "exchange": "gate",
        "market": "spot",
    }
    if explicit_thresholds:
        params.update(
            {
                "rsi_buy_low": 28,
                "rsi_buy_high": 48,
                "volume_multiplier": 1.2,
                "reversal_rsi_cross_low": 32,
                "reversal_rsi_cross_high": 38,
                "reversal_volume_multiplier": 1.3,
            }
        )
    market = MarketContext(
        symbol=SYMBOL,
        timeframe="4h",
        current_price=price,
        rsi=rsi,
        lower_bb=100.0,
        vol_multiplier=vol,
        has_position=has_position,
        open_positions=open_positions,
        average_entry=0.0,
        strategy_params=params,
        sim_state={"last_rsi": last_rsi, "last_ampel": "🟡", "rsi_sell_tiers_done": {}},
    )
    from core.config import get_bot_config

    expected = _pinned_entry_action(market, params, get_bot_config().max_open_positions)
    analysis = TechnicalRSIStrategy().analyze(_coin(), market)
    assert analysis.action == expected


def _function_source(fn) -> str:
    import inspect

    return inspect.getsource(fn)


def _discarded_calls(source: str, func_name: str) -> list[ast.Call]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != func_name:
            continue
        assert isinstance(parents[node], ast.Expr)
        found.append(node)
    return found


def _technical() -> SignalAnalysis:
    return SignalAnalysis(
        action="BUY",
        symbol=SYMBOL,
        timeframe="4h",
        rsi=40.0,
        lower_bb=95.0,
        vol_multiplier=1.5,
        ampel_emoji="",
        ampel_text="",
        sources=["technical", "reversal"],
        normalized_action=BUY,
    )


def _engine(enabled: bool):
    from strategies.decision_engine import DecisionEngine

    raw = {
        "regime_detector": {"enabled": False},
        "strategy_allocator": {"enabled": False},
        "volatile_altcoin": {"mode": "live"},
        "sell_rotation": {"mode": "off"},
        "max_open_positions": 36,
        "strategies": [_strategy_row()],
        "risk": {"fail_closed_guards": "log", "position_locks": {"enabled": False}},
    }
    if enabled:
        raw["indicator_compare"] = {"enabled": True}
    engine = DecisionEngine()
    engine.config = BotConfig(raw)
    return engine


@contextmanager
def _engine_stack(engine, *, write_error: bool = False):
    strategy = MagicMock()
    strategy.name = "technical_rsi_bb"
    strategy.analyze.return_value = _technical()
    with ExitStack() as stack:
        stack.enter_context(
            patch("strategies.decision_engine.get_strategy", return_value=strategy)
        )
        stack.enter_context(
            patch(
                "strategies.decision_engine.get_position",
                return_value={"amount": 0, "average_entry": 0, "strategy_tier": None},
            )
        )
        stack.enter_context(patch.object(engine, "_sync_watch_15m_state"))
        stack.enter_context(
            patch.object(
                engine,
                "_merge_buy",
                return_value=(BUY, ["technical", "reversal"], 70.0),
            )
        )
        stack.enter_context(
            patch.object(
                engine,
                "_apply_entry_sensor_buy",
                side_effect=lambda n, s, c, *a, **k: (n, s, c, "", ""),
            )
        )
        stack.enter_context(
            patch(
                "core.coin_eligibility.passes_coin_filters",
                return_value=(True, ""),
            )
        )
        stack.enter_context(
            patch("strategies.decision_engine.policy_shadow_active", return_value=False)
        )
        stack.enter_context(
            patch("strategies.decision_engine.read_oracle_state", return_value=None)
        )
        stack.enter_context(
            patch(
                "strategies.decision_engine.compute_volume_rel_window",
                return_value=(None, None),
            )
        )
        if write_error:
            stack.enter_context(
                patch(
                    "services.observability_store.append_jsonl",
                    side_effect=OSError("disk full"),
                )
            )
        yield


def _run_engine(*, enabled: bool, write_error: bool = False):
    engine = _engine(enabled=enabled)
    market = _market()
    coin = _coin()
    coin["strategy_params"] = dict(PARAMS)
    with _engine_stack(engine, write_error=write_error):
        return engine.evaluate_with_market(coin, market, x_signals=None, cmc_signals=None, lc_signals=None)
