# DCA Sniper — bester Weg (60d Sweep 2026-08-09)

## Setup
- Fenster: 60d Gate OHLCV
- Raster-Coins (Watchlist/Alts), Majors raus
- DD-Band **30–55 %** vom 60d-Hoch (keine −90 % Rugs)
- Entry: near-top (harter Recovery-Fall)
- Report: `dca_sniper_replay_60d_20260809_1216.md`

## Ranking vs BEAT-Baseline (A0)

| Rank | Policy | med Δ vs A0 | sum PnL | hard SL | Lesart |
|-----:|--------|------------:|--------:|--------:|--------|
| **1** | **A5 reanchor only** | **+156** | **0** | 0 | Peak nach Dip neu → kein BEAT-Loch |
| 2 | A4 small + hold + reclaim | −409 | −6824 | 3 | kleiner Add, Hold, nur Reclaim |
| 3 | A1 legacy small | −415 | −6858 | 3 | heutiges Small-DCA |
| 4 | A2 heavy + hold (kein Reclaim) | −471 | −7663 | 3 | blind Heavy |
| 5 | A3 heavy + hold + reclaim | −500 | −8307 | 3 | Heavy auch mit Reclaim teuer wenn weiter rot |

**BEST im Sample: A5_reanchor_only**

## Was das bedeutet

1. **Größter Hebel ist Peak-Reanchor + kein stale Trail** (BEAT fix) — nicht „möglichst viel nachkaufen“.
2. **Heavy-DCA in fortgesetzten Dumps verliert mehr Kapital** (Hard-SL −40 % auf größerer Bag).
3. **Reclaim-Gate** filtert Free-Fall; rettet PnL nicht allein, wenn der Coin danach nochmal bricht.
4. **Hold** zahlt sich erst aus, wenn nach dem Add noch Trail/TTP droht **und** der Coin Richtung BE+ läuft — in diesem Sample selten.

## Empfohlener Produkt-Pfad (beste Kombi)

```text
P0  immer: peak_epoch / reanchor nach jedem DCA   ← A5-Effekt
P0  recovery_hold nach Sniper/Focus-DCA           ← BLESS/BEAT Exit-Schutz
P1  DCA nur wenn: loss-band + NOT free_fall + reclaim (3 higher lows / bounce)
P1  default size: small / bag-relativ moderat
P2  heavy nur bei: reclaim + score hoch + DD nicht extrem (≤~55% path)
P2  serial quality focus 1…N aus Cash — kein Peanut-Spray
```

### Config-Defaults (lokal gesetzt, enabled=false)

- `require_reclaim_for_heavy: true`
- `heavy_only_on_reclaim: true`
- `prefer_small_before_heavy: true`
- `max_dd_pct_for_heavy: 55`
- `min_dd_pct_for_dca: 12`

## Nicht tun

- Blind Heavy auf −70 %…−99 % Coins  
- Hold als Ersatz für fehlende Struktur  
- Stale Peak nach DCA (BEAT)

## Nächster Validierungsschritt

Shadow 7d Staging mit:
1. reanchor immer an  
2. hold an  
3. execute nur small+reclaim (heavy shadow-log only)
