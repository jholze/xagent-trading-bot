"""Single source of truth for sniper DD / reclaim size gates."""

from __future__ import annotations

from typing import Any


def dd_pct_from_loss(loss_pct: float) -> float:
    """Absolute drawdown percent (positive number)."""
    return abs(min(0.0, float(loss_pct or 0)))


def dd_band_ok(
    loss_pct: float,
    cfg: dict[str, Any] | None = None,
    *,
    for_checklist: bool = False,
) -> tuple[bool, str]:
    """Whether loss is inside the sniper recovery band.

    Config keys (``dca_sniper`` section):
      min_dd_pct_for_dca (default 12)
      max_dd_pct_for_dca (default 55)

    Checklist and sizer share this band so candidates are not score-0 while
    size still thinks the bag is in range (or vice versa).
    """
    cfg = cfg or {}
    dd = dd_pct_from_loss(loss_pct)
    min_dd = float(cfg.get("min_dd_pct_for_dca") or 12)
    max_dd = float(cfg.get("max_dd_pct_for_dca") or 55)
    # checklist historically used a slightly tighter floor for "interesting"
    # bags; keep optional soft floor of 3pp when for_checklist unless configured.
    if for_checklist and "checklist_min_dd_pct" in cfg:
        min_dd = float(cfg.get("checklist_min_dd_pct") or min_dd)
    elif for_checklist:
        # align with sizing by default (was -40..-3 hard coded)
        min_dd = min_dd  # noqa: PLW0127 — explicit: same band
    if dd < min_dd:
        return False, "loss_too_shallow"
    if dd > max_dd:
        return False, "loss_too_deep"
    return True, "ok"


def reclaim_allows_dca(
    *,
    reclaim_ok: bool | None,
    free_fall: bool | None,
    require_reclaim: bool = True,
) -> tuple[bool, str]:
    if free_fall is True:
        return False, "free_fall"
    if require_reclaim and reclaim_ok is False:
        return False, "no_reclaim"
    return True, "ok"
