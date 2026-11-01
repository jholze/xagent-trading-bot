# Observe streams in Mongo

Shadow and observe rows are still appended to JSONL under the log directory (`/app/logs` on the paper deploy). Each stream is also copied into its own Mongo collection so it can be queried without a shell on the volume. The copy is best-effort: a Mongo error, a slow insert, or a full queue does not change the JSONL line and does not block the trading cycle.

`shadow_link` rows carry `shadow_signal_id`. A text search for `shadow_signal` therefore matches links. Read signals from `obs_shadow_signal` and links from `obs_shadow_link`.

Retention is a TTL index on `timestamp` of 120 days. Every collection has an index on `tenant_id` and that TTL index on `timestamp`. `timestamp` is a BSON date. The original `ts` or `ts_utc` string is kept when the row had one.

Set `OBSERVE_STREAMS_MONGO=0` to stop the copy. The JSONL files stay. This switch does not approve, size, or skip an order.

## Collections

| JSONL file | Collection | What a row is |
| --- | --- | --- |
| `buy_decision_tape.jsonl` | `obs_buy_decision_tape` | One BUY that reached risk evaluate (#628) |
| `daily_gainer_desk.jsonl` | `obs_daily_gainer_desk` | One gainer-desk label: `hit`, `late`, `missed`, or `rejected` (#630) |
| `cap_order_observe.jsonl` | `obs_cap_order_observe` | The existing cap order when `universe_trade_cap` would reject (#633) |
| `shadow_signal.jsonl` | `obs_shadow_signal` | One 15m shadow signal (#687, #688) |
| `shadow_exit.jsonl` | `obs_shadow_exit` | The shadow exit for that signal or its control |
| `shadow_link.jsonl` | `obs_shadow_link` | The buy stamped with `shadow_signal_id`. Not a signal |
| `shadow_control.jsonl` | `obs_shadow_control` | The control row paired with a shadow signal |
| none (Mongo only) | `obs_shadow_rules` | What a candidate entry/exit rule would have done. Observe only (see below) |

## Fields

Common on every document: `tenant_id` (string), `timestamp` (date). The rest is the JSONL object.

Buy decision tape: `ts`, `symbol`, `tenant`, `outcome`, `signal`, `filter_codes`, `book`, `macro_stress`, `correlation_id`.

Daily gainer desk: `ts`, `symbol`, `bucket`, `signal`, `filter_codes`, `source`, `correlation_id`. The log line for one emit is `n`, `hit`, `late`, `missed`, `rejected`, and `sum`. Those four buckets add up to `n` and to `sum`. `n` is the number of lines appended in that call, including when a later append fails. JSONL rotation can move older lines into `daily_gainer_desk.jsonl.1`, so the hot file can be shorter than the sum of `n`. The Mongo collection keeps each appended row until the TTL, which is the count to query.

Cap-order observe: `ts`, `symbol`, `rank`, `cap_name`, `reject_code`, `rejected_symbol`.

Shadow signal: `type` `shadow_signal`, `signal_id`, `ts_utc`, `symbol`, `timeframe`, `price_at_signal`, `gates`, `gates_passed`, `gate_reject_reason`, plus the stop and venue fields written by `services/shadow_signal_log.py`.

Shadow exit: `type` `shadow_exit`, `signal_id`, `ts_utc`, `exit_price`, `exit_reason`, `ret_net_pct`, `control`.

Shadow link: `type` `shadow_link`, `signal_id`, `shadow_signal_id`, `symbol`, `order_id`, `ts_utc`, `source`.

Shadow control: `type` `shadow_control`, `signal_id`, `control`, `control_symbol`, `price_at_signal`, `exit_hours`.

Macro stress observe (#625) is not its own file. It is a log line and, when a buy reaches the tape, the `macro_stress` object on `obs_buy_decision_tape`. Observe defaults on. `fire_enabled` defaults off and is not applied to an order.

## Read-only queries

Use `find`. Do not insert, update, or delete from these collections; the bot writes them and the TTL drops them.

```javascript
db.obs_buy_decision_tape.find(
  {tenant_id: "henry", outcome: "rejected"},
  {symbol: 1, filter_codes: 1, timestamp: 1}
).sort({timestamp: -1}).limit(50)

db.obs_daily_gainer_desk.aggregate([
  {$match: {tenant_id: "henry"}},
  {$group: {_id: "$bucket", n: {$sum: 1}}}
])

db.obs_shadow_signal.find(
  {tenant_id: "henry", gates_passed: false},
  {symbol: 1, gate_reject_reason: 1, timestamp: 1}
).sort({timestamp: -1}).limit(50)

db.obs_shadow_exit.find(
  {signal_id: "<id from obs_shadow_signal>"},
  {exit_reason: 1, ret_net_pct: 1, control: 1, timestamp: 1}
)

db.obs_cap_order_observe.find(
  {tenant_id: "henry"},
  {symbol: 1, rank: 1, rejected_symbol: 1, timestamp: 1}
).sort({timestamp: -1}).limit(20)
```

`obs_shadow_link` is the join from a buy to a signal id. It is not a substitute for `obs_shadow_signal`.

## Shadow rules (`obs_shadow_rules`)

`services/shadow_rules.py` evaluates three candidate rules and only writes what they would have done. No order, size, exit or DCA decision reads these rows. There is no JSONL file; rows go straight to the Mongo copy queue.

| rule | when it is checked | trigger | action |
| --- | --- | --- | --- |
| `g_no_chase` | every BUY that would open a new long lot (`execute_order`, before risk) | price rose more than the limit over the closed 1h lows of the last 48 h (at least 36 bars). Limit: 10 % in `RISK_OFF`/`CRASH`, 20 % in `RISK_ON`, else 15 % | `would_skip_entry`; otherwise `would_allow_entry` or `no_data` |
| `h_oracle_early_exit` | each open long lot seen by `process_coin` | lot pnl <= -10 % within its first 72 h and oracle `RISK_OFF` or `CRASH` | `would_full_exit` |
| `e_age_limit` | each open long lot seen by `process_coin` | lot older than 14 days and pnl <= -15 % | `would_close_aged` |

Thresholds are code constants in `services/shadow_rules.py`, not `config.json` keys. `e_age_limit` is a new rule; it is not harness scenario `e` (early10_72h) or `f` (flat -15 %). The 48 h pump definition is shared with `backtest/harness/scenarios.py` (`_pump48_ok`).

Fields: `type` `shadow_rule`, `observe_only` true, `timestamp`, `tenant_id`, `symbol`, `timeframe`, `lot_id` (`symbol|timeframe|entry_at`), `rule`, `action`, `rule_params`, `oracle_state`, `price`, `pnl_pct`. Entry rows add `pump_48h_pct`, `limit_pct`, `bars_in_window`, `order_source`, `idempotency_key`. Lot rows add `average_entry`, `entry_at`, `lot_age_hours`, `amount`.

A lot rule writes once per lot and rule per process (the first cycle it triggers). A restart can write it again; dedupe on `lot_id` + `rule` when counting.

The work (1h candle fetch, oracle read, insert) runs on a background thread. A failure there is logged and dropped. Set `SHADOW_RULES=0` to switch the rules off; `OBSERVE_STREAMS_MONGO=0` also stops the insert.

Every order document and ledger row now has `ctx_oracle_state`: the decision-time value when the analysis path set it, otherwise the oracle state when the row was written (`null` when no snapshot is available).

```javascript
db.obs_shadow_rules.aggregate([
  {$match: {tenant_id: "henry"}},
  {$group: {_id: {rule: "$rule", action: "$action", oracle: "$oracle_state"}, n: {$sum: 1}}}
])

db.obs_shadow_rules.find(
  {rule: "e_age_limit"},
  {symbol: 1, lot_id: 1, pnl_pct: 1, lot_age_hours: 1, timestamp: 1}
).sort({timestamp: -1}).limit(50)
```
