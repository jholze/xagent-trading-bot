#!/usr/bin/env python3
"""Fixture-Tests fuer scripts/backtest_volume_ignition_60d.py -- OHNE Netz.

Warum es diese Tests gibt: ein Backtest, der still das Falsche rechnet, ist
schlimmer als keiner -- er produziert Zahlen, denen man glaubt. Die Tests hier
sichern die vier Stellen, an denen ein Backtest typischerweise luegt:

  * Lookahead       -> T_causality, T_entry_is_next_open
  * Division durch ~0 -> T_baseline_floor  (fand real einen rvol von 15.094x)
  * Phantomfills    -> T_gap_aware_stop
  * verschwundene Diagnose bei n=0 -> T_diag_survives_zero_trades

Die Fixtures sind ECHTE Gate-1h-Kerzen (tests/fixtures/ignition_1h.json):
IMU / HEI / BMT sind Tagesleader des 10-Tage-Fensters, AKE ist die Kontrolle
(One-Day-Wonder, danach tot) und darf NICHT feuern.

  python3.13 -m pytest tests/test_volume_ignition_backtest.py -v
  python3.13 tests/test_volume_ignition_backtest.py          # ohne pytest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_volume_ignition_60d as BT  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ignition_1h.json"

SIMKW = dict(fee_rt=0.002, slip_bps=25.0, ticket=500.0, max_open=6,
             participation=0.02, min_ticket=50.0, start_equity=10000.0)


def _bars(rows: list[list[float]]) -> list[list[float]]:
    """Fixture [ts, quote_vol, c, h, l, o] -> ccxt-Form [ts, o, h, l, c, base_vol]."""
    out = []
    for ts, qv, c, h, l, o in rows:
        typ = (o + h + l + c) / 4.0
        out.append([int(ts), o, h, l, c, (qv / typ) if typ > 0 else 0.0])
    return out


def load_data() -> dict[str, list[list[float]]]:
    raw = json.loads(FIXTURE.read_text())["data"]
    return {sym: _bars(rows) for sym, rows in raw.items()}


def signals(data, **over):
    kw = dict(mult=10, win=12, cooldown_h=48, min_ign_qvol=5000,
              max_baseline_vol24=None, warmup_ts=0)
    kw.update(over)
    out = []
    for sym, bars in data.items():
        out += BT.find_signals(sym, bars, **kw)
    return out


# ------------------------------------------------------------------ tests ---

def test_qvol_roundtrip():
    """qvol() muss das Quote-Volumen der Fixture exakt rekonstruieren."""
    raw = json.loads(FIXTURE.read_text())["data"]["IMU/USDT"]
    bars = _bars(raw)
    for i in (32, 37):
        assert abs(BT.qvol(bars[i]) - raw[i][1]) / raw[i][1] < 1e-6


def test_leaders_fire_control_does_not():
    """Die drei Tagesleader zuenden, die Kontrolle AKE nicht."""
    data = load_data()
    got = {s["symbol"] for s in signals(data)}
    assert "IMU/USDT" in got and "HEI/USDT" in got and "BMT/USDT" in got
    assert "AKE/USDT" not in got, "One-Day-Wonder AKE darf kein Signal ausloesen"


def test_imu_ignition_hour():
    """IMU zuendet 08-08 08:00Z -- handgerechnet aus den Rohkerzen."""
    data = load_data()
    imu = [s for s in signals(data) if s["symbol"] == "IMU/USDT"]
    assert len(imu) == 1
    assert imu[0]["dt"].startswith("2026-08-08T08")


def test_baseline_floor_prevents_rvol_explosion():
    """Regression: ohne Floor kam IMU auf 15.094x, weil der Median gegen 0 lief.

    Der Floor muss den Nenner kappen, ohne den Coin auszuschliessen -- IMU hatte
    6 von 12 Nullstunden und ist trotzdem der groesste Gewinner des Fensters.
    """
    data = load_data()
    imu = [s for s in signals(data) if s["symbol"] == "IMU/USDT"][0]
    assert imu["rvol"] < 1000, f"rvol unplausibel: {imu['rvol']}"
    assert imu["rvol"] > 10
    # und ohne Floor explodiert es tatsaechlich -> der Floor ist nicht kosmetisch
    loose = [s for s in signals(data, baseline_floor=1e-9)
             if s["symbol"] == "IMU/USDT"][0]
    assert loose["rvol"] > 1000


def test_dead_pair_is_rejected_by_absolute_gate():
    """Totes Paar, ein einzelner 60-USDT-Trade: darf kein Signal geben."""
    dead = [[i * 3600, 1.0, 1.0, 1.0, 1.0, 0.0] for i in range(20)]
    dead.append([20 * 3600, 1.0, 1.2, 1.0, 1.15, 60.0])
    dead.append([21 * 3600, 1.15, 1.2, 1.1, 1.15, 10.0])
    assert BT.find_signals("DEAD/USDT", dead, mult=10, win=12, cooldown_h=48,
                           min_ign_qvol=5000, max_baseline_vol24=None,
                           warmup_ts=0) == []


def test_causality_no_lookahead():
    """Schneidet man alles nach dem Entry weg, muss dasselbe Signal entstehen."""
    data = load_data()
    imu = [s for s in signals(data) if s["symbol"] == "IMU/USDT"][0]
    trunc = {"IMU/USDT": data["IMU/USDT"][: imu["entry_idx"] + 1]}
    again = signals(trunc)
    assert again and again[0]["ts"] == imu["ts"]
    assert again[0]["entry_price"] == imu["entry_price"]


def test_entry_is_next_bar_open():
    """Entry zum OPEN der Folgestunde -- nicht zum Close der Signalstunde."""
    data = load_data()
    imu = [s for s in signals(data) if s["symbol"] == "IMU/USDT"][0]
    assert imu["entry_price"] == data["IMU/USDT"][imu["idx"] + 1][1]


def test_stop_beats_target_in_same_bar():
    """Waeren Ziel und Stop in derselben Stunde moeglich, gewinnt der Stop."""
    synth = [[0, 100, 100, 100, 100, 1], [3600, 100, 180, 60, 170, 1]]
    xp, _, why = BT.run_exit(synth, 1, 100.0,
                             {"kind": "trail", "trail_pct": 10, "arm_pct": 0,
                              "stop_pct": 25, "max_h": 10})
    assert why == "stop" and abs(xp - 75.0) < 1e-9


def test_gap_aware_stop():
    """Oeffnet die Stunde unter dem Stop, wird zum Open gefuellt -- nicht am Level."""
    synth = [[0, 100, 100, 100, 100, 1], [3600, 60, 62, 55, 58, 1]]
    xp, _, why = BT.run_exit(synth, 1, 100.0,
                             {"kind": "fixed", "hours": 5, "stop_pct": 25})
    assert why == "stop" and abs(xp - 60.0) < 1e-9


def test_participation_cap_limits_ticket():
    """Duenne Coins lassen sich nicht mit vollem Ticket kaufen."""
    data = load_data()
    r = BT.simulate(signals(data), data, "fix24", **SIMKW)
    assert r["n"] > 0
    assert r["median_realizable_ticket"] < SIMKW["ticket"]


def test_diag_survives_zero_trades():
    """Die Skip-Zaehler sind gerade dann wichtig, wenn nichts getradet wurde."""
    data = load_data()
    r = BT.simulate(signals(data), data, "fix24",
                    **{**SIMKW, "ticket": 9000.0, "min_ticket": 2000.0})
    assert r["n"] == 0
    assert r["skipped_too_illiquid"] == 3
    assert r["signals_in"] == 3


def test_max_open_is_enforced():
    data = load_data()
    sg = signals(data)
    r1 = BT.simulate(sg, data, "fix48", **{**SIMKW, "max_open": 1})
    r6 = BT.simulate(sg, data, "fix48", **{**SIMKW, "max_open": 6})
    assert r1["n"] <= r6["n"]
    assert r1["skipped_no_slot"] >= r6["skipped_no_slot"]


def test_all_exit_policies_run():
    data = load_data()
    sg = signals(data)
    for pol in BT.EXIT_POLICIES:
        r = BT.simulate(sg, data, pol, **SIMKW)
        assert r["n"] == 3, pol


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
    print("\nALLE TESTS BESTANDEN" if not fails else f"\n{fails} FEHLER")
    raise SystemExit(1 if fails else 0)
