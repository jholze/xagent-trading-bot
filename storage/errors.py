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
