# X-Agent Trading Bot

An autonomous crypto trading agent that combines **technical analysis** with **signals from selected X (Twitter) accounts**. It analyses tweets using Grok (LLM), scores them, and trades automatically while maintaining strong risk controls.

## Current Features
- Hybrid decision making (X signals + RSI + Bollinger Bands + Volume)
- Real-time X signal parsing with confidence scoring
- Virtual trading with accurate PnL (weighted average entry)
- Full management via Telegram (`/buy`, `/sell`, `/positions`, `/addx`, `/xsignals`, `/help`, etc.)
- Configurable update interval (default 10 minutes)
- Clean split terminal UI with live sections
- Comprehensive test suite (12+ tests)

## How It Works
1. Monitors curated X accounts (add/remove via `/addx` and `/listx`)
2. Uses Grok to parse tweets into structured signals (coin, action, confidence, rationale)
3. Combines X signals with technical analysis for hybrid scoring
4. Executes virtual trades automatically (real execution can be enabled)
5. Tracks positions, PnL, and account performance
6. Sends important events to Telegram automatically

## Big Plan Progress
- **Phase 0 (Foundation)**: Completed (config, XAnalyzer, x_accounts.json, Telegram commands)
- **Phase 1 (X Data + Parsing)**: Mostly completed (LLM parsing, /xsignals, /addx)
- **Phase 2 (Analysis & Scoring)**: Partially completed (hybrid scoring, average cost PnL)
- **Phase 3 (Autonomous Execution)**: Virtual trading done, real execution next
- **Phase 4 (Intelligence & Polish)**: Learning loop and dashboard pending

**Next Steps**: Real tweet fetching (Twitter API), on-chain data, real execution layer, performance learning.

## Configuration (`config.json`)
- `update_interval`: Seconds between cycles (default 600 = 10 min)
- `x_accounts`: List of monitored X accounts (managed via Telegram)
- `min_x_confidence`: Minimum confidence to act on X signals
- `x_weight`, `technical_weight`: Hybrid scoring weights
- `virtual_trading`: Set to `false` for real trading (use with caution)
- `max_usdt_per_trade`, `max_open_positions`, `stop_loss_pct`

## Telegram Commands (`/help` for full list)
- `/addx ACCOUNT` — Add X account
- `/listx` — List monitored X accounts
- `/xsignals` — Show latest parsed X signals
- `/buy SYMBOL USDT` or `/buy NUMBER USDT` — Virtual buy
- `/sell NUMBER PERCENT` — Sell from position (first use `/sell` to list)
- `/positions` — Portfolio overview with PnL and last trades
- `/help` — Show all commands with examples

## Files
- `aria_bot.py` — Main bot loop and webhook
- `x_analyzer.py` — X signal fetching and LLM parsing
- `strategies/core_strategy.py` — Hybrid decision engine
- `strategies/positions.py` — Position tracking with average cost PnL
- `telegram_notifier.py` — All Telegram commands and notifications
- `data_manager.py` — Config, watchlist, trade history
- `terminal_ui.py` — New split terminal interface
- `tests/` — Comprehensive test suite
- `x_accounts.json` — Monitored X accounts (rich metadata)
- `trade_history.json` — Virtual portfolio and trade log
- `.env` — API keys (XAI_API_KEY, TELEGRAM_BOT_TOKEN, etc.)

## Installation & Start
```bash
cd ~/Documents/scripts/trading_bot
pip3 install -r requirements.txt  # rich, ccxt, pandas, ta-lib, python-dotenv, flask, openai
python3 aria_bot.py
```

First run `ngrok http 5000` and set the webhook with the curl command (see earlier messages).

## Important Notes
- Start with `virtual_trading: true` for safety.
- Add good X accounts via `/addx`.
- Monitor the terminal and Telegram for activity.
- The bot is designed for stability (10 min cycles by default).

**Contributing**: Report issues or suggestions at the GitHub repo.

Last updated: 23 May 2026
