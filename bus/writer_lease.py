"""Single-writer lease with a monotonic fencing token (#306 slice 2).

Redis layout
------------
Lease key  ``{prefix}lease:writer:{tenant}:{scope}``
Fence key  ``{prefix}lease:fence:{tenant}:{scope}``

``prefix`` comes from ``bus.redis_keys.redis_key_prefix()`` (production
``aria:``, under pytest ``pytest:<suffix>[_gwN]:``).

The lease value is one JSON object ``{"token", "fence", "host", "pid"}``
so a single GET is atomic for identity + fencing token. The INCR key is
the monotonic source of truth and has **no TTL**: it survives holder
expiry so the next writer cannot reuse a lower fence. Burning a fence
number on a lost SET NX race is accepted.

State machine
-------------
``acquiring`` — startup, no successful SET NX yet. No orders, no ledger
writes. ``/health`` 503, ``writer_lease=readonly``.
``held`` — renewer confirmed our token within the last TTL. Orders and
writes allowed; documents stamped with ``fence``.
``readonly`` — another holder (or Redis down at startup). Same denies as
acquiring; operator notified once. Retry every ``lease_renew_sec``.
``lost`` — we held it and renew failed (Redis down or token replaced).
``held()`` is false immediately. Orders denied with ``no_writer_lease``.
The loop keeps retrying; a successful re-acquire sends one recovery
message.

``architecture.single_writer_lease_enabled`` (default true) is the
kill-switch. The unit-suite normalizer forces it false, matching
``ledger_lock_fail_closed``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from typing import Any, Callable

from bus.redis_client import get_redis, resolve_redis_url
from bus.redis_keys import redis_key_prefix
from logger import log
from storage.errors import LedgerWriteFailed, WriterLeaseLost

DEFAULT_TTL_SEC = 30
DEFAULT_RENEW_SEC = 10

_UNSET = object()
_hold_lock = threading.RLock()
_process_lease = None
_injected_redis = _UNSET
_active_leases: list = []
_readonly_notified: set[tuple[str, str]] = set()
_recovered_notified: set[tuple[str, str]] = set()
_on_acquired: Callable[[], None] | None = None
_callback_threads: list[threading.Thread] = []


def lease_redis_key(tenant_id: str, scope: str) -> str:
    return f"{redis_key_prefix()}lease:writer:{tenant_id}:{scope}"


def fence_redis_key(tenant_id: str, scope: str) -> str:
    return f"{redis_key_prefix()}lease:fence:{tenant_id}:{scope}"


def mongo_fence_filter(doc_id: Any, fence: int) -> dict:
    """replace_one filter: never overwrite a newer writer's document."""
    return {
        "_id": doc_id,
        "$or": [
            {"fence": {"$exists": False}},
            {"fence": {"$lte": int(fence)}},
        ],
    }


def lease_enabled() -> bool:
    try:
        from core.config import get_bot_config

        return bool(
            get_bot_config().architecture_config.get(
                "single_writer_lease_enabled", True
            )
        )
    except Exception:
        return True


def _arch_ints() -> tuple[int, int]:
    try:
        from core.config import get_bot_config

        arch = get_bot_config().architecture_config
        ttl = int(arch.get("lease_ttl_sec", DEFAULT_TTL_SEC) or DEFAULT_TTL_SEC)
        renew = int(arch.get("lease_renew_sec", DEFAULT_RENEW_SEC) or DEFAULT_RENEW_SEC)
    except Exception:
        ttl, renew = DEFAULT_TTL_SEC, DEFAULT_RENEW_SEC
    return max(1, ttl), max(1, renew)


def _redis_client(explicit=None):
    if explicit is not None:
        return explicit
    if _injected_redis is not _UNSET:
        return _injected_redis
    try:
        from core.config import get_bot_config

        arch = get_bot_config().architecture_config
        url = resolve_redis_url(arch.get("redis_url"))
    except Exception:
        url = resolve_redis_url(None)
    return get_redis(url, key_prefix=redis_key_prefix())


def _parse_lease_value(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("token"):
            return data
    except Exception:
        pass
    return {"token": text, "fence": None, "host": "?"}


def _host_name() -> str:
    try:
        return socket.gethostname() or "?"
    except Exception:
        return "?"


class WriterLease:
    """Redis SET NX lease for one (tenant, scope). See module docstring."""

    def __init__(
        self,
        tenant_id: str,
        scope: str,
        *,
        redis=None,
        ttl_sec: int = DEFAULT_TTL_SEC,
        renew_sec: int = DEFAULT_RENEW_SEC,
        token: str | None = None,
        host: str | None = None,
        clock=None,
        pid: int | None = None,
    ):
        self.tenant_id = tenant_id
        self.scope = scope
        self.ttl_sec = max(1, int(ttl_sec))
        self.renew_sec = max(1, int(renew_sec))
        self.token = token or uuid.uuid4().hex
        self.host = host if host is not None else _host_name()
        self.pid = int(os.getpid() if pid is None else pid)
        self.clock = clock or __import__("time").monotonic
        self.fence: int | None = None
        self._redis = redis
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._acquired = False
        self._lost = False
        self._ever_held = False
        self._last_confirmed_at = 0.0
        self._holder_info: dict | None = None
        self._lock = threading.Lock()
        with _hold_lock:
            _active_leases.append(self)

    @property
    def lease_key(self) -> str:
        return lease_redis_key(self.tenant_id, self.scope)

    @property
    def fence_key(self) -> str:
        return fence_redis_key(self.tenant_id, self.scope)

    def state(self) -> str:
        if self.held():
            return "held"
        if self._ever_held:
            return "lost"
        if self._holder_info is not None or self._acquired is False and self._lost:
            return "readonly"
        return "acquiring"

    def holder_label(self) -> str:
        info = self._holder_info or {}
        token = str(info.get("token") or "unknown")
        host = str(info.get("host") or "unknown")
        return f"{token}/{host}"

    def held(self) -> bool:
        """True only while the renewer confirmed our token within the last TTL."""
        if self._lost or not self._acquired:
            return False
        if self.clock() - self._last_confirmed_at >= self.ttl_sec:
            return False
        return True

    def _client(self):
        return _redis_client(self._redis)

    def _encode(self, fence: int) -> str:
        return json.dumps(
            {
                "token": self.token,
                "fence": int(fence),
                "host": self.host,
                "pid": self.pid,
            },
            separators=(",", ":"),
        )

    def _mark_held(self, fence: int | None = None) -> None:
        if fence is not None:
            self.fence = int(fence)
        self._acquired = True
        self._lost = False
        self._ever_held = True
        self._last_confirmed_at = self.clock()
        self._holder_info = None

    def _mark_lost(self) -> None:
        self._lost = True

    def acquire(self, *, start_renewer: bool = True) -> bool:
        """SET NX the lease key. Returns True iff this token is now the holder."""
        client = self._client()
        if client is None:
            self._holder_info = {"token": "unavailable", "host": "redis"}
            self._mark_lost()
            if start_renewer:
                self._start_renewer()
            return False
        try:
            current = _parse_lease_value(client.get(self.lease_key))
            if current and current.get("token") == self.token:
                client.expire(self.lease_key, self.ttl_sec)
                fence = current.get("fence")
                self._mark_held(int(fence) if fence is not None else self.fence)
                if start_renewer:
                    self._start_renewer()
                return True
            fence = int(client.incr(self.fence_key))
            payload = self._encode(fence)
            ok = client.set(self.lease_key, payload, nx=True, ex=self.ttl_sec)
            if ok:
                self._mark_held(fence)
                if start_renewer:
                    self._start_renewer()
                return True
            self._holder_info = _parse_lease_value(client.get(self.lease_key)) or current
            if start_renewer:
                self._start_renewer()
            return False
        except Exception as e:
            log(f"writer lease acquire error ({self.tenant_id}/{self.scope}): {e}", "WARNING")
            self._holder_info = {"token": "unavailable", "host": "redis"}
            self._mark_lost()
            if start_renewer:
                self._start_renewer()
            return False

    def renew(self) -> bool:
        """EXPIRE the key only if the stored token is ours."""
        client = self._client()
        if client is None:
            self._mark_lost()
            return False
        try:
            current = _parse_lease_value(client.get(self.lease_key))
            if not current or current.get("token") != self.token:
                self._mark_lost()
                return False
            if not client.expire(self.lease_key, self.ttl_sec):
                self._mark_lost()
                return False
            fence = current.get("fence")
            if fence is not None:
                self.fence = int(fence)
            self._last_confirmed_at = self.clock()
            self._lost = False
            self._acquired = True
            return True
        except Exception as e:
            log(f"writer lease renew error ({self.tenant_id}/{self.scope}): {e}", "WARNING")
            self._mark_lost()
            return False

    def release(self) -> bool:
        """DELETE the key only if the stored token is ours."""
        client = self._client()
        deleted = False
        try:
            if client is not None:
                current = _parse_lease_value(client.get(self.lease_key))
                if current and current.get("token") == self.token:
                    client.delete(self.lease_key)
                    deleted = True
        except Exception as e:
            log(f"writer lease release error ({self.tenant_id}/{self.scope}): {e}", "WARNING")
        self._acquired = False
        self._lost = True
        return deleted

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._thread = None

    def _start_renewer(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._renewer_loop,
            name=f"writer-lease-renewer-{self.tenant_id}-{self.scope}",
            daemon=True,
        )
        self._thread.start()

    def _renewer_loop(self) -> None:
        while True:
            # Held: renew every renew_sec. Not held: retry quickly so a new
            # container takes over within ~2 s once the old one releases.
            interval = float(self.renew_sec) if self.held() else min(2.0, float(self.renew_sec))
            if self._stop.wait(interval):
                return
            try:
                if self._acquired and not self._lost:
                    if self.renew():
                        continue
                    self._mark_lost()
                was_held = self._ever_held
                if self.acquire(start_renewer=False):
                    if was_held:
                        _notify_reacquired(self)
                    else:
                        _notify_first_hold(self)
                    _fire_on_acquired_async()
            except Exception as e:
                log(
                    f"writer lease renewer error ({self.tenant_id}/{self.scope}): {e}",
                    "WARNING",
                )
                self._mark_lost()


def writer_lease_held() -> bool:
    lease = _process_lease
    return bool(lease is not None and lease.held())


def writer_lease_status() -> str:
    """disabled | held | lost | standby.

    ``standby``: never held in this process (another holder during a Railway
    deploy overlap, or Redis unavailable at start). ``lost``: held before,
    renew failed. /health treats standby as 200 so a new container can pass
    the deploy healthcheck while the old one still holds the lease; lost is 503.
    """
    if not lease_enabled():
        return "disabled"
    if writer_lease_held():
        return "held"
    lease = _process_lease
    if lease is not None and lease._ever_held:
        return "lost"
    return "standby"


def writer_lease_fence() -> int | None:
    lease = _process_lease
    if lease is None or not lease.held() or lease.fence is None:
        return None
    return int(lease.fence)


def process_lease() -> WriterLease | None:
    return _process_lease


def set_lease_redis_for_tests(client) -> None:
    global _injected_redis
    _injected_redis = client


def set_process_lease_for_tests(lease: WriterLease | None) -> None:
    global _process_lease
    _process_lease = lease


def require_lease_for_order() -> None:
    """Raise WriterLeaseLost when the feature is on and we do not hold the lease."""
    if not lease_enabled():
        return
    lease = _process_lease
    if lease is not None and lease.held():
        # Review (#306): holding the lease is not enough right after a
        # takeover -- the exchange reconcile registered as on_acquired must
        # have completed first ("kein Zyklus ohne Abgleich", #314).
        if _on_acquired is not None and not _recovery_completed(lease):
            raise WriterLeaseLost(
                reason="recovery_pending",
                tenant_id=lease.tenant_id,
                scope=lease.scope,
            )
        return
    tid = lease.tenant_id if lease is not None else None
    scope = lease.scope if lease is not None else None
    reason = "not_held"
    if lease is not None and lease._ever_held:
        reason = "lost"
    elif lease is not None and (lease._holder_info or {}).get("token") == "unavailable":
        reason = "redis_unavailable"
    raise WriterLeaseLost(
        reason=reason,
        tenant_id=tid,
        scope=scope,
    )


def write_fence() -> int | None:
    """Stamp value for a ledger write, or None when the feature is off.

    Raises LedgerWriteFailed when the feature is on but the lease is not held.
    """
    if not lease_enabled():
        return None
    lease = _process_lease
    if lease is None or not lease.held() or lease.fence is None:
        tid = lease.tenant_id if lease is not None else None
        scope = lease.scope if lease is not None else None
        raise LedgerWriteFailed(
            "no writer lease",
            op="writer_lease",
            tenant_id=tid,
            scope=scope,
        )
    return int(lease.fence)


def refuse_stale_json_fence(path: str, our_fence: int) -> None:
    """Read ``path`` and raise LedgerWriteFailed if its fence is greater than ours."""
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception as e:
        raise LedgerWriteFailed(
            "stale fence",
            op="json_fence",
            cause=e,
        ) from e
    if not isinstance(existing, dict) or "fence" not in existing:
        return
    try:
        old = int(existing["fence"])
    except (TypeError, ValueError):
        return
    if old > int(our_fence):
        raise LedgerWriteFailed("stale fence", op="json_fence")


def prepare_json_ledger_write(data: dict, path: str) -> dict:
    """Copy ``data``, require a held lease, refuse a newer on-disk fence, stamp ours."""
    payload = dict(data)
    fence = write_fence()
    if fence is None:
        return payload
    refuse_stale_json_fence(path, fence)
    payload["fence"] = fence
    return payload


def _episode_key(lease: WriterLease) -> tuple[str, str]:
    return (lease.tenant_id, lease.scope)


def _notify_readonly(lease: WriterLease) -> None:
    key = _episode_key(lease)
    if key in _readonly_notified:
        return
    _readonly_notified.add(key)
    label = lease.holder_label()
    msg = (
        f"Kein Schreibrecht: Lease wird von {label} gehalten — Lesemodus"
    )
    log(msg, "ERROR")
    try:
        from core.operator_notify import notify_operator

        notify_operator(msg)
    except Exception:
        pass


def _notify_first_hold(lease: WriterLease) -> None:
    key = _episode_key(lease)
    if key not in _readonly_notified:
        return
    _notify_reacquired(lease)


def _notify_reacquired(lease: WriterLease) -> None:
    key = _episode_key(lease)
    _readonly_notified.discard(key)
    if key in _recovered_notified:
        return
    _recovered_notified.add(key)
    try:
        from services import trading_service as ts

        ts._ledger_unavailable_notified.discard(key)
    except Exception:
        pass
    try:
        from core.operator_notify import notify_operator

        notify_operator(
            f"✅ Schreibrecht wiederhergestellt: Lease gehalten "
            f"(fence={lease.fence})"
        )
    except Exception:
        pass
    _recovered_notified.discard(key)


def _recovery_completed(lease: "WriterLease") -> bool:
    try:
        from services.architecture_runtime import tenant_recovery_completed

        return bool(tenant_recovery_completed(lease.tenant_id, lease.scope))
    except Exception:
        return False


def _fire_on_acquired() -> None:
    cb = _on_acquired
    if cb is None:
        return
    try:
        cb()
    except Exception as e:
        log(f"writer lease on_acquired callback failed: {e}", "WARNING")


def _fire_on_acquired_async() -> None:
    """Run the on_acquired callback next to the renewer, not inside it.

    The callback is the exchange reconcile, which can take longer than the
    lease TTL; running it inline would starve renewals and lose the lease
    while reconciling. Orders stay denied until it completes
    (require_lease_for_order -> recovery_pending).
    """
    if _on_acquired is None:
        return
    thread = threading.Thread(
        target=_fire_on_acquired, name="writer-lease-on-acquired", daemon=True
    )
    with _hold_lock:
        _callback_threads[:] = [t for t in _callback_threads if t.is_alive()]
        _callback_threads.append(thread)
    thread.start()


def _join_callback_threads(timeout: float = 5.0) -> None:
    with _hold_lock:
        threads = list(_callback_threads)
        _callback_threads.clear()
    for thread in threads:
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)


def _make_process_lease(redis=None) -> WriterLease:
    from core.tenant_context import resolve_tenant_id, resolve_tenant_scope

    ttl, renew = _arch_ints()
    return WriterLease(
        resolve_tenant_id(),
        resolve_tenant_scope(),
        redis=redis,
        ttl_sec=ttl,
        renew_sec=renew,
    )


def ensure_writer_lease(*, on_acquired: Callable[[], None] | None = None) -> bool:
    """Acquire (or keep retrying) the process lease for the current tenant/scope.

    Returns True when writes are allowed (feature off, or lease held).
    """
    global _process_lease, _on_acquired
    if on_acquired is not None:
        _on_acquired = on_acquired
    if not lease_enabled():
        return True
    with _hold_lock:
        if _process_lease is None:
            _process_lease = _make_process_lease()
        lease = _process_lease
    if lease.held():
        return True
    ok = lease.acquire(start_renewer=True)
    if ok:
        if (lease.tenant_id, lease.scope) in _readonly_notified:
            _notify_reacquired(lease)
            _fire_on_acquired()
        return True
    _notify_readonly(lease)
    return False


def shutdown_writer_lease() -> None:
    """Release if we still hold the key, then join the renewer."""
    lease = _process_lease
    if lease is None:
        return
    try:
        lease.release()
    finally:
        lease.stop()
        _join_callback_threads()


def reset_writer_lease_for_tests() -> None:
    """Stop every renewer thread and drop process state (#329)."""
    global _process_lease, _injected_redis, _on_acquired
    with _hold_lock:
        leases = list(_active_leases)
        _active_leases.clear()
        _process_lease = None
        _injected_redis = _UNSET
        _on_acquired = None
        _readonly_notified.clear()
        _recovered_notified.clear()
    for lease in leases:
        try:
            lease.release()
        except Exception:
            pass
        try:
            lease.stop()
        except Exception:
            pass
    _join_callback_threads()
