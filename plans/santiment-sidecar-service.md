# Santiment Sidecar Service — Plan (Railway Test)

> **Status:** Phase 0–2 in progress (sidecar + bot ingest on staging)  
> **Environment:** Railway project `trading-bot` · Environment **`test`**  
> **Bot:** `xagent-test` (unverändert im Hot-Path; liest nur Snapshots/Events)  
> **Related:** [`arena-market-oracle-service.md`](arena-market-oracle-service.md) · Epic #6 · LC bleibt Social-Bein im Bot  
> **Erstellt:** 2026-07-17  

---

## 0. Security first (sofort, vor Implementierung)

Du hast den Santiment-API-Key **im Chat** gepostet.

| Aktion | Wo |
|--------|-----|
| **Key rotieren** | Santiment Dashboard → API Key revoke + neu erzeugen |
| **Nur in Railway speichern** | Service `xagent-santiment` (Name Vorschlag) → Variable `SANTIMENT_API_KEY` |
| **Nie** in Git, Issues, Telegram, `config.json` | — |
| Bot braucht den Key **nicht** | Nur der Sidecar |

> Der im Chat stehende Key gilt als kompromittiert — nicht in Code/Plan-Dateien übernehmen.

---

## 1. Zielbild

**Separater Railway-Dienst** im **Test**-Environment:

- holt Santiment-Daten (On-Chain, Sentiment, Flows, Network) im eigenen Takt  
- normalisiert zu einem **kleinen, versionierten Snapshot**  
- **pusht nur bei Änderung** (oder Heartbeat) an den Bot  
- **handelt nicht**, berührt **kein** Ledger, **kein** Telegram-Trade-Pfad  

```text
┌─────────────────────┐         ┌──────────────────────┐
│  xagent-santiment   │  HTTP   │     xagent-test      │
│  (Sidecar / Oracle) │────────►│  ingest endpoint     │
│  poll Santiment API │  or     │  Redis key / Mongo    │
│  compute features   │  Redis  │  DecisionEngine reads│
└─────────────────────┘         └──────────────────────┘
```

**Warum getrennt (wie Market-Oracle-Arena):**

| Sidecar | Bot |
|---------|-----|
| langsame, teure Santiment-Polls | schneller Price/TA-Cycle |
| überlebt Bot-Redeploy | liest letzten guten Snapshot |
| Rate-Limits isoliert | kein 1Hz-XAUT-Stil im Trading-Loop |
| eigene Credits/Keys | keine Key-Proliferation |

---

## 2. Railway Test — neuer Service

### 2.1 Anlegen

| Feld | Wert (Vorschlag) |
|------|------------------|
| Project | `trading-bot` (gleich) |
| Environment | **`test`** |
| Service name | `xagent-santiment` |
| Region | wie `xagent-test` (US West / sfo) |
| Repo | gleiches Repo **oder** `services/santiment_sidecar/` monorepo-Root |
| Start | `python -m santiment_sidecar` / `Dockerfile` slim |
| Public URL | optional intern only; Ingest lieber **Bot-URL** pushen |

### 2.2 Env Vars (Sidecar)

| Variable | Pflicht | Beispiel / Hinweis |
|----------|---------|-------------------|
| `SANTIMENT_API_KEY` | ja | **nur Railway**, rotiert |
| `BOT_INGEST_URL` | ja | `https://xagent-test-test.up.railway.app/api/santiment/ingest` |
| `BOT_INGEST_TOKEN` | ja | shared secret (= `SANTIMENT_INGEST_TOKEN` am Bot) |
| `POLL_INTERVAL_SEC` | nein | default `900` (15 min) |
| `WATCHLIST_URL` | nein | optional: Bot `GET /api/internal/watchlist` mit Token |
| `REDIS_URL` | optional | falls Publish statt HTTP |
| `LOG_LEVEL` | nein | INFO |
| `DRY_RUN` | nein | `1` = log only, kein Push |

### 2.3 Env Vars (Bot `xagent-test`)

| Variable | Pflicht | Hinweis |
|----------|---------|---------|
| `SANTIMENT_INGEST_TOKEN` | ja | gleich wie Sidecar |
| `SANTIMENT_SNAPSHOT_ENABLED` | nein | default true nach Go |
| (kein) `SANTIMENT_API_KEY` | — | **nicht** im Bot |

### 2.4 Networking

**Empfohlen (Phase 1):** Sidecar → **HTTP POST** an öffentlichen Bot-Ingest (wie Signal-Webhook), Token im Header.

**Phase 2 (optional):** Redis Pub/Sub `aria:santiment.snapshot` (gleicher Redis wie Bot im Test), Bot subscribed / liest last key `aria:santiment:latest`.

Private Railway-Netz: möglich, aber HTTP+Token reicht für Test und ist debuggbarer.

---

## 3. Verantwortlichkeiten (hart trennen)

### Sidecar **darf**

- Santiment GraphQL/REST pollen  
- Features berechnen (BTC funding proxy, exchange flow score, social volume delta, …)  
- State: `RISK_ON | NEUTRAL | RISK_OFF | CRASH` **oder** weichere Scores  
- Snapshot mit `as_of`, `version`, `confidence` speichern (lokal/Redis)  
- Bot nur bei **Diff** oder Heartbeat pushen  

### Sidecar **darf nicht**

- Orders, Ledger, Mongo positions  
- Telegram-Trade-Commands  
- Tenant-Ledger schreiben  
- CMC/LC ersetzen (die bleiben im Bot oder eigene Stories)  

### Bot **darf**

- Snapshot lesen  
- `size_mult` / Entry-Sensor policy / max new buys anwenden (über RiskManager / entry guard)  
- In `/market` oder Cycle-Zeile anzeigen  
- Snapshot verwerfen wenn `as_of` zu alt  

---

## 4. Datenvertrag (Bot ↔ Sidecar)

### 4.1 Snapshot JSON (v1)

```json
{
  "schema_version": 1,
  "source": "santiment",
  "as_of": "2026-07-17T19:45:00Z",
  "ttl_sec": 1800,
  "regime": "RISK_OFF",
  "confidence": 0.72,
  "size_mult": 0.35,
  "sensor_policy": "shadow",
  "max_new_entries_per_hour": 2,
  "features": {
    "btc_social_volume_z": -0.8,
    "btc_exchange_inflow_z": 1.4,
    "eth_dev_activity_7d_delta": 0.1,
    "watchlist_onchain_risk_mean": 0.62
  },
  "symbols": {
    "BTC/USDT": { "score": -0.4, "tags": ["exchange_inflow"] },
    "ETH/USDT": { "score": -0.2, "tags": [] }
  },
  "rationale": "BTC exchange inflow elevated; social volume weak",
  "sidecare_build": "gitsha…"
}
```

### 4.2 Change detection

Push nur wenn:

- `regime` gewechselt, **oder**
- `size_mult` Δ ≥ 0.1, **oder**
- Heartbeat alle `HEARTBEAT_SEC` (z. B. 3600) auch ohne Diff  

→ kein Spam, passt zu „nur informieren wenn sich was ändert“.

### 4.3 Bot Ingest API (neu im Bot)

```
POST /api/santiment/ingest
Header: X-Santiment-Token: <shared>
Body: snapshot JSON
```

Antwort:

```json
{ "ok": true, "applied": true, "prev_regime": "NEUTRAL", "regime": "RISK_OFF" }
```

Auth analog `signal_webhook_token` (siehe `services/signal_webhook_service.py`).

Persistenz Bot-seitig (Vorschlag):

| Store | Key / Collection | Inhalt |
|-------|------------------|--------|
| Redis | `aria:santiment:latest` | JSON snapshot |
| optional Mongo | `meta` / `santiment_snapshots` | last N for audit |

---

## 5. Santiment: was der Sidecar holt (MVP vs später)

### Phase S1 — MVP (wertvoll + sparsam)

Fokus **global + BTC/ETH** (wenig Credits, starker Market-Context-Fit):

| Metric (Konzept) | Zweck |
|------------------|--------|
| BTC social volume / sentiment | Risk-On/Off Soft-Signal |
| BTC exchange inflow/outflow (falls Plan) | „Coins aufs Book“-Stress |
| ETH social volume | Breadth |
| Optional: Fear-adjacent social dominance | Context |

State-Maschine: grobe Mapping-Regeln → `regime` + `size_mult` (kalibrierbar, paper).

### Phase S2 — Watchlist enrichment

- Top N Watchlist-Symbole vom Bot holen (intern endpoint oder Redis set)  
- Pro Symbol leichte Scores (nur wenn Plan/API es erlaubt)  
- Bot: optional Score in DecisionEngine als **Filter**, nicht alleiniger BUY  

### Phase S3 — Alignment mit Market Oracle

- Sidecar kann Features an `market_context` füttern  
- Oder fusion: technical regime (Bot/Oracle) × Santiment on-chain  

---

## 6. Repo-Layout (Vorschlag monorepo)

```text
services/
  santiment_sidecar/
    __main__.py          # poll loop
    client.py            # Santiment HTTP/GraphQL
    features.py          # pure feature build
    regime.py            # state machine
    publisher.py         # POST bot / Redis
    config.py            # env
    Dockerfile
    requirements.txt     # minimal: requests, (httpx)
tests/
  unit/
    test_santiment_regime.py
    test_santiment_publisher.py
```

**Deploy:** Railway Root Directory = `services/santiment_sidecar` **oder** Root-Dockerfile mit `CMD python -m services.santiment_sidecar`.

Bot-Änderungen (dünn):

```text
webhooks/ or http handlers: santiment_ingest.py
storage/: last snapshot helpers
risk/ or decision_engine: read size_mult / sensor_policy
config.json: santiment_ingest.enabled (feature flag)
```

---

## 7. Poll-Loop (Sidecar)

```text
every POLL_INTERVAL_SEC:
  1. load watchlist hints (optional, cached)
  2. fetch Santiment metrics (rate-limit aware, backoff on 429)
  3. build features + regime snapshot
  4. if changed or heartbeat due:
       POST BOT_INGEST_URL with token
  5. log: regime, credits used, latency
  6. sleep
```

**Rate limits:** nie im 1s-Takt (XAUT-Lektion). Default 10–15 min.  
**Backoff:** 429 → exponential, max 1h pause, klare Logs.

---

## 8. Bot-Integration (wie Snapshot wirkt)

| Feld | Bot-Wirkung (Vorschlag) |
|------|-------------------------|
| `size_mult` | `RiskManager` multipliziert genehmigte Buy-Size |
| `sensor_policy: shadow` | Entry-Sensor enqueued, aber keine Execute (oder nur shadow log) |
| `sensor_policy: block` | keine neuen Sensor-Buys |
| `max_new_entries_per_hour` | Cap zusätzlich zu daily limits |
| `regime: CRASH` | block all new buys; sells free |
| stale snapshot (`as_of` > 2× ttl) | **ignore** → neutral defaults (fail open or fail closed: Config) |

**Fail-closed vs open (Test):** erst `fail_open` (bei fehlendem Snapshot normal handeln), später optional fail_closed für RISK-Pfade.

---

## 9. Implementierungs-Phasen

### Phase 0 — Setup (30–60 min, kein Code im Bot nötig)

- [ ] Santiment-Key **rotieren**  
- [ ] Railway Service `xagent-santiment` im Env **test** anlegen  
- [ ] `SANTIMENT_API_KEY` + Dummy `BOT_INGEST_TOKEN` setzen  
- [ ] Health: leerer Container mit `python -c "print('ok')"`  

### Phase 1 — Sidecar MVP

- [ ] Client + Poll + Regime stub (auch mock metrics)  
- [ ] Publisher POST (Bot endpoint stub 404 ok mit DRY_RUN)  
- [ ] Unit tests Regime pure functions  
- [ ] Deploy Sidecar; Logs: snapshot every 15 min  

### Phase 2 — Bot Ingest

- [ ] `POST /api/santiment/ingest` + Token  
- [ ] Redis/Memory store latest  
- [ ] `/market` or `/status` line: `Santiment: RISK_OFF size=0.35`  
- [ ] Feature flag off by default → on in test  

### Phase 3 — Risk wire-up

- [ ] RiskManager liest `size_mult`  
- [ ] Entry sensor respects `sensor_policy`  
- [ ] Tests with frozen snapshot  
- [ ] Paper 3–7 Tage Observation  

### Phase 4 — Watchlist symbols (optional)

- [ ] Symbol scores  
- [ ] DecisionEngine soft filter  
- [ ] Never sole BUY source without TA  

---

## 10. Abgrenzung zu bestehenden Komponenten

| Komponente | Rolle | Santiment-Sidecar |
|------------|-------|-------------------|
| LunarCrush (im Bot) | Social Galaxy/AltRank | **nicht ersetzen**; parallel oder später LC-Interval fixen |
| CMC (im Bot) | Market trending / quotes | **nicht** hierher verschieben |
| Signal webhook | External entry alerts | anderes Schema; Santiment = **regime snapshot** |
| Market Oracle Plan | Global RISK_ON/OFF | Santiment ist **eine Feature-Quelle** dafür; kann später fusionieren |

**Empfehlung:** Sidecar **nicht** „Santiment-LC“ nennen — er liefert **Market Context / On-Chain**, nicht denselben Social-Buy-Pfad.

---

## 11. Kosten & API-Disziplin

- Poll **max** 4–6×/h global metrics  
- Symbol-Batch nur Top 10–20 der Watchlist  
- Cache responses 15–30 min  
- Metrics: `santiment_http_calls`, `last_push_at`, `last_regime`  
- Bei 429: backoff, Bot behält last good snapshot  

---

## 12. Observability

| Was | Wo |
|-----|-----|
| Sidecar health | `GET /health` → ok + last snapshot age |
| Bot | Cycle summary: `San: RISK_OFF (12m ago)` |
| Alert | Telegram optional: regime flip only (cooldown 1h) |
| Audit | Redis/Mongo last 50 snapshots |

---

## 13. Risiken

| Risiko | Mitigation |
|--------|------------|
| Key geleakt | Rotate; Railway only |
| Falsche Regime → zu wenig Trades | fail_open + paper calibration |
| Sidecar down | Bot ignores stale; no crash |
| Doppelte Social-Sources thrash | Santiment nur size/sensor, kein alleiniger BUY |
| GraphQL complexity / plan limits | Start global BTC/ETH only |

---

## 14. Done-Kriterien (MVP)

- [ ] Service `xagent-santiment` läuft im Railway **test**  
- [ ] Key nur als Env, rotiert  
- [ ] Snapshot alle 15 min; Push nur bei Diff/Heartbeat  
- [ ] Bot speichert Snapshot und zeigt Regime  
- [ ] Mit Flag: `size_mult` greift in Risk (Test nachweisbar)  
- [ ] Keine Orders aus dem Sidecar  
- [ ] Kein API-Key im Git  

---

## 15. Nächster Schritt

1. **Key rotieren** + in Railway `xagent-santiment` speichern  
2. Service-Skeleton deployen (Phase 0–1)  
3. Bot-Ingest (Phase 2)  
4. Risk wire-up (Phase 3)  

Kein Code in diesem Ticket erzwungen — Go = „Phase 0/1 umsetzen“.

---

## Anhang: Name & Railway UI

```
Services (test)
├── MongoDB-AeF7
├── Redis
├── xagent-test          ← Trading Bot
└── xagent-santiment     ← NEU (Sidecar)
```

Shared: optional gleiche `REDIS_URL` private.  
Separate: `SANTIMENT_API_KEY` nur Sidecar.
