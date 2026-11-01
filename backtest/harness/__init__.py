"""Reusable offline backtest harness for the paper bot.

Replays the recorded order tape and ledger against public OHLCV and runs the
bot's own stop and DCA functions (``strategies.dca``) offline. It never
imports an exchange client with keys, never writes to Mongo, and never places
orders. Entry point: ``python -m backtest.harness --help``.
"""
