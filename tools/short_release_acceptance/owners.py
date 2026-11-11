"""Static ownership labels for predecessor contracts.

Phase 1 does not import those modules. The strings are data. Nothing here
loads execution, services, storage, or a network client.
"""

from __future__ import annotations

from tools.short_release_acceptance.constants import OWNER_MODULES


def owner_status() -> dict[str, str]:
    """Every named contract stays missing until a later integration wires it."""
    return {contract_id: "missing" for contract_id in OWNER_MODULES}
