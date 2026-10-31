# Entry quality filter – Requirements

Version: 0.1 · Date: 2026-11-01 · Status: draft · Owner: Jens Holze

Rev 0. Frozen as a draft before any trading-code change. A later revision is Rev 1 and does not rewrite this text in place. This revision does not add the acceptance tests below and does not add a config key. Adding the tests now would fail the unit job, because the behavior is not in the bot. The names are the contract for the implementation revision.

The replay (`docs/investigations/2026-11-01-entry-quality-replay.md`) ranked `trend50-and-distance20` first on a pre-registered score and then killed it. This spec is the observe-only shadow of that rule. It is not permission to block buys.

## Behavior

One flag, not under `risk` and not a coin list:

`entry_quality.trend_distance_mode`: `off` | `observe` | `enforce`

Rev 0 specifies `observe` as the default for the implementation revision. `enforce` is named so a later revision cannot silently treat "on" as blocking. Rev 0 does not implement `enforce`. Shipping `enforce` needs a new revision, a passing hold-out under `tools/hypothesis_protocol.py`, and the owner's `owner-approved` label. The numbers below are the replay's registered values, not a new search:

- Daily length: 50 closed daily bars.
- Max extension: 20% above that average.
- Max 24h pump: 20%.
- The ±25% grid around 20 (15 and 25) is the cliff check already run. It is not a menu of live settings.

A buy is **allowed by the rule** only when all three are true, using candles that were already closed at the signal time:

1. The coin's last closed daily close is strictly above the mean of that close and the previous 49 daily closes, and those 50 bars are consecutive UTC days.
2. That close is at most 20% above the same average.
3. The last closed 1h close is at most 20% above the close 24 hourly bars earlier, and those bars are 24 hours apart with no gap.

Missing candles, a gap, or a short history are **not** an allow. In `observe` they are a log line `missing` and the order path is unchanged. No coin name and no source name appears in the rule. Stable-coin and denylist guards stay where they are.

`observe` writes one record per buy candidate that reaches the entry check: symbol, signal time, mode, the three booleans or `missing`, and `allow` / `block` / `missing`. It does not change `risk.approved`, size, or the order.

## Acceptance criteria

AC1. With no tenant override, `entry_quality.trend_distance_mode` reads as `observe`.

AC2. In `observe`, a buy that the rule would block is approved or rejected exactly as it is on this revision's code. The new log line is the only difference.

AC3. In `observe`, a fixture with a closed daily history and closed 1h bars produces `allow` or `block` equal to `tools.entry_filter_replay.accepts` for trial `trend50-and-distance20` on the same candles. The fixture's still-open daily bar is not an input.

AC4. In `observe`, a fixture with a gap in the daily bars or fewer than 50 closes logs `missing` and does not change the buy decision.

AC5. `enforce` is not the default, is not read as a block anywhere on the buy path in the implementation revision that only does AC1–AC4, and has no effect while the mode is `observe` or `off`.

AC6. The filter module contains no literal trading-symbol base. The 50 / 20 / 20 numbers come from config, with those defaults.

## Acceptance tests

Named for the implementation revision. Not added in Rev 0.

| Criterion | Test |
| --- | --- |
| AC1 | `tests/unit/test_entry_quality_trend_distance.py::test_mode_defaults_to_observe` |
| AC2 | `tests/unit/test_entry_quality_trend_distance.py::test_observe_does_not_change_buy_approval` |
| AC3 | `tests/unit/test_entry_quality_trend_distance.py::test_observe_matches_replay_accepts` |
| AC4 | `tests/unit/test_entry_quality_trend_distance.py::test_observe_logs_missing_and_does_not_block` |
| AC5 | `tests/unit/test_entry_quality_trend_distance.py::test_enforce_is_not_wired` |
| AC6 | `tests/unit/test_entry_quality_trend_distance.py::test_filter_has_no_symbol_literal` |

## Metric

Before: no observe record, and the replay's protocol status for this rule is KILL (kept n=272, 72h median −1.17% to −0.82% after costs, 72h −20% share 8.63% to 1.47%, walk-forward 2 of 3, deflated Sharpe 0.16).

After an observe implementation: the same approvals as before, plus a log line that matches the replay on the AC3 fixture. A change in approvals is a failed AC2, not a success.

## Non-goals

- Blocking buys, resizing, or exiting.
- Retuning 50, 20, or 20 from the October tape.
- Treating the open-book −20% problem as solved. That needs the owner's max-loss spec. The number is not decided.

## Changelog

- 2026-11-01 · Rev 0 draft. Criteria frozen. No bot code.
