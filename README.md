# X-Agent Trading Bot (Version 1.0)

**Stable autonomous agent that analyses X (Twitter) posts, compares them to technical strategy, recommends actions (trade, sell, watchlist add), and executes virtual trades.**

## Current Features
- Real X (Twitter) post tracking and LLM parsing with Grok-4
- Hybrid scoring: X signals vs. technical analysis (RSI, Bollinger Bands, Volume)
- Action recommendations (BUY, SELL, ADD_TO_WATCHLIST, IGNORE) with rationale
- Accurate virtual trading with weighted-average entry price and full PnL tracking
- Full Telegram bot management (`/buy`, `/sell`, `/positions`, `/addx`, `/removex`, `/listx`, `/xposts`, `/tracktest`, `/help` and more)
- Clean split terminal UI (rich library) with live sections
- **Major stability & safety refactor** (May/June 2026): atomic JSON writes, price caching, global state protection with locks, proper error logging instead of silent `pass`, `os.isatty` guards, centralized config, duplicate code removal, and more
- Comprehensive test suite (96+ unit tests: portfolio equity, X pipeline, commands, PnL, caching, etc.)
- Safe **--demo mode** for testing without touching live data files (separate `*.demo.json` + 🧪 [DEMO] prefixes in Telegram)
- Configurable update interval (default 10 minutes)

## How It Works
1. Monitors your X accounts (add/remove with `/addx` and `/listx`)
2. Tracks every relevant post and parses it with Grok into structured signals
3. Compares the X signal to your current technical strategy (`check_signal`)
4. Recommends an action and logs it to `x_posts.json`
5. Executes virtual trades if recommendation is strong
6. Sends automatic notifications to Telegram for important events
7. You can test instantly with `/tracktest` or view history with `/xposts`

## Major Stability & Safety Refactor (Completed on this branch)
This release includes a comprehensive audit and refactoring pass focused on reliability and safe operation:

- All critical safety issues fixed (duplicate exception handlers, silent failures, fragile screen clearing, global mutable state without protection, repeated network calls, etc.)
- Full `--demo` mode (user-chosen option C): completely separate data files (`watchlist.demo.json`, `positions.demo.json`, `trade_history.demo.json`, etc.) + automatic "🧪 [DEMO]" prefix on all Telegram messages
- Significant test expansion (portfolio multi-buy/sell, X pipeline, pytest runner)
- Robust Telegram command parsing with safe helpers and validation
- Atomic file writes, proper logging at ERROR/WARNING level, price caching, centralized config access

The bot is now much safer to run in production while still allowing realistic testing.

## Big Plan Progress (Product Roadmap)
- **Phase 0 (Foundation)**: Completed
- **Phase 1 (X Data + Parsing)**: Completed (LLM parsing, commands)
- **Phase 2 (Analysis & Scoring + Tracking)**: Completed (post tracking, comparison to current code, recommendations, visibility, tests)
- **Phase 3 (Autonomous Execution)**: Virtual trading done — real execution next
- **Phase 4 (Intelligence & Polish)**: Learning loop, on-chain data, dashboard pending

**Next Steps**: Real tweet fetching (Twitter API), on-chain data, real execution, automatic learning from tracked posts.

## Configuration (`config.json`)
- `update_interval`: Seconds between cycles (default 600 = 10 min)
- `x_accounts`: Managed via Telegram (`/addx`, `/listx`)
- `min_x_confidence`: Minimum confidence for recommendations (default 65)
- `x_weight`, `technical_weight`, `onchain_weight`: Scoring weights
- `virtual_trading`: `true` for safety (set `false` for real trading)
- Other settings for trade size, stop-loss, max positions

## Telegram Commands (send `/help` in Telegram for the latest list)

**Tipp:** Wenn du einen Befehl ohne Parameter sendest (z.B. nur `/buy`), antwortet der Bot sofort mit einem Beispiel.

### Watchlist — welche Coins der Bot beobachtet
| Befehl | Was passiert | Beispiel |
|--------|--------------|----------|
| `/list` | Alle Coins anzeigen | `/list` |
| `/add SYMBOL` | Coin hinzufügen | `/add RAVE` |
| `/remove NUMMER` | Coin entfernen (Nummer aus `/list`) | `/remove 2` |

### Handel — kaufen, verkaufen, Portfolio
| Befehl | Was passiert | Beispiel |
|--------|--------------|----------|
| `/buy SYMBOL USDT` | Coin kaufen | `/buy ARIA 200` |
| `/buy NUMMER USDT` | Coin per Listen-Nummer kaufen | `/buy 1 200` |
| `/sell` | Offene Positionen anzeigen | `/sell` |
| `/sell NUMMER PROZENT` | Anteil verkaufen | `/sell 1 30` |
| `/positions` | Portfolio, Kurse, Gewinn/Verlust | `/positions` |
| `/risk` | Risiko-Limits und Drawdown | `/risk` |

### Modus & Sicherheit
| Befehl | Was passiert | Beispiel |
|--------|--------------|----------|
| `/mode` | Aktuellen Handelsmodus anzeigen | `/mode` |
| `/mode paper` | Virtuelles Geld (Standard) | `/mode paper` |
| `/mode gate_testnet` | Gate.io Testnet | `/mode gate_testnet` |
| `/mode live` | Echtes Geld (braucht Bestätigung) | `/mode live` |
| `/live_confirm` | Live-Handel bestätigen | `/live_confirm` |
| `/live_cancel` | Live abbrechen, zurück zu Paper | `/live_cancel` |
| `/gate` | Gate.io API-Status | `/gate` |

### X / Twitter — Posts analysieren
| Befehl | Was passiert | Beispiel |
|--------|--------------|----------|
| `/addx ACCOUNT` | X-Account überwachen | `/addx CryptoCapo_` |
| `/removex ACCOUNT` | X-Account entfernen | `/removex CryptoCapo_` |
| `/listx` | Überwachte Accounts | `/listx` |
| `/xsignals` | Aktuelle starke Signale | `/xsignals` |
| `/xposts` | Letzte analysierte Posts | `/xposts` |
| `/xaccuracy` | Trefferquote (Leaderboard) | `/xaccuracy` |
| `/tracktest` | Test-Tweet sofort analysieren | `/tracktest` |

### Sandbox & CMC
| Befehl | Was passiert | Beispiel |
|--------|--------------|----------|
| `/sandbox` | Strategie-Experimente | `/sandbox` |
| `/sandbox_results ID` | Details zu einem Experiment | `/sandbox_results hyp_abc` |
| `/sandbox_promote ID` | Erfolgreiche Strategie aktivieren | `/sandbox_promote hyp_abc` |
| `/cmc` | CoinMarketCap Sentiment | `/cmc` |

### Hilfe
- `/help`, `/commands`, `/?` — Vollständige Befehlsliste

All commands work in both normal and `--demo` mode. In demo mode every Telegram message is prefixed with `🧪 [DEMO]`.

## Files
- `aria_bot.py` — Main loop with X tracking
- `x_analyzer.py` — Post tracking, LLM parsing, comparison, recommendations
- `strategies/core_strategy.py` — Hybrid decision and technical logic
- `strategies/positions.py` — Position tracking with average cost PnL
- `telegram_notifier.py` — All Telegram commands and auto-notifications
- `data_manager.py` — Config, watchlist, trade history, x_posts, x_accounts
- `terminal_ui.py` — Split terminal interface
- `tests/` — Unit + integration tests (`pytest tests/unit/`)
- `x_accounts.json` — Monitored X accounts with trust scores
- `x_posts.json` — Tracked posts and recommendations
- `trade_history.json` — Virtual portfolio and trades
- `*.demo.json` — Separate data files created automatically when using `--demo` (safe testing)
- `requirements.txt` — Python dependencies
- `.env` — API keys (never commit this file)

## Installation & Start
```bash
cd ~/Documents/scripts/trading_bot
pip3 install -r requirements.txt
python3 aria_bot.py --demo     # ← recommended for first testing (safe, separate data files)
```

**First time / safe testing**
1. Start with `--demo` — this creates isolated `*.demo.json` files and adds `🧪 [DEMO]` to every Telegram message.
2. Run `ngrok http 5000` (or your preferred tunnel) and register the webhook with Telegram.
3. Send `/help` in Telegram to see all commands.
4. Use `/tracktest` for instant X-analyzer testing without waiting for real posts.

Once you are happy, you can run without `--demo` for live operation (still virtual trading by default via `config.json`).

## Tests
```bash
pip3 install -r requirements.txt
pytest tests/unit/ -v              # 96+ unit tests (portfolio, X pipeline, commands, …)
pytest tests/integration/ -m integration   # optional stress test (network, slower)
DEMO_MODE=1 python3 tests/integration/full_bot_stress_test.py
```

## Important Notes
- `virtual_trading: true` in `config.json` is the safe default (strongly recommended).
- The entire audit/refactor focused on making the bot stable and safe to operate long-term.
- `--demo` mode is the best way to experiment with commands, positions, and X tracking without affecting your real data files.
- All JSON writes use atomic rename (crash-safe).
- Price fetching has a simple TTL cache to reduce external API calls.
- Global state (positions) is protected by a lock.
- Error paths now log properly instead of failing silently.

**GitHub**: https://github.com/jholze/xagent-trading-bot

**Contributing**: Report issues at the repo.

Last updated: 7 June 2026 (tests, docs, Telegram hints)

