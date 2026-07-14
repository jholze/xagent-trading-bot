// Rust regime-aware backtester skeleton (functional CLI).
// Pure spot, no leverage. Walk-forward style simulation + regime metrics.
// For speed comparison vs legacy Python profiles. Extendable to real data/TA.

use std::env;
use std::fs;
use std::process;

#[derive(Debug, Clone)]
struct Bar {
    ts: i64,
    close: f64,
    high: f64,
    low: f64,
}

#[derive(Debug)]
struct RegimeResult {
    primary: String,
    confidence: f64,
    sentiment: f64,
}

#[derive(Debug)]
struct SimResult {
    final_equity: f64,
    trades: usize,
    regime_stats: std::collections::HashMap<String, (usize, f64)>, // regime -> (bars, realized_pnl)
}

fn parse_args() -> (Vec<String>, String, String, String) {
    let mut symbols = vec!["BTC/USDT".to_string()];
    let mut start = "2024-01-01".to_string();
    let mut end = "2025-01-01".to_string();
    let mut out = "backtest_results.json".to_string();

    let args: Vec<String> = env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--symbols" | "-s" => {
                if i + 1 < args.len() {
                    symbols = args[i + 1]
                        .split(',')
                        .map(|s| s.trim().to_string())
                        .collect();
                    i += 2;
                } else {
                    i += 1;
                }
            }
            "--start" => {
                if i + 1 < args.len() {
                    start = args[i + 1].clone();
                    i += 2;
                } else {
                    i += 1;
                }
            }
            "--end" => {
                if i + 1 < args.len() {
                    end = args[i + 1].clone();
                    i += 2;
                } else {
                    i += 1;
                }
            }
            "--output" | "-o" => {
                if i + 1 < args.len() {
                    out = args[i + 1].clone();
                    i += 2;
                } else {
                    i += 1;
                }
            }
            "--help" | "-h" => {
                println!(
                    "regime-backtester\n\
                     Usage: regime-backtester [--symbols BTC/USDT,ETH/USDT] [--start 2024-01-01] [--end 2025-01-01] [--output results.json]\n\
                     Simulates regime switching (RANGING/MOMENTUM/DEFENSIVE) + simple grid/momentum allocation.\n\
                     Outputs JSON metrics for parity checks."
                );
                process::exit(0);
            }
            _ => {
                i += 1;
            }
        }
    }
    (symbols, start, end, out)
}

fn synthetic_bars(n: usize, seed_trend: f64) -> Vec<Bar> {
    let mut bars = Vec::with_capacity(n);
    let mut price = 100.0;
    for i in 0..n {
        let noise = ((i as f64).sin() * 0.8) + ((i % 7) as f64 - 3.0) * 0.1;
        price += seed_trend + noise * 0.3;
        let close = price;
        bars.push(Bar {
            ts: 1704067200 + (i as i64) * 3600,
            close,
            high: close * 1.004,
            low: close * 0.996,
        });
    }
    bars
}

fn detect_regime(bars: &[Bar], idx: usize, sentiment: f64) -> RegimeResult {
    if idx < 30 {
        return RegimeResult {
            primary: "TRANSITION".into(),
            confidence: 0.3,
            sentiment,
        };
    }
    let look = 20usize;
    let start = if idx > look { idx - look } else { 0 };
    let recent: Vec<f64> = bars[start..=idx].iter().map(|b| b.close).collect();
    let first = recent.first().copied().unwrap_or(100.0);
    let last = recent.last().copied().unwrap_or(100.0);
    let slope = (last - first) / first.max(1.0);

    let tech = (slope * 12.0).clamp(-1.0, 1.0);
    let fused = (0.62 * tech + 0.38 * sentiment).clamp(-1.0, 1.0);

    let primary = if fused > 0.5 {
        "STRONG_UPTREND"
    } else if fused < -0.5 {
        "STRONG_DOWNTREND"
    } else if fused.abs() < 0.35 {
        "RANGING"
    } else {
        "TRANSITION"
    }
    .to_string();

    RegimeResult {
        primary,
        confidence: (0.4 + 0.6 * fused.abs()).clamp(0.0, 0.98),
        sentiment,
    }
}

fn simulate_strategy(bars: &[Bar], regime: &RegimeResult) -> (f64, bool) {
    // Very simplified: momentum prefers trend, grid likes ranging.
    // Returns (pnl_delta, is_trade)
    let w_grid = if regime.primary == "RANGING" { 0.7 } else { 0.2 };
    let w_mom = 1.0 - w_grid;

    if bars.len() < 5 {
        return (0.0, false);
    }
    let ret = (bars.last().unwrap().close - bars[bars.len() - 5].close) / bars[bars.len() - 5].close.max(1.0);

    let mut pnl = 0.0;
    let mut traded = false;
    if regime.primary.contains("UPTREND") && w_mom > 0.5 {
        pnl = ret.abs() * 0.8; // mock long
        traded = true;
    } else if regime.primary.contains("RANGING") && w_grid > 0.5 {
        // grid scalps small mean rev
        pnl = if ret.abs() > 0.01 { ret.signum() * -0.004 } else { 0.003 };
        traded = true;
    } else if regime.primary.contains("DOWNTREND") {
        pnl = ret * -0.3; // defensive
        traded = true;
    }
    // exposure mult implicit
    if regime.sentiment < -0.55 {
        pnl *= 0.3;
    }
    (pnl, traded)
}

fn run_backtest(symbols: &[String]) -> SimResult {
    let mut total_equity = 10000.0;
    let mut total_trades = 0usize;
    let mut regime_stats: std::collections::HashMap<String, (usize, f64)> = std::collections::HashMap::new();

    for sym in symbols {
        let bars = synthetic_bars(800, if sym.contains("BTC") { 0.015 } else { 0.008 });
        let mut equity = 1000.0;
        let mut pos = 0.0;
        let mut last_reg = "INIT".to_string();

        for i in 30..bars.len() {
            // fake sentiment oscillation
            let sent = ((i as f64) / 90.0).sin() * 0.7;
            let reg = detect_regime(&bars, i, sent);

            let (delta, traded) = simulate_strategy(&bars[i - 20..=i], &reg);
            equity += delta * (equity * 0.2); // risk 20% notion mock
            if traded {
                total_trades += 1;
            }

            let entry = regime_stats.entry(reg.primary.clone()).or_insert((0, 0.0));
            entry.0 += 1;
            entry.1 += delta;

            if reg.primary != last_reg {
                last_reg = reg.primary.clone();
            }
        }
        total_equity += equity;
    }

    SimResult {
        final_equity: total_equity,
        trades: total_trades,
        regime_stats,
    }
}

fn main() {
    let (symbols, start, end, out_path) = parse_args();
    println!("Rust regime-backtester starting");
    println!("symbols={:?} start={} end={} out={}", symbols, start, end, out_path);

    let result = run_backtest(&symbols);

    let mut out_map = std::collections::HashMap::new();
    out_map.insert("final_equity_usdt", result.final_equity);
    out_map.insert("total_trades", result.trades as f64);
    // simple regime breakdown
    let mut regimes = vec![];
    for (k, (bars, pnl)) in &result.regime_stats {
        regimes.push(serde_json::json!({
            "regime": k,
            "bars": bars,
            "pnl_contrib": pnl,
            "share": *bars as f64 / 800.0
        }));
    }

    let report = serde_json::json!({
        "ok": true,
        "symbols": symbols,
        "period": {"start": start, "end": end},
        "metrics": {
            "final_equity": result.final_equity,
            "trades": result.trades,
            "regime_distribution": regimes
        },
        "note": "Skeleton: synthetic data + rule-based regime fusion (0.62 tech + 0.38 sent). Extend with real OHLCV loader + talib equiv."
    });

    let s = serde_json::to_string_pretty(&report).unwrap();
    if let Err(e) = fs::write(&out_path, &s) {
        eprintln!("write failed: {}", e);
    }
    println!("{}", s);
    println!("Wrote {}", out_path);
}
