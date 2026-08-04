# Strategy: Gate Top-Gainer Sniper (Momentum + Quality)

**Codename:** `gainer_sniper`  
**Ticket-Kontext:** baut auf `services/gainer_universe/*`, `exit_realtime`, RiskManager  
**Status:** Strategy Spec (implementierbar, noch kein Code-Change)  
**Modus:** Paper/Demo first · Staging · dann Live-Flag  

---

## 0. One-liner

**Finde liquide Gate-Spot-USDT-Coins, die gerade in die 24h-Top-Ränge schießen, kaufe nur früh im Move mit harten Quality-Filtern, halte wenige Slots, verkaufe per WS-Trailing — nie spät in den Squeeze rein chase’n.**

Das ist reines Momentum-Chasing mit **Quality-Moat**. Ohne die Filter ist es Müll-Token-Lotterie.

---

## 1. Problem, das die Strategy löst

| Symptom heute | Folge |
|---------------|--------|
| 24h-Leaderboard dreht live (SKYAI, VIC, ZRC, …) | Bot sieht Movers oft zu spät oder gar nicht im Trade-Universe |
| Leveraged `3L/5L` und Dust dominieren Top-Listen | Falsche Signale, extreme Givebacks |
| „Immer Top-5 kaufen“ am Ende der 24h-Kerze | Late entry → mean-reversion-Crash |
| Zu breites Expand (Top-50, ~55 inject, max_open ~36) | Kapital verdünnt, keine Sniper-Fokus-Positionen |
| Nur REST-Cycle-Exits | Trail greift Minuten zu spät bei Parabolics |

**Ziel-Outcome (messbar, 48–72h Demo):**

- ≥ 70 % der Sniper-Buys haben `source ∈ {gainer_live_heat, gainer_rank_entry, gainer_accel}`  
- Median Entry-Rank ≤ 8 (nicht Rank 40)  
- Median Entry-`pct_24h` im Band **12–40 %** (nicht +80 % Parabolic)  
- Keine Leveraged-Token-Fills  
- WS `trailing_stop` / `trailing_take_profit` ≥ 50 % der Sniper-Exits  
- Max Drawdown pro Sniper-Trade hard-capped durch Trail + Slot-Limit  

---

## 2. Non-Goals (explizit)

- Kein zweiter Order-Bot / paralleles Ledger  
- Kein same-day Oracle („wissen wer heute #1 schließt“) als Default  
- Kein News-NLP in Phase 1  
- Kein erzwungenes „nur noch Top-5 Universe, Rest tot“ für das Hauptbuch  
- Kein Auto-Buy ohne DecisionEngine + RiskManager  
- Path-stats Soft-Bias ist **Phase 2** (Exit-Feintuning), nicht Entry-Discovery  

---

## 3. Architektur (einfalten, nicht neu erfinden)

```
┌──────────────────────────────────────────────────────────────────┐
│  LAYER A — Discovery (1–5 min)                                   │
│  Gate tickers (REST ccxt fetch_tickers / später WS mirror)       │
│  → hard filters → rank → score → board snapshots                 │
│  Module: gainer_universe.scanner + filters (+ neu: sniper_score) │
└────────────────────────────┬─────────────────────────────────────┘
                             │ candidates + tags (no orders)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  LAYER B — Universe policy                                       │
│  observe: broad board (Top-N live) for memory/WQE/logs           │
│  trade:  sniper slots only (Top-K sticky) ∪ open ∪ base          │
│  Module: inject.py + optional mode sniper                        │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  LAYER C — Entry triggers (DE / sensor hooks)                    │
│  Rank-Entry | Acceleration | Breakout confirm                    │
│  → size_mult / block via RiskManager                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  LAYER D — Risk book                                             │
│  max sniper slots 3–5 · risk 0.5–1.5 % · chase_guard · venue     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  LAYER E — Exit (pro Coin, tick-speed)                           │
│  exit_realtime WS: TTP + trailing_stop (live)                    │
│  cycle fallback: lifetime / sensor / ladder                      │
│  optional Phase 2: path_stats soft_bias trail/arm                │
└──────────────────────────────────────────────────────────────────┘
```

**Prinzip:** Scanner **findet**. Pipeline **handelt**. WS **rettet**.

---

## 4. Datenquellen

### 4.1 Primary (muss)

| Source | Endpoint / API | Freq | Fields |
|--------|----------------|------|--------|
| Gate spot tickers | `ccxt.gate.fetch_tickers()` ≈ REST `GET /api/v4/spot/tickers` | **60s** (config `poll_sec`) | `change_percentage` / `percentage`, `quoteVolume`, `last`, high/low 24h |
| Persist board | `gainer_universe/store` JSON (or mongo later) | every successful scan | rank, pct, vol, ts, sticky state |

### 4.2 Secondary (Phase 1b)

| Source | Use |
|--------|-----|
| 1h OHLCV (existing market/historical) | Acceleration (1h %), breakout vs prior high, relative volume proxy |
| Daily board (existing `build_daily_history`) | `gate_prev_top` continuation — **nicht** primary sniper entry |
| BTC 24h % from same ticker book | Relative strength filter |

### 4.3 Optional later

| Source | Use |
|--------|-----|
| Gate WS `spot.tickers` | Faster rank refresh (reuse exit hub pattern / probe) |
| CMC mcap | Hard mcap floor for sniper only |
| Memory path_stats | Exit tighten/loosen after fill |
| CMC trending | Confluence tag only, never sole entry |

---

## 5. Hard Filters (Quality Moat) — **vor** Ranking

Jeder Ticker muss **alle** bestehen. Fail → drop (kein Score).

| # | Filter | Default (Sniper) | Begründung | Code-Anker |
|---|--------|------------------|------------|------------|
| F1 | Quote = USDT spot | `/USDT` only | Fokus Spot | `passes_spot_usdt_filter` |
| F2 | No leverage / ETF junk | suffix `3L,3S,5L,5S,UP,DOWN,BULL,BEAR` | Top-5 ist voll davon | `blacklist_suffixes` |
| F3 | Min 24h quote volume | **≥ 500_000 USDT** (strict: 1_000_000) | Liquidität, weniger rug-slippage | `min_volume_usdt_24h` |
| F4 | Min last price | **> 1e-5** (config) | Dust / weird ticks | **neu** `min_last_price` |
| F5 | Not stablecoin base | existing stable set | kein Fake-Mover | `_STABLES` |
| F6 | Manual blacklist bases | operator list | Scam / unlocked dumps | `blacklist_bases` |
| F7 | Max 24h % ceiling for **new** entries | **≤ 55 %** (config) | Parabolic already run → chase | **neu** sniper; heat hat 35 heute |
| F8 | Min 24h % floor for board | **≥ 8 %** | Noise raus | aligned `live_heat_min_pct` |
| F9 | BTC relative strength (optional on) | `pct_24h − btc_pct_24h ≥ +5` | Beta-Noise vs echtes Alt-Momentum | **neu** |
| F10 | Listing age (optional) | listing ≥ 48h if data | brand-new list pumps | Phase 1b |

**Reihenfolge:** F1–F6 immer → rank → F7–F9 als *entry* gates (Coin darf auf Board, aber Buy blocken wenn F7 fail).

### 5.1 Leveraged-Token Edge Cases

- Auch `BASE3L` ohne Slash-Normalisierung: immer auf **base** endswith testen.  
- Nie „erlauben wenn Hebel will“ in Sniper-Profil — eigener Profile-Flag `allow_leverage: false` fixed.

---

## 6. Ranking & Board State

### 6.1 Live board (jeder Poll)

Nach Hard Filters:

```
rank_key = (pct_24h DESC, quote_volume DESC)
board = top live_top_n   # sniper observe: 20–30, nicht 50+
```

Jedes Row:

```json
{
  "symbol": "SKYAI/USDT",
  "rank": 3,
  "pct_24h": 28.4,
  "quote_volume": 4200000,
  "last": 0.0123,
  "ts": "ISO",
  "btc_rel_pct": 24.1
}
```

### 6.2 Sticky rank memory (kritisch für „früh im Move“)

Persist pro Symbol zwischen Scans:

| Field | Meaning |
|-------|---------|
| `first_seen_rank` | bester (niedrigster) Rank seit Entry in Top-N window |
| `first_seen_ts` | wann erstmals Rank ≤ `sticky_rank_max` (default 10) |
| `scans_in_top_k` | consecutive polls mit Rank ≤ K |
| `prev_rank` | last scan rank |
| `rank_delta` | `prev_rank − rank` (positiv = steigt im Board) |
| `pct_24h_prev` | for acceleration proxy without 1h candle |

**Rank-Entry braucht Sticky**, sonst kaufst du jeden Flicker Rank 5→12→4.

### 6.3 Sniper score (0–100)

Zusammensetzung (gewichtet, clamp 0–100):

| Component | Weight | Formula (sketch) |
|-----------|--------|------------------|
| **Rank quality** | 30 | `30 * (1 - (rank-1)/max(K-1,1))` for rank≤K else 0 |
| **Momentum band** | 25 | peak score at mid of [min_pct, max_pct]; 0 outside |
| **Volume** | 20 | log-scale vs min_vol (cap at 20 when vol ≥ 10× min) |
| **Acceleration** | 15 | Δpct_24h since last scan or 1h% if available |
| **Rank rising** | 10 | rank_delta > 0 consecutive |

**Momentum band sweet spot (default):**

- full points near **15–30 %** 24h  
- linear decay to 0 at 8 % and at 55 %  
- → genau „früh genug, noch nicht tot“

Entry-Schwelle: `sniper_score ≥ 62` **und** Trigger (unten).

---

## 7. Entry-Trigger (OR-Logik, sniper mode)

Nur wenn Hard Filters + (optional F7–F9) + score ok.

### T1 — Rank-Entry (Primary, wie im Spec-Text)

```
rank <= 5  (config sniper_top_k)
AND scans_in_top_k >= 2          # 2–3 scans @ 60s ≈ 2–3 min sticky
AND rank_delta >= 0              # not falling out
AND pct_24h in [live_heat_min, live_heat_max]
AND not already open / not in rebuy cooldown
```

**Warum sticky ≥ 2:** eliminiert 1-scan Flicker (Pump-Print Artefakte).

### T2 — Acceleration-Entry (früher als reines Top-5)

```
pct_24h >= 15
AND (pct_1h >= 6 OR delta_pct_24h_per_scan >= 2.0)   # rising board
AND quote_volume >= 1.5 * min_volume
AND rank <= 15                                        # still board-relevant
AND sniper_score >= 62
```

Fängt Moves **bevor** sie stabil Top-5 sind — das ist der Edge vs „warte bis #1“.

### T3 — Breakout confirm (optional Phase 1b)

```
last >= high_24h * 0.995     # near/at 24h high
AND volume condition
AND rank <= 10
```

### Explicit non-triggers

- „War gestern #1“ allein (`gate_prev_top`) → **kein** sniper buy ohne T1/T2 heute  
- Chase: `pct_24h > max` → block (F7)  
- `chase_guard` für prev-day tops bleibt an (existiert)

---

## 8. Position Sizing & Book Risk

| Rule | Sniper default | Notes |
|------|----------------|-------|
| Risk per trade | **0.75–1.0 %** equity | via existing ticket/risk path |
| Max simultaneous **sniper** positions | **3** (hard 5) | **source-scoped**, nicht global max_open killen |
| Max notional per sniper fill | existing `max_usdt_per_trade` × 0.8 | slightly smaller than core |
| One position per symbol | yes | standard |
| Daily sniper loss circuit | **−2.5 %** portfolio from sniper tags | pause new sniper entries only |
| Daily max sniper buys | **6** | anti-overtrade |
| Rebuy same symbol | cooldown ≥ 4h after TP/SL | existing rebuy_cooldown |

**Wichtig:** Sniper ist ein **Slot-Profil innerhalb** des Bots, kein Ersatz für alle 36 Positionen. Base-WL und Open laufen weiter; Sniper bekommt **eigenes Slot-Budget**.

---

## 9. Exit Policy (pro Coin, warum WS)

Momentum-Sniper **lebt und stirbt** am Exit.

| Exit | Param (sniper overlay) | Path |
|------|------------------------|------|
| **Trail arm** | activation **5–8 %** peak/current (bestehende policy) | cycle + **WS** |
| **Trail width** | min_trail **6–10 %** (enger als super-wide runners) | WS `trailing_stop` |
| **BE / floor** | `floor_at_entry` on (staging already) | invariant |
| **TTP** | arm early, dynamic trail | WS `trailing_take_profit` |
| **Rank decay exit** (Phase 1b) | rank > 15 for ≥ 3 scans **and** gain < +3 % → soft exit flag | cycle |
| **Time stop** | max hold **12–24 h** for sniper source | `profit_max_lifetime` overlay |
| **Hard catastrophe** | existing stop_loss (wide) only last resort | cycle |

**Phase 2 Memory:** `path_stats` soft-bias  
- high giveback / high trail-hit → tighten trail  
- low giveback + extension → loosen slightly  
- fail-open if thin samples (LAB-tight vs BTC-thin aus Demo-Daten)

---

## 10. Config Profile (target — nicht sofort live knallen)

Vorschlag neuer Block **oder** Mode-Erweiterung:

```json
"gainer_universe": {
  "enabled": true,
  "mode": "sniper",
  "poll_sec": 60,

  "min_volume_usdt_24h": 500000,
  "min_last_price": 0.00001,
  "blacklist_suffixes": ["3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR"],

  "live_top_n": 25,
  "sniper_top_k": 5,
  "sniper_sticky_scans": 2,
  "sniper_max_slots": 3,
  "sniper_score_min": 62,

  "live_heat_trade": true,
  "live_heat_min_pct": 12,
  "live_heat_max_pct": 45,
  "live_heat_ttl_hours": 8,

  "accel_enabled": true,
  "accel_min_pct_24h": 15,
  "accel_min_pct_1h": 6,
  "accel_max_rank": 15,

  "require_btc_rs": true,
  "btc_rs_min_pct": 5,

  "expand_inject_max": 12,
  "trade_max_with_expand": 40,
  "scan_prefer_gainer": true,

  "chase_guard_enabled": true,
  "chase_max_gain_from_prev_close_pct": 15,
  "chase_guard_sources": ["gate_prev_top", "gainer_continuation"]
}
```

**Rollback:** `mode: "off"` oder `enabled: false` → Universe wie ohne Gainer; Exits unberührt.

Shadow first: `mode: "shadow"` → volle Scores/Logs, **kein** trade inject.

---

## 11. Signal → Order Flow (exact)

```
every poll_sec:
  tickers = fetch_gate_tickers()
  filtered = hard_filters(tickers)
  board = rank(filtered)[:live_top_n]
  update sticky state
  candidates = []
  for row in board:
      score = sniper_score(row, sticky)
      if score < sniper_score_min: continue
      if T1 or T2 or T3:
          candidates.append(tag(row, score, trigger))
  persist board + candidates
  if mode == sniper|trade_expand:
      inject top candidates into trade universe (cap sniper_max_slots free)
  
cycle process_coin(candidate):
  DE / sensor may BUY only if still on candidate list OR open
  RiskManager: chase_guard, venue, max slots, daily caps
  on fill: tag position meta source=gainer_sniper, entry_rank, entry_score

exit_realtime hub:
  on each ticker tick for open sniper positions → TTP/trail eval → sell
```

---

## 12. Logging & Audit (non-negotiable)

Every scan write structured lines / state:

- `gainer_sniper.board` top-10 snapshot  
- `gainer_sniper.reject` with filter reason counts  
- `gainer_sniper.trigger` {symbol, trigger, score, rank, pct, vol}  
- `gainer_sniper.skip` {already_open, no_slot, chase, score}  
- On fill: ledger meta `gainer_rank`, `gainer_score`, `gainer_trigger`  
- On exit: `exit_source` (ws vs cycle), pnl, peak_gain, hold_minutes  

Telegram (optional): only on trigger→fill and on WS full exit.

---

## 13. Warum das die „gestern→heute Top-5“ Moves erwischt

| Move-Typ | Wie gefangen |
|----------|----------------|
| Narrative pump + volume (SKYAI-like) | T2 accel while climbing ranks → T1 sticky Top-5 |
| Short-squeeze / event spike (VIC-like) | Volume+F3 + rank rise; sticky avoids 1-print fake |
| Mid-cap rotation | Vol ≥ 500k + BTC-RS filters beta noise |
| Leveraged garbage in raw Top-5 | F2 drops before score |
| Already +90 % parabolic | F7 / heat_max blocks new entry; chase_guard next day |

**Nicht** erwischt (by design): illiquid +500 % microcaps, pure wash, 3L tokens, late FOMO above ceiling.

---

## 14. Comparison: heute im Repo vs Sniper-Profil

| Dimension | Current `trade_expand` | This Sniper strategy |
|-----------|------------------------|----------------------|
| Board size | live_top_n **50** | **25** observe / **5** trade focus |
| Inject | expand **55** | **≤ 12** hot only |
| Same-day heat | 8–35 % | **12–45 %** + score |
| Sticky rank | weak / none | **required** for T1 |
| Acceleration | partial (heat only) | **first-class T2** |
| Slots | global wide book | **3 sniper slots** |
| Score | rank/pct raw | **composite 0–100** |
| Exits | WS on (good) | same + sniper trail overlay |
| Memory | path_stats off | Phase 2 on exits |

Reuse: `filters.py`, `scanner.filter_and_rank_live`, `runtime`, `store`, `chase_guard`, `exit_realtime`, RiskManager.

New pure functions (small): `sniper_score`, sticky update, trigger eval, mode `sniper`.

---

## 15. Implementation Phases

### Phase 0 — Shadow (1–2 Tage)
- mode `shadow` or flag `sniper_shadow: true`  
- log board + score + would-trigger  
- **no inject change**  
- Success: ≥ 20 would-triggers, 0 leverage, reject reasons make sense  

### Phase 1 — Sniper inject (Demo)
- mode `sniper`  
- inject only T1/T2 candidates  
- max 3 slots  
- existing DE still must agree (or light prefer boost)  
- Success metrics §1  

### Phase 1b — Accel + breakout
- 1h OHLCV for true acceleration  
- rank-decay soft exit  

### Phase 2 — Memory exits
- `path_stats.enabled: true`  
- soft bias on open sniper positions only  

### Phase 3 — Live capital
- only after Demo expectancy ≥ 0 after fees over ≥ 30 sniper round-trips  
- start 0.5 % risk  

---

## 16. Test Matrix (must write with code)

| Test | Assert |
|------|--------|
| leverage `BTC3L/USDT` | filtered out |
| vol 100k | filtered out |
| rank flicker 12→3→20 one scan | no T1 |
| rank 4 for 2 scans, pct 22, vol ok | T1 fire |
| pct 70, rank 1 | F7 block new entry |
| score components | golden table |
| mode off | no inject delta |
| chase prev_top +18 % | blocked |
| sticky persist | round-trip store |

---

## 17. Kill Switches

| Switch | Effect |
|--------|--------|
| `gainer_universe.enabled: false` | full off |
| `mode: "off"` | off |
| `mode: "shadow"` | scan only |
| `mode: "trade_expand"` | old broad behavior |
| `mode: "sniper"` | this strategy |
| `exit_realtime.mode: "shadow"` | no live WS sells |
| daily sniper loss circuit | stop new sniper entries |

---

## 18. Risks & Honesty

- Top-gainer sniping is **negative EV** without strict late-chase filters — ceiling + sticky are not optional.  
- Many winners give back 50–90 %; WS trail is mandatory, not nice-to-have.  
- 24h % is a **lagging** label of a move already underway; acceleration + sticky is the only edge.  
- Gate category pages can differ slightly from raw ticker `%` sort — we trade **ticker book**, not UI category HTML.  
- Not financial advice; paper first.

---

## 19. Success Review Checklist (operator)

After 72h Demo:

- [ ] Top reject reason is not „bugs“ but vol/leverage/chase (healthy)  
- [ ] Median entry rank ≤ 8  
- [ ] ≥ 1 full WS exit on a sniper winner with trail  
- [ ] No 3L/5L fills  
- [ ] Sniper slots never > 3  
- [ ] PnL expectancy after fees documented  
- [ ] Decision: promote / retune bands / kill  

---

## 20. Next action (when approved)

1. Branch from `origin/staging`: `feat/gainer-sniper-mode`  
2. Pure: `sniper_score.py` + sticky + triggers + unit tests  
3. Wire mode in `config.py` / `inject.py` / `runtime.py`  
4. Shadow deploy → review logs → sniper demo  

**Do not** mix this with #183 or path-stats PRs — orthogonal, merge later.

---

*End of strategy spec.*
