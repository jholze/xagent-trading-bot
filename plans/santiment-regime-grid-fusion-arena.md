# Arena: Santiment × Regime × Grid Fusion (P1–P3)

> **Status:** implemented on staging (`market_policy_fusion` + hooks)  
> **Date:** 2026-07-17  

## Kandidaten

| | These | Score |
|--|--------|-------|
| A | Nur Size/Sensor (Status vor Fusion) | 6 — Grid/Mode unberührt |
| B | RISK_OFF → force DEFENSIVE überall | 3 — **Kollision:** GridStrategy SELL_PARTIAL_50 auf allen Positionen |
| C | Soft sentiment + Mode MOMENTUM→HYBRID + wider Grid + Size einmal | **9** ✅ |

**Winner: C**

## Kollisionsmatrix

| Bestehend | Santiment-Wirkung | Kollision? | Mitigation |
|-----------|-------------------|------------|------------|
| Risk size_mult | ×0.35 RISK_OFF | nein | einzige Size-Schicht |
| exposure_multiplier Allocator | oft ungenutzt in Risk | nein | nicht zusätzlich × Santiment |
| DEFENSIVE mode + open pos | Grid verkauft 50% | **ja** | **nie** DEFENSIVE aus RISK_OFF erzwingen |
| Allocator defensive_thresh -0.55 | full defensive | **ja** | RISK_OFF Sentiment **-0.45** (> -0.55) |
| force_grid coins | muss Grid bleiben | nein | force_grid preserved |
| Grid sell guards (green) | Sells | nein | Spacing nur weiter, Sells ok |
| Entry sensor shadow | already P3 phase | nein | complementary |
| Coin-local regime | UPTREND vs global RISK_OFF | soft | Sentiment fusion weighted; local tech remains |

## Implemented hooks

1. `inject_global_sentiment` → RegimeDetector  
2. `apply_global_mode_bias` → trading_mode after resolve  
3. `grid_spacing_mult` → GridStrategy spacing  
4. LC key alias `lunarcrush_sentiment` for detector  

## Non-goals

- Market Oracle full service merge (later)  
- Forcing DEFENSIVE inventory reduction from Santiment alone  
