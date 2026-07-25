# Plan: Lokaler Live-naher Pre-Staging-Test (Mac) + Headless Telegram

> **Status:** Plan only — no implementation yet  
> **Datum:** 2026-07-25 · **Update:** Zielbild „Mac local ≈ live“  
> **Branch-Kontext:** unabhängig von WQE (#124); eigenes Backlog-Thema  
> **Primärziel:** Auf dem **MacBook alle neuen Features sauber testen, bevor sie nach Staging gehen** — möglichst **live-nah**, inkl. Bot-Lauf, Ledger, Signale, Risk, Telegram, Webhooks  
> **Telegram headless** ist **eine Säule**, nicht das Gesamtziel  

---

## 0. Ziel (kanonisch)

### Was du willst

1. **Lokal auf dem Mac** (nicht erst auf Railway Staging)  
2. **Jedes neue Feature** vor dem Staging-Push **durchspielen**  
3. **Komplett** — nicht nur Unit-Tests, sondern der **laufende Bot** mit allem, was zum Feature gehört  
4. **Live-Modus möglichst nah abbilden** — gleiche Pfade, gleiche Config-Semantik, gleiche Operator-UX — **ohne** echtes Mainnet-Geld zu riskieren, solange du nicht explizit live bestätigst  

### Was „fertig getestet“ heißt (Definition of Done lokal)

Ein Feature darf erst nach Staging, wenn **lokal** gilt:

| # | Check | Live-Nähe |
|---|--------|-----------|
| 1 | Unit/Integration grün (betroffene + Pre-Staging-Slice) | Logik |
| 2 | Bot läuft lokal im **Demo/Paper-Stack** wie Staging-Runtime | Prozess |
| 3 | Feature über **Telegram** bedienbar / sichtbar (echter Client oder headless) | Operator-UX |
| 4 | **Ledger/Scope** korrekt (demo vs paper vs live-Dateien/Mongo) | State |
| 5 | **Nebenwirkungen** geprüft (Signale, Risk, Memory, Cycle, Notifies) | System |
| 6 | Kein versehentlicher Mainnet-Trade (Guards) | Safety |
| 7 | Optional: `verify_pre_staging.sh` + Feature-spezifische Smokes | Gate |

### Nicht-Ziele

- Staging/Railway ersetzen (Staging bleibt Soak vor Prod)  
- Prod-Mainnet als Alltagstest  
- 100 % bit-identisch zu Railway (Netzwerk, Secrets, Multi-Service) — **Verhalten** spiegeln, nicht die Cloud  
- Jarvis · lokaler tdlib Bot-API-Server (v1)  

---

## 1. Leitprinzip: Live-Nähe in Schichten

```text
                    ┌─────────────────────────────────────┐
  am nächsten Live  │ L5  Optional: paper/live-confirm    │  echte Gate-Keys, dry_run
                    │     lokal (nur bewusst, selten)     │
                    ├─────────────────────────────────────┤
                    │ L4  Full local runtime               │  aria_bot + ngrok + Telegram
                    │     DEMO/Paper, echte APIs wo nötig  │  CMC/Gate public, Memory
                    ├─────────────────────────────────────┤
                    │ L3  Headless Telegram E2E            │  Telethon → Test-Bot
                    │     (optional, feature flows)        │
                    ├─────────────────────────────────────┤
                    │ L2  Integration / stress / capture   │  dispatch + mock send
                    ├─────────────────────────────────────┤
  am schnellsten    │ L1  Unit tests + verify_pre_staging  │  offline / local Mongo
                    └─────────────────────────────────────┘
```

**Alltag vor Staging:** L1 → L2 → **L4 Pflicht** für Feature-DoD.  
**L3** spart manuelles Tippen und macht Flows wiederholbar.  
**L5** nur wenn Feature **explizit** Gate-Live-Pfad braucht (Keys, dry_run, nie unattended).

„Live-nah“ heißt hier:

| Dimension | Live (Prod) | Lokales Zielbild |
|-----------|-------------|------------------|
| Code-Pfad | `aria_bot` Cycle + Webhook + Commands | **gleicher Code**, gleicher Branch |
| Trading | Gate Mainnet Orders | **Paper/Demo Ledger** oder Live+`dry_run` |
| Telegram | Operator-Bot | **eigener Local/Demo-Bot** (Token), echte App oder Telethon |
| Webhook | Railway Public URL | **ngrok** (`start_demo_with_ngrok.sh`) |
| Mongo | Railway `xagent_test` / prod DB | **localhost** via `dev_local_mongo.sh` — **nie** Railway-URL exportieren |
| Market data | Gate/CMC live | **echte** Marktdaten (realistisch) |
| Memory/Hermes | memory_* + baseline | lokal fail-open oder lokales Mongo memory_* |
| Identity | `BOT_STACK=production` | `BOT_STACK=local` + `🧪 [DEMO]` wo Demo |

---

## 2. Ist-Zustand (bereits brauchbare Bausteine)

| Baustein | Rolle fürs Ziel |
|----------|-----------------|
| `scripts/start_demo_with_ngrok.sh` | Lokal Bot + Webhook + Telegram |
| `scripts/start_with_ngrok.sh` | Non-demo Start |
| `scripts/dev_local_mongo.sh` | Sicheres lokales Mongo |
| `scripts/verify_pre_staging.sh` | Pre-Push-Gate (MT, ledger, slice pytest) |
| `scripts/smoke_mt_demo.sh` / `smoke_mt_demo_local.py` | Multi-tenant / demo smokes |
| `DEMO_MODE=1`, `*.demo` / Mongo ledger | Isolierter State |
| Paper / Live+dry_run | Handelsmodus-Nähe ohne (oder mit Guard) Geld |
| Unit-Tests + `patch(send_telegram_*)` | L1/L2 |
| `telegram_transparency_showcase.py` | Manuelle Message-Typen |
| `dispatch_command` / router | Headless Command-Einstieg |

**Lücke:** kein **einheitliches Local-Live-Parity-Playbook**, kein systematisches **Feature-DoD-Checklist**, Telegram-E2E nicht automatisiert, Pre-Staging-Script deckt **nicht** „Feature end-to-end im laufenden Bot“.

---

## 3. Zielbild: Local Pre-Staging Environment (LPSE)

### 3.1 Ein Befehl / ein Ritual (Soll)

```text
# Konzept — noch nicht implementiert als ein Super-Script
1) source scripts/dev_local_mongo.sh
2) ensure test bot token + chat (local .env, gitignored)
3) bash scripts/start_demo_with_ngrok.sh   # oder paper-live-near profile
4) bash scripts/verify_pre_staging.sh      # automated gate
5) feature checklist / optional Telethon suite
6) stop_bot + note results → dann erst deploy_staging
```

Später optional: `scripts/local_pre_staging.sh` orchestriert 1–5 mit Flags  
(`--unit-only`, `--with-telegram-e2e`, `--paper`, `--skip-ngrok`).

### 3.2 Profile (Config-Semantik)

| Profil | Zweck | Trading | Telegram | Wann |
|--------|-------|---------|----------|------|
| **`local-demo`** | Default Feature-Test | Demo ledger, paper-ähnlich | Demo-Bot, `🧪` | fast immer |
| **`local-paper`** | Paper-Scope wie Staging-Paper | `positions.paper` / paper capital | Local bot | Paper-Features |
| **`local-live-dry`** | Max. Live-Nähe ohne Fill | `trading_mode=live`, **dry_run=true** | Local bot | Gate/order-path Features |
| **`local-live-real`** | Nur bewusst, selten | live + confirm, small size | **nie** unattended | echte Fill-Verifikation |

**Regel:** Feature-Alltag = `local-demo` oder `local-paper`.  
`local-live-dry` wenn der Diff Order-Adapter / Gate / live-only Branches berührt.  
`local-live-real` nie in Headless-Automation ohne doppelte Guardrails.

### 3.3 „Komplett mit allem was dazu gehört“ — Feature-Matrix

Jedes Feature-Ticket bekommt vor Staging eine **Impact-Map**:

| Domäne | Beispiele | Lokal prüfen |
|--------|-----------|--------------|
| **Cycle / Engine** | Signale, Sensor, Eval-Queue | 1–2 Zyklen laufen lassen, Logs |
| **Risk / Capacity** | soft_block, slots, cash | Order reject reasons, `/risk` |
| **Ledger / Orders** | v2 dual-write, scopes | `/orders`, Mongo/JSON, demo vs paper |
| **Watchlist / WQE** | tiers, scores | `/list`, logs `watchlist_quality_*` |
| **Memory / Hermes** | profiles, baseline | fail-open + optional local memory rebuild |
| **Telegram UX** | commands, buttons, digests | App oder Telethon |
| **Webhooks** | TradingView / signal webhook | ngrok URL + curl/fixture |
| **Background** | ask-bridge, social, hermes external | Prozess + queue files / health |
| **Observability** | `/mode`, build info, decisions | `/mode`, `decisions.jsonl` |

Headless Telegram deckt die **UX-Spalte**; die anderen Spalten brauchen Runtime + Domain-Tests.

---

## 4. Telegram im Gesamtziel (untergeordnet, aber wichtig)

Telegram ist der **Operator-Kanal**. Live-nah testen ohne Telegram = blind fliegen.

### 4.1 Drei Stufen (wie zuvor, neu priorisiert)

| Layer | Was | Rolle im Mac-Pre-Staging |
|-------|-----|---------------------------|
| **T1 Mock/Capture** | `dispatch_command` + capture outbound | Schnell, CI, jeder PR |
| **T2 Fake HTTP** | sendMessage contract | Optional |
| **T3 Telethon E2E** | Echter User → Local/Demo-Bot | **Mac Feature-DoD** für UX-Flows |

Auf dem **Mac** ist T3 wertvoller als in PR-CI: du hast Secrets lokal, Bot läuft schon mit ngrok, und du willst „wie live tippen“ ohne selbst zu tippen.

### 4.2 Empfohlene Local-Telegram-Topologie

```text
[Telethon Test-User Session]  ──Nachrichten──►  [Local/Demo Bot Token]
        │                                              │
        │                                              ▼
        │                                     aria_bot (Mac) + ngrok webhook
        │                                              │
        └──────── Antworten / Buttons ◄────────────────┘
```

| Item | Empfehlung |
|------|------------|
| Bot | **Separater** Local/Demo-Bot (nicht Prod-Token im Alltag) |
| Chat | Dein privater Test-Chat oder „Saved Messages“-tauglicher Flow |
| Session | Telethon Session-String **nur lokal** / Keychain, gitignored |
| Automation | Scripts: `tests/e2e_telegram/` oder `scripts/e2e_telegram_*.py` |
| Guard | `DEMO_MODE=1` default; live-buy commands denylist in E2E |

### 4.3 Was Telethon lokal abdecken soll (Feature-DoD)

| Flow-Klasse | Beispiele | Assert |
|-------------|-----------|--------|
| Read-only | `/help`, `/mode`, `/portfolio`, `/orders`, `/list` | Antwort kommt, Keywords, kein Crash |
| Menü/Callbacks | Menu button, order detail | Callback verarbeitet, edit/new message |
| Feature-spezifisch | z. B. `/watchlist` scores nach WQE | Text/Button enthält Score/Tier |
| Write paper/demo | `/buy` nur in demo/paper mit mock/dry | Ledger ändert sich erwartet |
| Negativ | unknown command, risk block | verständliche Fehler-Message |

---

## 5. Live-Nähe: was wir spiegeln vs. was wir simulieren

| Komponente | Spiegeln (echt) | Simulieren / isolieren |
|------------|-----------------|-------------------------|
| Python-Codepfade Cycle/Risk/Commands | ✅ | — |
| Gate public market data | ✅ | — |
| Gate private orders | paper ledger oder dry_run | echte Fills nur L5 |
| CMC / LC APIs | ✅ wenn Keys da | skip + fixtures wenn Budget |
| Telegram Bot API | ✅ (local bot) | T1 mocks für Unit |
| Webhook ingress | ✅ ngrok | curl fixtures |
| Mongo | ✅ local | nie Railway write von Mac |
| Railway multi-service (Hermes/Weaviate) | optional partial | fail-open / memory off OK |
| Timing / load Railway | ❌ | nicht Ziel |

**Maximale Live-Nähe ohne Geld:**

```text
local-live-dry:
  trading_mode=live
  dry_run=true
  echte Gate API keys (read + order path dry)
  echte Marktdaten
  Telegram local bot
  ngrok webhook
  Mongo local
  DEMO_MODE=0 aber dry_run erzwungen + extra kill-switch env LOCAL_FORBID_LIVE_FILLS=1 (Soll)
```

---

## 6. Pre-Staging Workflow (Soll-Prozess)

```text
Feature-Branch fertig
        │
        ▼
┌───────────────────────┐
│ A. Automated          │  pytest (feature + verify_pre_staging)
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ B. Local runtime up   │  mongo + start_demo_with_ngrok (oder paper/live-dry profile)
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ C. Operator path      │  Telegram App und/oder Telethon suite
│    + domain checks    │  logs, /orders, cycle, feature flags
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ D. Regression glance  │  smoke: portfolio, mode, no live fill
└───────────┬───────────┘
            ▼
     deploy_staging.sh
            │
            ▼
     Staging soak (Railway) — unverändert nötig
```

### 6.1 Checkliste pro Feature (Copy-Paste)

```markdown
## Local pre-staging — <feature>
- [ ] Branch + diff reviewed
- [ ] Unit/integration for touched modules green
- [ ] `bash scripts/verify_pre_staging.sh` green
- [ ] Profile: local-demo | local-paper | local-live-dry
- [ ] Bot running (ngrok webhook OK)
- [ ] Telegram: feature command/notify verified (app or Telethon)
- [ ] Ledger/scope OK (no cross-contamination demo/paper/live)
- [ ] Risk/side effects checked (blocks, size, soft_block if relevant)
- [ ] Logs clean enough (no unexpected ERROR spam)
- [ ] Live fill impossible unless intentionally L5
- [ ] Notes for staging soak
```

---

## 7. Phases (Implementation später)

| Phase | Deliverable | Outcome |
|-------|-------------|---------|
| **P0** | Dieses Plan-Doc + DoD/Checklist | Ziel klar |
| **P1** | Docs: „Local Pre-Staging Playbook“ in DOCUMENTATION/README | Jeder (du) startet gleich |
| **P2** | `TelegramCapture` + P0 command matrix (T1) | Schnelle Regressions |
| **P3** | Telethon harness lokal (T3) + 5–10 smokes | Headless Operator-UX |
| **P4** | Feature-Impact-Map template + erweitertes `verify_pre_staging` optional | Weniger vergessene Domains |
| **P5** | `local-live-dry` profile + `LOCAL_FORBID_LIVE_FILLS` guard | Max Live-Nähe sicher |
| **P6** | Optional one-shot `local_pre_staging.sh` | Ritual in einem Befehl |

Reihenfolge bewusst: **Playbook + Runtime-Disziplin zuerst**, dann Telegram-Automation, dann Guards/Profiles.

---

## 8. Telegram-Technik (Detail, unverändert relevant)

### 8.1 T1 Capture (CI + lokal schnell)

```python
# Idee — plan only
@pytest.fixture
def tg_capture(monkeypatch):
    out = []
    def _send(text, **kw):
        out.append({"type": "message", "text": text, **kw})
    monkeypatch.setattr("telegram_notifier.send_telegram_message", _send)
    # + buttons / edit / answer_callback
    return out
```

### 8.2 T3 Telethon (Mac Feature-DoD)

| Item | Empfehlung |
|------|------------|
| Lib | Telethon |
| Secrets | local only: API_ID/HASH, SESSION, BOT username |
| Guard | `TELEGRAM_E2E=1`, skip otherwise |
| Safety | demo/paper default; deny live buy in suite |
| Timeout | 15–30s per response |

```python
async with client.conversation(bot_username, timeout=30) as conv:
    await conv.send_message("/help")
    response = await conv.get_response()
    assert response.text
```

### 8.3 Nicht über Telegram testen

Ledger-v2 correctness, WQE math, Hermes promotion, pure risk math → **Domain unit/integration** + Runtime logs.  
Telegram prüft **Wiring & UX**, nicht die ganze Quant-Engine.

---

## 9. Sicherheit (Mac + Live-Nähe)

| Risiko | Mitigation |
|--------|------------|
| Railway Mongo von Mac | `dev_local_mongo.sh`; nie `MONGO_URL` aus Railway exportieren |
| Versehentlicher Mainnet-Fill | Demo/Paper default; dry_run; Soll-Flag `LOCAL_FORBID_LIVE_FILLS` |
| Prod-Telegram spammen | Separater Local/Demo-Bot-Token |
| Telethon Session leak | gitignore, nicht committen |
| ngrok öffentlich | nur solange Test; Token/Webhook rotieren bei Leak-Verdacht |
| API-Kosten CMC | Cache; nicht unnötig Full-Suite spammen |

---

## 10. Abgrenzung

| Thema | Relation |
|-------|----------|
| WQE Epic #124 | Features dort brauchen **dieses** Local-DoD vor Staging |
| Top-Trader #131 | Später gleiches Ritual |
| `ARCHITECTURE_PLAN` gateway/worker | Runtime-Zukunft; LPSE funktioniert schon mit heutigem Stack |
| Railway Staging | Bleibt; lokal filtert **vor** dem Push |

---

## 11. Offene Fragen

1. **Default-Profil:** Immer `local-demo`, oder oft `local-live-dry`?  
2. **Ein Local-Bot-Token** reicht, oder Demo-Bot = Staging-Bot teilen? (Empfehlung: **eigenes Local-Token**)  
3. **Telethon:** willst du das als Standard vor jedem Staging-Push, oder nur bei Telegram-touchenden Features?  
4. **Live-dry:** Gate-Keys lokal schon vorhanden und OK für Alltag?  
5. **One-shot Script** (`local_pre_staging.sh`) high priority oder Playbook-first?  
6. **Ollama/Ask:** `/ask` lokal mit Ollama mittesten oder Bridge-only?  

---

## 12. Empfehlung (für dein Ziel)

| Priorität | Aktion |
|-----------|--------|
| **Jetzt** | Ziel + Checklist + Profile in diesem Plan (done) |
| **Als Nächstes** | Kurzes **Local Pre-Staging Playbook** (README/Doku-Abschnitt) auf Basis bestehender Scripts |
| **Dann** | T1 Capture-Matrix (schnell, CI) |
| **Dann** | T3 Telethon gegen **laufenden** Local-Demo-Bot (dein Mac-Workflow) |
| **Parallel** | `local-live-dry` + Fill-Forbid Guard spezifizieren/implementieren wenn Order-Pfad kritisch |

Damit erreichst du: **lokal, feature-vollständig, live-nah, vor Staging** — Telegram headless als Beschleuniger, Runtime-Parity als Kern.

---

## 13. Nächste Schritte (Freigabe)

- [x] Plan auf Gesamtziel „Mac local pre-staging ≈ live“ erweitert  
- [ ] Fragen §11 beantworten (kurz)  
- [ ] Optional GitHub Issue/Epic: „Local Pre-Staging Environment (LPSE)“  
- [ ] Implementation: P1 Playbook → P2 T1 → P3 Telethon  
- [ ] **Nicht** auf WQE-Branch mischen, außer Feature dort getestet wird  

**Heute:** nur Plan-Update, kein Code, keine Tickets (außer du willst sie).
