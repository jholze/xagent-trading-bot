"""Telegram /config — snapshot help, diff, and confirmed revert (#330 slice 2).

Operator-only (router ``OPERATOR_ONLY``). ``/config revert <n>`` only *requests*
a confirmation; the restore runs after the same button pattern as ``/panic``.
Revert goes through ``data_manager.save_config`` so slice-1 bounds still apply.
"""

from __future__ import annotations

import time
import uuid
from html import escape as _esc
from typing import Any, Callable

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id
from logger import log
from notifications.telegram_i18n import t
from telegram_notifier import (
    answer_callback_query,
    send_telegram_buttons,
    send_telegram_message,
)

REVERT_TTL_SEC = 60.0
_TELEGRAM_MAX = 3900

_clock: Callable[[], float] = time.monotonic
_pending_revert: dict[str, dict[str, Any]] = {}


def reset_config_revert_for_tests(*, clock: Callable[[], float] | None = None) -> None:
    """Drop pending revert tokens; optionally pin the clock."""
    global _clock
    _pending_revert.clear()
    _clock = clock or time.monotonic


def _now() -> float:
    return float(_clock())


def _tenant() -> str:
    return resolve_tenant_id(None) or DEFAULT_TENANT


def _create_revert_token(
    n: int, tenant_id: str, snapshot_id: str, *, ttl: float = REVERT_TTL_SEC
) -> str:
    token = uuid.uuid4().hex[:12]
    _pending_revert[token] = {
        "expires_at": _now() + float(ttl),
        "n": int(n),
        "tenant_id": tenant_id,
        "snapshot_id": str(snapshot_id),
    }
    return token


def consume_revert_token(token: str) -> dict[str, Any] | None:
    rec = _pending_revert.pop(token, None)
    if rec is None:
        return None
    if _now() >= float(rec.get("expires_at") or 0):
        return None
    return rec


def _newest_line(tenant_id: str) -> str:
    from storage.config_history import list_snapshots

    snaps = list_snapshots(tenant_id)
    if not snaps:
        return t("config_no_snapshots")
    snap = snaps[0]
    return t(
        "config_newest",
        n=snap.n,
        snapshot_id=_esc(snap.snapshot_id),
        created_at=_esc(snap.created_at or "—"),
        count=len(snaps),
    )


def _send_help() -> bool:
    send_telegram_message(
        t("config_help") + "\n\n" + _newest_line(_tenant())
    )
    return True


def _parse_n(raw: str | None, *, default: int | None) -> int | None:
    if raw is None or not str(raw).strip():
        return default
    text = str(raw).strip()
    if not text.isdigit():
        return None
    n = int(text)
    if n < 1:
        return None
    return n


def _current_body() -> dict:
    import data_manager

    cfg = data_manager.get_config()
    return dict(cfg) if isinstance(cfg, dict) else {}


def _handle_diff(n: int) -> bool:
    from storage.config_history import get_snapshot, unified_diff

    tid = _tenant()
    snap = get_snapshot(tid, n)
    if snap is None:
        send_telegram_message(t("config_snapshot_missing", n=n))
        return True
    current = _current_body()
    diff = unified_diff(
        current,
        snap.body,
        from_label="current",
        to_label=f"snapshot {snap.n} ({snap.snapshot_id})",
    )
    if not diff.strip():
        send_telegram_message(
            t(
                "config_diff_empty",
                n=snap.n,
                snapshot_id=_esc(snap.snapshot_id),
            )
        )
        return True
    body = t(
        "config_diff_header",
        n=snap.n,
        snapshot_id=_esc(snap.snapshot_id),
        created_at=_esc(snap.created_at or "—"),
    )
    # Telegram HTML: <pre> + escaped unified diff.
    clipped = diff
    suffix = ""
    room = _TELEGRAM_MAX - len(body) - 32
    if room < 80:
        room = 80
    if len(clipped) > room:
        clipped = clipped[:room]
        suffix = "\n…"
    send_telegram_message(
        f"{body}\n<pre>{_esc(clipped)}{suffix}</pre>"
    )
    return True


def _handle_revert_request(n: int) -> bool:
    from storage.config_history import get_snapshot

    tid = _tenant()
    snap = get_snapshot(tid, n)
    if snap is None:
        send_telegram_message(t("config_snapshot_missing", n=n))
        return True
    token = _create_revert_token(n, tid, snap.snapshot_id)
    keyboard = [
        [
            {"text": t("config_revert_btn_ok"), "callback_data": f"config_ok:{token}"},
            {"text": t("config_revert_btn_no"), "callback_data": f"config_no:{token}"},
        ]
    ]
    send_telegram_buttons(
        t(
            "config_revert_confirm",
            n=snap.n,
            snapshot_id=_esc(snap.snapshot_id),
            created_at=_esc(snap.created_at or "—"),
        ),
        keyboard,
    )
    return True


def _apply_revert(n: int, tenant_id: str, snapshot_id: str) -> None:
    import data_manager
    from core.config_guardrails import ConfigValidationError
    from storage.config_history import ConfigSnapshotError, get_snapshot_by_id

    snap = get_snapshot_by_id(tenant_id, snapshot_id)
    if snap is None:
        send_telegram_message(
            t("config_snapshot_missing", n=_esc(str(snapshot_id or n)))
        )
        return
    try:
        ok = data_manager.save_config(snap.body, tenant_id=tenant_id)
    except ConfigValidationError as e:
        send_telegram_message(t("config_revert_rejected", reason=_esc(str(e))))
        return
    except ConfigSnapshotError as e:
        log(f"/config revert snapshot failed: {e}", "ERROR")
        send_telegram_message(t("config_revert_failed", error=_esc(str(e))))
        return
    except Exception as e:
        log(f"/config revert failed: {e}", "ERROR")
        send_telegram_message(t("config_revert_failed", error=_esc(str(e))))
        return
    if not ok:
        send_telegram_message(t("config_save_failed"))
        return
    send_telegram_message(
        t(
            "config_revert_done",
            n=snap.n,
            snapshot_id=_esc(snap.snapshot_id),
        )
    )


def handle(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower != "/config" and not lower.startswith("/config "):
        return False

    parts = raw.split()
    if len(parts) == 1:
        return _send_help()

    sub = parts[1].lower()
    rest = parts[2] if len(parts) > 2 else ""

    if sub in ("help", "?", "h"):
        return _send_help()

    if sub == "diff":
        n = _parse_n(rest, default=1)
        if n is None:
            send_telegram_message(t("config_usage"))
            return True
        return _handle_diff(n)

    if sub == "revert":
        n = _parse_n(rest, default=None)
        if n is None:
            send_telegram_message(t("config_usage"))
            return True
        return _handle_revert_request(n)

    send_telegram_message(t("config_usage"))
    return True


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not data.startswith("config_"):
        return False
    answer_callback_query(callback_query.get("id"))
    parts = data.split(":", 1)
    if len(parts) != 2:
        send_telegram_message(t("config_revert_expired"))
        return True
    action, token = parts
    if action == "config_no":
        _pending_revert.pop(token, None)
        send_telegram_message(t("config_revert_cancelled"))
        return True
    if action == "config_ok":
        rec = consume_revert_token(token)
        if rec is None:
            send_telegram_message(t("config_revert_expired"))
            return True
        _apply_revert(
            int(rec["n"]),
            str(rec["tenant_id"]),
            str(rec.get("snapshot_id") or ""),
        )
        return True
    return True
