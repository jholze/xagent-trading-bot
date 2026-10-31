# Strategy hypothesis protocol

Kill rules for a strategy test. The coded check is `tools/hypothesis_protocol.py`. It judges an evidence JSON file. It does not run the backtest. CI fails `unit` when any `docs/evidence/*.json` is not `PASS`.

Do not invent a threshold. Items marked **open** or **UNCLEAR** stay that way.

## Kill rules (scout list, then the later ledger)

A run is not a pass when any of these hold:

- Same-bar fill. The run is invalid and is repeated. The module status is `INVALID`.
- Sharpe > 3. Status `SUSPICIOUS` (not a pass). Sharpe = 3 is not this rule.
- Fewer than 100 trades. The scout list calls this a kill. Ledger s3 says the top-10 result is **UNCLEAR** and is not a pass. The module uses `UNCLEAR`. Both mean the file does not pass CI.
- Parameter cliff on a ±25% grid. Status `KILL`. The module does not rebuild the grid. The evidence must set `parameter_cliff` and `parameter_cliff_grid_pct` = 25.
- Signal against the higher frame (4h bias / 1h trigger). Status `KILL` when `signal_against_higher_frame` is true.
- Buying into a loss. Status `KILL` when `buying_into_loss` is true.
- Deflated Sharpe: trial count N is taken from the ledger. **DSR < 0.95 kills.** DSR = 0.95 does not.
- Walk-forward wins on fewer than 2 of 3 windows. Status `KILL`. A report that is not 3 windows is also `KILL`.
- Hold-out effective entries per group < 100. The trial is withdrawn (`WITHDRAWN`), not a pass.
- Edge after removing the top 10 entries ≤ 0. Status `KILL`. If the variant has fewer than 100 trades, that top-10 result is `UNCLEAR` and is not a pass (s3).
- Edge ≤ 0 in either half. Status `KILL`.
- Group median ≤ 0. The group is not shown, and no single trial saves it. Status `KILL`.
- More than 10% of B0 entries excluded. Status `UNCLEAR`. 10% exactly is not this rule.

Every test also has a 1-bar delay, a point-in-time universe, fees, slippage, and funding. Missing any of those is not a pass (`KILL` or `INVALID` as coded). Spot funding does not apply; the evidence must say `funding: "not_applicable"` or give the funding percent that was used.

## Cost model (current = ledger s2, 2026-10-26)

Taker **0.2% per side**, plus slippage by the paper model:

| Class | Base slippage per side |
| --- | --- |
| stable | 0.10% |
| other | 0.20% |
| volatile | 0.35% |

- Stress slippage = `max(0.3%, class slippage)`.
- Hard stress slippage = `max(s1, class slippage)`.
- s1 = **0.5% per side for a small-cap alt**, and **0.3% per side for every other entry**. The earlier flat "hard stress 0.5%" line is superseded by this. The three hard-stress trials still count in N.

**Small-cap alt** (Lena): at entry the Gate spot USDT pair is not in the top 100 by median quote volume of the last 30 days, from 1h candles, point in time, using only data before the signal candle. Missing volume counts as small-cap.

Fee 0.2% per side is the config rate. A read `execution.fee` overrides it once that field exists (E1/E2 pre-registration). Funding does not apply to spot.

## E1 / E2 pre-registration (2026-10-26, before any run)

No data pulled and no trial run at the time of the ledger. Later blocks below replace hashes and the cost line. They do not mark any trial as run.

- In-sample signal time 2026-07-25 through 2026-10-23 (at least 2026-08-24 through 2026-10-23). That window may only kill, not prove.
- Hold-out from 2026-11-06 00:00 Europe/Berlin, stop 2026-12-31.
- Exit comparison: fixed 72h at the open of the 1h candle. Fill at the open of the first 1h candle after the signal (+1 bar). Delay variant: the 4h candle one bar further back, fill at the open of the second 1h candle after t.
- E1+E2 counts only if, on the hold-out, it beats both single filters on net EV and on DSR. Otherwise it is discarded.

### Binding sources (s2, replace s1)

| File | sha256 |
| --- | --- |
| E1-4h-trendfilter.md | `36ffff79530e894ed6808910ec27925b38b2306df5ada9b18e6a11457e629030` |
| E2-btc-regime-sperre.md | `c984ed03e0e67cfe78ec5e8b0d79c10c6617acea5449db812caf85f6d841986b` |
| E-universe-archive.csv | `b65e74fa04a102c0e2cda284ddce809e758be353e8b1efa08693f82e8608026c` |

s1 hashes (superseded, kept so a later reader can see what changed): E1 `14e55844bec81e3e9a7921ad57e4458f22bdaafae07672d9e869a4d44d4e7758`, E2 `88942c41523d26e3a8a42e5f54ee7857a0d6839a8f8d7476cf5efc75fe2d276d`. The hashes `fc4d5b6d` / `9e5decf9` were never valid.

**UNCLEAR (s3):** Lena read `E-s3-entwurf.md` as sha256 `1f03fbfbc754356305f28704c821197ba7af53b9074c580c53428d46c06da7fc`. Scout reported `f3d0cd38`, which does not match. s3 is valid only on the hash Scout confirms for that file. No new variants. N stays 50. The checks only kill; they never select. A failed variant stays in the ledger and in the median.

### Halves and warmup (s3)

- Half A: 2026-07-25 through 2026-09-08. Half B: 2026-09-09 through 2026-10-23 (Berlin).
- Edge versus B0 ≤ 0 in either half kills the variant.
- Candles and volume load from 2026-06-20. Counting starts 2026-07-25. If a symbol lacks the warmup, that entry is dropped on both sides (variant and B0). Report the count. More than 10% of B0 entries excluded → the comparison is **UNCLEAR**.
- E2 by half and by segments S-704 and S-ZS is a report only, not a kill.
- H1 needs its own addendum with an episode definition before it is computed. **open** until that addendum exists.

### E2 regime

AB only from candles: BTC 4h close under EMA50 and EMA50 falling. Oracle labels (`RISK_OFF` / `CRASH`) are a descriptive column from a logged tape. Never reconstructed. Never a trial.

### Other ledger facts

- **UNCLEAR:** the protocols assigned only id `B0`. The other trial ids were built by Lena from group + parameters, 1:1 with the protocol parameters, not copied as ids from the protocols.
- Effective first entries are unique by symbol + signal candle across tenants `default` and `henry`. The same coin on the same candle counts once.
- Rejected buy signals are their own counterfactual group (path from the candle after the signal). Group R uses only 50/6, no ±25% grid.
- K-a. Cycle silence is a structural break: henry from 2026-10-17, default from 2026-10-20, end **open**. Exits in that span are from the websocket only. For H1/K7 that window is **UNCLEAR** and does not count. E1/E2 compute MAE and return from candles, not from bot exits.
- K-b. Before #704 (2026-10-22 23:06) slot counts mixed tenants. Split first entries by tenant. Time before 2026-10-22 is its own segment. An effect that exists only because of that mix is dead.
- K-c. Symbol source for now is only `orders/` and `lots/` in the archive. The Mongo list since 2026-07-25 is missing. **UNCLEAR.** No point-in-time universe. Survivor and selection risk.
- Archive symbols are evidenced only from 2026-09-23 (henry) and 2026-09-29 (default). Before that, **UNCLEAR**, until the Mongo list exists.
- N for DSR is **50** (34 scout + 3 hard-stress + 13 group R). This replaces the earlier 37.

Any further change to the protocols needs a new ledger block before anything is computed.

## Registered trials

Status of every row at handover: **planned, not run**.

### H1, H2, M6

| Date | ID | Statement | Window | Status | Patterns seen since the previous row |
| --- | --- | --- | --- | --- | --- |
| 2026-10-23 | H1 | Long lots with peak ≥ 8%: trailing stop at least entry + `COST_RT` | Test only exits from 2026-11-06 | open | 5 (M1–M5) |
| 2026-10-23 | H2 | Partial stop dies at `partial_sell_guard` → full close | Exits from 2026-11-06 / bug check | open | 0 |
| 2026-10-26 | M6 | Observation: 0 fills, both tenants, 23–26 Oct | Discovery window | observation | 1 (M6); H2 cases 2→4 coins |

### Main block (34) plus hard stress (3) plus group R (13)

Parameters are EMA length / slope window on the named 4h series. Delay 0 and base slippage unless the id says otherwise. From s2, a row that says slippage 0.3% or 0.5% means the s2 stress or hard-stress definition, not a flat add-on.

| Trial | Group | Parameter |
| --- | --- | --- |
| B0 | Baseline | All first entries, no filter. Fee 0.2%, slippage 0.1% until s2; after s2, base class slippage |
| E1-L38-S5 | E1 | Coin 4h EMA 38, slope 5 |
| E1-L38-S6 | E1 | Coin 4h EMA 38, slope 6 |
| E1-L38-S8 | E1 | Coin 4h EMA 38, slope 8 |
| E1-L50-S5 | E1 | Coin 4h EMA 50, slope 5 |
| E1-L50-S6 | E1 | Coin 4h EMA 50, slope 6 |
| E1-L50-S8 | E1 | Coin 4h EMA 50, slope 8 |
| E1-L63-S5 | E1 | Coin 4h EMA 63, slope 5 |
| E1-L63-S6 | E1 | Coin 4h EMA 63, slope 6 |
| E1-L63-S8 | E1 | Coin 4h EMA 63, slope 8 |
| E1-L50-S6-D1 | E1 | EMA 50, slope 6, 1-bar delay, slippage 0.1% / s2 base |
| E1-L50-S6-SL03 | E1 | EMA 50, slope 6, delay 0, stress |
| E1-L50-S6-SL05 | E1 | EMA 50, slope 6, hard stress |
| E2-L38-S5 | E2 | BTC_USDT 4h EMA 38, slope 5 |
| E2-L38-S6 | E2 | BTC_USDT 4h EMA 38, slope 6 |
| E2-L38-S8 | E2 | BTC_USDT 4h EMA 38, slope 8 |
| E2-L50-S5 | E2 | BTC_USDT 4h EMA 50, slope 5 |
| E2-L50-S6 | E2 | BTC_USDT 4h EMA 50, slope 6 |
| E2-L50-S8 | E2 | BTC_USDT 4h EMA 50, slope 8 |
| E2-L63-S5 | E2 | BTC_USDT 4h EMA 63, slope 5 |
| E2-L63-S6 | E2 | BTC_USDT 4h EMA 63, slope 6 |
| E2-L63-S8 | E2 | BTC_USDT 4h EMA 63, slope 8 |
| E2-L50-S6-D1 | E2 | EMA 50, slope 6, 1-bar delay |
| E2-L50-S6-SL03 | E2 | EMA 50, slope 6, stress |
| E2-L50-S6-SL05 | E2 | EMA 50, slope 6, hard stress |
| E12-L38-S5 | E1+E2 | Both filters, EMA 38, slope 5 |
| E12-L38-S6 | E1+E2 | Both filters, EMA 38, slope 6 |
| E12-L38-S8 | E1+E2 | Both filters, EMA 38, slope 8 |
| E12-L50-S5 | E1+E2 | Both filters, EMA 50, slope 5 |
| E12-L50-S6 | E1+E2 | Both filters, EMA 50, slope 6 |
| E12-L50-S8 | E1+E2 | Both filters, EMA 50, slope 8 |
| E12-L63-S5 | E1+E2 | Both filters, EMA 63, slope 5 |
| E12-L63-S6 | E1+E2 | Both filters, EMA 63, slope 6 |
| E12-L63-S8 | E1+E2 | Both filters, EMA 63, slope 8 |
| E12-L50-S6-D1 | E1+E2 | EMA 50, slope 6, 1-bar delay |
| E12-L50-S6-SL03 | E1+E2 | EMA 50, slope 6, stress |
| E12-L50-S6-SL05 | E1+E2 | EMA 50, slope 6, hard stress |
| B0-R | Baseline R | Current logic, no filter |
| E1-R-L50-S6 | E1 R | 50/6 base |
| E1-R-L50-S6-D1 | E1 R | 50/6, 1-bar delay |
| E1-R-L50-S6-SL03 | E1 R | 50/6, stress |
| E1-R-L50-S6-SL05 | E1 R | 50/6, hard stress |
| E2-R-L50-S6 | E2 R | 50/6 base |
| E2-R-L50-S6-D1 | E2 R | 50/6, 1-bar delay |
| E2-R-L50-S6-SL03 | E2 R | 50/6, stress |
| E2-R-L50-S6-SL05 | E2 R | 50/6, hard stress |
| E12-R-L50-S6 | E1+E2 R | 50/6 base |
| E12-R-L50-S6-D1 | E1+E2 R | 50/6, 1-bar delay |
| E12-R-L50-S6-SL03 | E1+E2 R | 50/6, stress |
| E12-R-L50-S6-SL05 | E1+E2 R | 50/6, hard stress |

### E1-specific kills (protocol, not the shared module)

The shared module does not encode these. They apply only to E1:

- The share of MAE ≤ −10% over 7 days in "filter allows" is not at least one third lower than without the filter.
- Net EV per trade (72h) with the filter is not higher.
- DSR < 0.95 on the hold-out, or N is not shown in the ledger.
- The advantage disappears with a 1-bar delay or with slippage stress.
- Parameter cliff on the ±25% grid.
- Walk-forward advantage in fewer than 2 of 3 evaluable windows.
- The advantage comes from one coin, one week, or one tenant (leave-one-out).
- Hold-out through 2026-12-31 has < 100 effective entries per group → withdrawn.
- Invalid run (repeat): same-bar fill, an open 4h candle, costs missing, or a null-data run that shows a leak.

### E2-specific kills

- Median 72h net in AB is not at least 1 percentage point worse than not-AB.
- The MAE ≤ −10% share in AB is not higher.
- The net sum with the block is not better than without it.
- The same DSR, delay, stress, cliff, walk-forward, and leave-one-out kills as E1 (leave-one-out also includes one AB episode).
- Hold-out through 2026-12-31 has < 100 effective entries per group, or < 10 AB episodes → withdrawn.
- Invalid run: same as E1.

## Evidence JSON

`tools/hypothesis_protocol.py` `validate_payload` documents the required keys. A strategy pull request that claims a backtest puts one JSON object per trial in `docs/evidence/` and the `unit` job runs the module on every `*.json` in that directory.
