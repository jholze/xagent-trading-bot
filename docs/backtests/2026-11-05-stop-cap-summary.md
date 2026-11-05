# Full-stop cap backtest, 2026-11-05: verdict KILL (no implementation)

Paper research only. Nothing here reads keys, writes the DB, places orders, or changes henry/default runtime config.

Inputs:
- Railway export of 2026-10-31: 2476 fills and 1049 episodes, Jul 28 to Oct 30.
- 1h candles from `/workspace/bt-data/candles`.
- Staging stop/DCA code at `0ef3a4bc` (after #774). Partial pause is already grace-only (#733).

Runs: `python -m backtest.harness --scenarios s,s_pause_rerun,s_oct --cost-tier base|stress|hard_stress`.

Full reports:
- `2026-11-05-stop-cap-base.md`
- `2026-11-05-stop-cap-stress.md`
- `2026-11-05-stop-cap-hard_stress.md`

## Rule under test

Cap the full hard stop at −X % vs the running average entry while **keeping DCA adds**. Separately, whether the partial stop should stay paused for the life of the lot after any DCA round (pre-#733) or keep firing after the grace window (staging).

Caps: 12 / 15 / 20 / 25 % vs no-cap baseline. Primary (pre-registered): `cap20` (matches open #696). Alternate primary for the pause factor: `cap20_dcapause`.

## Trials (registered in `backtest/harness/trials.py`)

`s` = 8 candidates (4 caps × {grace partial, forever pause}). `s_pause_rerun` and `s_oct` re-run the same candidates → add 0. Scenario N is now 79. N over everything (incl. long walk-forward) is 87.

## Walk-forward (entries, base costs)

| Variant | PnL USDT | delta vs baseline | W1 / W2 / W3 pnl % | Cap exits | Stopouts later recovered | Open ≤ −20 % |
|---|---|---|---|---|---|---|
| baseline (no cap) | −19,962 | | 1.76 / 0.10 / −11.27 | 0 | 2 | 77/134 |
| cap12 | −3,805 | +16,157 | 1.08 / 0.26 / −5.24 | 216 | 117 | 0/18 |
| cap15 | −12,477 | +7,485 | | 185 | 80 | 0/22 |
| **cap20 (primary)** | **−12,502** | **+7,460** | **1.57 / 0.11 / −8.76** | **135** | **36** | **0/35** |
| cap25 | −15,895 | +4,067 | | 95 | 20 | 11/61 |
| cap20_dcapause | −12,030 | +7,932 | 1.57 / 0.15 / −8.72 | 135 | 36 | 0/35 |

Invested about 2.5M USDT. Forever-pause vs grace differs by a few hundred USDT once a hard cap is on (the cap full-exits before most partials matter).

## Verdicts (release framework rule 4)

| Scenario | Primary | Windows won | DSR (N=79) | Excess bp/day, 95 % CI | PBO | Harness label | Implementation |
|---|---|---|---|---|---|---|---|
| s (entries) | cap20 | 2/3 | 0.00 | ≈ 0 (CI includes 0) | 0.53 | OBSERVE | **KILL** |
| s_pause_rerun | cap20_dcapause | 2/3 | 0.00 | CI includes 0 | 0.53 | OBSERVE | **KILL** |
| s_oct (October book) | cap20 | 2/3 | 0.00 | CI entirely below 0 (base.md: −30.1 [−39.2, −0.7] bp/day) | 0.16 | OBSERVE | **KILL** |

Stress and hard_stress give the same labels. hard_stress equals stress here because the harness never sets `small_cap` (known limit).

### Honest reading

- **No candidate clears GO after DSR.** Primary wins W2/W3 and loses W1; DSR is ≈ 0 and the bootstrap CI of daily excess includes 0. Per the brief ("clearly beats no-cap after DSR correction") that is not a win → **KILL, no henry/runtime PR**.
- **Tighter caps look better on raw PnL but cut more winners.** cap12 recovers +16k vs baseline yet 117 of 216 stop-outs later reclaim the average entry (false stops). That is exactly the cost of a late-stop fix that is too early.
- **Partial-after-DCA forever pause is noise under a hard cap.** Delta vs grace-only is < 500 USDT on the primary. Staging already pauses only in grace (#733); no further change is justified by this tape.
- **#696's −20 % base-config change is not supported by this evidence** for a merge that would also hit tenant `default` and mass-exit open lots. Keep #696 open as history; do not merge it on the strength of this study.

## Decision

**KILL:** no henry overlay, no `config.json` cap, no rebase/merge of #696 from this run. Revisit only with a pre-registered trial on a fresh export (post e20261104T095950Z) or a different primary that is named before the run.

## Measured vs assumed

- **Measured:** tables above; fees 0.2 %/side + class slippage in every cost tier.
- **Assumed:** hourly bars (high/low order unknown); cap modelled as a flat full exit at −cap % while tape DCA stays on; recorded non-stop tape sells stay as recorded.
- **Not modelled:** resting exchange stops, funding, WS tick latency (covered separately by #774).
