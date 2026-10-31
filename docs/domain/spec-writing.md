# Spec writing

Every behavior change starts as a spec. The spec is frozen before implementation.

The generic stage files (`specs/<feature>/`, quick path, IDs) are `docs/requirements-engineering-guide.md`. The review chain in that guide's older section 1a (Kay, Lena, Viktor, Omnigent labels) is retired. `AGENTS.md` is the operating rule.

## Required shape

1. One behavior change per spec.
2. Numbered acceptance criteria `AC1`, `AC2`, … `ACn`. Each criterion is observable.
3. An acceptance test for each criterion, named in the spec. The test is written to fail on today's code when the criterion is not already true, and to pass when the criterion is met.
4. The spec is frozen before the build. Implementation does not quietly widen a criterion.
5. A change to a frozen spec is a new revision `Rev N` with a date. The previous revision stays readable. Old labels, tests, and evidence keep the revision they were written under.

## Tests are the frozen spec

Existing assertions stay. A pull request may add tests. It may change an existing assertion only when the spec names the behavior that changes and why. A failing test is reported (name, expected, actual). It is not edited so the new code looks green.

A test diff is reviewed with the same care as a code diff.

## Evidence

The spec names the metric that will be compared before and after (test count, mutants killed, backtest after fees, a risk metric, or a paper result). A change with no measured before/after is not ready. See `AGENTS.md`.
