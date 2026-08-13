# Experiment: Correlated-Tier + Stagnant-Rotation v0

**Kill:** `ctexp` Tenant `sell_policy.correlated_tier.enabled=false` **und** `sell_policy.rotation.stagnant_rotation_enabled=false` (Mongo `tenant_configs`).  
Global in `config.json` bleiben beide Flags **false** — Henry/default sind nicht das Experiment.

## Steckbrief

| | |
|---|---|
| **Status** | Code auf Branch, **nicht** auf `staging`. Tenant `ctexp` + Overlay liegen in Mongo. Headless-Cycle-Tor (#260) ist auf Staging live. |
| **Slug** | `correlated-tier-rotation-v0` |
| **Branch** | `experiment/correlated-tier-rotation-v0` (rebased auf `staging` @ `bb1fabb` / #260) |
| **Tenant** | **`ctexp` only** (paper, `telegram.headless=true`, Prefix `[ctexp]`) |
| **Kontrollgruppe** | `default` + `henry` — Flags aus, `max_open` 36 |
| **Umgebung** | Railway `trading-bot` / Env **test** / `xagent-test` / Mongo `xagent_test` |
| **GitHub** | [#263](https://github.com/jholze/xagent-trading-bot/issues/263) |
| **Nicht** | PR #260 (nur Cycle ohne Telegram-Chat). Epic #261 (RelVol/Risk/Slots-Aufräumen). LOB #251. Jesse-Research #250. |

## Hypothese

Wenn verwandte Positionen **gemeinsam** fallen (US-Stock-Korb oder BTC/ETH als Markt-Proxy), verkauft der Bot **früher und als Gruppe** statt Coin für Coin hinterherzuhinken.  
Wenn das Buch fast voll ist, gibt eine **grün-aber-tote** Position den Slot frei, bevor schlechtere Neueinsteige an `max_open` sterben.

Prüfbar auf `ctexp` vs Henry über 14 Tage Paper: Peak-Capture, Giveback, Slot-Rejects, Stagnant-Fires, Realized PnL. Nicht über Bauchgefühl.

## Zwei Mechanismen (unabhängig, ein Overlay)

### 1. Correlated-Tier — Gruppen-Dump + Trail-Overlay

Config: `sell_policy.correlated_tier` (Default **`enabled: false`**).

| Gruppe | Proxy (Sensor) | Mitglieder | Trigger |
|---|---|---|---|
| `us_stock` | CRWVG, NBISG, SOXLG, MVLLG | genau diese 4 | ≥2 fallen ≥5 % in 10 min |
| `crypto_market` | BTC, ETH | `*` (Rest, keine Proxies) | ≥1 Proxy fällt ≥4 % in 15 min |

Wenn das Flag **an** ist:

- Hintergrund-Tracker schreibt ein kurzlebiges Selloff-Flag (~30 s TTL).
- **Risk** blockt neue BUYs auf betroffene Symbole (`code=correlated_tier_selloff`).
- **Stagnant-Rotation** halbiert Gain- und Idle-Schwelle, solange das Flag steht.
- **Trail-Overlay** nur für die Gruppe. `us_stock`: engeres TTP (`trail_pct` 3.5, Arm 10, Min 8), TS ab 8 %, Full-Close ab +12 %. `crypto_market` hat in v0 **kein** Trail-Overlay, nur den Dump-Trigger.
- Amplifier (Regime RISK_OFF/CRASH, News-Pulse) existieren im Code, Default **aus**. Nicht Teil von v0 live.

### 2. Stagnant-Rotation — Slot-Recycling

Config: `sell_policy.rotation.stagnant_*` (Default **`stagnant_rotation_enabled: false`**).

Feuert `SELL_FULL` / `source=stagnant_rotation` wenn:

1. Buch fast voll: `open_full_slots >= max_open − slack`
2. Gain ≥ Schwelle (grün)
3. Idle seit letztem **Peak** (`peak_at`, nicht jeder Partial/DCA-Fill) ≥ Stunden

Auf `ctexp` (Overlay `_experiment_ctexp_v0`):

| Knopf | Henry / default | `ctexp` |
|---|---|---|
| `max_open_positions` | 36 | **18** |
| `correlated_tier.enabled` | false | **true** |
| `stagnant_rotation_enabled` | false | **true** |
| `stagnant_slack_slots` | 2 | **8** (greift ab 10 offenen Full-Slots) |
| `stagnant_gain_pct` | 8 | **6** |
| `stagnant_idle_hours` | 24 | **12** |

Gruppen-Definitionen (`us_stock` / `crypto_market`) erbt `ctexp` unverändert aus `config.json`.

## Isolation

- `config.json` auf Disk: beide Master-Flags **false**. Amplifier **false**.
- Live-Experiment = **nur** Mongo `tenant_configs` für `ctexp`.
- Cycle: `iter_price_cycle_tenants` nimmt `ctexp` wegen `telegram.headless=true` (#260). Ohne Headless-Flag wäre der Tenant unsichtbar.
- RelVol-Push-Env bleibt `default,henry`. RelVol ist **nicht** Teil dieses Experiments.
- Setup: `scripts/setup_ctexp_tenant.py` (Paper-Klon + headless). Overlay: `scripts/patch_ctexp_correlated_tier_v0.py` (schreibt Backup nach `tenant_configs_backups`).

## Was schon da ist / was fehlt

| Stück | Wo |
|---|---|
| Code + Tests | Branch `experiment/correlated-tier-rotation-v0` |
| Tenant + Overlay + `headless=true` | Staging-Mongo `xagent_test` |
| Cycle-Tor ohne Telegram | Staging live via #260 |
| Code auf Railway Staging | **nein** — ohne Merge handelt `ctexp` nur mit `max_open=18` |
| Amplifier live | **nein** (und sollen in v0 aus bleiben) |
| Production / `xagent-bot` | **nein** |

## Backtest (nur Kontext, kein Go-Beweis)

90 Tage, 2026-05-14 → 2026-08-12. Reports: `auswertungen/gis/2026-08-12_correlated-tier-backtest-90d-phase*.md` und Phase-3 Opportunity Cost.

- **1h:** Experiment etwas weniger schlecht als Baseline (−14.6 % vs −15.1 % realisiert). Ein Pfad, kein Sweep.
- **4h:** Experiment schlechter als Baseline.
- **Phase 3:** Abgelehnte BUYs wegen vollem Buch waren im Mittel **schlechtere** Holds als genommene. Slots binden (~9 % der Fills), sind aber nicht der dominante Filter (Illiquidität/Cash größer).
- Stagnant-Rotation hat im Replay **kaum** gefeuert.

Deshalb Forward-Test auf **einem** Paper-Tenant, nicht auf Henry.

## Erfolg / Kill (14 Tage Paper ab Code-Deploy)

**Erfolg** (mindestens eines, ohne Henry zu verschlechtern):

- `us_stock`-Korb: weniger Giveback nach Cluster-Dumps als Henry im gleichen Fenster, **oder**
- Stagnant-Fires ≥ 3 und nachfolgende Fills nicht systematisch schlechter als die verkauften Lots, **oder**
- Slot-Rejects (`max_open_positions`) auf `ctexp` klar niedriger bei vergleichbarem oder besserem PnL.

**Kill:**

- `ctexp` Drawdown oder Realized PnL deutlich schlechter als Henry bei gleichem Markt, **oder**
- Stagnant verkauft Gewinner, die Henry noch sauber nachzieht, **oder**
- Selloff-Flag blockt Buys, ohne dass die Gruppe danach fällt, **oder**
- 14 Tage keine einzige sinnvolle Fire (dann ist das Overlay tot, nicht „noch zu kurz“).

Revert: Backup in `tenant_configs_backups` oder Overlay `enabled: false` / `stagnant_rotation_enabled: false`. Code kann auf Staging bleiben, solange Flags aus sind.

## Related tickets

| Issue | Rolle |
|---|---|
| **#263** | dieses Experiment (Ticket-Home) |
| #260 | **nur** Headless-Cycle. Nicht das Experiment. Bereits gemergt. |
| #261 | RelVol/Risk/Slots-Aufräumen. **Getrennt.** Nicht auf diesen Branch. |
| #89 | Epic Adaptive Cash + Rotation — Parent-Kontext, nicht ersetzen. |
| #92 | Rotation kleiner Gewinne (arm/tail) — verwandt, andere Knöpfe. |
| #94 | Rotation urgency / HARVEST — verwandt. |
| #110 / #111 | Capacity + Slot-Eviction — `ctexp` recycelt **eigene** Lots, evictet nicht sync auf HTTP. |
| #178 / #181 / #183 | Realtime Trail — Overlay hängt an TTP/TS, ändert die WS-Pipeline nicht. |
| #250 | Research-Scorecard später auf `ctexp` vs Henry anwenden. |
| #251 | LOB Dump-Schutz — **Geschwister**, nicht mischen. |

## Ops

```bash
# Code-Review / Tests auf dem Branch
pytest tests/unit/test_correlated_tier_pure.py \
       tests/unit/test_stagnant_rotation.py \
       tests/test_ctexp_setup_scripts.py

# Overlay an/aus (Staging-Mongo, PUBLIC url)
MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \
  python3 scripts/patch_ctexp_correlated_tier_v0.py --dry-run
```

Logs: `[ctexp]` in Telegram; Cycle-WQE/`eval_worker tenant=ctexp`; Sells mit `source=stagnant_rotation` oder Trail nach Gruppen-Overlay.

## Check-Notiz (2026-08-13)

- Rebase auf Staging inkl. #260: sauber, 0 hinter Staging.
- Setup-Script setzt `telegram.headless=true` (Commit `bc0b2e6`).
- Disk-Flags aus. Amplifier aus.
- Unit-Kern (pure / amplifiers / stagnant / setup / headless): grün.
- `tests/unit/test_exit_realtime_live.py` zwei Fails: `UnboundLocalError: log` in `services/exit_realtime/execute.py` — **vor Merge klären**, ob das der Branch oder Staging-Bestand ist.
