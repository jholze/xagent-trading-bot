# Plan: Realtime exits (Gate WS)

**Branch:** `feat/exit-realtime-shadow`  
**Epic:** #178  
**Arena:** Go with conditions — phase per PR; shadow before live.

## Live on staging (demo)
- `services/exit_realtime/`: Gate `spot.tickers` → pure trail eval → **TradingService.execute_order**
- Config: `enabled=true`, `mode=live` (kill: `enabled=false`)
- Sources: trailing_take_profit + trailing_stop
- Single-flight + cooldown; cycle still runs as fallback

## Tickets
#178 epic · #179/#180 implemented live · #181 peak metrics later · #182 ops · #183 trail policy

## Kill
`exit_realtime.enabled=false`
