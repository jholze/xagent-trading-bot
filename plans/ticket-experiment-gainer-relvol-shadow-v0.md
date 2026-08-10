# Ticket: Gainer RelVol shadow detector v0

| | |
|--|--|
| **Typ** | **feature** (shadow discovery; log-only, no capital) |
| **Branch** | `feature/gainer-relvol-shadow-v0` |
| **Priorität** | **Hoch** (unlocks Klasse-B discovery design) |
| **Scope** | Staging / paper (demo ledger); market data global |
| **Horizon** | 5–10 Tage Shadow → scorecard; **no live buys** in v0 |
| **Status** | Implementing |
| **Related** | Früherkennungs-Befund 2026-08-10; `ticket-experiment-gainer-catch-v1.md` (Klasse A only) |

## Problem

### Klasse B — Thin-Vol Ignition (strukturell unsichtbar)

Aus `auswertungen/gis/2026-08-10_gainer-frueherkennung-analyse.md` + `sniper_10d_20260809_094539.json`:

| Coin | Muster | Filter-Kette |
|------|--------|--------------|
| **IMU** | ~+$240 % Tag; vor Spike 24h-Vol ~$3–20k | `min_volume_usdt_24h=500k` → erst spät sichtbar; dann oft &gt; `live_heat_max_pct` (40) |
| **HEI** | großer Einzeltag +108 % im Compound-Pfad | gleiches Muster dünn → dick |
| **BMT** | +84 % Leader-Tag | gleiches Muster |

**Kern:** `scanner.py` verwirft Paare mit `quote_vol_24h < min_volume_usdt_24h` **vor** Ranking/Heat.  
Caps (`max_open` / `max_buys_per_day`) und Inject-Limits werden für diese Coins **nie** erreicht — sie waren nie eligible.

### Was funktioniert hätte (Hypothese, n=3+1 Stichprobe)

**Relatives Volumen vs. eigene Baseline**, nicht absolutes 24h-Vol und nicht primär Tagesreturn:

> Erste Stunde mit `quote_vol_1h > K × median(quote_vol_1h, last N hours)` **und** grüne Kerze → „Zündung“.

| Variante (Befund) | IMU / HEI / BMT | AKE Kontrolle |
|-------------------|----------------|---------------|
| 1h &gt;10× med 12h | trifft alle drei | kein Signal |
| 3h &gt;5× | trifft + AKE noise | false positive risk |
| 6h roll &gt;10× | trifft, ehrlicher Timing | kein Signal |

**Einschränkung des Befunds:** n klein, Survivorship, 10d Melt-up — **Trefferquote über volles Universum unbekannt**. Deshalb **Shadow first**.

### Chase / Heat (Nuance für Design)

- `live_heat_max_pct` blockt späte Preis-Returns.  
- `chase_guard` greift in Config nur bei **`gate_prev_top`**, nicht live_heat — trotzdem Redesign später (Zündungsdistanz vs. prev close).  
- Für v0: nur **loggen**, keine Chase-Änderung.

## Hypothesis

If we log RelVol ignition events over the **full** Gate USDT universe (or top-by-vol-with-tail thin names) for 5–10 days **without trading**, we can measure:

1. Hit rate vs next-day / same-day Gate leaders (Top-20 / Top-5)  
2. False-alarm rate and MAE if we had entered at signal close  
3. Lead time of RelVol vs first time coin would pass 500k absolute vol  

That either **confirms** RelVol as discovery layer or **kills** it before any capital risk.

## Scope v0 (Shadow only)

### In

| Item | Detail |
|------|--------|
| Detector variants | Start with **A:** 1h vol &gt; **10×** median(last **12** 1h bars), green candle; log also **B:** 6h roll &gt;10× for comparison |
| Universe | Gate USDT spot; exclude leverage suffixes (3L/3S/…); **do not** require 500k for *detection* |
| Output | JSONL or Mongo collection e.g. `gainer_relvol_shadow` / append `logs/gainer_relvol_shadow.jsonl` |
| Fields per event | `symbol`, `ts`, `variant`, `vol_1h`, `baseline_med`, `factor`, `close`, `pct_from_local_open_optional`, `abs_vol_24h_at_signal`, `would_pass_min_vol_500k` |
| Join (daily job) | vs day leaders from existing GIS / sniper method; mark `became_top20_within_24h/48h`, `max_ret_forward_12h/24h`, `mfe`, `mae` if entry@close |
| Mode | **shadow only** — no orders, no DE, no inject into trade |

### Out of v0

| Item | Why |
|------|-----|
| Live buys / size rules | After scorecard |
| Changing `min_volume_usdt_24h` in production path | After evidence |
| Full heat/chase rewrite | Separate PR after shadow |
| Henry-specific | Optional later; market data is shared |
| Catch-v1 caps experiment | Orthogonal (Klasse A) |

## Success metrics (after 5–10d shadow)

| KPI | Pass (directional) | Kill RelVol idea |
|-----|--------------------|------------------|
| Precision: signals → Top-20 within 24–48h | clearly &gt; random baseline | ≈ random / noise |
| Capture: share of Top-5 / big-day leaders that had prior RelVol signal | meaningful uplift vs no signal | almost never fires before leaders |
| Median MFE 12–24h after signal (paper) | positive skew | median MFE ≤0 with fat left tail |
| MAE 12h | controlled vs 3h&gt;5× variant | catastrophic without filter |
| Variant A vs B | pick one for v1 trade design | both useless |

Document **regime** (melt-up vs quiet) in scorecard.

## Design notes (implementation sketch)

1. **Data:** reuse Gate tickers / 1h candles path used by gainer scanner or WS board seed; thrifty rate limits (batch, cache).  
2. **Baseline:** rolling median of prior 12 completed 1h quote volumes (exclude current bar until close — **bar close only** to avoid look-ahead).  
3. **Dedupe:** one event per symbol per N hours (e.g. 6–12h cooldown) after fire.  
4. **Integration point:** prefer sidecar or gainer-signal process; bot core stays clean. WS board already `shadow` — optional hook for logging only.  
5. **Absolute vol at signal:** always log; later size = f(abs liquidity), discovery = RelVol.  
6. **Config (proposed, all shadow):**

```json
"gainer_relvol_shadow": {
  "enabled": true,
  "mode": "shadow",
  "variants": ["1h_10x_12hmed", "6h_roll_10x"],
  "min_factor": 10,
  "baseline_hours": 12,
  "require_green": true,
  "cooldown_hours": 8,
  "log_path": "logs/gainer_relvol_shadow.jsonl",
  "exclude_leverage_suffixes": ["3L","3S","5L","5S","UP","DOWN","BULL","BEAR"],
  "_doc": "Discovery shadow only. Kill: enabled=false. No trade path in v0."
}
```

## Implementation plan

1. [ ] Ticket review  
2. [ ] Branch from staging  
3. [ ] Implement shadow logger + config + unit tests (synthetic bars: quiet→spike fires; liquid stable does not)  
4. [ ] Deploy staging (gainer-signal or bot sidecar — pick one process owner)  
5. [ ] Run 5–10d  
6. [ ] Scorecard script → `auswertungen/gis/relvol_shadow_*.md`  
7. [ ] Decision: kill / trade-design v1 (vol split + optional heat on RelVol)  

## Downstream (only if shadow passes)

Ordered as in Früherkennungs-Befund:

1. **Split volume filter:** discovery (RelVol) vs order size (absolute vol floor per ticket size)  
2. **Heat/chase:** distance to ignition (or relax max for RelVol-sourced only)  
3. **Hold/exit** productive for runners (do not multiply half-trades)  
4. Tiny staged capital on RelVol with hard size cap  

## Kill / rollback

| Signal | Action |
|--------|--------|
| Logging cost / rate-limit pain | disable flag; reduce universe |
| Shadow precision terrible | **kill** RelVol product path; keep analysis artifact |
| Ops noise | raise min_factor / cooldown |

Rollback: `gainer_relvol_shadow.enabled=false` or remove deploy.

## Risks

| Risk | Mitigation |
|------|------------|
| Look-ahead in bar construction | signal only on **closed** 1h |
| API cost | throttle; subset if needed; reuse bulk |
| False confidence from 10d melt-up | multi-regime note; compare to quiet days |
| Scope creep into live | v0 PR review: no order path |

## Non-goals

- Replacing gainer-catch-v1 (Klasse A caps)  
- Live sniper on $3k/24h names without size rules  
- Claiming +200 % expectancy from n=3 illustration  

## Acceptance

- [ ] Shadow events landing for real Gate pairs  
- [ ] Scorecard vs leaders for ≥5 full days  
- [ ] Written keep/kill for RelVol discovery  
- [ ] No production trades from this ticket  

## References

- `auswertungen/gis/2026-08-10_gainer-frueherkennung-analyse.md`  
- `auswertungen/gis/sniper_10d_20260809_094539.json`  
- `auswertungen/gis/2026-07-31_to_2026-08-09_sniper-gate-leaders-vs-bot.md`  
- `services/gainer_universe/scanner.py` (hard `min_volume_usdt_24h`)  
- `config.json` → `gainer_universe.min_volume_usdt_24h`, heat, chase  
- Sibling (Klasse A caps): `plans/ticket-experiment-gainer-catch-v1.md`  
