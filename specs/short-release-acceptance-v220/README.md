# Short release acceptance (D-E2E)

Version: 0.2 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Draft id D-E2E. Not a GitHub issue. Phase 1 is the offline judge, evidence schema, and disabled manual runbook. `services/trading_service.py` is not wired.

| File | Role |
| --- | --- |
| [04-requirements.md](04-requirements.md) | Rev 0 ACs, Rev 1 phase boundary, AC-to-test map |
| [evidence-schema.json](evidence-schema.json) | Row and index shape |
| [evidence-index.json](evidence-index.json) | This package's fixture index. Verdict is not an authenticated pass |

Hindsight: `RECALL_FAILED`. Recall is only reachable via the Mac loopback, not from this cloud VM.

Existing in code, not rebuilt here: `execution/gate_adapter.py` testnet host, `strategies/short_policy.py` 4h cap and cover-always-allowed, `strategies/positions.py` `#600` snapshot keys. Open predecessors own routing, partials, protection, admission, lineage, and net booking. This package does not copy them.
