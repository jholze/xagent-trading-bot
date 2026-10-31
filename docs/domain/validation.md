# Validation and backtest

Gates for the Live Long epic (#620). A gate that was only proposed is marked as proposed.

## G1–G5

- **G1.** The entry is proven on `staging` with named tests.
- **G2.** The exit detector fires within 60 seconds of the fixture start. `fire_enabled` is false. A multiplier of 3.0 by itself is not proof.
- **G3.** No shorts on the live path.
- **G4.** Comparison against the current logic.
- **G5.** Process (spec, tests, evidence, review). The written handover does not expand G5 into further checks. **open:** no extra G5 checklist was specified.

## Proposed G4 replacement (#642)

Proposed for the henry paper bot's first 24 hours. Not a replacement that has been adopted in place of G4 above.

- 0 executed buys with depth below the order size, or with `qv24` < 500000.
- 0 adds below average, and 0 second adds.
- Reject counts per reason code. Alarm if `missing_input` blocks more than half of buy attempts in the first hour.
- At least 1 allowed buy executes.

## T4 (#628)

Tables of real tape rows. No invented rates. If `n` is too small, the result is **UNCLEAR**. **open:** the handover does not give a numeric cutoff for "too small" on T4.

## Mutation

Surviving mutants on the changed logic are **NOT PASS**. The invariant tests under `tests/unit/test_safety_invariants.py` monkeypatch each pinned guard and must go red when that guard is disabled.

## Costs and trials

The cost model, deflated Sharpe, walk-forward, halves, and top-10 rules are in [hypothesis-protocol.md](hypothesis-protocol.md). The coded check is `tools/hypothesis_protocol.py`. CI runs it on every `docs/evidence/*.json`. No JSON means nothing to judge. A JSON file that is not `PASS` fails `unit`.

Exit-cause labels used in a validation write-up follow [exit-cause-rules.md](exit-cause-rules.md). A trial-ledger row is required before the test (`hypothesis-protocol.md`).

## What is not in code

`scripts/backtest_*.py`, `backtest/`, and `tests/backtest*.py` do not encode PASS/FAIL thresholds. Thresholds live in the hypothesis module above, not in those scripts.
