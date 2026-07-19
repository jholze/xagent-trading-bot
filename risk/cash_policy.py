"""Adaptive cash policy — pure evaluation (no ledger / order writes).

Phase 0–1 of plans/adaptive-cash-rotation-master.md:
  - mode DEPLOY | STEADY | HARVEST from fusion size_mult / block_buys / drawdown
  - floor_pct_eff clamped between min/max
  - dual spendable: spendable_new vs spendable_dca (DCA buffer not eaten by floor)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


MODE_DEPLOY = "DEPLOY"
MODE_STEADY = "STEADY"
MODE_HARVEST = "HARVEST"


@dataclass(frozen=True)
class CashPolicyResult:
    enabled: bool
    mode: str
    floor_pct_eff: float
    floor_abs: float
    spendable_new: float
    spendable_dca: float
    dca_buffer_target: float
    size_mult: float
    block_buys: bool
    drawdown_active: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return d


def cash_policy_section(risk_config: dict | None) -> dict[str, Any]:
    risk = risk_config if isinstance(risk_config, dict) else {}
    raw = risk.get("cash_policy")
    return dict(raw) if isinstance(raw, dict) else {}


def is_cash_policy_enabled(risk_config: dict | None) -> bool:
    return bool(cash_policy_section(risk_config).get("enabled"))


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return float(default)


def resolve_cash_mode(
    *,
    size_mult: float,
    block_buys: bool = False,
    drawdown_active: bool = False,
    size_mult_deploy: float = 1.0,
    size_mult_harvest: float = 0.7,
) -> str:
    """Map fusion-style inputs to DEPLOY / STEADY / HARVEST."""
    sm = float(size_mult)
    if block_buys or sm < float(size_mult_harvest):
        return MODE_HARVEST
    if sm >= float(size_mult_deploy):
        return MODE_DEPLOY
    # Drawdown alone does not force HARVEST mode label for spendable rules,
    # but adds floor delta in floor_pct_effective.
    _ = drawdown_active
    return MODE_STEADY


def floor_pct_effective(
    *,
    floor_pct_base: float,
    size_mult: float,
    block_buys: bool = False,
    drawdown_active: bool = False,
    floor_pct_min: float = 5.0,
    floor_pct_max: float = 25.0,
    rotation_pressure_pct: float = 0.0,
    recovery_dca_boost: bool = False,
    size_mult_deploy: float = 1.0,
    size_mult_harvest: float = 0.7,
    regime_deploy_delta: float = -4.0,
    regime_harvest_delta: float = 6.0,
    drawdown_delta: float = 4.0,
    recovery_floor_delta: float = -3.0,
) -> tuple[float, str, list[str]]:
    """Return (floor_pct_eff, mode, reason_codes)."""
    reasons: list[str] = []
    mode = resolve_cash_mode(
        size_mult=size_mult,
        block_buys=block_buys,
        drawdown_active=drawdown_active,
        size_mult_deploy=size_mult_deploy,
        size_mult_harvest=size_mult_harvest,
    )
    reasons.append(f"mode={mode}")

    if mode == MODE_HARVEST:
        regime_delta = float(regime_harvest_delta)
        reasons.append("regime_harvest")
    elif mode == MODE_DEPLOY:
        regime_delta = float(regime_deploy_delta)
        reasons.append("regime_deploy")
    else:
        regime_delta = 0.0
        reasons.append("regime_steady")

    dd_delta = float(drawdown_delta) if drawdown_active else 0.0
    if drawdown_active:
        reasons.append("drawdown")

    rec_delta = float(recovery_floor_delta) if recovery_dca_boost else 0.0
    if recovery_dca_boost:
        reasons.append("recovery_dca_boost")

    rot = max(0.0, float(rotation_pressure_pct or 0.0))
    if rot:
        reasons.append(f"rotation_pressure={rot:.1f}")

    raw = (
        float(floor_pct_base)
        + regime_delta
        + dd_delta
        + rec_delta
        - rot
    )
    lo = float(floor_pct_min)
    hi = float(floor_pct_max)
    if lo > hi:
        lo, hi = hi, lo
    clamped = max(lo, min(hi, raw))
    if clamped != raw:
        reasons.append("clamped")
    return clamped, mode, reasons


def compute_dual_spendable(
    *,
    cash_total: float,
    floor_abs: float,
    equity: float,
    dca_buffer_usdt: float = 800.0,
    dca_buffer_pct_equity: float = 1.5,
    dca_floor_haircut: float = 0.0,
    mode: str = MODE_STEADY,
    harvest_dca_buffer_mult: float = 0.5,
) -> tuple[float, float, float]:
    """Return (spendable_new, spendable_dca, dca_buffer_target).

    haircut 0: DCA budget may use cash even when cash ≈ floor (Phase 0 unblock).
    haircut 1: DCA only from cash above floor.
    New-entry always keeps floor + reserved DCA budget.
    """
    cash = max(0.0, float(cash_total))
    floor = max(0.0, float(floor_abs))
    eq = max(0.0, float(equity))
    pct_part = eq * (max(0.0, float(dca_buffer_pct_equity)) / 100.0)
    target = max(max(0.0, float(dca_buffer_usdt)), pct_part)
    if str(mode).upper() == MODE_HARVEST:
        target *= max(0.0, float(harvest_dca_buffer_mult))

    haircut = max(0.0, min(1.0, float(dca_floor_haircut)))
    # cash available for DCA pool before cap
    dca_pool = max(0.0, cash - floor * haircut)
    dca_budget = min(target, dca_pool)

    spendable_new = max(0.0, cash - floor - dca_budget)
    spendable_dca = dca_budget
    return spendable_new, spendable_dca, target


def evaluate_cash_policy(
    *,
    cash_total: float,
    basis_for_floor: float,
    equity: float,
    size_mult: float = 1.0,
    block_buys: bool = False,
    drawdown_active: bool = False,
    risk_config: dict | None = None,
    rotation_pressure_pct: float = 0.0,
    recovery_dca_boost: bool = False,
) -> CashPolicyResult:
    """Full policy evaluation from injectable numbers (unit-test friendly)."""
    risk = risk_config if isinstance(risk_config, dict) else {}
    cfg = cash_policy_section(risk)
    if not cfg.get("enabled"):
        # Legacy static floor for callers that still want a structured result
        pct = float(risk.get("cash_floor_pct", 0) or 0)
        basis = max(0.0, float(basis_for_floor))
        floor_abs = max(0.0, basis * (pct / 100.0)) if pct > 0 else 0.0
        cash = max(0.0, float(cash_total))
        free = max(0.0, cash - floor_abs)
        return CashPolicyResult(
            enabled=False,
            mode=MODE_STEADY,
            floor_pct_eff=pct,
            floor_abs=floor_abs,
            spendable_new=free,
            spendable_dca=free,
            dca_buffer_target=0.0,
            size_mult=float(size_mult),
            block_buys=bool(block_buys),
            drawdown_active=bool(drawdown_active),
            reason_codes=("legacy_static_floor",),
        )

    base = _f(cfg, "floor_pct_base", float(risk.get("cash_floor_pct", 12) or 12))
    floor_min = _f(cfg, "floor_pct_min", 5.0)
    floor_max = _f(cfg, "floor_pct_max", 25.0)
    sm_deploy = _f(cfg, "size_mult_deploy", 1.0)
    sm_harvest = _f(cfg, "size_mult_harvest", 0.7)
    deploy_d = _f(cfg, "regime_deploy_delta", -4.0)
    harvest_d = _f(cfg, "regime_harvest_delta", 6.0)
    dd_d = _f(cfg, "drawdown_delta", 4.0)
    rec_d = _f(cfg, "recovery_floor_delta", -3.0)

    link = cfg.get("link_fusion_size_mult", True)
    sm = float(size_mult) if link else 1.0
    bb = bool(block_buys) if link else False

    pct_eff, mode, reasons = floor_pct_effective(
        floor_pct_base=base,
        size_mult=sm,
        block_buys=bb,
        drawdown_active=bool(drawdown_active),
        floor_pct_min=floor_min,
        floor_pct_max=floor_max,
        rotation_pressure_pct=float(rotation_pressure_pct or 0),
        recovery_dca_boost=bool(recovery_dca_boost),
        size_mult_deploy=sm_deploy,
        size_mult_harvest=sm_harvest,
        regime_deploy_delta=deploy_d,
        regime_harvest_delta=harvest_d,
        drawdown_delta=dd_d,
        recovery_floor_delta=rec_d,
    )
    basis = max(0.0, float(basis_for_floor))
    floor_abs = max(0.0, basis * (pct_eff / 100.0))

    sn, sd, target = compute_dual_spendable(
        cash_total=float(cash_total),
        floor_abs=floor_abs,
        equity=float(equity),
        dca_buffer_usdt=_f(cfg, "dca_buffer_usdt", 800.0),
        dca_buffer_pct_equity=_f(cfg, "dca_buffer_pct_equity", 1.5),
        dca_floor_haircut=_f(cfg, "dca_floor_haircut", 0.0),
        mode=mode,
        harvest_dca_buffer_mult=_f(cfg, "harvest_dca_buffer_mult", 0.5),
    )
    return CashPolicyResult(
        enabled=True,
        mode=mode,
        floor_pct_eff=pct_eff,
        floor_abs=floor_abs,
        spendable_new=sn,
        spendable_dca=sd,
        dca_buffer_target=target,
        size_mult=sm,
        block_buys=bb,
        drawdown_active=bool(drawdown_active),
        reason_codes=tuple(reasons),
    )
