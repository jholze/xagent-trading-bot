# Evidence: paper/testnet freeze 2026-11-10 18:40 (#347)

Money-path module `execution/paper_testnet_freeze.py` and committed
`shorts.testnet_futures.paper_start`. Not wired into the adapter, recovery,
or `short_cover.py`. `shorts.allow_live` stays false.
`shorts.testnet_futures.enabled` stays false. `shorts.liquidation_buffer`
stays `0.05`.

Kay froze these paper/testnet start values after Jens delegated the decision
at 18:40 Europe/Berlin. This is not a live approval and not a strategy result.

| metric | before | after |
| --- | --- | --- |
| committed host allowlist | none | exactly `https://api-testnet.gateapi.io:443` |
| fx-api fallback hosts | none in the new module | empty; legacy host rejected |
| reconciliation tolerance T | unset | `0.01` USDT; a larger T is rejected |
| exact gap `0.01` | n/a | pass |
| gap `0.01 + 0.000001` | n/a | fail |
| ADL warning / elevated / unknown new short | no frozen rule | denied, protection kept, no auto close |
| emergency auto close | no frozen key | false, including if the key is forced true |
| minute or USDT emergency limits in config | absent | absent |
| `tests/unit/test_paper_testnet_freeze_2026_11_10.py` | 0 | 6 passed |

Mutant: `used > 0.01` was flipped to `used < 0.01`. A tolerance of `0.02`
then returned `within_tolerance` instead of `tolerance_widened`.
`test_item4_tolerance_is_one_cent_and_does_not_widen` failed. The mutant was
reverted.

The generated settings reference did not change: `paper_start` is nested
inside the existing shorts block, whose displayed length is unchanged.

No profitability claim. No venue call.

Code measured at `cd8ed3520f5818f88115e98a364052286387e6be` (6 passed).
The commit that adds this sentence is the PR head and does not change behavior.
Coder model: grok-4.7. DeepSeek and Opus reviews are not claimed by this run.
