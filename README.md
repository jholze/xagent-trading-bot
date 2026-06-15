# X-Agent Trading Bot (Version 1.9.0)

**Autonomer Krypto-Agent:** Technische Analyse (RSI, Bollinger, Volumen) + X/Twitter-Signale + CMC-Sentiment → Handelsentscheidungen mit Risiko-Limits, Cooldowns und Telegram-Steuerung.

**Auch für Einsteiger:** Jede wichtige Telegram-Nachricht erklärt **warum** der Bot etwas tut — auf Deutsch, ohne Trading-Vorkenntnisse.

> **Vollständige Dokumentation:** [DOCUMENTATION.md](DOCUMENTATION.md) — Architektur, Strategien, **Transparenz & Glossar**, alle Telegram-Befehle, Hermes, Demo-Modus.

---

## Schnellstart

```bash
cd ~/Documents/scripts/trading_bot
pip3 install -r requirements.txt

# Empfohlen: Bot + ngrok + Telegram-Webhook (stoppt alte Prozesse automatisch)
bash scripts/start_demo_with_ngrok.sh

# Stoppen
bash scripts/stop_bot.sh
```

In Telegram: Menü-Button neben der Eingabezeile (alle Befehle) oder `/help` senden.

---

## Was ist neu in 1.9 (Juni 2026)

- **Volatile-Altcoin-Profil** — automatische Erkennung hektischer Coins (ATR, Meme, Micro-Cap); extra Verkaufsregeln (Bollinger oben, Volumen-Erschöpfung, Volumen-Dump); **Shadow-Mode** zum Beobachten ohne Ausführung ([Doku §6.5](DOCUMENTATION.md#65-strategie-auswahl--wer-bekommt-welche-regeln))
- **Hermes Memory als Live-Fallback** — gelernte Parameter aus `hermes/memory/baseline.json` gelten im Bot auch für Coins **ohne** `config.strategies[]`-Eintrag (z. B. H/USDT) — über Sell, Rebuy und erneuten Kauf hinweg ([HERMES_DOKUMENTATION.md](HERMES_DOKUMENTATION.md))
- **Praxis-Beispiele H / ARIA / WLD** — 30-Tage-Nachvollzug in Alltagssprache ([Doku §6.6](DOCUMENTATION.md#66-praxis-beispiele--h-aria-wld-ca-30-tage-maijuni-2026))
- **381 Unit-Tests** — inkl. volatile profile, Hermes fallback, market structure

### Aus 1.8

- **CMC-Churn-Schutz** — stabile Quote-IDs, Sell-TA-Pflicht, höhere Sell-Schwellen, Social-Sell-Cooldowns ([Plan](plans/cmc-churn-fixes.md), [Doku §10](DOCUMENTATION.md#10-cmc-coinmarketcap))
- **Build-Info in Telegram** — Version + Git-Branch bei Restart, `/gate`, `/mode` (ohne neues Menü)
- **Telegram-Transparenz** — Trade-Nachrichten mit **„Warum:“** auf Deutsch + technischer Kurzzeile
- **Neue Befehle** — `/decisions`, `/why SYMBOL`, `/hermes_last` — Bot-Entscheidungen nachvollziehen
- **Zyklus-Digests** — CMC/X-Signale und Hermes-Lernzyklen automatisch erklärt
- **Entscheidungs-Protokoll** — `logs/decisions.jsonl` für alle Analysen
- **Showcase-Skript** — `python3 scripts/telegram_transparency_showcase.py` (alle Nachrichtentypen testen)
- **Coin-Links** — klickbare CMC/Gate/Chart-Links + Mini-Chart bei ausgeführten Trades

Details: [DOCUMENTATION.md §7](DOCUMENTATION.md#7-telegram--alle-befehle-mit-beispielen) und [§16 Transparenz](DOCUMENTATION.md#16-transparenz--nachvollziehbarkeit-technik)

### Aus 1.7

- **Hermes Hybrid-Pool** — Pins + offene Positionen + CMC-Top-Coins (max. 8), Rotation per `signal_activity`
- **Live-Evidenz & Dual-Modus** — Dry-Run-Ledger + Counterfactual für sichere Hermes-Promotions
- **Tages-Auswertung** — `scripts/daily_auswertung.py` → `auswertungen/YYYY-MM-DD_tag.md` (Cron 23:55)

### Aus 1.6 / 1.5

- **Enhanced Dry Run** — Sim-Wallet ($5000), CMC-Trending-Watchlist, `/dryrun`, realistischeres Live-Üben ohne Orders
- **Sim-Cash Fix** — Cash wird aus Trade-Historie neu berechnet; Portfolio = Cash (Sim) + Positionen
- **Strategy Backtest** — Auto-Backtest + Parameter-Tuning pro Coin (`/backtest`, gestaffelt, Auto-Apply mit Guardrails)
- **Manuell vs. Auto** — `/orders` und `/positions` zeigen Trade-Quelle korrekt (Manuell / Auto)
- **Scope-Ledger** — `positions.live.json` / `positions.paper.json` getrennt vom Paper-Ledger
- Portfolio-Invarianten (`test_dry_run_portfolio.py`)

### Aus 1.4

- **Gate.io Live-Trading** — Paper + Live Mainnet (2-Stufen: `/mode live` + `/live_confirm`)
- **Order-Ledger** — `/orders` scope-isoliert (demo/paper/live)
- **Anti-Churn** — Cooldowns, RSI-Cross-Sells, Take-Profit
- **Produktiv-Start** — `scripts/start_with_ngrok.sh` (ohne `--demo`)

---

## Handelsmodi (Kurz)

| Modus | Befehl | Beschreibung |
|-------|--------|--------------|
| Paper | `/mode paper` | Virtuelles Ledger (Standard) |
| Live | `/mode live` + `/live_confirm` | Gate.io Mainnet (dry_run standardmäßig an) |
| Off | `/mode off` | Nur Analyse |

Details: [DOCUMENTATION.md §4](DOCUMENTATION.md#4-handelsmodi)

---

## Hauptzyklus

Alle **10 Minuten** (`update_interval: 600`):

1. X-Posts + CMC abrufen
2. Sandbox-Hypothesen testen
3. Jeden Watchlist-Coin analysieren → ggf. Trade
4. **Telegram Cycle-Summary** (wenn `notify_on_cycle: true`)

Alle Intervalle: [DOCUMENTATION.md §3](DOCUMENTATION.md#3-wann-läuft-was--alle-intervalle)

---

## Telegram (Auszug)

| Bereich | Befehle |
|---------|---------|
| Watchlist | `/list` `/add` `/remove` |
| Handel | `/buy` `/sell` `/positions` `/orders` `/risk` `/dryrun` |
| **Transparenz** | `/decisions` `/why SYMBOL` `/hermes_last` `/hermes` `/cmc` |
| Backtest | `/backtest` `/backtest_results` `/backtest_lock` |
| Modus | `/mode` `/live_confirm` `/gate` |
| X/Twitter | `/addx` `/xsignals` `/xposts` `/testaccount` `/tracktest` |
| Sandbox | `/sandbox` `/sandbox_results` `/sandbox_promote` |

**Einsteiger-Tipp:** Nach einem Trade steht unter **„Warum:“** die Erklärung in normalem Deutsch. `/why H` zeigt die letzte Entscheidung für Humanity (H).

Vollständige Liste mit Glossar: [DOCUMENTATION.md §7](DOCUMENTATION.md#7-telegram--alle-befehle-mit-beispielen)

---

## Demo-Modus

```bash
python3 aria_bot.py --demo
```

- Separate `*.demo.json` Dateien (echtes Portfolio unberührt)
- `🧪 [DEMO]` Prefix in Telegram
- Gleiche `config.json` und Strategien

Details: [DOCUMENTATION.md §5](DOCUMENTATION.md#5-demo-modus---demo)

---

## Strategien (Kurz)

**Priorität:** `config.strategies[]` → **Hermes Memory** → **Volatile-Overlay** → Trending-Defaults

- **BUY:** Preis am unteren BB + RSI in Range + Volumen
- **SELL:** Stop-Loss → Take-Profit / Struktur (BB, Volumen) → RSI-Tiers (Cross, einmalig)
- **Cooldown:** 3–6 h zwischen Trades pro Coin
- **H/USDT & Co.:** Kein Config-Eintrag nötig — Hermes-Memory + volatile Regeln reichen

Beispiele: [DOCUMENTATION.md §6](DOCUMENTATION.md#6-strategien--wie-sie-funktionieren) · [§6.6 H/ARIA/WLD](DOCUMENTATION.md#66-praxis-beispiele--h-aria-wld-ca-30-tage-maijuni-2026)

---

## Projektstruktur

```
aria_bot.py              # Hauptschleife + Flask-Webhook
strategies/              # TA, DecisionEngine, Sandbox
services/                # Trading, Orchestrator, Social Pipeline
execution/               # Paper + Gate.io Adapter
notifications/           # Telegram-Befehle + user_explain.py (DE-Erklärungen)
hermes/                  # Self-Improvement Agent
config.json              # Strategien, Limits, Modi
DOCUMENTATION.md         # ← Vollständige Doku (inkl. §17 Feature-Branch-Workflow)
HERMES_DOKUMENTATION.md  # ← Hermes für Einsteiger
```

---

## Tests

```bash
pytest tests/unit/ -v                         # 381+ Tests
pytest tests/unit/test_dry_run_portfolio.py -v
pytest tests/unit/test_strategy_backtest.py -v
python3 scripts/gate_live_smoke_test.py       # Keys + Balance (.env)
python3 scripts/reconcile_gate_positions.py   # Live-Modus
```

---

## Wichtige Defaults (`config.json`)

| Setting | Wert |
|---------|------|
| `update_interval` | 600 (10 Min) |
| `max_usdt_per_trade` | 25 |
| `max_daily_trades` | 8 |
| `trade_cooldown_hours` | 1.0 |
| `notify_on_cycle` | true |
| `observability.telegram_explanations.enabled` | true |
| `observability.telegram_explanations.verbosity` | verbose |
| `live.dry_run` | true (sicher) |
| `live.dry_run_enhanced` | false (true = Sim-Wallet + Trending) |
| `strategy_backtest.auto_run` | true |

---

**GitHub:** https://github.com/jholze/xagent-trading-bot

Letzte Aktualisierung: 15. Juni 2026