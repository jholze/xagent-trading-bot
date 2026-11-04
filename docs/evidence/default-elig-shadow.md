# Evidence: default tenant runs criteria eligibility (#643 extension) in shadow

Config-only change plus one test update. No runtime code changed. Not merged, not deployed by the author.

## What changes

`config.json` gets a top-level `rule_switches` block with all four `elig_*` rules in `shadow`:
`elig_liq7`, `elig_collapse90`, `elig_chase48`, `elig_mcap`. Shadow logs what the rule would block
(decision tape, `shadow_codes`) and never blocks a BUY. No entry rule (`trend_filter`, `no_chase`,
`vol_sizing`, `age_loss_exit`, `limits`) is enabled.

The default tenant has no `tenant_configs` body; it reads `config.json` only (`data_manager.load_config`,
default path). So the only way to give default the shadow filter without touching its trading rules is
the shipped config.

## Who is affected (measured on staging Mongo `xagent_test`, 04.11.2026 ~10:00 UTC)

Effective config via `data_manager.load_config(tenant)` in the staging MCP container:

| tenant | elig modes before (staging 94dc9fe) | elig modes after (this branch) | entry rules |
| --- | --- | --- | --- |
| default | all `off` | all `shadow` | all off (unchanged) |
| ctexp (no `rule_switches` in its body) | all `off` | all `shadow` | all off (unchanged) |
| henry (own `rule_switches` in Mongo) | liq7/collapse90 `enforce`, chase48/mcap `shadow` | unchanged (tenant body wins in the deep merge) | as in its body |

Trading behaviour (which BUYs pass) is unchanged for every tenant: shadow never blocks.
Cost: with any `elig_*` mode not `off` the guard fetches daily/hourly candles per BUY candidate
(see `docs/evidence/643.md`: "with every mode off the guard fetches no candles"). Expect more
candle requests for default and ctexp; watch latency and rate-limit logs after the staging deploy.

## Tests

`tests/unit/test_criteria_eligibility.py::test_shipped_and_tenant_configs_enable_nothing` asserted the
shipped config enables no elig rule at all. It is replaced by:

- `test_shipped_config_blocks_nothing`: shipped config may use `off`/`shadow` only, never `enforce`,
  and enables no entry rule (guard against an accidental enforce in `config.json`).
- `test_shipped_config_runs_default_elig_in_shadow`: all four elig rules are `shadow`.

| metric (local Mongo 8.0, `scripts/run_unit_tests.sh -n 4`) | staging 94dc9fe | this branch |
| --- | --- | --- |
| passed | 6635 | 6636 |
| skipped / xfailed | 2 / 1 | 2 / 1 |
| collection errors (same two local ImportErrors in both: `test_backtest_harness_properties.py`, `test_observe_streams.py`) | 2 | 2 |
| targeted files (rule switches, eligibility, config, tenant config) | 207 passed, 1 failed before the test update | 209 passed |

## Bestand geprüft

- `risk/rule_switches.py` (#755) and `services/venue_quality.evaluate_eligibility` (#756): modes and
  shadow handling reused as is; no code change needed.
- `docs/evidence/643.md`, section "Intended henry/default config (NOT applied by this PR)": proposes
  liq7/collapse90 enforce for henry and default. This PR deliberately keeps default at shadow only
  (owner decision for the 04.11.2026 fresh start: henry variant B, default keeps its old rules with
  the criteria filter in shadow).
- henry got its variant B body via the existing Mongo `tenant_configs` mechanism (no PR needed);
  backup id `henry:variant_b_reset:20261104T100018Z` in `tenant_configs_backups`.
- Searched branches (`git branch -a`), `git log --all -G '"rule_switches"' -- config.json` (no hit)
  and PRs (`rule_switches`, `elig shadow`: only #755 and #756, both merged): nothing existing sets
  default to shadow.
