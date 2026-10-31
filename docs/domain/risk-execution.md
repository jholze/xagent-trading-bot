# Risk and execution

Epic #620 names review gates R1–R6. This handover specifies **R4** and the exit-review checks below. **open:** the text of R1, R2, R3, R5, and R6 was not in the handover. Do not invent them.

## R4

- A per-trade max loss.
- A kill switch.
- Size fails closed in `HARVEST` and `RISK_OFF`.
- The live cap is smaller than the paper cap.

**Conflict, unresolved.** `docs/requirements-engineering-guide.md` (2026-10-23, section 1a) says this project deliberately has no daily-loss cap, no emergency brake / kill switch, and no fixed max-open-positions number. R4 names a kill switch and a per-trade max loss. Neither source gives the numbers. Do not add a kill switch, a daily-loss cap, or a new max-open number until the owner decides.

**open:** the numeric per-trade max loss, the kill-switch threshold, and the live-versus-paper cap sizes are not in the handover. The 2026-10-08 small-live discussion (~25 USDT per position, 20% stop, 200 USDT reserve, no daily-loss cap) is not an explicit owner yes. Do not treat those figures as config.

## Exit review

Every exit review needs order-tape proof that the path is alive in production: the last order per source and per tenant.

## Cycle silence

Alarm when a tenant receives no `source=auto` order for N cycles. **unknown:** N was not specified.

## Positions at −20%

Positions at −20% or worse should not exist; entry quality has to improve. That needs its own max-loss spec. **open:** the owner decides the values. −20% is the problem statement, not a coded stop.

## Where risk config lives

`config.json` key `risk` (cash floor, position capacity, daily buy caps, `partial_stop_ratio`, position locks, and the rest of that object). Real tenant overrides are in Mongo and are not readable from this repo. Tenant merge is `config.json` → profile preset → tenant body (`core/trading_profiles.py` `apply_effective_config`). A list under `risk` is replaced by the tenant list, not concatenated.

`risk.fail_closed_guards` is `deny` on the checked-in config: a guard that raises denies the order.
