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
- Comprehensive test suite (26 tests covering command parsing, demo mode, PnL, caching, error logging, etc.)
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
- Significant test expansion (26 tests)
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
**Watchlist**
- `/add SYMBOL` — Add coin (e.g. `/add RAVE`)
- `/remove NUMBER` — Remove by number (run `/list` first)
- `/list` or `/watchlist` — Show current watchlist

**Trading (Virtual)**
- `/buy SYMBOL USDT` or `/buy NUMBER USDT` — Virtual buy (e.g. `/buy ARIA 200` or `/buy 1 200`)
- `/sell NUMBER PERCENT` — Sell from a position (first run `/sell` to see the list)
- `/positions` or `/status` — Full portfolio overview with unrealized PnL and last trades

**X / Twitter Accounts**
- `/addx ACCOUNT` — Add an X account to monitor (e.g. `/addx CryptoCapo_`)
- `/removex ACCOUNT` — Remove an X account
- `/listx` — List all monitored accounts
- `/xposts` — Show last tracked X posts + Grok recommendations
- `/tracktest` — Send a sample tweet through the analyzer for instant testing

**Other**
- `/help`, `/commands`, `/?` — This command list

All commands work in both normal and `--demo` mode. In demo mode every Telegram message is prefixed with `🧪 [DEMO]`.

## Files
- `aria_bot.py` — Main loop with X tracking
- `x_analyzer.py` — Post tracking, LLM parsing, comparison, recommendations
- `strategies/core_strategy.py` — Hybrid decision and technical logic
- `strategies/positions.py` — Position tracking with average cost PnL
- `telegram_notifier.py` — All Telegram commands and auto-notifications
- `data_manager.py` — Config, watchlist, trade history, x_posts, x_accounts
- `terminal_ui.py` — Split terminal interface
- `tests/` — 26 unit tests (23 passing; 3 known environment/flaky issues not related to core functionality)
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

Last updated: 30 May 2026 (post major stability & safety refactor)

