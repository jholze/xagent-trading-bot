"""Overfitting statistics: Sharpe, deflated Sharpe, PBO via CSCV, bootstrap CI.

Pure Python (``statistics``/``math``); no numpy or scipy needed.

References: Bailey & López de Prado (2014) "The Deflated Sharpe Ratio";
Bailey, Borwein, López de Prado & Zhu (2016) "The Probability of Backtest
Overfitting" (combinatorially symmetric cross-validation, CSCV).
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from dataclasses import dataclass
from typing import Callable, Sequence

EULER = 0.5772156649015329
_NORMAL = statistics.NormalDist()


def sharpe(returns: Sequence[float]) -> float:
    """Mean over sample standard deviation. Not annualised. 0 when undefined."""
    n = len(returns)
    if n < 2:
        return 0.0
    sd = statistics.stdev(returns)
    mean = statistics.fmean(returns)
    # float noise on a constant series is not dispersion
    if not math.isfinite(sd) or sd <= 1e-12 * max(1.0, abs(mean)):
        return 0.0
    return mean / sd


def _moments(returns: Sequence[float]) -> tuple[float, float]:
    n = len(returns)
    mean = statistics.fmean(returns)
    m2 = sum((x - mean) ** 2 for x in returns) / n
    m3 = sum((x - mean) ** 3 for x in returns) / n
    m4 = sum((x - mean) ** 4 for x in returns) / n
    try:
        skew, kurt = m3 / m2**1.5, m4 / m2**2
    except ZeroDivisionError:  # m2 underflows on near-constant input
        return 0.0, 3.0
    if not (math.isfinite(skew) and math.isfinite(kurt)):
        return 0.0, 3.0
    return skew, kurt


def sharpe_std(returns: Sequence[float]) -> float:
    """Std error of the Sharpe estimate under non-normal returns (Mertens/Lo)."""
    n = len(returns)
    if n < 3:
        return 0.0
    sr = sharpe(returns)
    skew, kurt = _moments(returns)
    var = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """SR0: the Sharpe the best of ``n_trials`` unskilled trials reaches by luck."""
    if n_trials <= 1 or sr_std <= 0:
        return 0.0
    z1 = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sr_std * ((1.0 - EULER) * z1 + EULER * z2)


def deflated_sharpe(
    returns: Sequence[float],
    n_trials: int,
    *,
    trial_sharpe_std: float | None = None,
) -> float:
    """Probability that the true Sharpe exceeds the luck benchmark SR0.

    ``trial_sharpe_std`` is the dispersion of Sharpe across all trials tried.
    When it is unknown the estimator's own std error is used, which is the
    convention in ``tools.entry_filter_replay`` (so both tools agree).
    Undefined input returns 0.0, which fails a threshold instead of passing.
    """
    n = len(returns)
    if n < 3 or n_trials < 1:
        return 0.0
    sr = sharpe(returns)
    se = sharpe_std(returns)
    if sr == 0.0 or se <= 0:
        return 0.0
    sr0 = expected_max_sharpe(n_trials, trial_sharpe_std if trial_sharpe_std else se)
    return float(_NORMAL.cdf((sr - sr0) / se))


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_splits: int
    n_strategies: int
    median_logit: float
    oos_rank_of_is_best: list[float]


def pbo_cscv(
    matrix: Sequence[Sequence[float]],
    *,
    n_blocks: int = 8,
    metric: Callable[[Sequence[float]], float] = sharpe,
) -> PBOResult:
    """Probability of backtest overfitting by CSCV.

    ``matrix[t][k]`` is the return of strategy ``k`` in period ``t`` (rows are
    time-ordered observations, e.g. days). Rows are cut into ``n_blocks``
    contiguous blocks. For every way to pick half the blocks as in-sample, the
    in-sample best strategy is ranked out-of-sample. PBO is the share of
    splits where that strategy lands at or below the OOS median (logit <= 0).
    """
    t_len = len(matrix)
    if t_len == 0:
        raise ValueError("empty matrix")
    n_strat = len(matrix[0])
    if n_strat < 2:
        raise ValueError("PBO needs at least two strategies")
    if n_blocks % 2 or n_blocks < 2:
        raise ValueError("n_blocks must be even and >= 2")
    n_blocks = min(n_blocks, t_len - (t_len % 2)) if t_len >= 2 else 2
    if n_blocks % 2:
        n_blocks -= 1
    size = t_len // n_blocks
    blocks = [list(range(i * size, (i + 1) * size if i < n_blocks - 1 else t_len)) for i in range(n_blocks)]
    logits: list[float] = []
    ranks: list[float] = []
    for is_ids in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_rows = [r for b in is_ids for r in blocks[b]]
        oos_rows = [r for b in range(n_blocks) if b not in is_ids for r in blocks[b]]
        is_score = [metric([matrix[r][k] for r in is_rows]) for k in range(n_strat)]
        oos_score = [metric([matrix[r][k] for r in oos_rows]) for k in range(n_strat)]
        best = max(range(n_strat), key=lambda k: (is_score[k], -k))
        # relative rank in (0, 1): share of strategies the IS winner beats OOS
        below = sum(1 for k in range(n_strat) if oos_score[k] < oos_score[best])
        ties = sum(1 for k in range(n_strat) if oos_score[k] == oos_score[best]) - 1
        omega = (below + 0.5 * ties + 1.0) / (n_strat + 1.0)
        ranks.append(omega)
        logits.append(math.log(omega / (1.0 - omega)))
    pbo = sum(1 for x in logits if x <= 0) / len(logits)
    return PBOResult(pbo, len(logits), n_strat, statistics.median(logits), ranks)


@dataclass(frozen=True)
class BootstrapCI:
    estimate: float
    low: float
    high: float
    n: int
    resamples: int
    p_le_zero: float


def bootstrap_ci(
    values: Sequence[float],
    *,
    stat: Callable[[Sequence[float]], float] = statistics.fmean,
    resamples: int = 2000,
    alpha: float = 0.05,
    block: int = 1,
    seed: int = 744,
) -> BootstrapCI:
    """Percentile bootstrap CI. ``block > 1`` resamples contiguous blocks
    (moving-block bootstrap) to keep serial dependence in a time-ordered
    sequence such as daily PnL or trades in time order.
    """
    n = len(values)
    if n == 0:
        return BootstrapCI(0.0, 0.0, 0.0, 0, 0, 1.0)
    rng = random.Random(seed)
    est = float(stat(values))
    block = max(1, min(block, n))
    starts = n - block + 1
    draws: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < n:
            s = rng.randrange(starts)
            sample.extend(values[s : s + block])
        draws.append(float(stat(sample[:n])))
    draws.sort()
    lo = draws[int(math.floor(alpha / 2 * (resamples - 1)))]
    hi = draws[int(math.ceil((1 - alpha / 2) * (resamples - 1)))]
    p_le = sum(1 for d in draws if d <= 0) / resamples
    return BootstrapCI(est, lo, hi, n, resamples, p_le)
