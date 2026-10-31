# Cycle scan stall – Requirements
Version: 1.0 · Date: 2026-10-31 · Status: approved · Owner: Jens Holze

Rev 1. Frozen before the build. The price cycle slowed because the gainer daily board refetched Gate candles for every tenant once a scan outlasted its own freshness stamp, and each symbol opened a new Gate client.

## AC1

Two daily-board builds whose clocks fall inside one `daily_refresh_sec` window fetch each symbol once. The second build returns the same ranked history. After the window, the symbol is fetched again.

Acceptance test: `tests/unit/test_cycle_scan_stall.py::test_daily_history_fetches_once_per_refresh_window` and `test_daily_history_refetches_after_refresh_window`.

## AC2

`daily_scanned_at` is the time the fetch finished, not the clock captured before it. A later tenant cycle that starts within `daily_refresh_sec` of that completion does not fetch daily bars again, even if the pre-fetch clock is already older than the window.

Acceptance test: `tests/unit/test_cycle_scan_stall.py::test_slow_scan_completion_stamp_suppresses_rescan`.

## AC3

Historical OHLCV uses one Gate client per process. Parallel symbol fetches do not construct another client.

Acceptance test: `tests/unit/test_cycle_scan_stall.py::test_ohlcv_reuses_one_gate_client`.

## AC4

A local benchmark of 40 symbols, a 50ms fetch, and 4 workers runs the fetch wave once when the board is built twice inside the refresh window. The second build takes less than half the first.

Acceptance test: `tests/unit/test_cycle_scan_stall.py::test_two_scan_benchmark_stays_on_fast_path`.

## Out of scope

Which coins are bought or sold, sizes, stops, and limits. The daily board still refreshes on `daily_refresh_sec`. Live 24h rank still comes from the ticker fetch on `poll_sec`.

## Changelog

- 2026-10-31 · Rev 1 · Initial freeze.
