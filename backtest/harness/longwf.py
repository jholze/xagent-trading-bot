"""Long walk-forward (Jan 2025 - Oct 2026) with DCA caps, 2026-11-01.

Pre-registered (thresholds fixed before the run, no tuning):

* entries before the tape starts come from a *proxy* of the gainer entry
  (Freqtrade cross-check rule: 24 h return > 8 %, 24 h volume > 2x its
  6-day baseline, green bar), at most K per Berlin day, first come;
* DCA adds are decided by the real ``strategies.dca.should_dca`` on 1h bars
  (rule mode), stops by the real ``evaluate_long_hard_stop``;
* proxy exit: full sell at the bar close when close >= upper BB(20,2) and
  close >= avg * 1.015 (stands in for the recorded bb_upper SELL_FULL exits);
* variants (a) max 2 adds/lot, (b) adds <= 2x entry, (c) no add while breadth
  < 40 %, (d) 24 h cooldown per coin after a stop exit, (e) lot older than
  14 d and <= -15 % -> close; combo = a+b+c+d+e; rg = early exit -10 %/72 h
  only while breadth weak (< 40 %) + 48 h no-chase limit 10/15/20 % for
  weak/mid/strong (h_oracle with breadth as the regime);
* walk-forward: 2 calendar months train, next month test, rolling. Each
  fixed variant is judged on all test months; the WF selector picks on the
  train months and is applied out of sample.

Breadth = share of the downloaded universe whose 1h close is above its own
168-bar mean, from bars closed before the decision bar.
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import stats
from .costs import CostModel
from .data import Fill, load_book, load_fills, load_tenant_params
from .engine import HOUR, Engine, Episode, build_episodes
from .metrics import daily_returns, decide, guardrails_worse, summarize
from .policy import REPO_ROOT, import_real
from .scenarios import ENTRY_WINDOWS, N_TRIALS, TAPE_END, policy

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
DAY = 86400
LONG = Path(os.environ.get("BT_LONG", "/workspace/bt-data/candles_long"))
DATA = Path(os.environ.get("BT_DATA", "/workspace/trading-learning/railway_export"))
T0 = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
END = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp())
TEST_FIRST = (2025, 3)
STABLE = {"USDC", "FDUSD", "DAI", "TUSD", "USDE", "USDD", "PYUSD", "BUSD", "USDP", "EUR", "EURC", "USD1",
          "RLUSD", "USDG", "XAUT", "PAXG", "USDT0", "BFUSD", "USDS"}

# pre-registered proxy constants
RET24_MIN = 0.08
RELVOL_MIN = 2.0
K_PER_DAY = 5
SIGNAL_DEDUP_H = 24
ENTRY_USDT = 1275.0  # median recorded entry size
TP_MIN_GAIN = 1.015
WEAK, STRONG = 0.40, 0.60
NEW_TRIALS = 8  # a, b, c, d, e, combo, rg, wf_selector
N_ALL = N_TRIALS + NEW_TRIALS
OVL_START = ENTRY_WINDOWS[0][1]
OVL_END = TAPE_END


# --------------------------------------------------------------------------- data
def _synth_from_4h(rows4, first_real_1h):
    out = []
    for t, o, h, l, c, v in rows4:
        if t < T0 - 10 * DAY or t + 4 * HOUR > first_real_1h:
            continue
        a, b = (l, h) if c >= o else (h, l)
        p = [o, a, (a + b) / 2.0, b, c]
        for k in range(4):
            x, y = p[k], p[k + 1]
            out.append((t + k * HOUR, x, max(x, y), min(x, y), y, v / 4.0))
    return out


def load_hourly(pair: str):
    p1, p4 = LONG / "1h" / f"{pair}.json", LONG / "4h" / f"{pair}.json"
    h1 = [tuple(r) for r in json.load(open(p1))] if p1.exists() else []
    h4 = [tuple(r) for r in json.load(open(p4))] if p4.exists() else []
    first = h1[0][0] if h1 else 2**62
    rows = _synth_from_4h(h4, first) + [r for r in h1 if r[0] < END]
    rows = [r for r in rows if r[4] > 0 and r[3] > 0]
    rows.sort()
    return rows, (first if h1 else None)


class Market:
    def __init__(self):
        pairs = sorted(p[:-5] for p in os.listdir(LONG / "4h") if p.endswith("_USDT.json"))
        self.candles: dict[str, list] = {}
        self.first_real: dict[str, int | None] = {}
        for pair in pairs:
            rows, first = load_hourly(pair)
            if len(rows) < 300:
                continue
            sym = pair.replace("_", "/")
            self.candles[sym] = rows
            self.first_real[sym] = first
        self.universe = [s for s in self.candles if s.split("/")[0] not in STABLE]
        self._breadth()
        self._ub: dict[str, np.ndarray] = {}

    def _breadth(self):
        n = (END - T0) // HOUR
        self.grid0 = T0
        mat = np.full((len(self.universe), n), np.nan)
        for k, s in enumerate(self.universe):
            for r in self.candles[s]:
                j = (r[0] - T0) // HOUR
                if 0 <= j < n:
                    mat[k, j] = r[4]
        # forward fill gaps after listing
        for k in range(mat.shape[0]):
            row = mat[k]
            ok = ~np.isnan(row)
            if not ok.any():
                continue
            idx = np.where(ok, np.arange(n), 0)
            np.maximum.accumulate(idx, out=idx)
            filled = row[idx]
            filled[: int(np.argmax(ok))] = np.nan
            mat[k] = filled
        valid = ~np.isnan(mat)
        z = np.where(valid, mat, 0.0)
        cs = np.concatenate([np.zeros((mat.shape[0], 1)), np.cumsum(z, axis=1)], axis=1)
        cv = np.concatenate([np.zeros((mat.shape[0], 1)), np.cumsum(valid, axis=1)], axis=1)
        w = 168
        s168 = cs[:, w:] - cs[:, :-w]  # window ending at j (j >= w-1)
        c168 = cv[:, w:] - cv[:, :-w]
        br = np.full(n, np.nan)
        cnt = np.zeros(n)
        mean = np.where(c168 == w, s168 / w, np.nan)
        cur = mat[:, w - 1 :]
        ok = ~np.isnan(mean) & ~np.isnan(cur)
        above = (cur > mean) & ok
        tot = ok.sum(axis=0)
        val = np.where(tot >= 20, above.sum(axis=0) / np.maximum(tot, 1), np.nan)
        br[w - 1 :] = val
        cnt[w - 1 :] = tot
        self.breadth = br  # breadth[j] uses closes of bars <= j
        self.breadth_n = cnt

    def breadth_at(self, ts: int) -> float | None:
        """Breadth known at ``ts`` (bar open): from bars that closed at or before ts."""
        j = (ts - self.grid0) // HOUR - 1
        if j < 0 or j >= len(self.breadth):
            return None
        v = self.breadth[j]
        return None if np.isnan(v) else float(v)

    def label_at(self, ts: int) -> str | None:
        b = self.breadth_at(ts)
        if b is None:
            return None
        return "weak" if b < WEAK else ("strong" if b > STRONG else "mid")

    def upper_bb(self, sym: str) -> np.ndarray:
        if sym not in self._ub:
            cl = np.asarray([r[4] for r in self.candles[sym]], dtype=float)
            c1 = np.concatenate([[0.0], np.cumsum(cl)])
            c2 = np.concatenate([[0.0], np.cumsum(cl * cl)])
            idx = np.arange(len(cl))
            lo = np.clip(idx - 19, 0, None)
            mid = (c1[idx + 1] - c1[lo]) / 20.0
            sd = np.sqrt(np.maximum((c2[idx + 1] - c2[lo]) / 20.0 - mid * mid, 0.0))
            ub = mid + 2 * sd
            ub[:19] = np.inf
            self._ub[sym] = ub
        return self._ub[sym]


# --------------------------------------------------------------------------- proxy
# robustness proxy P2 (broad, closer to the recorded entry features); not a strategy trial
P2_RET24_MIN, P2_RELVOL_MIN = 0.03, 1.5


def raw_signals(mk: Market, start: int = T0 + 8 * DAY, end: int = END, ret_min: float = RET24_MIN,
                relvol_min: float = RELVOL_MIN) -> list[tuple[int, str, float, float, float]]:
    """(decision_unix, symbol, close, ret24, relvol) at the bar close, deduped 24 h per coin."""
    out = []
    for sym in mk.universe:
        rows = mk.candles[sym]
        a = np.asarray(rows, dtype=float)
        if len(a) < 200:
            continue
        ts, o, c, v = a[:, 0], a[:, 1], a[:, 4], a[:, 5] * a[:, 4]  # quote volume
        ret24 = np.full(len(a), np.nan)
        ret24[24:] = c[24:] / c[:-24] - 1.0
        cv = np.concatenate([[0.0], np.cumsum(v)])
        v24 = np.full(len(a), np.nan)
        v24[23:] = cv[24:] - cv[:-24]
        base = np.full(len(a), np.nan)
        # mean of v24 shifted by 24 over 144 rows
        sh = np.full(len(a), np.nan)
        sh[24:] = v24[:-24]
        shz = np.where(np.isnan(sh), 0.0, sh)
        cs = np.concatenate([[0.0], np.cumsum(shz)])
        cn = np.concatenate([[0], np.cumsum(~np.isnan(sh))])
        m = 144
        s = cs[m:] - cs[:-m]
        n_ = cn[m:] - cn[:-m]
        base[m - 1 :] = np.where(n_ == m, s / m, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            relvol = v24 / base
        hit = (ret24 > ret_min) & (relvol > relvol_min) & (c > o)
        last = -(10**12)
        for j in np.nonzero(hit)[0]:
            t = int(ts[j]) + HOUR
            if t < start or t >= end:
                continue
            if t - last < SIGNAL_DEDUP_H * HOUR:
                continue
            last = t
            out.append((t, sym, float(c[j]), float(ret24[j]), float(relvol[j])))
    out.sort()
    return out


def proxy_entries(signals, k_per_day: int = K_PER_DAY) -> list[tuple[int, str, float]]:
    per_day: dict = {}
    out = []
    for t, sym, px, _r, _v in signals:
        d = datetime.fromtimestamp(t, BERLIN).date()
        if per_day.get(d, 0) >= k_per_day:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        out.append((t, sym, px))
    return out


def proxy_episodes(entries) -> list[Episode]:
    eps = []
    for t, sym, px in entries:
        ts = datetime.fromtimestamp(t - 1, UTC)
        f = Fill(f"proxy-{sym}-{t}", "default", sym, "1h", "buy", "BUY", "gainer_proxy", None, ts, px,
                 ENTRY_USDT / px, ENTRY_USDT)
        eps.append(Episode("default", sym, "1h", f, [f], None))
    eps.sort(key=lambda e: (e.entry.ts, e.key))
    return eps


# --------------------------------------------------------------------------- variants
def _pump48(mk: Market, sym: str, sig: int, price: float) -> float | None:
    rows = mk.candles.get(sym) or []
    from bisect import bisect_left

    i1 = bisect_left(rows, (sig - HOUR + 1,))
    i0 = bisect_left(rows, (sig - 48 * HOUR,))
    lows = [r[3] for r in rows[i0:i1] if r[0] + HOUR <= sig]
    if len(lows) < 36 or min(lows) <= 0:
        return None
    return (price / min(lows) - 1.0) * 100.0


def variant_specs(mk: Market, *, labels=None, proxy: bool = True):
    """name -> (kwargs builder). ``labels`` overrides the breadth label (random baselines)."""
    lab = labels or mk.label_at

    def weak(ts):
        return lab(ts) == "weak"

    def dca_ok(ts):
        return lab(ts) != "weak"  # None (warm-up) -> allowed

    def nochase(ep):
        sig = int(ep.entry.ts.timestamp()) + 1
        p = _pump48(mk, ep.symbol, sig, ep.entry.price)
        if p is None:
            return None
        lb = lab(sig)
        lim = 10.0 if lb == "weak" else (20.0 if lb == "strong" else 15.0)
        return p <= lim

    def tp(sym, i, candles, pos):
        c = candles[i][4]
        return c >= mk.upper_bb(sym)[i] and c >= pos["average_entry"] * TP_MIN_GAIN

    common = dict(dca_mode="rule", bar_exit=tp, skip_if_open=True) if proxy else dict(dca_mode="tape")
    caps = dict(max_adds=2, dca_budget_mult=2.0, dca_gate=dca_ok, cooldown_after_stop_h=24.0,
                time_limit_days=14.0, time_limit_loss_pct=15.0)
    return {
        "baseline": dict(common),
        "a_max2adds": dict(common, max_adds=2),
        "b_budget2x": dict(common, dca_budget_mult=2.0),
        "c_breadth40": dict(common, dca_gate=dca_ok),
        "d_cool24h": dict(common, cooldown_after_stop_h=24.0),
        "e_age14_m15": dict(common, time_limit_days=14.0, time_limit_loss_pct=15.0),
        "combo_abcde": dict(common, **caps),
        "rg_breadth": dict(common, early_exit_pct=10.0, early_exit_hours=72.0, early_exit_gate=weak,
                           entry_filter=nochase),
    }


DESC = {
    "baseline": "staging stop + DCA code, no cap",
    "a_max2adds": "(a) at most 2 DCA adds per lot",
    "b_budget2x": "(b) DCA adds per lot <= 2x entry USDT (last add truncated)",
    "c_breadth40": "(c) no DCA add while breadth < 40 %",
    "d_cool24h": "(d) no new lot on the coin for 24 h after a stop exit",
    "e_age14_m15": "(e) lot older than 14 d and <= -15 % vs avg -> close at bar close",
    "combo_abcde": "a+b+c+d+e together",
    "rg_breadth": "early exit -10 %/72 h only while breadth weak + no-chase 48 h pump 10/15/20 % (weak/mid/strong)",
}
VARIANTS = list(DESC)


# --------------------------------------------------------------------------- engine/world (fork-shared)
W: dict = {}


def build_world(cost_tiers=("base", "stress")):
    t = time.time()
    mk = Market()
    fills = load_fills(DATA / "orders_v2_filled_alltime.jsonl")
    book = load_book(DATA / "positions_current.jsonl")
    tenants = load_tenant_params(DATA / "tenant_configs.jsonl", REPO_ROOT / "config.json")
    engines = {}
    for tier in cost_tiers:
        eng = Engine(candles_1h=lambda s: mk.candles.get(s, []), tenants=tenants, book=book,
                     positions_mod=import_real("strategies.positions"), costs=CostModel(tier=tier))
        eng.fast_indicators = True
        engines[tier] = eng
    sigs = raw_signals(mk)
    sigs2 = raw_signals(mk, ret_min=P2_RET24_MIN, relvol_min=P2_RELVOL_MIN)
    W.update(mk=mk, engines=engines, tape=build_episodes(fills), signals=sigs, signals2=sigs2,
             proxy=proxy_episodes(proxy_entries(sigs)), proxy2=proxy_episodes(proxy_entries(sigs2)),
             built_s=time.time() - t)
    return W


def _random_labels(mk: Market, seed: int):
    """Block-random weak/mid/strong with the empirical shares and median spell length."""
    labs = [mk.label_at(mk.grid0 + (j + 1) * HOUR) for j in range(len(mk.breadth))]
    known = [x for x in labs if x]
    shares = {k: known.count(k) / len(known) for k in ("weak", "mid", "strong")}
    spells, cur, n = [], None, 0
    for x in known:
        if x == cur:
            n += 1
        else:
            if cur:
                spells.append(n)
            cur, n = x, 1
    block = max(1, int(statistics.median(spells)))
    rng = random.Random(seed)
    out = []
    keys = list(shares)
    weights = [shares[k] for k in keys]
    while len(out) < len(labs):
        out.extend([rng.choices(keys, weights)[0]] * block)
    out = out[: len(labs)]
    out = [o if l else None for o, l in zip(out, labs)]

    def f(ts):
        j = (ts - mk.grid0) // HOUR - 1
        return out[j] if 0 <= j < len(out) else None

    return f, shares, block


def _run_job(job):
    kind, name, tier, seed = job
    mk = W["mk"]
    eng = W["engines"][tier]
    labels = None
    if seed is not None:
        labels, _sh, _bl = _random_labels(mk, seed)
    proxy = kind in ("proxy", "proxy2")
    kw = variant_specs(mk, labels=labels, proxy=proxy)[name]
    pol = policy(name if seed is None else f"{name}_rnd{seed}", ref=None, escalate=True, **kw)
    t = time.time()
    if proxy:
        start, end, horizon = datetime.fromtimestamp(T0, UTC), datetime.fromtimestamp(END, UTC), datetime.fromtimestamp(END, UTC)
        res = eng.run(pol, W[kind], start, end, carry_in=False, horizon_end=horizon)
    elif kind == "tape_rule":
        pol = policy(f"{name}_rule", ref=None, escalate=True, **dict(kw, dca_mode="rule"))
        res = eng.run(pol, W["tape"], OVL_START, OVL_END, carry_in=False, horizon_end=OVL_END)
    else:
        res = eng.run(pol, W["tape"], OVL_START, OVL_END, carry_in=False, horizon_end=OVL_END)
    res.notes = [f"cooldown_skips={res.notes.count('cooldown')}", f"secs={time.time() - t:.0f}"]
    return job, res


def run_jobs(jobs, procs=7):
    ctx = get_context("fork")
    out = {}
    with ctx.Pool(procs) as pool:
        for job, res in pool.imap_unordered(_run_job, jobs):
            out[job] = res
            print("done", job, res.notes, flush=True)
    return out


# --------------------------------------------------------------------------- evaluation
def month_keys():
    y, m = TEST_FIRST
    out = []
    while datetime(y, m, 1, tzinfo=UTC).timestamp() < END:
        out.append((y, m))
        y, m = (y + (m == 12), m % 12 + 1)
    return out


def _mstart(y, m):
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp())


def _mend(y, m):
    y2, m2 = (y + (m == 12), m % 12 + 1)
    return min(int(datetime(y2, m2, 1, tzinfo=UTC).timestamp()), END)


def day_table(run):
    pnl, caph = {}, {}
    cap = 0.0
    for ts in sorted(set(run.pnl_curve) | set(run.capital_curve)):
        d = ts - ts % DAY
        pnl[d] = pnl.get(d, 0.0) + run.pnl_curve.get(ts, 0.0)
        cap += run.capital_curve.get(ts, 0.0)
        caph[d] = caph.get(d, 0.0) + max(cap, 0.0)
    return pnl, {d: v / 24.0 for d, v in caph.items()}


def period_stats(run, a: int, b: int):
    pnl, cap = day_table(run)
    days = range(a - a % DAY, b, DAY)
    p = sum(pnl.get(d, 0.0) for d in days)
    caps = [cap.get(d, 0.0) for d in days]
    mc = statistics.fmean(caps) if caps else 0.0
    return {"pnl_usdt": p, "avg_deployed": mc, "roc_pct": 100.0 * p / mc if mc > 1 else 0.0}


def mc_shuffle_dd(lot_pnls, n=2000, seed=7):
    def dd(seq):
        cum = peak = m = 0.0
        for x in seq:
            cum += x
            peak = max(peak, cum)
            m = max(m, peak - cum)
        return m

    rng = random.Random(seed)
    act = dd(lot_pnls)
    xs = list(lot_pnls)
    draws = []
    for _ in range(n):
        rng.shuffle(xs)
        draws.append(dd(xs))
    draws.sort()
    return {"actual_dd": act, "p50": draws[n // 2], "p95": draws[int(0.95 * n)], "p_ge_actual": sum(d >= act for d in draws) / n}


def evaluate_proxy(runs: dict, tier: str, *, first_month=None):
    months = month_keys()
    if first_month:
        months = [m for m in months if m >= first_month]
    a0, b0 = _mstart(*months[0]), END
    dkeys = list(range(a0, b0, DAY))
    daily = {v: daily_returns(runs[v], dkeys) for v in VARIANTS}
    monthly = {v: {f"{y}-{m:02d}": period_stats(runs[v], _mstart(y, m), _mend(y, m)) for y, m in months} for v in VARIANTS}
    full = {v: summarize(runs[v]) for v in VARIANTS}
    base = full["baseline"]
    out = {"months": [f"{y}-{m:02d}" for y, m in months], "variants": {}}
    for v in VARIANTS:
        lots = [x for x in runs[v].lots if int(x.entry_ts.timestamp()) >= a0]
        inv = sum(x.invested for x in lots)
        pnl = sum(x.pnl for x in lots)
        tot = period_stats(runs[v], a0, b0)
        row = {"desc": DESC[v], "lots": len(lots), "invested": inv, "pnl_usdt_cohort": pnl,
               "pnl_pct_cohort": 100 * pnl / inv if inv else 0.0, "pnl_usdt_curve": tot["pnl_usdt"],
               "avg_deployed": tot["avg_deployed"], "roc_pct": tot["roc_pct"],
               "share_lots_le_minus20": 100 * sum(x.ret <= -0.2 for x in lots) / len(lots) if lots else 0.0,
               "max_dd_pct": full[v].max_dd_pct, "share_open_le_minus20": full[v].share_open_le_minus20,
               "capital_in_losing_lots": full[v].capital_in_losing_lots, "open_lots_end": full[v].open_lots,
               "adds": sum(x.dca_adds for x in lots), "capped_adds": sum(x.capped_adds for x in lots),
               "time_stops": sum(x.time_stops for x in lots), "early_exits": sum(x.early_exits for x in lots),
               "stops_full": sum(x.stop_full + x.escalated for x in lots), "filtered_out": runs[v].filtered_out,
               "monthly": monthly[v]}
        if v != "baseline":
            wins = [monthly[v][k]["roc_pct"] > monthly["baseline"][k]["roc_pct"] for k in out["months"]]
            wins_usdt = [monthly[v][k]["pnl_usdt"] > monthly["baseline"][k]["pnl_usdt"] for k in out["months"]]
            excess = [c - b for c, b in zip(daily[v], daily["baseline"])]
            worse = guardrails_worse(base, full[v])
            need = math.ceil(2 * len(wins) / 3)
            verdict = decide(window_wins=wins, excess_daily=excess, n_trials=N_ALL, worse=worse)
            label = verdict.label
            why = verdict.reason
            # rule 4 needs >= 2/3 of windows (decide() only needs 2 of 3 windows)
            if sum(wins) < need:
                label, why = "KILL", f"won {sum(wins)}/{len(wins)} test months (< 2/3 = {need}); " + why
            ci = stats.bootstrap_ci(excess, block=5)
            row.update(windows_won=sum(wins), windows=len(wins), windows_won_usdt=sum(wins_usdt), dsr=verdict.dsr,
                       ci_low_bp=1e4 * ci.low, ci_high_bp=1e4 * ci.high, excess_bp=1e4 * ci.estimate,
                       guardrails_worse=worse, verdict=label, why=why,
                       mc=mc_shuffle_dd([x.pnl for x in sorted(lots, key=lambda x: x.entry_ts)]))
        else:
            row["mc"] = mc_shuffle_dd([x.pnl for x in sorted(lots, key=lambda x: x.entry_ts)])
        out["variants"][v] = row
    matrix = [[daily[v][t] for v in VARIANTS] for t in range(len(dkeys))]
    pbo = stats.pbo_cscv(matrix, n_blocks=10)
    out["pbo"] = {"pbo": pbo.pbo, "splits": pbo.n_splits, "median_logit": pbo.median_logit}
    out["wf_selector"] = wf_selector(runs, months, daily, dkeys)
    out["n_trials"] = N_ALL
    out["tier"] = tier
    return out


def wf_selector(runs, months, daily, dkeys, n_random=500):
    """Pick the variant with the best ROC over the previous 2 months, apply next month."""
    allm = month_keys()
    picks, oos = [], []
    for y, m in months:
        i = allm.index((y, m))
        if i < 2:
            # train months before the first test month (Jan/Feb 2025)
            tr = [(2025, 1), (2025, 2)] if (y, m) == (2025, 3) else allm[max(0, i - 2) : i]
        else:
            tr = allm[i - 2 : i]
        a, b = _mstart(*tr[0]), _mend(*tr[-1])
        score = {v: period_stats(runs[v], a, b)["roc_pct"] for v in VARIANTS}
        best = max(VARIANTS, key=lambda v: (score[v], v == "baseline"))
        picks.append(best)
    meta = []
    month_of = []
    for t, d in enumerate(dkeys):
        dt = datetime.fromtimestamp(d, UTC)
        month_of.append(months.index((dt.year, dt.month)))
    for t in range(len(dkeys)):
        meta.append(daily[picks[month_of[t]]][t])
    excess = [x - b for x, b in zip(meta, daily["baseline"])]
    wins = []
    for k, (y, m) in enumerate(months):
        key = f"{y}-{m:02d}"
        wins.append(period_stats(runs[picks[k]], _mstart(y, m), _mend(y, m))["roc_pct"]
                    > period_stats(runs["baseline"], _mstart(y, m), _mend(y, m))["roc_pct"])
    rng = random.Random(11)
    tot = sum(excess)
    rand_tot = []
    for _ in range(n_random):
        pk = [rng.choice(VARIANTS) for _ in months]
        rand_tot.append(sum(daily[pk[month_of[t]]][t] - daily["baseline"][t] for t in range(len(dkeys))))
    ci = stats.bootstrap_ci(excess, block=5)
    return {"picks": dict(zip([f"{y}-{m:02d}" for y, m in months], picks)), "windows_won": sum(wins),
            "windows": len(wins), "dsr": stats.deflated_sharpe(excess, N_ALL), "excess_bp": 1e4 * ci.estimate,
            "ci_low_bp": 1e4 * ci.low, "ci_high_bp": 1e4 * ci.high,
            "random_switch_pct_rank": sum(r < tot for r in rand_tot) / n_random,
            "random_switch_median_excess_bp": 1e4 * statistics.median(rand_tot) / len(dkeys)}


# --------------------------------------------------------------------------- faithfulness
def _feat(mk, sym, sig, price):
    rows = mk.candles.get(sym) or []
    from bisect import bisect_left

    j = bisect_left(rows, (sig,)) - 1
    if j < 72:
        return None
    c = price
    r24 = c / rows[j - 23][1] - 1 if rows[j - 23][1] > 0 else None
    r72 = c / rows[j - 71][1] - 1 if rows[j - 71][1] > 0 else None
    p48 = _pump48(mk, sym, sig, price)
    return r24, r72, p48


def faithfulness(runs_proxy_base, tape_base, tape_rule, which="proxy"):
    mk, sigs = W["mk"], W["signals" if which == "proxy" else "signals2"]
    a, b = int(OVL_START.timestamp()), int(OVL_END.timestamp())
    real = [e for e in W["tape"] if a <= int(e.entry.ts.timestamp()) < b]
    gainer_src = {"gainer_relvol", "gainer_rank_entry", "gainer_live_heat"}
    real_g = [e for e in real if e.entry.source in gainer_src]
    prox = [e for e in W[which] if a <= int(e.entry.ts.timestamp()) + 1 < b]
    rs = [s for s in sigs if a <= s[0] < b]
    ndays = (b - a) / DAY
    by_coin: dict = {}
    for t, s, *_ in rs:
        by_coin.setdefault(s, []).append(t)
    real_by: dict = {}
    for e in real:
        real_by.setdefault(e.symbol, []).append(int(e.entry.ts.timestamp()))

    def near(lst, t, h=24):
        return any(abs(x - t) <= h * HOUR for x in lst or [])

    def recall(eps):
        return 100 * sum(near(by_coin.get(e.symbol), int(e.entry.ts.timestamp())) for e in eps) / max(1, len(eps))

    prec = 100 * sum(near(real_by.get(e.symbol), int(e.entry.ts.timestamp()) + 1) for e in prox) / max(1, len(prox))
    rc, pc = {e.symbol for e in real}, {e.symbol for e in prox}
    covered = 100 * sum(e.symbol in mk.candles for e in real) / max(1, len(real))

    def feats(eps, off=0):
        fs = [_feat(mk, e.symbol, int(e.entry.ts.timestamp()) + off, e.entry.price) for e in eps]
        fs = [f for f in fs if f]

        def med(k):
            xs = [f[k] for f in fs if f[k] is not None]
            return statistics.median(xs) * (100 if k < 2 else 1) if xs else None

        return {"n": len(fs), "ret24_med_pct": med(0), "ret72_med_pct": med(1), "pump48_med_pct": med(2)}

    def outcome(run, lot_filter):
        lots = [x for x in run.lots if lot_filter(x)]
        inv = sum(x.invested for x in lots)
        holds = [(x.exit_ts - int(x.entry_ts.timestamp())) / 86400 for x in lots if x.exit_ts]
        ratios = [x.add_usdt / x.entry_usdt for x in lots if x.entry_usdt and x.dca_adds]
        return {"lots": len(lots), "pnl_pct": 100 * sum(x.pnl for x in lots) / inv if inv else 0,
                "median_ret_pct": 100 * statistics.median([x.ret for x in lots]) if lots else None,
                "share_le_minus20": 100 * sum(x.ret <= -0.2 for x in lots) / max(1, len(lots)),
                "share_with_adds": 100 * sum(x.dca_adds > 0 for x in lots) / max(1, len(lots)),
                "adds_per_lot": sum(x.dca_adds for x in lots) / max(1, len(lots)),
                "add_entry_ratio_med": statistics.median(ratios) if ratios else None,
                "hold_days_med_closed": statistics.median(holds) if holds else None,
                "share_open_end": 100 * sum(x.open_at_end for x in lots) / max(1, len(lots))}

    in_ovl = lambda x: a <= int(x.entry_ts.timestamp()) + 1 < b  # noqa: E731
    # weekly cohort correlation (baseline)
    def weekly(run):
        wk: dict = {}
        for x in run.lots:
            if in_ovl(x):
                k = (int(x.entry_ts.timestamp()) - a) // (7 * DAY)
                p, i = wk.get(k, (0.0, 0.0))
                wk[k] = (p + x.pnl, i + x.invested)
        return {k: 100 * p / i for k, (p, i) in wk.items() if i > 0}

    wp, wt = weekly(runs_proxy_base), weekly(tape_base)
    ks = sorted(set(wp) & set(wt))
    corr = float(np.corrcoef([wp[k] for k in ks], [wt[k] for k in ks])[0, 1]) if len(ks) > 3 else None
    return {
        "period": [OVL_START.isoformat(), OVL_END.isoformat()],
        "entries_per_day": {"real_all_tenants": len(real) / ndays, "real_default": sum(e.tenant == "default" for e in real) / ndays,
                            "real_gainer_sources": len(real_g) / ndays, "proxy_raw_signals": len(rs) / ndays,
                            "proxy_entries_K": len(prox) / ndays},
        "real_entries_with_candles_pct": covered,
        "coins": {"real": len(rc), "proxy": len(pc), "both": len(rc & pc), "jaccard": len(rc & pc) / max(1, len(rc | pc))},
        "timing_recall_pct": {"all_real": recall(real), "gainer_sources": recall(real_g),
                              "per_source": {s: recall([e for e in real if e.entry.source == s])
                                             for s in sorted({e.entry.source for e in real})}},
        "timing_precision_pct": prec,
        "pre_entry_features": {"real_all": feats(real, 1), "real_gainer": feats(real_g, 1), "proxy": feats(prox, 1)},
        "outcome_baseline": {"proxy_rule_dca_tp_exit": outcome(runs_proxy_base, in_ovl),
                             "tape_recorded_adds_and_exits": outcome(tape_base, lambda x: True),
                             "tape_entries_rule_dca": outcome(tape_rule, lambda x: True)},
        "weekly_cohort_corr": corr, "weekly_n": len(ks),
    }


def tape_eval(tape_runs: dict):
    out = {}
    base = tape_runs["baseline"]
    srcs = sorted({x.source for x in base.lots})
    for v in VARIANTS:
        run = tape_runs[v]
        lots = run.lots
        inv = sum(x.invested for x in lots)
        row = {"lots": len(lots), "pnl_usdt": sum(x.pnl for x in lots), "pnl_pct": 100 * sum(x.pnl for x in lots) / inv if inv else 0,
               "invested": inv, "share_le_minus20": 100 * sum(x.ret <= -0.2 for x in lots) / max(1, len(lots)),
               "skipped_adds": sum(x.skipped_adds for x in lots), "time_stops": sum(x.time_stops for x in lots),
               "filtered_out": run.filtered_out}
        wins = []
        for lab, wa, wb in ENTRY_WINDOWS:
            def w(r):
                ls = [x for x in r.lots if wa <= x.entry_ts < wb]
                i = sum(x.invested for x in ls)
                return (100 * sum(x.pnl for x in ls) / i if i else 0.0, sum(x.pnl for x in ls))
            row[lab] = w(run)
            wins.append(w(run)[0] > w(base)[0])
        row["windows_won"] = sum(wins)
        per = {}
        for s in srcs:
            ls = [x for x in lots if x.source == s]
            i = sum(x.invested for x in ls)
            per[s] = {"lots": len(ls), "pnl_usdt": sum(x.pnl for x in ls), "pnl_pct": 100 * sum(x.pnl for x in ls) / i if i else 0}
        row["per_source"] = per
        out[v] = row
    return out


def random_gate_eval(rnd_runs: dict, real_runs: dict, tier="base"):
    out = {}
    months = month_keys()
    a0 = _mstart(*months[0])
    dkeys = list(range(a0, END, DAY))
    base_d = daily_returns(real_runs["baseline"], dkeys)
    for v in ("c_breadth40", "rg_breadth"):
        real_ex = sum(x - b for x, b in zip(daily_returns(real_runs[v], dkeys), base_d))
        real_usdt = period_stats(real_runs[v], a0, END)["pnl_usdt"]
        rs = [(k, r) for k, r in rnd_runs.items() if k[1] == v]
        ex = [sum(x - b for x, b in zip(daily_returns(r, dkeys), base_d)) for _k, r in rs]
        us = [period_stats(r, a0, END)["pnl_usdt"] for _k, r in rs]
        out[v] = {"n_random": len(rs), "real_excess_bp_per_day": 1e4 * real_ex / len(dkeys),
                  "random_excess_bp_median": 1e4 * statistics.median(ex) / len(dkeys) if ex else None,
                  "real_rank_vs_random": sum(e < real_ex for e in ex) / len(ex) if ex else None,
                  "real_pnl_usdt": real_usdt, "random_pnl_usdt_median": statistics.median(us) if us else None,
                  "real_usdt_rank_vs_random": sum(u < real_usdt for u in us) / len(us) if us else None}
    return out


# --------------------------------------------------------------------------- main
def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO_ROOT / "docs" / "backtests"))
    ap.add_argument("--random-seeds", type=int, default=20)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--render", help="only render markdown from this json")
    ap.add_argument("--tape-only", action="store_true")
    a = ap.parse_args(argv)
    if a.render:
        rep = json.loads(Path(a.render).read_text())
        Path(a.render).with_suffix(".md").write_text(render_md(rep))
        return rep
    build_world()
    mk = W["mk"]
    print("world", len(mk.candles), "pairs", len(mk.universe), "universe", len(W["proxy"]), "proxy entries",
          len(W["signals"]), "signals", f"{W['built_s']:.0f}s", flush=True)
    if a.tape_only:
        tape_phase(Path(a.out), a.procs, a.random_seeds)
        return None
    if a.quick:
        W["proxy"] = W["proxy"][-300:]
        W["proxy2"] = W["proxy2"][-300:]
    jobs = [(k, v, t, None) for k in ("proxy", "proxy2") for t in ("base", "stress") for v in VARIANTS]
    jobs += [("tape", v, t, None) for t in ("base", "stress") for v in VARIANTS]
    jobs += [("tape_rule", "baseline", "base", None)]
    jobs += [("proxy", v, "base", s) for v in ("c_breadth40", "rg_breadth") for s in range(1, a.random_seeds + 1)]
    res = run_jobs(jobs, a.procs)
    report = {"generated": datetime.now(UTC).isoformat(), "head": os.popen(f"git -C {REPO_ROOT} rev-parse --short=8 HEAD").read().strip(),
              "constants": {"RET24_MIN": RET24_MIN, "RELVOL_MIN": RELVOL_MIN, "K_PER_DAY": K_PER_DAY, "ENTRY_USDT": ENTRY_USDT,
                            "TP_MIN_GAIN": TP_MIN_GAIN, "WEAK": WEAK, "STRONG": STRONG, "N_ALL": N_ALL},
              "data": {"pairs": len(mk.candles), "universe": len(mk.universe), "proxy_entries": len(W["proxy"]),
                       "raw_signals": len(W["signals"]),
                       "first_real_1h_median": statistics.median([v for v in mk.first_real.values() if v]),
                       "breadth_label_share": {k: float(np.mean([mk.label_at(mk.grid0 + (j + 1) * HOUR) == k for j in range(len(mk.breadth))])) for k in ("weak", "mid", "strong")}}}
    _f, shares, block = _random_labels(mk, 0)
    report["random_labels"] = {"shares": shares, "block_hours": block}
    report["data"]["proxy2_entries"] = len(W["proxy2"])
    for tier in ("base", "stress"):
        for k in ("proxy", "proxy2"):
            pr = {v: res[(k, v, tier, None)] for v in VARIANTS}
            report[f"{k}_{tier}"] = evaluate_proxy(pr, tier)
            report[f"{k}_{tier}_fine_only"] = evaluate_proxy(pr, tier, first_month=(2025, 10))
        report[f"tape_{tier}"] = tape_eval({v: res[("tape", v, tier, None)] for v in VARIANTS})
    report["faithfulness"] = faithfulness(res[("proxy", "baseline", "base", None)], res[("tape", "baseline", "base", None)],
                                          res[("tape_rule", "baseline", "base", None)])
    report["faithfulness_p2"] = faithfulness(res[("proxy2", "baseline", "base", None)], res[("tape", "baseline", "base", None)],
                                             res[("tape_rule", "baseline", "base", None)], which="proxy2")
    report["random_gate"] = random_gate_eval({k: r for k, r in res.items() if k[3] is not None},
                                             {v: res[("proxy", v, "base", None)] for v in VARIANTS})
    # proxy vs tape delta sign agreement (overlap cohort, base)
    a0, b0 = int(OVL_START.timestamp()), int(OVL_END.timestamp())
    def coh(r):
        return sum(x.pnl for x in r.lots if a0 <= int(x.entry_ts.timestamp()) + 1 < b0)

    for k, key in (("proxy", "faithfulness"), ("proxy2", "faithfulness_p2")):
        agree = {}
        for v in VARIANTS[1:]:
            dp = coh(res[(k, v, "base", None)]) - coh(res[(k, "baseline", "base", None)])
            dt = sum(x.pnl for x in res[("tape", v, "base", None)].lots) - sum(x.pnl for x in res[("tape", "baseline", "base", None)].lots)
            agree[v] = {"proxy_delta_usdt": dp, "tape_delta_usdt": dt, "same_sign": (dp > 0) == (dt > 0)}
        report[key]["variant_delta_sign"] = agree
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / "2026-11-01-longwf-dcacap.json"
    p.write_text(json.dumps(report, indent=1, default=str))
    report = json.loads(p.read_text())
    (outdir / "2026-11-01-longwf-dcacap.md").write_text(render_md(report))
    print("wrote", p)
    return report



# --------------------------------------------------------------------------- markdown
def _f(x, nd=1):
    return "-" if x is None else (f"{x:,.{nd}f}" if isinstance(x, (int, float)) else str(x))


def render_md(r: dict) -> str:
    L = []
    c = r["constants"]
    L += ["# Walk-Forward lang + DCA-Deckel (2026-11-01)", "",
          f"Harness `python -m backtest.harness.longwf`, code at `{r['head']}`, generated {r['generated']} (UTC).", "",
          "## Method", "",
          "* Data: public Gate.io spot candles (no keys). 1h is served only for the last 10,000 hours (from about "
          "2025-09-10); before that each 4h bar is split into four synthetic 1h bars (path O-L-H-C on green bars, "
          "O-H-L-C on red, volume / 4). Results are shown for the full test period (Mar 2025 - Oct 2026, 20 test months) "
          "and for the real-1h part only (Oct 2025 - Oct 2026, 13 test months).",
          f"* Universe: {r['data']['universe']} USDT pairs (coins the bot traded or watched for breadth, still listed today). "
          "Survivorship bias: coins delisted before today are missing, which flatters long-history results.",
          f"* Proxy entries P1 (pre-registered, Freqtrade cross-check rule): 24h return > {100*c['RET24_MIN']:.0f} %, "
          f"24h quote volume > {c['RELVOL_MIN']:.1f}x its 6-day baseline, green bar; one signal per coin per 24 h; "
          f"max {c['K_PER_DAY']} entries per Berlin day, first come; {c['ENTRY_USDT']:.0f} USDT (median recorded entry), "
          "tenant default params. Robustness proxy P2: 24h return > 3 %, relvol > 1.5 (broader, closer to recorded entry features).",
          "* Adds: real `should_dca` on 1h bars (rule mode, tenant default DCA config). Stops: real `evaluate_long_hard_stop`. "
          f"Proxy take-profit: full sell at the close when close >= upper BB(20,2) and >= avg x {c['TP_MIN_GAIN']} "
          "(stands in for the recorded bb_upper/trailing exits). No new lot on a coin while a simulated lot is open.",
          f"* Breadth: share of the universe with close above its own 168h mean, from bars closed before the decision; "
          f"weak < {100*c['WEAK']:.0f} %, strong > {100*c['STRONG']:.0f} %. Label shares over the period: "
          + ", ".join(f"{k} {100*v:.0f} %" for k, v in r["data"]["breadth_label_share"].items()) + ".",
          "* Walk-forward: rolling 2 months train / 1 month test. Fixed variants have no parameters to fit, so the train "
          "months only feed the WF selector (best variant by return on deployed capital over the 2 train months, applied "
          "in the test month; daily returns spliced, an approximation because each variant runs as its own book).",
          f"* Statistics: DSR with N = {c['N_ALL']} (50 earlier harness trials + 8 new), 5-day block bootstrap CI of the "
          "daily excess return on deployed capital, PBO by CSCV (10 blocks) over all 8 variants, Monte-Carlo trade "
          "shuffle (2000) of lot PnL for drawdown. Costs: fee 0.2 %/side + class slippage, base and stress.",
          "* Rule 4 (release framework), applied per variant: GO only if >= 2/3 of test months better (return on deployed "
          "capital), DSR > 0.5, bootstrap CI low > 0, no guardrail (open lots <= -20 %, max DD, capital in losing lots) "
          "> 1 % worse. Otherwise KILL (months or guardrail fail) or OBSERVE (months ok, statistics not).", ""]
    for key, title in (("faithfulness", "P1"), ("faithfulness_p2", "P2")):
        f = r[key]
        e = f["entries_per_day"]
        L += [f"## Proxy faithfulness {title} (overlap {f['period'][0][:10]} - {f['period'][1][:10]})", "",
              f"* Entries/day: real all tenants {e['real_all_tenants']:.1f}, real default {e['real_default']:.1f}, "
              f"real gainer sources {e['real_gainer_sources']:.1f}; proxy raw signals {e['proxy_raw_signals']:.1f}, proxy after cap {e['proxy_entries_K']:.1f}.",
              f"* Coins: real {f['coins']['real']}, proxy {f['coins']['proxy']}, both {f['coins']['both']}, Jaccard {f['coins']['jaccard']:.2f}.",
              f"* Timing (same coin, +-24 h): recall of real entries {f['timing_recall_pct']['all_real']:.0f} % "
              f"(gainer sources {f['timing_recall_pct']['gainer_sources']:.0f} %), precision of proxy entries {f['timing_precision_pct']:.0f} %.",
              "* Recall by source: " + ", ".join(f"{k} {v:.0f} %" for k, v in f["timing_recall_pct"]["per_source"].items()) + ".",
              "", "| pre-entry (median) | real all | real gainer | proxy |", "|---|---|---|---|"]
        pf = f["pre_entry_features"]
        for k, lab in (("ret24_med_pct", "24h return %"), ("ret72_med_pct", "72h return %"), ("pump48_med_pct", "48h pump vs low %")):
            L.append(f"| {lab} | {_f(pf['real_all'][k])} | {_f(pf['real_gainer'][k])} | {_f(pf['proxy'][k])} |")
        L += ["", "| baseline outcome | lots | PnL % | median lot % | lots <= -20 % | lots with adds % | adds/lot | adds/entry (median) | hold d (closed, median) | open at end % |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for k, lab in (("proxy_rule_dca_tp_exit", "proxy, rule DCA, proxy TP"), ("tape_recorded_adds_and_exits", "tape, recorded adds+exits"),
                       ("tape_entries_rule_dca", "tape entries, rule DCA")):
            o = f["outcome_baseline"][k]
            L.append(f"| {lab} | {o['lots']} | {_f(o['pnl_pct'],2)} | {_f(o['median_ret_pct'])} | {_f(o['share_le_minus20'])} | "
                     f"{_f(o['share_with_adds'])} | {_f(o['adds_per_lot'],2)} | {_f(o['add_entry_ratio_med'],2)} | {_f(o['hold_days_med_closed'],2)} | {_f(o['share_open_end'])} |")
        L += ["", f"Weekly cohort PnL % correlation proxy vs tape: {_f(f['weekly_cohort_corr'],2)} (n = {f['weekly_n']} weeks).", "",
              "| variant | proxy delta USDT (overlap cohort) | tape delta USDT | same sign |", "|---|---|---|---|"]
        for v, d in f["variant_delta_sign"].items():
            L.append(f"| {v} | {_f(d['proxy_delta_usdt'],0)} | {_f(d['tape_delta_usdt'],0)} | {'yes' if d['same_sign'] else 'no'} |")
        L.append("")
    for tier in ("base", "stress"):
        for k, lab in (("proxy", "P1"), ("proxy2", "P2")):
            for suf, per in (("", "Mar 2025 - Oct 2026 (20 test months)"), ("_fine_only", "real 1h only, Oct 2025 - Oct 2026 (13 test months)")):
                e = r[f"{k}_{tier}{suf}"]
                L += [f"## Results {lab}, costs {tier}, {per}", "",
                      "| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
                for v, x in e["variants"].items():
                    if v == "baseline":
                        L.append(f"| baseline | {x['lots']} | {_f(x['pnl_usdt_curve'],0)} | {_f(x['avg_deployed'],0)} | {_f(x['roc_pct'],2)} | - | - | - | "
                                 f"{_f(x['share_lots_le_minus20'])} | {_f(x['max_dd_pct'])} | {_f(x['mc']['p95'],0)} | - | - |")
                    else:
                        L.append(f"| {v} | {x['lots']} | {_f(x['pnl_usdt_curve'],0)} | {_f(x['avg_deployed'],0)} | {_f(x['roc_pct'],2)} | "
                                 f"{x['windows_won']}/{x['windows']} / {x['windows_won_usdt']}/{x['windows']} | {_f(x['dsr'],2)} | "
                                 f"{_f(x['excess_bp'],2)} [{_f(x['ci_low_bp'],2)}, {_f(x['ci_high_bp'],2)}] | {_f(x['share_lots_le_minus20'])} | "
                                 f"{_f(x['max_dd_pct'])} | {_f(x['mc']['p95'],0)} | {', '.join(x['guardrails_worse']) or '-'} | **{x['verdict']}** |")
                w = e["wf_selector"]
                L += ["", f"PBO (CSCV, {e['pbo']['splits']} splits): {e['pbo']['pbo']:.2f}. WF selector: won {w['windows_won']}/{w['windows']} months, "
                      f"DSR {w['dsr']:.2f}, excess {w['excess_bp']:.2f} bp/day [CI {w['ci_low_bp']:.2f}, {w['ci_high_bp']:.2f}], "
                      f"beats {100*w['random_switch_pct_rank']:.0f} % of 500 random monthly switches. Picks: "
                      + ", ".join(f"{m} {p}" for m, p in w["picks"].items()) + ".", ""]
            if tier == "base":
                L += [f"### Monthly return on deployed capital % ({lab}, base)", "", "| month | " + " | ".join(VARIANTS) + " |",
                      "|---|" + "---|" * len(VARIANTS)]
                e = r[f"{k}_base"]
                for m in e["months"]:
                    L.append(f"| {m} | " + " | ".join(_f(e['variants'][v]['monthly'][m]['roc_pct'], 2) for v in VARIANTS) + " |")
                L.append("")
    rg = r["random_gate"]
    L += ["## Random on/off baselines (P1, base)", "",
          f"Random weak/mid/strong labels in blocks of {r['random_labels']['block_hours']} h with the empirical shares, "
          f"{rg['c_breadth40']['n_random']} seeds per variant.", "",
          "| variant | real excess bp/day | random median | real beats random % | real PnL USDT | random median USDT | real beats random (USDT) % |", "|---|---|---|---|---|---|---|"]
    for v, x in rg.items():
        L.append(f"| {v} | {_f(x['real_excess_bp_per_day'],2)} | {_f(x['random_excess_bp_median'],2)} | {_f(100*(x['real_rank_vs_random'] or 0),0)} | "
                 f"{_f(x['real_pnl_usdt'],0)} | {_f(x['random_pnl_usdt_median'],0)} | {_f(100*(x['real_usdt_rank_vs_random'] or 0),0)} |")
    for tier in ("base", "stress"):
        t = r[f"tape_{tier}"]
        L += ["", f"## Real tape (W1-W3 entries, recorded adds/exits), costs {tier}", "",
              "| variant | lots | PnL USDT | PnL % | W1 % | W2 % | W3 % | windows won | lots <= -20 % | adds skipped | time stops | filtered |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for v, x in t.items():
            L.append(f"| {v} | {x['lots']} | {_f(x['pnl_usdt'],0)} | {_f(x['pnl_pct'],2)} | {_f(x['W1'][0],2)} | {_f(x['W2'][0],2)} | {_f(x['W3'][0],2)} | "
                     f"{x['windows_won'] if v != 'baseline' else '-'}/3 | {_f(x['share_le_minus20'])} | {x['skipped_adds']} | {x['time_stops']} | {x['filtered_out']} |")
        if tier == "base":
            srcs = list(t["baseline"]["per_source"])
            L += ["", "### Per signal source, PnL USDT (tape, base)", "", "| source | lots | " + " | ".join(VARIANTS) + " |", "|---|---|" + "---|" * len(VARIANTS)]
            for s in srcs:
                L.append(f"| {s} | {t['baseline']['per_source'][s]['lots']} | " + " | ".join(_f(t[v]['per_source'].get(s, {}).get('pnl_usdt'), 0) for v in VARIANTS) + " |")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- tape-only phase (priority 2)
def tape_verdicts(res: dict, tier: str) -> dict:
    a, b = OVL_START, OVL_END
    dkeys = list(range(int(a.timestamp()) // DAY * DAY, int(b.timestamp()), DAY))
    runs = {v: res[("tape", v, tier, None)] for v in VARIANTS}
    daily = {v: daily_returns(runs[v], dkeys) for v in VARIANTS}
    full = {v: summarize(runs[v]) for v in VARIANTS}
    out = {}

    def wpct(r, wa, wb):
        ls = [x for x in r.lots if wa <= x.entry_ts < wb]
        i = sum(x.invested for x in ls)
        return 100 * sum(x.pnl for x in ls) / i if i else 0.0

    for v in VARIANTS:
        lots = sorted(runs[v].lots, key=lambda x: x.entry_ts)
        row = {"pnl_usdt": sum(x.pnl for x in lots), "invested": sum(x.invested for x in lots),
               "roc_pct": period_stats(runs[v], int(a.timestamp()), int(b.timestamp()))["roc_pct"],
               "max_dd_pct": full[v].max_dd_pct, "share_open_le_minus20": full[v].share_open_le_minus20,
               "capital_in_losing_lots": full[v].capital_in_losing_lots,
               "mc": mc_shuffle_dd([x.pnl for x in lots])}
        if v != "baseline":
            wins = [wpct(runs[v], wa, wb) > wpct(runs["baseline"], wa, wb) for _l, wa, wb in ENTRY_WINDOWS]
            excess = [x - y for x, y in zip(daily[v], daily["baseline"])]
            worse = guardrails_worse(full["baseline"], full[v])
            vd = decide(window_wins=wins, excess_daily=excess, n_trials=N_ALL, worse=worse)
            row.update(windows_won=sum(wins), dsr=vd.dsr, ci_low_bp=1e4 * vd.ci_low, ci_high_bp=1e4 * vd.ci_high,
                       excess_bp=1e4 * statistics.fmean(excess), guardrails_worse=worse, verdict=vd.label, why=vd.reason)
        out[v] = row
    matrix = [[daily[v][t] for v in VARIANTS] for t in range(len(dkeys))]
    pbo = stats.pbo_cscv(matrix, n_blocks=8)
    return {"variants": out, "pbo": pbo.pbo, "pbo_splits": pbo.n_splits, "days": len(dkeys)}


def tape_random(res: dict) -> dict:
    base = sum(x.pnl for x in res[("tape", "baseline", "base", None)].lots)
    out = {}
    for v in ("c_breadth40", "rg_breadth"):
        real = sum(x.pnl for x in res[("tape", v, "base", None)].lots) - base
        rnd = [sum(x.pnl for x in r.lots) - base for k, r in res.items() if k[0] == "tape" and k[1] == v and k[3] is not None]
        out[v] = {"real_delta_usdt": real, "n_random": len(rnd),
                  "random_delta_median": statistics.median(rnd) if rnd else None,
                  "random_delta_p05_p95": [sorted(rnd)[int(0.05 * len(rnd))], sorted(rnd)[int(0.95 * len(rnd)) - 1]] if rnd else None,
                  "real_beats_random_share": sum(real > x for x in rnd) / len(rnd) if rnd else None}
    return out


def tape_phase(out_dir: Path, procs: int, seeds: int) -> dict:
    jobs = [("tape", v, t, None) for t in ("base", "stress") for v in VARIANTS]
    jobs += [("tape_rule", "baseline", "base", None)]
    jobs += [("tape", v, "base", s) for v in ("c_breadth40", "rg_breadth") for s in range(1, seeds + 1)]
    res = run_jobs(jobs, procs)
    rep = {"generated": datetime.now(UTC).isoformat(), "n_trials": N_ALL,
           "head": os.popen(f"git -C {REPO_ROOT} rev-parse --short=8 HEAD").read().strip(),
           "tape_base": tape_eval({v: res[("tape", v, "base", None)] for v in VARIANTS}),
           "tape_stress": tape_eval({v: res[("tape", v, "stress", None)] for v in VARIANTS}),
           "verdict_base": tape_verdicts(res, "base"), "verdict_stress": tape_verdicts(res, "stress"),
           "random_gate": tape_random(res)}
    _f2, shares, block = _random_labels(W["mk"], 0)
    rep["random_labels"] = {"shares": shares, "block_hours": block}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "2026-11-01-dcacap-tape.json").write_text(json.dumps(rep, indent=1, default=str))
    (out_dir / "2026-11-01-dcacap-tape.md").write_text(render_tape_md(rep))
    return res


def render_tape_md(r: dict) -> str:
    L = ["# DCA-Deckel auf den echten Einstiegen Jul-Okt 2026 (2026-11-01)", "",
         f"Code `{r['head']}`. Recorded entries W1-W3 (29.07.-23.10.2026, followed to tape end 30.10.), recorded adds and exits; "
         "variants skip recorded adds beyond the cap, filter entries in cooldown, close at the time stop. "
         f"DSR N = {r['n_trials']}. Rule 4: GO needs >= 2 of 3 windows, DSR > 0.5, CI low > 0, no guardrail > 1 % worse.", ""]
    for tier in ("base", "stress"):
        vd = r[f"verdict_{tier}"]
        t = r[f"tape_{tier}"]
        L += [f"## Costs {tier} (PBO {vd['pbo']:.2f}, {vd['pbo_splits']} splits, {vd['days']} days)", "",
              "| variant | lots | PnL USDT | PnL % | W1 % | W2 % | W3 % | windows | DSR | excess bp/day [CI] | max DD % | open<=-20 % | capital in losers | MC DD p95 | guardrails worse | verdict |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for v in VARIANTS:
            x, y = t[v], vd["variants"][v]
            if v == "baseline":
                L.append(f"| baseline | {x['lots']} | {_f(x['pnl_usdt'],0)} | {_f(x['pnl_pct'],2)} | {_f(x['W1'][0],2)} | {_f(x['W2'][0],2)} | {_f(x['W3'][0],2)} | - | - | - | "
                         f"{_f(y['max_dd_pct'])} | {_f(y['share_open_le_minus20'])} | {_f(y['capital_in_losing_lots'],0)} | {_f(y['mc']['p95'],0)} | - | - |")
            else:
                L.append(f"| {v} | {x['lots']} | {_f(x['pnl_usdt'],0)} | {_f(x['pnl_pct'],2)} | {_f(x['W1'][0],2)} | {_f(x['W2'][0],2)} | {_f(x['W3'][0],2)} | "
                         f"{y['windows_won']}/3 | {_f(y['dsr'],2)} | {_f(y['excess_bp'],1)} [{_f(y['ci_low_bp'],1)}, {_f(y['ci_high_bp'],1)}] | {_f(y['max_dd_pct'])} | "
                         f"{_f(y['share_open_le_minus20'])} | {_f(y['capital_in_losing_lots'],0)} | {_f(y['mc']['p95'],0)} | {', '.join(y['guardrails_worse']) or '-'} | **{y['verdict']}** |")
        L.append("")
    L += ["## Random on/off baseline (base)", "", f"Block-random breadth labels, block {r['random_labels']['block_hours']} h, shares "
          + ", ".join(f"{k} {100*v:.0f} %" for k, v in r["random_labels"]["shares"].items()) + ".", "",
          "| variant | real delta USDT | random median | random p5/p95 | real beats random |", "|---|---|---|---|---|"]
    for v, x in r["random_gate"].items():
        pp = x["random_delta_p05_p95"] or [None, None]
        L.append(f"| {v} | {_f(x['real_delta_usdt'],0)} | {_f(x['random_delta_median'],0)} | {_f(pp[0],0)} / {_f(pp[1],0)} | {_f(100*(x['real_beats_random_share'] or 0),0)} % |")
    t = r["tape_base"]
    srcs = list(t["baseline"]["per_source"])
    L += ["", "## Per signal source, PnL USDT (base)", "", "| source | lots | " + " | ".join(VARIANTS) + " |", "|---|---|" + "---|" * len(VARIANTS)]
    for s_ in srcs:
        L.append(f"| {s_} | {t['baseline']['per_source'][s_]['lots']} | " + " | ".join(_f(t[v]['per_source'].get(s_, {}).get('pnl_usdt'), 0) for v in VARIANTS) + " |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main(sys.argv[1:])
