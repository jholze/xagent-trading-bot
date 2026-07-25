# Epic: Watchlist Quality Engine (WQE)

> **GitHub:** [#124](https://github.com/jholze/xagent-trading-bot/issues/124)  
> **Branch:** [`epic/wqe-124-watchlist-quality-engine`](https://github.com/jholze/xagent-trading-bot/tree/epic/wqe-124-watchlist-quality-engine) (base: `staging`)  
> **Status:** Open · plan ready · implementation pending  
> **Updated:** 2026-07-25  
> **Priority:** P2  
> **Area:** signals / universe  

## Vision

Die **effective Watchlist** ist die Single Source für Scan-Order, Sensor-Universe und Buy-Gates.  
Nicht „was ist heiß“, sondern **handelbar, liquid, regime-fit, memory-bewusst und signal-würdig**.

**Winner-Design:** Arena-Kandidat B — Scored Multi-Tier Watchlist  
**Canonical Arena-Plan:** [`arena-watchlist-quality-signals.md`](arena-watchlist-quality-signals.md)  
**Memory-Schnitt:** Arena §4.9 (`MemoryWqeInput` → `memory_score` + optional hard-exclude neuer Adds)

```text
Gate catalog / sources  →  WQE (hard + score + tier + regime)  →  Scan / Sensor / Buy
                                ↑
                     Trading Memory (soft_block / prefer)
```

## Non-goals

- Jarvis / Chat-UI  
- Order-Ledger-v2  
- Hand-picked coin lists  
- Hermes RSI/param learning ersetzen (orthogonal: `baseline.json`)  
- Social allein → BUY oder soft_block  
- Sells blockieren  

## Rollout

| Mode | Verhalten |
|------|-----------|
| `off` | Status quo |
| `shadow` | Score/Tier loggen, Liste unverändert |
| `soft` | Vol floors + Sort by score |
| `enforce` | Tiers, caps, min_buy_score, memory exclude new adds |

## Children

| Phase | Issue | Title | Depends |
|-------|-------|--------|---------|
| W1 | [#125](https://github.com/jholze/xagent-trading-bot/issues/125) | Memory→WQE adapter (`MemoryWqeInput`, fail-open) | — |
| W2 | [#126](https://github.com/jholze/xagent-trading-bot/issues/126) | Shadow quality score + metrics (no behavior change) | W1 |
| W3 | [#127](https://github.com/jholze/xagent-trading-bot/issues/127) | Soft mode: hard vol floors + scan sort | W2 |
| W4 | [#128](https://github.com/jholze/xagent-trading-bot/issues/128) | Enforce: tiers, caps, regime, buy gates, memory hard-exclude | W3 |
| W5 | [#129](https://github.com/jholze/xagent-trading-bot/issues/129) | Sensor + CMC-only universe alignment to WQE tiers | W4 |
| W6 | [#130](https://github.com/jholze/xagent-trading-bot/issues/130) | Staging soak metrics + optional operator visibility | W5* |

\* Soak may start partially after W3/W4.

## Related

| Issue / Plan | Relation |
|--------------|----------|
| [Arena plan](arena-watchlist-quality-signals.md) | Design + research + §4.9 Memory-Schnitt |
| `trading-memory-hermes.md` / `intelligence/memory` | Profile source |
| Sensor entry guard | Downstream venue/memory on entry |
| [#110](https://github.com/jholze/xagent-trading-bot/issues/110) capacity / [#111](https://github.com/jholze/xagent-trading-bot/issues/111) eviction | Weniger Bad-Opens → weniger Slot-Druck |
| Market context / fusion | Regime mult input |
| CMC trending / DCA trending | Source, re-ranked by WQE |

## Success (staging, ~7d after enforce)

| Metric | Ziel |
|--------|------|
| Avg quote_vol Buy-Universe | +50 %+ |
| Buys score &lt; 0.4 | → 0 |
| Sensor/CMC buys unter vol floor | 0 |
| Eval jobs / cycle | −20–40 % |
| Winrate/PnL neuer Entries | nicht schlechter |

## Next

1. ~~Implement **#125 (W1)** adapter only (pure, tests).~~ **done** — `services/watchlist_quality/memory_bias.py`  
2. ~~**#126 (W2)** shadow score + metrics.~~ **done** — `scoring.py` / `engine.py` / `store.py`; mode=`shadow`  
3. Soft → Enforce hinter Flag (#127 → #128).
