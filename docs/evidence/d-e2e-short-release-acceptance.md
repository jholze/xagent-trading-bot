# Evidence: D-E2E offline short release acceptance (not issued)

Draft id D-E2E. Not a GitHub issue. Phase 1 only: evidence schema, offline chain judge, and a disabled manual manifest. `services/trading_service.py` is not wired. This is not an authenticated testnet pass, not a Short release, and not a strategy result.

Hindsight: `RECALL_FAILED` (Mac loopback only).

Coder: grok-4.7. DeepSeek, Opus, and a separate tester have not reviewed this HEAD.

## Before / After

Before is staging `3995b22ff298fcdef933168e44b9e7565f75e2a1`. After is this branch. The suite below ran on the working tree that this commit records. Local interpreter was Python 3.12.3; CI runs Python 3.13.

| check | before | after |
| --- | --- | --- |
| `tools/short_release_acceptance/` | absent | judge, schema classifier, disabled manifest |
| fixture row can be labeled an authenticated pass | no classifier | refused (`NO-GO` if claimed, otherwise `NOT_VERIFIED`) |
| manual acceptance without a manifest | no command | `attempt_acceptance(None)` makes 0 venue calls and enables no tenant |
| complete manifest | none | still `executable: false`; transport is not called |
| `tests/integration/test_short_release_offline.py` | absent | 10 passed |
| full `./scripts/run_unit_tests.sh -q` with `PYTEST_DB_SUFFIX=pkgH4224b` | not re-run as a baseline | 6806 passed, 2 skipped, 1 xfailed |
| ruff `E9,F821,F823` on the new Python files | n/a | pass |
| `config.json` `shorts.allow_live` | false | false |
| authenticated testnet passes | 0 | 0 |
| #412 | closed `NOT_PLANNED` | unchanged |

Skips and the xfail are existing: Telegram E2E secrets are unset, `decisions.jsonl` is absent, and `test_t7_cap_stop_explicit_volatile_shadow_is_sell_stop_full` is an expected xfail until #696.

## What the tests kill

A fixture chain that repeats a trade id, books an unknown fee as 0, widens the 0.01 USDT tolerance, or moves the stop one tick inside the frozen 5% clearance is not a linked rehearsal. A fault trace with a duplicate client id, a reversal, an emergency flatten, or an adopted orphan is `NO-GO`. A fixture evidence row with `authenticated_testnet_pass: true` is `NO-GO`. An offline producer row cannot be upgraded by changing `kind` to `actual`.

The in-memory `ACCEPTED` classification in `test_ac_e2e_3_evidence_kind_and_sha_guard` is a shape drill. It is not a venue result. The committed index is fixture-only and classifies `NOT_VERIFIED`.

## Not claimed

Implemented here: the phase-1 judge and runbook. Tested: the harness and the repo unit suite, locally. Reviewed: not yet. Merged: no. Deployed: no. Accepted: no.
