"""Regime series for the backtest harness must not look ahead (2026-11-01)."""

import math

from backtest.harness import regime as rg

H = 3600


def _bars(n, start=1_784_505_600, f=lambda i: 100 + 10 * math.sin(i / 30.0) + i * 0.01):
    return [(start + i * H, f(i), f(i) * 1.01, f(i) * 0.99, f(i + 0.5), 10.0) for i in range(n)]


def _daily(h):
    out = []
    for i in range(0, len(h) - 23, 24):
        blk = h[i : i + 24]
        out.append((blk[0][0], blk[0][1], max(b[2] for b in blk), min(b[3] for b in blk), blk[-1][4], 1.0))
    return out


def test_labels_do_not_change_when_future_bars_are_cut():
    btc = _bars(24 * 120)
    eth = _bars(24 * 120, f=lambda i: 50 + 5 * math.cos(i / 25.0))
    cut = btc[24 * 100][0]
    full = {
        "oracle": rg.oracle_series(btc, eth, _daily(btc)),
        "sma": rg.sma_series(_daily(btc), btc, n=50),
        "ret": rg.ret_series(btc, hours=168),
    }
    btc_c = [b for b in btc if b[0] < cut]
    eth_c = [b for b in eth if b[0] < cut]
    cutr = {
        "oracle": rg.oracle_series(btc_c, eth_c, _daily(btc_c)),
        "sma": rg.sma_series(_daily(btc_c), btc_c, n=50),
        "ret": rg.ret_series(btc_c, hours=168),
    }
    for k in full:
        common = [t for t in cutr[k].ts if t < cut]
        assert common, k
        assert all(full[k].at(t) == cutr[k].at(t) for t in common), k
