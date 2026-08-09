"""Position lock — block auto-exits / DCA / eviction while held.

Persists on the position document under key ``lock``. Manual sells still allowed
unless mode ``no_manual_sell`` is set (default off).

Kill: risk.position_locks.enabled=false
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from logger import log

LOCK_KEY = "lock"

# Modes enforced by the platform
MODE_NO_AUTO_SELL = "no_auto_sell"
MODE_NO_DCA = "no_dca"
MODE_NO_EVICT = "no_evict"
MODE_NO_MANUAL_SELL = "no_manual_sell"

ALL_MODES = frozenset(
    {MODE_NO_AUTO_SELL, MODE_NO_DCA, MODE_NO_EVICT, MODE_NO_MANUAL_SELL}
)
DEFAULT_MODES: tuple[str, ...] = (MODE_NO_AUTO_SELL, MODE_NO_DCA, MODE_NO_EVICT)

# Order / signal sources treated as *manual* (bypass no_auto_sell)
_MANUAL_SOURCES = frozenset(
    {
        "manual",
        "telegram",
        "user",
        "operator",
        "confirm",
        "manual_order",
        "manual_sell",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def position_locks_enabled(config: dict | None = None) -> bool:
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        risk = (config or {}).get("risk") if isinstance(config, dict) else {}
        pl = risk.get("position_locks") if isinstance(risk, dict) else None
        if isinstance(pl, dict) and "enabled" in pl:
            return bool(pl.get("enabled"))
        # top-level optional
        if isinstance(config, dict) and isinstance(config.get("position_locks"), dict):
            if "enabled" in config["position_locks"]:
                return bool(config["position_locks"]["enabled"])
    except Exception:
        pass
    return True


def get_lock(pos: dict | None) -> dict[str, Any] | None:
    if not isinstance(pos, dict):
        return None
    raw = pos.get(LOCK_KEY)
    if not isinstance(raw, dict):
        return None
    if not raw.get("enabled", True):
        return None
    return raw


def lock_is_active(lock: dict | None, *, now: datetime | None = None) -> bool:
    if not lock or not lock.get("enabled", True):
        return False
    until = _parse_iso(lock.get("until") if isinstance(lock.get("until"), str) else None)
    if until is None and lock.get("until") in (None, "", 0, False):
        return True  # permanent
    if until is None:
        return True
    n = now or _utc_now()
    return n < until


def is_position_locked(
    pos: dict | None,
    *,
    mode: str | None = None,
    now: datetime | None = None,
    config: dict | None = None,
) -> bool:
    if not position_locks_enabled(config):
        return False
    lock = get_lock(pos)
    if not lock_is_active(lock, now=now):
        return False
    if mode is None:
        return True
    modes = set(lock.get("modes") or DEFAULT_MODES)
    return mode in modes


def is_manual_source(source: str | None) -> bool:
    s = str(source or "").strip().lower()
    if not s:
        return False
    if s in _MANUAL_SOURCES:
        return True
    if s.startswith("manual"):
        return True
    return False


def auto_sell_blocked(
    pos: dict | None,
    source: str | None = None,
    *,
    now: datetime | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """True if auto/exit sell must be denied."""
    if not is_position_locked(pos, mode=MODE_NO_AUTO_SELL, now=now, config=config):
        # optional hard lock of manual too
        if is_position_locked(pos, mode=MODE_NO_MANUAL_SELL, now=now, config=config):
            if is_manual_source(source):
                return True, _reason(pos, MODE_NO_MANUAL_SELL)
        return False, ""
    if is_manual_source(source):
        # manual allowed unless no_manual_sell
        if is_position_locked(pos, mode=MODE_NO_MANUAL_SELL, now=now, config=config):
            return True, _reason(pos, MODE_NO_MANUAL_SELL)
        return False, ""
    return True, _reason(pos, MODE_NO_AUTO_SELL)


def dca_blocked(
    pos: dict | None,
    *,
    now: datetime | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    if is_position_locked(pos, mode=MODE_NO_DCA, now=now, config=config):
        return True, _reason(pos, MODE_NO_DCA)
    return False, ""


def eviction_blocked(
    pos: dict | None,
    *,
    now: datetime | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    if is_position_locked(pos, mode=MODE_NO_EVICT, now=now, config=config):
        return True, _reason(pos, MODE_NO_EVICT)
    return False, ""


def _reason(pos: dict | None, mode: str) -> str:
    lock = get_lock(pos) or {}
    why = str(lock.get("reason") or "locked").strip() or "locked"
    until = lock.get("until") or "permanent"
    return f"position_locked ({mode}): {why} until={until}"


def build_lock(
    *,
    reason: str = "user",
    locked_by: str = "system",
    until: datetime | str | None = None,
    modes: list[str] | tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    n = now or _utc_now()
    mode_list = [m for m in (modes or DEFAULT_MODES) if m in ALL_MODES]
    if not mode_list:
        mode_list = list(DEFAULT_MODES)
    until_s: str | None
    if until is None:
        until_s = None
    elif isinstance(until, datetime):
        until_s = until.astimezone(timezone.utc).isoformat()
    else:
        until_s = str(until) if until else None
    return {
        "enabled": True,
        "modes": mode_list,
        "reason": str(reason or "user")[:120],
        "locked_by": str(locked_by or "system")[:80],
        "locked_at": n.astimezone(timezone.utc).isoformat(),
        "until": until_s,
    }


def apply_lock(pos: dict, lock: dict[str, Any]) -> dict:
    pos = pos if isinstance(pos, dict) else {}
    pos[LOCK_KEY] = dict(lock)
    return pos


def clear_lock(pos: dict) -> dict:
    pos = pos if isinstance(pos, dict) else {}
    if LOCK_KEY in pos:
        pos.pop(LOCK_KEY, None)
    return pos


def parse_duration_to_until(token: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse 2h, 24h, 7d, permanent/forever/lock → until datetime (None = permanent)."""
    n = now or _utc_now()
    if not token:
        return None
    t = str(token).strip().lower()
    if t in ("permanent", "forever", "perm", "inf", "infinite", "lock", "0"):
        return None
    # pure hours as number
    if t.isdigit():
        return n + timedelta(hours=int(t))
    if t.endswith("h") and t[:-1].isdigit():
        return n + timedelta(hours=int(t[:-1]))
    if t.endswith("d") and t[:-1].isdigit():
        return n + timedelta(days=int(t[:-1]))
    if t.endswith("m") and t[:-1].isdigit():
        return n + timedelta(minutes=int(t[:-1]))
    # ISO fallback
    dt = _parse_iso(token)
    return dt


def lock_summary(pos: dict | None) -> str:
    lock = get_lock(pos)
    if not lock or not lock_is_active(lock):
        return ""
    modes = ",".join(lock.get("modes") or [])
    until = lock.get("until") or "∞"
    why = lock.get("reason") or ""
    return f"🔒 {why} [{modes}] until={until}"


def log_lock_block(symbol: str, message: str, *, source: str = "") -> None:
    try:
        log(f"position_lock block {symbol} src={source or '-'}: {message}", "INFO")
    except Exception:
        pass
    try:
        from services.watchlist_quality.soak_log import log_risk_reject

        log_risk_reject(
            symbol=symbol,
            side="SELL",
            source=str(source or ""),
            code="position_locked",
            message=message[:200],
            config=None,
        )
    except Exception:
        pass
