# Branch Review: `feature/entry-guard-15m`

**Date:** 2026-07-08  
**Hardening branch:** `feature/entry-guard-hardening`  
**Scope:** Entry guard, eval queue, sell policy, caches, DCA unification

## Executive Summary

Functional core is solid (P0 sell overlay, eval queue, entry guard tests). Main risks: **false pump-state on metrics fallback**, **duplicate 15m OHLCV fetches**, **triplicated entry_guard config**, and **dead code** (`market_data.py`, unreachable merge_buy branch).

`feature/shop-optimization` addresses P1/P2 items without behavior changes except corrected guard fallback.

---

## P1 — High Priority

| ID | File | Issue | Fix |
|----|------|-------|-----|
| F1 | `decision_engine.py:673-679` | Fallback forces `price_momentum: True` → false CONTINUATION blocks | Use stored momentum or default NEUTRAL |
| F2 | `ledger_sync.py` | `entry_15m_vol_ratio` not rebuilt from orders | Persist on order request (follow-up) |
| F3 | `entry_sensor_loop.py` + `eval_queue_runtime.py` | Double 15m OHLCV fetch | Pass pending metrics before enqueue |
| F4 | `core/config.py` + `entry_guard.py` | Duplicated defaults | Single `BotConfig.entry_guard_config` |
| F5 | `decision_engine.py:667-672` | Silent exception on metrics fetch | Log WARNING |

## P2 — Medium

| ID | File | Issue | Fix |
|----|------|-------|-----|
| F6 | `decision_engine.py:666` | 15m fetch every sell eval | OHLCV cache (existing `ohlcv_cache`) |
| F7 | `decision_engine.py:121` | `load_effective_watchlist()` per coin | 60s TTL cache |
| F8 | `decision_engine.py:496-500` | Unreachable `social_count >= 2` branch | Remove |
| F9 | `decision_engine.py:840-847` | Duplicate `_sync_watch_15m_state` | Single call |
| F10 | `signal_orchestrator.py:11,15` | Duplicate import | Remove |
| F11 | `sell_rotation_policy.py` + `entry_guard.py` | Duplicate source sets | `strategies/sell_sources.py` |
| F12 | `bus/eval_queue.py:280` | `reset_eval_queue_for_tests` no-op | Implement Redis key delete |
| F13 | `market_data.py` | Unused legacy OHLCV cache | Delete |

## Positive

- `process_entry_sensor()` buy-only split
- P0 sell overlay for cmc_trending positions
- Redis price cache → `/positions` ~164ms warm
- Unit test coverage for entry guard, eval queue, sell rotation

## Implemented in `feature/entry-guard-hardening`

- F1, F3, F4, F5, F7, F8, F9, F10, F11, F12, F13