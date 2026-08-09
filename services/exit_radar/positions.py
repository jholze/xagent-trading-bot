"""Load open ledger positions for exit radar (read-only)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def load_open_positions(scope: str) -> list[dict[str, Any]]:
    """Load open ledger positions + resolved exit params (read-only)."""
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    # Prefer public Railway proxy when present (internal hostnames fail locally)
    pub = os.environ.get("MONGO_PUBLIC_URL") or ""
    if pub and not os.environ.get("MONGO_URL"):
        os.environ["MONGO_URL"] = pub
    if os.environ.get("MONGO_URL") and "railway.internal" not in os.environ.get(
        "MONGO_URL", ""
    ):
        os.environ.setdefault("DEMO_ALLOW_REMOTE_MONGO", "1")
    try:
        from scripts.operator_mongo import prepare_operator_mongo

        meta = prepare_operator_mongo()
        print(
            f"[{_ts()}] mongo db={meta.get('db')} host={meta.get('host')}",
            flush=True,
        )
    except Exception as e:
        print(f"[{_ts()}] operator_mongo skip: {e}", flush=True)

    from strategies.positions import is_open_position, load_positions, positions
    from strategies.registry import resolve_strategy_params

    load_positions(scope)
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    global_sl = float(raw.get("stop_loss_pct") or 50)
    partial_default = float(raw.get("partial_stop_pct") or 25)

    out: list[dict[str, Any]] = []
    for key, pos in positions.items():
        if not is_open_position(pos):
            continue
        base, _, tf = key.rpartition("_")
        symbol = base.replace("_", "/")
        entry = float(pos.get("average_entry") or 0)
        if entry <= 0:
            continue
        coin = {"symbol": symbol, "timeframe": tf}
        try:
            params = resolve_strategy_params(
                coin, has_position=True, frozen_tier=pos.get("strategy_tier")
            )
        except TypeError:
            params = resolve_strategy_params(coin, has_position=True)
        except Exception:
            params = {}

        ttp = dict(params.get("trailing_take_profit") or {})
        ts = dict(params.get("trailing_stop") or {})
        life = dict(params.get("profit_max_lifetime") or {})
        sl = params.get("stop_loss_pct")
        if sl is None:
            sl = global_sl

        # Position lock summary (optional)
        lock_active = False
        lock_modes: list[str] = []
        try:
            from strategies.position_lock import get_lock, lock_is_active, lock_modes as _lock_modes

            lk = get_lock(pos)
            if lk and lock_is_active(lk):
                lock_active = True
                lock_modes = sorted(_lock_modes(lk))
        except Exception:
            pass

        out.append(
            {
                "symbol": symbol,
                "timeframe": tf,
                "entry": entry,
                "amount": float(pos.get("amount") or 0),
                "recent_high": float(pos.get("recent_high") or 0),
                "peak_epoch_high": float(pos.get("peak_epoch_high") or 0) or None,
                "strategy_tier": pos.get("strategy_tier"),
                "first_buy_at": pos.get("first_buy_at") or pos.get("entry_at"),
                "profit_armed_at": pos.get("profit_armed_at"),
                "trail_tp_steps": int(pos.get("trail_tp_steps") or 0),
                "sold_percent": float(pos.get("sold_percent") or 0),
                "dca_rounds": int(pos.get("dca_rounds") or 0),
                # DCA sniper / recovery_hold (live board)
                "recovery_hold": bool(pos.get("recovery_hold")),
                "sniper_focus": bool(pos.get("sniper_focus")),
                "dca_heavy_used": bool(pos.get("dca_heavy_used")),
                "last_sniper_score": pos.get("last_sniper_score"),
                "last_sniper_reason": pos.get("last_sniper_reason"),
                "position_locked": lock_active,
                "lock_modes": lock_modes,
                "ttp": {
                    "enabled": bool(ttp.get("enabled", False)),
                    "arm_gain_pct": float(ttp.get("arm_gain_pct") or 12),
                    "trail_pct": float(ttp.get("trail_pct") or 6),
                    "trail_pct_min": float(ttp.get("trail_pct_min") or 3),
                    "trail_pct_max": float(ttp.get("trail_pct_max") or 12),
                    "trail_pct_scale_start_pct": float(
                        ttp.get("trail_pct_scale_start_pct") or 18
                    ),
                    "trail_pct_scale_peak_pct": float(
                        ttp.get("trail_pct_scale_peak_pct") or 45
                    ),
                    "dynamic_trail": bool(ttp.get("dynamic_trail", True)),
                    "min_gain_pct": float(
                        ttp.get("min_gain_pct_floor")
                        or ttp.get("min_gain_pct")
                        or 8
                    ),
                    "cooldown_hours": float(ttp.get("cooldown_hours") or 6),
                },
                "trailing_stop": {
                    "enabled": bool(ts.get("enabled", True)),
                    "activation_gain_pct": float(ts.get("activation_gain_pct") or 5),
                    "min_trail_pct": float(ts.get("min_trail_pct") or 8),
                    "max_trail_pct": float(ts.get("max_trail_pct") or 25),
                    "atr_multiplier": float(ts.get("atr_multiplier") or 2),
                },
                "stop_loss_pct": float(sl),
                "partial_stop_pct": float(
                    params.get("partial_stop_pct") or partial_default
                ),
                "safety_tp_pct": params.get("safety_tp_pct"),
                "safety_tp_min_gain_pct": params.get("safety_tp_min_gain_pct"),
                "take_profit_tiers": list(params.get("take_profit_tiers") or []),
                "rsi_sell_min_gain_pct": params.get("rsi_sell_min_gain_pct"),
                "bb_sell_min_gain_pct": params.get("bb_sell_min_gain_pct"),
                "life": {
                    "enabled": bool(life.get("enabled")),
                    "arm_gain_pct": float(life.get("arm_gain_pct") or 3),
                    "max_hours": float(life.get("max_hours") or 96),
                    "min_gain_pct": float(life.get("min_gain_pct") or 1),
                    "skip_if_peak_above_pct": float(
                        life.get("skip_if_peak_above_pct") or 40
                    ),
                },
                "prefer_full_close": True,
            }
        )
    out.sort(key=lambda r: r["symbol"])
    return out



