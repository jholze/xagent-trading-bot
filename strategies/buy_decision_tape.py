"""#628 Buy-Decision-Tape (observe only).

One tape row per BUY order that reaches RiskManager.evaluate. Not every
scanner tick. Skip/hold that never calls evaluate is out of v1. There is
no second emit in decision_engine.

fire_enabled is never applied. This module does not add a buy filter, a
score, a size change, or a money-path gate. The order path is unchanged.

Durable file: logs/buy_decision_tape.jsonl under logger.LOG_DIR. Staging
volume is xagent-test-volume mounted at /app/logs (see
docs/WQE_STAGING_SOAK.md). Same Ist pattern as logs/risk_rejects.jsonl.
No new Mongo collection, no Redis, no second analytics stack.

Write with services.observability_store.append_jsonl and
maybe_rotate_jsonl. Rotate at max_bytes=8_000_000, keep_lines=20_000.
maybe_rotate keeps a `.1` sibling. That rotate window is the retention
(hot tail 20_000 rows plus the previous file). Soak length is that
volume file, not an in-memory buffer.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from core.models import RiskDecision, TradeOrder
from logger import log

TAPE_FILENAME = "buy_decision_tape.jsonl"
ROTATE_MAX_BYTES = 8_000_000
ROTATE_KEEP_LINES = 20_000
_AMOUNT_EPS = 1e-12
_UNDER_TEST_ENV = "BUY_DECISION_TAPE_UNDER_TEST"

ROW_KEYS = (
    "ts",
    "symbol",
    "tenant",
    "outcome",
    "signal",
    "filter_codes",
    "book",
    "macro_stress",
    "correlation_id",
)

MACRO_STRESS_KEYS = (
    "reason",
    "regime",
    "calendar_mult",
    "session_mult",
    "pm_mult",
    "block_new_entries",
    "would_block",
    "would_cut",
    "observe_enabled",
    "fire_enabled",
    "sources_ok",
    "bias_error",
    "mults_error",
)


def tape_path() -> str:
    from logger import LOG_DIR

    return os.path.join(LOG_DIR, TAPE_FILENAME)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tape_enabled(config: dict | None = None) -> bool:
    """Env BUY_DECISION_TAPE=0/1/false/true overrides; else config; else True."""
    try:
        env = (os.environ.get("BUY_DECISION_TAPE") or "").strip().lower()
        if env in ("0", "false"):
            return False
        if env in ("1", "true"):
            return True
        if config is None:
            try:
                from core.config import get_bot_config

                config = get_bot_config().raw
            except Exception:
                config = None
        sec = (config or {}).get("buy_decision_tape") if isinstance(config, dict) else None
        if isinstance(sec, dict) and "enabled" in sec:
            return bool(sec.get("enabled"))
        return True
    except Exception:
        return True


def _file_write_allowed() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(_UNDER_TEST_ENV) != "1":
        return False
    return True


def _correlation_id(order: TradeOrder) -> str:
    for attr in ("idempotency_key", "client_order_id", "order_id"):
        val = str(getattr(order, attr, "") or "").strip()
        if val:
            return val
    return uuid.uuid4().hex


def _lookup_position(symbol: str, timeframe: str) -> dict:
    import risk.risk_manager as risk_manager

    return risk_manager.get_position(symbol, timeframe)


def _copy_macro_stress(rec: Any) -> dict[str, Any] | None:
    if not isinstance(rec, dict):
        return None
    return {key: rec[key] if key in rec else None for key in MACRO_STRESS_KEYS}


def _observe_macro_stress(
    config: dict | None,
    *,
    has_position: bool,
    action: str,
) -> dict[str, Any] | None:
    try:
        from strategies.macro_stress_observe import observe_macro_stress

        rec = observe_macro_stress(
            config if isinstance(config, dict) else None,
            has_position=has_position,
            action=action,
        )
    except Exception:
        return None
    return _copy_macro_stress(rec)


def emit_buy_decision_tape(
    order: TradeOrder,
    decision: RiskDecision,
    *,
    source: str | None = None,
    timeframe: str = "4h",
    config: dict | None = None,
) -> None:
    """Append one observe-only row. Never mutates *decision* or *order*."""
    if str(getattr(order, "type", "") or "").upper() != "BUY":
        return
    if not tape_enabled(config):
        return

    from core.tenant_context import resolve_tenant_id

    tenant = resolve_tenant_id()
    pos = _lookup_position(getattr(order, "symbol", "") or "", timeframe)
    if not isinstance(pos, dict):
        raise TypeError("get_position did not return a dict")
    amount = float(pos.get("amount") or 0)
    has_position = amount > _AMOUNT_EPS
    signal_name = getattr(order, "signal", "") or ""
    signal_source = source if source is not None else (getattr(order, "source", "") or "")
    approved = bool(getattr(decision, "approved", False))
    if approved:
        filter_codes: list[str] = []
    else:
        code = str(getattr(decision, "code", "") or "").strip()
        filter_codes = [code] if code else ["rejected"]
    outcome = "accepted" if approved else "rejected"
    row = {
        "ts": _now_ts(),
        "symbol": getattr(order, "symbol", "") or "",
        "tenant": tenant,
        "outcome": outcome,
        "signal": {"name": signal_name, "source": signal_source},
        "filter_codes": filter_codes,
        "book": {
            "state": "has_position" if has_position else "empty",
            "dca": signal_name == "BUY_DCA",
        },
        "macro_stress": _observe_macro_stress(
            config,
            has_position=has_position,
            action=signal_name or "BUY",
        ),
        "correlation_id": _correlation_id(order),
    }
    if _file_write_allowed():
        from services.observability_store import append_jsonl, maybe_rotate_jsonl

        path = tape_path()
        append_jsonl(path, row)
        maybe_rotate_jsonl(path, max_bytes=ROTATE_MAX_BYTES, keep_lines=ROTATE_KEEP_LINES)
    log(
        f"buy_decision_tape symbol={row['symbol']} outcome={row['outcome']} "
        f"filter_codes={row['filter_codes']}",
        "INFO",
    )
