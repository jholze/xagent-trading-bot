"""Look up predecessor contracts without copying them.

Missing modules stay missing. A test may register a pure stub. Nothing here
opens a socket, reads a secret, or admits a live order.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Iterator

from tools.short_release_acceptance.constants import OWNER_IMPORTS

_OVERRIDES: dict[str, Any] = {}


def resolve_owner(contract_id: str) -> Any | None:
    if contract_id in _OVERRIDES:
        return _OVERRIDES[contract_id]
    spec = OWNER_IMPORTS.get(contract_id)
    if spec is None:
        return None
    module_name, attr = spec
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(module, attr, None)


def owner_status() -> dict[str, str]:
    status: dict[str, str] = {}
    for contract_id in OWNER_IMPORTS:
        status[contract_id] = "available" if resolve_owner(contract_id) is not None else "missing"
    return status


@contextmanager
def owner_overrides(mapping: dict[str, Any]) -> Iterator[None]:
    previous = dict(_OVERRIDES)
    _OVERRIDES.update(mapping)
    try:
        yield
    finally:
        _OVERRIDES.clear()
        _OVERRIDES.update(previous)
