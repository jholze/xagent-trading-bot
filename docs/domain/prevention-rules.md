# Prevention rules

Owner rules dated **2026-08-24**. Until a risk change lands in `RiskManager`, the symbol and source blocks are enforced by an external tool, not by this bot. The two DCA guards below are already in the bot.

## Blocks

No new buy and no add on these base assets:

- `BLESS`
- `LAB`
- `AVAIL`
- `H`

Hard-block signal source `gainer_rank_entry`.

No coin names belong in strategy code. The lists, when the bot enforces them, are config (`risk.symbol_denylist`, `risk.source_denylist`), overridable per tenant the same way as other `risk` keys.

## DCA guards already in code

`strategies/dca_policy.py` `evaluate_dca_guard`, called from `risk/risk_manager.py` on a BUY that reaches the buy lock.

| Rule | Code | Behavior on staging `930792a` |
| --- | --- | --- |
| No DCA when live price < average entry | `dca_guard_below_avg` | Add with an open lot and `live < avg` is blocked. `live == avg` is not this rule. A new entry (no open lot) is not this guard. |
| No second add when `dca_rounds >= 1` | `dca_guard_max_rounds` | Blocked. |
| No add when the lot is locked | `dca_guard_locked` | Any active lock blocks the add. There is no lock mode that allows DCA. |
| Missing or stale inputs | `dca_guard_missing_input` | Fail closed: missing position, non-positive live or average, missing or negative `dca_rounds`, unreadable lock, stale price, or the guard raising. |

Partial-stop pause, as implemented in `strategies/dca.py` `should_pause_partial_stop`:

- No completed DCA round → partial stop is not paused.
- `pause_partial_stop_during_dca` defaults to true. While it is true and a round has been used, the partial stop is paused (`effective_stop_loss_thresholds` returns no partial level).
- If the flag is false, the partial stop stays paused while used rounds are still below the max (`dca_max_rounds` on the lot, otherwise `max_rounds` in config). Once used rounds reach that max, the partial stop is not paused.

`gainer_rank_entry` is produced in `services/gainer_signal/pure.py` (`select_entry_signals`) and consumed in `services/gainer_signal/bot_http.py` (`process_gainer_signal`). As of `930792a` there is no central symbol or source denylist in `RiskManager`. Scattered `blacklist_bases` / `exclude_symbols` are not this rule.

Pinned by `tests/unit/test_safety_invariants.py`.
