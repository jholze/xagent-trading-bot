# Experiment GIS-14 — Gate Identify & Sell (14 Tage)

**Status:** IMPLEMENTING → branch `feat/gainer-ws-board-identify` (2026-08-05)  
**Dauer:** 14 Tage ab Merge/Deploy auf Staging  
**Kapital:** `virtual_trading: true` (Paper-Ledger) — **kein** Live-Cash in diesem Experiment  
**Ziel:** Top-Coins auf Gate **erkennen** und **gewinnbringend verkaufen** — nicht Peak-FOMO.

### GIS-14+ shipped in this branch

- REST seeds watch set; **Gate WS** ticks rank board → `logs/gainer_ws_board.jsonl` (shadow, **no auto-buy**)
- Config freeze: inject 12, live_top 25, heat 12–40, chase 15, `ws_board.enabled=true`
- Exit path unchanged (trail/TTP live on open positions)

---

## 1. Haben wir genug?

| Frage | Antwort |
|-------|---------|
| Ziel klar? | Ja — identify + profitable sell |
| 10d Retro da? | Ja — Board, Mixe, Bias dokumentiert |
| Infrastruktur? | Ja — `gainer_universe`, WS exits, chase_guard, rot_mid |
| Eigenes Buy-Signal? | Noch schwach (inject ≠ sniper BUY) — **ok für v1** |
| Messbar 14d? | Ja, wenn Identify-Log + Exit-Tags laufen |

**Fazit:** Genug für **v1 Experiment**. Nicht genug für „perfekten Sniper“ — und das ist für 14 Tage absichtlich so (eine eingefrorene Policy, keine Feature-Orgie).

### Was ich von dir brauche (nur das)

1. **Go** auf dieses Freeze (oder 1–2 Zahlen ändern, siehe §6).  
2. **Umgebung:** Demo/Virtual (empfohlen, config hat schon `virtual_trading: true`) vs. echtes Live-Geld (**nicht** empfohlen für GIS-14).  
3. Optional: ob **`*G` Stock-Tokens** im Experiment **mit** (default) oder **blacklist**.

Mehr brauche ich nicht, um heute zu starten.

---

## 2. Eingefrorene Strategie v1 („GIS-14“)

### One-liner

> **Erkenne** liquide Gate-USDT-Tops (Prev-Day + Live-Heat mit Ceiling).  
> **Handle** nur im engen Gainer-Sleeve.  
> **Verkaufe** per WS-Trail (eher wide) + Zeitlimit — nicht am Peak jagen.

### Sleeve-Mix (fest, 14 Tage nicht drehen)

| Anteil | Name | Identify | Enter (über bestehende Pipeline) | Exit |
|--------|------|----------|----------------------------------|------|
| **50%** | **Continuation** | gestern daily top (existiert) | eligible + DE/Sensor/Risk | WS trail + chase_guard |
| **50%** | **Live Heat** | 24h% im Band, sticky/rank via board | heat inject + prefer scan | WS trail mid/wide |

**Anti-Peak (hart):**

- Live heat nur **12–40%** 24h (nicht 8–35 weich nach oben offen ohne Ceiling)  
- Heat max **40%** (block parabolic new)  
- Prev-day chase_guard **an** (15–18% from prev close)  
- Keine 3L/5L  
- Vol ≥ **500k** USDT  

**Nicht in v1:** Accel-BUY-Signal neu, full WS board, path_stats bias, sniper 3-slot hard counter (Mess-Proxy: manuell/logs).

### Config-Freeze (Zielwerte)

```json
"gainer_universe": {
  "enabled": true,
  "mode": "trade_expand",
  "poll_sec": 60,
  "min_volume_usdt_24h": 500000,
  "live_top_n": 25,
  "expand_inject_max": 12,
  "trade_max_with_expand": 50,
  "live_heat_trade": true,
  "live_heat_min_pct": 12,
  "live_heat_max_pct": 40,
  "live_heat_ttl_hours": 8,
  "enable_continuation": true,
  "continuation_max_chase_pct_today": 15,
  "chase_guard_enabled": true,
  "chase_max_gain_from_prev_close_pct": 15,
  "scan_prefer_gainer": true
},
"exit_realtime": {
  "enabled": true,
  "mode": "live"
},
"exit_rotation": {
  "enabled": true,
  "profile": "rot_mid"
}
```

**Interpretation vs heute:** engeres Board (25), viel weniger inject (12 statt 55), Heat 12–40, chase 15%, WS exits an, rot_mid an.

### Exit-Policy (v1)

| | |
|--|--|
| Primär | `exit_realtime` WS: TTP + trailing_stop |
| Profile | `rot_mid` (früher arm als base — schon an) |
| Geist | Auf Gainer-Tagen **nicht** trail_tight; lieber mid + Zeitrotation |
| v1.1 (optional nach Tag 7) | gainer-tag → etwas **weiterer** trail — nur wenn Giveback-Messung es fordert |

---

## 3. Was der 14-Tage-Test **beweisen** soll

| Hypothese | Messgröße | Pass |
|-----------|-----------|------|
| H1 Identify | Overlap: daily logged top-15 vs EOD top-8 (nachträglich Script) | ≥ 35% hit |
| H2 No peak FOMO | Median entry 24h% der gainer-source buys | **≤ 35%** |
| H3 Sell quality | Median giveback peak→exit auf gainer-tagged | **≤ 50%** of peak gain |
| H4 WS works | Anteil exits mit exit_ws / trail | **≥ 40%** |
| H5 Edge | Gainer-source round-trips after fees (virtual) | expectancy **≥ 0** oder klar lernen |
| H6 Safety | Leverage fills | **= 0** |

---

## 4. Betriebsregeln (14 Tage)

**Do**

- Config Freeze committen/taggen (`experiment/GIS-14`)  
- Täglich 5-Min Check: gainer refresh log, exit_ws ticks, keine 3L  
- 1× am Ende: `retrospect_gate_top10d_strategies.py` + Identify-Hit aus Logs  

**Don’t**

- Mitten im Test heat band / inject / trail wild drehen  
- Parallel 3 andere Exit-Experimente  
- Live-Cash ohne explizites neues Go  

**Kill (sofort mode abschwächen)**

- 3 Tage in Folge gainer-sleeve −2%+ virtual NAV attributed  
- Leverage fill  
- exit_realtime down > 2h ohne Fallback-Verständnis  
- >50% gainer entries mit entry 24h% > 45% → Peak-FOMO, Heat max senken oder heat off  

**Rollback**

```text
gainer_universe.mode = "shadow" | enabled false
# exits: exit_realtime bleibt idealerweise an
```

---

## 5. Minimal-Build heute (empfohlen, klein)

Damit der Test **auswertbar** ist:

| # | Item | Pflicht? |
|---|------|----------|
| 1 | Config-Freeze wie §2 | **Ja** |
| 2 | JSONL: jedes Scan top-15 + heat/prev eligible snapshot | **Ja** (sonst H1 blind) |
| 3 | Fill-Meta: source, gainer_rank, pct_24h wenn vorhanden | Schön / wenn schon da loggen |
| 4 | Eigenes sniper BUY signal | **Nein** für v1 |
| 5 | Board-WS | **Nein** für v1 (nach 14d entscheiden) |

Geschätzt: Config **30 min**; Identify-Snapshot-Log **2–4 h** wenn sauber.

---

## 6. Tunable (nur vor Start, dann freeze)

| Knob | Default GIS-14 | Alt (konservativer) |
|------|----------------|---------------------|
| live_heat_min/max | 12 / 40 | 15 / 35 |
| expand_inject_max | 12 | 8 |
| chase_max | 15 | 12 |
| *G tokens | allow | blacklist_name / bases |
| virtual | true | — |

---

## 7. Zeitplan

| Tag | Aktion |
|-----|--------|
| **0 (heute)** | Freeze + Config + Snapshot-Log deploy; Start markieren |
| 1–3 | Nur beobachten, keine Knob-Änderungen |
| 7 | Mid-check: H2/H4/H6; nur Kill greift |
| 14 | Full report; Go/No-Go für v1.1 (WS board / sniper signal / trail overlay) |

---

## 8. Entscheidung

- [ ] **GO GIS-14** mit Defaults  
- [ ] GO mit Änderungen: _______________  
- [ ] NO-GO — brauche noch: _______________  

Nach GO: Config anwenden + Identify-Log + Start-Timestamp in diesem Doc nachtragen.
