# Profit floor backtest, 2026-11-04: verdict KILL (no implementation)

Paper research only. Nothing here reads keys, writes the DB or places orders.

Inputs:
- Railway export of 2026-10-31: 2476 fills and 1049 episodes, Jul 28 to Oct 30.
- 1h candles from `/workspace/bt-data/candles`.
- Staging code for the stop/DCA logic.

Runs: `python -m backtest.harness --scenarios p,p_t8_rerun,p_oct --cost-tier base|stress|hard_stress` and `python -m backtest.harness.fee_sweep`.

Full reports:
- `2026-11-04-profit-floor-base.md`
- `2026-11-04-profit-floor-stress.md`
- `2026-11-04-profit-floor-hard_stress.md`
- `2026-11-04-profit-floor-fee-sweep.json`

## Rule under test

Once a lot has reached +X % (the bar high vs the average entry), it must never close below a net floor.

The floor price is the gross price that nets +floor % after exit slippage and both taker fees.

Arming is applied after the bar's exits, so a lot is never armed and stopped on the same bar.

Recorded tape sells (trail, take-profit, rotation, manual) still apply as recorded. The floor only adds exits.

## Trials (registered in `backtest/harness/trials.py`)

`p` = 11 candidates:
- arm {4, 6, 8} % x net floor {0.5, 1, 2} %
- `pf_a8_trail3_f3`: the earlier TP recommendation (arm 8, 3 % trail from the peak high, floor +3 %)
- `pf_a4_f0.5_close`: the primary, checked and filled on the candle close (stop-fill realism)

`p_t8_rerun` and `p_oct` re-run the same candidates with another primary or another book, so they add 0 trials.

Scenario N is now 71. N over everything, including the long walk-forward, is 79.

## Walk-forward (entries mode: windows W1/W2/W3 = Jul 28 to Aug 26, Aug 26 to Sep 23, Sep 23 to Oct 23; base costs)

| Variant | PnL USDT | delta vs baseline | W1 / W2 / W3 delta pp | Floor exits | below floor | mean net of floor exits |
|---|---|---|---|---|---|---|
| baseline (staging) | -19,962 | | | | | |
| pf_a4_f0.5 (primary) | -19,024 | +938 | -0.24 / +0.25 / +0.63 | 275 | 47 | +0.17 % |
| pf_a4_f1 | -21,665 | -1,703 | -0.51 / +0.36 / +0.55 | 294 | 57 | +0.61 % |
| pf_a4_f2 | -18,606 | +1,356 | -0.33 / +0.40 / +0.71 | 326 | 94 | +1.42 % |
| pf_a6_f0.5 | -8,659 | +11,303 | +0.52 / +0.50 / +0.35 | 164 | 30 | +0.11 % |
| pf_a6_f1 | -8,155 | +11,807 | +0.52 / +0.56 / +0.36 | 170 | 38 | +0.54 % |
| pf_a6_f2 | -6,432 | +13,530 | +0.63 / +0.57 / +0.42 | 186 | 51 | +1.34 % |
| pf_a8_f0.5 | -8,116 | +11,846 | +0.78 / +0.35 / -0.09 | 95 | 16 | -0.07 % |
| pf_a8_f1 | -7,227 | +12,735 | +0.82 / +0.38 / -0.07 | 98 | 20 | +0.36 % |
| pf_a8_f2 | -6,310 | +13,652 | +0.91 / +0.34 / -0.02 | 104 | 26 | +1.19 % |
| pf_a8_trail3_f3 (TP rec.) | -3,606 | +16,356 | +1.05 / +0.36 / +0.18 | 175 | 34 | +5.88 % |
| pf_a4_f0.5_close | -19,727 | +235 | -0.04 / -0.04 / +0.35 | 235 | 235 | -1.15 % |

Invested about 2.5M USDT in every variant. The deltas are 0.04 to 0.65 % of the invested capital.

## Verdicts (release framework rule 4)

| Scenario | Primary | Windows won | DSR (N=71) | Excess bp/day, 95 % CI | PBO | Verdict |
|---|---|---|---|---|---|---|
| p (entries) | pf_a4_f0.5 | 2/3 | 0.00 | -8.2 [-25.3, +3.5] | 0.24 | KILL (guardrails: share of open lots <= -20 %, max DD) |
| p_t8_rerun (entries) | pf_a8_trail3_f3 | 3/3 | 0.08 | +5.5 [-5.9, +13.9] | 0.24 | KILL (guardrail: share of open lots <= -20 %) |
| p_oct (October book) | pf_a4_f0.5 | 2/3 | 0.10 | +6.2 [-5.6, +12.0] | 0.27 | KILL (same guardrail) |

Stress and hard_stress give the same labels. The PnL deltas move by under 0.2 pp.

hard_stress equals stress here, because the harness never sets `small_cap`. That is a known harness limit, not a result.

### Honest reading

- **The guardrail that fails is partly a denominator effect.** The floor closes winners, so fewer lots stay open: 134 -> 91 for the primary. The open lots at <= -20 % actually fall from 77 to 61. Over all lots, the lots at <= -20 % fall from 95 to 70, and the capital in losing lots falls from 333k to 192k. I did not change the guardrail: moving the goalposts after seeing the result is exactly what the framework forbids. Kay or Jens should decide whether `share_open_le_minus20` stays the right guardrail for exit rules.
- **The verdict is not GO even without that guardrail.** No candidate has DSR > 0.5, and no bootstrap CI is above 0. The best case would be OBSERVE.
- **The primary (+4 % / +0.5 %) is noise.** Its delta is +938 USDT on 2.5M, it lost W1, and its excess per day is negative.
- **The useful signal is in the higher arm levels.** a6/a8 and the trail variant are consistently better (+11k to +16k), but not significantly so.

## Stop-fill realism (measured)

| Fill model | Floor exits | realised below floor | mean realised net |
|---|---|---|---|
| intrabar touch, fill at min(open, max(level, low)) minus slippage | 275 | 47 (17 %) | +0.17 % (target +0.5 %) |
| candle close, fill at the close | 235 | 235 (100 %) | -1.15 % |

The bot's current WS/cycle exits often act on a candle close or a delayed tick. With a candle-close fill, the "never below +0.5 %" promise turns into an average realised -1.15 %, and the primary's edge disappears (+235 USDT).

The floor only holds if it is a real resting stop at the exchange, or WS-tick driven with a working stale-price guard (PR4).

## Break-even fee sensitivity (measured, taker fee per side 0 to 2 %, base slippage)

| fee/side | a4_f0.5 | a6_f2 | a8_f2 | a8_trail3_f3 | a4_f0.5_close |
|---|---|---|---|---|---|
| 0.0 % | +527 | +11,842 | +13,546 | +16,058 | -1,974 |
| 0.2 % (model) | +938 | +13,530 | +13,652 | +16,356 | +236 |
| 0.5 % | -452 | +14,528 | +14,609 | +16,743 | -2,438 |
| 1.0 % | +3,793 | +14,785 | +15,935 | +17,008 | +95 |
| 2.0 % | +8,120 | +18,634 | +20,895 | +19,331 | +3,808 |

There is no break-even fee in the range: the excess does not shrink with higher fees.

This is because the floor is defined net of fees, so a higher fee raises the exit level and sells earlier. Both sides pay the same entry fee, and a lot sells once either way.

So the excess is not a fee artefact. But the primary's sign flips within the grid (around 0.45 % and 0.5 %), which confirms it is noise.

## Versus BTC Buy & Hold (same period, peak deployed capital)

| | Return | Max DD |
|---|---|---|
| BTC B&H | +22.3 % | 10.0 % |
| baseline (staging) | -5.2 % | 19.4 % |
| pf_a4_f0.5 | -7.9 % (smaller capital base) | 20.3 % |
| pf_a8_trail3_f3 | -1.3 % | 17.2 % |

Every variant is about 23 to 30 pp behind BTC B&H.

## Decision

**KILL:** no implementation and no config, per the brief ("if positive, implement behind config, otherwise KILL").

## Measured vs assumed

- **Measured:** everything in the tables above.
- **Assumed:**
  - Hourly bars, so the order of high and low inside a bar is unknown; handled by arming only after the bar.
  - Taker fees and class slippage from `tools.hypothesis_protocol`.
  - Recorded tape sells stay as recorded.
- **Not modelled:** funding, partial fills, and the exchange rejecting a resting stop.

## Follow-up worth a pre-registered trial (not done here)

Arm 6 to 8 % with a trail, re-run on fresh data after the October export. The register already counts these candidates; a new run on new data adds 0 trials only if the candidates stay exactly the same.
