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
    fusion_fresh: bool = True
    fusion_measured: bool = True
    fusion_degraded: bool = False
    fail_closed_guards: str = "log"
    # #103 coin-fact layer (fail-open defaults)
    fact_hard_negative: bool = False
    fact_unlock: bool = False
    fact_profit_taking: bool = False
    fact_flow_only: bool = False
    fact_structure_risk: bool = False
    fact_volume_breakout: bool = False
    fact_catalyst: bool = False
    fact_utility: bool = False
    fact_noise_only: bool = False
    fact_event_count: int = 0
    fact_min_impact: float = 0.0
    fact_summary: str = ""
    # P6: DCA lesson memory (advisory observability)
    dca_lesson_count: int = 0
    dca_lesson_summary: str = ""

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
        # D4 persist (#98)
        "persist_events": True,
        "index_rag": True,
        # D4b /ask live snapshot (#99)
        "ask_snapshot": True,
        # #103 coin facts (multipliers; skip beats size)
        "fact_unlock_mult": 0.5,
        "fact_profit_taking_mult": 0.7,
        "fact_flow_only_mult": 0.8,
        "fact_structure_risk_mult": 0.5,
        "fact_volume_breakout_mult": 1.1,
        "fact_catalyst_mult": 1.1,
        "fact_utility_mult": 1.1,
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
    size_bias = float(ctx.size_bias) if ctx else 1.0
    entry_bias = (ctx.entry_bias if ctx else "") or "neutral"
    dca_n = int(ctx.dca_lesson_count) if ctx else 0
    dca_sum = (ctx.dca_lesson_summary if ctx else "") or ""
    dca_part = f" dca_lessons={dca_n}"
    if dca_sum:
        dca_part += f"({dca_sum[:48]})"
    return (
        f"DCA policy {symbol or '?'}: {applied} "
        f"mode={mode} fusion_sm={sm:.2f} "
        f"size_bias={size_bias:.2f} entry_bias={entry_bias} "
        f"mult={result.size_mult} skip={result.skip} "
        f"{'shadow ' if shadow else ''}"
        f"reasons=[{codes}] "
        f"usdt={base_usdt:.0f}->{final_usdt:.0f} spendable_dca={spd_s}"
        f"{dca_part} "
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
    # D4: memory + optional RAG (fail-open)
    if cfg.get("persist_events", True):
        try:
            from strategies.dca_decision_event import persist_dca_decision_event

            persist_dca_decision_event(
                symbol=symbol,
                result=result,
                ctx=ctx,
                shadow=shadow,
                base_usdt=base_usdt,
                final_usdt=final_usdt,
                applied=applied,
                policy_cfg=cfg,
            )
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

    deny = str(getattr(ctx, "fail_closed_guards", "log") or "log").strip().lower() == "deny"
    measured = bool(getattr(ctx, "fusion_measured", True))
    fresh = bool(getattr(ctx, "fusion_fresh", True))
    degraded = bool(getattr(ctx, "fusion_degraded", False))
    allow_deploy = True
    if deny:
        allow_deploy = bool(measured and fresh) and not degraded

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

    # 2) DEPLOY boost — under deny only when fusion is measured and fresh
    if not skip and allow_deploy and (mode == "DEPLOY" or sm >= deploy_thr):
        mult *= _f(cfg, "deploy_mult", 1.35)
        reasons.append("deploy_boost")
    elif not skip and (mode == "STEADY" or not mode or not allow_deploy):
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

    # 10) Coin facts (#103) — declarative rules in dca_fact_policy
    from strategies.dca_fact_policy import apply_coin_fact_policy

    mult, skip, reasons = apply_coin_fact_policy(
        ctx, cfg, mult=mult, skip=skip, reasons=reasons
    )

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


# #640 add rules. The lock document is read only through dca_blocked.


_CODE_MISSING = "dca_guard_missing_input"
_CODE_LOCKED = "dca_guard_locked"
_CODE_BELOW = "dca_guard_below_avg"
_CODE_ROUNDS = "dca_guard_max_rounds"


@dataclass
class DcaGuardResult:
    blocked: bool
    codes: list[str] = field(default_factory=list)
    price: float | None = None
    avg: float | None = None
    dca_rounds: int | None = None
    locked: bool | None = None

    @property
    def code(self) -> str:
        return self.codes[0] if self.codes else ""


def is_human_operator_buy(source: str | None) -> bool:
    """True only for the exact source ``manual``.

    Prefixes (``manual_x``), aliases (``operator``, ``telegram``, ``user``,
    ``confirm``) and every ``mcp:`` source are automatic. A missing source
    is not a human buy. ``is_manual_source`` stays the sell/lock check and
    is intentionally not used here.
    """
    return str(source or "").strip().lower() == "manual"


def _price_is_stale(symbol: str, indicators: dict | None) -> bool:
    from price_fetcher import _stale_price_max_age_sec, stale_expired_symbols

    max_age = float(_stale_price_max_age_sec())
    if isinstance(indicators, dict) and indicators.get("price_age_sec") is not None:
        try:
            age = float(indicators.get("price_age_sec"))
        except (TypeError, ValueError):
            return True
        if age > max_age:
            return True
    try:
        expired = stale_expired_symbols()
    except Exception:
        return True
    return str(symbol or "") in expired


def _lock_state(pos: dict) -> bool | None:
    """One read of the lock document via ``dca_blocked``. None if it cannot be read."""
    raw = pos.get("lock") if isinstance(pos, dict) else None
    if "lock" in pos and raw is not None and not isinstance(raw, dict):
        return None
    try:
        from strategies.position_lock import dca_blocked

        blocked, _msg = dca_blocked(pos)
        return bool(blocked)
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def evaluate_dca_guard(
    pos: dict | None,
    *,
    price: float | None,
    source: str | None,
    has_open_lot: bool,
    symbol: str = "",
    indicators: dict | None = None,
) -> DcaGuardResult:
    """Add-on rules. New entries (no open lot) are not this guard.

    A human manual source is still evaluated. The caller logs that result
    and does not auto-block it.
    """
    del source
    if not has_open_lot:
        return DcaGuardResult(blocked=False, price=_as_float(price))

    if not isinstance(pos, dict):
        return DcaGuardResult(
            blocked=True,
            codes=[_CODE_MISSING],
            price=_as_float(price),
            locked=None,
        )

    live = _as_float(price)
    avg = _as_float(pos.get("average_entry"))
    if avg is None:
        avg = _as_float(pos.get("entry_price"))
    rounds_raw = pos.get("dca_rounds") if "dca_rounds" in pos else None
    rounds: int | None
    if rounds_raw is None:
        rounds = None
    else:
        try:
            rounds = int(rounds_raw)
        except (TypeError, ValueError):
            rounds = None
    locked = _lock_state(pos)
    stale = False
    try:
        stale = _price_is_stale(symbol or str(pos.get("symbol") or ""), indicators)
    except Exception:
        stale = True

    missing = (
        live is None
        or live <= 0
        or avg is None
        or avg <= 0
        or rounds is None
        or rounds < 0
        or locked is None
        or stale
    )
    if missing:
        return DcaGuardResult(
            blocked=True,
            codes=[_CODE_MISSING],
            price=live,
            avg=avg,
            dca_rounds=rounds,
            locked=locked,
        )

    codes: list[str] = []
    if locked:
        codes.append(_CODE_LOCKED)
    if live < avg:
        codes.append(_CODE_BELOW)
    if rounds >= 1:
        codes.append(_CODE_ROUNDS)
    return DcaGuardResult(
        blocked=bool(codes),
        codes=codes,
        price=live,
        avg=avg,
        dca_rounds=rounds,
        locked=bool(locked),
    )


def policy_skip_for_guard(
    pos: dict | None,
    price: float | None,
    *,
    symbol: str = "",
    indicators: dict | None = None,
) -> DcaPolicyResult | None:
    """Skip the policy buy when the add rules would block it."""
    result = evaluate_dca_guard(
        pos,
        price=price,
        source="dca",
        has_open_lot=True,
        symbol=symbol,
        indicators=indicators,
    )
    if not result.blocked:
        return None
    return DcaPolicyResult(
        size_mult=1.0,
        skip=True,
        reason_codes=tuple(result.codes),
    )
