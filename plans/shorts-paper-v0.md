# Paper shorts v0 — Simulated Live sleeve

**Branch:** `feat/shorts-paper-v0`  
**Status:** review-fixes round (P0.1). Paper only, not merged.  
**Not:** Gate futures, Jesse engine, `bb_upper` auto, live `allow_live`.

## Why this shape (team + other bots)

| Source | Steal | Skip |
|---|---|---|
| **Freqtrade** | Isolated; `is_short`; stop as **% of margin** (2× + 10% stop = 5% price); `liquidation_buffer` so stop hits before liq | Spot cannot short; we **simulate** perps on spot ticks |
| **Jesse** | `long \| short \| close` FSM; stop at open; `risk_to_qty`; isolated liq price | `should_short` DSL, Jesse DB |
| **Hummingbot** | **One-way** default (not hedge) | Perp MM / dual books |
| **Venue practice** | 2–3× start, isolated, hard stop, liquid names | Cross margin, 10×+ |

Our 60d sell tape: `bb_upper` is a coin-flip; `rsi_sell` only 4h edge. Auto allowlist is RSI/climax only. Cover is short-lived.

## Locked product

- Same Simulated Live path (`TradingService` → Risk → adapter → order ledger).
- Orders: `BUY`/`SELL` (long) and `SHORT`/`COVER` (short). Not SELL-as-short.
- One-way per coin+TF. Isolated margin. Hebel global + lot. Cap 5×.
- Tier defaults `volatile` / `stable`, then `shorts.coins[SYMBOL]`, then lot override.
- Auto (P1): `rsi_sell`, `exit_1h_rsi_rollover`, `oracle_climax_harvest`, `exit_volume_climax`.
- Cover: stop, trail-down, RSI-cover, time-cap (4h vol / 8h stable), tick liq. No DCA/grid/RelVol.
- Kill: `shorts.enabled=false`. Live Gate: `allow_live=false` + adapter reject.

## P0 this round

Math SSOT, config, lot `side`/`leverage`, `SHORT`/`COVER` on paper/dry-run, risk gates, Telegram `/short` `/cover`. Radar PnL side-aware.

## Shipped on this branch

- Tick: liq, stop, trail-down, RSI-cover, time-cap → COVER  
- Auto-short after full RSI/climax sells  
- Paper funding on cover (`funding_rate_8h`)  
- Telegram /positions `S` badge + side-aware PnL/NAV  
- MCP `xagent_short` / `xagent_cover`

## Kill / rollback

`shorts.enabled=false` blocks new SHORT. Existing shorts still COVER (liq/stop/trail/time/manual). Redeploy previous staging SHA if Book-replay breaks NAV.

## Review fixes (P0.1 / P0.2)

- One-way also hops TFs (`find_open_position_for_symbol`); sell-repair will not size a SELL from a short.
- SHORT refused on an open long in portfolio + `update_position`.
- Dry-run / `record_trade` cash: SHORT locks margin, COVER returns margin+PnL (not BUY-else-SELL).
- Order-ledger replay skips SELL on a short lot.
- NAV for shorts is margin+uPnL; unknown NAV / short-book errors fail-closed.
- `recent_low` flushed with `force=True`.
- Trade-tree Gesamt uses short Wert/uPnL; desk skips DCA on shorts.

- One-way both directions: BUY/SELL on a short lot fail-closed (risk, portfolio, `update_position`).
- Hub/execute: short-side errors never fall through to long TTP/SELL.
- Auto-short after full allowlisted exit uses `_execute_order_locked` (no nested `ledger_lock`) and `auto_notional_pct` (default 0.35).
- Leverage persisted on order record + request + execution for Book replay.
- `recent_low` flushed so sidecar reload keeps the trail.
- `market_cap_min_usd` and `max_margin_pct` enforced in risk.
- Cycle fallback COVER; RSI stamped before eval.
- UI: Einstand=margin for shorts, SHORT/COVER banners, `/sell` skips shorts, trade tree, desk `side`.
