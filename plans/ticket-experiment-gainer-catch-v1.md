# Ticket: Staging experiment gainer-catch v1

| | |
|--|--|
| **Typ** | **experiment** / config (staging-first; not a bugfix) |
| **Branch** | ships with `feature/gainer-relvol-shadow-v0` (same PR) |
| **Priorität** | Mittel–Hoch (bandwidth for **already liquid** leaders) |
| **Scope** | **default** (`config.json`) + **henry** (Mongo `tenant_configs`) — same knobs |
| **Horizon** | 3–5 Tage messen, dann keep / rollback / tune |
| **Status** | Implementing — **scope narrowed after Früherkennungs-Befund** |
| **Related** | `ticket-experiment-gainer-relvol-shadow-v0.md` (discovery); grid-share v1; GIS-14; hold/exit follow-up |

## Zwei Coin-Klassen (wichtig)

| Klasse | Beispiele (10d Fenster) | Primärer Blocker | Löst **dieses** Ticket? |
|--------|-------------------------|------------------|-------------------------|
| **A — Liquide Persistenz** | TUT, SKYAI, BICO, HFT, CYS, SQD (wenn schon ≥~500k vol) | Inject-Cap, entry caps, DE HOLD, WQE, heat 12–40, Attention/Grid | **Ja (teilweise)** |
| **B — Thin-Vol Ignition** | IMU, HEI, BMT vor Spike | `min_volume_usdt_24h=500k` → unsichtbar bis spät; dann oft `live_heat_max_pct` | **Nein** |

**Früherkennungs-Befund** (`auswertungen/gis/2026-08-10_gainer-frueherkennung-analyse.md`, JSON `sniper_10d_20260809_094539.json`):

- 200 Leader-Slots: **12,5 %** same-day buy, **66 %** nie berührt; Gainer-Sleeve nur **4** Leader-Slots  
- Buy-Sources auf Leader-Slots: **grid 20**, dca 2, gainer_live_heat 3, gainer_rank_entry 1  
- **82–91 %** der Top-5 waren in ≤3 Vortagen schon im Gate Top-20 (Persistenz)  
- IMU/HEI/BMT: 24h-Vol vor Zündung **weit unter 500k** → strukturell unsichtbar; Caps greifen nie  

**Konsequenz:** Caps/Inject heben ist **nicht** die Lösung für Klasse B.  
Dieses Ticket adressiert nur: *„Bekommen wir mehr echte `gainer_*`-Fills auf Klasse-A-Leadern, wenn Pipeline-Bandwidth steigen?“*

Klasse B → **`ticket-experiment-gainer-relvol-shadow-v0.md`**.  
Exit/Hold (VANRY same-day sell, BLESS trail) → separates Hold-Ticket; **vor** massivem Entry-Upsize produktiv priorisieren.

## Problem (Klasse A)

| Observation | Evidence |
|-------------|----------|
| Leader oft **gesehen** (observe) | Logs / inject / WQE on liquid names |
| Gainer-tagged buys rare | ~0–1/day; henry ~0/7d; 10d sleeve 12 buys, precision@5 ~17 % |
| Caps / inject eng | `expand_inject_max=12`, `max_open=3`, `max_buys_per_day=6` |
| DE confirm | `require_de_confirm=true` → oft HOLD |
| Heat band | 12–40 % (staging `live_heat_max_pct=40`) — späte Parade out |
| Push 409 | caps / not_eligible / DE path |
| WS board | `shadow` — no auto-buy |

### Chase-Guard (Nuance)

- Config: `chase_guard_sources: ["gate_prev_top"]` only — **nicht** live_heat.  
- Schmerzt **Continuation / Prev-Top-Folgetag**, nicht primär Thin-Vol-Erstzündung.  
- `continuation_max_chase_pct_today=15` ist ein zweiter, verwandter Hebel in `build_eligible`.

## Hypothesis (narrow)

If we **widen inject + entry caps** slightly and allow a bit more heat headroom (**config only**), then for **Klasse A**:

- Gainer-tagged buy count and share rise  
- Overlap with liquid Gate top-20 / multi-day leaders improves  
- DE confirm stays on (no raw rank-buy)

**Does not claim:** catching IMU-class ignition, fixing exits, or that caps were the only miss reason overall.

## Hebel (Config only — moderate unlock)

| # | Knob | Before | Experiment | Why |
|---|------|--------|------------|-----|
| 1 | `gainer_universe.expand_inject_max` | `12` | **`25`** | More liquid leaders in trade expand |
| 2 | `gainer_universe.trade_max_with_expand` | `50` | **`60`** | Room beside grid/open lots |
| 3 | `gainer_entry.max_open` | `3` | **`6`** | More concurrent gainer positions |
| 4 | `gainer_entry.max_buys_per_day` | `6` | **`10`** | Fewer day-cap 409s |
| 5 | `gainer_universe.live_heat_max_pct` | `40` | **`45`** | Slightly later heat still eligible |
| 6 | Docs | — | `_experiment_gainer_catch_v1` | Kill/rollback clear |

### Explicitly **out of scope** for v1

| Item | Ticket / why |
|------|----------------|
| Thin-vol discovery / RelVol | **relvol-shadow-v0** |
| Split vol filter (discover vs size) | after shadow green |
| `require_de_confirm=false` | blowup risk |
| WQE avoid_new hard-disable | code path later |
| Chase rework (ignition distance) | with RelVol / entry redesign |
| Exit/hold for gainer lots | **hold ticket** (priority) |
| WS board shadow→trade | GIS-14 / after metrics |
| Production | staging only |

### Optional Phase 1b (only if after 48h still ~0 **Klasse-A** gainer fills)

| Knob | Change |
|------|--------|
| `live_heat_min_pct` | 12 → **10** |
| `expand_inject_max` | 25 → **30** |

## Tenants

| Tenant | Config path | Action |
|--------|-------------|--------|
| **default** | Service `config.json` | Deploy branch |
| **henry** | Mongo `tenant_configs.body` deep-merge | Same knobs; backup before patch |

Prefer **both same** for sample size. A/B (henry control) optional.

## Success metrics (3–5d, both tenants)

Baseline = last 3–7d pre-deploy + 10d sniper JSON as context.

| KPI | Baseline (approx) | Target (Klasse A) |
|-----|-------------------|-------------------|
| Gainer-tagged buys / day | ~0–1 default; ~0 henry | **≥ 2–4** default |
| Gainer-buy share of all buys | very low vs grid | **≥ 10–15 %** |
| Overlap liquid Top-20 with `gainer_*` | low | **≥ 2–3 coins / 3d** |
| Multi-day leaders (e.g. TUT/SKYAI-class) with gainer path | rare | **any** early or same-day gainer touch on ≥1 such name |
| 409 rate (cap-related) | high | **down** (log sample) |
| Median hold gainer lots | often short | **not worse**; ideal &gt;6–12h |
| Gainer-sleeve expectancy | — | **not clearly &lt;0** over ≥8–10 RTs |

**Not a success criterion for this ticket:** catching IMU/HEI/BMT-class pre-500k-vol (that's relvol-shadow).

### Day-0 / daily

- [ ] `gainer_*` buys 7d, open gainer count, caps  
- [ ] Liquid top-20 snapshot  
- [ ] Deploy timestamp  
- Daily: new gainer fills, 409 samples, blowups −20 % &lt;2h  

## Kill / rollback

| Signal | Action |
|--------|--------|
| ≥3 severe blowups on gainer entry | Immediate rollback |
| Expectancy clearly &lt;0 at ≥8–10 RTs | Rollback or tighten caps |
| Still ~0 Klasse-A gainer fills | **Stop** expanding caps → DE/WQE; do **not** claim RelVol solved by caps |
| Cash chaos | inject + max_open down first |

### Rollback values

```json
"gainer_universe.expand_inject_max": 12,
"gainer_universe.trade_max_with_expand": 50,
"gainer_universe.live_heat_max_pct": 40,
"gainer_entry.max_open": 3,
"gainer_entry.max_buys_per_day": 6
```

Henry: `tenant_configs_backups` pre-patch restore.

## Implementation plan

1. [ ] Branch `experiment/gainer-catch-v1` from `origin/staging`  
2. [ ] Patch `config.json` + experiment notes  
3. [ ] Henry patch script (dry-run / --apply)  
4. [ ] PR → staging deploy  
5. [ ] Day-0 baseline  
6. [ ] Day 3–5 scorecard → keep / 1b / kill  

## Risks

| Risk | Mitigation |
|------|------------|
| Chase under 45% heat | +5 max only; DE stays on |
| More bags | max_open 6 not 15 |
| Exit kills edge | track hold KPI; hold ticket parallel |
| False hope for Klasse B | documented non-goal; relvol ticket |

## Non-goals

- Thin-vol ignition (IMU class)  
- Production  
- Replacing RelVol discovery  
- Auto-lock after every gainer fill  

## Follow-ups

1. **`ticket-experiment-gainer-relvol-shadow-v0`** — RelVol shadow over full universe  
2. **Gainer hold v1** — trail/full-close / lock for `gainer_*`  
3. Vol filter split (discover vs size) after shadow  
4. Heat/chase relative to ignition / prev-close redesign  
5. DE soft-confirm / WQE avoid for heat leaders  

## Acceptance

- [ ] Ticket reviewed (scope = Klasse A only)  
- [ ] Branch + config + Henry  
- [ ] Staging deploy + Day-0  
- [ ] ≥3d metrics + keep/kill  
- [ ] No claim of solving Klasse B  

## References

- Früherkennung: `auswertungen/gis/2026-08-10_gainer-frueherkennung-analyse.md`  
- 10d data: `auswertungen/gis/sniper_10d_20260809_094539.json`  
- Config: `gainer_universe`, `gainer_entry`  
- Code: `scanner.py` (`min_volume`, `build_eligible`), `chase_guard.py` (prev_top only), `bot_http.py`  
- Sibling: `plans/ticket-experiment-gainer-relvol-shadow-v0.md`  
