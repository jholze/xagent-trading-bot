# Evidence: entry-filter replay

Non-money-path change. No trading runtime code, no config change, no order-flow change. The replay is `tools/entry_filter_replay.py` plus unit tests. The write-up is `docs/investigations/2026-11-01-entry-quality-replay.md`.

| metric | before | after |
| --- | ---: | ---: |
| tests in `tests/unit/test_entry_filter_replay.py` | 0 | 20 |
| trading runtime files changed | 0 | 0 |
| leading filter protocol status | not run | KILL |

The hypothesis payload is `docs/investigations/2026-11-01-entry-quality-replay.json`. `validate_payload` returns KILL (`dsr<0.95`, `edge_half_a<=0`, `edge_without_top10<=0`, `excluded_entry_pct>10`, `holdout_entries<100`). It is not copied to `docs/evidence/*.json`, because a non-PASS file there fails the unit job. The hold-out starts 2026-11-06, so this tape cannot produce a PASS.
