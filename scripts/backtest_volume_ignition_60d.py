#!/usr/bin/env python3
"""Backtest: relative-volume ignition als Fruehsignal fuer Gate-Gainer (60 Tage).

HYPOTHESE (aus auswertungen/gis/2026-08-10_gainer-frueherkennung-analyse.md):
  Nicht der Tagesreturn, sondern die relative Volumenexpansion gegen die EIGENE
  Baseline des Coins ist das Fruehsignal. `gainer_universe.min_volume_usdt_24h`
  schliesst genau die Kandidaten aus, bevor sie laufen.

Diese Datei testet das ueber das VOLLE Universum und ALLE Signale -- nicht ueber
eine rueckblickend gewaehlte Gewinnerstichprobe.

METHODIK (die Bias-Fallen, bewusst adressiert)
----------------------------------------------
1. KEIN LOOKAHEAD.  Baseline = Median der quote-Volumina der W Stunden STRIKT VOR
   der Signalstunde. Signal wird am CLOSE der Stunde t erkannt, Entry zum OPEN von
   t+1. Man weiss das Stundenvolumen erst, wenn die Stunde vorbei ist.
2. KEINE SELEKTION.  Es werden ALLE Coins des Universums gescannt und ALLE Signale
   genommen (bis auf Cooldown + Kapitalrestriktion). Keine Vorauswahl von Gewinnern.
3. SURVIVORSHIP.  Das Universum kommt aus den HEUTE gelisteten Gate-USDT-Paaren.
   Seither delistete Coins fehlen -> Ergebnis ist nach oben verzerrt. Der Effekt ist
   bei 60 Tagen klein, aber real. Wird im Report ausgewiesen, nicht wegdiskutiert.
   Gegenmittel: --min-history verlangt volle Historie, sonst faellt der Coin raus.
4. KOSTEN.  fee_rt (default 0.2% Round-Trip) + Slippage in bps auf beiden Seiten.
   Zusaetzlich PARTIZIPATIONS-CAP: Ticket <= participation * quote-Volumen der
   Zuendungsstunde. Ein Coin mit 3k$/Tag laesst sich nicht mit 4000$ kaufen.
   Signale, deren realisierbares Ticket unter --min-ticket faellt, werden verworfen
   und separat gezaehlt (das ist der ehrliche Teil an "toter Coin waecht auf").
5. KAPITALRESTRIKTION.  Portfolio-Simulation chronologisch mit max_open Slots und
   festem Ticket. Signale bei vollem Portfolio werden verworfen und gezaehlt.
   Ein Signal-Mittelwert ohne Kapitalrestriktion ist eine Fantasiezahl.
6. BENCHMARK.  BTC/USDT Buy&Hold ueber dasselbe Fenster + "Zufallseintritt auf
   denselben Coins zu zufaelliger Stunde" (gleiche Coin-Auswahl, Timing zerstoert).
   Letzteres trennt "Signal" von "die Coins liefen ohnehin".
7. REGIME.  Alles zusaetzlich je 15-Tage-Bucket. Wenn es nur in einem Bucket
   funktioniert, ist es ein Regime-Artefakt und kein Signal.
8. SENSITIVITAET.  Sweep ueber Volumen-Multiple, Baseline-Fenster und Exit-Policy.
   Wenn nur eine Parameterecke traegt, ist es Overfitting.

DATEN
-----
ccxt / Gate spot, 1h OHLCV. ccxt liefert BASE-Volumen -> quote-Volumen wird als
base_vol * typical_price ((o+h+l+c)/4) approximiert. Fuer Relativvolumen (Quotient
gegen die eigene Baseline) ist das unkritisch, fuer den absoluten Partizipations-Cap
eine Naeherung.

Alle Rohdaten werden auf Platte gecached (gzip JSON), damit der Parameter-Sweep
ohne erneuten Fetch laeuft. Cache-Verzeichnis: auswertungen/cache/ignition/

AUFRUF
------
  # 1) Daten holen + Basislauf (dauert beim ersten Mal, danach Cache)
  python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --max-symbols 400

  # 2) Sensitivitaets-Sweep auf den gecachten Daten (schnell)
  python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --sweep

  # 3) Nur Coins unterhalb des Produktivfilters (die These im Kern)
  python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --max-baseline-vol24 500000

Keine Orders. Nur oeffentliche Marktdaten.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None

CACHE_DIR = ROOT / "auswertungen" / "cache" / "ignition"
OUT_DIR = ROOT / "auswertungen" / "gis"

_STABLES = {
    "USDT", "USDC", "USD", "DAI", "BUSD", "FDUSD", "TUSD", "USDD", "USDE",
    "EUR", "EURT", "PYUSD", "XAUT", "PAXG",
}
_LEV = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")

HOUR = 3600


# ---------------------------------------------------------------- universe ---

def tradeable(sym: str) -> bool:
    if not sym or not str(sym).endswith("/USDT") or ":" in str(sym):
        return False
    base = str(sym).split("/")[0].upper()
    if base in _STABLES:
        return False
    return not any(base.endswith(s) for s in _LEV)


def _gate():
    if ccxt is None:
        raise RuntimeError("ccxt nicht installiert")
    return ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def universe(max_symbols: int) -> list[str]:
    """Alle handelbaren USDT-Spotpaare, nach heutigem Volumen sortiert.

    BEWUSST OHNE min-volume-Filter: die These ist ja, dass der Produktivfilter
    genau die interessanten Coins ausschliesst. Der Cut bei max_symbols dient nur
    der Laufzeit und wird im Report ausgewiesen.
    """
    ex = _gate()
    tickers = ex.fetch_tickers() or {}
    rows: list[tuple[str, float]] = []
    for sym, t in tickers.items():
        if not tradeable(sym) or not isinstance(t, dict):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            bv = float(t.get("baseVolume") or 0)
            qv = last * bv if last > 0 else 0.0
        rows.append((sym, float(qv or 0)))
    rows.sort(key=lambda x: x[1], reverse=True)
    total = len(rows)
    out = [s for s, _ in rows[:max_symbols]]
    print(f"[universe] {total} handelbare USDT-Paare, genutzt: {len(out)} "
          f"(abgeschnitten: {total - len(out)})")
    return out


# -------------------------------------------------------------------- data ---

def _cache_path(sym: str, start: datetime, end: datetime) -> Path:
    key = f"{sym.replace('/', '_')}_{start:%Y%m%d}_{end:%Y%m%d}_1h.json.gz"
    return CACHE_DIR / key


def fetch_1h(sym: str, start: datetime, end: datetime) -> list[list[float]]:
    """1h-Kerzen [ts_s, o, h, l, c, base_vol]; Disk-Cache; leere Liste bei Fehler."""
    p = _cache_path(sym, start, end)
    if p.exists():
        try:
            with gzip.open(p, "rt") as fh:
                return json.load(fh)
        except Exception:
            pass
    ex = _gate()
    since = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    merged: list[list[float]] = []
    try:
        while since < end_ms:
            chunk = ex.fetch_ohlcv(sym, timeframe="1h", since=since, limit=1000)
            if not chunk:
                break
            merged.extend(chunk)
            last = int(chunk[-1][0])
            if last <= since:
                break
            since = last + HOUR * 1000
            if len(chunk) < 1000:
                break
    except Exception as e:
        print(f"  ! fetch {sym}: {e}")
        return []
    seen: dict[int, list[float]] = {}
    for b in merged:
        ts = int(b[0]) // 1000
        if start.timestamp() <= ts <= end.timestamp():
            seen[ts] = [ts, float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])]
    bars = [seen[k] for k in sorted(seen)]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as fh:
        json.dump(bars, fh)
    return bars


def load_all(symbols: list[str], start: datetime, end: datetime, workers: int) -> dict[str, list]:
    out: dict[str, list] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_1h, s, start, end): s for s in symbols}
        for f in as_completed(futs):
            s = futs[f]
            try:
                bars = f.result()
            except Exception:
                bars = []
            done += 1
            if done % 25 == 0:
                print(f"  ... {done}/{len(symbols)}")
            if bars:
                out[s] = bars
    return out


def qvol(bar: list[float]) -> float:
    """quote-Volumen ~ base_vol * typical price."""
    o, h, l, c, v = bar[1], bar[2], bar[3], bar[4], bar[5]
    return v * ((o + h + l + c) / 4.0)


# ------------------------------------------------------------------ signal ---

def find_signals(
    sym: str,
    bars: list[list[float]],
    *,
    mult: float,
    win: int,
    cooldown_h: int,
    min_ign_qvol: float,
    max_baseline_vol24: float | None,
    warmup_ts: float,
    baseline_floor: float = 100.0,
    min_nonzero: float = 0.25,
) -> list[dict]:
    """Alle Zuendungen in einem Coin. Streng kausal.

    Signal in Stunde t  <=>  qvol[t] >= mult * baseline(t)
                             UND close[t] > open[t]
                             UND qvol[t] >= min_ign_qvol
    mit baseline(t) = max( median(qvol[t-win .. t-1]), baseline_floor )
    Entry = OPEN von t+1.

    ZUR BASELINE-UNTERGRENZE (nicht kosmetisch!):
    Schlafende Coins haben Stunden ganz ohne Umsatz. Der Median laeuft dann gegen 0
    und jede beliebige Kleinorder sieht wie ein 10.000x-Ausbruch aus -- im ersten
    Testlauf kam IMU auf 15.094x. Ein reiner Zaehlfilter ("mind. X% der
    Baselinestunden mit Umsatz") loest das NICHT, sondern wirft genau die
    interessanten Dornroeschen-Coins raus: IMU hatte 6 von 12 Nullstunden und waere
    komplett verschwunden -- der groesste Gewinner des Fensters.

    Richtig ist die Kombination:
      * baseline_floor kappt den Nenner  -> rvol bleibt endlich und vergleichbar
      * min_ign_qvol  als ABSOLUTE Huerde -> echte Leichen (ein 60-USDT-Trade in
        einem toten Paar) fallen raus, weil dort nie genug Geld fliesst
    Der Zaehlfilter bleibt nur als milde Plausibilitaetsschranke erhalten.
    """
    sigs: list[dict] = []
    qs = [qvol(b) for b in bars]
    last_sig = -10**9
    need_nonzero = max(1, int(math.ceil(min_nonzero * win)))
    for t in range(win, len(bars) - 1):
        ts = bars[t][0]
        if ts < warmup_ts:          # Warmup-Zone erzeugt keine Trades
            continue
        if t - last_sig < cooldown_h:
            continue
        prior = qs[t - win:t]
        if sum(1 for q in prior if q > 0) < need_nonzero:
            continue
        base = max(median(prior), baseline_floor)
        if qs[t] < mult * base:
            continue
        if bars[t][4] <= bars[t][1]:
            continue
        if qs[t] < min_ign_qvol:
            continue
        base24 = median(prior) * 24.0   # Schaetzung OHNE Floor (echtes Volumen)
        if max_baseline_vol24 is not None and base24 > max_baseline_vol24:
            continue                # nur Coins UNTER dem Produktivfilter
        last_sig = t
        sigs.append({
            "symbol": sym,
            "idx": t,
            "ts": ts,
            "dt": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "rvol": qs[t] / base,
            "baseline_qvol_h": base,
            "baseline_vol24_est": base24,
            "ign_qvol": qs[t],
            "entry_idx": t + 1,
            "entry_price": bars[t + 1][1],
        })
    return sigs


# -------------------------------------------------------------------- exit ---

def run_exit(
    bars: list[list[float]],
    entry_idx: int,
    entry_price: float,
    policy: dict,
) -> tuple[float, int, str]:
    """Gibt (exit_price, exit_idx, grund) zurueck. Konservativ:
    Stop/Trail werden am LOW der Stunde ausgeloest, Ziel am HIGH -- und wenn beides
    in derselben Stunde moeglich waere, gewinnt der Stop (worst case).
    """
    kind = policy["kind"]
    peak = entry_price
    n = len(bars)
    horizon = policy.get("max_h", 240)
    stop_pct = policy.get("stop_pct")
    for i in range(entry_idx, min(n, entry_idx + horizon)):
        op, hi, lo, cl = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        # harter Stop zuerst (worst case). Gap-aware: oeffnet die Stunde bereits
        # unter dem Stop, wird zum Open gefuellt, nicht zum Stoplevel.
        if stop_pct is not None:
            lvl = entry_price * (1 - stop_pct / 100.0)
            if lo <= lvl:
                return min(lvl, op), i, "stop"
        peak = max(peak, hi)
        if kind == "fixed":
            if i - entry_idx + 1 >= policy["hours"]:
                return cl, i, "time"
        elif kind == "trail":
            arm = policy.get("arm_pct", 0.0)
            trail = policy["trail_pct"]
            if peak >= entry_price * (1 + arm / 100.0):
                lvl = peak * (1 - trail / 100.0)
                if lo <= lvl:
                    return min(lvl, op) if op < lvl else lvl, i, "trail"
        elif kind == "vol_death":
            # Ausstieg, wenn das Relativvolumen K Stunden unter Faktor faellt
            k = policy.get("k", 4)
            fac = policy.get("fac", 2.0)
            base = policy["baseline"]
            if i - entry_idx + 1 >= k:
                recent = [qvol(bars[j]) for j in range(i - k + 1, i + 1)]
                if all(q < fac * base for q in recent):
                    return cl, i, "vol_death"
        else:
            raise ValueError(kind)
    last = min(n - 1, entry_idx + horizon - 1)
    return bars[last][4], last, "horizon_end"


EXIT_POLICIES: dict[str, dict] = {
    "fix6":        {"kind": "fixed", "hours": 6,  "stop_pct": 25},
    "fix24":       {"kind": "fixed", "hours": 24, "stop_pct": 25},
    "fix48":       {"kind": "fixed", "hours": 48, "stop_pct": 25},
    "trail20":     {"kind": "trail", "trail_pct": 20, "arm_pct": 0,  "stop_pct": 25, "max_h": 240},
    "trail30arm25":{"kind": "trail", "trail_pct": 30, "arm_pct": 25, "stop_pct": 25, "max_h": 240},
    "trail40arm50":{"kind": "trail", "trail_pct": 40, "arm_pct": 50, "stop_pct": 25, "max_h": 240},
    "voldeath":    {"kind": "vol_death", "k": 6, "fac": 2.0, "stop_pct": 25, "max_h": 240},
}


# --------------------------------------------------------------- portfolio ---

def simulate(
    signals: list[dict],
    data: dict[str, list],
    policy_name: str,
    *,
    fee_rt: float,
    slip_bps: float,
    ticket: float,
    max_open: int,
    participation: float,
    min_ticket: float,
    start_equity: float,
) -> dict:
    """Chronologische Portfolio-Simulation mit Slot- und Groessenrestriktion."""
    pol = dict(EXIT_POLICIES[policy_name])
    sigs = sorted(signals, key=lambda s: s["ts"])
    open_until: list[float] = []        # ts, wann Slot wieder frei
    equity = start_equity
    curve: list[tuple[float, float]] = []
    trades: list[dict] = []
    skipped_slots = skipped_size = 0

    for s in sigs:
        bars = data[s["symbol"]]
        open_until = [t for t in open_until if t > s["ts"]]
        if len(open_until) >= max_open:
            skipped_slots += 1
            continue
        size = min(ticket, participation * s["ign_qvol"])
        if size < min_ticket:
            skipped_size += 1
            continue
        if pol["kind"] == "vol_death":
            pol["baseline"] = s["baseline_qvol_h"]
        ep = s["entry_price"] * (1 + slip_bps / 10000.0)
        xp, xi, why = run_exit(bars, s["entry_idx"], ep, pol)
        xp *= (1 - slip_bps / 10000.0)
        gross = xp / ep - 1.0
        net = gross - fee_rt
        pnl = size * net
        equity += pnl
        open_until.append(bars[min(xi, len(bars) - 1)][0])
        curve.append((s["ts"], equity))
        trades.append({
            "symbol": s["symbol"], "dt": s["dt"], "rvol": round(s["rvol"], 1),
            "baseline_vol24_est": round(s["baseline_vol24_est"]),
            "size_usdt": round(size, 2), "hold_h": xi - s["entry_idx"] + 1,
            "net_pct": round(100 * net, 2), "pnl_usdt": round(pnl, 2), "exit": why,
        })

    rets = [t["net_pct"] for t in trades]
    n = len(rets)
    # Die Skip-Zaehler sind gerade dann die interessanteste Information, wenn
    # nichts getradet wurde -- darum IMMER mitgeben.
    diag = {
        "policy": policy_name,
        "signals_in": len(sigs),
        "skipped_no_slot": skipped_slots,
        "skipped_too_illiquid": skipped_size,
    }
    if n == 0:
        return {**diag, "n": 0, "note": "keine Trades", "trades": []}
    wins = [r for r in rets if r > 0]
    rets_sorted = sorted(rets)

    peak = start_equity
    mdd = 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = min(mdd, (e - peak) / peak)

    return {
        **diag,
        "n": n,
        "win_rate": round(len(wins) / n, 4),
        "median_pct": round(median(rets), 2),
        "avg_pct": round(sum(rets) / n, 2),
        "p10_pct": round(rets_sorted[int(0.10 * (n - 1))], 2),
        "p90_pct": round(rets_sorted[int(0.90 * (n - 1))], 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "total_pnl_usdt": round(equity - start_equity, 2),
        "return_on_start_pct": round(100 * (equity - start_equity) / start_equity, 2),
        "max_drawdown_pct": round(100 * mdd, 2),
        "avg_hold_h": round(sum(t["hold_h"] for t in trades) / n, 1),
        "median_realizable_ticket": round(median(t["size_usdt"] for t in trades), 2),
        "exit_reasons": {k: sum(1 for t in trades if t["exit"] == k) for k in
                         {t["exit"] for t in trades}},
        "trades": trades,
    }


# -------------------------------------------------------------- benchmarks ---

def shuffled_entry_benchmark(
    signals: list[dict], data: dict[str, list], policy_name: str, seed: int, **kw
) -> dict:
    """Gleiche Coins, gleiche Trade-Anzahl -- aber zufaelliger Einstiegszeitpunkt.

    Trennt 'das Signal timed richtig' von 'diese Coins liefen sowieso'.
    """
    rng = random.Random(seed)
    fake: list[dict] = []
    for s in signals:
        bars = data[s["symbol"]]
        lo, hi = 24, len(bars) - 50
        if hi <= lo:
            continue
        idx = rng.randint(lo, hi)
        f = dict(s)
        f["idx"] = idx
        f["ts"] = bars[idx][0]
        f["entry_idx"] = idx + 1
        f["entry_price"] = bars[idx + 1][1]
        fake.append(f)
    return simulate(fake, data, policy_name, **kw)


def btc_buy_hold(start: datetime, end: datetime) -> float:
    bars = fetch_1h("BTC/USDT", start, end)
    if len(bars) < 2:
        return float("nan")
    return round(100 * (bars[-1][4] / bars[0][1] - 1), 2)


# ------------------------------------------------------------------- main ----

def bucket_stats(trades: list[dict], start: datetime, days: int, n_buckets: int = 4) -> list[dict]:
    span = timedelta(days=days) / n_buckets
    out = []
    for b in range(n_buckets):
        a = start + span * b
        z = start + span * (b + 1)
        sel = [t for t in trades if a.isoformat() <= t["dt"] < z.isoformat()]
        if not sel:
            out.append({"bucket": f"{a:%m-%d}..{z:%m-%d}", "n": 0})
            continue
        r = [t["net_pct"] for t in sel]
        out.append({
            "bucket": f"{a:%m-%d}..{z:%m-%d}", "n": len(r),
            "win_rate": round(sum(1 for x in r if x > 0) / len(r), 3),
            "median_pct": round(median(r), 2),
            "avg_pct": round(sum(r) / len(r), 2),
            "pnl_usdt": round(sum(t["pnl_usdt"] for t in sel), 2),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--max-symbols", type=int, default=400)
    ap.add_argument("--workers", type=int, default=6)
    # Signal
    ap.add_argument("--mult", type=float, default=10.0, help="Volumen-Multiple vs Baseline")
    ap.add_argument("--win", type=int, default=12, help="Baseline-Fenster in Stunden")
    ap.add_argument("--cooldown", type=int, default=48, help="Stunden Sperre je Coin nach Signal")
    ap.add_argument("--min-ign-qvol", type=float, default=5000.0,
                    help="Mindest-quote-Volumen in der Zuendungsstunde")
    ap.add_argument("--baseline-floor", type=float, default=100.0,
                    help="Untergrenze fuer die Baseline in USDT/h (verhindert rvol-Explosion)")
    ap.add_argument("--min-nonzero", type=float, default=0.25,
                    help="Mindestanteil Baselinestunden mit Umsatz (milde Schranke)")
    ap.add_argument("--max-baseline-vol24", type=float, default=None,
                    help="nur Coins, deren geschaetztes 24h-Baselinevolumen darunter liegt")
    # Kosten / Portfolio
    ap.add_argument("--fee-rt", type=float, default=0.002)
    ap.add_argument("--slip-bps", type=float, default=25.0)
    ap.add_argument("--ticket", type=float, default=500.0)
    ap.add_argument("--max-open", type=int, default=6)
    ap.add_argument("--participation", type=float, default=0.02,
                    help="max Anteil am Zuendungsstunden-Volumen")
    ap.add_argument("--min-ticket", type=float, default=50.0)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--exit", default="trail30arm25", choices=sorted(EXIT_POLICIES))
    ap.add_argument("--sweep", action="store_true", help="Sensitivitaets-Sweep")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    warm = timedelta(hours=max(args.win, 48) + 6)
    start = end - timedelta(days=args.days)
    fetch_start = start - warm

    print(f"[window] {start:%Y-%m-%d %H:%M}Z -> {end:%Y-%m-%d %H:%M}Z ({args.days}d) "
          f"+ {warm.total_seconds()/3600:.0f}h Warmup")

    syms = universe(args.max_symbols)
    print(f"[fetch] 1h-Kerzen (Cache: {CACHE_DIR})")
    data = load_all(syms, fetch_start, end, args.workers)
    need = args.days * 24 * 0.8
    data = {s: b for s, b in data.items() if len(b) >= need}
    print(f"[fetch] {len(data)} Coins mit ausreichender Historie "
          f"(>= {need:.0f} Kerzen); {len(syms) - len(data)} verworfen")

    warmup_ts = start.timestamp()

    def collect(mult: float, win: int) -> list[dict]:
        out: list[dict] = []
        for s, bars in data.items():
            out += find_signals(
                s, bars, mult=mult, win=win, cooldown_h=args.cooldown,
                min_ign_qvol=args.min_ign_qvol,
                max_baseline_vol24=args.max_baseline_vol24,
                warmup_ts=warmup_ts, baseline_floor=args.baseline_floor,
                min_nonzero=args.min_nonzero,
            )
        return out

    simkw = dict(fee_rt=args.fee_rt, slip_bps=args.slip_bps, ticket=args.ticket,
                 max_open=args.max_open, participation=args.participation,
                 min_ticket=args.min_ticket, start_equity=args.equity)

    report: dict[str, Any] = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "params": vars(args),
        "universe": {"scanned": len(syms), "with_history": len(data)},
        "caveats": [
            "Survivorship: Universum = HEUTE gelistete Gate-USDT-Paare; seither delistete Coins fehlen.",
            "quote-Volumen aus ccxt base_vol * typical price approximiert.",
            "Slippage pauschal in bps; reale Slippage in duennen Baechern ist pfadabhaengig.",
            "Kein Orderbuch -> Partizipations-Cap ist eine Naeherung fuer Fuellbarkeit.",
        ],
    }

    if args.sweep:
        print("\n[sweep] Sensitivitaet")
        grid = []
        for mult in (5, 10, 20, 40):
            for win in (12, 24, 48):
                sg = collect(mult, win)
                for pol in ("fix24", "trail30arm25", "voldeath"):
                    r = simulate(sg, data, pol, **simkw)
                    r.pop("trades", None)
                    r.update({"mult": mult, "win": win, "signals_raw": len(sg)})
                    grid.append(r)
                    print(f"  mult={mult:>3} win={win:>3} {pol:<13} n={r.get('n',0):>4} "
                          f"win%={r.get('win_rate',0):>5} med={r.get('median_pct',0):>7} "
                          f"avg={r.get('avg_pct',0):>7} pnl={r.get('total_pnl_usdt',0):>9} "
                          f"mdd={r.get('max_drawdown_pct',0):>7}")
        report["sweep"] = grid
    else:
        sigs = collect(args.mult, args.win)
        print(f"\n[signals] {len(sigs)} Rohsignale bei mult={args.mult} win={args.win}")
        report["signals_raw"] = len(sigs)
        results = {}
        for pol in sorted(EXIT_POLICIES):
            r = simulate(sigs, data, pol, **simkw)
            results[pol] = r
            print(f"  {pol:<13} n={r.get('n',0):>4} win%={r.get('win_rate',0):>6} "
                  f"med={r.get('median_pct',0):>7} avg={r.get('avg_pct',0):>7} "
                  f"pnl={r.get('total_pnl_usdt',0):>9} mdd={r.get('max_drawdown_pct',0):>7}")
        main_r = results[args.exit]
        report["by_exit"] = {k: {kk: vv for kk, vv in v.items() if kk != "trades"}
                             for k, v in results.items()}
        report["main"] = main_r
        report["buckets"] = bucket_stats(main_r.get("trades", []), start, args.days)
        print("\n[regime] je Zeitfenster (Policy: %s)" % args.exit)
        for b in report["buckets"]:
            print(f"  {b['bucket']}  n={b.get('n',0):>4} win%={b.get('win_rate','-'):>6} "
                  f"med={b.get('median_pct','-'):>7} pnl={b.get('pnl_usdt','-'):>9}")

        print("\n[benchmark]")
        bh = btc_buy_hold(start, end)
        print(f"  BTC buy&hold ueber Fenster: {bh:+.2f}%")
        sh = shuffled_entry_benchmark(sigs, data, args.exit, args.seed, **simkw)
        sh.pop("trades", None)
        print(f"  Zufalls-Timing, gleiche Coins: n={sh.get('n',0)} "
              f"med={sh.get('median_pct',0)}% avg={sh.get('avg_pct',0)}% "
              f"pnl={sh.get('total_pnl_usdt',0)}")
        report["benchmark"] = {"btc_buy_hold_pct": bh, "shuffled_entry": sh}
        edge = (main_r.get("avg_pct", 0) - sh.get("avg_pct", 0))
        print(f"\n  >>> Timing-Edge (avg Signal - avg Zufall): {edge:+.2f} Prozentpunkte")
        report["timing_edge_pp"] = round(edge, 2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.tag or f"{args.days}d"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"volume_ignition_backtest_{tag}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
