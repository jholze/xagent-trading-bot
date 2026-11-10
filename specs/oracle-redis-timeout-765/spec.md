# Order-path Redis timeout (#765)

Status: behavior already on `cursor/oracle-redis-timeout-765-28aa`. This revision freezes the measurement Marcus required on 2026-11-10. It does not open a new issue.

RECALL_FAILED: Hindsight at `127.0.0.1:8888` is not reachable from this cloud VM.

## Rev 0 — 2026-11-09

Preserved. The order-path read is a Redis GET with a 190 ms cap and no in-process cache fallback. A timeout raises `OracleReadTimeout`. A BUY is not sent. A SELL is not blocked by that read. A missing key returns None. A connection failure returns None and is not a cache hit.

The Rev 0 measurement timed 200 bare GETs (`test_measured_redis_get_p95_and_p99`) and treated p95 < 50 ms and p99 < 200 ms as acceptance. Those thresholds were not in issue #765.

## Rev 1 — 2026-11-10 (Marcus, binding)

Marcus: measure with the stop-write before and after the measurement, not a pure GET microbenchmark. Do not carry the Rev 0 head's CI or reviews onto a new head.

AC1. The recorded latency is the protective stop write. The clock starts immediately before that write and stops immediately after it. A loop of `GET` outside the write is not the measurement.

AC2. **Before:** the same stop write when the order already carries a decision-time oracle state, so the write does not call Redis. **After:** the same stop write when the order has no decision-time state, so `trade_ctx_fields` performs the bounded order-path GET inside the same before/after bracket, and then the reduce-only stop is submitted. Both samples report p95. The after sample includes that GET. The before sample includes zero GETs.

AC3. A socket timeout does not read the in-process cache. BUY and SHORT are not sent (`PortfolioService.execute_order` and `GateExecutionAdapter.execute`). SELL and COVER still run (risk-reducing). Existing Rev 0 assertions for BUY fail-closed and SELL not blocked stay.

AC4. No new execution flag. `shorts.allow_live` stays false. This revision does not edit tenant config.

## AC to test

| AC | Test |
| --- | --- |
| AC1, AC2 | `tests/unit/test_oracle_redis_timeout_765.py::test_stop_write_latency_before_and_after` |
| AC3 | `test_timeout_is_fail_closed_and_does_not_use_the_cache`, `test_timeout_blocks_buy_and_short_and_lets_sell_and_cover_reduce` |
| Rev 0 read shape | `tests/unit/test_ctx_axes_populate.py` (patched onto `get_order_path_redis_snapshot`) |

`test_measured_redis_get_p95_and_p99` is removed under Rev 1. It asserted a bare GET, which Marcus rejected. The fail-closed assertions from that module stay.
