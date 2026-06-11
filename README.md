# X-Agent Trading Bot (Version 1.4.0)

**Autonomer Krypto-Agent:** Technische Analyse (RSI, Bollinger, Volumen) + X/Twitter-Signale + CMC-Sentiment → Handelsentscheidungen mit Risiko-Limits, Cooldowns und Telegram-Steuerung.

> **Vollständige Dokumentation:** [DOCUMENTATION.md](DOCUMENTATION.md) — Architektur, Intervalle, Strategien mit Beispielen, alle Telegram-Befehle, Demo-Modus, X/Twitter, Sandbox.

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

In Telegram: `/help` senden.

---

## Was ist neu in 1.4 (Juni 2026)

- **Gate.io Live-Trading** — Paper + Live Mainnet (2-Stufen: `/mode live` + `/live_confirm`)
- **Anti-Churn** — Trade-Cooldowns, RSI-Cross-Sells, einmalige Sell-Tiers
- **Take-Profit** — `take_profit_pct` pro Coin + X `price_target`
- **5 Coin-Strategien** — ARIA, RAVE, HIGH, SOL, BTC (4h, 25 USDT/Trade)
- **Telegram** — SIGNAL / EXECUTED / BLOCKED, Mode-Badges, Cycle-Summaries
- **ngrok-Neustart** — `start_demo_with_ngrok.sh` richtet Tunnel + Webhook bei jedem Start neu ein

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
| Handel | `/buy` `/sell` `/positions` `/risk` |
| Modus | `/mode` `/live_confirm` `/gate` |
| X/Twitter | `/addx` `/xsignals` `/xposts` `/testaccount` `/tracktest` |
| Sandbox | `/sandbox` `/sandbox_results` `/sandbox_promote` |

Vollständige Liste mit Beispielen: [DOCUMENTATION.md §7](DOCUMENTATION.md#7-telegram--alle-befehle-mit-beispielen)

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

Pro Coin in `config.json` → `strategies[]`:

- **BUY:** Preis am unteren BB + RSI in Range + Volumen
- **SELL:** Stop-Loss → Take-Profit → RSI-Tiers (Cross, einmalig)
- **Cooldown:** 3–6 h zwischen Trades pro Coin

Beispiele: [DOCUMENTATION.md §6](DOCUMENTATION.md#6-strategien--wie-sie-funktionieren)

---

## Projektstruktur

```
aria_bot.py              # Hauptschleife + Flask-Webhook
strategies/              # TA, DecisionEngine, Sandbox
services/                # Trading, Orchestrator, Social Pipeline
execution/               # Paper + Gate.io Adapter
notifications/           # Telegram-Befehle
config.json              # Strategien, Limits, Modi
DOCUMENTATION.md         # ← Vollständige Doku
```

---

## Tests

```bash
pytest tests/unit/ -v
pytest tests/unit/test_trade_cooldown.py -v
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
| `live.dry_run` | true (sicher) |

---

**GitHub:** https://github.com/jholze/xagent-trading-bot

Letzte Aktualisierung: 11. Juni 2026