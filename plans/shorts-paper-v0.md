# Paper shorts v0 — Simulated Live sleeve

**Branch:** `feat/shorts-paper-v0`  
**Status:** first round (P0). Improve after soak.  
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

## P0.5 (this commit)

WS tick: stop / liquidation / time-cap → `COVER` on the same `spot.tickers` hub.  
Auto-short after a **full** sell whose `exit_source` is in the RSI/climax allowlist (not `bb_upper`).

## Kill / rollback

`shorts.enabled=false`. Existing shorts still `cover`. Redeploy previous staging SHA if Book-replay breaks NAV.
