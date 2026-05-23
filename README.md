# X-Agent Trading Bot (Version 1.0)

**Stable autonomous agent that analyses X (Twitter) posts, compares them to technical strategy, recommends actions (trade, sell, watchlist add), and executes virtual trades.**

## Current Features
- Real X post tracking and LLM parsing with Grok
- Comparison to current technical code (RSI, Bollinger, Volume)
- Action recommendations (BUY, SELL, ADD_TO_WATCHLIST, IGNORE)
- Hybrid scoring and autonomous virtual trading
- Accurate PnL with weighted average entry price
- Full Telegram management (`/buy`, `/sell`, `/positions`, `/addx`, `/xsignals`, `/xposts`, `/tracktest`, `/help`, `/listx`)
- Clean split terminal UI with live sections
- Comprehensive test suite (15 tests covering parsing, PnL, UI, commands)
- Configurable update interval (default 10 minutes)

## How It Works
1. Monitors your X accounts (add/remove with `/addx` and `/listx`)
2. Tracks every relevant post and parses it with Grok into structured signals
3. Compares the X signal to your current technical strategy (`check_signal`)
4. Recommends an action and logs it to `x_posts.json`
5. Executes virtual trades if recommendation is strong
6. Sends automatic notifications to Telegram for important events
7. You can test instantly with `/tracktest` or view history with `/xposts`

## Big Plan Progress
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

## Telegram Commands (`/help` for full list with examples)
- **X Management**: `/addx ACCOUNT`, `/removex ACCOUNT`, `/listx`, `/xsignals`, `/xposts`, `/tracktest`
- **Trading**: `/buy SYMBOL USDT` or `/buy NUMBER USDT`, `/sell NUMBER PERCENT`, `/positions` or `/status`
- **General**: `/help`, `/list` (watchlist)

## Files
- `aria_bot.py` — Main loop with X tracking
- `x_analyzer.py` — Post tracking, LLM parsing, comparison, recommendations
- `strategies/core_strategy.py` — Hybrid decision and technical logic
- `strategies/positions.py` — Position tracking with average cost PnL
- `telegram_notifier.py` — All Telegram commands and auto-notifications
- `data_manager.py` — Config, watchlist, trade history, x_posts, x_accounts
- `terminal_ui.py` — Split terminal interface
- `tests/` — 15 tests covering commands, PnL, UI, tracking, parsing
- `x_accounts.json` — Monitored X accounts with trust scores
- `x_posts.json` — Tracked posts and recommendations
- `trade_history.json` — Virtual portfolio and trades
- `.env` — API keys (never commit this file)

## Installation & Start
```bash
cd ~/Documents/scripts/trading_bot
pip3 install -r requirements.txt  # rich, ccxt, pandas, ta-lib, python-dotenv, flask, openai
python3 aria_bot.py
```

First run `ngrok http 5000` and set the webhook (see earlier messages or /help in Telegram).

## Important Notes
- Start with `virtual_trading: true` for safety.
- Add good X accounts with `/addx`.
- Use `/tracktest` to test tracking instantly.
- Use `/xposts` to see tracked posts and recommendations.
- The bot is designed for stability (10 min cycles by default, configurable).

**GitHub**: https://github.com/jholze/xagent-trading-bot (Version 1.0 tagged and released)

**Contributing**: Report issues at the repo.

Last updated: 23 May 2026

