"""Session/macro regime rules (MC-8)."""

from __future__ import annotations

from typing import Any

from intelligence.macro.session_clock import SessionStatus


def apply_regime_rules(
    session: SessionStatus,
    *,
    in_macro_pre_window: bool = False,
    macro_event_code: str | None = None,
    high_impact: bool = False,
    config: dict | None = None,
    data_complete: bool = True,
) -> dict[str, Any]:
    """Return regime flags + suggested size mults (fail-open defaults).

    ``data_complete=False`` marks the result unmeasured without changing the
    1.0 multipliers (consumers that care read ``measured``).
    """
    cfg = config or {}
    fakeout_mult = float(cfg.get("fakeout_size_mult", 0.5))
    pre_mult = float(cfg.get("size_mult_pre_high_impact", 0.5))

    session_mult = 1.0
    calendar_mult = 1.0
    tags: list[str] = []
    fakeout_risk = float(session.fakeout_risk or 0.0)

    if session.asia_open and session.low_volume:
        session_mult = min(session_mult, fakeout_mult)
        fakeout_risk = max(fakeout_risk, 0.7)
        tags.append("asia_open_low_vol_fakeout")
    elif session.asia_open:
        tags.append("asia_open")
        fakeout_risk = max(fakeout_risk, 0.25)

    if session.overlap_london_ny:
        tags.append("london_ny_overlap")
        # default neutral — no automatic size up

    if in_macro_pre_window and high_impact:
        calendar_mult = min(calendar_mult, pre_mult)
        tags.append(f"macro_pre_{macro_event_code or 'HIGH'}")

    return {
        "session_mult": round(session_mult, 4),
        "calendar_mult": round(calendar_mult, 4),
        "fakeout_risk": round(fakeout_risk, 3),
        "tags": tags,
        "regime": tags[0] if tags else "neutral",
        "measured": bool(data_complete),
    }
