# Evidence: observe streams (#630, #625, #633, #687, #688)

Observe only. The order decision is unchanged. `fire_enabled` stays off and is not applied.

Measured on this branch against staging, before the code change and after the new tests.

| metric | before | after |
| --- | --- | --- |
| shadow streams readable from Mongo | 0 | 7 |
| gainer desk invariant violations on a fixture | 1 | 0 |
| macro observe lines on a test cycle | 0 | 1 |

Shadow streams after: `obs_buy_decision_tape`, `obs_daily_gainer_desk`, `obs_cap_order_observe`, `obs_shadow_signal`, `obs_shadow_exit`, `obs_shadow_link`, `obs_shadow_control`. Before, none of these collections were written. JSONL files are still appended.

Gainer desk fixture: five rows, and the third append raises. Before, two lines landed in `daily_gainer_desk.jsonl`, the call returned 0, and the log was `daily_gainer_desk emit failed` with no bucket tally (bucket sum 0, rows written 2). After, the tally is `n=2 hit=0 late=0 missed=2 rejected=0 sum=2`, which matches the two lines. `hit + late + missed + rejected == n == sum`.

The 4-week scrape (`shadow630_gainer_desk_daily.csv`) has 2 days where the columns do not add up, both by 1099 rejected: 2026-10-25 (rows 660, rejected 1756) and 2026-10-28 (rows 828, rejected 1918). Totals: rows 29370, hit 408, late 26, missed 155, rejected 30979. A tally line with that shape fails `tally_is_consistent`. The emitter no longer publishes one.

Macro observe: an empty config used to return no record, so a first-buy cycle logged 0 lines. The same cycle now logs 1 line (`observe_enabled=True`, `fire_enabled=False`, `would_block=True` under RISK_OFF). The order fields (`normalized_action`, `action`, `shadow_action`, `confidence`, `dca_usdt`, `sources`) match the cycle with observe forced off. A risk `evaluate` on the same buy matches approved, code, size multiplier, and order qty / usdt.

## Money path

`ci/money_paths.json` marks every file under `strategies/` as a money path. This change edits `strategies/daily_gainer_desk.py` (the tally) and `strategies/macro_stress_observe.py` (observe defaults on). It does not change order sizing, entries, exits, stops, locks, or limits. `owner-approved` was not added.
