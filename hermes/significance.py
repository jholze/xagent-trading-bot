"""Block-bootstrap significance for Hermes walk-forward fold deltas (#308).

Operator-facing number is a win probability, never a p-value:
``Gewinnwahrscheinlichkeit 0,97 bei 41 Trades``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

DEFAULT_BLOCK_LENGTH = 2
DEFAULT_N_RESAMPLES = 1000
DEFAULT_SEED = 308


def fold_sharpe_deltas(
    baseline_folds: Sequence[dict],
    variant_folds: Sequence[dict],
) -> list[float]:
    """Per-fold Sharpe(variant) − Sharpe(baseline), skipping excluded folds."""
    by_id = {
        f.get("fold_id"): f
        for f in baseline_folds
        if not f.get("excluded")
    }
    deltas: list[float] = []
    for vf in variant_folds:
        if vf.get("excluded"):
            continue
        bf = by_id.get(vf.get("fold_id"))
        if bf is None:
            continue
        deltas.append(float(vf.get("sharpe") or 0) - float(bf.get("sharpe") or 0))
    return deltas


def total_in_sample_trades(folds: Sequence[dict]) -> int:
    return sum(int(f.get("trades") or 0) for f in folds if not f.get("excluded"))


def block_bootstrap_win_probability(
    deltas: Sequence[float],
    *,
    block_length: int = DEFAULT_BLOCK_LENGTH,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> float:
    """P(mean_delta > 0) via moving-block bootstrap of fold Sharpe deltas.

    Block length 2, 1000 resamples, seeded RNG so tests are deterministic.
    """
    d = [float(x) for x in deltas]
    n = len(d)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0 if d[0] > 0 else 0.0

    bl = max(1, min(int(block_length), n))
    rng = random.Random(int(seed))
    blocks = [d[i : i + bl] for i in range(n - bl + 1)]
    n_need = int(math.ceil(n / bl))
    n_positive = 0
    for _ in range(int(n_resamples)):
        sample: list[float] = []
        for _j in range(n_need):
            sample.extend(rng.choice(blocks))
        sample = sample[:n]
        if sum(sample) / n > 0:
            n_positive += 1
    return n_positive / float(n_resamples)


def tightened_threshold(min_win_probability: float, n_variables: int) -> float:
    """Bonferroni-style: ``1 − (1 − min_win_probability) / n``."""
    n = max(1, int(n_variables))
    p = float(min_win_probability)
    return 1.0 - (1.0 - p) / n


def format_win_probability(win_probability: float, total_trades: int) -> str:
    body = f"{float(win_probability):.2f}".replace(".", ",")
    return f"Gewinnwahrscheinlichkeit {body} bei {int(total_trades)} Trades"
