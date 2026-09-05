#!/usr/bin/env python3
"""Re-evaluate Hermes experiments against CostModel 2026-09-v1 (#316).

Reads a **read-only** snapshot of `hermes/memory/*.json`. Re-runs the same
walk-forward validation HermesAgent used (created_at − backtest_days, then
`hermes.validation.run_walk_forward` + CostModel-aware `hermes.backtester`),
writes a tagged copy of the snapshot to `--out-dir` (never the input, never
`hermes/memory/`), and writes `docs/audit/hermes-recost.md`.

No live orders, no Hermes promotion, no writes to the input snapshot.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config import BotConfig, get_bot_config  # noqa: E402
from core.costs import COST_MODEL_VERSION  # noqa: E402
from hermes.backtester import Backtester  # noqa: E402
from hermes.goals import GoalEngine  # noqa: E402
from hermes.memory.store import DEFAULT_PARAMS, profile_key  # noqa: E402
from hermes.validation import run_walk_forward  # noqa: E402

REPO_ROOT = _ROOT
HERMES_MEMORY_DIR = REPO_ROOT / "hermes" / "memory"
LEGACY_COST_MODEL = "legacy"
METRIC_KEYS = ("realized_pnl", "sharpe", "win_rate", "trades")
EXPERIMENT_FILES = (
    ("experiments.json", "live", "baseline.json"),
    ("experiments.demo.json", "demo", "baseline.demo.json"),
)


FetchBars = Callable[[str, str, datetime, datetime], list]


class RecostError(SystemExit):
    """CLI abort; tests catch this as SystemExit."""


@dataclass
class RecostRow:
    experiment_id: str
    ledger: str
    symbol: str
    timeframe: str
    variable: str
    old_value: Any
    new_value: Any
    source: str
    created_at: str
    old_verdict: str
    new_verdict: str
    new_verdict_reason: str
    old_baseline: dict
    old_variant: dict
    new_baseline: dict
    new_variant: dict
    cost_model: str
    unresolvable: bool = False
    unresolvable_reason: str = ""
    folds_won: int = 0
    folds_total: int = 0
    pnl_delta: float = 0.0


@dataclass
class RecostSummary:
    rows: list[RecostRow] = field(default_factory=list)
    runtime_sec: float = 0.0
    input_dir: str = ""
    out_dir: str = ""
    considered: int = 0
    skipped_filter: int = 0
    cost_model: str = COST_MODEL_VERSION
    backtest_days: int = 14
    fold_days: int = 3
    step_days: int = 3
    min_bars_per_fold: int = 12
    validation_mode: str = "walk_forward"
    backtest_mode: str = "ta_only"


def _die(msg: str) -> None:
    raise RecostError(msg)


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def assert_out_dir_safe(out_dir: Path, input_dir: Path) -> None:
    """Refuse to write inside the snapshot or the live Hermes memory dir."""
    out = resolve_path(out_dir)
    inp = resolve_path(input_dir)
    mem = resolve_path(HERMES_MEMORY_DIR)
    if out == inp or is_inside(out, inp):
        _die(
            f"refusing to run: --out-dir {out} resolves inside the read-only "
            f"input snapshot {inp}"
        )
    if out == mem or is_inside(out, mem):
        _die(
            f"refusing to run: --out-dir {out} resolves inside hermes/memory/ "
            f"({mem})"
        )


def parse_created_at(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def window_for_experiment(
    created_at: datetime, backtest_days: int
) -> tuple[datetime, datetime]:
    """Same window HermesAgent used: `_fetch_ohlcv(..., days)` from `now≈created_at`."""
    return created_at - timedelta(days=int(backtest_days)), created_at


def metric_slice(metrics: dict | None) -> dict:
    src = metrics or {}
    out = {}
    for key in METRIC_KEYS:
        val = src.get(key, 0)
        if key == "trades":
            try:
                out[key] = int(val or 0)
            except (TypeError, ValueError):
                out[key] = 0
        else:
            try:
                out[key] = float(val or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
    return out


def rebuild_params(
    profile_params: dict | None, variable: str, old_value: Any, new_value: Any
) -> tuple[dict, dict]:
    baseline = dict(DEFAULT_PARAMS)
    if profile_params:
        baseline.update(profile_params)
    if variable:
        baseline[variable] = old_value
    variant = dict(baseline)
    if variable:
        variant[variable] = new_value
    return baseline, variant


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return copy.deepcopy(default)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from data_manager import atomic_write_json

    atomic_write_json(str(path), data)


def recost_bot_config(base: BotConfig | None = None) -> BotConfig:
    """Clone config: ta_only backtest, CostModel from config, Hermes stays off."""
    raw = copy.deepcopy((base or get_bot_config()).raw)
    hermes = raw.setdefault("hermes", {})
    hermes["enabled"] = False
    hermes["backtest_mode"] = "ta_only"
    hermes.setdefault("live_evidence", {})["enabled"] = False
    costs = raw.setdefault("costs", {})
    costs["fee_source"] = "config"
    return BotConfig(raw)


def fetch_ohlcv_bars(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> list:
    """Gate public OHLCV via the existing historical_prices cache + rate limiter."""
    from historical_prices import _fetch_ohlcv_range

    try:
        return _fetch_ohlcv_range(symbol, start, end, timeframe=timeframe) or []
    except Exception:
        return []


class SymbolOhlcvCache:
    """One Gate fetch per (symbol, timeframe) covering the union of experiment windows."""

    def __init__(self, fetch_bars: FetchBars):
        self.fetch_bars = fetch_bars
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._failed: dict[tuple[str, str], str] = {}

    def prefetch(self, spans: dict[tuple[str, str], tuple[datetime, datetime]]) -> None:
        for (symbol, timeframe), (start, end) in spans.items():
            key = (symbol, timeframe)
            if key in self._frames or key in self._failed:
                continue
            bars = self.fetch_bars(symbol, timeframe, start, end)
            if not bars:
                self._failed[key] = "ohlcv unavailable"
                continue
            df = pd.DataFrame(
                bars, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            self._frames[key] = df

    def window(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame | None:
        key = (symbol, timeframe)
        if key in self._failed:
            return None
        df = self._frames.get(key)
        if df is None or df.empty:
            return None
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        sliced = df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].copy()
        if sliced.empty:
            return None
        return sliced

    def fail_reason(self, symbol: str, timeframe: str) -> str:
        return self._failed.get((symbol, timeframe), "")


def stratify_experiments(
    experiments: list[dict],
    *,
    limit: int | None,
    symbols: set[str] | None,
) -> tuple[list[dict], int]:
    selected = list(experiments)
    skipped = 0
    if symbols:
        kept = [e for e in selected if str(e.get("symbol") or "") in symbols]
        skipped += len(selected) - len(kept)
        selected = kept
    if limit is None or limit <= 0 or limit >= len(selected):
        return selected, skipped
    by_var: dict[str, list[dict]] = defaultdict(list)
    for exp in selected:
        by_var[str(exp.get("variable") or "_")].append(exp)
    out: list[dict] = []
    cursors = {var: 0 for var in by_var}
    variables = list(by_var)
    while len(out) < limit and variables:
        next_vars = []
        for var in variables:
            idx = cursors[var]
            bucket = by_var[var]
            if idx < len(bucket):
                out.append(bucket[idx])
                cursors[var] = idx + 1
                if idx + 1 < len(bucket):
                    next_vars.append(var)
                if len(out) >= limit:
                    break
        variables = next_vars
    skipped += len(selected) - len(out)
    return out, skipped


def load_snapshot_experiments(input_dir: Path) -> list[dict]:
    """Flatten experiments.json + experiments.demo.json with ledger/baseline pointers."""
    rows: list[dict] = []
    for filename, ledger, baseline_name in EXPERIMENT_FILES:
        path = input_dir / filename
        data = load_json(path, {"experiments": []})
        experiments = data.get("experiments") if isinstance(data, dict) else data
        if not isinstance(experiments, list):
            continue
        baseline = load_json(input_dir / baseline_name, {"profiles": {}})
        profiles = baseline.get("profiles") if isinstance(baseline, dict) else {}
        for exp in experiments:
            if not isinstance(exp, dict):
                continue
            item = dict(exp)
            item["_ledger"] = ledger
            item["_source_file"] = filename
            item["_profiles"] = profiles or {}
            rows.append(item)
    return rows


def copy_snapshot_tagged(input_dir: Path, out_dir: Path) -> None:
    """Copy the snapshot to out_dir and tag every experiment `cost_model: legacy`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(input_dir.iterdir()):
        if not src.is_file():
            continue
        dest = out_dir / src.name
        if src.name in {"experiments.json", "experiments.demo.json"}:
            data = load_json(src, {"experiments": []})
            experiments = data.get("experiments") if isinstance(data, dict) else []
            if isinstance(experiments, list):
                for exp in experiments:
                    if isinstance(exp, dict):
                        exp["cost_model"] = LEGACY_COST_MODEL
            dump_json(dest, data)
        else:
            shutil.copy2(src, dest)


def _empty_metrics() -> dict:
    return {k: 0 if k == "trades" else 0.0 for k in METRIC_KEYS}


def recost_one(
    exp: dict,
    *,
    backtester: Backtester,
    goals: GoalEngine,
    hermes_cfg: dict,
    backtest_days: int,
    ohlcv: SymbolOhlcvCache,
) -> RecostRow:
    symbol = str(exp.get("symbol") or "")
    timeframe = str(exp.get("timeframe") or "4h")
    variable = str(exp.get("variable") or "")
    old_verdict = str(exp.get("verdict") or "rejected")
    created = parse_created_at(exp.get("created_at"))
    profiles = exp.get("_profiles") or {}
    profile = profiles.get(profile_key(symbol, timeframe)) or {}
    baseline_params, variant_params = rebuild_params(
        profile.get("params") if isinstance(profile, dict) else None,
        variable,
        exp.get("old_value"),
        exp.get("new_value"),
    )
    row = RecostRow(
        experiment_id=str(exp.get("id") or ""),
        ledger=str(exp.get("_ledger") or ""),
        symbol=symbol,
        timeframe=timeframe,
        variable=variable,
        old_value=exp.get("old_value"),
        new_value=exp.get("new_value"),
        source=str(exp.get("source") or ""),
        created_at=str(exp.get("created_at") or ""),
        old_verdict=old_verdict,
        new_verdict="unresolvable",
        new_verdict_reason="",
        old_baseline=metric_slice(exp.get("baseline_metrics")),
        old_variant=metric_slice(exp.get("variant_metrics")),
        new_baseline=_empty_metrics(),
        new_variant=_empty_metrics(),
        cost_model=COST_MODEL_VERSION,
    )
    if not symbol:
        row.unresolvable = True
        row.unresolvable_reason = "missing symbol"
        return row
    if created is None:
        row.unresolvable = True
        row.unresolvable_reason = "missing created_at"
        return row

    start, end = window_for_experiment(created, backtest_days)
    fail = ohlcv.fail_reason(symbol, timeframe)
    if fail:
        row.unresolvable = True
        row.unresolvable_reason = fail
        return row
    df = ohlcv.window(symbol, timeframe, start, end)
    if df is None or df.empty:
        row.unresolvable = True
        row.unresolvable_reason = "ohlcv unavailable"
        return row

    wf_base = run_walk_forward(
        backtester, symbol, timeframe, baseline_params, df, hermes_cfg
    )
    if wf_base.folds_total == 0:
        row.unresolvable = True
        row.unresolvable_reason = "no valid walk-forward folds"
        return row
    wf_var = run_walk_forward(
        backtester,
        symbol,
        timeframe,
        variant_params,
        df,
        hermes_cfg,
        baseline_folds=wf_base.fold_metrics,
    )
    verdict = goals.evaluate_walk_forward(wf_base, wf_var)
    row.new_baseline = metric_slice(wf_base.aggregate.__dict__)
    row.new_variant = metric_slice(wf_var.aggregate.__dict__)
    row.folds_won = int(wf_var.folds_won)
    row.folds_total = int(wf_var.folds_total)
    row.new_verdict = "promoted" if verdict.promoted else "rejected"
    row.new_verdict_reason = verdict.reason
    row.unresolvable = False
    row.pnl_delta = float(row.new_variant.get("realized_pnl", 0) or 0) - float(
        row.old_variant.get("realized_pnl", 0) or 0
    )
    return row


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" if i == 0 else "---:" for i in range(len(headers))) + " |",
    ]
    if not rows:
        lines.append("| " + " | ".join(["—"] * len(headers)) + " |")
        return "\n".join(lines)
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _fmt_val(val: Any) -> str:
    if isinstance(val, float):
        if val == int(val):
            return str(int(val))
        return f"{val:g}"
    return str(val)


def render_report(summary: RecostSummary) -> str:
    rows = summary.rows
    flips = [
        r
        for r in rows
        if not r.unresolvable
        and r.old_verdict == "rejected"
        and r.new_verdict == "promoted"
    ]
    unchanged = [
        r
        for r in rows
        if not r.unresolvable and r.new_verdict == r.old_verdict
    ]
    unresolvable = [r for r in rows if r.unresolvable]
    promoted_new = [r for r in rows if r.new_verdict == "promoted"]
    still_rejected = [
        r for r in rows if not r.unresolvable and r.new_verdict == "rejected"
    ]

    by_var = Counter(r.variable or "?" for r in flips)
    by_sym = Counter(r.symbol or "?" for r in flips)
    by_src = Counter(r.source or "?" for r in flips)
    unres_by_sym = Counter(r.symbol or "?" for r in unresolvable)
    unres_reason = Counter(r.unresolvable_reason or "?" for r in unresolvable)

    pnl_sorted = sorted(
        (r for r in rows if not r.unresolvable),
        key=lambda r: abs(r.pnl_delta),
        reverse=True,
    )[:10]

    lines = [
        "# Hermes Recost — CostModel `2026-09-v1` vs. Legacy",
        "",
        f"**Stand:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')} · "
        f"**Ticket:** #316 · **CostModel:** `{COST_MODEL_VERSION}` · "
        f"**Legacy:** `{LEGACY_COST_MODEL}`",
        "",
        "Read-only Snapshot der Operator-Hermes-Memory. Jedes Experiment wurde "
        "auf dem **gleichen** Walk-Forward-Fenster wie `HermesAgent.run_cycle` "
        f"(`created_at` − `{summary.backtest_days}`d, "
        f"`fold_days={summary.fold_days}`, `step_days={summary.step_days}`) "
        f"mit `hermes/validation.py` + CostModel-fähigem `hermes/backtester.py` "
        f"(`backtest_mode={summary.backtest_mode}`) neu bewertet. "
        "Keine Live-Orders, keine Promotion, keine Schreibzugriffe auf den Input "
        "oder `hermes/memory/`.",
        "",
        f"- Input: `{summary.input_dir}`",
        f"- Getaggte Kopie (`cost_model: {LEGACY_COST_MODEL}`): `{summary.out_dir}`",
        f"- Laufzeit: **{summary.runtime_sec:.1f}s**",
        "",
        "## Gesamt",
        "",
        _md_table(
            ["Kennzahl", "Wert"],
            [
                ["Experimente bewertet", summary.considered],
                ["Filter übersprungen (Limit/Symbols)", summary.skipped_filter],
                ["Alt: rejected", sum(1 for r in rows if r.old_verdict == "rejected")],
                ["Alt: promoted", sum(1 for r in rows if r.old_verdict == "promoted")],
                ["Neu: promoted (`2026-09-v1`)", len(promoted_new)],
                ["**rejected → promoted (Flips)**", len(flips)],
                ["Unverändert (gleiches Verdict)", len(unchanged)],
                ["Weiter rejected", len(still_rejected)],
                ["unresolvable (keine OHLCV / keine Folds)", len(unresolvable)],
            ],
        ),
        "",
        "## Flips rejected → promoted nach Variable",
        "",
        (
            _md_table(["variable", "Flips"], [[k, n] for k, n in by_var.most_common()])
            if by_var
            else "Keine Flips."
        ),
        "",
        "## Flips nach Symbol",
        "",
        (
            _md_table(["symbol", "Flips"], [[k, n] for k, n in by_sym.most_common()])
            if by_sym
            else "Keine Flips."
        ),
        "",
        "## Flips nach Quelle (grok / heuristic)",
        "",
        (
            _md_table(["source", "Flips"], [[k, n] for k, n in by_src.most_common()])
            if by_src
            else "Keine Flips."
        ),
        "",
    ]

    if flips:
        lines += [
            "### Flip-Liste",
            "",
            _md_table(
                [
                    "id",
                    "ledger",
                    "symbol",
                    "variable",
                    "old → new",
                    "source",
                    "pnl Δ",
                    "sharpe alt→neu (variant)",
                    "folds",
                ],
                [
                    [
                        r.experiment_id,
                        r.ledger,
                        r.symbol,
                        r.variable,
                        f"{_fmt_val(r.old_value)} → {_fmt_val(r.new_value)}",
                        r.source,
                        f"{r.pnl_delta:+.2f}",
                        f"{r.old_variant.get('sharpe', 0)} → {r.new_variant.get('sharpe', 0)}",
                        f"{r.folds_won}/{r.folds_total}",
                    ]
                    for r in flips
                ],
            ),
            "",
        ]

    lines += [
        "## Die 10 größten realized_pnl-Deltas",
        "",
        "Delta = `variant.realized_pnl` (CostModel `2026-09-v1`) − "
        "`variant.realized_pnl` (legacy, gespeichert im Experiment).",
        "",
        _md_table(
            [
                "id",
                "symbol",
                "variable",
                "old variant pnl",
                "new variant pnl",
                "Δ pnl",
                "old trades",
                "new trades",
                "verdict alt→neu",
            ],
            [
                [
                    r.experiment_id,
                    r.symbol,
                    r.variable,
                    f"{r.old_variant.get('realized_pnl', 0):.2f}",
                    f"{r.new_variant.get('realized_pnl', 0):.2f}",
                    f"{r.pnl_delta:+.2f}",
                    r.old_variant.get("trades", 0),
                    r.new_variant.get("trades", 0),
                    f"{r.old_verdict} → {r.new_verdict}",
                ]
                for r in pnl_sorted
            ],
        ),
        "",
        "## Was das für die Baseline bedeutet",
        "",
    ]

    if not flips:
        lines += [
            "Kein rejected→promoted-Flip. Würde man die Varianten anwenden, "
            "änderte sich **kein** Wert in `baseline.json` / `baseline.demo.json`. "
            "Hermes bleibt aus, bis dieser Bericht gelesen ist (#310).",
            "",
        ]
    else:
        lines.append(
            "Würde man die geflippten Varianten anwenden (das tut dieses Skript "
            "**nicht**), änderten sich folgende Profilwerte. Mehrere Flips auf "
            "derselben `symbol|timeframe|variable`-Zelle sind einzeln gelistet — "
            "der Operator entscheidet, welcher Wert gilt."
        )
        lines.append("")
        lines.append(
            _md_table(
                ["ledger", "profile", "variable", "aktuell (old_value)", "Variante (new_value)", "id"],
                [
                    [
                        r.ledger,
                        profile_key(r.symbol, r.timeframe),
                        r.variable,
                        _fmt_val(r.old_value),
                        _fmt_val(r.new_value),
                        r.experiment_id,
                    ]
                    for r in flips
                ],
            )
        )
        lines.append("")

    resolved = [r for r in rows if not r.unresolvable]
    any_trades = any(
        (r.new_variant.get("trades") or 0) or (r.old_variant.get("trades") or 0)
        for r in resolved
    )
    if resolved and not any_trades:
        lines += [
            "### Befund: Walk-Forward hat nie gehandelt",
            "",
            "Alle bewerteten Experimente haben `trades = 0` sowohl in den "
            "gespeicherten Legacy-Metriken als auch unter `2026-09-v1`. "
            f"Die Fold-Fenster (`fold_days={summary.fold_days}`, "
            f"`backtest_days={summary.backtest_days}`) liefern auf 4h typisch "
            "18 Bars; `Backtester.run` bricht bei `< 30` Bars ab, bevor "
            "Indikatoren oder Fills gerechnet werden. Das Kostenmodell kommt "
            "auf diesen Fenstern nicht zum Tragen — die Ablehnungen sind ein "
            "Geometrie-Problem der Walk-Forward-Folds, kein 3-%-Round-Trip.",
            "",
        ]

    if unresolvable:
        lines += [
            "### unresolvable",
            "",
            _md_table(
                ["Grund", "Anzahl"],
                [[k, n] for k, n in unres_reason.most_common()],
            ),
            "",
            _md_table(
                ["symbol", "unresolvable"],
                [[k, n] for k, n in unres_by_sym.most_common()],
            ),
            "",
        ]

    lines += [
        "## Caveat: Sharpe und Win-Rate sind erstmals netto",
        "",
        "`hermes/metrics.py` (`sharpe_from_trades`, Win-Rate über `pnl > 0`) "
        "rechnet über das `pnl`-Feld der SELL-Trades. Unter dem Legacy-Modell "
        "war `pnl = (price − entry) · qty` **brutto** (1,5 % Slippage traf nur "
        "`balance`, keine Fee). Unter `core/costs.py` ist `pnl = "
        "CostModel.realized_pnl(...)` **netto** (0,2 % Fee + Tier-Slippage "
        "stecken in `quote_net` und der Kostenbasis). Sharpe und Win-Rate der "
        "Neu-Bewertung sind deshalb nicht 1:1 mit den gespeicherten "
        "Legacy-Metriken vergleichbar — sie messen zum ersten Mal denselben "
        "Cash-Strom wie der Kontostand.",
        "",
        "## Methode",
        "",
        "- Baseline-Params: Profil `symbol|timeframe` aus `baseline*.json` des "
        "jeweiligen Ledgers, `variable = old_value`.",
        "- Varianten-Params: dieselben Params, `variable = new_value`.",
        f"- Fenster: `[created_at − {summary.backtest_days}d, created_at]`, "
        "OHLCV über `historical_prices._fetch_ohlcv_range` (Gate, "
        "`enableRateLimit`, Prozess-Cache). Ein Fetch pro Symbol über die "
        "Vereinigung der Fenster.",
        "- Verdict: `GoalEngine.evaluate_walk_forward` — **ohne** Live-Evidence "
        "und ohne Dual-Promote (beides ist Ledger, nicht Kostenmodell).",
        "- Fehlt die Historie eines Symbols → `unresolvable`, kein Raten.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def run_recost(
    *,
    input_dir: Path,
    out_path: Path,
    out_dir: Path,
    limit: int | None = None,
    symbols: set[str] | None = None,
    fetch_bars: FetchBars | None = None,
    config: BotConfig | None = None,
    progress: bool = True,
) -> RecostSummary:
    input_dir = resolve_path(input_dir)
    out_dir = resolve_path(out_dir)
    out_path = Path(out_path)
    if not input_dir.is_dir():
        _die(f"--input is not a directory: {input_dir}")
    assert_out_dir_safe(out_dir, input_dir)

    t0 = time.perf_counter()
    copy_snapshot_tagged(input_dir, out_dir)

    cfg = recost_bot_config(config)
    hermes_cfg = cfg.hermes_config
    vcfg = hermes_cfg.get("validation") or {}
    backtest_days = int(vcfg.get("backtest_days", hermes_cfg.get("backtest_days", 35)))

    all_exps = load_snapshot_experiments(input_dir)
    selected, skipped = stratify_experiments(
        all_exps, limit=limit, symbols=symbols
    )

    spans: dict[tuple[str, str], tuple[datetime, datetime]] = {}
    for exp in selected:
        created = parse_created_at(exp.get("created_at"))
        symbol = str(exp.get("symbol") or "")
        timeframe = str(exp.get("timeframe") or "4h")
        if not created or not symbol:
            continue
        start, end = window_for_experiment(created, backtest_days)
        key = (symbol, timeframe)
        if key in spans:
            prev_s, prev_e = spans[key]
            spans[key] = (min(prev_s, start), max(prev_e, end))
        else:
            spans[key] = (start, end)

    cache = SymbolOhlcvCache(fetch_bars or fetch_ohlcv_bars)
    cache.prefetch(spans)

    backtester = Backtester(cfg)
    goals = GoalEngine(cfg)
    rows: list[RecostRow] = []
    total = len(selected)
    from unittest.mock import patch

    # Backtester already uses sim_state; still block any live-ledger peek.
    with patch("strategies.technical_rsi_bb.get_position", return_value={}):
        for i, exp in enumerate(selected, 1):
            if progress and (i == 1 or i == total or i % 25 == 0):
                print(
                    f"recost {i}/{total} {exp.get('id')} {exp.get('symbol')}",
                    file=sys.stderr,
                )
            rows.append(
                recost_one(
                    exp,
                    backtester=backtester,
                    goals=goals,
                    hermes_cfg=hermes_cfg,
                    backtest_days=backtest_days,
                    ohlcv=cache,
                )
            )

    summary = RecostSummary(
        rows=rows,
        runtime_sec=time.perf_counter() - t0,
        input_dir=str(input_dir),
        out_dir=str(out_dir),
        considered=len(rows),
        skipped_filter=skipped,
        backtest_days=backtest_days,
        fold_days=int(vcfg.get("fold_days", 7)),
        step_days=int(vcfg.get("step_days", 3)),
        min_bars_per_fold=int(vcfg.get("min_bars_per_fold", 12)),
        validation_mode=str(vcfg.get("mode", "walk_forward")),
        backtest_mode=str(hermes_cfg.get("backtest_mode", "ta_only")),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(summary), encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Re-cost Hermes experiments against core/costs.py. "
            "Read-only on --input; tagged copy goes to --out-dir."
        )
    )
    p.add_argument(
        "--input",
        required=True,
        help="Read-only snapshot directory (experiments*.json, baseline*.json)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Markdown report path (e.g. docs/audit/hermes-recost.md)",
    )
    p.add_argument(
        "--out-dir",
        default="",
        help=(
            "Directory for the tagged snapshot copy. Default: a scratch dir "
            "under the system temp directory. Refused if it resolves inside "
            "--input or hermes/memory/."
        ),
    )
    p.add_argument("--limit", type=int, default=0, help="Max experiments (stratified by variable)")
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbol filter (e.g. ARIA/USDT,ETH/USDT)",
    )
    p.add_argument("--quiet", action="store_true", help="No progress on stderr")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = (
        resolve_path(args.out_dir)
        if args.out_dir
        else Path(tempfile.mkdtemp(prefix="hermes-recost-"))
    )
    symbols = {s.strip() for s in str(args.symbols).split(",") if s.strip()} or None
    summary = run_recost(
        input_dir=Path(args.input),
        out_path=Path(args.out),
        out_dir=out_dir,
        limit=args.limit or None,
        symbols=symbols,
        progress=not args.quiet,
    )
    flips = sum(
        1
        for r in summary.rows
        if not r.unresolvable
        and r.old_verdict == "rejected"
        and r.new_verdict == "promoted"
    )
    unres = sum(1 for r in summary.rows if r.unresolvable)
    print(
        f"recost done: {summary.considered} experiments, "
        f"{flips} rejected→promoted, {unres} unresolvable, "
        f"{summary.runtime_sec:.1f}s",
        file=sys.stderr,
    )
    print(f"report: {args.out}")
    print(f"tagged copy: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
