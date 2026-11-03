# Evidence: ledger epoch tooling (backup / verify / reset / restore)

Non-money-path change (scripts/, bus/writer_lease.py helper, tests). No trading runtime path calls the new code. No reset was run against Railway; no tenant config was changed. Measured locally on Mongo 7.0.14 (127.0.0.1, scratch DBs `xagent_pytest_*` and `xagent_epoch_smoke`), `scripts/run_unit_tests.sh -n 4`.

## Behaviour

| check | before (staging 23831074) | after (this branch) |
| --- | --- | --- |
| reset without `--tenant` | resets every active tenant + default + henry | refused (exit 2) |
| `--tenant all` / `*` | treated as a tenant id | refused (exit 2) |
| live scope (`<tenant>:live`, `peak_equity`) | not backed up, not reset (high-water mark survives) | backed up; reset sets trades [] and peak_equity = start capital |
| orders_archive parts | deleted by the reset, not exported raw | exported raw, verified, deleted only after verify, restored on restore |
| orders_v2, order_day_stats, portfolio_nav_daily, obs_*, memory_* | not exported | exported per tenant (JSONL, canonical extended JSON); never deleted |
| checksum / count verify | none | manifest sha256 per file + manifest.sha256, line counts, ledger summary, Mongo counts (`verify --against-mongo`) |
| restore | none | `restore` (dry run default), refuses a broken backup |
| epoch marker | none | `ledger_epochs` doc + `ledger_epoch` in the fresh trade_history docs |
| default | `--apply-reset` ran immediately after the snapshot | dry run unless `--apply --confirm-writers-stopped` |
| running writers | not checked | takes the writer lease (#306) for every tenant/scope; refuses while a bot/side service holds it, without Redis, or when Mongo has a newer fence |
| balance vs config | `--balance` only rewrote JSON files; cash is replayed from config capital | refused unless `--balance` = config start capital |
| `reset_demo_ledger.py` | no backup, default demo only | refused unless `--unsafe-no-backup` |
| `reset_railway_test_ledger.sh` | called reset_demo_ledger.py | backup -> verify --against-mongo -> reset dry run; apply only with `RESET_LEDGER_APPLY=1` + Redis service |

## Tests

| metric | before | after |
| --- | --- | --- |
| tests in `tests/unit/test_ledger_epoch.py` | 0 | 32 |
| full unit suite passed (pre-rebase, staging 23831074 + branch) | 6577 | 6609 |
| skipped / xfailed | 6 / 1 | 6 / 1 |
| suite exit code | 0 | 0 |
| ruff E9,F821,F823 on changed files | n/a | clean |

Hand mutants (each must make a test fail): skip the live-scope reset, accept `all`, skip the confirm flag, ignore lost Mongo docs, ignore backup age, overwrite extras on restore, keep stale archive parts on restore. All 7 are caught.

## CLI smoke (scratch DB, local Mongo)

| step | result |
| --- | --- |
| `backup` without `--tenant` / with `--tenant all` | `REFUSED`, exit 2 |
| `backup --tenant henry --tenant default` | 35 files; henry:demo 9 order rows (5 hot + 4 archived), 3 open lots, cash 8352.5; henry:live peak_equity 108500 |
| `verify --against-mongo` | OK |
| `reset` (dry run) | plan printed, nothing written |
| `reset --apply` without `--confirm-writers-stopped` | `REFUSED`, exit 2 |
| `reset --apply --confirm-writers-stopped` without the bot's Redis | `REFUSED: writer lease ... Redis unreachable`, exit 2, verify still OK (nothing written) |
| `snapshot_and_fresh_start.py --apply-reset` / `reset_demo_ledger.py --yes` / `reset_railway_test_ledger.sh` (no tenant) | `REFUSED`, exit 2 |

A first smoke run before the lease change showed that outside pytest the store refuses every ledger write without the writer lease (`LedgerWriteFailed: no writer lease`); restore put the scratch DB back. That is why reset/restore now take the lease first and fail before any write.
