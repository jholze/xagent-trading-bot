# Correlated-tier — 90-Tage Phase-3 Opportunity Cost

**Fenster:** 2026-05-14 → 2026-08-12 · 1h only · config.json nicht geschrieben
(`correlated_tier.enabled` and `stagnant_rotation_enabled` remain `false` on disk).

## Ergebnis in einem Satz

Capacity does bind on 1h (43 `max_open_positions` rejects vs 453 taken BUYs), but the names we could not buy were **worse on mean hold-return** at every horizon than the names we did buy. The one Phase-2 `stagnant_rotation` fire did **not** reproduce on a clean re-run of the same tight knobs, so there is no redeploy event to score.

---

## 0. Was diese Phase misst (und was nicht)

Phase 1/2 scored whether a rotated or trail-overlaid position's *own* outcome was better. That is a direct-P&L question. Phase 3 is an opportunity-cost question that applies to the whole book, not just the four `us_stock` tokens:

1. When a BUY is denied because the book is at `max_open`, was the thing we could not buy better — on a simple hold — than the things we did buy?
2. When the one observed `stagnant_rotation` fire freed a slot, did anything actually take that slot, and was it better than what we sold?

**Scoping choice for forward returns (deliberate, not a shortcut):** this is a cached-OHLCV close lookup at `fill_ts + {24h, 72h, 7d}`, divided by the would-be (or actual) entry price, minus 1. No fees, no stops, no trail, no DecisionEngine exit, no second shadow book. The question is "was the missed name a better hold", not "would the bot have exited it well". Taken BUYs get the **same** lookup so the comparison is the same object.

**Capacity reject = live `RiskDecision(code='max_open_positions')`.** In `risk/risk_manager.py`, a new BUY (`not has_position`) is denied when `open_slots >= cap.max_open_eff`. The Phase-1/2 sim already discarded those at fill time (`if (not is_dca) and open_n >= knobs.max_open: skipped_slots += 1; continue`) and only kept a counter. Phase 3 keeps that skip gate unchanged and **logs** the event when it also matches the live condition. Cash-floor and illiquidity skips stay in their own counters (142 and 1773 on this baseline) and are not mixed in.

The sim still uses the static `knobs.max_open` ceiling (36 / 18). Live `risk.position_capacity` (enabled in config.json, adaptive `max_open_eff`) is not replayed — same isolation as Phase 1/2.

## 1. Why 1h only

Phase 1 4h baseline already reported `skipped_no_slot=0` at `peak_open=35` (cap 36). Capacity never bound on 4h. Re-running 4h would spend a simulation pass to reconfirm zero events. 1h Phase 1 had `skipped_no_slot=42` at `peak_open=36` — that is the timeframe where the question can have an answer.

## 2. A — Production baseline (`max_open=36`)

One new 1h pass, production in-memory flags (both experiment flags off), same Phase-1 window and OHLCV cache.

| | Phase 1 baseline | This re-run |
|--|--:|--:|
| n_buys | 453 | 453 |
| n_sells | 1308 | 1308 |
| peak_open | 36 | 36 |
| skipped_no_slot | 42 | 43 |
| capacity_rejections (`max_open_positions`) | (not logged) | **43** |
| skipped_cash_floor | 144 | 142 |
| skipped_too_illiquid | 1772 | 1773 |
| total_pnl_usdt | -15101.83 | -15101.77 |

The path matches Phase 1 to the ruble. The extra slot-skip (42 → 43) is one event in 90 days; cash/illiquid counters move by 1–2. Not a new book.

**How often does capacity bind?** 43 denied BUYs against 453 filled ones (~9.5% of filled entries). They cluster on **19 calendar days** between 2026-06-18 and 2026-08-12, across **19 symbols**. This is not "the book is always full"; it is intermittent saturation, then it clears. Cash (142) and especially illiquidity (1773) reject far more BUYs than slots do. Slots are a real constraint. They are not the dominant one.

**Who got rejected?** 42/43 are `crypto_market`. One is `us_stock` (`SOXLG/USDT` on 2026-07-29). Most frequent: LIKE (5), GNC (4), NYAN (4), then TYCOON / LIT / GMRT / STG / PEPE (3 each).

### Forward-return distribution: rejected vs taken

Same simple hold. Rejected n drops from 43 → 42 / 40 at longer horizons because the last events sit inside 24h–7d of the window end (no tape past 2026-08-12). Taken n drops 453 → 451 / 443 / 430 for the same reason.

| Horizon | Rejected n | Rejected mean | Rejected median | Rejected % pos | Taken n | Taken mean | Taken median | Taken % pos | Δ mean (rej − taken) |
|---------|-----------:|--------------:|----------------:|---------------:|--------:|-----------:|-------------:|------------:|---------------------:|
| 24h | 42 | -3.16% | -1.23% | +38.1% | 451 | +0.42% | -0.75% | +44.8% | **-3.59 pp** |
| 72h | 40 | -5.17% | -1.07% | +35.0% | 443 | +2.41% | -1.41% | +45.4% | **-7.58 pp** |
| 7d | 40 | -2.24% | -2.68% | +45.0% | 430 | +3.08% | -4.24% | +39.1% | **-5.32 pp** |

How to read this without stretching it:

- **Means:** the missed names lose to the taken names at every horizon. The gap widens at 72h. That is the core comparison this phase was built to make, and it does **not** support "we should free slots because better names are waiting."
- **Medians are mixed.** Rejected is worse at 24h (−1.23 vs −0.75) but *less bad* at 72h (−1.07 vs −1.41) and 7d (−2.68 vs −4.24). Taken names have a fat right tail (a few large winners pull the mean to +3% at 7d while the median sits at −4%). The "missed names are worse" claim is a **mean** claim, not a typical-trade claim.
- **% positive** is lower for rejected at 24h/72h, slightly higher at 7d. No horizon has rejected names winning on both mean and hit-rate.
- The single `us_stock` miss (`SOXLG/USDT`) is the opposite anecdote: +19.0% / +17.1% / +42.6% on the three holds. One name. It does not flip the 42-name crypto_market pile.

So: capacity binds often enough to measure (n=43 is not n=2). What we measure is **not** "the queue was full of better opportunities." On average the stuff we could not buy was a worse 24h/72h/7d hold than the stuff we did buy.

## 3. B — The one stagnant_rotation fire (tight book)

Re-run of Phase 2 sweep point `tight_maxopen18_slack8_gain6_idle12`:

- in-memory only: `correlated_tier.enabled=true`, `stagnant_rotation_enabled=true`
- `max_open=18`, `stagnant_slack_slots=8`, `stagnant_gain_pct=6`, `stagnant_idle_hours=12`
- 1h, `peak_stamp=on_progress`
- experiment pass only (no second tight-baseline; Phase 2 already has that Δ)

| | Phase 2 sweep (same knobs) | This re-run |
|--|--:|--:|
| n_buys | 160 | 169 |
| n_sells | 383 | 427 |
| peak_open | 18 | 18 |
| stagnant_rotation_n | **1** | **0** |
| total_pnl_usdt | -3130.32 | -8393.07 |
| capacity_rejections | (not logged) | **218** |

Capacity **does** bind hard on this tight book: 218 `max_open_positions` rejects, cash-floor 0, peak_open pinned at 18. The missing fire is not "the book was never full." The book was full, a lot. `stagnant_rotation` still did not emit a sell.

**No candidate was waiting, because there was no fire to free a slot.** The Phase-2 n=1 event did not come back on a clean two-pass process (production baseline, then this tight experiment). Phase 2 had run shuffled / regime-tape / walk-forward / earlier sweep points in the same process first. That n=1 was already called out in Phase 2 as "ein Fire ist kein Tuning-Ergebnis." This re-run says it is also not a stable event.

We did **not** spend a third simulation pass hunting it. A fire we can only produce by replaying Phase 2's leftover process state is not a finding we should score a redeploy on.

What we *can* say from this pass, without the fire:

- Tightening to `max_open=18` makes capacity bind (218 rejects vs 43 at 36). The "free a slot" motive becomes numerically real only after we shrink the book well below production.
- Even then, the rotation rule that is supposed to do the freeing (`gain=6 / idle=12 / slack=8`) did not fire. Production knobs (`max_open=36 / slack=2 / 8% / 24h`) are looser on slack and stricter on gain/idle — Phase 1/2 already showed they never fire.
- us_stock on this tight pass: n=2, both winners, +78.45 USDT. crypto_market: n=425, −8471.52. Same story as Phase 2's tight point (rotation, when it happened, helped crypto, not us_stock) — except this time the rotation itself is absent.

## 4. Verdict

**Capacity binds often enough on 1h to talk about, and the evidence that freed slots would have caught something better is not there.**

1. **Production `max_open=36` binds intermittently, not constantly.** 43 slot-denials in 90 days, 19 days, 19 symbols. Illiquidity (1773) and cash-floor (142) deny more BUYs than the slot ceiling. 4h never binds. "We need to free slots" is a 1h, sometimes, 36-wide-book story — not a structural emergency.
2. **The missed names were not the better names, on the object this phase measures.** Mean 24h/72h/7d hold-return of capacity-rejected candidates is worse than that of taken BUYs by 3.6 / 7.6 / 5.3 percentage points. Medians are mixed (rejected less-bad at 72h and 7d) because taken names have a winner tail. That is not a queue of alpha sitting outside the book. It is, on average, the same (or worse) crypto_market sludge the bot was already buying.
3. **Part B is too thin to conclude anything about redeploy quality.** The only fire Phase 2 ever saw did not reproduce. n=0 events in this run, n=1 unreproduced events historically. We cannot say the freed slot went into something better, because no slot was freed by `stagnant_rotation` here. Saying "rotation redeploys into better names" from that record would be making it up.

Do not flip `stagnant_rotation_enabled` (or `correlated_tier.enabled`, or any trail/full-close knob) in config.json on the back of this. Phase 2 already said the overlay does not generalize. Phase 3 says the *motive* for rotation — free a slot, catch something better — does not show up as a better waiting queue at production capacity, and the one fire we had to test actual redeploy is not even stable enough to re-measure.

## 5. Was wurde begrenzt / warum

- Exactly **2** new full-universe simulation passes: production baseline + the one Phase-2 tight point that had fired. Wall time ~2 minutes (cache hit, no network).
- No 4h pass. Phase 1 4h already had `skipped_no_slot=0`.
- No sweep, no walk-forward, no shuffled-timing. Those are Phase 2.
- No third pass to chase the missing fire.
- Forward returns are a price lookup, not a third simulation of shadow exits.
- Tight pass is experiment-only. No second tight-baseline — Phase 2 already published that Δ (+2469 book / −59 us_stock).

## 6. Limitations

- Forward returns are a cached-OHLCV close lookup at entry_ts + {24h, 72h, 7d}, not a re-simulated shadow position with its own stop / trail / rotation / fee path. That is deliberate: the question is "was the missed name better on a hold", not "would DecisionEngine have exited it well".
- The sim still uses the Phase-1/2 static `knobs.max_open` ceiling. Live `risk.position_capacity` (enabled in config.json, adaptive `max_open_eff` from regime / cash mode / memory) is not replayed. The reject code we log is still risk_manager's `max_open_positions`.
- Capacity is checked at fill time (next-bar open), which is the sim analogue of `RiskManager.evaluate`. A skipped pending BUY is discarded, not queued: freeing a slot does not automatically admit the last rejected name.
- Other skip reasons (cash floor, participation/illiquidity) are counted separately and are not capacity-rejections. Universe-trade-cap and rebuy-cooldown are not wired into this engine (Phase 1 limitation, unchanged).
- 1h only. Phase 1 4h baseline had `skipped_no_slot=0`.
- Part B has n=0 fires in this run and n=1 unreproduced fires in Phase 2. That cannot support a general claim about redeploy quality.
- The 42 vs 43 `skipped_no_slot` difference vs Phase 1 is one event; headline P&L matches to 0.06 USDT.
- config.json is never written. All flag/knob changes are in-memory deep copies.

## 7. Dateien

- `auswertungen/gis/correlated_tier_backtest_90d_phase3_1h_20260813_053353.json`
- `auswertungen/gis/2026-08-13_correlated-tier-opportunity-cost-phase3.md`
- Phase 2 (unchanged): `auswertungen/gis/2026-08-12_correlated-tier-backtest-90d-phase2.md`
- Phase 1 (unchanged): `auswertungen/gis/2026-08-12_correlated-tier-backtest-90d-phase1.md`
