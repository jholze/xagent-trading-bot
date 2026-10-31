# Evidence: symbol and source denylist

Risk change. The owner decides whether this merges. Do not add `owner-approved` until that decision is explicit.

The check is `risk/denylist.py`, called once at the start of `RiskManager._evaluate_impl` for every `BUY` (new buy and add, including `source=manual`). Sells, covers, and shorts are not this rule. Reason codes on the decision, and therefore on the buy decision tape, are `symbol_denylist`, `source_denylist`, and `denylist_config_invalid`.

Defaults in `config.json`:

| Key | Value |
| --- | --- |
| `risk.symbol_denylist` | BLESS, LAB, AVAIL, H |
| `risk.source_denylist` | gainer_rank_entry |

A missing key is an empty list (partial test configs stay as they were). A present value that is not a list of non-empty strings fail-closes every BUY. A tenant overlay replaces the list (`apply_effective_config`), the same way other non-dict `risk` values are replaced. A tenant can clear a list with `[]`.

## Fixture buys that would be blocked

Source: `tests/fixtures/loss_cases_641.csv` (276 buy/add rows: 137 `entry`, 139 `add_same_tf`). Other files under `tests/fixtures/` contributed 0 additional buy/add rows. `auswertungen/` replay files do not contain `gainer_rank_entry`.

| metric | before | after |
| --- | --- | --- |
| buy/add rows the central denylist rejects | 0 (no denylist in RiskManager) | 33 |
| of which entries | 0 | 13 |
| of which adds | 0 | 20 |
| fixture `expected=PASS` (old row said allow; denylist now rejects) | 0 | 5 |
| fixture `expected=BLOCK` (already blocked by an older rule) | 0 | 20 |
| fixture `expected=NOT_PROVABLE` | 0 | 8 |
| symbol hit only | 0 | 30 |
| source hit only (`gainer_rank_entry`, base not listed) | 0 | 2 |
| both symbol and source | 0 | 1 |

33 = 30 symbol-only + 2 source-only + 1 both.

## Tests

Same command as the docs PR, against staging `fb078d7` and this branch. Local Mongo 7.0.14.

| metric | before | after |
| --- | --- | --- |
| unit tests passed | 5556 | 5576 |
| skipped | 5 | 5 |
| xfailed | 1 | 1 |
| collected (passed + skipped + xfailed) | 5562 | 5582 |
| new tests | 0 | 20 |
| pytest runtime (s) | 47.29 | 47.73 |
| wall clock (s) | 50 | 50 |
| suite exit code | 0 | 0 |

The 20 new tests are `tests/unit/test_symbol_source_denylist.py`. `test_t1` in `tests/unit/test_issue_671_liquidity_cap.py` still passes: it now buys `TAG/USDT` on the shipped config so the liquidity band stays covered. Listed bases are covered by the new file.

`tests/unit/test_dca_guard.py` still uses a fixture config with no denylist keys, so those guards are unchanged.
