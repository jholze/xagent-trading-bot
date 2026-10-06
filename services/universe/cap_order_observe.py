"""#633 Slice B — log the existing cap order when ``universe_trade_cap`` would reject.

Observe only. The rows are the trade list ``load_trade_universe`` already
returned, in that order. Rank is the 1-based position in that list. This
module does not score, does not pick a rank key, and does not build a new
candidate set. It does not change accept, reject, or size.

The caller passes ``context_tenant_id()`` (the contextvar value, or None).
No context stays ``tenant_id=None``: the row and the INFO line store JSON
null, not the default tenant name, and nothing is written under that name.
The cap list is the order for the id that was passed.

Flag: ``universe.cap_order_observe.observe_enabled`` (default false), same
shape as other observe/shadow flags. ``fire_enabled`` is always false and
is never applied, even if config sets it.

Fail-open: a read or write error is logged and swallowed. It is not a gate
and it does not change the risk decision.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from logger import log
from services.universe.membership_revise import ERSATZ_CAP_NAME, parse_membership_revise

REJECT_CODE = "universe_trade_cap"
CONFIG_KEY = "cap_order_observe"
IST_CAP_NAME = "trade_max_coins"
LOG_FILENAME = "cap_order_observe.jsonl"
ROTATE_MAX_BYTES = 8_000_000
ROTATE_KEEP_LINES = 20_000
_UNDER_TEST_ENV = "CAP_ORDER_OBSERVE_UNDER_TEST"

# Jsonl whitelist. No order, size, or secret fields.
ROW_KEYS = (
    "ts",
    "tenant",
    "symbol",
    "rank",
    "cap_name",
    "reject_code",
    "rejected_symbol",
)


def _section(config: dict | None) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    universe = config.get("universe")
    if not isinstance(universe, dict):
        return None
    sec = universe.get(CONFIG_KEY)
    if not isinstance(sec, dict):
        return None
    return sec


def cap_order_observe_enabled(config: dict | None) -> bool:
    """True only when the observe flag is set. Missing section stays off."""
    try:
        sec = _section(config)
        if sec is None:
            return False
        return bool(sec.get("observe_enabled", False))
    except Exception:
        return False


def cap_order_fire_enabled(_config: dict | None = None) -> bool:
    """Money flag. Always false. Config cannot turn it on."""
    return False


def existing_cap_name(config: dict | None) -> str:
    """Name of the cap that produced the live trade order.

    Enforce applies the named Ersatz-Cap. Shadow and off keep the Ist
    ``trade_max_coins`` order; the Ersatz-Cap is recorded there and not applied.
    A parse error keeps the Ist name and does not widen.
    """
    try:
        revise = parse_membership_revise(config)
    except Exception:
        return IST_CAP_NAME
    if revise.mode == "enforce":
        return ERSATZ_CAP_NAME
    return IST_CAP_NAME


def _symbol(coin: dict | None) -> str:
    if not isinstance(coin, dict):
        return ""
    return str(coin.get("symbol") or "").strip()


def existing_cap_order(
    config: dict | None,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read the live trade list. Do not re-rank and do not resize the cap.

    Rank is the position ``load_trade_universe`` already chose. Symbols stay
    in that order. This does not call the selector with a new key or a
    non-positive max (that max means open-all on the Ist selector).
    """
    from services.universe.split import load_trade_universe

    trade = load_trade_universe(tenant_id=tenant_id, config=config)
    cap_name = existing_cap_name(config)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for coin in trade or []:
        sym = _symbol(coin)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        rows.append(
            {
                "symbol": sym,
                "rank": len(rows) + 1,
                "cap_name": cap_name,
                "reject_code": REJECT_CODE,
            }
        )
    return rows


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logged_tenant(tenant_id: str | None) -> str | None:
    """Tenant stored on the row.

    ``None`` stays ``None`` (JSON null). Do not call ``resolve_tenant_id``
    here: with no context that helper returns the default tenant name.
    """
    if not isinstance(tenant_id, str):
        return None
    if not tenant_id.strip():
        return None
    return tenant_id


def _tenant_log_token(tenant: str | None) -> str:
    if tenant is None:
        return "null"
    return str(tenant)


def _stamp(
    row: dict[str, Any],
    *,
    rejected_symbol: str,
    ts: str,
    tenant: str | None,
) -> dict[str, Any]:
    """Copy the would-rank fields onto a whitelist row."""
    return {
        "ts": ts,
        "tenant": tenant,
        "symbol": row.get("symbol"),
        "rank": row.get("rank"),
        "cap_name": row.get("cap_name"),
        "reject_code": row.get("reject_code"),
        "rejected_symbol": str(rejected_symbol or ""),
    }


def format_cap_order_observe_log(
    rows: list[dict[str, Any]],
    *,
    rejected_symbol: str,
    tenant: str | None = None,
) -> str:
    body = " ".join(
        (
            f"symbol={row.get('symbol')} "
            f"rank={row.get('rank')} "
            f"cap_name={row.get('cap_name')} "
            f"reject_code={row.get('reject_code')}"
        )
        for row in rows
    )
    head = (
        f"[cap_order_observe] tenant={_tenant_log_token(tenant)} "
        f"rejected_symbol={rejected_symbol}"
    )
    tail = f"fire_enabled={cap_order_fire_enabled()}"
    if body:
        return f"{head} {body} {tail}"
    return f"{head} {tail}"


def observe_log_path() -> str:
    from logger import LOG_DIR

    return os.path.join(LOG_DIR, LOG_FILENAME)


def _file_write_allowed() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(_UNDER_TEST_ENV) != "1":
        return False
    return True


def _warn(detail: object) -> None:
    try:
        log(f"[cap_order_observe] fail-open: {detail}", "WARNING")
    except Exception:
        pass


def _write_rows(rows: list[dict[str, Any]]) -> None:
    if not rows or not _file_write_allowed():
        return
    from services.observability_store import append_jsonl, maybe_rotate_jsonl

    path = observe_log_path()
    for row in rows:
        append_jsonl(path, {key: row.get(key) for key in ROW_KEYS})
    maybe_rotate_jsonl(path, max_bytes=ROTATE_MAX_BYTES, keep_lines=ROTATE_KEEP_LINES)


def maybe_log_existing_cap_order(
    config: dict | None,
    *,
    rejected_symbol: str,
    tenant_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """Log the existing cap order. Never raises. Does not approve or resize.

    Returns the whitelist rows when observe is on and the read succeeded,
    including when the write then fails. Returns None when observe is off
    or the read fails.
    """
    try:
        if not cap_order_observe_enabled(config):
            return None
        tenant = _logged_tenant(tenant_id)
        base = existing_cap_order(config, tenant_id=tenant)
        ts = _now_ts()
        rows = [
            _stamp(row, rejected_symbol=rejected_symbol, ts=ts, tenant=tenant)
            for row in base
        ]
    except Exception as exc:
        _warn(exc)
        return None
    try:
        log(
            format_cap_order_observe_log(
                rows,
                rejected_symbol=rejected_symbol,
                tenant=tenant,
            ),
            "INFO",
        )
    except Exception as exc:
        _warn(exc)
    try:
        _write_rows(rows)
    except Exception as exc:
        _warn(exc)
    return rows
