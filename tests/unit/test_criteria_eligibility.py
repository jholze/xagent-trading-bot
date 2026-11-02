"""Criteria eligibility in the #641 guard (#643 extension, kauf_setup report).

Rules ``rule_switches.elig_liq7`` / ``elig_collapse90`` / ``elig_chase48`` /
``elig_mcap``; per-tenant mode off | shadow | enforce, default off.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_tenant_rule_switches import NOW, SYM, D, H, _cfg, _evaluate, _order

from data_manager import get_config
from risk import rule_switches as rs
from risk.risk_manager import RiskManager
from services import venue_quality as vq
from strategies import buy_decision_tape as tape

ROOT = Path(__file__).resolve().parents[2]
DAY_OPEN = (NOW // D) * D  # today's forming daily bar
HOUR_OPEN = (NOW // H) * H  # forming hourly bar


# --- fixtures -------------------------------------------------------------------------


def daily(n=95, *, close=1.0, high=None, qv=1_000_000.0, closes=None, qvs=None, forming=True):
    """*n* closed daily bars ending yesterday (+ today's forming bar). Volume is
    base units, so quote volume = volume * close = *qv*."""
    out = []
    for i in range(n):
        c = closes[i] if closes is not None else close
        q = qvs[i] if qvs is not None else qv
        hi = high[i] if isinstance(high, list) else (high if high is not None else c * 1.01)
        t = DAY_OPEN - (n - i) * D
        out.append([t * 1000, c, max(hi, c), c * 0.99, c, q / c])
    if forming:
        c = out[-1][4]
        out.append([DAY_OPEN * 1000, c, c, c, c, out[-1][5]])
    return out


def hourly(n=72, *, low=1.0, close=1.0, lows=None, forming=True):
    out = []
    for i in range(n):
        lo = lows[i] if lows is not None else low
        t = HOUR_OPEN - (n - i) * H
        out.append([t * 1000, close, max(close, lo) * 1.001, lo, close, 1000.0])
    if forming:
        out.append([HOUR_OPEN * 1000, close, close, close, close, 1000.0])
    return out


class Market:
    def __init__(self, d=None, h=None):
        self.d = d if d is not None else daily()
        self.h = h if h is not None else hourly()
        self.calls: list[tuple] = []

    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        self.calls.append((symbol, timeframe, limit))
        return self.h if timeframe == "1h" else self.d

    def fetch_indicators(self, *a, **k):
        return {"atr_pct": 3.0, "rsi": 45.0}


def sw(**modes) -> dict:
    return {rule: {"mode": mode} for rule, mode in modes.items()}


ALL_ENFORCE = sw(elig_liq7="enforce", elig_collapse90="enforce", elig_chase48="enforce", elig_mcap="enforce")
# Intended henry/default config (documented in the PR, not shipped).
INTENDED = sw(elig_liq7="enforce", elig_collapse90="enforce", elig_chase48="shadow", elig_mcap="shadow")


def rm(switches=None, market=None) -> RiskManager:
    return RiskManager(_cfg(switches), market_service=market or Market())


ILLIQUID = {"d": daily(qv=50_000.0)}
COLLAPSED = {"d": daily(high=[1.01] * 40 + [10.0] * 10 + [1.01] * 45)}  # close = 10 % of 90d high
CHASED = {"h": hourly(low=1.0)}  # order at 1.30 = +30 % over the 48h low

CASES = [
    ("elig_liq7", ILLIQUID, 1.0),
    ("elig_collapse90", COLLAPSED, 1.0),
    ("elig_chase48", CHASED, 1.30),
]


def _tape_rows(monkeypatch, tmp_path):
    # Same isolation as test_buy_decision_tape_628 (isolate_bot_logs tmp_path).
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    monkeypatch.setenv(tape._UNDER_TEST_ENV, "1")
    return tmp_path / "logs" / tape.TAPE_FILENAME


# --- defaults and modes ---------------------------------------------------------------


def test_defaults_all_off():
    raw = _cfg().raw
    for rule in rs.ELIG_RULES:
        assert rs.rule_mode(raw, rule) == rs.MODE_OFF
    assert not rs.any_elig_rule_on(raw)
    assert rs.rule_config(raw, "elig_liq7")["min_median_quote_volume_usdt"] == 100_000
    assert rs.rule_config(raw, "elig_collapse90")["min_close_to_high"] == 0.20
    assert rs.rule_config(raw, "elig_chase48")["max_above_48h_low_pct"] == 15


def test_shipped_and_tenant_configs_enable_nothing():
    assert not rs.any_elig_rule_on(get_config())
    # Tenant bodies live in Mongo tenant_configs; the repo ships only config.json.
    raw = json.loads((ROOT / "config.json").read_text())
    assert "rule_switches" not in raw or not rs.any_elig_rule_on(raw)


@pytest.mark.parametrize(
    "value, expected",
    [(None, "off"), ("off", "off"), ("shadow", "shadow"), ("enforce", "enforce"), (" Shadow ", "shadow"),
     (True, "enforce"), (False, "off"), ("garbage", "enforce"), (3, "enforce")],
)
def test_rule_mode_parsing(value, expected):
    cfg = {"rule_switches": {"elig_liq7": {"mode": value}}}
    assert rs.rule_mode(cfg, "elig_liq7") == expected


def test_all_off_no_fetch_no_change():
    m = Market(**ILLIQUID)
    base = _evaluate(rm(None, m), _order(price=1.0))
    off = _evaluate(rm(sw(elig_liq7="off", elig_collapse90="off", elig_chase48="off"), Market(**ILLIQUID)),
                    _order(price=1.0))
    assert base.approved and off.approved
    assert base.order.usdt_amount == pytest.approx(off.order.usdt_amount)
    assert m.calls == []


@pytest.mark.parametrize("rule, market, price", CASES)
def test_each_rule_off_shadow_enforce(rule, market, price):
    off = _evaluate(rm(sw(**{rule: "off"}), Market(**market)), _order(price=price))
    shadow = _evaluate(rm(sw(**{rule: "shadow"}), Market(**market)), _order(price=price))
    enforce = _evaluate(rm(sw(**{rule: "enforce"}), Market(**market)), _order(price=price))
    assert off.approved and shadow.approved
    assert off.order.usdt_amount == pytest.approx(shadow.order.usdt_amount)
    assert not enforce.approved
    assert enforce.code == rule
    assert rule in enforce.message
    assert enforce.details["eligibility"]["enforced"] == [rule]


@pytest.mark.parametrize("rule, market, price", CASES)
def test_each_rule_passes_healthy_coin(rule, market, price):
    d = _evaluate(rm(sw(**{rule: "enforce"}), Market()), _order(price=1.0))
    assert d.approved, d.message


def test_thresholds_at_boundary():
    # liq7: exactly 100k passes; collapse: exactly 20 % blocks (<=); chase: exactly 15 % passes.
    modes = {"elig_liq7": "enforce", "elig_collapse90": "enforce", "elig_chase48": "enforce"}
    cfgs = {r: rs.rule_config({}, r) for r in rs.ELIG_RULES}
    r = vq.evaluate_eligibility(modes=modes, cfgs=cfgs, price=1.15, now_unix=NOW,
                                daily_bars=daily(qv=100_000.0, high=[1.0] * 50 + [5.0] + [1.0] * 44), hourly_bars=hourly())
    by = {c.code: c for c in r.checks}
    assert by["elig_liq7"].verdict == "pass"
    assert by["elig_collapse90"].verdict == "block" and by["elig_collapse90"].value == pytest.approx(0.20)
    assert by["elig_chase48"].verdict == "pass" and by["elig_chase48"].value == pytest.approx(15.0)


def test_codes_in_fixed_order_and_all_reported():
    m = Market(d=daily(qv=50_000.0, high=[1.01] * 40 + [10.0] * 10 + [1.01] * 45), h=hourly(low=1.0))
    d = _evaluate(rm(ALL_ENFORCE, m), _order(price=1.30))
    assert not d.approved and d.code == "elig_liq7"
    assert d.details["eligibility"]["enforced"] == ["elig_liq7", "elig_collapse90", "elig_chase48"]


def test_missing_data_keeps_by_default_and_blocks_when_configured():
    thin = Market(d=daily(n=2), h=hourly(n=10))
    assert _evaluate(rm(ALL_ENFORCE, thin), _order(price=1.0)).approved
    strict = {r: {"mode": "enforce", "on_missing": "block"} for r in ("elig_liq7", "elig_collapse90", "elig_chase48")}
    d = _evaluate(rm(strict, Market(d=daily(n=2), h=hourly(n=10))), _order(price=1.0))
    assert not d.approved and d.code == "elig_liq7"
    checks = {c["code"]: c["verdict"] for c in d.details["eligibility"]["checks"]}
    assert checks == {"elig_liq7": "missing", "elig_collapse90": "missing", "elig_chase48": "missing"}


def test_check_error_fails_open_and_logs():
    r = rm(ALL_ENFORCE)
    with patch.object(r, "_eligibility_check", side_effect=RuntimeError("boom")):
        assert _evaluate(r, _order(price=1.0)).approved


# --- coverage: DCA, #707, manual -------------------------------------------------------


@pytest.mark.parametrize("rule, market, price", CASES)
def test_dca_adds_are_checked(rule, market, price):
    pos = {"amount": 10.0, "average_entry": 1.0, "dca_rounds": 0, "symbol": SYM}
    d = _evaluate(rm(sw(**{rule: "enforce"}), Market(**market)), _order(price=price, signal="BUY_DCA", source="dca"),
                  source="dca", position=pos)
    assert not d.approved and d.code == rule


def test_leftover_dust_lot_707_is_checked_like_a_new_entry():
    dust = {"amount": 0.001, "average_entry": 1.0, "symbol": SYM}  # below venue min -> new entry
    d = _evaluate(rm(sw(elig_liq7="enforce"), Market(**ILLIQUID)), _order(price=1.0), position=dust)
    assert not d.approved and d.code == "elig_liq7"


def test_mcap_rule_skips_707_dust_lot():
    # A #707 dust lot is not an open position, so elig_mcap (DCA adds only) does not apply;
    # the always-on #563 floor in _evaluate_impl stays the only mcap check on that path.
    dust = {"amount": 0.001, "average_entry": 1.0, "symbol": SYM}
    small = [patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000.0)]
    d = _evaluate(rm(sw(elig_mcap="enforce")), _order(price=1.0), position=dust, extra=small)
    assert d.code != "elig_mcap"
    assert "elig_mcap" not in str((d.details or {}).get("eligibility") or "")


def test_mcap_floor_extends_to_dca_adds():
    pos = {"amount": 10.0, "average_entry": 1.0, "dca_rounds": 0, "symbol": SYM, "strategy_tier": "main"}
    order = _order(price=1.0, signal="BUY_DCA", source="dca")
    small = [patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000.0)]
    # Off (default): a DCA add on a 1M-cap coin goes through, as before.
    assert _evaluate(rm(None), order, source="dca", position=pos, extra=small).approved
    d = _evaluate(rm(sw(elig_mcap="enforce")), _order(price=1.0, signal="BUY_DCA", source="dca"),
                  source="dca", position=pos,
                  extra=[patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000.0)])
    assert not d.approved and d.code == "elig_mcap"
    unknown = _evaluate(rm(sw(elig_mcap="enforce")), _order(price=1.0, signal="BUY_DCA", source="dca"),
                        source="dca", position=pos,
                        extra=[patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=None)])
    assert not unknown.approved and unknown.code == "elig_mcap"  # #563: unknown mcap is denied
    big = _evaluate(rm(sw(elig_mcap="enforce")), _order(price=1.0, signal="BUY_DCA", source="dca"),
                    source="dca", position=pos,
                    extra=[patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=5e9)])
    assert big.approved, big.message


def test_mcap_rule_does_not_touch_new_entries():
    # New entries keep the always-on #563 check in _evaluate_impl; elig_mcap adds nothing there.
    r = rm(sw(elig_mcap="enforce"))
    res = r._eligibility_check(_order(price=1.0), pos={}, has_position=False)
    assert [c.code for c in res.checks] == []


def test_manual_buy_is_logged_not_blocked():
    d = _evaluate(rm(ALL_ENFORCE, Market(**ILLIQUID)), _order(price=1.0, source="manual"), source="manual")
    assert d.approved, d.message


# --- exits never blocked ---------------------------------------------------------------


@pytest.mark.parametrize(
    "signal, source",
    [("SELL", "auto"), ("SELL_STOP_FULL", "dca"), ("SELL_TP", "auto"), ("SELL_STOP", "kill_switch"),
     ("SELL", "manual")],
)
def test_sells_stops_exits_never_blocked(signal, source):
    pos = {"amount": 500.0, "average_entry": 2.0, "symbol": SYM, "entry_at": "2026-01-01T00:00:00"}
    bad = Market(d=daily(qv=1.0, high=[100.0] * 95), h=hourly(low=0.01))
    r = rm(ALL_ENFORCE, bad)
    with patch.object(r, "_eligibility_check", side_effect=AssertionError("eligibility on sell")):
        d = _evaluate(r, _order(type_="SELL", signal=signal, source=source, amount=500.0, price=1.0),
                      source=source, position=pos, open_slots=99,
                      extra=[patch("risk.risk_manager.find_open_position_for_symbol", return_value=("4h", pos))])
    assert d.approved, d.message
    assert bad.calls == []


def test_check_ignores_non_buy():
    assert rm(ALL_ENFORCE)._eligibility_check(_order(type_="SELL"), pos={}, has_position=True) is None


# --- no lookahead ----------------------------------------------------------------------


def test_forming_daily_bar_ignored():
    d = daily(qv=1_000_000.0)
    d[-1] = [DAY_OPEN * 1000, 1.0, 1.0, 1.0, 1.0, 1.0]  # forming bar: tiny volume
    assert vq.median_daily_quote_volume(vq._closed_daily(d, NOW)) == pytest.approx(1_000_000.0)
    d2 = daily(qv=50_000.0)
    d2[-1] = [DAY_OPEN * 1000, 1.0, 1.0, 1.0, 1.0, 1e12]  # forming bar: huge volume
    assert vq.median_daily_quote_volume(vq._closed_daily(d2, NOW)) == pytest.approx(50_000.0)
    d3 = daily()
    d3[-1] = [DAY_OPEN * 1000, 1.0, 1000.0, 1.0, 1.0, 1e6]  # forming bar: new high
    assert vq.close_to_high_ratio(vq._closed_daily(d3, NOW)) > 0.9


def test_future_bars_ignored():
    future = daily(qv=1_000_000.0)
    future += [[(DAY_OPEN + k * D) * 1000, 1.0, 1.0, 1.0, 1.0, 1.0] for k in range(1, 6)]
    assert vq.median_daily_quote_volume(vq._closed_daily(future, NOW)) == pytest.approx(1_000_000.0)
    h = hourly(low=1.0) + [[(HOUR_OPEN + k * H) * 1000, 1.0, 1.0, 0.5, 1.0, 1.0] for k in range(1, 6)]
    m = Market(h=h)
    assert _evaluate(rm(sw(elig_chase48="enforce"), m), _order(price=1.10)).approved


def test_forming_hourly_low_ignored():
    h = hourly(low=1.0)
    h[-1] = [HOUR_OPEN * 1000, 1.0, 1.0, 0.5, 1.0, 1.0]  # forming bar dips to 0.5
    assert _evaluate(rm(sw(elig_chase48="enforce"), Market(h=h)), _order(price=1.10)).approved


# --- tape (#628) -----------------------------------------------------------------------


def test_tape_records_shadow_values(monkeypatch, tmp_path):
    path = _tape_rows(monkeypatch, tmp_path)
    d = _evaluate(rm(sw(elig_chase48="shadow"), Market(**CHASED)), _order(price=1.30))
    assert d.approved
    row = json.loads(path.read_text().strip().splitlines()[-1])
    assert set(row) == set(tape.ROW_KEYS)
    el = row["eligibility"]
    assert el["shadow_would_block"] == ["elig_chase48"] and el["enforced"] == []
    assert el["values"]["above_48h_low_pct"] == pytest.approx(30.0)
    assert el["decision_unix"] == NOW


def test_tape_records_rejection_reason_and_values(monkeypatch, tmp_path):
    path = _tape_rows(monkeypatch, tmp_path)
    d = _evaluate(rm(sw(elig_liq7="enforce", elig_collapse90="shadow"), Market(**ILLIQUID)), _order(price=1.0))
    assert not d.approved
    row = json.loads(path.read_text().strip().splitlines()[-1])
    assert "elig_liq7" in row["filter_codes"]
    el = row["eligibility"]
    assert el["enforced"] == ["elig_liq7"]
    assert el["values"]["qv7_median_usdt"] == pytest.approx(50_000.0)
    assert el["values"]["close_to_90d_high"] > 0.9
    assert el["modes"] == {"elig_liq7": "enforce", "elig_collapse90": "shadow"}


def test_tape_eligibility_null_when_off(monkeypatch, tmp_path):
    path = _tape_rows(monkeypatch, tmp_path)
    assert _evaluate(rm(None), _order(price=1.0)).approved
    row = json.loads(path.read_text().strip().splitlines()[-1])
    assert row["eligibility"] is None


# --- the 4 former denylist coins, caught by data (report section 10) ---------------------
# Feature values from the kauf_setup report; the code under test never sees a name.

FORMER_DENYLIST = {
    # chase48 +176 %, thin but liquid enough on the median
    "BLESS": ({"d": daily(qv=250_000.0), "h": hourly(low=1.0)}, 2.76, "elig_chase48"),
    # dd90 -99.5 %
    "LAB": ({"d": daily(high=[1.01] * 30 + [200.0] * 5 + [1.01] * 60)}, 1.0, "elig_collapse90"),
    # 7d median quote volume 9.8k
    "AVAIL": ({"d": daily(qv=9_800.0)}, 1.0, "elig_liq7"),
    # dd90 -91 %, Oct 7d median 254k (liquid)
    "H": ({"d": daily(qv=254_000.0, high=[1.01] * 30 + [11.2] * 5 + [1.01] * 60)}, 1.0, "elig_collapse90"),
}


@pytest.mark.parametrize("name", sorted(FORMER_DENYLIST))
def test_former_denylist_coin_caught_by_data(name):
    market, price, code = FORMER_DENYLIST[name]
    sym = f"{name}/USDT"
    d = _evaluate(rm(ALL_ENFORCE, Market(**market)), _order(price=price, symbol=sym))
    assert not d.approved and d.code == code, (name, d.code, d.message)
    # The same features on another symbol get the same answer (no names).
    other = _evaluate(rm(ALL_ENFORCE, Market(**market)), _order(price=price, symbol="ZZZ/USDT"))
    assert other.code == code


def test_former_denylist_under_intended_config():
    for name, (market, price, code) in FORMER_DENYLIST.items():
        d = _evaluate(rm(INTENDED, Market(**market)), _order(price=price, symbol=f"{name}/USDT"))
        if code == "elig_chase48":
            assert d.approved, name  # no-chase is shadow in the intended config
        else:
            assert not d.approved and d.code == code, name


def test_no_coin_names_in_live_code():
    names = re.compile(r"\b(BLESS|LAB|AVAIL|H)/USDT\b|['\"](BLESS|LAB|AVAIL)['\"]")
    for rel in ("services/venue_quality.py", "risk/rule_switches.py", "risk/risk_manager.py"):
        assert not names.search((ROOT / rel).read_text()), rel


# --- parity with harness scenario (k) --------------------------------------------------


def _harness_decision(daily_bars, hourly_bars, price):
    from backtest.harness import scenarios as sc

    def to_s(bars):
        return [[b[0] // 1000, *b[1:]] for b in bars]

    ctx = SimpleNamespace(
        cache=SimpleNamespace(get=lambda sym, tf: to_s(daily_bars)),
        engine=SimpleNamespace(candles_1h=lambda sym: to_s(hourly_bars)),
    )
    ep = SimpleNamespace(symbol=SYM, key=("t", SYM, "4h"),
                         entry=SimpleNamespace(ts=datetime.fromtimestamp(NOW, tz=UTC), price=price))
    hits: dict = {}
    keep = sc._elig(ctx, 100_000, -0.80, 15.0, set(), hits)(ep)
    return keep, sorted(hits)


def test_parity_with_harness_scenario_k():
    rnd = random.Random(643)
    modes = {"elig_liq7": "enforce", "elig_collapse90": "enforce", "elig_chase48": "enforce"}
    cfgs = {r: rs.rule_config({}, r) for r in rs.ELIG_RULES}
    for _ in range(200):
        n = rnd.choice([2, 5, 30, 95])
        closes = [rnd.uniform(0.5, 2.0) for _ in range(n)]
        highs = [c * rnd.choice([1.01, 1.5, 8.0]) for c in closes]
        qvs = [rnd.choice([5e3, 6e4, 9e4, 1.2e5, 1e6]) for _ in range(n)]
        d = daily(n, closes=closes, high=highs, qvs=qvs)
        h = hourly(rnd.choice([20, 72]), lows=None, low=rnd.uniform(0.5, 1.5))
        price = rnd.uniform(0.5, 2.0)
        live = vq.evaluate_eligibility(modes=modes, cfgs=cfgs, price=price, now_unix=NOW, daily_bars=d, hourly_bars=h)
        keep, codes = _harness_decision(d, h, price)
        assert sorted(live.enforced_codes) == codes
        assert keep == (not live.enforced_codes)


def test_harness_n_trials_counts_scenario_k():
    from backtest.harness import scenarios as sc

    assert sc.N_TRIALS == 55
    assert "k" in sc.SCENARIOS
