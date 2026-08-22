import { useEffect, useMemo, useRef } from 'react'
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
} from 'lightweight-charts'
import type {
  CandlestickData,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  LineData,
  UTCTimestamp,
  WhitespaceData,
} from 'lightweight-charts'
import type { DeskLot, OhlcvPack, Timeframe } from './api'
import {
  asFiniteNumber,
  formatPx,
  lotAverageEntry,
  lotNextDcaPrice,
  lotPartialStopPrice,
} from './api'

const TFS: Timeframe[] = ['15m', '1h', '4h']

const AVG_COLOR = '#5B8DEF'
const PS_COLOR = '#E8A838'
const PS_COLOR_DIM = 'rgba(232, 168, 56, 0.42)'
const DCA_COLOR = '#7C6CFF'
const LIVE_COLOR = '#C8C8C0'
const RSI_COLOR = '#93b0ff'
const BB_COLOR = 'rgba(138, 138, 128, 0.85)'
const BB_MID_COLOR = 'rgba(147, 176, 255, 0.45)'
const BAND_TOP = 'rgba(124, 108, 255, 0.22)'
const BAND_BOTTOM = 'rgba(124, 108, 255, 0.04)'

type LineKey = 'avg' | 'ps' | 'dca' | 'live'

type ChartHandle = {
  chart: IChartApi
  candle: ISeriesApi<'Candlestick'>
  bbUpper: ISeriesApi<'Line'>
  bbMiddle: ISeriesApi<'Line'>
  bbLower: ISeriesApi<'Line'>
  rsi: ISeriesApi<'Line'>
  band: ISeriesApi<'Area'>
  lines: Partial<Record<LineKey, IPriceLine>>
}

type ChartProps = {
  symbol: string
  tf: Timeframe
  onTfChange: (tf: Timeframe) => void
  ohlcv: OhlcvPack | null
  lot: DeskLot | null
  partialStopPaused: boolean
  dcaRoundsRemain: boolean
}

function toUnixSeconds(ts: number): UTCTimestamp | null {
  if (!Number.isFinite(ts)) return null
  const sec = ts > 1e12 ? ts / 1000 : ts
  if (!Number.isFinite(sec) || sec <= 0) return null
  return Math.floor(sec) as UTCTimestamp
}

function toCandle(
  bar: { ts?: number | null; open?: number | null; high?: number | null; low?: number | null; close?: number | null },
): CandlestickData<UTCTimestamp> | null {
  const ts = asFiniteNumber(bar.ts)
  const open = asFiniteNumber(bar.open)
  const high = asFiniteNumber(bar.high)
  const low = asFiniteNumber(bar.low)
  const close = asFiniteNumber(bar.close)
  if (ts == null || open == null || high == null || low == null || close == null) {
    return null
  }
  const time = toUnixSeconds(ts)
  if (time == null) return null
  return { time, open, high, low, close }
}

function linePoint(
  time: UTCTimestamp,
  value: number | null | undefined,
): LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp> {
  if (value == null || !Number.isFinite(value)) return { time }
  return { time, value }
}

function lastClose(candles: CandlestickData<UTCTimestamp>[]): number | null {
  for (let i = candles.length - 1; i >= 0; i--) {
    const close = candles[i]?.close
    if (typeof close === 'number' && Number.isFinite(close)) return close
  }
  return null
}

function alignCandles(ohlcv: OhlcvPack | null): {
  candles: CandlestickData<UTCTimestamp>[]
  indices: number[]
} {
  const bars = ohlcv?.bars ?? []
  const rows: { candle: CandlestickData<UTCTimestamp>; i: number }[] = []
  const seen = new Map<number, number>()
  for (let i = 0; i < bars.length; i++) {
    const candle = toCandle(bars[i] ?? {})
    if (!candle) continue
    const t = candle.time as unknown as number
    const prev = seen.get(t)
    if (prev != null) {
      rows[prev] = { candle, i }
    } else {
      seen.set(t, rows.length)
      rows.push({ candle, i })
    }
  }
  rows.sort((a, b) => (a.candle.time as number) - (b.candle.time as number))
  return {
    candles: rows.map((r) => r.candle),
    indices: rows.map((r) => r.i),
  }
}

function syncPriceLine(
  series: ISeriesApi<'Candlestick'>,
  store: ChartHandle['lines'],
  key: LineKey,
  price: number | null,
  options: { color: string; lineStyle: LineStyle; title: string; lineWidth?: 1 | 2 },
) {
  const existing = store[key]
  if (price == null) {
    if (existing) {
      series.removePriceLine(existing)
      delete store[key]
    }
    return
  }
  const next = {
    price,
    color: options.color,
    lineStyle: options.lineStyle,
    lineWidth: options.lineWidth ?? 1,
    axisLabelVisible: true,
    title: options.title,
  }
  if (existing) {
    existing.applyOptions(next)
    return
  }
  store[key] = series.createPriceLine(next)
}

export function Chart({
  symbol,
  tf,
  onTfChange,
  ohlcv,
  lot,
  partialStopPaused,
  dcaRoundsRemain,
}: ChartProps) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const handleRef = useRef<ChartHandle | null>(null)

  const { candles, indices } = useMemo(() => alignCandles(ohlcv), [ohlcv])
  const empty = candles.length === 0
  const live = lastClose(candles)
  const avg = lotAverageEntry(lot)
  const ps = lotPartialStopPrice(lot)
  const dcaOn = dcaRoundsRemain
  const dca = dcaOn ? lotNextDcaPrice(lot) : null

  useEffect(() => {
    const el = hostRef.current
    if (!el) return

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: '#141412' },
        textColor: '#8a8a80',
        fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.045)' },
      },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.09)',
        scaleMargins: { top: 0.08, bottom: 0.12 },
      },
      timeScale: {
        borderColor: 'rgba(255,255,255,0.09)',
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: 'rgba(147,176,255,0.35)', width: 1, style: LineStyle.Dotted },
        horzLine: { color: 'rgba(147,176,255,0.35)', width: 1, style: LineStyle.Dotted },
      },
    })

    const candle = chart.addSeries(
      CandlestickSeries,
      {
        upColor: '#6ee7a8',
        downColor: '#ff7a8a',
        borderVisible: false,
        wickUpColor: '#6ee7a8',
        wickDownColor: '#ff7a8a',
      },
      0,
    )
    const bbOpts = {
      lineWidth: 1 as const,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    }
    const bbUpper = chart.addSeries(LineSeries, { ...bbOpts, color: BB_COLOR }, 0)
    const bbMiddle = chart.addSeries(
      LineSeries,
      { ...bbOpts, color: BB_MID_COLOR, lineStyle: LineStyle.Dashed },
      0,
    )
    const bbLower = chart.addSeries(LineSeries, { ...bbOpts, color: BB_COLOR }, 0)

    chart.addPane(true)
    const panes = chart.panes()
    panes[0]?.setStretchFactor(2.8)
    panes[1]?.setStretchFactor(1)

    const rsi = chart.addSeries(
      LineSeries,
      {
        color: RSI_COLOR,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        autoscaleInfoProvider: () => ({
          priceRange: { minValue: 0, maxValue: 100 },
        }),
      },
      1,
    )
    const band = chart.addSeries(
      AreaSeries,
      {
        topColor: BAND_TOP,
        bottomColor: BAND_BOTTOM,
        lineColor: 'rgba(124,108,255,0)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => ({
          priceRange: { minValue: 0, maxValue: 100 },
        }),
      },
      1,
    )
    rsi.createPriceLine({
      price: 30,
      color: 'rgba(255,122,138,0.45)',
      lineStyle: LineStyle.Dashed,
      lineWidth: 1,
      axisLabelVisible: true,
      title: '30',
    })
    rsi.createPriceLine({
      price: 70,
      color: 'rgba(110,231,168,0.4)',
      lineStyle: LineStyle.Dashed,
      lineWidth: 1,
      axisLabelVisible: true,
      title: '70',
    })
    chart.priceScale('right', 1).applyOptions({
      borderColor: 'rgba(255,255,255,0.09)',
      scaleMargins: { top: 0.12, bottom: 0.08 },
    })

    handleRef.current = {
      chart,
      candle,
      bbUpper,
      bbMiddle,
      bbLower,
      rsi,
      band,
      lines: {},
    }

    return () => {
      handleRef.current = null
      chart.remove()
    }
  }, [])

  useEffect(() => {
    const handle = handleRef.current
    if (!handle) return

    const times = candles.map((c) => c.time)
    handle.candle.setData(candles)

    const zip = (values: Array<number | null> | undefined) =>
      times.map((time, slot) => {
        const src = indices[slot]
        return linePoint(time, src == null ? null : values?.[src])
      })

    handle.bbUpper.setData(zip(ohlcv?.bb_upper))
    handle.bbMiddle.setData(zip(ohlcv?.bb_middle))
    handle.bbLower.setData(zip(ohlcv?.bb_lower))
    handle.rsi.setData(zip(ohlcv?.rsi))
    handle.band.setData(
      dcaOn && times.length
        ? times.map((time) => ({ time, value: 40 }))
        : times.map((time) => ({ time })),
    )

    syncPriceLine(handle.candle, handle.lines, 'avg', avg, {
      color: AVG_COLOR,
      lineStyle: LineStyle.Solid,
      title: 'avg',
      lineWidth: 2,
    })
    syncPriceLine(handle.candle, handle.lines, 'ps', ps, {
      color: partialStopPaused ? PS_COLOR_DIM : PS_COLOR,
      lineStyle: partialStopPaused ? LineStyle.Dashed : LineStyle.Solid,
      title: 'PS',
      lineWidth: 1,
    })
    syncPriceLine(handle.candle, handle.lines, 'dca', dca, {
      color: DCA_COLOR,
      lineStyle: LineStyle.Solid,
      title: 'DCA',
      lineWidth: 1,
    })
    syncPriceLine(handle.candle, handle.lines, 'live', live, {
      color: LIVE_COLOR,
      lineStyle: LineStyle.Solid,
      title: 'live',
      lineWidth: 1,
    })

    if (candles.length) handle.chart.timeScale().fitContent()
  }, [ohlcv, candles, indices, avg, ps, dca, dcaOn, live, partialStopPaused])

  return (
    <>
      <div className="pane-hd">
        <h2>{symbol}</h2>
        <div className="tf-switch" role="tablist" aria-label="Timeframe">
          {TFS.map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tf === id}
              onClick={() => onTfChange(id)}
            >
              {id}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-legend" aria-label="Strategy lines">
        <span className="legend-item">
          <i className="swatch" style={{ background: AVG_COLOR }} />
          avg {formatPx(avg)}
        </span>
        {ps != null || partialStopPaused ? (
          <span className={`legend-item${partialStopPaused ? ' dim' : ''}${ps == null ? ' off' : ''}`}>
            <i
              className={`swatch${partialStopPaused ? ' dashed' : ''}`}
              style={{ background: partialStopPaused ? PS_COLOR_DIM : PS_COLOR }}
            />
            PS{partialStopPaused ? ' ⏸' : ''} {ps == null ? '—' : formatPx(ps)}
          </span>
        ) : null}
        {dcaOn ? (
          <span className={`legend-item${dca == null ? ' off' : ''}`}>
            <i className="swatch" style={{ background: DCA_COLOR }} />
            DCA {dca == null ? '—' : formatPx(dca)}
          </span>
        ) : null}
        <span className={`legend-item${live == null ? ' off' : ''}`}>
          <i className="swatch" style={{ background: LIVE_COLOR }} />
          live {formatPx(live)}
        </span>
      </div>
      <div className="chart-stage">
        <div ref={hostRef} className="chart-canvas" aria-label="chart" />
        {empty ? (
          <div className="chart-empty" role="status">
            no snapshot
          </div>
        ) : null}
      </div>
    </>
  )
}
