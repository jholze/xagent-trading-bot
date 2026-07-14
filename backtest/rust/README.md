# Rust Regime Backtester (Skeleton - Functional CLI)

High-performance backtesting for the adaptive regime system (pure spot).

Current state: self-contained functional CLI skeleton.
- Parses --symbols, --start, --end, --output
- Generates synthetic OHLCV
- Runs simplified RegimeDetector fusion (tech 0.62 + sent 0.38)
- StrategyAllocator style rules (grid priority in ranging, mom in trend, defensive on extreme neg)
- Walk-forward style loop + per-regime contrib stats
- Emits JSON report (for parity comparison with Python)

## Build & Run (when Rust available)
cd backtest/rust
cargo build --release
./target/release/regime-backtester --symbols BTC/USDT,ETH/USDT --start 2024-01-01 --end 2025-06-01 --output /tmp/regime_rust.json

## Python parity helper
See ../regime_parity_check.py (uses real RegimeDetector + Allocator for output comparison).

## Planned next
- Real OHLCV loader (csv or ccxt)
- talib-equiv indicators in Rust (or link via PyO3)
- Monte-Carlo + WFO
- Full metrics: Sharpe per regime, maxDD, turnover
- Comparison mode vs legacy volatility_tier profiles

Python can shell out to the binary when present for heavy jobs.
