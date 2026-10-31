# Evidence: price-cycle stall from the gainer daily board

Non-money-path change. The daily gainer board was fetched again for the third tenant whenever a scan outlasted its own freshness stamp, and each symbol opened a new Gate client. Trading decisions are unchanged: the board still refreshes on `daily_refresh_sec` (900s), and the live 24h rank still comes from tickers on `poll_sec`.

## Local benchmark

Same fixture both times: 40 symbols, 50ms fetcher, 4 workers, two builds 200s apart (`daily_refresh_sec` 900). Measured on this branch before the fix and after it.

| metric | before | after |
| --- | --- | --- |
| OHLCV fetches, two builds | 80 | 40 |
| wall time, two builds | 1.008 s | 0.506 s |
| second build / first build | 1.00 | 0.004 |
| Gate clients, 2 sequential symbols | 2 | 1 |
| daily fetches 1000s after a scan that finished at +800s | 40 | 0 |

The acceptance test `test_two_scan_benchmark_stays_on_fast_path` fails when the second build is another fetch wave. `test_ohlcv_reuses_one_gate_client` pins 8 parallel symbol fetches to one client.

## Production logs

`scripts/analysis/cycle_duration.py` on the xagent-test export (`Cycle completed` and `cycle_summary`, 2026-10-06 through 2026-10-31). The price loop stamps one start for every tenant, so the logged duration is cumulative. Own time is the gap to the previous tenant. Loops are length 3. Slot 0 is `default`. Open lots on 2026-10-31 are default 51, ctexp 37, henry 36, which matches slot 0 / slot 1 / slot 2.

| day | completed p50 (s) | slot0 p50 (s) | slot1 p50 (s) | slot2 p50 (s) | slot0 p90 (s) | slot2 p90 (s) |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | 147 | 84 | 75 | 54 | 113 | 223 |
| 2026-10-21 | 160 | 86 | 84 | 38 | 353 | 305 |
| 2026-10-25 | 290 | 140 | 168 | 86 | 459 | 411 |
| 2026-10-26 | 393 | 158 | 178 | 120 | 590 | 709 |
| 2026-10-27 | 992 | 763 | 217 | 739 | 1030 | 922 |
| 2026-10-28 | 841 | 252 | 178 | 610 | 868 | 770 |
| 2026-10-29 | 869 | 745 | 157 | 640 | 953 | 840 |
| 2026-10-30 | 1041 | 896 | 183 | 899 | 1007 | 1004 |
| 2026-10-31 | 686 | 172 | 126 | 197 | 835 | 796 |

Watchlist size and open positions did not jump (summary `wl` median about 70–83, `pos` median about 35). The middle slot stays near 150–220s on the slow days. The two bookends go to about 700–900s.

Every `Cycle took` line in the export says `interval 120s`. `config.json` `update_interval` is 120. 1800s is `hermes.cycle_interval_sec` and the LunarCrush fetch interval, not the price loop. `hermes_external` is true. Some call sites still default `update_interval` to 600 when the key is missing (`aria_bot.py`, `core/config.py`).

## Expected production effect

A daily board is up to 250 symbols (`universe_top_by_volume`). The old path built a Gate client per symbol, so each symbol reloaded markets, and a slow scan was stamped with its start time. The third tenant then downloaded the board again. That is up to two waves of about 750 HTTP calls (markets plus candles) per loop, eight at a time. Gate's client timeout is 10s and `rateLimit` is 50ms.

After the fix, markets load once, candle calls share that client's rate-limit clock, and the second tenant in the window does not fetch. One pass of 250 candles at 50ms is about 13s when Gate answers, once per 900s, not twice per loop. Bookend own-time should leave the 700–900s band. Healthy days were already about 80–160s per tenant, and the raw `Cycle completed` median on those days was 147–290s. Cycles can still run past the 120s `update_interval`, as they did on those days. They should stop running past 1800s.

## Root cause

No staging commit landed on 2026-10-26 or 2026-10-27. The previous runtime commit is #704 (2026-10-22 23:06). #724 (2026-10-23) is CI-only. The scan path is older than both. It shows up after the process has been up long enough to trip Gate: fast on 2026-10-10–12 and again on 2026-10-20 after an 87-hour log gap, then slow. The export has no per-phase timings, so the phase is identified from the slot pattern and this code path, not from a log line that names the scan.
