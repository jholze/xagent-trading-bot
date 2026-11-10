# Evidence D-EXH Rev 1 — raw exhaustion features

Draft id D-EXH is not a GitHub issue. Status: **pending owner freeze**. This is not a profitability result.

Hindsight: `RECALL_FAILED` (Mac loopback only; this VM cannot reach bank `coding-agent::trading_bot`).

Coder model for this change: `grok-4.7`. DeepSeek (`deepseek-v4-pro`), Claude Opus (`claude-opus-5-5`), and a separate tester have not reviewed this head. Those roles are not claimed here.

## Problem

Jens wants exorbitant risers measured for exhaustion (funding, RSI, volume). Marcus marked the thresholds, the lookback, and the divergence definition UNCLEAR, and marked freshness grace undefined. A filter or a short signal would freeze those open definitions. The historical Climax 1x summary stays NO-GO and was not recomputed: profit factor 0.9160, −3500.42 USDT, n=1016, spot-proxy.

`jholze/trading-analyst` pull 1 returned GitHub 404 from this environment. No scanner SHA was pinned. The input contract is the local fixture schema `d-exh-measurement-input` / `r1-raw-2026-11-10`.

## Before / after

| Item | Before | After |
| --- | --- | --- |
| Package `services.short_signal_feed` | absent (`ModuleNotFoundError` collecting `tests/unit/test_short_signal_feed.py`) | raw feature log, default off |
| Order intents on a sandbox fixture | no path | `[]`, `venue_calls` 0 |
| `shorts.climax_fade.enabled: true` alone | old climax path unchanged | does not enable this path |
| RSI | `MarketService._bars_to_dataframe` uses `talib.RSI` period 14; empty frames in `indicators_from_df` return 45 | period 14 logged, no cutoff; empty history is null, not 45 |
| Funding / volume | not logged for this draft | point-in-time history and consecutive ratios; missing stays null |
| Grace | undefined | not applied; age seconds is raw |
| Trial result | Climax 1x NO-GO stands (not rerun) | `insufficient_data`; profit factor null; `#769` `SCENARIO_TRIALS` not modified |
| Targeted tests | collection error | 7 passed |
| Full unit suite | not rerun for this package | 6803 passed, 2 skipped, 1 xfailed |

## Red to green

Red, before any module file existed:

```text
E   ModuleNotFoundError: No module named 'services.short_signal_feed'
ERROR tests/unit/test_short_signal_feed.py
```

Green, same file, `HOST=127.0.0.1` `PYTEST_DB_SUFFIX=dexh8685`:

```text
7 passed in 0.91s
```

A filter mutant (a pass/fail on RSI, a divergence flag, or treating `climax_fade.enabled` as the switch) fails `test_ac_exh_2_raw_features_no_threshold` or `test_ac_exh_3_default_off_and_live_denied`: both a RSI of 100 and a RSI of 0 stay `decision is None`, forbidden keys include `divergence`, and the climax flag is not read.

## Suite

`HOST=127.0.0.1 UNIT_TEST_PROGRESS=0 PYTEST_DB_SUFFIX=dexh8685suite ./scripts/run_unit_tests.sh --parallel -q`

6803 passed, 2 skipped, 1 xfailed, 113 subtests passed, 103.61s. Skips: `tests/unit/test_morning_briefing.py` (no `decisions.jsonl`), `tests/e2e_telegram/test_telethon_optional.py` (`TELEGRAM_E2E` unset). Xfail: `test_t7_cap_stop_explicit_volatile_shadow_is_sell_stop_full` (pre-existing, #696). `find data -newer` the pre-suite marker printed nothing.

`ruff check --select E9,F821,F823` on `services/short_signal_feed` and `tests/unit/test_short_signal_feed.py`: passed.

`python -m tools.hypothesis_protocol --evidence docs/evidence`: no JSON in that directory (exit 0). No evidence JSON was added, because a non-PASS payload would fail `unit` and this trial is `insufficient_data` / not run. Trial id `D-EXH-R1-RAW` is on the measurement report only. `backtest/harness/trials.py` is unchanged, so N does not grow.

## Not done

Thresholds, lookback, divergence, and grace remain pending owner freeze. No backtest was fabricated. `#694` was not edited. `config.json` was not edited. This path is outside `ci/money_paths.json`; `owner-approved` was not set.
