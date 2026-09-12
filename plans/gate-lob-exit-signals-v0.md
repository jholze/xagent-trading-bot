# Gate.io LOB & Tape Exit Signals — Concept Paper (v0)

> **Branch:** `docs/gate-lob-exit-signals-v0`  
> **Status:** Concept only — no implementation in this PR  
> **Date:** 2026-08-13  
> **Context:** COTI post-mortem (CMC entry → ATR trail stop @ entry floor → ~0% / −4%);  
> peak ~+8% at 02:12 UTC, TA (RSI/EMA/BB) flipped ~02:15–02:30; bot exits hours later.  
> Goal: capture **+3–4%** on failed runners via **earlier microstructure stress**, not only chart indicators.

---

## 1. Problem

| Layer | What we use today | Gap |
|---|---|---|
| Chart TA | RSI, BB, EMA, ATR trail | Late relative to peak; ATR **floor_at_entry** waits for BE dump |
| Venue quality | Snapshot at **buy** (`venue_quality`) | Not used on **exit**; often fail-open for CMC |
| Order book | Top bid/ask stamp (often `$0` top size) | No continuous LOB / imbalance |
| Tape (aggressor trades) | Indirect via OHLCV volume | No `sell_ratio` / CVD-style window |
| Wallet / on-chain | Soft optional (DCA sniper) | Not in hot-path exit |

**Thesis:** On thin Gate alts, **book + aggressor tape** often stress **seconds to minutes before** RSI/EMA death-cross. Combined with *still green vs entry*, they can trigger early take-profit / full close and raise peak capture.

**Non-thesis:** Replace DecisionEngine or trailing stops. LOB is an **overlay / early-exit channel**, fail-open, kill-switchable.

---

## 2. Goals & non-goals

### Goals

1. Define a **minimal Gate-native feature set** for detecting *distribution / dump onset* on open longs.
2. Specify **how** to stream, store (short ring), and **rule-combine** with price/peak state.
3. Map to **xagent** (exit-ws, bot cycle, venue_quality) without a second trading engine.
4. Enable later research (scorecard, significance) — see also `plans/research-jesse-inspired-v0.md`.

### Non-goals

- Full LOB history warehouse / ML on order books (v0).
- Wallet/on-chain as primary real-time dump sensor (optional soft entry only).
- Blocking **sells** on venue quality (sells stay executable).
- Spoof-perfect HFT; we optimize for **staging paper + thin alts**, not co-lo arb.

---

## 3. Gate.io data sources

| Channel | REST | WebSocket (typical) | Use |
|---|---|---|---|
| BBO + sizes | `GET /spot/order_book?limit=1..` / tickers | `spot.book_ticker` | Spread, microprice, top notional |
| Depth (L2) | `GET /spot/order_book?limit=20..50` | `spot.order_book_update` / order_book | Imbalance, depth bands, walls |
| Aggressor tape | `GET /spot/trades` | `spot.trades` (`side` = taker) | Sell/buy ratio, climax |
| 24h context | `GET /spot/tickers` | ticker stream | Vol baseline (already used) |

**Precision notes (Gate):**

- Public book is **not** full institutional L3; still enough for **relative** stress (collapse vs own baseline).
- `trades.side` is the right field for aggressor flow (buy = lift ask, sell = hit bid).
- Thin pairs: prefer **depth within ±0.2–1% of mid**, not only level-1.

**Evidence from COTI buy stamp (paper fill ~01:52 UTC):**

| Field | Value | Read |
|---|---|---|
| `spread_pct` | ~0.37% (~37 bps) | Wide for a quality venue |
| `top_book_bid_usdt` / `ask` | **0** | Below `min_top_book_usdt_per_side` (200) |
| `venue_ok` | **false** | Reasons: bid/ask book $0 &lt; min |
| Planned size | ~4109 USDT | Large vs thin book |

Entry already had a **red venue flag**; continuous LOB was never attached to the open position for exit.

---

## 4. Feature catalog (LOB + tape)

All features are **per symbol**, updated on WS events, held in a short ring buffer (e.g. 2–5 minutes).

### 4.1 P0 — must have

#### F1 — Spread (bps)

```text
spread_bps = (ask - bid) / mid * 10_000
```

| Dump-ish signal | Example threshold (tune) |
|---|---|
| Absolute wide | `spread_bps > 20` on alt |
| Relative spike | `spread_bps > 2 × median_60s` |

**Use:** Entry filter + stress flag on open longs.

#### F2 — Microprice skew

```text
micro = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
skew_bps = (micro - mid) / mid * 10_000
```

| Signal | Meaning |
|---|---|
| `skew_bps` strongly negative | Size leans toward lower prices / ask pressure |
| Micro drifts under last trade | Next prints often lower |

**Use:** Instant, no history; confirm dump with tape.

#### F3 — Near-touch depth & imbalance

```text
depth_bid(x%) = Σ bid_notional for bids within mid*(1 − x)
depth_ask(x%) = Σ ask_notional for asks within mid*(1 + x)
imb(x%) = (depth_bid - depth_ask) / (depth_bid + depth_ask)
```

Recommended bands for Gate alts: **`x ∈ {0.2%, 0.5%, 1.0%}`**. Primary: **0.5%**.

| Signal | Example |
|---|---|
| Imbalance crash | `imb_0.5` drops from &gt; +0.2 to &lt; −0.25 within 15–30s |
| Bid liquidity collapse | `depth_bid_0.5` &lt; 60% of median_60s |

**Use:** Core “support gone” detector.

#### F4 — Top-of-book notional (existing venue fields)

```text
top_bid_usdt = bid * bid_size
top_ask_usdt = ask * ask_size
```

Already in `services/venue_quality.py`. Extend from **buy-only stamp** → **live series** on watched symbols.

| Signal | Example |
|---|---|
| Bid top collapse | `top_bid_usdt < 0.5 * median_60s` |

#### F5 — Aggressor sell ratio (tape)

```text
# window W ∈ {30s, 60s, 120s}
sell_ratio_W = sell_notional_W / (buy_notional_W + sell_notional_W)
```

| Signal | Example |
|---|---|
| Persistent selling | `sell_ratio_60s > 0.65–0.70` |
| Climax | High tape volume **and** high sell_ratio at local price peak |

**Use:** Often the strongest “distribution” print on Gate; pair with price still green.

---

### 4.2 P1 — should have (phase 2)

#### F6 — Wall / slope hints

- Distance (bps) to nearest ask “wall” (level size ≥ k × median level size).
- Bid “holes”: large gaps between bid levels → vacuum risk.

#### F7 — Slippage estimate for position size

Walk the book for a simulated market sell of `position_notional`:

```text
slippage_sell_pct = (mid - avg_fill) / mid
```

| Signal | Example |
|---|---|
| Exit quality collapse | `slippage_sell_pct` worsens 2× vs entry-time estimate |

#### F8 — VAMP (optional)

Depth-weighted mid over ±0.5% — only if F2/F3 noisy on spoofy books.

---

### 4.3 Explicitly deferred

| Idea | Why later |
|---|---|
| Full L2 history lake | Cost; research-first |
| Wallet CEX inflow as exit trigger | Latency/coverage; keep entry soft only |
| Heavy ML on LOB images | Needs data pipeline + labels first |

---

## 5. Price context features (required co-signals)

LOB alone false-triggers. Always bind to **position state**:

| Feature | Source |
|---|---|
| `gain_pct` | (mark − entry) / entry |
| `peak_gain_pct` | (peak − entry) / entry |
| `drop_from_peak_pct` | (peak − mark) / peak |
| `hold_minutes` | since entry |
| `tier` / `entry_source` | volatile, cmc, gainer_relvol, … |

**Peak** = max(recent_high, mark) already maintained on positions / exit-ws.

---

## 6. Decision rules (v0 proposal)

### 6.1 Early profit protect (target +3–4% on failed runners)

Fire only if **all** hold:

```text
gain_pct >= 3.0
AND drop_from_peak_pct >= 2.0
AND book_tape_stress == true
```

`book_tape_stress` if **any two** of:

```text
sell_ratio_60s >= 0.68
OR imb_0.5 <= -0.25
OR depth_bid_0.5 <= 0.60 * median_60s(depth_bid_0.5)
OR spread_bps >= 2.0 * median_60s(spread_bps)
OR skew_bps <= -8
```

**Action (staging):**

- Prefer `SELL_FULL` if `gain_pct >= 3` and stress (simple v0), **or**
- `SELL_PARTIAL` 50% then tighten trail (v1).

**Priority vs existing exits:**

- Should compete with / precede **ATR trail floor_at_entry** when green.
- Must **not** block protective stops when red (stress + gain &lt; 0 → let trail/stop handle).

### 6.2 Entry hardening (same features, buy path)

Extend `venue_quality` apply_to to include **`cmc`** (and optionally all gainer sources):

| Check | Soft | Hard (optional) |
|---|---|---|
| `venue_ok == false` | size × 0.5 | block new entry |
| `spread_bps > 20` | size × 0.5 | block |
| `slippage_sell_pct` for planned size &gt; 0.4% | size down | block |

COTI would have been **size-cut or skipped** under hard rules — independent of dump detection.

### 6.3 Kill switches

```json
{
  "lob_exit": {
    "enabled": false,
    "mode": "shadow",
    "tenants": ["default", "henry"],
    "apply_sources": ["cmc", "gainer_relvol", "gainer_live_heat", "gainer_rank_entry"],
    "min_gain_pct": 3.0,
    "min_drop_from_peak_pct": 2.0,
    "shadow_log_only": true
  }
}
```

- `mode=shadow`: log would-fire, no orders.  
- `mode=live`: emit exit via same path as exit_ws / trading service.  
- `enabled=false`: zero overhead beyond optional not subscribing.

---

## 7. Architecture (xagent)

```text
┌─────────────────────────────────────────────────────────┐
│  Gate WS: book_ticker + trades [+ order_book L2]         │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  lob_microstructure service (or exit-radar module)      │
│  - ring buffers per symbol                               │
│  - features F1–F5                                       │
│  - stress score / flags                                 │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Exit eval (exit_ws hub preferred for speed)            │
│  - merge mark, peak, gain                               │
│  - rule 6.1 → SELL_FULL / partial                       │
│  - shadow: log only                                     │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  TradingService (existing dry_run / paper ledger)       │
└─────────────────────────────────────────────────────────┘
```

**Subscription policy:** Only symbols with **open long** in default/henry (cap e.g. 40–60 streams), plus optional permanent proxies for correlated-tier — not full universe.

**Ownership options:**

| Option | Pros | Cons |
|---|---|---|
| **A. exit-radar sidecar** | Fast path already trail-aware | New WS load on radar |
| **B. gainer-signal / new micro service** | Isolation | Extra hop to fire sell |
| **C. in-bot** | Simple | Bot already heavy |

**Recommendation:** **A (exit-radar)** for live eval + fire; bot remains SSOT for execution. Align with existing `exit_ws` COTI default path (faster than henry auto cycle).

---

## 8. Relation to classic indicators

| Time (COTI 1m example) | Chart | LOB/tape (expected role) |
|---|---|---|
| 02:12 peak | Vol climax candle | Bid pull / sell tape climax **often simultaneous or earlier** |
| 02:15–02:17 | BB leave upper, RSI leave OB, close &lt; EMA8 | Imbalance + sell_ratio should already be stressed |
| 02:26 | Peak drop ≥3%, still ~+4.7% | Ideal **combined** fire for +3–4% |
| 04:31 | Structure break under entry | Book vacuum; profit already gone |
| 06:32 / 07:10 | ATR stop @ entry | Too late for peak capture |

**Stacking rule:** LOB/tape = **early**; RSI/EMA/BB = **confirm**; ATR floor = **last resort**.

---

## 9. Metrics for success (experiment)

When shadow or live:

| Metric | Target |
|---|---|
| Peak capture on volatile CMC/gainer winners | Improve vs ATR-only baseline |
| False early exits (price reclaims peak within 30m) | Track rate; tune thresholds |
| Extra giveback vs default trail on losers | Should not worsen much |
| Venue-blocked bad entries | Count + avoided notional |

Log every would-fire:

```json
{
  "ts": "...",
  "symbol": "COTI/USDT",
  "tenant_id": "henry",
  "gain_pct": 4.7,
  "drop_from_peak_pct": 3.0,
  "sell_ratio_60s": 0.71,
  "imb_0.5": -0.32,
  "depth_bid_0.5": 180.0,
  "spread_bps": 28.0,
  "action": "shadow_sell_full"
}
```

---

## 10. Phased delivery

### Phase 0 — this paper  
Concept + thresholds draft.

### Phase 1 — Shadow telemetry  
- WS book_ticker + trades for open symbols  
- Compute F1–F5, log stress events  
- No orders  

### Phase 2 — Shadow + counterfactual  
- Join logs to later marks: “if sold at stress, PnL vs actual exit”  
- Tune thresholds offline  

### Phase 3 — Live soft (staging)  
- `mode=live` for henry and/or default  
- Only `gain_pct ≥ 3` + stress  
- Kill switch env `LOB_EXIT_MODE=shadow|off`  

### Phase 4 — Entry apply_to cmc  
- Harden venue on CMC buys  

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Spoof walls | Prefer band depth + tape; don’t trust single huge level |
| WS disconnect | Fail-open (no LOB exit); keep ATR/TTP |
| Over-trading / chop | Require green + peak drop; cooldown per symbol |
| Load / rate limits | Cap symbols; book_ticker before full L2 |
| Thin book noise | Medians over 60s; two-of-N stress rules |

---

## 12. Open questions

1. Shadow-only for 7–14d before any live fire?  
2. Henry first (more rotation-hungry) vs both tenants?  
3. Partial 50% vs full close on first stress?  
4. Store rings only in memory or sample to Redis for multi-instance?  
5. Reuse `venue_quality` config block vs new `lob_exit` top-level config?

---

## 13. References (internal)

- COTI ledger: buy `cmc` ~01:52, default sell `exit_ws`/`trailing_stop` ~06:32, henry `auto`/`trailing_stop` ~07:10  
- `services/venue_quality.py` — buy-path venue stamp  
- Exit path: `services/exit_realtime/`, trailing stop `strategies/trailing_stop.py`  
- Research patterns: `plans/research-jesse-inspired-v0.md`  
- Correlated US-stock experiment (separate): worktree `experiment/correlated-tier-rotation-v0`  

---

## 14. One-line summary

**Use Gate book_ticker + trades (and light L2) to compute spread, microprice skew, near-touch imbalance, bid-depth collapse, and aggressor sell_ratio; fire early green exits when peak is already giving back — without waiting for ATR stop at entry.**

---

*v0 concept paper. Implementation starts at Phase 1 when approved.*
