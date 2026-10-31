# Entry quality replay, 2026-11-01

Paper only. This is an offline replay of recorded buys. It does not change order flow, config, or exits.

The question was whether a pre-registered entry filter would have avoided the losing buys. It would not have passed the house rules. Do not turn a filter on.

## What the attached tape already showed

Window 2026-10-03 through 2026-10-31, Europe/Berlin, three paper tenants. NAV from the attached daily series, first day to last day:

| tenant | 2026-10-03 | 2026-10-31 | change |
| --- | ---: | ---: | ---: |
| default | 117,391.75 | 103,765.93 | −11.61% |
| henry | 107,660.00 | 87,143.78 | −19.06% |
| ctexp | 111,215.48 | 89,592.06 | −19.44% |

Open lots priced on 2026-10-31, share at −20% or worse: default 30/51, henry 25/36, ctexp 24/37. Together 79/124 (63.7%). That is the owner's rule that positions at −20% or worse should not exist. It is a mark on the open book, not a 72-hour return.

`buy_decisions_forward_returns.csv` has 12,364 rows, not ~23k. The file's own readme says it is already the first buy decision per tenant, symbol, code, and day. Its published filled `entry_sensor_15m` 72h median is −4.66% (n=87). This replay reads that cell as −4.66 (n=87). A recomputed gross return, filled at the next 1h open, is −4.23% (n=84). The gap is the one-bar delay, not a different population.

Filled buys in `orders_v2_filled_alltime.jsonl`: 1,530 from 2026-07-29 through 2026-10-26. Adds (`source=dca` or signal `BUY_DCA`) are out of this study: 1,148 rows. First entries after de-duplicating the same symbol in the same UTC hour: 827.

## How the replay was scored

Registered before the run. Not refit afterwards. The list is `trial_specs()` in `tools/entry_filter_replay.py` (31 trials).

- Fill at the open of the next 1h candle after the signal. Exit at the open 24h and 72h later. Same-bar fills are not used.
- Daily features use the last daily bar that had already closed. A Berlin calendar day with no clock time is treated as 00:00 Europe/Berlin, so the replay cannot see that day's close.
- Costs: taker 0.2% per side plus the s2 slippage schedule. Non-stable names pay 0.20% slippage per side at the base tier (0.80% round trip). Two protocol-window entries are stablecoins and pay 0.10%. Stress and hard stress were run on the three primary rules. A flat cost cancels in the mean-edge difference; hard stress does not, because small-caps pay more.
- Spot funding does not apply (`funding: "not_applicable"`).
- "Hit −20%" means some 1h low in the 72 hours after the fill was at least 20% under the fill price. Seven-day hit rate is reported beside it and was not used to rank.
- Small-cap means not in the top 100 by median daily quote volume of the prior 30 UTC days, built from 1h candles, using only days before the signal. Missing volume counts as small-cap. The peer set is the symbols on this tape, not the whole Gate catalog.
- Listing age is the first daily candle at or after 2026-04-01. Coins already listed then are treated as at least that old. That lower bound is above 112.5 days for every fill in this window, so old coins are not dropped by mistake. Coins that first appear after 2026-04-01 use that candle as the listing.
- Protocol window for the kill rules: signal days 2026-07-25 through 2026-10-23. Walk-forward windows: 2026-07-29..08-26, 2026-08-27..09-23, 2026-09-24..10-23. Halves: through 2026-09-08, and 2026-09-09..10-23. A window wins only when the kept trades' mean net 72h return beats the unfiltered trades in that window.
- Rank, frozen beforehand, among the eight primary rules only: at least 100 kept trades first, then walk-forward wins, then how much the −20% share falls, then the 72h median, then the 72h mean. The grid, stress, and extra-delay rows are checks. They are not candidates to pick from.
- Trial count N for the deflated Sharpe is 31. Hold-out entries are 0. The hold-out in the protocol starts 2026-11-06. This tape can kill a rule. It cannot prove one.
- Sharpe is the mean of the net 72h trade returns divided by their sample standard deviation. It is not annualized.

Inputs: decisions sha256 `6c11bd7eac7b9a5d5215079efd5af567ed49fadcf6e83a9a62670711726d0a1d`, fills sha256 `e06ef6b8e41a2bddb2ae8d52a30616c6024149d3396f8ca14ff93151ebdd0ff2`. Candles are public Gate spot klines, cached locally, no key. The machine tables are `2026-11-01-entry-quality-replay-report.json` next to this file. The hypothesis payload is `2026-11-01-entry-quality-replay.json`.

Of 823 first entries inside the protocol window, 730 have a complete 72h path and a 50-day average. 93 (11.30%) do not. The protocol marks an exclusion above 10% as UNCLEAR. That reason is on every primary trial.

## Ranked table

Net of fees and base slippage. "Before" is the scored book with no filter. "After" is the trades the filter would have kept. Hit −20% is the 72h path, not the open-book mark.

| filter | kept n | 72h median before | 72h median after | hit −20% before | hit −20% after | walk-forward wins | protocol |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| trend + distance (20/20) | 272 | −1.17 | −0.82 | 8.63 | 1.47 | 2 of 3 | KILL |
| distance 20/20 | 443 | −1.17 | −0.78 | 8.63 | 2.03 | 2 of 3 | KILL |
| drop small-caps (top 100) | 471 | −1.19 | −1.04 | 8.59 | 5.10 | 2 of 3 | KILL |
| trend + BTC daily SMA50 | 536 | −1.17 | −1.19 | 8.63 | 9.33 | 2 of 3 | KILL |
| coin daily close above SMA50 | 549 | −1.17 | −1.21 | 8.63 | 9.47 | 2 of 3 | KILL |
| BTC daily close above SMA50 | 715 | −1.19 | −1.21 | 8.59 | 8.53 | 1 of 3 | KILL |
| listed at least 30 days | 732 | −1.19 | −1.18 | 8.59 | 8.61 | 1 of 3 | KILL |
| listed at least 90 days | 724 | −1.19 | −1.20 | 8.59 | 8.70 | 1 of 3 | KILL |

The small-cap and listing rows score 733 entries rather than 730, because they do not require a 50-day average. Their "before" column is that slightly larger book.

72h detail for the two rules that actually change the book, and for the plain trend rule:

| filter | kept n | 72h mean after | share positive after | 24h median after | dropped n | dropped 72h median | dropped hit −20% | 7d hit before | 7d hit after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend + distance | 272 | +0.53 | 40.1% | −0.76 | 458 | −1.67 | 12.88 | 12.60 | 3.31 |
| distance 20/20 | 443 | +0.33 | 41.5% | −0.89 | 287 | −3.00 | 18.82 | 12.60 | 4.51 |
| coin above SMA50 | 549 | −0.14 | 39.0% | −1.09 | 181 | −0.87 | 6.08 | 12.60 | 13.66 |

Share positive on the unfiltered book is 39.7%. The filters barely move it.

## Why the first-ranked rule is still a kill

`trend50-and-distance20`: keep the buy only when the coin's last closed daily close is above its 50-day average, the close is at most 20% above that average, and the last closed 1h close is at most 20% above the close 24 hours earlier.

On the scored book it keeps 272 of 730. The 72h median goes from −1.17% to −0.82%. The share that trade 20% down within 72 hours goes from 8.63% to 1.47%. The mean of the kept trades is +0.53% after costs, against −0.28% unfiltered (edge +0.81 percentage points). Walk-forward wins 2 of 3:

| window | entries | kept | edge (percentage points) | win |
| --- | ---: | ---: | ---: | --- |
| 2026-07-29 .. 08-26 | 344 | 124 | −1.63 | no |
| 2026-08-27 .. 09-23 | 203 | 81 | +3.33 | yes |
| 2026-09-24 .. 10-23 | 183 | 67 | +2.26 | yes |

`tools.hypothesis_protocol.validate_payload` returns **KILL**. Reasons, in the module's words:

- `dsr<0.95` — deflated Sharpe is 0.16, with N=31. Per-trade Sharpe is 0.06.
- `edge_half_a<=0` — through 2026-09-08 the edge is −1.20 percentage points (152 kept of 399). The second half is +3.26 (120 of 331). The early window is where the rule loses.
- `edge_without_top10<=0` — after removing the 10 best kept trades, the edge is −0.23. The positive mean does not survive its best trades.
- `excluded_entry_pct>10` — 11.30% of first entries had no complete path or no 50-day average. That comparison is also UNCLEAR.
- `holdout_entries<100` — the hold-out has not started, so the trial is also withdrawn. KILL is the worse status, so the payload is KILL.

The same half-A loss and the same top-10 failure show up under stress slippage, under hard stress, and with a second bar of delay. The distance grid at ±25% (15 and 25, one parameter at a time) keeps a positive full-sample edge, so that grid is not a cliff. The plain trend grid is a cliff: SMA 38 has a negative edge. The combined rule was not given its own 3-by-3 grid; that is recorded as `parameter_grid_ran: false` and is not treated as a pass.

The payload is not in `docs/evidence/`. A non-PASS file there fails the unit job. The check was run on the investigations file instead.

## What the other ideas did

**Plain trend (close above the 50-day average).** This is the Herman idea on its own. It does not help. Kept trades have a worse median (−1.21% vs −1.17%) and a higher 72h −20% rate (9.47% vs 8.63%). The 181 trades it would have blocked had a better median (−0.87%) and a lower −20% rate (6.08%). The mean edge is a small positive (+0.15 percentage points) and dies without the top 10 trades. SMA 38 flips the edge negative, which is a parameter cliff. Do not ship "only buy above the average" from this tape.

**Distance alone (not more than 20% above the average, and not after a 24h gain over 20%).** This does the work inside the combined rule. Median −0.78%, −20% rate 2.03%, mean edge +0.62 percentage points, 443 kept. It fails the same way as the combined rule: the first half is worse (−1.10), and the edge dies without the top 10 trades. Walk-forward wins the second and third windows and loses the first.

**BTC daily close above its 50-day average.** It drops 18 of 733 scored entries. In the second half it drops nobody, so the edge there is zero. Walk-forward wins 1 of 3. It is not a filter on this window.

**Drop small-caps.** The −20% rate falls from 8.59% to 5.10%, and the median improves a little. The mean gets worse. The 262 small-cap entries have a mean of +0.65% and a median of −1.92%: a few large winners carry them. The edge versus the unfiltered book is −0.54 percentage points, and the group's median edge is negative. The rank places this third because of the walk-forward count and the −20% share. The edge test still kills it. Do not drop small-caps for expectancy.

**Time since listing.** Almost a no-op. "At least 30 days" drops 1 scored trade. "At least 90 days" drops 9, and those 9 had a positive median, so the rule's edge is negative. Most coins that are actually new never reach the scored set, because they do not have a 72h path yet (51 rows younger than 30 days, 1 of them scorable). There is no result here.

**Volatility-scaled size (report only, not a trial).** Target daily vol 5%. Size multiplier is `min(1, 0.05 / realized daily vol)` from the last 24 closed hourly returns. Wild coins shrink. Quiet coins stay at full size. On 733 scored first entries the sum of 72h USDT PnL goes from −2,024 to +433. The median multiplier is 0.89 and 54% of trades are scaled down. That sum is not a pass. The worst raw loss is about −1,801 USDT on HFT/USDT (2026-08-06) and scaling cuts that trade to about −279. The worst loss after scaling is still about −737 USDT, on KMNO/USDT (2026-08-07), which was left at full size because its daily vol was under 5%. The 5 USDT cap in the Herman/Volrix note is not an owner rule, and this book does not come near it. Sizing does not stop a −20% price path.

**By source (descriptive, not a trial).** A source ban was not pre-registered, so none is recommended from this table. On the protocol window, after costs:

| source | scored n | 72h median | 72h mean | share positive | hit −20% |
| --- | ---: | ---: | ---: | ---: | ---: |
| grid | 202 | −0.29 | +3.05 | 48.0% | 7.92 |
| cmc | 132 | −2.71 | −2.45 | 40.9% | 12.12 |
| gainer_relvol | 123 | −0.95 | −0.69 | 39.8% | 6.50 |
| entry_sensor_15m | 110 | −4.54 | −4.96 | 20.0% | 7.27 |
| lc | 84 | −0.35 | +0.60 | 47.6% | 4.76 |
| auto | 74 | −0.78 | −0.56 | 35.1% | 10.81 |
| gainer_rank_entry | 6 | −13.63 | −16.98 | 0% | 33.33 |

`entry_sensor_15m` is the weak large source on the long tape as well as in the October file. `gainer_rank_entry` is already a prevention-rule hard block; six scored fills are not a new study. Grid's positive mean is a right tail (median still slightly negative) and 45 further grid rows had no complete path.

## October book, separately

First-entry fills with a Berlin day from 2026-10-03 through 2026-10-31: 98 candidates, 94 scored. That is under 100, so this slice cannot prove a rule. It is the window where NAV fell.

| filter | kept n | 72h median before | 72h median after | hit −20% before | hit −20% after |
| --- | ---: | ---: | ---: | ---: | ---: |
| trend + distance | 42 | −4.78 | −4.40 | 6.38 | 0.00 |
| distance 20/20 | 54 | −4.78 | −4.13 | 6.38 | 0.00 |
| coin above SMA50 | 82 | −4.78 | −4.97 | 6.38 | 7.32 |
| BTC above SMA50 | 91 | −4.78 | −5.05 | 6.38 | 6.59 |

Even the distance rule leaves a median loss of about −4%. None of the 54 kept October fills hit −20% inside 72 hours. The open lots at −20% are not that 72-hour path.

The deduped decision file for the same weeks (filled and blocked, one row per tenant, symbol, day, and source) excludes 27–29% of rows for missing candles or an unfinished 72h path. That is above the 10% line, so the blocked-decision comparison is UNCLEAR and was not used to rank. Every primary filter still shows a negative 72h median on the rows that can be scored.

## Recommendation

Do not change buys. No filter here is allowed to block an order.

The rule that ranks first on the frozen score would skip a buy unless the coin is above its 50-day average, not more than 20% above that average, and not up more than 20% in the last 24 hours. It trims the 72-hour median from −1.17% to −0.82% and cuts the share that fall 20% inside 72 hours from 8.6% to 1.5%. The average of what it keeps is slightly positive, and it wins the later two walk-forward windows. It loses the first half of the sample, the gain is the top 10 trades, and the deflated Sharpe is 0.16. That is a kill, not a green light.

Buying only because the coin is above its 50-day average does not avoid the losers. Those blocked trades did better over 72 hours. Bitcoin's own average barely binds. Refusing small-caps throws out winners along with the dips. Shrinking wild positions changes the dollar total and leaves a large loss on quiet names.

The open book at −20% is a different fact from the 72-hour path. About 9% of these entries trade 20% down inside 72 hours, and about 13% inside 7 days. About two thirds of the lots still open on 31 October are at −20% or worse because they were held much longer than that. An entry filter does not close them. A max-loss rule for open positions is a separate spec, and the owner has not set the number.

The draft spec `specs/entry-quality-filter/` describes the first-ranked rule as a log-only observe flag. Default observe. It must not change an approval. Enforce mode is out of scope until a later revision, and only if a hold-out actually passes.

## Reproduce

```
python -m tools.entry_filter_replay \
  --decisions <buy_decisions_forward_returns.csv> \
  --fills <orders_v2_filled_alltime.jsonl> \
  --cache .cache/entry_filter_replay.sqlite \
  --out <directory> \
  --network
```

`--network` fetches public Gate candlesticks only. Unit tests do not use the network. Re-running from `--from-features` rebuilds the tables without a fetch. Do not retune `trial_specs()` to this result.
