# Short release acceptance — disabled manual run

Status: **disabled**. Draft id D-E2E, not a GitHub issue. Revision `rev-1-2026-11-10`.

This page is not an authorization to trade, to read keys, to change a tenant, to reset a ledger, or to deploy. Nothing in this package discovers credentials, contacts Gate, sets an account mode, or flips `shorts.allow_live`. `shorts.allow_live` stays false. There is no command here that opens a socket.

A green offline test is a fixture result. It is not an authenticated testnet pass. #412 stays closed `NOT_PLANNED`. This runbook does not reopen or close it.

## What a future run would have to name

A later, separately authorized operator would fill `tests/fixtures/short_release_acceptance/manifest.disabled.json` and still would not get an execution from this package. `attempt_acceptance` refuses every document, including a structurally complete one. `validate_manifest` reports `executable: false` in every case.

The document must name all of the following. Missing means fail closed.

| Field | Rule |
| --- | --- |
| Tenant | One exact tenant id. `default`, `henry`, `all`, and `*` are refused. |
| Origin | Only `https://api-testnet.gateapi.io:443`. The legacy `fx-api-testnet.gateio.ws` host is not a failover. HTTP and mainnet hosts are refused. |
| Actions | `SHORT`, `protective_stop`, and `COVER` only. ADL, liquidation, flatten, deploy, ledger reset, account-mode change, tenant flip, and a new kill switch are refused. |
| Limits | Leverage 1. Notional at most 10% of virtual NAV. Stop risk at most 0.25% per position and 0.75% across shorts. Safety cap at most 20%, never raised. Exchange minimum quantity must be known and must fit. |
| Window | Europe/Berlin, cutoff 20:00:00, Monday–Friday, same civil day, holding at most 4 hours and below 24. Entry is before 20:00. Exit is at or before 20:00. Funding, when claimed, needs entry < actual settlement < exit <= due. |
| Time buffer | Key `execution_buffer_minutes` must be present and null. No minute value is frozen. The window is not executable. |
| Permit | Emergency flatten stays off. Minutes and amounts are not frozen, so a permit does not authorize a flatten. |
| SHA | 40-hex candidate SHA. The all-zero placeholder is refused. |
| Keys, config, orders | Each authorization field is the sentinel `separately_authorized_not_in_repo`. Key material in the file is refused. |
| Redaction | Secrets, account ids, and raw venue payloads are redacted before any evidence is stored. |

Frozen 2026-11-10 18:40 Europe/Berlin as paper/testnet start values. They are not a live approval and not a strategy result.

## Evidence

Rows live in `specs/short-release-acceptance-v220/evidence-schema.json`. Kinds are `fixture`, `manual`, and `actual`.

- A fixture or manual row is `NOT_VERIFIED`. It cannot report an authenticated pass.
- A stale candidate, reviewer, or tester SHA is `NO-GO`.
- An `actual` row still needs both `deepseek-v4-pro` and `claude-opus-5-5` on that SHA, a separate tester who is not the coder and not either reviewer, and venue-account funding proof. Synthetic or indicative funding is not proof.
- This package's own producer ids cannot be relabeled into acceptance. `acceptance_executed_by_this_package` cannot be true.
- The checked-in index `specs/short-release-acceptance-v220/evidence-index.json` is a fixture index. Its sample SHA is not a reviewed head.

## Offline check

```bash
HOST=127.0.0.1 PYTEST_DB_SUFFIX=pkgH4224 \
  python3 -m pytest tests/integration/test_short_release_offline.py -q
```

That command does not authorize a venue session.

## Still blocked

- B, C, and F contracts are not on this branch. Final system evidence waits for one candidate HEAD.
- Items 6, 8, and 11 are unmeasured.
- #764 is open. The freeze recommends `deny` for the staging stance. Main config stays with Jens. This package does not change it.
- `owner-approved` is not set by this package.
- No DeepSeek review, Opus review, or separate tester has run on this HEAD.
- PR #794 is untouched.
