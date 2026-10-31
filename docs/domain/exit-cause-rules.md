# Exit cause rules (v1.3)

Version **v1.3**, fixed before the first label. Earlier versions are not relabeled. Date 2026-10-22. Author: Lena. In force from the first run, 2026-10-23 09:30 Europe/Berlin.

Scope: this repo, tenants `default` and `henry`, realized losses only.

Thresholds are relative to measured cost and ATR. The labeler only computes. No judgment after the fact.

Line numbers cited in the original notes were from older commits (`956807c`, `96987b5`). This file cites symbols. `CAP_STOP_LABEL` is still `None` in `strategies/decision_engine.py` on staging `930792a`.

## 1. Units

- **Loss unit:** every filled sell or cover whose net realized PnL is < 0 USDT after fees. A partial sell is its own unit. Also count one position cycle so correlated partials are not treated as independent cases.
- **Position cycle:** from the first buy (inventory 0 → > 0) until inventory is 0 again. `entry_ts` is the first buy in the cycle. `exit_ts` is the sell under review.
- **Direction d:** +1 long, −1 short. Return `r` is always read as `d · (price change)`. Against the position means `d · r < 0`.
- **DCA:** the running size-weighted average entry including fees (`avg_entry_t`) up to time t. MFE and MAE are measured per candle against `avg_entry_t`. ENTRY rules use the first buy.
- **No look-ahead.** Every field except `post_*` uses data through `exit_ts` only (indicators on the last *closed* candle before that time). The only exception is the post window in section 3, read only for rule R5 and stored as `post_*`.

## 2. Required fields per loss

| Field | Source |
| --- | --- |
| `tenant_id`, `symbol`, `timeframe`, `side` | Order record (`services/order_service.py`), collection `xagent_orders`. Not the trade history. `portfolio_service` trade history has no `exit_source`. `xagent_orders` holds about 200 rows, so a daily copy under `/workspace/trading-learning/orders/` is required. The loop reads orders or that copy. |
| Entry and exit order ids | `exchange_order_id`, `client_order_id`, internal `id` |
| `entry_ts`, `exit_ts` | `timestamps.created`. **Assumption:** fill time equals `created` until a fill field exists. |
| `entry_price`, `exit_price` | `execution.price`. DCA uses the position `average_entry` (`strategies/positions.py`). |
| Net PnL USDT and net PnL % | `pnl` or `realized_pnl` (`storage/order_ledger_v2.py`). Fees from `execution.fee`. **Assumption:** `realized_pnl` is already after fees; if it is not, recompute it. `execution.fee_unknown = true` means a required field is missing. |
| Position size at entry (USDT) | `execution.usdt` of the first buy |
| DCA yes/no and add count | See the v1.3 rule below. |
| Lock during the hold | Position `lock` with `enabled`, `modes`, `locked_at`, `until`. Modes: `no_auto_sell`, `no_dca`, `no_evict`, `no_manual_sell` (`strategies/position_lock.py`). |
| Exit path | One of `STOP_BOT`, `STOP_TWEET`, `GEWINNSCHUTZ`, `SHORT_FORCED`, `MANUAL`, `KEINER`, `CAP`. Mapping below. |
| BTC and ETH return over the hold | OHLCV `BTC/USDT` and `ETH/USDT` on the trade's exchange. Close before `entry_ts` to close before `exit_ts`. |
| BTC/ETH regime at entry and exit | Section 3, computed from OHLCV. Not `ctx_coin_regime` (its definition is not fixed). |
| Alt-market breadth | Optional. Recorded only. Not decisive in v1. |
| MFE, MAE (% vs `avg_entry_t`) | Symbol OHLCV on the trade `timeframe` between `entry_ts` and `exit_ts`. |

### DCA determination (v1.3)

Primary source: `dca_rounds` on the lot (`strategies/positions.py`), from a **valid pre-exit snapshot** taken while the lot was open before `exit_ts` (`amount > 0` and the same `first_buy_at`). A snapshot after a partial sell stays valid. Then `>= 1` means yes and `0` means no.

A snapshot after a full exit (`dca_rounds` reset to 0 on flat, and on a new open) is not evidence and is ignored.

Otherwise the order fallback: count distinct buy order ids on the same lot before `exit_ts` from `xagent_orders` or the daily copy. Partial fills are not counted separately. More than 1 means yes. Exactly 1 means no.

If there is no valid pre-exit snapshot and no first buy in the window: DCA **UNLABELED** with `missing=[first_buy]`. Never "no".

`dca_conflict` only when a valid pre-exit snapshot and the order count disagree. The snapshot wins. The conflict count goes in the daily report.

Recovery fills increment `dca_rounds` and count as DCA (an add into the position).

**Data requirement:** a daily snapshot of open lots (`amount > 0`, `first_buy_at`, `dca_rounds`) next to `/workspace/trading-learning/orders/`. Without it, every row uses the fallback.

### Exit path

From `exit_source` on the exit order. Direction matters.

- **STOP_BOT** = `stop_loss` (full or partial from `SELL_STOP_FULL` / `SELL_STOP_PARTIAL`; `partial_stop` is rewritten to `stop_loss` in the cycle).
- **STOP_TWEET** = `x_stop_loss` (stop price from the tweet). Its own group.
- **GEWINNSCHUTZ** = `trailing_stop` on a long (ATR trail). Not a stop.
- **SHORT_FORCED** = short cover with `liquidation`, `trailing_stop` (margin stop), `climax_stop`, `time_cap`, or `climax_time`.
- **MANUAL** = `exit_source` null (manual, MCP, or DCA-funding sells) or `panic`.
- **`technical`** stays excluded (take-profit / RSI tier, never a hard stop), even though `strategies/sell_sources.py` `STOP_SOURCES` contains it.
- **CAP:** not on staging (`CAP_STOP_LABEL = None`). After #696 the cap writes `stop_loss` + `SELL_STOP_FULL`, which cannot be told from the hard stop. `CAP_SOURCES` is empty. CAP is **UNLABELED** with `missing=[cap_flag]` until a dedicated cap field is stored.
- **KEINER** = any other non-empty value.
- `unknown_sell` = missing.

## 3. Parameters (v1, unchanged through v1.3)

| Name | Definition |
| --- | --- |
| `COST_RT` | Measured entry fee + exit fee in % plus measured slippage in % (`abs(execution.price − request.price) / request.price`, per order). Actual cost. Nothing estimated. |
| `ATR_PCT` | ATR(14, Wilder) of the symbol on the trade timeframe, last closed candle before `entry_ts`, in % of price. |
| `ATR_BTC_PCT` | The same for `BTC/USDT`, same timeframe. |
| `NOISE` | `max(COST_RT, 0.5 · ATR_PCT)`. Below this, a "gain" is not distinguishable from cost or noise. |
| `MFE_ENTRY_MAX` | `NOISE`. MFE below `NOISE` means the trade was never really in profit. |
| `MFE_EXIT_MIN` | `COST_RT + 1.0 · ATR_PCT`. One ATR above costs is a gain that could have been kept. |
| `BTC_MOVE_MIN` | `1.0 · ATR_BTC_PCT`. |
| `BETA` | OLS beta of the symbol's returns on BTC returns, trade timeframe, 30 days before `entry_ts`. If it cannot be computed, **1.0**. |
| `REGIME` | 4h. Close > EMA50 **and** EMA50 higher than 6 candles ago → UP. Close < EMA50 **and** EMA50 lower than 6 candles ago → DOWN. Otherwise RANGE. |
| `HTF_MAP` | Next higher frame: 15m/1h → 4h, 4h → 1d, 1d → 1w. |
| `POST_WINDOW` | The hold length, clamped to [24h, 7d], starting at `exit_ts`. |
| `RECOVERY` | Inside `POST_WINDOW`, the high (long) or low (short) reaches `avg_entry ± COST_RT` (net break-even). |
| `ROUND` | Compare only after rounding to 4 decimal places, in %. |
| `SAMPLE_N` | 20 losses, or all of them if fewer. Random sample, seed `20261106`. |

## 4. Decision tree

First matching rule wins. One primary label. Compare with `<` and `≥` as written, after `ROUND`.

- **R0 UNLABELED.** A required field is missing or invalid (including `fee_unknown`, missing OHLCV, missing exit path). Store `missing=[…]`.
- **R0b PENDING (no label).** `exit_ts + POST_WINDOW` is still in the future. Label on a later run. R5 never decides on a short window.
- **R0c MANUAL.** Exit path is MANUAL (`exit_source` null or `panic`). Not part of FALLBACK. Reported separately. R0c stands before MARKT so a manual exit does not pollute the other classes.
- **R1 MARKT.** All of: (a) `d · r_BTC < 0` and `abs(r_BTC) ≥ BTC_MOVE_MIN`; (b) `sign(r_ETH) = sign(r_BTC)`; (c) `sign(r_sym) = sign(r_BTC)`; (d) `abs(r_sym − BETA · r_BTC) < abs(BETA · r_BTC)` (the beta part outweighs the rest); (e) BTC `REGIME` at exit differs from entry and the exit regime is against the position (DOWN for a long, UP for a short).
- **R2 ENTRY (never in profit).** `MFE < MFE_ENTRY_MAX` and `abs(r_BTC) < BTC_MOVE_MIN`.
- **R3 EXIT (gave back a gain).** `MFE ≥ MFE_EXIT_MIN`. R3 is before R4 because a large MFE confirmed the entry.
- **R4 ENTRY (against the higher frame).** Symbol `REGIME` on the `HTF_MAP` frame at `entry_ts` is against the position. R4 is before R5 because rules that do not need the post window win over rules that do.
- **R5 EXIT (recovered after the exit).** Exit path is KEINER and `RECOVERY` holds in `POST_WINDOW`. The only rule that reads `post_*`. The old cap branch is UNLABELED with `missing=[cap_flag]` until a cap field exists (#696 re-review).
- **R6 ENTRY + tag `FALLBACK`.** Everything else (`MFE < MFE_EXIT_MIN` and no earlier rule).

Primary classes: UNLABELED, PENDING, MANUAL, MARKT, ENTRY, EXIT. ENTRY may carry the FALLBACK tag. Report the FALLBACK share excluding MANUAL. A high FALLBACK share means the tree is too coarse; that is fixed in v2, not by editing v1 labels.

## 5. Tags, not primary classes

Several tags may sit on one loss. They do not change the primary class.

- **LOCK.** A lock with `no_auto_sell` was active between `entry_ts` and `exit_ts`.
- **DCA.** DCA = yes by section 2. UNLABELED (`missing=[first_buy]`) sets no tag and is not "no".
- **RISIKO.** MAE < −(the strategy's configured `stop_loss_pct` at entry + `COST_RT`), so the stop was exceeded. Or the entry size was above the tenant's configured position limit. The limits come from the bot config, not from this document.

Only realized losses are labeled. An open locked lot is not. If a locked lot is sold (manually, or after `until`), the normal tree runs and the LOCK tag is set.

## 6. Working rules

1. Rules are fixed before the first entry. A change is a new version with a date and an author. Old labels stay stored with their `rules_version`. Relabeling with v2 is an extra column, not a rewrite.
2. A trial-ledger row before every test. Hypothesis, date, wording, data window, and the count of every pattern looked at and discarded since the last row. The path in the original note (`trading-learning/trial-ledger.md`) was an assumption. No ledger row, no result. The pre-registered rows that were handed over are copied in [hypothesis-protocol.md](hypothesis-protocol.md).
3. Test only outside the discovery window. Discovery window: 2026-10-23 through 2026-11-05. A hypothesis from that window is tested only on losses with `exit_ts` ≥ 2026-11-06. Record the window in the ledger.
4. Validation check after two weeks (2026-11-06): a second person labels `SAMPLE_N` losses from this document without seeing the first labels. Report agreement (% and Cohen's kappa), disagreements per rule, and the shares of UNLABELED (with reasons), PENDING, MANUAL, and FALLBACK, each per tenant. Disagreements produce v2 for the future only.

## Version notes

**v1 → v1.1** (2026-10-22, before the first entry). Thresholds in section 3 unchanged.

1. No separate cap exit on staging. Cap-related branches are UNLABELED `missing=[cap_flag]`.
2. Stop groups as in section 2. `trailing_stop` on a long is profit protection, not a stop. On a short cover it is a stop.
3. SHORT_FORCED sources as in section 2.
4. New primary class MANUAL (R0c). In v1 an empty `exit_source` was UNLABELED.
5. `exit_source` comes from the order record, not the trade history.
6. R5 uses exit path KEINER, not {empty, cap}.

**v1.1 → v1.2.** DCA determination only. Snapshot `dca_rounds` first; order-id fallback; missing first buy is UNLABELED, never "no"; snapshot wins on conflict. Determining DCA from an order source named `dca` was removed: that path writes `exit_source` `stop_loss` or null and is not usable.

**v1.2 → v1.3.** Snapshot counts only when it was taken on an open lot before `exit_ts`. A post-exit snapshot with `dca_rounds = 0` is ignored. Recovery fills count as DCA. Daily open-lot snapshot is a data requirement.
