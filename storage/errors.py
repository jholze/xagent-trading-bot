"""Ledger I/O errors: failed reads/writes must not look like empty state (#318)."""

from __future__ import annotations


class LedgerUnavailable(RuntimeError):
    """Ledger read/write failed. Carries scope, tenant_id, op ('load_orders', …), cause."""

    def __init__(
        self,
        message: str = "",
        *,
        scope: str | None = None,
        tenant_id: str | None = None,
        op: str = "",
        cause: BaseException | None = None,
    ):
        self.scope = scope
        self.tenant_id = tenant_id
        self.op = op
        self.cause = cause
        bits: list[str] = []
        if op:
            bits.append(op)
        if tenant_id:
            bits.append(f"tenant={tenant_id}")
        if scope:
            bits.append(f"scope={scope}")
        prefix = " ".join(bits) if bits else "ledger"
        detail = message or (
            f"{type(cause).__name__}: {cause}" if cause is not None else "unavailable"
        )
        super().__init__(f"{prefix}: {detail}")


class LedgerWriteFailed(LedgerUnavailable):
    """Ledger write failed. Return type of save_* stays bool (always True) or this."""


class LedgerLockUnavailable(LedgerUnavailable):
    """Ledger lock could not be acquired; fail closed (#306).

    ``reason`` is one of ``timeout``, ``redis_error``, ``redis_unavailable``.
    Subclassing ``LedgerUnavailable`` is deliberate: ``TradingService.execute_order``
    already denies every order type and notifies the operator once per episode.
    """

    def __init__(
        self,
        message: str = "",
        *,
        scope: str | None = None,
        tenant_id: str | None = None,
        reason: str = "timeout",
        waited_sec: float = 0.0,
        cause: BaseException | None = None,
    ):
        self.reason = reason
        self.waited_sec = float(waited_sec)
        detail = message or f"lock {reason} after {self.waited_sec:.1f}s"
        super().__init__(
            detail,
            scope=scope,
            tenant_id=tenant_id,
            op="ledger_lock",
            cause=cause,
        )


class WriterLeaseLost(LedgerUnavailable):
    """Process does not hold the single-writer lease; fail closed (#306).

    ``reason`` is one of ``not_held``, ``lost``, ``redis_unavailable``.
    Subclassing ``LedgerUnavailable`` is deliberate: ``TradingService.execute_order``
    already denies every order type and notifies the operator once per episode.
    """

    def __init__(
        self,
        message: str = "",
        *,
        scope: str | None = None,
        tenant_id: str | None = None,
        reason: str = "lost",
        cause: BaseException | None = None,
    ):
        self.reason = reason
        detail = message or f"writer lease {reason}"
        super().__init__(
            detail,
            scope=scope,
            tenant_id=tenant_id,
            op="writer_lease",
            cause=cause,
        )
