"""Pre-registered scenarios (a)-(d). Definitions are fixed here, before the run.

(a) exit/DCA bug fixes #733 + #738 on the October book (counterfactual)
(b) volatility-based position sizing on recorded entries
(c) SMA50 trend filter + distance filter (Herman) on recorded entries
(d) PR #740 coin / source denylist on recorded entries

Every variant runs the bot's own ``strategies.dca`` stop code. Book mode
starts each window from the tape's own lot state; entry mode takes lots
entered inside a window and follows them to the end of the tape.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from tools import entry_filter_replay as efr

from . import stats
from .costs import CostModel
from .data import CandleCache, load_book, load_fills, load_tenant_params
from .engine import Engine, Episode, Policy, build_episodes
from .metrics import RunMetrics, Verdict, daily_returns, decide, guardrails_worse, summarize
from .policy import REPO_ROOT, SimClock, import_real, load_module, resolve_ref

UTC = timezone.utc
DAY = 86400

PRE_733_738 = "0319ceac"  # staging before #733 (and with #738 not yet in the WS path)
PR740_REF = "refs/remotes/origin/pr740"  # cursor/symbol-source-denylist-5e8f
TAPE_END = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)  # last filled order 06:29 Berlin, Oct 30

# (a) October split in three ~equal windows (UTC)
OCT_WINDOWS = (
    ("O1", datetime(2026, 10, 3, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC)),
    ("O2", datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)),
    ("O3", datetime(2026, 10, 21, tzinfo=UTC), TAPE_END),
)


def _berlin_window(label: str, first: str, last: str) -> tuple[str, datetime, datetime]:
    nxt = date.fromordinal(date.fromisoformat(last).toordinal() + 1).isoformat()
    return (label, efr.day_start_berlin(first), efr.day_start_berlin(nxt))


# (b)-(d) the #744 walk-forward windows (Berlin days, inclusive)
ENTRY_WINDOWS = tuple(_berlin_window(*w) for w in efr.WINDOWS)

# Every candidate variant evaluated by this harness run (3 per scenario).
# DSR deflates each primary candidate against all of them.
N_TRIALS = 50  # 12 from (a)-(d) + 11 loser-coin candidates (e)-(h) + 27 regime-conditional (r_*), 2026-11-01


@dataclass
class Context:
    engine: Engine
    episodes: list[Episode]
    cache: CandleCache
    notes: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)


def build_context(data_dir: Path, candle_dir: Path, *, cost_tier: str = "base") -> Context:
    fills = load_fills(data_dir / "orders_v2_filled_alltime.jsonl")
    book = load_book(data_dir / "positions_current.jsonl")
    tenants = load_tenant_params(data_dir / "tenant_configs.jsonl", REPO_ROOT / "config.json")
    cache = CandleCache(candle_dir)
    memo: dict[str, list] = {}

    def candles(symbol: str):
        if symbol not in memo:
            memo[symbol] = cache.get(symbol, "1h")
        return memo[symbol]

    engine = Engine(
        candles_1h=candles,
        tenants=tenants,
        book=book,
        positions_mod=import_real("strategies.positions"),
        costs=CostModel(tier=cost_tier),
    )
    episodes = build_episodes(fills)
    ctx = Context(engine, episodes, cache)
    ctx.sources = {
        "fills": len(fills),
        "first_fill": min(f.ts for f in fills).isoformat(),
        "last_fill": max(f.ts for f in fills).isoformat(),
        "episodes": len(episodes),
        "open_episodes_at_tape_end": sum(e.closed_at is None for e in episodes),
        "book_lots": len(book),
        "tenants": sorted(tenants),
        "cost_tier": cost_tier,
        "staging_sha": _head(),
        "reconciliation": _reconcile(book, episodes),
    }
    return ctx


def _reconcile(book, episodes: list[Episode]) -> str:
    real = {
        k
        for k, v in book.items()
        if k[0] != "decisions" and float(v.raw.get("amount") or 0) * float(v.raw.get("average_entry") or 0) > 1
    }
    tape = {e.key for e in episodes if e.closed_at is None}
    return (
        f"open lots in positions export {len(real)}, open on tape {len(tape)}, both {len(real & tape)}, "
        f"export only {len(real - tape)}, tape only {len(tape - real)}"
    )


def _head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def policy(name: str, *, ref: str | None, escalate: bool, **kw) -> Policy:
    clock = SimClock()
    module = load_module("strategies.dca", ref=ref, clock=clock)
    return Policy(name, module, clock, escalate_rejected_hard_partial=escalate, **kw)


# ---------------------------------------------------------------------------
@dataclass
class VariantResult:
    name: str
    description: str
    full: RunMetrics
    windows: dict[str, RunMetrics]
    daily: list[float]


@dataclass
class ScenarioResult:
    key: str
    title: str
    question: str
    mode: str
    baseline: str
    primary: str
    variants: list[VariantResult]
    verdict: Verdict
    pbo: float | None
    pbo_splits: int
    window_labels: list[tuple[str, str, str]]
    notes: list[str]
    excess_daily_mean_bp: float

    def to_json(self) -> dict:
        d = asdict(self)
        return d


def _day_keys(a: datetime, b: datetime) -> list[int]:
    s = int(a.timestamp()) // DAY * DAY
    e = int(b.timestamp())
    return list(range(s, e, DAY))


def _evaluate(
    ctx: Context,
    *,
    key: str,
    title: str,
    question: str,
    mode: str,
    windows,
    variants: list[tuple[Policy, str]],
    baseline: str,
    primary: str,
    informational: bool = False,
    notes: list[str] | None = None,
) -> ScenarioResult:
    eng, eps = ctx.engine, ctx.episodes
    start, end = windows[0][1], windows[-1][2]
    days = _day_keys(start, end if mode == "book" else TAPE_END)
    carry = mode == "book"
    results: list[VariantResult] = []
    for pol, desc in variants:
        full = eng.run(pol, eps, start, end if carry else end, carry_in=carry, horizon_end=TAPE_END)
        per = {}
        for label, a, b in windows:
            per[label] = summarize(eng.run(pol, eps, a, b, carry_in=carry, horizon_end=TAPE_END))
        results.append(VariantResult(pol.name, desc, summarize(full), per, daily_returns(full, days)))
    by = {r.name: r for r in results}
    base, cand = by[baseline], by[primary]
    wins = [cand.windows[w].pnl_pct > base.windows[w].pnl_pct for w, _a, _b in windows]
    excess = [c - b for c, b in zip(cand.daily, base.daily)]
    worse = guardrails_worse(base.full, cand.full)
    verdict = decide(window_wins=wins, excess_daily=excess, n_trials=N_TRIALS, worse=worse, informational=informational)
    matrix = [[r.daily[t] for r in results] for t in range(len(days))]
    try:
        pbo = stats.pbo_cscv(matrix, n_blocks=8)
        pbo_v, pbo_n = pbo.pbo, pbo.n_splits
    except ValueError:
        pbo_v, pbo_n = None, 0
    return ScenarioResult(
        key,
        title,
        question,
        mode,
        baseline,
        primary,
        results,
        verdict,
        pbo_v,
        pbo_n,
        [(w, a.isoformat(), b.isoformat()) for w, a, b in windows],
        list(notes or []),
        1e4 * (sum(excess) / len(excess)) if excess else 0.0,
    )


# ---------------------------------------------------------------------------
def scenario_a(ctx: Context) -> ScenarioResult:
    pre = resolve_ref(PRE_733_738)
    variants = [
        (policy("before", ref=pre, escalate=False), f"strategies/dca.py at {pre}, rejected hard partial stays rejected"),
        (policy("733_only", ref=None, escalate=False), "#733 grace/stop code, no #738 escalation"),
        (policy("738_only", ref=pre, escalate=True), "pre-#733 code, #738 escalation of rejected hard partial"),
        (policy("after", ref=None, escalate=True), "staging: #733 + #738"),
    ]
    tape_stops = _tape_stop_counts(ctx, OCT_WINDOWS[0][1], OCT_WINDOWS[-1][2])
    notes = [
        f"Tape stop fills in the same span: {tape_stops['partial']} SELL_STOP_PARTIAL, {tape_stops['full']} SELL_STOP_FULL "
        "(calibration for the 'before' variant).",
        "Adds and non-stop sells are replayed from the tape; a lot the policy already closed ignores later tape events.",
    ]
    return _evaluate(
        ctx,
        key="a",
        title="Exit/DCA fixes #733 + #738 on the October book",
        question="Would the October losses have been smaller with #733 and #738 live?",
        mode="book",
        windows=OCT_WINDOWS,
        variants=variants,
        baseline="before",
        primary="after",
        informational=True,
        notes=notes,
    )


def _tape_stop_counts(ctx: Context, a: datetime, b: datetime) -> dict[str, int]:
    out = {"partial": 0, "full": 0}
    for ep in ctx.episodes:
        for f in ep.events:
            if a <= f.ts < b and f.side == "sell":
                if f.signal == "SELL_STOP_PARTIAL":
                    out["partial"] += 1
                elif f.signal == "SELL_STOP_FULL":
                    out["full"] += 1
    return out


def _signal_unix(ep: Episode) -> int:
    return int(ep.entry.ts.timestamp())


def _vol_multiplier(ctx: Context, target: float, missing: set) -> Callable[[Episode], float]:
    def mult(ep: Episode) -> float:
        rv = efr._rv_daily(ctx.engine.candles_1h(ep.symbol), _signal_unix(ep))
        if rv is None or rv <= 0:
            missing.add((ep.key, ep.entry.ts))
            return 1.0
        return min(1.0, target / rv)

    return mult


def scenario_b(ctx: Context) -> ScenarioResult:
    missing: set = set()
    variants = [(policy("baseline", ref=None, escalate=True), "recorded entry and add sizes")]
    for target in (0.03, efr.TARGET_DAILY_VOL, 0.08):
        variants.append(
            (
                policy(f"vol_{target:.2f}", ref=None, escalate=True, size_multiplier=_vol_multiplier(ctx, target, missing)),
                f"size x min(1, {target:.2f} / realized daily vol of the 24 h before entry), entry and its adds",
            )
        )
    res = _evaluate(
        ctx,
        key="b",
        title="Volatility-based position sizing",
        question="Do vol-scaled sizes improve return on deployed capital without worse guardrails?",
        mode="entries",
        windows=ENTRY_WINDOWS,
        variants=variants,
        baseline="baseline",
        primary=f"vol_{efr.TARGET_DAILY_VOL:.2f}",
        notes=["Primary target 0.05 is #744's pre-registered TARGET_DAILY_VOL; 0.03/0.08 are the PBO grid."],
    )
    res.notes.append(f"Lots without 24 h of hourly bars before entry kept size 1.0: {len(missing)}.")
    return res


def _trend_distance(ctx: Context, sma: int, missing: set) -> Callable[[Episode], bool | None]:
    def ok(ep: Episode) -> bool | None:
        unix = _signal_unix(ep)
        daily = ctx.cache.get(ep.symbol, "1d")
        above, ext, _ = efr._sma_above(daily, unix, sma)
        pump = efr._pump_24h(ctx.engine.candles_1h(ep.symbol), unix)
        if above is None or ext is None or pump is None:
            missing.add((sma, ep.key, ep.entry.ts))
            return None  # unknown: keep, same as the bot without data
        return bool(above) and ext <= efr.DISTANCE_X_PCT and pump <= efr.DISTANCE_Y_PCT

    return ok


def scenario_c(ctx: Context) -> ScenarioResult:
    missing: set = set()
    variants = [(policy("baseline", ref=None, escalate=True), "all recorded entries")]
    for sma in (efr.SMA_GRID[0], efr.SMA_PRIMARY, efr.SMA_GRID[1]):
        variants.append(
            (
                policy(f"sma{sma}_dist", ref=None, escalate=True, entry_filter=_trend_distance(ctx, sma, missing)),
                f"enter only if daily close > SMA{sma}, extension <= {efr.DISTANCE_X_PCT:.0f} %, 24 h pump <= {efr.DISTANCE_Y_PCT:.0f} %",
            )
        )
    res = _evaluate(
        ctx,
        key="c",
        title="Trend filter (SMA50 daily) + distance filter",
        question="Does skipping entries below SMA50 or too far extended improve results with real exits/DCA?",
        mode="entries",
        windows=ENTRY_WINDOWS,
        variants=variants,
        baseline="baseline",
        primary=f"sma{efr.SMA_PRIMARY}_dist",
        notes=["Same features as tools/entry_filter_replay.py (#744); here lots run with the real stop code and recorded adds/sells."],
    )
    primary_missing = sum(1 for m in missing if m[0] == efr.SMA_PRIMARY)
    res.notes.append(
        f"Lots without enough daily/hourly history for SMA{efr.SMA_PRIMARY} (young listings, tokenized stocks) were kept: "
        f"{primary_missing}."
    )
    return res


def _pr740_risk() -> dict:
    blob = subprocess.run(
        ["git", "show", f"{PR740_REF}:config.json"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return json.loads(blob)["risk"]


def scenario_d(ctx: Context) -> ScenarioResult:
    deny = load_module("risk.denylist", ref=PR740_REF)
    risk = _pr740_risk()
    sym_only = {"symbol_denylist": risk.get("symbol_denylist", [])}
    src_only = {"source_denylist": risk.get("source_denylist", [])}
    hits: dict[str, set] = {"symbol": set(), "source": set()}

    def flt(cfg: dict, track: bool = False) -> Callable[[Episode], bool]:
        def ok(ep: Episode) -> bool:
            v = deny.evaluate_denylist(ep.symbol, [ep.entry.source, ep.entry.signal], cfg)
            if track:
                ident = (ep.key, ep.entry.ts)
                if "symbol_denylist" in v.codes:
                    hits["symbol"].add(ident)
                if "source_denylist" in v.codes:
                    hits["source"].add(ident)
            return not v.blocked

        return ok

    variants = [
        (policy("baseline", ref=None, escalate=True), "all recorded entries"),
        (policy("symbols_only", ref=None, escalate=True, entry_filter=flt(sym_only)), f"symbol_denylist {sym_only['symbol_denylist']}"),
        (policy("sources_only", ref=None, escalate=True, entry_filter=flt(src_only)), f"source_denylist {src_only['source_denylist']}"),
        (policy("pr740", ref=None, escalate=True, entry_filter=flt(risk, True)), "PR #740 config: both lists"),
    ]
    res = _evaluate(
        ctx,
        key="d",
        title="PR #740 coin/source denylist",
        question="Would blocking the #740 coins and the gainer_rank_entry source have improved results?",
        mode="entries",
        windows=ENTRY_WINDOWS,
        variants=variants,
        baseline="baseline",
        primary="pr740",
        notes=[f"evaluate_denylist loaded from {resolve_ref(PR740_REF)} (PR #740 head)."],
    )
    res.notes.append(
        f"Distinct lots blocked by PR #740 in W1-W3: symbol {len(hits['symbol'])}, source {len(hits['source'])}."
    )
    return res


# ---------------------------------------------------------------------------
# Loser-coin rules (2026-11-01, /workspace/umbau/loser_coins_analysis.md).
# Pre-registered before the run: primary thresholds are the ones from the
# analysis (-10 % in 72 h; -15 % flat stop without adds; 48 h pump > 15 %);
# the neighbours are sensitivity variants and count as trials for the DSR.


def _pump48_ok(ctx: Context, limit_pct: float, missing: set) -> Callable[[Episode], bool | None]:
    # Same 48 h pump definition as the live shadow rule (services/shadow_rules.py).
    from services.shadow_rules import pump_pct_from_lows

    def ok(ep: Episode) -> bool | None:
        hourly = ctx.engine.candles_1h(ep.symbol)
        sig = _signal_unix(ep)
        lows = [b[3] for b in hourly if sig - 48 * 3600 <= b[0] and b[0] + 3600 <= sig]
        pump = pump_pct_from_lows(lows, ep.entry.price)
        if pump is None:
            missing.add((limit_pct, ep.key, ep.entry.ts))
            return None  # not enough history: keep
        return pump <= limit_pct

    return ok


def _early(name: str, pct: float, hours: float = 72.0, **kw) -> Policy:
    return policy(name, ref=None, escalate=True, early_exit_pct=pct, early_exit_hours=hours, **kw)


def _flat(name: str, pct: float, **kw) -> Policy:
    return policy(name, ref=None, escalate=True, flat_stop_pct=pct, no_adds=True, **kw)


def _early_variants():
    return [
        (policy("baseline", ref=None, escalate=True), "staging stop/DCA code, recorded entries and adds"),
        (_early("early10_72h", 10.0), "full sell when the lot is -10 % vs avg within 72 h of entry; closed lot takes no further adds"),
        (_early("early8_72h", 8.0), "same, -8 %"),
        (_early("early12_72h", 12.0), "same, -12 %"),
    ]


def _flat_variants():
    return [
        (policy("baseline", ref=None, escalate=True), "staging stop/DCA code, recorded entries and adds"),
        (_flat("stop15_noadd", 15.0), "full stop at -15 % vs avg any time, no DCA/add buys at all"),
        (_flat("stop12_noadd", 12.0), "same, -12 %"),
        (_flat("stop20_noadd", 20.0), "same, -20 %"),
    ]


def scenario_e(ctx: Context) -> ScenarioResult:
    return _evaluate(ctx, key="e", title="Early exit: -10 % within 72 h -> full sell", question="Does cutting lots that fall 10 % in their first 72 h beat staging (Jul-Oct entries)?",
                     mode="entries", windows=ENTRY_WINDOWS, variants=_early_variants(), baseline="baseline", primary="early10_72h")


def scenario_f(ctx: Context) -> ScenarioResult:
    return _evaluate(ctx, key="f", title="Plain -15 % full stop, no DCA", question="Does a flat -15 % stop without any adds beat staging (Jul-Oct entries)?",
                     mode="entries", windows=ENTRY_WINDOWS, variants=_flat_variants(), baseline="baseline", primary="stop15_noadd")


def scenario_g(ctx: Context) -> ScenarioResult:
    missing: set = set()
    variants = [(policy("baseline", ref=None, escalate=True), "all recorded entries")]
    for lim in (15.0, 10.0, 20.0):
        variants.append((policy(f"nochase{lim:.0f}", ref=None, escalate=True, entry_filter=_pump48_ok(ctx, lim, missing)),
                         f"skip entry if price > {lim:.0f} % above the lowest 1h low of the 48 h before the signal"))
    res = _evaluate(ctx, key="g", title="No chasing: skip entry after >15 % 48 h pump", question="Does skipping entries after a 48 h pump > 15 % beat staging?",
                    mode="entries", windows=ENTRY_WINDOWS, variants=variants, baseline="baseline", primary="nochase15")
    res.notes.append(f"Entries kept for lack of 48 h history (primary): {sum(1 for m in missing if m[0] == 15.0)}.")
    return res


def scenario_h(ctx: Context) -> ScenarioResult:
    missing: set = set()
    nc = _pump48_ok(ctx, 15.0, missing)
    variants = [
        (policy("baseline", ref=None, escalate=True), "staging, all recorded entries"),
        (_early("early10+nochase15", 10.0, entry_filter=nc), "(e) early10_72h + (g) nochase15"),
        (_flat("stop15_noadd+nochase15", 15.0, entry_filter=nc), "(f) stop15_noadd + (g) nochase15"),
    ]
    return _evaluate(ctx, key="h", title="Combined: early exit + no chasing", question="Do the exit and entry rules together beat staging?",
                     mode="entries", windows=ENTRY_WINDOWS, variants=variants, baseline="baseline", primary="early10+nochase15")


def scenario_e_oct(ctx: Context) -> ScenarioResult:
    return _evaluate(ctx, key="e_oct", title="Early exit on the October book (check)", question="Same rule on the whole October book (lots carried in at window start).",
                     mode="book", windows=OCT_WINDOWS, variants=_early_variants(), baseline="baseline", primary="early10_72h",
                     notes=["Book mode: lots entered before a window keep their 72 h clock from the real entry, so carried-in lots older than 72 h only see the staging stops."])


def scenario_f_oct(ctx: Context) -> ScenarioResult:
    return _evaluate(ctx, key="f_oct", title="Flat -15 % stop, no DCA, on the October book (check)", question="Same rule on the whole October book.",
                     mode="book", windows=OCT_WINDOWS, variants=_flat_variants(), baseline="baseline", primary="stop15_noadd",
                     notes=["Book mode: a carried-in lot already below -15 % vs avg at window start is sold on the first bar."])


# ---------------------------------------------------------------------------
# Regime-conditional loser-coin rules (2026-11-01, second run).
# Regime known at decision time only (bars closed before the bar / the signal).
# Per regime definition: primary parameters + 2 shifted neighbours (count as trials).
import os as _os

from . import regime as _rg

_REGIME_CACHE: dict = {}


def _regimes(ctx: Context) -> dict[str, list]:
    if "series" in _REGIME_CACHE:
        return _REGIME_CACHE["series"]
    cdir = _os.environ.get("BT_CANDLES", "/workspace/bt-data/candles")
    I = _rg.load_inputs(
        cdir,
        _os.environ.get("BT_BREADTH", "/workspace/bt-data/candles_breadth"),
        _os.environ.get("BT_FUNDING", "/workspace/bt-data/btc_funding_gate.json"),
    )
    br = _rg.breadth_table(I["universe"])
    b1, e1, bd = I["btc_1h"], I["eth_1h"], I["btc_1d"]
    fund = I["funding"]
    series = {
        "oracle": [
            _rg.oracle_series(b1, e1, bd, breadth=br, funding=fund, name="oracle"),
            _rg.oracle_series(b1, e1, bd, breadth=br, funding=fund, risk_off_24h=-2.5,
                              breadth_risk_off_max_green=0.40, breadth_rotten_max_green=0.30, name="oracle_sensitive"),
            _rg.oracle_series(b1, e1, bd, breadth=br, funding=fund, risk_off_24h=-3.5,
                              breadth_risk_off_max_green=0.30, breadth_rotten_max_green=0.20, name="oracle_lax"),
        ],
        "sma": [_rg.sma_series(bd, b1, n=50), _rg.sma_series(bd, b1, n=40), _rg.sma_series(bd, b1, n=60)],
        "ret": [_rg.ret_series(b1, hours=168, name="ret7d"), _rg.ret_series(b1, hours=120, name="ret5d"),
                _rg.ret_series(b1, hours=240, name="ret10d")],
    }
    _REGIME_CACHE["series"] = series
    _REGIME_CACHE["breadth"] = br
    return series


def _nochase_regime(ctx: Context, ser: "_rg.Series", missing: set, off: float = 10.0, on: float = 20.0,
                    neutral: float = 15.0) -> Callable[[Episode], bool | None]:
    filters = {lim: _pump48_ok(ctx, lim, missing) for lim in (off, on, neutral)}

    def ok(ep: Episode) -> bool | None:
        lab = ser.at(_signal_unix(ep))
        if lab is None:
            lim = neutral
        elif lab in _rg.OFF_STATES or lab == "OFF":
            lim = off
        elif lab == "NEUTRAL":
            lim = neutral
        else:
            lim = on
        return filters[lim](ep)

    return ok


def _regime_variants(ctx: Context, fam: str, rule: str):
    missing: set = set()
    out = [(policy("baseline", ref=None, escalate=True), "staging, all recorded entries")]
    for ser in _regimes(ctx)[fam]:
        kw: dict = {}
        desc = []
        if rule in ("e", "h"):
            kw.update(early_exit_pct=10.0, early_exit_hours=72.0, early_exit_gate=ser.gate())
            desc.append(f"early exit -10 %/72 h only while {ser.name} is risk-off")
        if rule in ("g", "h"):
            kw["entry_filter"] = _nochase_regime(ctx, ser, missing)
            desc.append(f"no-chase 48 h pump limit 10 % if {ser.name} risk-off, 20 % risk-on (15 % NEUTRAL)")
        out.append((policy(f"{rule}_{ser.name}", ref=None, escalate=True, **kw), "; ".join(desc)))
    return out


_RTITLE = {"e": "Early exit only in risk-off", "g": "No-chase threshold by regime", "h": "Both (regime early exit + regime no-chase)"}
_RFAM = {"oracle": "bot Market Oracle state (RISK_OFF/CRASH)", "sma": "BTC daily close < SMA50", "ret": "BTC 7-day return < 0"}


def _regime_scenario(fam: str, rule: str):
    def run(ctx: Context) -> ScenarioResult:
        variants = _regime_variants(ctx, fam, rule)
        prim = variants[1][0].name
        return _evaluate(ctx, key=f"r_{rule}_{fam}", title=f"{_RTITLE[rule]} [{_RFAM[fam]}]",
                         question="Does making the loser-coin rule regime-conditional beat staging in all market phases?",
                         mode="entries", windows=ENTRY_WINDOWS, variants=variants, baseline="baseline", primary=prim,
                         notes=["Regime from BTC/ETH (and breadth/funding for oracle) bars closed before the decision bar."])
    return run


SCENARIOS: dict[str, Callable[[Context], ScenarioResult]] = {
    "e": scenario_e,
    "f": scenario_f,
    "g": scenario_g,
    "h": scenario_h,
    "e_oct": scenario_e_oct,
    "f_oct": scenario_f_oct,
    "a": scenario_a,
    "b": scenario_b,
    "c": scenario_c,
    "d": scenario_d,
}
for _fam in ("oracle", "sma", "ret"):
    for _rule in ("e", "g", "h"):
        SCENARIOS[f"r_{_rule}_{_fam}"] = _regime_scenario(_fam, _rule)
