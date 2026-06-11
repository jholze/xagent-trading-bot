# X-Agent Trading Bot — Vollständige Dokumentation

Stand: Juni 2026 · Version 1.4

Dieses Dokument ist die zentrale Übersicht: Architektur, Intervalle, Strategien, Telegram-Befehle, Demo-Modus, X/Twitter und Sandbox.

---

## 1. Was macht der Bot?

Der X-Agent Trading Bot ist ein **hybrider Krypto-Trading-Agent**:

1. Beobachtet Coins auf der **Watchlist** (technische Analyse: RSI, Bollinger Bands, Volumen)
2. Liest **X/Twitter-Posts** und **CMC-Sentiment** ein
3. Führt Signale in der **DecisionEngine** zusammen
4. Führt Trades aus (Paper oder Live auf Gate.io)
5. Meldet alles per **Telegram**

**Kernprinzip:** Kein blindes Folgen von Tweets — technische Signale und Social-Signale werden gewichtet zusammengeführt. Risiko-Limits und Cooldowns verhindern Übertrading.

---

## 2. Architektur (Überblick)

```mermaid
flowchart TB
    subgraph loop [Hauptzyklus alle 10 Min]
        WL[Watchlist]
        SP[SocialPipeline]
        SB[PaperSandbox]
        OR[SignalOrchestrator]
        WL --> OR
        SP --> OR
        SB --> SB
    end

    subgraph signals [Signalquellen]
        TA[TechnicalRSIStrategy]
        X[X / Grok]
        CMC[CMC Community]
    end

    subgraph decision [Entscheidung]
        DE[DecisionEngine]
        TA --> DE
        X --> DE
        CMC --> DE
    end

    OR --> DE
    DE --> RM[RiskManager]
    RM --> EX[ExecutionAdapter]
    EX --> PAPER[PaperAdapter]
    EX --> GATE[GateAdapter]

    OR --> TG[Telegram]
    SP --> TG
```

### Wichtige Module

| Modul | Datei | Aufgabe |
|-------|-------|---------|
| Hauptschleife | `aria_bot.py` | Flask-Webhook + Preis-/Signal-Zyklus |
| Entscheidung | `strategies/decision_engine.py` | TA + X + CMC → BUY/SELL/HOLD |
| Technik | `strategies/technical_rsi_bb.py` | RSI, BB, Volumen, TP, SL |
| Ausführung | `services/trading_service.py` | Modus, Risiko, Order |
| Gate.io | `execution/gate_adapter.py` | ccxt Market Orders |
| Social | `services/social_pipeline.py` | X-Posts, CMC, Accuracy |
| Sandbox | `strategies/paper_sandbox.py` | Isolierte Strategie-Tests |
| Telegram | `notifications/telegram_commands/` | Alle `/`-Befehle |

---

## 3. Wann läuft was? — Alle Intervalle

| Was | Intervall | Config / Quelle | Beschreibung |
|-----|-----------|-----------------|--------------|
| **Hauptzyklus** | **600 s (10 Min)** | `update_interval` | Watchlist, X, CMC, Sandbox, Trades, Telegram Cycle-Summary |
| X-Search Cache | 900 s (15 Min) | `x_performance.x_search_cache_ttl_sec` | Grok-Suche pro Account gecacht |
| X Live-Suche | 2 Tage zurück | `x_performance.live_search_days` | Zeitraum für Tweet-Suche |
| Trade-Cooldown (global) | 1 h | `trade_cooldown_hours` | Mindestabstand gleicher Trade-Typ |
| Cooldown pro Coin (Buy) | 4–6 h | `strategies[].min_hours_between_buys` | Pro Coin in `config.json` |
| Cooldown pro Coin (Sell) | 3–4 h | `strategies[].min_hours_between_sells` | Ausgenommen: Stop-Loss / Vollverkauf |
| Tages-Trade-Limit | 8 / 24 h | `max_daily_trades` | Global über alle Modi |
| RSI-Timeframe | 4 h (Standard) | `watchlist` + `strategies[]` | OHLCV-Kerzen für Indikatoren |
| Sandbox Mindestdauer | 7 Tage | `sandbox.min_test_days` | Vor Promotion |
| Sandbox Max-Laufzeit | 30 Tage | `sandbox.max_test_days` | Danach `expired` |
| X-Backtest Standard | 60 Tage | `x_backtest.default_days` | `/testaccount` |
| Accuracy-Tracking | 24 h Haltezeit | `x_backtest.min_signal_age_hours` | Bewertung alter Signale |
| Preis-Cache | TTL intern | `price_fetcher` | Weniger Gate/Binance-Calls |

### Ablauf eines Zyklus (Schritt für Schritt)

```
1. config.json neu laden
2. SocialPipeline: neue X-Posts holen + parsen (Grok)
3. CMC-Posts / Sentiment
4. Accuracy-Update (Trust-Scores der X-Accounts)
5. Sandbox-Hypothesen testen (parallel, isoliert)
6. Für jeden aktiven Watchlist-Coin:
   a. Preis von Gate.io (Fallback: Binance, KuCoin, Bybit)
   b. Indikatoren (RSI, BB, Volumen, ATR)
   c. DecisionEngine → Aktion
   d. RiskManager → Größe / Block
   e. Execution → Paper oder Gate
   f. Telegram bei Signal / Trade / Block
7. Cycle-Summary an Telegram (wenn notify_on_cycle: true)
8. Warten update_interval Sekunden → zurück zu 1
```

---

## 4. Handelsmodi

| Modus | Befehl | Was passiert | Echtes Geld? |
|-------|--------|--------------|--------------|
| **Paper** | `/mode paper` | Lokales Ledger (`trade_history.json`, `positions.json`) | Nein |
| **Live** | `/mode live` + `/live_confirm` | Gate.io Mainnet | **Ja** (wenn `dry_run: false`) |
| **Off** | `/mode off` | Nur Analyse, keine Orders | Nein |

### Live-Aktivierung (2 Stufen)

```
/mode live          → trading_mode=live, live_confirmed=false (noch gesperrt)
/live_confirm       → live_confirmed=true (Orders erlaubt)
/gate               → API-Keys, Balance, dry_run-Status prüfen
```

**Sicherheit:** `live.dry_run: true` (Standard) loggt Orders nur lokal — nichts geht an Gate.io, bis du `dry_run` auf `false` setzt.

### Telegram-Modus-Badges in Signalen

| Badge | Bedeutung |
|-------|-----------|
| 📋 PAPER | Lokales virtuelles Trading |
| 🔶 LIVE DRY | Live bestätigt, aber dry_run |
| 🔴 LIVE | Echte Mainnet-Orders |

---

## 5. Demo-Modus (`--demo`)

### Start

```bash
bash scripts/start_demo_with_ngrok.sh   # empfohlen: Bot + ngrok + Webhook
# oder manuell:
python3 aria_bot.py --demo
```

### Was ist anders?

| Aspekt | Normal | Demo (`--demo`) |
|--------|--------|-----------------|
| Daten-Dateien | `watchlist.json`, `positions.json`, … | `*.demo.json` (isoliert) |
| Telegram-Prefix | — | `🧪 [DEMO]` vor jeder Nachricht |
| Echtes Portfolio | Unberührt | Separate Demo-Dateien |

**Wichtig:** Demo nutzt dieselbe `config.json` — Handelsmodus und Strategien sind identisch, nur die **Daten** sind getrennt.

### Start / Stop

```bash
bash scripts/start_demo_with_ngrok.sh   # Stoppt alte Prozesse, startet ngrok neu, registriert Webhook
bash scripts/stop_bot.sh                # Bot + ngrok beenden
```

Das Start-Skript:
1. Beendet alte Bot-/ngrok-Prozesse (Ports 5000, 4040)
2. Startet Bot, wartet auf `/health`
3. Startet **frischen** ngrok-Tunnel
4. Testet Tunnel (HTTP 200)
5. Registriert Telegram-Webhook
6. Sendet Bestätigung an Telegram

---

## 6. Strategien — Wie sie funktionieren

### 6.1 Technische Basis (`TechnicalRSIStrategy`)

Jeder Watchlist-Coin hat Parameter in `config.json` → `strategies[]` (pro Symbol + Timeframe).

#### BUY (nur ohne offene Position)

Alle Bedingungen müssen gleichzeitig erfüllt sein:

- Preis ≤ Lower Bollinger Band × 1.01
- RSI zwischen `rsi_buy_low` und `rsi_buy_high`
- Volumen ≥ `volume_multiplier` × Durchschnitt

**Beispiel ARIA (4h):** RSI 28–45, Vol ≥ 1.4×, Preis am unteren BB → **BUY**

#### SELL (nur mit offener Position)

Priorität (höchste gewinnt):

| Priorität | Trigger | Aktion |
|-----------|---------|--------|
| 1 | X-Stop-Loss Preis erreicht | 100 % verkaufen |
| 2 | Verlust > `stop_loss_pct` | 100 % (`SELL_STOP_FULL`) |
| 3 | Verlust > 67 % von stop_loss | 50 % (`SELL_STOP_PARTIAL`) |
| 4 | Gewinn ≥ `take_profit_pct` | 30 % (`SELL_TP`) — einmalig |
| 5 | RSI kreuzt `rsi_sell_30` von unten | 30 % — einmalig pro Tier |
| 6 | RSI kreuzt `rsi_sell_20` von unten | 20 % — einmalig pro Tier |
| 7 | X `price_target` erreicht | 30 % |
| 8 | X/CMC SELL-Signal | 20–30 % je nach Confidence |

**Anti-Churn (neu):**
- **RSI-Cross:** Sell nur wenn RSI die Schwelle **von unten kreuzt**, nicht wenn er dauerhaft darüber bleibt
- **Tier-Flags:** 30 %-Verkauf passiert nur einmal, bis RSI wieder unter Schwelle − 5 fällt
- **Cooldown:** Kein zweiter Buy/Sell desselben Typs innerhalb von `min_hours_between_*`

### 6.2 DecisionEngine — Social + Technik

**Ohne Position → Buy-Merge:**
- Technisches BUY + X-BUY + CMC-BUY → `BUY_STRONG`
- Technisches BUY + (X oder CMC) → `BUY`
- Nur X + CMC (ohne TA) → `BUY` mit Social-Confidence

**Mit Position → Sell-Merge:**
- Stärkstes Sell-Signal gewinnt (Full > 50 % > 30 % > 20 %)

**X-BUY-Schwelle:** dynamisch nach Trust-Score (höherer Trust → niedrigere Confidence nötig)

### 6.3 Aktuelle Coin-Strategien (25 USDT/Trade)

| Coin | Tier | SL | TP | RSI Buy | RSI Sell 30/20 | Buy-Cooldown | Sell-Cooldown |
|------|------|----|----|---------|----------------|--------------|---------------|
| ARIA | Meme | 15 % | 12 % | 28–45 | 72/84 | 4 h | 3 h |
| RAVE | Meme | 15 % | 12 % | 28–45 | 72/84 | 4 h | 3 h |
| HIGH | Mid | 12 % | 10 % | 30–46 | 70/80 | 4 h | 3 h |
| SOL | Large | 8 % | 6 % | 32–48 | 68/78 | 6 h | 4 h |
| BTC | Large | 8 % | 6 % | 32–48 | 68/78 | 6 h | 4 h |

ARIA 1h-Strategie existiert nur für Sandbox (`live_enabled: false`).

### 6.4 Strategie-Beispiele

**Beispiel 1 — Konservativer Einstieg (ARIA):**
```
RSI: 38, Preis am unteren BB, Volumen 1.5×
→ Technisch: BUY
X-Account sagt auch BUY (Conf 80 %, Trust 85)
→ DecisionEngine: BUY_STRONG
→ RiskManager: 25 USDT (ggf. dynamisch angepasst)
→ Telegram: 🟢 BUY EXECUTED
```

**Beispiel 2 — Take-Profit (HIGH):**
```
Entry: $0.10, aktueller Preis: $0.11 (+10 %)
take_profit_pct: 10 → SELL_TP (30 % der Position)
→ Einmalig, Tier-Flag gesetzt
→ Nächster Zyklus: HOLD (kein erneuter TP)
```

**Beispiel 3 — Cooldown blockiert:**
```
Vor 2 h: BUY ARIA ausgeführt
Neues BUY-Signal heute
→ RiskManager: BLOCKED — „Trade cooldown: 2.0h (min 4.0h)“
→ Telegram: 🟢 BUY BLOCKED + Grund
```

**Beispiel 4 — Stop-Loss bypassed Cooldown:**
```
Position −16 % (stop_loss_pct: 15)
→ SELL_STOP_FULL sofort, Cooldown wird ignoriert
```

---

## 7. Telegram — Alle Befehle mit Beispielen

Sende `/help` für die komplette Liste. Bei unvollständigen Befehlen (z.B. nur `/buy`) antwortet der Bot mit einem Beispiel.

### 📋 Watchlist

| Befehl | Beispiel | Ergebnis |
|--------|----------|----------|
| `/list` | `/list` | Alle Coins mit Status (aktiv/inaktiv) |
| `/add SYMBOL` | `/add RAVE` | Fügt `RAVE/USDT` zur Watchlist hinzu |
| `/remove NUMMER` | `/remove 2` | Entfernt Coin Nr. 2 aus `/list` |

### 💰 Handel

| Befehl | Beispiel | Ergebnis |
|--------|----------|----------|
| `/buy SYMBOL USDT` | `/buy ARIA 25` | Kauft ARIA für 25 USDT |
| `/buy NUMMER USDT` | `/buy 1 25` | Kauft Coin Nr. 1 aus `/list` |
| `/sell` | `/sell` | Zeigt offene Positionen mit Entry, PnL |
| `/sell NUMMER PROZENT` | `/sell 1 30` | Verkauft 30 % von Position 1 |
| `/positions` | `/positions` | Portfolio-Übersicht, Kurse, letzte Trades |
| `/risk` | `/risk` | Limits, Drawdown, Trade-Größe |

### ⚙️ Modus & Gate.io

| Befehl | Beispiel | Ergebnis |
|--------|----------|----------|
| `/mode` | `/mode` | Aktueller Modus + alle Optionen |
| `/mode paper` | `/mode paper` | Zurück zu virtuellem Trading |
| `/mode live` | `/mode live` | Live vorbereiten (noch nicht aktiv) |
| `/live_confirm` | `/live_confirm` | Live-Trading freischalten |
| `/live_cancel` | `/live_cancel` | Live abbrechen → Paper |
| `/gate` | `/gate` | API-Keys, Balance, dry_run |

### 🐦 X / Twitter

| Befehl | Beispiel | Ergebnis |
|--------|----------|----------|
| `/addx ACCOUNT` | `/addx CryptoCapo_` | Account überwachen |
| `/removex ACCOUNT` | `/removex CryptoCapo_` | Account entfernen |
| `/listx` | `/listx` | Alle Accounts + Trust-Score |
| `/xsignals` | `/xsignals` | Aktuelle starke BUY/SELL-Signale |
| `/xposts` | `/xposts` | Letzte 10 analysierte Posts |
| `/xaccuracy` | `/xaccuracy` | Leaderboard (Trefferquote) |
| `/testaccount ACCOUNT [TAGE]` | `/testaccount CryptoCapo_ 30` | Backtest der Empfehlungen |
| `/tracktest` | `/tracktest` | Sofort-Test mit Beispiel-Tweet |

### 🧪 Sandbox & CMC

| Befehl | Beispiel | Ergebnis |
|--------|----------|----------|
| `/sandbox` | `/sandbox` | Laufende Strategie-Experimente |
| `/sandbox_results ID` | `/sandbox_results hyp_abc` | Win Rate, Sharpe, Drawdown |
| `/sandbox_promote ID` | `/sandbox_promote hyp_abc` | Strategie → `config.strategies[]` |
| `/cmc` | `/cmc` | CMC Community-Sentiment |

### Automatische Telegram-Nachrichten

| Auslöser | Inhalt |
|----------|--------|
| **Cycle-Summary** (alle 10 Min) | Balance, Signale, ausgeführte Trades |
| **BUY/SELL SIGNAL** | Signal ohne Ausführung |
| **BUY/SELL EXECUTED** | Trade mit Amount, PnL, Mode-Badge |
| **BUY/SELL BLOCKED** | Abgelehnt + Grund (Cooldown, Limit, …) |
| **X-Recommendation** | Neuer Tweet mit TP/SL, Confidence |
| **Nach jedem Trade** | Positions-Snapshot |
| **Bot-Neustart** | Webhook-URL, Modus |

---

## 8. X / Twitter — Pipeline im Detail

### Datenfluss

```
1. X-Accounts in x_accounts.json (via /addx)
2. Pro Zyklus: Grok X-Search (letzte 2 Tage, Cache 15 Min)
3. Grok parst Tweet → BUY/SELL/HOLD + coin + confidence + price_target + stop_loss
4. DecisionEngine vergleicht mit technischer Strategie
5. Empfehlung in x_posts.json gespeichert
6. Bei recommended: Telegram X-Recommendation
7. AccuracyTracker bewertet nach 24 h → Trust-Score Update
```

### Trust-Score & Accuracy

- Jeder X-Account startet mit Trust **70**
- Nach jedem Signal: nach 24 h wird geprüft, ob Kurs in erwartete Richtung lief
- `buy_success_pct: 3 %` / `sell_success_pct: -2 %` als Erfolgs-Schwellen
- Trust wird per EMA aktualisiert (`trust_ema_alpha: 0.3`)
- Niedriger Trust → höhere Confidence nötig für Live-Trades

### X-Backtest (`/testaccount`)

- Sucht historische Posts eines Accounts (Grok)
- Simuliert BUY/SELL mit `price_target` und `stop_loss`
- Zeigt Hit-Rate, durchschnittliche Returns
- Optional: Account direkt zur Watchlist hinzufügen (Button)

### Strategy Discovery (automatisch)

Tweets mit RSI/Volumen/Breakout-Keywords erzeugen **Sandbox-Hypothesen** — getrennt vom Haupt-Portfolio getestet.

---

## 9. Sandbox-Modus

### Zweck

Isoliertes Testen von Strategie-Ideen **ohne** das echte/virtuelle Haupt-Portfolio zu beeinflussen.

### Wie Hypothesen entstehen

1. X-Post enthält Strategie-Keywords (RSI, breakout, volume, …)
2. `StrategyDiscovery` extrahiert Parameter (Grok)
3. Hypothese mit Status `testing` in `paper_strategies.json`
4. Jeder Hauptzyklus: Sandbox führt TA auf Watchlist-Coins aus
5. Separates Portfolio pro Hypothese (`paper_sandbox_history.json`)

### Sandbox-Config

| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `initial_capital_usdt` | 1000 | Startkapital pro Hypothese |
| `usdt_per_trade` | 50 | Trade-Größe in Sandbox |
| `min_test_days` | 7 | Mindestlaufzeit vor Promotion |
| `max_test_days` | 30 | Danach `expired` |

### Promotion-Kriterien (`/sandbox_promote`)

| Metrik | Minimum |
|--------|---------|
| Win Rate | 55 % |
| Sharpe | 0.5 |
| Max Drawdown | ≤ 15 % |
| Trades | ≥ 3 |

Bei Erfolg: Eintrag in `config.strategies[]` — ab dann im Haupt-Bot aktiv.

### Beispiel-Workflow

```
1. CryptoCapo_ tweetet: „Buy RSI 30 on dips, sell 70, 4h“
2. Bot erstellt hyp_abc123 (Status: testing)
3. /sandbox → hyp_abc123, WR: 62 %, 5 Trades
4. /sandbox_results hyp_abc123 → Promotion: ✅ Ready
5. /sandbox_promote hyp_abc123 → Strategie in config.json
```

---

## 10. CMC (CoinMarketCap)

- **Community-Posts** (wenn API-Plan verfügbar) oder **Quotes-Fallback**
- Signale: BUY / SELL / HOLD mit Confidence und Vote-Ratio
- In DecisionEngine wie X-Signale gewichtet (`onchain_weight: 0.2`)
- `/cmc` zeigt aktuelle Sentiment-Signale

---

## 11. Risiko-Management

| Limit | Standard | Wo |
|-------|----------|-----|
| Max pro Trade | 25 USDT | `max_usdt_per_trade` / `live.max_usdt_per_trade` |
| Max offene Positionen | 5 | `max_open_positions` |
| Max Coin-Anteil | 30 % Portfolio | `max_position_percent` |
| Tages-Trades | 8 / 24 h | `max_daily_trades` |
| Drawdown-Drossel | −10 % → halbe Größe | `risk.drawdown_throttle_pct` |
| Min Trade | 5 USDT | `risk.min_trade_usdt` |
| Slippage (Paper) | 1.5 % | `slippage_percent` |

Dynamische Größe: Trust × Confidence × ATR-Faktor × Drawdown-Multiplikator (max ×2.0).

`/risk` zeigt alle Werte live.

---

## 12. Konfiguration (`config.json`) — Schnellreferenz

```json
{
  "trading_mode": "paper",
  "update_interval": 600,
  "max_usdt_per_trade": 25,
  "trade_cooldown_hours": 1.0,
  "max_daily_trades": 8,
  "observability": {
    "notify_on_cycle": true,
    "terminal_dashboard": true
  },
  "strategies": [ /* pro Coin, siehe Abschnitt 6.3 */ ]
}
```

### `.env` (nicht committen)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
GATE_API_KEY=...          # Gate.io Live (Mainnet)
GATE_API_SECRET=...
CMC_API_KEY=...
XAI_API_KEY=...            # Grok
```

---

## 13. Daten-Dateien

| Datei | Inhalt |
|-------|--------|
| `watchlist.json` | Beobachtete Coins |
| `config.json` | Strategien, Limits, Modi |
| `positions.json` | Offene Positionen, Cooldowns, RSI-Tiers |
| `trade_history.json` | Trades, Balance, PnL |
| `live_trade_history.json` | Gate-Orders (Live) |
| `orders.live.json` | Order-Ledger für Live (scope `live`) |
| `orders.paper.json` | Order-Ledger für lokales Paper |
| `x_accounts.json` | Überwachte X-Accounts |
| `x_posts.json` | Analysierte Posts + Empfehlungen |
| `paper_strategies.json` | Sandbox-Hypothesen |
| `paper_sandbox_history.json` | Sandbox-Portfolios |
| `*.demo.json` | Demo-Modus-Kopien |

---

## 14. Tests

```bash
pytest tests/unit/ -v
pytest tests/unit/test_trade_cooldown.py -v   # Cooldown + RSI-Churn
pytest tests/unit/test_live_gate_readiness.py -v

# Gate readiness (keys in .env required)
python3 scripts/gate_live_smoke_test.py
python3 scripts/reconcile_gate_positions.py
```

---

## 15. Go-Live Checkliste

1. Bot **ohne** `--demo` starten (Demo isoliert `*.demo.json` / `orders.demo.json`)
2. `bash scripts/start_demo_with_ngrok.sh` oder Produktiv-Start — Telegram testen
3. Paper laufen lassen, `/positions` + `/orders` + Cycle-Summaries prüfen
4. `python3 scripts/gate_live_smoke_test.py` — Keys + Balance prüfen
5. `/mode live` + `live.dry_run: true` — Dry-Run-Zyklus, Ledger in `orders.live.json`
6. `live.dry_run: false` in `config.json`, Bot neu starten, dann `/live_confirm`
7. Manueller `/buy` mit kleinem Betrag — Gate Spot Order History + `/orders` vergleichen
8. `python3 scripts/reconcile_gate_positions.py` — lokale Positionen vs. Gate-Bestand
9. `/gate` — USDT, Spot-Bestände, Dry-Run-Status prüfen
10. Grok-Credits prüfen (`use_grok_x_search`) — sonst keine X-Auto-Trades im Live-Modus

**Hinweis:** Gate.io Testnet ist in Deutschland nicht verfügbar — Üben mit Paper, Live nur auf Mainnet. Im Live-Modus nutzen Risk Manager und `/positions` die **echte Gate-USDT-Balance**; `trade_history.json` wird für Gate-Orders nicht beschrieben (nur `live_trade_history.json` + Order-Ledger). `positions.json` bleibt Bot-Cache — regelmäßig mit `reconcile_gate_positions.py` abgleichen.

---

**Weitere Hilfe:** `/help` in Telegram · GitHub Issues im Repo