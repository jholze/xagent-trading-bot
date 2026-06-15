# X-Agent Trading Bot (Version 1.9.0)

**Language:** [Deutsch](README.md) · English

**Autonomous crypto agent:** Technical analysis (RSI, Bollinger, volume) + X/Twitter signals + CMC sentiment → trading decisions with risk limits, cooldowns, and Telegram control.

**Beginner-friendly:** Every important Telegram message explains **why** the bot did something — in plain language, no trading background required.

> **Full documentation:** [DOCUMENTATION.en.md](DOCUMENTATION.en.md) — architecture, strategies, **transparency & glossary**, all Telegram commands, Hermes, demo mode.

---

## Quick start

```bash
cd ~/Documents/scripts/trading_bot
pip3 install -r requirements.txt

# Recommended: bot + ngrok + Telegram webhook (stops old processes automatically)
bash scripts/start_demo_with_ngrok.sh

# Stop
bash scripts/stop_bot.sh
```

In Telegram: use the menu button next to the input field (all commands) or send `/help`.

---

## What's new in 1.9 (June 2026)

- **Volatile altcoin profile** — auto-detects hectic coins (ATR, meme, micro-cap); extra sell rules (upper Bollinger, volume exhaustion, volume dump); **shadow mode** to observe without execution ([docs §6.5](DOCUMENTATION.en.md#65-strategy-selection--who-gets-which-rules))
- **Hermes memory as live fallback** — learned params from `hermes/memory/baseline.json` apply in the bot for coins **without** a `config.strategies[]` entry (e.g. H/USDT) — across sell, rebuy, and new buys ([HERMES_DOCUMENTATION.md](HERMES_DOCUMENTATION.md))
- **Practical examples H / ARIA / WLD** — ~30-day walkthrough in plain language ([docs §6.6](DOCUMENTATION.en.md#66-practical-examples--h-aria-wld-30-days-mayjune-2026))
- **381 unit tests** — including volatile profile, Hermes fallback, market structure

### From 1.8

- **CMC churn protection** — stable quote IDs, TA requirement for sells, higher sell thresholds, social sell cooldowns
- **Build info in Telegram** — version + git branch on restart, `/gate`, `/mode`
- **Telegram transparency** — trade messages with **“Why:”** in plain language + technical line
- **New commands** — `/decisions`, `/why SYMBOL`, `/hermes_last`
- **Cycle digests** — CMC/X signals and Hermes learning cycles explained automatically
- **Decision log** — `logs/decisions.jsonl` for all analyses
- **Showcase script** — `python3 scripts/telegram_transparency_showcase.py`
- **Coin links** — clickable CMC/Gate/chart links + mini chart on executed trades

### From 1.7

- **Hermes hybrid pool** — pins + open positions + CMC top coins (max 8), `signal_activity` rotation
- **Live evidence & dual mode** — dry-run ledger + counterfactual for safer Hermes promotions
- **Daily report** — `scripts/daily_auswertung.py` → `auswertungen/YYYY-MM-DD_tag.md` (cron 23:55)

### From 1.6 / 1.5

- **Enhanced dry run** — sim wallet ($5000), CMC trending watchlist, `/dryrun`
- **Sim-cash fix** — cash recalculated from trade history; portfolio = sim cash + positions
- **Strategy backtest** — auto backtest + parameter tuning per coin (`/backtest`)
- **Manual vs auto** — `/orders` and `/positions` show trade source correctly
- **Scope ledger** — `positions.live.json` / `positions.paper.json` separated from paper ledger

### From 1.4

- **Gate.io live trading** — paper + live mainnet (two-step: `/mode live` + `/live_confirm`)
- **Order ledger** — `/orders` scope-isolated (demo/paper/live)
- **Anti-churn** — cooldowns, RSI-cross sells, take-profit
- **Production start** — `scripts/start_with_ngrok.sh` (without `--demo`)

---

## Trading modes (short)

| Mode | Command | Description |
|------|---------|-------------|
| Paper | `/mode paper` | Virtual ledger (default) |
| Live | `/mode live` + `/live_confirm` | Gate.io mainnet (`dry_run` on by default) |
| Off | `/mode off` | Analysis only |

Details: [DOCUMENTATION.en.md §4](DOCUMENTATION.en.md#4-trading-modes)

---

## Main cycle

Every **10 minutes** (`update_interval: 600`):

1. Fetch X posts + CMC
2. Test sandbox hypotheses
3. Analyze each watchlist coin → trade if needed
4. **Telegram cycle summary** (if `notify_on_cycle: true`)

All intervals: [DOCUMENTATION.en.md §3](DOCUMENTATION.en.md#3-schedules--all-intervals)

---

## Telegram (excerpt)

| Area | Commands |
|------|----------|
| Watchlist | `/list` `/add` `/remove` |
| Trading | `/buy` `/sell` `/positions` `/orders` `/risk` `/dryrun` |
| **Transparency** | `/decisions` `/why SYMBOL` `/hermes_last` `/hermes` `/cmc` |
| Backtest | `/backtest` `/backtest_results` `/backtest_lock` |
| Mode | `/mode` `/live_confirm` `/gate` |
| X/Twitter | `/addx` `/xsignals` `/xposts` `/testaccount` `/tracktest` |
| Sandbox | `/sandbox` `/sandbox_results` `/sandbox_promote` |

**Beginner tip:** After a trade, read **“Why:”** in plain language. `/why H` shows the last decision for Humanity (H).

Full list with glossary: [DOCUMENTATION.en.md §7](DOCUMENTATION.en.md#7-telegram--all-commands-with-examples)

---

## Demo mode

```bash
python3 aria_bot.py --demo
```

- Separate `*.demo.json` files (real portfolio untouched)
- `🧪 [DEMO]` prefix in Telegram
- Same `config.json` and strategies

Details: [DOCUMENTATION.en.md §5](DOCUMENTATION.en.md#5-demo-mode---demo)

---

## Strategies (short)

**Priority:** `config.strategies[]` → **Hermes memory** → **volatile overlay** → trending defaults

- **BUY:** price at lower BB + RSI in range + volume
- **SELL:** stop-loss → take-profit / structure (BB, volume) → RSI tiers (cross, once each)
- **Cooldown:** 3–6 h between trades per coin
- **H/USDT & co.:** No config entry needed — Hermes memory + volatile rules are enough

Examples: [DOCUMENTATION.en.md §6](DOCUMENTATION.en.md#6-strategies--how-they-work) · [§6.6 H/ARIA/WLD](DOCUMENTATION.en.md#66-practical-examples--h-aria-wld-30-days-mayjune-2026)

---

## Project structure

```
aria_bot.py              # Main loop + Flask webhook
strategies/              # TA, DecisionEngine, Sandbox
services/                # Trading, orchestrator, social pipeline
execution/               # Paper + Gate.io adapter
notifications/           # Telegram commands + user_explain.py
hermes/                  # Self-improvement agent
config.json              # Strategies, limits, modes
DOCUMENTATION.en.md      # Full docs (incl. §17 feature-branch workflow)
HERMES_DOCUMENTATION.md  # Hermes for beginners (EN)
HERMES_DOKUMENTATION.md  # Hermes für Einsteiger (DE)
```

---

## Tests

```bash
pytest tests/unit/ -v                         # 381+ tests
pytest tests/unit/test_dry_run_portfolio.py -v
pytest tests/unit/test_strategy_backtest.py -v
python3 scripts/gate_live_smoke_test.py       # Keys + balance (.env)
python3 scripts/reconcile_gate_positions.py   # Live mode
```

---

## Important defaults (`config.json`)

| Setting | Value |
|---------|-------|
| `update_interval` | 600 (10 min) |
| `max_usdt_per_trade` | 25 |
| `max_daily_trades` | 8 |
| `trade_cooldown_hours` | 1.0 |
| `notify_on_cycle` | true |
| `observability.telegram_explanations.enabled` | true |
| `observability.telegram_explanations.verbosity` | verbose |
| `live.dry_run` | true (safe) |
| `live.dry_run_enhanced` | false (true = sim wallet + trending) |
| `strategy_backtest.auto_run` | true |

---

**GitHub:** https://github.com/jholze/xagent-trading-bot

Last updated: 15 June 2026