# Plan: Realtime exits (Gate WS)

**Branch:** `feat/exit-realtime-shadow`  
**Epic:** #178  
**Arena:** Go with conditions — phase per PR; shadow before live.

## Phase 1 (this PR) — Shadow hub #179
- `services/exit_realtime/` Gate `spot.tickers` for open symbols
- Pure `evaluate_trailing_*` → `exit_ws_shadow` logs
- Config `exit_realtime` (default **enabled=false**)
- Wire `architecture_runtime.ensure_started`
- **No orders**

## Phase 2 — Live TTP #180
Debounced sell via RiskManager; single-flight; cycle skip window.

## Phase 3 — trailing_stop live + peak persist #181

## Ops #182 · Policy trail 5/8 #183 (orthogonal)

## Kill
`exit_realtime.enabled=false`
