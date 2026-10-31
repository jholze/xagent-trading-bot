# Evidence: review gates and domain docs

Non-money-path change. No trading runtime code. Measured on staging `930792a` versus this branch, local Mongo 7.0.14, `pytest tests/unit -n auto --dist loadfile`.

| metric | before | after |
| --- | --- | --- |
| unit tests passed | 5477 | 5533 |
| skipped | 5 | 5 |
| xfailed | 1 | 1 |
| collected (passed + skipped + xfailed) | 5483 | 5539 |
| new tests | 0 | 56 |
| pytest runtime (s) | 47.75 | 47.40 |
| wall clock (s) | 50 | 50 |
| suite exit code | 0 | 0 |

The 56 new tests are `tests/unit/test_safety_invariants.py`, `tests/unit/test_hypothesis_protocol.py`, and `tests/unit/test_review_gates.py`. Same 5 skips (Redis down, no `decisions.jsonl`) and the same expected xfail (`#696` cap stop).

Gate behavior checked by those tests, and by the new steps in the required `unit` job:

| check | result |
| --- | --- |
| money-path list | `ci/money_paths.json` |
| non-money-path evidence | this file's before/after table, or an `Evidence` section in the PR body |
| money-path without `owner-approved` | fail |
| hypothesis JSON in `docs/evidence/` | none in this PR, so the step passes |
| Ruff | `E9,F821,F823` on changed Python files only |

Branch protection already requires `unit`. These steps are in that job. No new required check.
