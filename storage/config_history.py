"""Versioned config snapshots (#330 slice 2).

On every successful ``save_config`` the *previous* document is written under
``data/config_history/`` (via ``resolve_data_path``) before the new body is
persisted. Snapshot failure fails closed: the new config must not be written.

Writes are serialized with a process-local lock (the in-process half of the
ledger-lock pattern: one writer per process).
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from logger import log

MAX_SNAPSHOTS = 20
_TENANT_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_WRITE_LOCK = threading.Lock()


class ConfigSnapshotError(RuntimeError):
    """Raised when a snapshot of the previous config cannot be taken."""


@dataclass(frozen=True)
class ConfigSnapshot:
    n: int
    snapshot_id: str
    created_at: str
    path: str
    tenant_id: str
    body: dict


@contextmanager
def config_write_lock() -> Iterator[None]:
    """One config writer per process (snapshot + persist)."""
    with _WRITE_LOCK:
        yield


def _safe_tenant_id(tenant_id: str) -> str:
    raw = str(tenant_id or "default").strip() or "default"
    safe = _TENANT_SAFE.sub("_", raw)
    return safe or "default"


def history_dir(tenant_id: str) -> str:
    """Tenant snapshot directory under ``resolve_data_path('config_history')``."""
    from data_manager import resolve_data_path

    root = resolve_data_path("config_history")
    return os.path.join(root, _safe_tenant_id(tenant_id))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def record_snapshot(
    previous: dict | None,
    *,
    tenant_id: str,
    keep: int = MAX_SNAPSHOTS,
) -> ConfigSnapshot | None:
    """Persist ``previous`` as the newest snapshot. No-op when there is none.

    Raises :class:`ConfigSnapshotError` if a previous document exists but cannot
    be written. Rotation of extras older than ``keep`` is best-effort: a
    failed delete does not undo a successful snapshot write.
    """
    if previous is None:
        return None
    if not isinstance(previous, dict):
        raise ConfigSnapshotError(
            f"previous config for tenant {tenant_id} is not an object: "
            f"{type(previous).__name__}"
        )
    try:
        body = json.loads(json.dumps(previous))
    except (TypeError, ValueError) as e:
        raise ConfigSnapshotError(
            f"previous config for tenant {tenant_id} is not JSON-serializable: {e}"
        ) from e

    stamp = _now_utc()
    snapshot_id = stamp.strftime("%Y%m%dT%H%M%S%fZ")
    created_at = stamp.isoformat()
    dest_dir = history_dir(tenant_id)
    path = os.path.join(dest_dir, f"{snapshot_id}.json")
    if os.path.exists(path):
        snapshot_id = f"{snapshot_id}_{os.getpid()}"
        path = os.path.join(dest_dir, f"{snapshot_id}.json")
    envelope = {
        "id": snapshot_id,
        "created_at": created_at,
        "tenant_id": str(tenant_id),
        "body": body,
    }
    try:
        from data_manager import atomic_write_json

        atomic_write_json(path, envelope)
    except ConfigSnapshotError:
        raise
    except Exception as e:
        log(
            f"config snapshot write failed tenant={tenant_id} path={path}: {e}",
            "ERROR",
        )
        raise ConfigSnapshotError(
            f"cannot write config snapshot for tenant {tenant_id}: {e}"
        ) from e

    try:
        _rotate(dest_dir, keep=keep)
    except Exception as e:
        log(f"config snapshot rotation failed tenant={tenant_id}: {e}", "WARNING")

    log(f"config snapshot {snapshot_id} stored for tenant {tenant_id}", "INFO")
    return ConfigSnapshot(
        n=1,
        snapshot_id=snapshot_id,
        created_at=created_at,
        path=path,
        tenant_id=str(tenant_id),
        body=body,
    )


def _rotate(dest_dir: str, keep: int) -> None:
    keep = max(0, int(keep))
    files = _snapshot_files(dest_dir)
    extra = files[keep:]
    for path in extra:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue


def _snapshot_files(dest_dir: str) -> list[str]:
    if not os.path.isdir(dest_dir):
        return []
    names = [
        os.path.join(dest_dir, name)
        for name in os.listdir(dest_dir)
        if name.endswith(".json") and not name.endswith(".tmp")
    ]
    names.sort(reverse=True)
    return names


def _load_envelope(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigSnapshotError(f"snapshot {path} is not an object")
    if "body" in data and isinstance(data.get("body"), dict):
        return data
    # raw config document (defensive)
    return {
        "id": os.path.splitext(os.path.basename(path))[0],
        "created_at": "",
        "tenant_id": "",
        "body": data,
    }


def list_snapshots(tenant_id: str, *, limit: int | None = None) -> list[ConfigSnapshot]:
    """Newest-first snapshots. ``n=1`` is the previous document."""
    files = _snapshot_files(history_dir(tenant_id))
    if limit is not None:
        files = files[: max(0, int(limit))]
    out: list[ConfigSnapshot] = []
    for idx, path in enumerate(files, start=1):
        try:
            env = _load_envelope(path)
        except Exception as e:
            log(f"config snapshot skip unreadable {path}: {e}", "WARNING")
            continue
        out.append(
            ConfigSnapshot(
                n=idx,
                snapshot_id=str(env.get("id") or os.path.splitext(os.path.basename(path))[0]),
                created_at=str(env.get("created_at") or ""),
                path=path,
                tenant_id=str(env.get("tenant_id") or tenant_id),
                body=dict(env.get("body") or {}),
            )
        )
    return out


def get_snapshot(tenant_id: str, n: int) -> ConfigSnapshot | None:
    """Return snapshot ``n`` (1 = newest / previous). ``None`` if missing."""
    try:
        idx = int(n)
    except (TypeError, ValueError):
        return None
    if idx < 1:
        return None
    snaps = list_snapshots(tenant_id)
    if idx > len(snaps):
        return None
    return snaps[idx - 1]


def get_snapshot_by_id(tenant_id: str, snapshot_id: str) -> ConfigSnapshot | None:
    """Return snapshot by stable id. ``None`` if rotated or unreadable."""
    wanted = str(snapshot_id or "").strip()
    if not wanted:
        return None
    for snap in list_snapshots(tenant_id):
        if snap.snapshot_id == wanted:
            return snap
    return None


def unified_diff(current: dict, snapshot: dict, *, from_label: str, to_label: str) -> str:
    """Unified diff of two config documents (pretty JSON, sorted keys)."""
    import difflib

    left = json.dumps(current if isinstance(current, dict) else {}, indent=2, sort_keys=True, ensure_ascii=False)
    right = json.dumps(snapshot if isinstance(snapshot, dict) else {}, indent=2, sort_keys=True, ensure_ascii=False)
    lines = list(
        difflib.unified_diff(
            left.splitlines(),
            right.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
        )
    )
    return "\n".join(lines)
