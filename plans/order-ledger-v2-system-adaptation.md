# Order Ledger v2 — Systemanpassung für schnelle `/orders` & `/portfolio`

**Status:** Plan / Architektur  
**Datum:** 2026-07-24  
**Kontext:** Staging `xagent_test`, Multi-Tenant (default, henry), Demo-Ledger Mongo  
**Related:** Epic #119 (Mikro-Perf, unzureichend), #123 (Day-Summary Backlog)

---

## 1. Problem

### 1.1 Datenmodell-Anti-Pattern

Heute: **ein Mongo-Dokument pro Tenant/Scope** mit ungebremstem Array:

```text
orders (collection)
  _id: "henry:demo"
  orders: [ … 1475+ Order-Objekte … ]
```

Das ist der MongoDB-Anti-Pattern **„unbounded array / document grows forever“**:

- Jeder Read lädt die **gesamte Historie**
- Jeder Write riskiert **Rewrite des ganzen Blobs**
- Schlechte Cache-Nutzung, wachsende Latenz, 16 MB-Doc-Limit

### 1.2 Gemessene Latenz (Railway Mongo public proxy, 2026-07-24)

Handler-Code gegen Staging-Mongo (Telegram gemockt wo angegeben):

| Schritt | Henry (1475 orders) | Default (2101 orders) |
|--------|--------------------:|----------------------:|
| Mongo Ping | 1,7 s | — |
| `load_orders` #1 | **5,6 s** | **1,1 s** |
| `load_orders` #2 (kein Data-Manager-Cache) | **1,0 s** | **1,4 s** |
| `OrderService._load` Cache #2 | 0 ms | 0 ms |
| `list_day_filled_all` (Day-Fills) | **17,1 s** (27 fills) | **0,5 ms** (19 fills, warm) |
| Pure `stats_from_filled_orders` | 0,1 ms | 0 ms |
| **`/orders` DAY** (TG mocked) | **40,7 s** | **16,4 s** |
| **`resolve_portfolio_context(fast)`** | **125 s** | **7,4 s** |
| `get_prices_batch` (~40 Coins) | **4,5 s** | **2,4 s** |
| **`send_positions_snapshot`** (Preise mocked) | **251 s** | **71 s** |

**Fazit Messung:** Mikro-Optimierungen (#119: kein Doppel-expire, 20s OrderService-Cache, lightweight daily) ändern das Nutzergefühl **nicht spürbar**. Bottlenecks:

1. Ganzes Orders-Blob laden  
2. Teure Day-List / Format-Pfade  
3. Portfolio-Zusammenbau (auch ohne Preis-Netz)  
4. Zusätzlich: Price-Batch bei vielen offenen Coins  

---

## 2. Industrie-Lösung (was andere machen)

Drei Schichten (Broker / Fintech-Ledger / Crypto-Exchanges):

| Schicht | Idee |
|---------|------|
| **Append-only Write Model** | 1 Event / 1 Order / 1 Fill = 1 Zeile oder kleines Doc; kein History-Blob-Rewrite |
| **CQRS Read Models** | UI liest **nicht** die volle Historie, sondern indexierte Queries / Views |
| **Materialized Daily Stats** | Bei jedem Fill: Tages-Aggregate `$inc`; Dashboard = O(1) Lookup |

Optional in großen Systemen: Kafka, Timescale continuous aggregates, ClickHouse.  
**Für diesen Bot:** Prinzip übernehmen, Stack schlank halten (Mongo + Indexes + Summary-Docs).

---

## 3. Zielbild für xagent

```text
                 WRITE (fill / reject / pending)
                            │
                            ▼
         ┌──────────────────────────────────┐
         │  orders_v2  (1 Document / Order) │
         │  indexes: tenant+scope+day_key   │
         │           tenant+scope+status    │
         │           tenant+scope+display_seq│
         └──────────────────┬───────────────┘
                            │ on write (sync, cheap)
         ┌──────────────────▼───────────────┐
         │  order_day_stats                 │
         │  _id: tenant:scope:YYYY-MM-DD    │
         │  buys, sells, volumes, pnl, W/L  │
         └──────────────────┬───────────────┘
                            │
  /orders list  ────────────┼──► query orders_v2 (day / range)
  /orders header ───────────┤
  /portfolio daily line ────┘──► findOne day_stats   (O(1))
  /portfolio lots  ──────────────► positions doc (+ warm memory)
  Risk daily limits ─────────────► day_stats / day query
```

### 3.1 Schema-Skizze

**Order-Doc (`orders_v2`):**

```js
{
  _id: "henry:demo:fac6f0e34c4a",
  tenant_id: "henry",
  ledger_scope: "demo",
  display_seq: 2042,
  status: "filled",          // filled | rejected | …
  side: "sell",
  symbol: "BEAT/USDT",
  timeframe: "1h",
  source: "auto",
  signal: "SELL_FULL",
  exit_source: "trailing_stop",
  request: { … },
  risk: { … },
  execution: { … },
  pnl: 375.86,
  error: null,
  timestamps: { created, updated, filled },
  ts_filled: ISODate("…"),   // for index
  ts_created: ISODate("…"),
  day_key: "2026-07-24",     // display calendar day (Europe/Berlin)
  idempotency_key: "…"
}
```

**Day-Stats (`order_day_stats`):**

```js
{
  _id: "henry:demo:2026-07-24",
  tenant_id: "henry",
  ledger_scope: "demo",
  day_key: "2026-07-24",
  buys: 16,
  sells: 11,
  buy_usdt: 8200.0,
  sell_usdt: 5100.0,
  realized_pnl: 557.1,
  sell_wins: 10,
  sell_losses: 1,
  updated_at: ISODate("…")
}
```

**Positions / Cash:** wie heute (kleines Doc); Cash/Realized bei Fill mitpflegen (Projektion).

---

## 4. Gesamtsystem — was angepasst werden muss

### 4.1 Kern-Persistenz (Muss)

| Bereich | Heute | Nachher |
|---------|--------|---------|
| Schema | 1 Doc mit `orders[]` | `orders_v2` + `order_day_stats` |
| `MongoLedgerStore` | load/save ganzes Array | `insert_order`, `update_order`, `query_orders`, `get_day_stats`, `inc_day_stats` |
| Indexes | faktisch keine auf Array-Inhalt | siehe §3 |
| `data_manager.load_orders/save_orders` | Blob-API | Kompat-Schicht oder deprecated |
| Dual-write | — | Write: Blob **und** v2 bis Cutover |
| Backfill | — | Blob → v2 + day_stats rebuild |
| Verify/Rebuild | — | day_stats aus v2 neu berechenbar (kein Angst vor Drift) |

### 4.2 Write-Path (Muss — sonst Drift)

| Modul | Anpassung |
|-------|-----------|
| `OrderService.create_from_request` | insert **1** Doc |
| `record_rejected` / `update_status` / `link_execution_result` | update **1** Doc; filled → `$inc` day_stats |
| `expire_stale_pending` | `update_many`, kein Full-Doc-Rewrite |
| `reconcile_legacy_sources` | gezielte Updates |
| `trading_service` / Gate-Adapter | nur über OrderService |
| Manual order flow | unverändert, wenn OrderService die einzige Write-API ist |

**Regel:** Kein Pfad mehr „lade N Orders → ändere 1 → speichere N“.

### 4.3 Read-Path UI (Muss für Speed)

| Feature | Nachher |
|---------|---------|
| `/orders` day | Query `day_key=today, status=filled` |
| `/orders` header | `findOne(day_stats)` |
| `/orders_blocked` | Query status ∈ blocked + day |
| `/orders_month` | day_key-Range **oder** Summe day_stats |
| Order-Detail `#seq` | Index `display_seq` |
| `/portfolio` Tageszeile | **nur day_stats** |
| `/portfolio` Lots | positions / memory; kein Full-Replay im Hot Path |

### 4.4 Portfolio / Cash / Replay (Muss für korrekte Zahlen)

| Modul | Anpassung |
|-------|-----------|
| `_sim_order_ledger_bundle` | Normal: positions + trade_history; Replay nur Repair |
| `compute_sim_cash_from_orders` / `compute_realized_pnl_from_orders` | aus trade_history; Full-Replay selten |
| `reconcile_*_trade_history` | v2-Cursor oder day_stats + open positions |
| `rebuild_positions_from_orders` (Startup) | incremental / last N days, nicht blockierend full history |
| `list_active_positions_from_ledger` | positions-Doc first; Rebuild async |

### 4.5 Risk / Trading-Loop (Muss)

| Modul | Anpassung |
|-------|-----------|
| Daily buy/sell limits | day_stats oder day count-query |
| Trade cooldown | last trade per symbol (Index oder Position-Feld) |
| Idempotency | unique sparse Index `idempotency_key` |
| display_seq | atomarer Counter `counters` collection, nicht `max(array)+1` |

### 4.6 Side-Systeme (Sollten)

| Modul | Anpassung |
|-------|-----------|
| `daily_portfolio` / `daily_stats` / morning briefing | gemeinsame day_stats-API |
| Position trade-tree (`position_ledger`) | query symbol + limit |
| `ledger_sync` (peak/ladder) | recent fills query |
| Memory rebuild / seed_from_ledger | v2 cursor (paginiert) |
| Hermes live_evidence / churn_replay | v2 Query-API |
| Telegram ask (Order-Kontext) | day/recent only |

### 4.7 Ops / Scripts (vor Cutover)

| Script | Anpassung |
|--------|-----------|
| `railway_seed_demo_mongo.py` | seed v2 + day_stats |
| `demo_ledger_bundle.py` export/import | neues Format |
| `reset_demo_ledger` / `snapshot_and_fresh_start` | beide Collections |
| `inspect_mt_ledger` / `inspect_ledger_summary` | v2 counts |
| `purge_phantom_ledger_symbols` | v2 |
| `migrate_tenant_orders` / `mongo_migrate_json` | v2-aware |
| `smoke_mt_demo_local` | Assertions v2 |

### 4.8 Multi-Tenant / Merge / Startup

| Thema | Anpassung |
|-------|-----------|
| Isolation | Jedes Order-Doc mit `tenant_id`; Queries immer filtern |
| `ledger_merge` (legacy + compound) | einmal migrieren, dann nur compound |
| Startup rebuild | nicht full-history blockierend |
| display_seq | atomarer Counter pro tenant/scope |

### 4.9 Tests (Muss)

- Unit OrderService v2 (insert/update/query day/month/blocked/detail)
- Day-stats parity nach Fills
- `/portfolio` und `/orders` gleiche Tageszahlen
- Dual-write Periode: blob ↔ v2 sync
- Backfill idempotent
- Risk daily limits mit day_stats
- Perf smoke: day query &lt; 200 ms bei 5k Orders (Fixture)
- Bestehende order/portfolio tests auf neue API

### 4.10 Nicht in v1

- Kafka / EventStore  
- ClickHouse  
- Vollständiges Double-Entry-Finanzledger  
- Soft-delete / Cold-Archiv (erst bei &gt;50k Orders/Tenant)

---

## 5. Phasenplan (ohne Big-Bang)

| Phase | Inhalt | Exit-Kriterium |
|-------|--------|----------------|
| **0** | Feature-Flag `ORDER_LEDGER_V2=off` | Flag existiert |
| **1** | Schema + Indexes + dual-write | Neue Fills in blob **und** v2 + day_stats |
| **2** | Backfill + verify | Henry/default: Counts/PnL matchen Blob |
| **3** | Reads: `/orders*`, portfolio daily → v2 | Staging Flag on; Latenz &lt;~300 ms intern |
| **4** | Risk / cooldown / idempotency → v2 | Limits korrekt ohne Full-Scan |
| **5** | Replay / repair / positions rebuild → v2 | Startup ohne Minuten-Hang |
| **6** | Scripts / seeds | Railway reset/seed grün |
| **7** | Blob-write off | Nur noch emergency read |
| **8** | Blob archivieren/löschen (optional) | Storage clean |

Jede Phase: Staging messen + Parity Tages-PnL.

---

## 6. Akzeptanz „System läuft sauber“

- [ ] Kein Hot Path lädt mehr das volle `orders[]`-Blob  
- [ ] Jeder Fill: **1 Order-Doc + day_stats + positions/cash**  
- [ ] `/orders` und `/portfolio` gleiche Tageszahlen  
- [ ] Handler intern typisch **&lt; 300 ms** (ohne Telegram-RTT, intern Railway ideal)  
- [ ] Risk-Tageslimits korrekt  
- [ ] Startup ohne Full-History-Block  
- [ ] Multi-tenant (default/henry) Isolation grün  
- [ ] Backfill + rebuild-stats dokumentiert und lauffähig  
- [ ] Seeds/Reset Railway demo ok  
- [ ] Tests + Staging-Smoke  

---

## 7. Erwarteter Effekt

| Command | Heute (Henry, gemessen) | Ziel nach v2 |
|---------|------------------------:|-------------:|
| `/orders` day | ~40 s | **&lt; 0,3 s** (intern) |
| `/portfolio` daily line | Teil von 7–125 s Context | **O(1) day_stats** |
| `/portfolio` lots | teurer Zusammenbau | positions + price cache |
| Write pro Fill | Full-Doc-Risiko | **O(1)** |

---

## 8. Kurzfassung

| | |
|--|--|
| **Root cause** | Unbounded `orders[]` im Tenant-Dokument + Full-Scan/Format im Hot Path |
| **Lösung** | 1 Doc pro Order + materialisierte `order_day_stats` + CQRS-light Reads |
| **Scope** | Persistenz, Writes, UI, Risk, Replay, Scripts, Tests — nicht nur Telegram |
| **Nicht** | Noch mehr Python-Caches um den Blob; kein Kafka in v1 |

---

## 9. Nächste Schritte

1. Epic + Tickets aus diesem Dokument ableiten (Phasen 1–8)  
2. Phase 1 implementieren: dual-write  
3. Backfill Henry/default, Parity-Check  
4. Read-Cutover Staging, messen, dann Blob-Write abschalten  

---

## 10. Referenzen (Messung / Code)

- Commit Mikro-Perf (unzureichend): `a91a698`  
- Day-Stats-Alignment Portfolio↔Orders: `3d1267c`  
- Full-list orders UX: #118, `f3a3e1d`  
- Sell lot-TF fix: #117, `db9e6c7`  
- Benchmark: lokal gegen Railway Mongo public URL, Tenant henry/default, 2026-07-24  
