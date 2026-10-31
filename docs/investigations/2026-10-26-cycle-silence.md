# Cycle silence: no `source=auto` orders since 17 Oct (henry) and 20 Oct (default)

Date: 2026-10-26
Base: staging `930792a` (same tree as `e357df2a` / #704 except the CI-only commit #724).
Mode: read-only. No live calls, no config writes, no keys. Evidence is this tree plus the attached order export (`orders_union.json`, 1068 rows, tenants `henry` and `default`, 2026-09-23 through 2026-10-26) and the risk note `viktor-h2-check.md`.

Order timestamps in the export are naive Berlin local time, as in that note.

## Verdict

The cycle hard stop is not being executed for the open lots the websocket is already stopping. That is why there is no `source=auto` stop, and why a guard escalation that only watches the cycle never sees one.

The fill drought from the morning of 23 Oct is a second fact. It is not the same defect, and it is not a dead fill adapter. After 23 Oct 08:16 every later row in this export is a risk reject (`risk.approved` false) recorded before `adapter.execute`. Detail is in "No fills after the morning of 23 Oct" below.

`source=auto` is not a separate strategy. It is the leftover label in `SignalOrchestrator.execute_if_needed` when the decision's sources are not retagged to `x`, `cmc`, `lc`, `dca*`, `entry_sensor_15m`, or `grid` (`services/signal_orchestrator.py:141-158`). With `architecture.eval_queue_enabled: true` (`config.json:452`), the meta cycle does not call `process_coin`. It runs portfolio DCA and enqueues eval jobs (`aria_bot.py:929-966`). The only place a held lot is analyzed for a stop or a Bollinger exit is the eval worker: `process_eval_job` → `process_coin` → `DecisionEngine.evaluate` → `TechnicalRSIStrategy.analyze` (`services/eval_queue_runtime.py:184-213`, `services/signal_orchestrator.py:455-489`, `strategies/technical_rsi_bb.py:181-205`).

The orders that are still being written do not go through that call:

| Path that still writes rows | Where the hard stop is evaluated |
|---|---|
| `exit_ws` | `services/exit_realtime/shadow_eval.py:144-200`, direct call to `evaluate_long_hard_stop` |
| `entry_sensor_15m` | `services/eval_queue_runtime.py:118-121` returns before `analyze` when the symbol already has an open lot |
| `dca` (portfolio pass) | `services/signal_orchestrator.py:644-710` and `:773-792`. No call to `evaluate_long_hard_stop` |
| `gainer_relvol` | Separate entry path, not the position heartbeat |

So the export can show a live book, fresh DCA attempts, fresh entry-sensor rejects, and a websocket stop every few minutes, while the cycle stop stays at zero.

The three reviewer hypotheses, checked against this tree and the export:

| Hypothesis | Result |
|---|---|
| (a) Grid is selected (`strategies/registry.py:532-554`) and never evaluates the hard stop | Discarded for the committed allocator. Still a real hole if a tenant override sets `grid > momentum`. Runtime weights: **UNCLEAR**. |
| (b) The cycle does not analyze the held symbol | Confirmed as the shape of the failure. The path that would analyze the lot is not the path still writing orders. Why that job does not complete for these lots: leading mechanism below, historical queue depth **UNCLEAR**. |
| (c) Another signal wins the merge (`strategies/decision_engine.py:909-950`) and the stop is dropped | Discarded when the stop is actually a candidate. Priority 7 beats every other cycle sell except an oracle harvest, and harvest only exists for a lot that is up at least 12%. |

No staging commit landed on 17, 18, 19, 20, or 21 October. The start dates are not a deploy.

## What the export shows

1068 orders. `source=auto` is 107 rows, every one `filled`. There is no rejected `source=auto` row in the window, so nothing with that label reached `TradingService._execute_order_locked` (`services/trading_service.py:410-447` records the reject).

Last `source=auto` rows:

| Tenant | Created (Berlin) | Symbol | Signal | `exit_source` | Rationale prefix |
|---|---|---|---|---|---|
| henry | 2026-10-17 05:37:58 | AKT/USDT | SELL_FULL | `bb_upper` | `TA->HOLD \| BB->upper extension` |
| default | 2026-10-20 06:18:12 | WLD/USDT | SELL_FULL | `bb_upper` | `TA->HOLD \| BB->upper extension` |

`source=auto` in this window is a profit-exit stream, not a stop stream:

| `exit_source` on `source=auto` sells | n |
|---|---|
| `bb_upper` | 58 |
| `profit_max_lifetime` | 19 |
| `rsi_sell` | 9 |
| `tail_idle` | 2 |
| `partial_stop` | 2 (both henry Q/USDT, 2026-09-28, signal `SELL_PARTIAL_50`) |
| `exit_15m_weakness` | 1 |

`SELL_STOP_*` anywhere in the export: 132 rows, all `source=exit_ws`. Zero cycle hard stops after #663 (10 Oct), including on days when `bb_upper` fills and websocket stops were both happening (default ARC 13 Oct, USELESS 14–20 Oct, 2Z 15 Oct, HBAR 15–22 Oct, PONS 18 Oct).

Symbols the websocket is stopping have no cycle sell at all:

| Lot | Cycle rows in the export | Websocket |
|---|---|---|
| henry STRK/USDT | one `gainer_relvol` buy, 2026-10-17 21:57 | 79 rejected `SELL_STOP_PARTIAL` on 2026-10-26 |
| henry ULTIMA/USDT | none (entry is outside this export) | 17 rejected `SELL_STOP_PARTIAL` on 2026-10-26 |
| default USELESS, HBAR, PONS, ARC, 2Z | buys (`entry_sensor_15m`, `dca`, `gainer_relvol`) only | the only sells are `exit_ws` |

After the cutoff, henry still writes `dca` (ASTER fill 23 Oct; SUI/XRP/AAVE/AVAX/VVV rejects on 26 Oct), `gainer_relvol`, `entry_sensor_15m`, and `exit_ws`. Default still writes `entry_sensor_15m` through 26 Oct and one `exit_ws` fill on 23 Oct (DOS). Default `dca` is a burst of 112 `dca_guard_below_avg` rejects from 19 Oct 22:08 through 20 Oct 15:55, then nothing. HBAR got three of those DCA rejects on 19 Oct between 22:37 and 23:55, in the same hour the websocket was rejecting HBAR partials. A per-coin `process_coin` that had produced a hard-stop candidate would have sold, not bought: DCA add-on runs only when the sell merge returned HOLD (`strategies/decision_engine.py:1621-1679`). Those buys match `run_portfolio_dca_pass`, which never asks the hard stop.

The string `TA->` on the last auto fills is written only when `"technical"` is in the decision sources (`strategies/decision_engine.py:1868-1869`). `GridStrategy` puts `"grid"` in sources, not `"technical"` (`strategies/grid.py:409`). The last auto fills were not grid decisions. There is no `source=grid` row in the 1068.

## Confirmed: a generated cycle stop is not thrown away

`evaluate_long_hard_stop` (`strategies/dca.py:461-497`) returns `(SELL_STOP_FULL|SELL_STOP_PARTIAL, "stop_loss")` or `None`. The cycle calls it in exactly one strategy: `TechnicalRSIStrategy.analyze`, and only when `market.has_position` and `average_entry > 0` (`strategies/technical_rsi_bb.py:181-205`). `GridStrategy.analyze` (`strategies/grid.py:149-233`) does not call it. Portfolio DCA does not call it. The entry-15m job does not call it for an open lot.

When the technical strategy does append `"stop_loss"`, `_merge_sell` forces priority 7 and labels a partial as `partial_stop` (`strategies/decision_engine.py:909-950`). Other cycle candidates top out at 6 (trailing, lifetime, rotation). Oracle harvest is priority 8 (`strategies/oracle_climax.py:37-38`) and is only added when gain is at least `harvest_min_gain_pct` (12, `config.json:818`). STRK and ULTIMA are losers. Harvest does not outrank them.

These filters do not remove `stop_loss` or `partial_stop`:

- Grind blocks `bb_upper`, `trailing_take_profit`, `trailing_stop`, `profit_max_lifetime`, `time_profit_exit`. It does not block stops (`strategies/oracle_climax.py:25-35`).
- Trail-exclusive blocks structure and social sources, not stop sources (`strategies/sell_sources.py:33-36`, `strategies/sell_rotation_policy.py:336-374`).
- #713 shadow holds a technical or shadow-owned sell. `stop_loss`, `partial_stop`, and `SELL_STOP_FULL` are in `_UNSUPPRESSIBLE_STOP_LABELS` (`strategies/decision_engine.py:93-98` and `:1347-1389`). A partial is protected by its source even though the action string `SELL_STOP_PARTIAL` is not itself in that set.
- Sell-policy shadow is off. `sell_policy.mode` is `active` (`config.json:738-739`), and `policy_shadow_active` is true only for `shadow` (`strategies/sell_rotation_policy.py:112-117`).

`analysis.long_hard_stop` is set only when the technical result carried the flag and the winning source is `stop_loss` or `partial_stop` (`strategies/decision_engine.py:1947-1952`). The orchestrator then keeps the signal as `SELL_STOP_*` and forces `exit_source=stop_loss` (`services/signal_orchestrator.py:221-250`). An amount of 0 returns before the ledger (`services/signal_orchestrator.py:231-238`). The default fraction for `SELL_STOP_PARTIAL` is 0.5 (`strategies/positions.py:1841-1844`). A zero-amount skip is not a fit for a lot the websocket is sizing.

Conclusion: if `TechnicalRSIStrategy.analyze` ran on the STRK or ULTIMA lot with the same inputs the websocket used, the export would contain a `source=auto` stop or a `source=auto` reject. It contains neither, on 26 Oct, after #704. The call is not happening for those lots.

## Hypothesis (a): grid selection

`get_strategy` switches to `GridStrategy` only when `allocation.strategy_weights.grid` is strictly greater than `momentum` (`strategies/registry.py:541-544`). It never switches the other way. A `strategies[]` row with `strategy_class: "grid"` would also stick.

Committed allocator (`intelligence/strategy_allocator.py:50-51`, `config.json:1983-1990`):

- `default_grid_weight` 0.4, `default_momentum_weight` 0.6. The 2026-08-10 note on that block says the point of 0.4 was to stop RANGING stable coins landing in pure GRID.
- RANGING + neutral sentiment uses those defaults (`intelligence/strategy_allocator.py:76-80`). 0.4 is not greater than 0.6.
- RANGING with a sentiment bias is 0.45 / 0.55.
- STRONG_UPTREND, STRONG_DOWNTREND, CHOPPY_HIGH_VOL, and the defensive early-return all leave momentum greater than or equal to grid.
- TRANSITION is 0.40 / 0.40, which is not strictly greater.
- Volatile tier multiplies grid by 0.8 (`intelligence/strategy_allocator.py:115-118`), which makes grid smaller.

`scripts/patch_henry_grid_experiment_v1.py:26-32` writes the same 0.4 / 0.6 pair. Whether that script was applied, and whether a later tenant edit put grid back above momentum: **UNCLEAR**. The MCP config was not read.

`resolve_trading_mode` can still return `GRID` or `HYBRID` for a stable coin at these weights (`strategies/trading_modes.py:87-95`). That mode is used inside `GridStrategy` after the class has already been chosen. It does not, by itself, replace `TechnicalRSIStrategy`. And `TechnicalRSIStrategy.analyze` does not consult the mode before the hard stop.

If a runtime override did select grid, two things would follow. The hard stop would not run. Any non-HOLD sell that still came out of `_merge_sell` (Bollinger, trail) would be tagged `source=grid`, because `"grid"` is copied from the technical sources and the orchestrator checks `grid` before the `auto` fallback (`services/signal_orchestrator.py:155-158`). `dca` is checked first, so a grid-selected cycle can still write `source=dca`. Zero `source=grid` rows means grid never produced an executed order. It does not prove grid never returned HOLD. For the lots that did execute on 17 and 20 Oct, the `TA->` rationale rules grid out.

## Hypothesis (b): the held lot is not analyzed

This is the failure that matches the rows.

`aria_bot.py:936-952` with the queue enabled does not loop `process_coin`. It calls `seed_meta_producers` and then reads whatever the worker has already finished. Heartbeats are enqueued from the open-lot list at priority 30, every `eval_position_heartbeat_sec` (300) unless that member was marked processed more recently (`services/eval_queue_runtime.py:304-323`, `config.json:456`).

Two defects in that queue keep a held lot off `process_coin` while entry-sensor rows continue.

1. One Redis member per tenant, symbol, and timeframe (`bus/eval_queue.py:108-117` and `:156`). `ZADD` replaces the score and the meta reason (`bus/eval_queue.py:170-194`). An `entry_15m` or `15m_watch` enqueue for a symbol that already has a pending `position_heartbeat` deletes the heartbeat. The worker then takes the entry reason, sees an open lot, and returns HOLD without `analyze` (`services/eval_queue_runtime.py:118-121`). It still stamps `mark_eval_processed` (`services/eval_queue_runtime.py:253-258`), so the next heartbeat is suppressed for 300 seconds (`services/eval_queue_runtime.py:313-315`).

2. After every enqueue, `ZREMRANGEBYRANK key eval_queue_max_len -1` drops the highest scores (`bus/eval_queue.py:177-181`, cap 500 at `config.json:459`). Scores are `priority * 10^16 + timestamp` (`bus/eval_queue.py:97-99`). Lower priority number is popped first (`bus/eval_queue.py:13-19`: entry 15, social 25, heartbeat 30, stale 50). The trim therefore deletes stale jobs first, then heartbeats, and keeps entry jobs. A queue that is already full of entry and social members deletes a heartbeat in the same pipeline that inserted it.

Either defect is sufficient to produce this split: `entry_sensor_15m` rows keep appearing, `source=auto` does not, portfolio DCA (outside the queue) keeps appearing, and the websocket (outside the queue) keeps stopping. STRK was opened by `gainer_relvol`, not by the cycle. It is still an open lot, so the seeder should enqueue it. No cycle row exists for it on 26 Oct. The job is not reaching `execute_order`.

What is **UNCLEAR**: Redis depth on 17–20 Oct, the size of the 15m watch set, and whether STRK's heartbeat was trimmed or overwritten. Logs were not available. The code defect does not need those facts to be real. The dates do, before this is called the proven trigger rather than the mechanism.

`build_market_context` does rebind a lot that sits on a different timeframe than the watchlist coin (`strategies/decision_engine.py:530-541`). A timeframe miss is not a good explanation once that fallback is in the path the worker actually runs. It matters only when `process_coin` runs.

## Hypothesis (c): merge

Discarded for a real stop candidate. See the priority and filter list above. A later RSI or trail winner would have to outrank priority 7. Nothing in `_merge_sell` does, except harvest on a winner. The `long_hard_stop` flag is cleared when the winner is not the stop (`strategies/decision_engine.py:1947-1952`). That is a real footgun for a partial that lost, and it would still have written an order for the winner. The export does not have that order either.

## What did not start this on 17 and 20 Oct

`git log` on staging from 10 Oct through 26 Oct:

| Commit | Date | What it is |
|---|---|---|
| `6318c29` | 2026-10-10 | #663, websocket hard stop. First build that can emit the `exit_ws` rows in the export. |
| `96987b5` | 2026-10-16 11:32 +0200 | #713. Shadow no longer turns every cycle sell into HOLD. Stops stay executable. Technical sells under a shadow tag stay HOLD. |
| (none) | 17–21 Oct | No merge. |
| `e357df2a` | 2026-10-22 23:06 +0200 | #704. Position-store key moves from a process global to a `ContextVar`. |
| `930792a` | 2026-10-23 | #724, CI and a test timezone leak only. |

#713 cannot be the switch that turned `source=auto` off. Default still filled `bb_upper` on AKT (17 Oct 05:33), ACU (17 Oct 12:32), and WLD (20 Oct 06:18), all after the merge. `bb_upper` is not a technical sell source and not a shadow-owned source, so `_apply_shadow_mode` does not hold it (`strategies/decision_engine.py:1363-1375`). Stops are explicitly exempt. The bug #713 fixed (a shadow tag holding the hard stop) is the opposite of the current hole: stops are allowed, and they are still absent because they are not produced.

#704 cannot be the start. Both tenants had already gone quiet. The silence is unchanged on `e357df2a`, which is the production floor given for this check. The ContextVar change does not put `evaluate_long_hard_stop` on the portfolio-DCA path or the entry-15m early return, and it does not change queue trim.

Interaction that is real but not this incident's start: before #704, `_active_key` was process-global (`strategies/positions.py` on `e357df2a^`, as the risk note describes). A henry cycle could retarget the book a default thread was reading. That can mis-file `hard_partial_stop_done` and `sold_percent`. It is a plausible account of the pre-22 Oct re-fires after a filled partial (USELESS after 20 Oct 18:21, HBAR after 15 Oct 11:55). **UNCLEAR** without the lot documents. It does not explain STRK and ULTIMA on 26 Oct: those partials never filled, and the flag is set only on a filled `SELL_STOP_PARTIAL` with `exit_source=stop_loss` (`strategies/positions.py:2226-2231`). A rejected websocket partial leaves the flag false, so the websocket tries again. That retry loop is expected. After the 22 Oct 23:06 merge the export has no new post-fill re-fire (HBAR's last attempt is 22 Oct 17:59, USELESS's last is 22 Oct 13:54, both before the merge).

## Oracle grind, RISK_OFF, and the profit-exit half

`sell_policy.oracle_climax.enabled` is true (`config.json:807-820`). Grind drops exactly the sources that dominate historical `source=auto`: `bb_upper`, lifetime, trailing (`strategies/oracle_climax.py:25-35`). It has been in the tree since August, and `bb_upper` fills continued through 20 Oct, so grind is not permanently on.

Fusion `RISK_OFF` or `CRASH` forces the overlay idle (`strategies/oracle_climax.py:92-94` and `:159-160`). The entry-sensor rejects in the export already show `regime=RISK_OFF, cash=HARVEST` on 20 Oct and again on 26 Oct (for example the `max_open_positions` text). During those windows grind is not blocking `bb_upper`. The auto stream is still empty. Grind can hide profit exits while oracle is `RISK_ON` and fusion is not `RISK_OFF`. It cannot hide a hard stop, and it cannot by itself explain a silence that runs through `RISK_OFF`.

Healthy gaps between `source=auto` fills were already long before the cutoff (default: 15 Oct 21:52 to 17 Oct 05:33, then to 20 Oct 06:18; 16 Oct had zero auto rows for both tenants). A stretch with no upper-band extension is normal. A stretch with no cycle stop, while the websocket is hitting the shared function on STRK and ULTIMA, is not.

Repo thresholds, for the record. `stop_loss_pct` 50 (`config.json:117`), `partial_stop_ratio` 0.67 (`config.json:623` and the dry-run copy at `:80`). Stable partial is 33.5%. Volatile `partial_stop_pct` is 25 (`config.json:1480`, under `volatile_altcoin`). The risk note's HBAR print on 19 Oct was about −21% and still produced a websocket partial. That runtime threshold is not the 33.5% figure. Which tier and which override were live: **UNCLEAR**. It does not split the cycle from the websocket: the hub builds those params with the same `resolve_strategy_params` the cycle uses (`services/exit_realtime/hub.py:861-868` and `:1226-1235`). If the websocket hits, a cycle call on that lot hits too.

## Exits that are missing because of this

Missing, and currently replaced by nothing on the cycle:

- `SELL_STOP_PARTIAL` and `SELL_STOP_FULL` with `source=auto` and `exit_source=stop_loss`, for every open lot past the partial or full threshold. STRK (−30%, about $195) and ULTIMA (−35%, about $276) are the ones the websocket is rejecting as below the partial minimum. The cycle never submits them, so there is nothing for a cycle-side guard to escalate into one full close.
- The same missing cycle stop on the earlier websocket names (default USELESS, HBAR, PONS, ARC, 2Z). Their only stop attempts are `exit_ws`.

Still present, and not a substitute:

- Websocket `SELL_STOP_PARTIAL`. For a remainder under `min_position_usdt_for_partial_sell` (repo 500, henry's live text says 300; which override key is **UNCLEAR**) the guard rejects and does not upgrade to a full close. `exit_ws` is outside the #723 escalation condition. The position stays open and the reject repeats.
- Portfolio DCA buys on underwater names (rejected by `dca_guard_below_avg`, but the attempt is the wrong direction).
- Entry-sensor and gainer entries.

Also absent after the cutoff, same `process_coin` path, not proven to have had a setup:

- `bb_upper`, `profit_max_lifetime`, `rsi_sell`, `trailing_stop`, `exit_15m_weakness` as `source=auto`. Treat the absence as a consequence of the position job not running. Do not treat a single quiet day of `bb_upper` as proof. 16 Oct was already a quiet day while the path was alive.

Not missing from the cycle for a filter reason, if the job had run: a hard stop under grind, under trail-exclusive, under #713 shadow, or under an active sell-policy. Those keep the stop.

## Proposed fix

Two code changes. Both are required. A stop inside `TechnicalRSIStrategy` only does not help a lot whose heartbeat never runs. A queue fix only does not help a lot that is evaluated by `GridStrategy`.

1. Evaluate the hard stop in `DecisionEngine._merge_sell` for every open lot with a positive entry, by calling `evaluate_long_hard_stop` there and appending `(action, 7, "stop_loss"|"partial_stop")`. Do not wait for `technical.sources` to contain `"stop_loss"`. Grid, a HOLD from indicators, and a missing strategy class then cannot skip it. Keep the existing priority, the #713 exemption, and the `long_hard_stop` flag so the orchestrator still emits `SELL_STOP_*` / `exit_source=stop_loss`.

2. Stop dropping position evals.
   - In `enqueue_eval`, do not `ZREMRANGEBYRANK` a member whose priority is `PRIORITY_POSITION_HEARTBEAT` or better. Trim `PRIORITY_STALE` and worse only. If the cap must still apply to heartbeats, give them a separate key so entry jobs cannot evict them.
   - Do not let a higher-priority reason replace a pending `position_heartbeat` for the same member. Coalesce, or keep the heartbeat and run the entry check after `process_coin`.
   - In the worker, do not call `mark_eval_processed` when `_process_entry_15m_job` returned without analyzing because the lot is open. That stamp is what defers the next heartbeat.

3. Watchdog, below. It does not close the position. It tells us the position path has gone quiet before the next STRK loop.

Out of scope for this fix, already described by the risk note: teaching the guard to turn a below-minimum hard partial into one full close, on the websocket as well as the cycle. This investigation's hole is that the cycle never submits the partial. Closing that hole still leaves the websocket reject loop until the guard change exists. Do not pretend the cycle fix covers `exit_ws`.

### Reproduction and acceptance tests

These go through the worker and the orchestrator. They must not call `evaluate_long_hard_stop` on its own and assert on that tuple. Mock the ticker, the OHLCV read, and the exchange submit. Do not mock `get_strategy`, `_merge_sell`, `TechnicalRSIStrategy.analyze`, or `process_coin`.

Drive `services.eval_queue_runtime.process_eval_job` with an `EvalJob` of `reason="position_heartbeat"`, `priority=30`, inside `tenant_cycle_context` for the tenant under test. The worker already calls `orchestrator.begin_tenant_cycle` and `process_coin` on that reason (`services/eval_queue_runtime.py:184-213`). Spy on `TradingService.execute_order` (or the ledger) for the order that comes out.

Shared fixture: one open lot, entry 1.0, last price 0.65 (loss 35%), `stop_loss_pct` 50, `partial_stop_ratio` 0.67 so the partial threshold is 33.5% and the full threshold is 50%. `hard_partial_stop_done` false, `dca_rounds` 0, no lock, `recovery_hold` false. Amount large enough that 50% is above the partial minimum, so the guard is not what the test is measuring. A second case uses amount such that 50% is under the minimum, and asserts the order is still submitted (the guard's reaction is a separate test).

| Test | Setup | Expect |
|---|---|---|
| A1 | Committed allocator weights 0.4 / 0.6. Job reason `position_heartbeat`. | `execute_order` once. `source=auto`, signal `SELL_STOP_PARTIAL`, `exit_source=stop_loss`, amount = half the lot. This pins the technical path. It should pass on today's tree if the job is actually processed. |
| A2 | Same lot. Allocator `default_grid_weight` 0.7 and `default_momentum_weight` 0.3, regime forced to RANGING so `get_strategy` selects `GridStrategy` for real. | Same order as A1. Fails today: grid never calls the stop, and a HOLD from grid writes no row. |
| A3 | Loss 20% with the repo 33.5% partial. | No stop order. Guards the threshold. |
| A4 | Loss 55%. | One `SELL_STOP_FULL`, amount = the whole lot, not a partial. |
| A5 | `enqueue_eval` of a `position_heartbeat`, then 500 `entry_15m` jobs on other symbols, cap 500. | The heartbeat member is still in the zset and `pop_eval_batch` returns it before it returns a stale job. Fails today: `ZREMRANGEBYRANK` drops it. |
| A6 | Pending heartbeat for STRK. Then `enqueue_eval` of `entry_15m` for the same tenant, symbol, and timeframe. | `process_eval_job` still runs `process_coin` on that lot (the stop from A1), and an entry early-return does not stamp `last_processed_at`. Fails today: the member reason is replaced and the open-lot branch returns HOLD. |
| A7 | Shadow tag present (`trailing_shadow` or a missing volatile mode). Stop candidate as in A2. | The stop is still submitted. Pins #713 so the new merge-site call is not re-held. |
| A8 | `recovery_hold` true. | Full stop still submitted. Partial behaviour stays whatever `filter_sell_candidates_for_recovery_hold` already does. Do not change that in this fix. Document it in the test name. |
| A9 | Tenant henry's job must not read or write tenant default's book. One open lot on each, same symbol, different entry. | The order's tenant and the entry used in the rationale match the job tenant. Pins #704. |

A1 passing and A2 failing is the current tree. A production incident is "A1's job never ran", which A5 and A6 reproduce without a broker.

### Watchdog

Per tenant, alert when `N` consecutive meta cycles have completed with no new order whose `source` is `auto`.

Where it lives. New `services/cycle_silence_watchdog.py`, function `note_tenant_cycle(tenant_id)`. Call it from the per-tenant meta cycle in `aria_bot.py`, immediately after the existing "Cycle completed" log (`aria_bot.py:973-980`), while that tenant's context is still active. That is one call per cycle per tenant, on the loop that already runs when the eval worker is wedged. Do not hook the eval worker. A dead worker is the condition we want to see. Do not hook `process_coin`. A cycle that never reaches it would never arm the counter.

What it counts. Read the order ledger for that tenant, the same store `record_rejected` and fills write. Remember the newest `source=auto` `created` timestamp. If it is unchanged since the previous `note_tenant_cycle` for that tenant, increment. If a newer auto row exists, reset to 0. Another tenant's auto row does not reset this one. Persist the counter and the watermark in Redis under the existing architecture prefix, keys `aria:watchdog:auto_silent:{tenant}:count` and `:watermark`. An in-memory counter resets on deploy and hides a silence that started before the restart.

`N` and the switch. `observability.cycle_watchdog` next to `observability.cycle_notifications` (that block already owns cycle Telegram, `services/cycle_notification_policy.py:12-34`):

```json
"cycle_watchdog": {
  "enabled": true,
  "auto_silent_cycles": 2160,
  "repeat_every_cycles": 2160
}
```

`update_interval` is 120 seconds (`config.json:831`). 2160 cycles is 72 hours. That is the smallest default that does not page through the gaps this export already shows on healthy days (16 Oct had zero auto rows for both tenants; default's gap from 17 Oct 12:32 to 20 Oct 06:18 is about 66 hours). Override per tenant in the tenant body if henry should be tighter. Alert on the cycle where the counter first reaches `N`, then every `repeat_every_cycles`, not every cycle. Send with `telegram_notifier.send_telegram_message` to that tenant's chat. Text should name the tenant, `N`, the watermark timestamp, and the last auto symbol if the ledger has one. No order placement, no config write.

False alarm. `source=auto` is mostly `bb_upper`. A book that is only underwater will sit at zero auto orders while the new hard stop is working and writing `source=auto` stops. Once fix 1 is in, a real stop resets the watchdog, which is what we want. Until then, a 72 hour page means "the position path has not submitted anything", which is this incident, not "a stop was due and skipped". A second counter is worth adding in the same function and the same test file: cycles in which the tenant held at least one lot whose loss exceeded the partial threshold and no `source=auto` stop was written. That one can use a small `N` (suggest 3, about 6 minutes at 120 seconds) because it is conditioned on a lot the websocket would already be stopping. Threshold math must be `effective_stop_loss_thresholds`, not a hardcoded 20%.

Test. Call `note_tenant_cycle` the way `aria_bot` calls it. Do not unit-test a bare integer helper and leave the wiring unchecked. One test module:

| Test | What it drives | Expect |
|---|---|---|
| W1 | `note_tenant_cycle("henry")` N times with a ledger whose newest auto row is older than the first call. `auto_silent_cycles` set to 3 for the test. | One Telegram send, on the third call, body contains `henry`. The fourth call does not send again until `repeat_every_cycles`. |
| W2 | Same, but the ledger gains a `source=auto` row for henry before the third call. | Counter resets. No send. |
| W3 | Default writes `source=auto` every cycle. Henry writes none. | Henry alerts. Default does not. |
| W4 | Henry writes `exit_ws` and `dca` every cycle, no `auto`. | Henry still alerts. Those sources must not count. |
| W5 | `enabled: false`. | No send, no Redis write. |
| W6 | Restart: new process, Redis counter already at `N - 1`, ledger watermark unchanged. | The next `note_tenant_cycle` sends. Proves the counter is not process-local. |

W1 through W4 should use the real `note_tenant_cycle` and a ledger fixture. Patch `send_telegram_message` only. Do not patch the counter.

## No fills after the morning of 23 Oct

Risk review: from 23 Oct both tenants show orders and no fills. The tape supports a sharper version of that. There are two fills on 23 Oct itself, then none.

| When (Berlin) | Tenant | Source | What filled |
|---|---|---|---|
| 2026-10-23 00:38:18 | henry | `dca` | BUY_DCA ASTER/USDT, risk message `Approved`, fill price 0.6936 |
| 2026-10-23 08:15:52 | default | `exit_ws` | SELL_FULL DOS/USDT, `exit_source=trailing_take_profit`, risk `Sell approved`, request price 0.2306 |

From 23 Oct 08:16 through 26 Oct 09:47 the export has 321 further orders. All 321 are `rejected` and `risk.approved` is false. There is no `ACTIVE`, pending, or approved-but-unfilled row anywhere in the 1068. `TradingService._execute_order_locked` calls `adapter.execute` only after approval (`services/trading_service.py:444-497`). A reject is written at `:447` and returns at `:450`. These 321 rows never reached the paper or venue fill.

`architecture.trading_engine_mode` is `in_process` (`config.json:442`). The Redis trading-engine worker in `workers/trading_engine.py` runs only when that mode is `external` or `distributed` (`workers/trading_engine.py:27-28`). It is not the process that fills these rows. "Worker" below means the background producers that stamp their own `source`: `gainer_relvol`, `entry_sensor_15m`, `dca`, `dca_sniper`.

### Last order and last fill

`auto` is the cycle label. `exit_ws` is the websocket hub. `worker` is the four background sources above. `cmc`, `lc`, and `manual` are already quiet before 23 Oct (last fills 6 Oct, 15 Oct, and 12 Oct on henry; default manual is 30 Sep). They are not part of this drought.

| Tenant | Bucket | Last order | Last fill |
|---|---|---|---|
| default | `auto` | 2026-10-20 06:18 WLD/USDT SELL_FULL `bb_upper`, filled | same row. No later auto order |
| default | `exit_ws` | 2026-10-23 08:15 DOS/USDT SELL_FULL `trailing_take_profit`, filled | same row. No later `exit_ws` row |
| default | `worker` | 2026-10-26 09:14 entry_sensor BUY 龙虾/USDT, `max_open_positions` | 2026-10-21 02:29 entry_sensor BUY DOS/USDT |
| henry | `auto` | 2026-10-17 05:37 AKT/USDT SELL_FULL `bb_upper`, filled | same row. No later auto order |
| henry | `exit_ws` | 2026-10-26 09:47 STRK/USDT SELL_STOP_PARTIAL, `partial_sell_guard` | 2026-10-22 15:18 ONDO/USDT SELL_FULL `trailing_stop` |
| henry | `worker` | 2026-10-26 09:43 gainer NFLXON/USDT, `long_mcap` | 2026-10-23 00:38 dca BUY_DCA ASTER/USDT |

Per worker source:

| Tenant | Source | Last order | Last fill |
|---|---|---|---|
| default | `gainer_relvol` | 2026-10-13 20:56 QNT/USDT, filled | same row. No later gainer order |
| default | `entry_sensor_15m` | 2026-10-26 09:14, `max_open_positions` | 2026-10-21 02:29 DOS/USDT BUY |
| default | `dca` | 2026-10-20 15:55 GRAM/USDT, `dca_guard_below_avg` | 2026-10-06 04:44 DEXE/USDT |
| default | `dca_sniper` | 2026-09-30 21:28 Q/USDT, filled | same row |
| henry | `gainer_relvol` | 2026-10-26 09:43 NFLXON/USDT, `long_mcap` | 2026-10-19 01:06 ONDO/USDT |
| henry | `entry_sensor_15m` | 2026-10-26 09:11 CT/USDT, `cash_floor` | 2026-10-16 17:13 AKT/USDT |
| henry | `dca` | 2026-10-26 09:37 VVV/USDT, `dca_guard_below_avg` | 2026-10-23 00:38 ASTER/USDT |
| henry | `dca_sniper` | no rows | no rows |

### This is not the cycle-silence defect

Cycle silence removes `source=auto` only. It started on 17 Oct (henry) and 20 Oct (default). Fills of other sources continued after those dates: default entry-sensor fills on 20 and 21 Oct, default `exit_ws` fills on 20, 21, 22, and 23 Oct, henry `exit_ws` ONDO on 22 Oct, henry DCA ASTER on 23 Oct. The position-heartbeat hole does not run the gainer, the entry sensor, portfolio DCA, or the websocket, and it does not sit in front of `adapter.execute`.

What the 321 rows actually are:

| Code | n since 23 Oct 00:00 | Who | What the guard is doing |
|---|---|---|---|
| `cash_floor` | 205, all henry, first row in the whole export is 2026-10-23 02:56 | gainer 201, entry sensor 4 | HARVEST, free $0.00 for new buys, floor about $16k, cash $7826.48 |
| `partial_sell_guard` | 96, all henry `exit_ws` | STRK 79, ULTIMA 17 | Below-min hard partial. Same loop as the original note |
| `long_mcap` | 79 henry gainer, plus 3 default entry sensor | new buys | Universe / cap gate, not a fill |
| `max_open_positions` | 38 default entry sensor | book 50/22 or 51/22 under RISK_OFF HARVEST | Entry cap. Started 20 Oct, and default still filled after that |
| `universe_trade_cap` | 32 default, 1 henry | entry sensor | Observe-only names. Started 20 Oct |
| `dca_guard_below_avg` | 29 henry | DCA buys under the average | The 23 Oct ASTER fill was approved; later DCA prices are under the average |
| other henry gainer | 11 | `stablecoin_blocked`, `coin_memory_soft_block`, `gainer_chase_guard` | Entry gates |

`cash_floor` is the adaptive policy, not a new branch added on 23 Oct. `risk.cash_policy.enabled` is true and `mode` is `adaptive` (`config.json:493-495`). HARVEST is chosen when fusion `block_buys` is set or `size_mult` is below `size_mult_harvest` (0.7) (`risk/cash_policy.py:57-68`). The buy check is `_cash_floor_blocked` (`risk/risk_manager.py:2985-3018`). It does not run on sells. A hard stop or a trailing full close is not rejected by this code.

Ticker price is present on every one of the 321. The DOS fill the same morning used a request price of 0.2306 and a trail rationale with a peak timestamp of 07:52 that day. The STRK guard text still carries `last=` and `bid=`. There is no zero-price cluster and no new "no ticker" reject code.

#704 merged 22 Oct 23:06. Fills continued after that merge: default USELESS `exit_ws` at 22 Oct 23:21, henry ASTER at 23 Oct 00:38, default DOS at 23 Oct 08:15. The adapter was alive on the morning the drought starts. No staging commit on 23 Oct changes execution (`930792a` is CI only).

### What is actually missing, by path

- `auto`: no order, so no fill. That is the cycle silence. It does not explain the other buckets.
- `exit_ws` default: the last row filled. Nothing was submitted after DOS. Whether a later default lot should have trailed or stopped is **UNCLEAR** without the book after 08:15. The path did not fail a fill.
- `exit_ws` henry: orders keep coming and the guard rejects them. The fill path is not what stops STRK and ULTIMA. The partial-minimum guard is. A full close is still not submitted.
- `worker` buys: submitted and rejected by cash floor, open-slot cap, universe cap, mcap, or DCA-below-average. Those guards are behaving as written. Henry's cash-floor outcome is new in this export at 02:56 on 23 Oct (no earlier `cash_floor` row). Whether fusion actually flipped henry to HARVEST then, or a tenant cash read changed after #704, is **UNCLEAR**. The message quotes henry cash $7826 against a ~$16k floor, so the block is consistent with that cash figure. It does not block the sells that are missing.

A paper-fill or venue-fill failure would show an approved order that does not reach `filled` (status `ACTIVE`, or `executed` false after `adapter.execute`). This export has none of those in the drought window. Fixing the cycle heartbeat will not make a `cash_floor` or `max_open_positions` buy fill, and it will not turn a below-min `exit_ws` partial into a fill. Those stay rejects until the guard rules or the cash and slot state change.

## UNCLEAR

- Redis eval-queue depth, 15m watch size, and whether STRK's heartbeat was trimmed or overwritten on 17–26 Oct. The mechanism is in the code. The historical saturation is not in the export.
- Tenant runtime `strategy_allocator` weights, `strategy_class`, and the henry key that makes the guard say $300 instead of the repo's $500 `min_position_usdt_for_partial_sell`.
- Runtime `stop_loss_pct` / tier for default HBAR on 19 Oct (websocket partial near −21%, repo stable partial 33.5%).
- Whether `hard_partial_stop_done` was written to the wrong tenant's book before #704. No post-merge filled-partial re-fire is in this export.
- Production commit past `e357df2a`. `930792a` does not change this path.
- Logs. Not readable from the attached data.
- Whether any held lot had a real `bb_upper` setup after 20 Oct. The missing stop does not depend on that.
- Why henry's first `cash_floor` row in this export is 23 Oct 02:56 (HARVEST, free $0 new, cash $7826, floor about $16k). The policy is older than that morning. Whether fusion `size_mult` / `block_buys` flipped then, or the #704 book split changed which cash figure the guard saw, is not in the tape.
- Whether any default lot after the DOS trail fill at 23 Oct 08:15 should have produced another `exit_ws` row. The hub emitted nothing after that fill. The adapter had just filled DOS.
