# Entry indicator lineage (D-LIN)

Version: 1.1 · Date: 2026-11-10 · Status: in review · Owner: Kay

Not a GitHub issue. Follow-up wiring for #600 / #364. Does not replace #628 or #582.

RECALL_FAILED. Hindsight is not reachable from this cloud VM (Mac loopback only).

| File | Stage |
|---|---|
| `04-requirements.md` | Rev 1 acceptance criteria and AC-to-test map |
| `HANDOFF.md` | One-page contract for packages G and H |

Quick path: requirements plus the handoff. No new screen. No config.json change.

## Decisions log

- 2026-11-10 Rev 1: pass already-captured decision fields through the existing first-fill snapshot. Do not add #600 keys. Extended context lives in `entry_lineage` schema `d-lin-1`.
- 90% operational coverage is not measured in this change (`runtime_target_measured: false`).
- No adapter edit. Portfolio reads the order row by the `order_id` the fill path already passes.
