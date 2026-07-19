"""DCA policy v1 — pure evaluate (no I/O, no order writes). Spec: plans/dca-policy-v1.md"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


POLICY_VERSION = "1"


@dataclass
class DcaContext:
    symbol: str = ""
    cash_mode: str = ""
    fusion_size_mult: float = 1.0
    block_buys: bool = False
    drawdown_active: bool = False
    spendable_dca: float | None = None
    calendar_high_impact: bool = False
    session_low_liquidity: bool = False
    score: int = 0
    max_score: int = 10
    loss_pct: float = 0.0
    size_bias: float = 1.0
    entry_bias: str = "neutral"
    extreme_funding: bool = False
    rag_hit_count: int = 0
    fusion_missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DcaPolicyResult:
    size_mult: float
    skip: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return d


def dca_policy_config(dca_cfg: dict | None) -> dict[str, Any]:
    defaults = {
        "enabled": False,
        "shadow": True,
        "policy_version": POLICY_VERSION,
        "max_policy_mult": 2.0,
        "harvest_mode": "skip",  # skip | soft
        "deploy_mult": 1.35,
        "harvest_mult": 0.4,
        "calendar_mult": 0.5,
        "session_mult": 0.7,
        "drawdown_mult": 0.5,
        "score_boost_mult": 1.25,
        "score_boost_ratio": 0.8,
        "soft_block_mult": 0.6,
        "size_mult_harvest": 0.7,
        "size_mult_deploy": 1.0,
        # D6 observability (#101)
        "log_audit": True,
        "telegram_audit": False,
        "telegram_on_skip_only": True,
    }
    raw = dict((dca_cfg or {}).get("policy") or {})
    return {**defaults, **raw}


def format_dca_policy_audit(
    *,
    symbol: str,
    result: DcaPolicyResult,
    ctx: DcaContext | None = None,
    shadow: bool = False,
    base_usdt: float = 0.0,
    final_usdt: float = 0.0,
    applied: str = "apply",
) -> str:
    """Single-line operator audit (logs / Telegram)."""
    codes = ",".join(result.reason_codes) if result.reason_codes else "-"
    mode = (ctx.cash_mode if ctx else "") or "-"
    sm = float(ctx.fusion_size_mult) if ctx else 1.0
    spd = ctx.spendable_dca if ctx and ctx.spendable_dca is not None else None
    spd_s = f"{spd:.0f}" if spd is not None else "n/a"
    return (
        f"DCA policy {symbol or '?'}: {applied} "
        f"mode={mode} fusion_sm={sm:.2f} "
        f"mult={result.size_mult} skip={result.skip} "
        f"{'shadow ' if shadow else ''}"
        f"reasons=[{codes}] "
        f"usdt={base_usdt:.0f}->{final_usdt:.0f} spendable_dca={spd_s} "
        f"v{result.policy_version}"
    )


def emit_dca_policy_audit(
    *,
    symbol: str,
    result: DcaPolicyResult,
    ctx: DcaContext | None = None,
    shadow: bool = False,
    base_usdt: float = 0.0,
    final_usdt: float = 0.0,
    applied: str = "apply",
    policy_cfg: dict | None = None,
) -> str:
    """Log policy audit; optional Telegram. Returns the audit line."""
    cfg = policy_cfg or {}
    line = format_dca_policy_audit(
        symbol=symbol,
        result=result,
        ctx=ctx,
        shadow=shadow,
        base_usdt=base_usdt,
        final_usdt=final_usdt,
        applied=applied,
    )
    if cfg.get("log_audit", True):
        try:
            from logger import log

            log(line, "INFO")
        except Exception:
            pass
    want_tg = bool(cfg.get("telegram_audit"))
    if want_tg and cfg.get("telegram_on_skip_only", True) and not result.skip:
        want_tg = False
    if want_tg:
        try:
            from telegram_notifier import send_telegram_message

            send_telegram_message(f"📊 <code>{line}</code>")
        except Exception:
            pass
    return line


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return float(default)


def evaluate_dca_policy(
    ctx: DcaContext,
    policy_cfg: dict | None = None,
) -> DcaPolicyResult:
    """Apply factor table; skip beats size; clamp mult to [0, max_policy_mult]."""
    cfg = policy_cfg if isinstance(policy_cfg, dict) else {}
    # Allow full dca.policy section or already-resolved defaults
    if "deploy_mult" not in cfg and "policy" in cfg:
        cfg = dca_policy_config(cfg)
    elif "max_policy_mult" not in cfg and "enabled" not in cfg:
        cfg = {**dca_policy_config(None), **cfg}
    else:
        cfg = {**dca_policy_config(None), **cfg}

    mult = 1.0
    reasons: list[str] = []
    skip = False

    mode = str(ctx.cash_mode or "").upper()
    sm = float(ctx.fusion_size_mult if ctx.fusion_size_mult is not None else 1.0)
    harvest_thr = _f(cfg, "size_mult_harvest", 0.7)
    deploy_thr = _f(cfg, "size_mult_deploy", 1.0)

    if ctx.fusion_missing:
        reasons.append("fail_open_fusion")

    # 1) HARVEST / risk-off
    harvest = (
        mode == "HARVEST"
        or bool(ctx.block_buys)
        or sm < harvest_thr
    )
    if harvest:
        hmode = str(cfg.get("harvest_mode") or "skip").lower()
        if hmode == "soft":
            mult *= _f(cfg, "harvest_mult", 0.4)
            reasons.append("harvest_soft")
        else:
            skip = True
            reasons.append("harvest_skip")
        if ctx.block_buys:
            reasons.append("block_buys")
        if sm < harvest_thr and mode != "HARVEST":
            reasons.append("low_size_mult")

    # 2) DEPLOY boost
    if not skip and (mode == "DEPLOY" or sm >= deploy_thr):
        mult *= _f(cfg, "deploy_mult", 1.35)
        reasons.append("deploy_boost")
    elif not skip and (mode == "STEADY" or not mode):
        reasons.append("steady")

    # 3) Calendar
    if not skip and ctx.calendar_high_impact:
        mult *= _f(cfg, "calendar_mult", 0.5)
        reasons.append("calendar")
        if mult < 0.35:
            skip = True
            reasons.append("calendar_skip")

    # 4) Session
    if not skip and ctx.session_low_liquidity:
        mult *= _f(cfg, "session_mult", 0.7)
        reasons.append("session")

    # 5) Drawdown
    if not skip and ctx.drawdown_active:
        mult *= _f(cfg, "drawdown_mult", 0.5)
        reasons.append("drawdown")

    # 6) Funding
    if not skip and ctx.extreme_funding:
        skip = True
        reasons.append("funding")

    # 7) Profile soft_block — mult only
    if not skip and str(ctx.entry_bias or "").lower() == "soft_block":
        mult *= _f(cfg, "soft_block_mult", 0.6)
        reasons.append("profile_soft_block")

    # 8) size_bias
    bias = float(ctx.size_bias if ctx.size_bias is not None else 1.0)
    if not skip and bias < 0.75:
        mult *= max(0.5, bias)
        reasons.append("size_bias")

    # 9) Score boost (not in harvest)
    max_s = max(1, int(ctx.max_score or 10))
    ratio = _f(cfg, "score_boost_ratio", 0.8)
    if not skip and not harvest and int(ctx.score or 0) >= ratio * max_s:
        mult *= _f(cfg, "score_boost_mult", 1.25)
        reasons.append("score_boost")

    max_m = max(0.0, _f(cfg, "max_policy_mult", 2.0))
    mult = max(0.0, min(max_m, mult))
    if skip:
        # still report mult for audit but candidate dropped when not shadow
        pass

    return DcaPolicyResult(
        size_mult=round(mult, 4),
        skip=bool(skip),
        reason_codes=tuple(reasons),
        policy_version=str(cfg.get("policy_version") or POLICY_VERSION),
    )


def apply_policy_to_usdt(
    base_usdt: float,
    result: DcaPolicyResult,
    *,
    spendable_dca: float | None = None,
    shadow: bool = False,
) -> float:
    """Scale usdt by policy; optionally cap by spendable_dca. Shadow keeps base."""
    if shadow:
        return float(base_usdt)
    usdt = float(base_usdt) * float(result.size_mult)
    if spendable_dca is not None and spendable_dca >= 0:
        usdt = min(usdt, float(spendable_dca))
    return max(0.0, usdt)
