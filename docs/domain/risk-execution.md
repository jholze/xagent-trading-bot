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

`risk.fail_closed_guards` is `deny` on the checked-in staging config: a guard that raises denies the order.

## #764 main value (not decided)

`main` `config.json` has a `risk` object and, as of 2026-11-10, no `fail_closed_guards` key. Since #754 a missing or malformed key is `deny` in code, so shipping that code to live without an explicit key changes live from the old implicit `log` to `deny`. Explicit `"log"` still selects the old behavior.

The prepared recommendation is an explicit `"deny"` on main. Jens selects the value. The proposed diff is in `docs/evidence/764.md` and `tests/fixtures/fail_closed_guards_764/proposal.json`. It is not applied to `config.json` or to a running tenant. #764 stays open.

## Gate clientOrderId

Gate `text` / ccxt `clientOrderId` is `t-` plus a payload of at most 26 bytes. Charset is `[0-9A-Za-z_.-]`. Two payload formats exist for the same persisted key:

- **Current** (`execution/gate_adapter.py` `_clamp_gate_client_order_id`, #753). An over-long structured key (for example `autoshort|SYM|tf|source|price`) becomes `<11-character head>.<14-hex sha256 of the raw key>`. A pure-hex token (uuid4) stays a 26-character cut so an in-flight id still matches.
- **Legacy** (before #753, `_legacy_gate_client_order_id`). Hyphen-stripped legal text, then the first 26 characters. The source tail and the price were dropped, so two different structured keys could share one id.

Startup recovery (`execution/recovery.py` `_order_ids`) looks up both formats for `client_order_id` and `idempotency_key`. A row written before the format change is found by the legacy cut. A row written after it is found by the current clamp. Short keys are the same string in both formats.
