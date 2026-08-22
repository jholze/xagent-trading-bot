/**
 * DEV-only LAB snapshot/OHLCV fallback.
 * Production graphs must never import this module — api.ts dynamic-imports it
 * behind `import.meta.env.DEV` so Vite tree-shakes it out of the prod bundle.
 */
import type { DeskSnapshot, FetchOhlcvArgs, FetchSnapshotArgs, OhlcvBar, OhlcvPack } from './api'
import { emptyOhlcv } from './api'

const LAB = 'LAB/USDT'
const LAB_NEXT_EDGE =
  'TA: dip miss; next edge is DCA when RSI<40 (RelVol cap is a different path).'

/** Fixed epoch so the fixture is deterministic (ms, matching the bot). */
const END_TS_MS = Date.UTC(2026, 7, 22, 16, 0, 0)

function isLab(symbol: string): boolean {
  const s = String(symbol || '')
    .trim()
    .toUpperCase()
    .replace(/-/g, '/')
  return s === LAB || s === 'LAB'
}

function tfMs(tf: string): number {
  if (tf === '15m') return 15 * 60 * 1000
  if (tf === '4h') return 4 * 60 * 60 * 1000
  return 60 * 60 * 1000
}

function makeBars(tf: string): OhlcvBar[] {
  const step = tfMs(tf)
  const n = tf === '15m' ? 160 : tf === '4h' ? 48 : 80
  const seed = tf === '15m' ? 0.161 : tf === '4h' ? 0.119 : 0.148
  const bars: OhlcvBar[] = []
  let close = seed
  for (let i = 0; i < n; i++) {
    const ts = END_TS_MS - (n - 1 - i) * step
    const t = i / Math.max(n - 1, 1)
    const drift = -0.00055 - t * 0.00025
    const wobble = Math.sin(i / 7) * 0.0018 + Math.sin(i / 3.2) * 0.0006
    const bounce = i > n - 8 ? 0.0011 * (i - (n - 8)) : 0
    const open = close
    close = Math.max(0.05, open * (1 + drift) + wobble + bounce)
    const high = Math.max(open, close) * 1.008
    const low = Math.min(open, close) * 0.992
    bars.push({ ts, open, high, low, close })
  }
  return bars
}

function rsiWilder(closes: number[], period = 14): Array<number | null> {
  const out: Array<number | null> = closes.map(() => null)
  if (closes.length <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1]
    if (d >= 0) gain += d
    else loss -= d
  }
  let avgGain = gain / period
  let avgLoss = loss / period
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    const g = d > 0 ? d : 0
    const l = d < 0 ? -d : 0
    avgGain = (avgGain * (period - 1) + g) / period
    avgLoss = (avgLoss * (period - 1) + l) / period
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  }
  return out
}

function bbands(
  closes: number[],
  period = 20,
): { upper: Array<number | null>; middle: Array<number | null>; lower: Array<number | null> } {
  const upper: Array<number | null> = closes.map(() => null)
  const middle: Array<number | null> = closes.map(() => null)
  const lower: Array<number | null> = closes.map(() => null)
  for (let i = period - 1; i < closes.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += closes[j]
    const mean = sum / period
    let varSum = 0
    for (let j = i - period + 1; j <= i; j++) {
      const d = closes[j] - mean
      varSum += d * d
    }
    const std = Math.sqrt(varSum / period)
    middle[i] = mean
    upper[i] = mean + 2 * std
    lower[i] = mean - 2 * std
  }
  return { upper, middle, lower }
}

export function labSnapshot(args: FetchSnapshotArgs): DeskSnapshot {
  return {
    ok: true,
    tenant_id: args.tenant || 'default',
    symbol: isLab(args.symbol) ? LAB : args.symbol || LAB,
    badges: {
      fusion: 'NEUTRAL',
      size_mult: 0.85,
      cash: 'DEPLOY',
      relvol: '8 / 8',
    },
    lots: [
      {
        symbol: LAB,
        timeframe: '1h',
        amount: 1,
        average_entry: 0.132,
        pnl_pct: -40.0,
        dca_rounds: 1,
        dca_max_rounds: 2,
        source: 'grid',
        // Fixture-only geometry so DEV can render the 4-line grammar.
        // Chart.tsx never computes these from a %.
        partial_stop_price: 0.105,
        next_dca_price: 0.078,
      },
    ],
    hud: {
      ta: {
        setup: 'dip miss',
        path: 'DCA 1/2',
        blocker: 'not at lower BB',
        stance: 'MISS',
      },
      social: {
        lead: 'CMC 83×72 → 60',
        chorus: 'Santiment muted (fusion NEUTRAL); Lunar thin',
        ttl: 'quotes fallback',
        stance: 'ARMED',
      },
      memory: {
        bias: 'neutral',
        flag: null,
        lesson: '—',
        stance: 'IDLE',
      },
    },
    conflict: null,
    next_edge: LAB_NEXT_EDGE,
    partial_stop_paused: true,
    dev_fixture: true,
  }
}

export function labOhlcv(args: FetchOhlcvArgs): OhlcvPack {
  if (!isLab(args.symbol)) return emptyOhlcv()
  const bars = makeBars(args.tf || '1h')
  const closes = bars.map((b) => Number(b.close))
  const rsi = rsiWilder(closes, 14)
  const bb = bbands(closes, 20)
  const lastClose = closes[closes.length - 1]
  const lastLower = bb.lower[bb.lower.length - 1]
  const lastRsi = rsi[rsi.length - 1]
  const atLower =
    lastLower == null || lastClose == null ? null : lastClose <= lastLower * 1.02
  return {
    ok: true,
    bars,
    rsi,
    bb_upper: bb.upper,
    bb_middle: bb.middle,
    bb_lower: bb.lower,
    last_rsi: lastRsi,
    at_lower_bb: atLower,
    dev_fixture: true,
  }
}
