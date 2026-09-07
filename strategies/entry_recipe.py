"""Personal entry recipes: coin-specific RSI/vol/regime params for DecisionEngine.

Product:
  - Leaders board is observe/scorecard, not a buy source.
  - Personal params live in Hermes profile store (same shape DE already loads).
  - Primary 30d comparison metric: mean total_return_pct of bar backtests
    (personal vs tier defaults) on the same OHLCV window.

Pure logic stays free of Flask/Mongo; I/O helpers are thin wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd

# Keys DecisionEngine / TechnicalRSIStrategy actually read for entries.
PERSONAL_BUY_PARAM_KEYS: tuple[str, ...] = (
    "rsi_buy_low",
    "rsi_buy_high",
    "volume_multiplier",
    "buy_regime",
    "reversal_rsi_cross_low",
    "reversal_rsi_cross_high",
    "reversal_volume_multiplier",
)

STRATEGY_PROFILE_PERSONAL = "personal_entry_v1"
# Written when search falls back to tier defaults — must NOT override config.strategies[].
STRATEGY_PROFILE_TIER_FALLBACK = "entry_recipe_tier_fallback"
MIN_TRADES_FOR_PERSONAL = 2
MIN_BARS_FOR_SCORE = 40

# Pre-declared primary metric for 30d retrospective (see renew script).
PRIMARY_METRIC = "mean_total_return_pct"

_BUY_REGIMES = ("dip", "reversal", "both")


def normalize_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if not s:
        return ""
    if ":" in s:
        s = s.split(":", 1)[0]
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def build_symbol_universe(
    *,
    watchlist: Iterable[Any] | None = None,
    open_positions: Iterable[Any] | None = None,
    orders: Iterable[Any] | None = None,
) -> list[str]:
    """Union of watchlist, open lots, and filled trade symbols (stable order)."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, dict):
            sym = raw.get("symbol") or raw.get("pair") or ""
        else:
            sym = raw
        sym = normalize_symbol(str(sym))
        if not sym or not sym.endswith("/USDT"):
            return
        if sym in seen:
            return
        seen.add(sym)
        out.append(sym)

    for item in watchlist or []:
        _add(item)
    for item in open_positions or []:
        _add(item)
    for item in orders or []:
        if isinstance(item, dict):
            st = str(item.get("status") or "").lower()
            # missing status → include (legacy); skip explicit non-fills
            if st and st not in ("filled", "closed"):
                continue
        _add(item)
    return out


def tier_default_buy_params(tier: str | None = None) -> dict[str, Any]:
    """Stable/volatile defaults matching config.json buy keys (no I/O)."""
    t = (tier or "volatile").strip().lower()
    if t in ("stable", "stable_altcoin", "large_cap"):
        return {
            "rsi_buy_low": 30,
            "rsi_buy_high": 50,
            "volume_multiplier": 1.05,
            "buy_regime": "dip",
            "reversal_rsi_cross_low": 32,
            "reversal_rsi_cross_high": 40,
            "reversal_volume_multiplier": 1.2,
        }
    return {
        "rsi_buy_low": 28,
        "rsi_buy_high": 48,
        "volume_multiplier": 1.15,
        "buy_regime": "both",
        "reversal_rsi_cross_low": 32,
        "reversal_rsi_cross_high": 38,
        "reversal_volume_multiplier": 0.95,
    }


def normalize_personal_params(raw: dict | None) -> dict[str, Any]:
    """Clamp/normalize buy params into DE-safe values."""
    base = tier_default_buy_params("volatile")
    src = dict(raw or {})
    out = dict(base)
    for k in PERSONAL_BUY_PARAM_KEYS:
        if k not in src or src[k] is None:
            continue
        out[k] = src[k]

    try:
        lo = int(round(float(out["rsi_buy_low"])))
        hi = int(round(float(out["rsi_buy_high"])))
    except (TypeError, ValueError):
        lo, hi = 28, 48
    lo = max(15, min(40, lo))
    hi = max(35, min(60, hi))
    if hi <= lo:
        hi = min(60, lo + 8)
    out["rsi_buy_low"] = lo
    out["rsi_buy_high"] = hi

    try:
        vm = float(out["volume_multiplier"])
    except (TypeError, ValueError):
        vm = 1.15
    out["volume_multiplier"] = round(max(1.0, min(2.5, vm)), 2)

    regime = str(out.get("buy_regime") or "both").lower()
    out["buy_regime"] = regime if regime in _BUY_REGIMES else "both"

    try:
        rlo = int(round(float(out["reversal_rsi_cross_low"])))
        rhi = int(round(float(out["reversal_rsi_cross_high"])))
    except (TypeError, ValueError):
        rlo, rhi = 32, 38
    rlo = max(25, min(38, rlo))
    rhi = max(35, min(48, rhi))
    if rhi <= rlo:
        rhi = min(48, rlo + 4)
    out["reversal_rsi_cross_low"] = rlo
    out["reversal_rsi_cross_high"] = rhi

    try:
        rvm = float(out["reversal_volume_multiplier"])
    except (TypeError, ValueError):
        rvm = 1.0
    out["reversal_volume_multiplier"] = round(max(1.0, min(2.0, rvm)), 2)
    return out


def merge_personal_over_tier(
    personal: dict | None,
    tier_params: dict | None,
    *,
    tier: str | None = None,
) -> dict[str, Any]:
    """Tier provides base; personal buy keys always win when present."""
    base = dict(tier_params or tier_default_buy_params(tier))
    person = normalize_personal_params(personal) if personal else {}
    if personal:
        for k in PERSONAL_BUY_PARAM_KEYS:
            if k in person:
                base[k] = person[k]
        if personal.get("strategy_profile"):
            base["strategy_profile"] = personal.get("strategy_profile")
        if personal.get("personal_entry_renewed_at"):
            base["personal_entry_renewed_at"] = personal.get(
                "personal_entry_renewed_at"
            )
    return base


def preserve_buy_params(base: dict, preferred: dict | None) -> dict:
    """After tier overlay, restore personal/hermes buy keys from preferred."""
    if not preferred:
        return base
    out = dict(base)
    for k in PERSONAL_BUY_PARAM_KEYS:
        if k in preferred and preferred[k] is not None:
            out[k] = preferred[k]
    for meta in (
        "strategy_profile",
        "personal_entry_renewed_at",
        "hermes_baseline_updated_at",
        "personal_entry_fallback",
        "personal_entry_score",
    ):
        if preferred.get(meta) is not None:
            out[meta] = preferred[meta]
    return out


def is_personal_entry_profile(params: dict | None) -> bool:
    if not params:
        return False
    return str(params.get("strategy_profile") or "") == STRATEGY_PROFILE_PERSONAL


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """RSI/BB/vol mult without requiring TA-Lib (pandas-only for portability)."""
    out = df.copy()
    close = out["close"].astype(float)
    vol = out["volume"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.rolling(14, min_periods=14).mean()
    avg_loss = loss.rolling(14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out["rsi"] = 100.0 - (100.0 / (1.0 + rs))
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std()
    out["lower_bb"] = mid - 2.0 * std
    out["vol_avg"] = vol.rolling(20, min_periods=20).mean()
    recent = vol.rolling(4, min_periods=1).mean()
    out["vol_mult"] = recent / out["vol_avg"].replace(0.0, np.nan)
    return out.dropna().reset_index(drop=True)


@dataclass
class ParamScore:
    params: dict
    total_return_pct: float
    trades: int
    win_rate: float
    max_drawdown_pct: float
    buy_signals: int
    bars: int
    fallback_reason: str = ""


def score_entry_params_on_df(
    df: pd.DataFrame,
    params: dict,
    *,
    capital: float = 1000.0,
    usdt_per_trade: float = 50.0,
    fee_pct: float = 0.1,
) -> ParamScore:
    """Bar backtest aligned with TechnicalRSIStrategy dip/reversal BUY rules."""
    p = normalize_personal_params(params)
    if df is None or len(df) < MIN_BARS_FOR_SCORE:
        return ParamScore(
            params=p,
            total_return_pct=0.0,
            trades=0,
            win_rate=0.0,
            max_drawdown_pct=0.0,
            buy_signals=0,
            bars=0 if df is None else len(df),
            fallback_reason="insufficient_bars",
        )

    work = df
    if "rsi" not in work.columns:
        work = add_indicators(work)
    if len(work) < MIN_BARS_FOR_SCORE:
        return ParamScore(
            params=p,
            total_return_pct=0.0,
            trades=0,
            win_rate=0.0,
            max_drawdown_pct=0.0,
            buy_signals=0,
            bars=len(work),
            fallback_reason="insufficient_bars_after_indicators",
        )

    rsi_lo = float(p["rsi_buy_low"])
    rsi_hi = float(p["rsi_buy_high"])
    vol_need = float(p["volume_multiplier"])
    regime = str(p["buy_regime"])
    rev_lo = float(p["reversal_rsi_cross_low"])
    rev_hi = float(p["reversal_rsi_cross_high"])
    rev_vol = float(p["reversal_volume_multiplier"])
    # Fixed exit band for fair param comparison (entry focus).
    take_profit = 0.12
    stop_loss = 0.10

    balance = float(capital)
    amount = 0.0
    entry = 0.0
    last_rsi = 45.0
    trades = 0
    wins = 0
    buy_signals = 0
    peak = capital
    max_dd = 0.0
    fee = fee_pct / 100.0

    for i in range(len(work)):
        row = work.iloc[i]
        price = float(row["close"])
        rsi = float(row["rsi"])
        lower = float(row["lower_bb"])
        vol_m = float(row["vol_mult"]) if pd.notna(row["vol_mult"]) else 1.0

        if amount > 0 and entry > 0:
            gain = (price / entry) - 1.0
            if gain >= take_profit or gain <= -stop_loss:
                proceeds = amount * price * (1.0 - fee)
                pnl = proceeds - (amount * entry)
                balance += proceeds
                trades += 1
                if pnl > 0:
                    wins += 1
                amount = 0.0
                entry = 0.0

        if amount <= 0 and balance >= usdt_per_trade:
            dip = (
                regime in ("dip", "both")
                and price <= lower * 1.01
                and rsi_lo <= rsi <= rsi_hi
                and vol_m >= vol_need
            )
            crossed = last_rsi < rev_lo and rsi >= rev_hi
            rev = (
                regime in ("reversal", "both")
                and crossed
                and vol_m >= rev_vol
            )
            if dip or rev:
                buy_signals += 1
                cost = usdt_per_trade * (1.0 + fee)
                if balance >= cost:
                    amount = usdt_per_trade / price
                    entry = price
                    balance -= cost

        last_rsi = rsi
        equity = balance + amount * price
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    if amount > 0:
        price = float(work.iloc[-1]["close"])
        balance += amount * price * (1.0 - fee)
        trades += 1
        if price >= entry:
            wins += 1
        amount = 0.0

    total_ret = (balance - capital) / capital * 100.0 if capital else 0.0
    wr = (wins / trades) if trades else 0.0
    return ParamScore(
        params=p,
        total_return_pct=float(total_ret),
        trades=int(trades),
        win_rate=float(wr),
        max_drawdown_pct=float(max_dd),
        buy_signals=int(buy_signals),
        bars=len(work),
    )


def candidate_param_grid(tier: str | None = None) -> list[dict[str, Any]]:
    """Small discrete grid around tier defaults (Hermes-style buy knobs only)."""
    base = tier_default_buy_params(tier)
    grid: list[dict[str, Any]] = []
    rsi_lows = (22, 26, 28, 30, 32, 34)
    rsi_highs = (42, 45, 48, 50, 52, 55)
    vols = (1.0, 1.05, 1.15, 1.3, 1.5)
    regimes = ("dip", "both", "reversal")
    for lo in rsi_lows:
        for hi in rsi_highs:
            if hi <= lo + 6:
                continue
            for vm in vols:
                for reg in regimes:
                    cand = dict(base)
                    cand.update(
                        {
                            "rsi_buy_low": lo,
                            "rsi_buy_high": hi,
                            "volume_multiplier": vm,
                            "buy_regime": reg,
                        }
                    )
                    grid.append(normalize_personal_params(cand))
    # de-dupe
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for g in grid:
        key = tuple(sorted((k, g[k]) for k in PERSONAL_BUY_PARAM_KEYS))
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)
    return unique


def select_best_params(
    df: pd.DataFrame,
    *,
    tier: str | None = "volatile",
    min_trades: int = MIN_TRADES_FOR_PERSONAL,
) -> tuple[dict[str, Any], ParamScore, ParamScore, str]:
    """Pick best personal params vs tier baseline.

    Returns (chosen_params, personal_score, baseline_score, reason).
    reason is '' when personal wins; otherwise fallback reason.
    """
    baseline = tier_default_buy_params(tier)
    base_score = score_entry_params_on_df(df, baseline)
    if base_score.fallback_reason:
        return baseline, base_score, base_score, base_score.fallback_reason

    best = base_score
    best_params = baseline
    for cand in candidate_param_grid(tier):
        sc = score_entry_params_on_df(df, cand)
        if sc.trades < min_trades and sc.trades < best.trades:
            continue
        # Primary: total_return_pct; tie-break: more trades, lower DD
        better = sc.total_return_pct > best.total_return_pct + 1e-9
        tie = abs(sc.total_return_pct - best.total_return_pct) <= 1e-9
        if better or (
            tie
            and (
                sc.trades > best.trades
                or (
                    sc.trades == best.trades
                    and sc.max_drawdown_pct < best.max_drawdown_pct
                )
            )
        ):
            best = sc
            best_params = cand

    if best.trades < min_trades:
        return (
            baseline,
            best,
            base_score,
            f"min_trades:{best.trades}<{min_trades}",
        )
    if best.total_return_pct + 1e-9 < base_score.total_return_pct:
        return baseline, best, base_score, "personal_worse_than_tier"
    return best_params, best, base_score, ""


def is_personal_fallback_profile(params: dict | None) -> bool:
    """True when renewal stored tier defaults (must not clobber config.strategies)."""
    if not params:
        return False
    if params.get("personal_entry_fallback"):
        return True
    return str(params.get("strategy_profile") or "") == STRATEGY_PROFILE_TIER_FALLBACK


def build_personal_profile_payload(
    symbol: str,
    timeframe: str,
    params: dict,
    score: ParamScore,
    *,
    fallback_reason: str = "",
    tier: str | None = "volatile",
) -> dict[str, Any]:
    """Hermes-compatible profile document for save_profile.

    On fallback we persist a *non-personal* profile marker so resolve leaves
    config.strategies[] / tier path intact (does not stamp generic 28/48/1.15).
    """
    now = datetime.now(timezone.utc).isoformat()
    personal = normalize_personal_params(params)
    used_fallback = bool(fallback_reason)
    if used_fallback:
        personal = tier_default_buy_params(tier)
        personal["strategy_profile"] = STRATEGY_PROFILE_TIER_FALLBACK
        personal["personal_entry_fallback"] = fallback_reason
    else:
        personal["strategy_profile"] = STRATEGY_PROFILE_PERSONAL
        personal.pop("personal_entry_fallback", None)
    personal["personal_entry_renewed_at"] = now
    personal["personal_entry_score"] = {
        "total_return_pct": score.total_return_pct,
        "trades": score.trades,
        "win_rate": score.win_rate,
        "max_drawdown_pct": score.max_drawdown_pct,
        "buy_signals": score.buy_signals,
        "bars": score.bars,
        "primary_metric": PRIMARY_METRIC,
        "fallback": bool(used_fallback),
    }
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": personal,
        "metrics": {
            "total_return_pct": score.total_return_pct,
            "trades": score.trades,
            "win_rate": score.win_rate,
            "source": "entry_recipe_renewal",
            "fallback_reason": fallback_reason or None,
            "tier": tier,
            "renewed_at": now,
        },
    }


def load_personal_params(symbol: str, timeframe: str) -> dict | None:
    """Load *winning* personal_entry_v1 params only.

    Tier-fallback stamps (personal_entry_fallback / entry_recipe_tier_fallback)
    return None so config.strategies[] and tier defaults stay authoritative.
    """
    try:
        from hermes.memory import store

        profile = store.load_profile(symbol, timeframe)
    except Exception:
        return None
    params = dict(profile.get("params") or {})
    if not any(k in params for k in PERSONAL_BUY_PARAM_KEYS):
        return None
    if is_personal_fallback_profile(params):
        return None
    if str(params.get("strategy_profile") or "") != STRATEGY_PROFILE_PERSONAL:
        return None
    out = normalize_personal_params(params)
    out["strategy_profile"] = STRATEGY_PROFILE_PERSONAL
    if params.get("personal_entry_renewed_at"):
        out["personal_entry_renewed_at"] = params["personal_entry_renewed_at"]
    if params.get("personal_entry_score"):
        out["personal_entry_score"] = params["personal_entry_score"]
    return out


def save_personal_params(symbol: str, timeframe: str, profile: dict) -> None:
    from hermes.memory import store

    store.save_profile(symbol, timeframe, profile)


@dataclass
class RenewalResult:
    symbol: str
    timeframe: str
    params: dict
    personal_return_pct: float
    baseline_return_pct: float
    fallback_reason: str = ""
    persisted: bool = False


def renew_symbol_params(
    symbol: str,
    df: pd.DataFrame,
    *,
    timeframe: str = "1h",
    tier: str | None = "volatile",
    persist: bool = True,
) -> RenewalResult:
    """Recompute and optionally persist personal entry params for one symbol."""
    best, personal_sc, base_sc, reason = select_best_params(df, tier=tier)
    # Score the *chosen* set for personal_return field
    chosen_sc = score_entry_params_on_df(df, best)
    payload = build_personal_profile_payload(
        symbol,
        timeframe,
        best,
        chosen_sc if not reason else base_sc,
        fallback_reason=reason,
        tier=tier,
    )
    persisted = False
    if persist:
        save_personal_params(symbol, timeframe, payload)
        persisted = True
    return RenewalResult(
        symbol=symbol,
        timeframe=timeframe,
        params=dict(payload["params"]),
        personal_return_pct=float(personal_sc.total_return_pct),
        baseline_return_pct=float(base_sc.total_return_pct),
        fallback_reason=reason,
        persisted=persisted,
    )


def compare_cohort(
    results: list[RenewalResult],
) -> dict[str, Any]:
    """Aggregate primary metric: mean total_return_pct personal search vs baseline."""
    if not results:
        return {
            "primary_metric": PRIMARY_METRIC,
            "n_symbols": 0,
            "personal_mean_total_return_pct": 0.0,
            "baseline_mean_total_return_pct": 0.0,
            "delta": 0.0,
            "equal_or_better": True,
            "n_personal_kept": 0,
            "n_tier_fallback": 0,
        }
    p_vals = [r.personal_return_pct for r in results]
    b_vals = [r.baseline_return_pct for r in results]
    # For symbols that fell back, personal_return is still the best grid score;
    # use chosen path: if fallback, count baseline as both for equality fairness.
    chosen = []
    base_only = []
    kept = 0
    fb = 0
    for r in results:
        base_only.append(r.baseline_return_pct)
        if r.fallback_reason:
            chosen.append(r.baseline_return_pct)
            fb += 1
        else:
            chosen.append(r.personal_return_pct)
            kept += 1
    p_mean = float(sum(chosen) / len(chosen))
    b_mean = float(sum(base_only) / len(base_only))
    return {
        "primary_metric": PRIMARY_METRIC,
        "n_symbols": len(results),
        "personal_mean_total_return_pct": p_mean,
        "baseline_mean_total_return_pct": b_mean,
        "delta": p_mean - b_mean,
        "equal_or_better": p_mean + 1e-9 >= b_mean,
        "n_personal_kept": kept,
        "n_tier_fallback": fb,
        "search_mean_best_grid_pct": float(sum(p_vals) / len(p_vals)),
        "search_mean_baseline_pct": float(sum(b_vals) / len(b_vals)),
    }
