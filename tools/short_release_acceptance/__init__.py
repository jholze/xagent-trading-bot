"""Offline short-release acceptance judge (D-E2E phase 1).

The package classifies fixture evidence and refuses a manual run. It does
not place orders, read secrets, or wire ``services/trading_service.py``.
"""

from tools.short_release_acceptance.chain import (
    INTEGRATION_WIRED,
    assess_clearance,
    compose_offline_chain,
    release_enabled,
)
from tools.short_release_acceptance.constants import CANONICAL_AC_TEXT, FAULT_OWNERS
from tools.short_release_acceptance.faults import judge_fault
from tools.short_release_acceptance.manifest import (
    attempt_acceptance,
    prepare_disabled_manifest,
    validate_manifest,
)
from tools.short_release_acceptance.schema import (
    canonical_ac_hash,
    classify_index,
    classify_row,
    phase1_evidence_index,
)

__all__ = [
    "CANONICAL_AC_TEXT",
    "FAULT_OWNERS",
    "INTEGRATION_WIRED",
    "assess_clearance",
    "attempt_acceptance",
    "canonical_ac_hash",
    "classify_index",
    "classify_row",
    "compose_offline_chain",
    "judge_fault",
    "phase1_evidence_index",
    "prepare_disabled_manifest",
    "release_enabled",
    "validate_manifest",
]
