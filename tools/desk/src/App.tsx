import { useEffect, useMemo, useState } from 'react'
import { motion, useReducedMotion } from 'motion/react'
import { Chart } from './Chart'
import { Hud } from './Hud'
import type { DeskLot, DeskSnapshot, OhlcvPack, Tenant, Timeframe } from './api'
import {
  asFiniteNumber,
  badgeText,
  dcaRoundsRemain,
  emptyOhlcv,
  emptySnapshot,
  fetchOhlcv,
  fetchSnapshot,
  formatDca,
  formatPct,
  formatPx,
  isNonEmptyString,
  lotSource,
  lotTimeframe,
} from './api'

const TENANTS: Tenant[] = ['default', 'henry']

function Badge({
  label,
  value,
  tone,
  reduceMotion,
}: {
  label: string
  value: string
  tone: 'ok' | 'accent' | 'warn'
  reduceMotion: boolean
}) {
  return (
    <motion.div
      className="badge"
      initial={reduceMotion ? false : { opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={
        reduceMotion
          ? { duration: 0 }
          : { type: 'spring', stiffness: 420, damping: 30, mass: 0.55 }
      }
    >
      <span className={`dot dot-${tone}`} />
      <span className="badge-k">{label}</span>
      <strong className="badge-v">{value}</strong>
    </motion.div>
  )
}

function lotKey(lot: DeskLot, index: number): string {
  return `${lot.symbol}:${lotTimeframe(lot)}:${index}`
}

export default function App() {
  const prefersReducedMotion = useReducedMotion() === true
  const [tenant, setTenant] = useState<Tenant>('default')
  const [symbol, setSymbol] = useState('LAB/USDT')
  const [tf, setTf] = useState<Timeframe>('1h')
  const [snap, setSnap] = useState<DeskSnapshot>(() => emptySnapshot('default', 'LAB/USDT'))
  const [ohlcv, setOhlcv] = useState<OhlcvPack>(() => emptyOhlcv())
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)

  useEffect(() => {
    setOhlcv(emptyOhlcv())
  }, [tenant, symbol, tf])

  useEffect(() => {
    let cancelled = false
    let ac = new AbortController()

    const load = async () => {
      ac.abort()
      ac = new AbortController()
      const signal = ac.signal
      try {
        const next = await fetchSnapshot({
          tenant,
          symbol,
          tf,
          signal,
        })
        if (cancelled || signal.aborted) return
        setSnap(next)
        const pack = await fetchOhlcv({
          symbol: next.symbol || symbol,
          tf,
          signal,
        })
        if (cancelled || signal.aborted) return
        setOhlcv(pack)
        setUpdatedAt(Date.now())
      } catch {
        if (cancelled || signal.aborted) return
        setSnap(emptySnapshot(tenant, symbol))
        setOhlcv(emptyOhlcv())
      }
    }

    void load()
    const tick = window.setInterval(() => {
      void load()
    }, 10_000)
    return () => {
      cancelled = true
      ac.abort()
      window.clearInterval(tick)
    }
  }, [tenant, symbol, tf])

  const lots = snap.lots ?? []

  useEffect(() => {
    if (!lots.length) return
    if (lots.some((lot) => lot.symbol === symbol)) return
    const first = lots[0]?.symbol
    if (first) setSymbol(first)
  }, [lots, symbol])

  const selected = useMemo(
    () => lots.find((lot) => lot.symbol === symbol) ?? null,
    [lots, symbol],
  )

  const fusion = badgeText(snap.badges?.fusion)
  const cash = badgeText(snap.badges?.cash)
  const relvol = badgeText(snap.badges?.relvol)
  const nextEdge = isNonEmptyString(snap.next_edge) ? snap.next_edge : 'no snapshot'

  return (
    <div className="desk" data-tenant={tenant}>
      <header className="desk-header">
        <div className="brand">
          <h1>xagent desk</h1>
          <p>
            paper · read-only · {tenant}
            {snap.dev_fixture || ohlcv.dev_fixture ? ' · dev fixture' : ''}
            {updatedAt
              ? ` · ${new Date(updatedAt).toLocaleTimeString()}`
              : ''}
          </p>
        </div>
        <div className="header-right">
          <div className="badges" aria-label="Desk badges">
            <Badge
              label="Fusion"
              value={fusion}
              tone="accent"
              reduceMotion={prefersReducedMotion}
            />
            <Badge
              label="Cash×mode"
              value={cash}
              tone="ok"
              reduceMotion={prefersReducedMotion}
            />
            <Badge
              label="RelVol"
              value={relvol}
              tone="warn"
              reduceMotion={prefersReducedMotion}
            />
          </div>
          <div className="tenant-toggle" role="tablist" aria-label="Tenant">
            {TENANTS.map((id) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tenant === id}
                onClick={() => setTenant(id)}
              >
                {id}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main className="desk-main">
        <section className="pane" aria-label="Lots">
          <div className="pane-hd">
            <h2>Lots</h2>
            <span className="pane-meta">{lots.length}</span>
          </div>
          <div className="lot-list">
            {lots.length === 0 ? (
              <div className="lot-empty">no snapshot</div>
            ) : (
              lots.map((lot, index) => {
                const pnl = formatPct(lot.pnl_pct)
                const avg = asFiniteNumber(lot.average_entry) ?? asFiniteNumber(lot.entry_price)
                const dn = pnl != null && (asFiniteNumber(lot.pnl_pct) ?? 0) < 0
                const up = pnl != null && (asFiniteNumber(lot.pnl_pct) ?? 0) > 0
                return (
                  <button
                    key={lotKey(lot, index)}
                    type="button"
                    className={`lot-card${selected?.symbol === lot.symbol ? ' selected' : ''}`}
                    onClick={() => setSymbol(lot.symbol)}
                  >
                    <div className="lot-top">
                      <div className="sym">
                        {lot.symbol}
                        {lot.side === 'short' ? ' S' : ''}
                        <span className="tf">
                          {lotTimeframe(lot)} · {lotSource(lot)}
                          {lot.side === 'short'
                            ? ` · ${lot.leverage ? `${lot.leverage}×` : 'short'}`
                            : ` · DCA ${formatDca(lot)}`}
                        </span>
                      </div>
                      <div className={`num${up ? ' up' : ''}${dn ? ' dn' : ''}`}>
                        {pnl ?? '—'}
                        <span className="sub">avg {formatPx(avg)}</span>
                      </div>
                      <span className="num">
                        {asFiniteNumber(lot.amount) ?? '—'}
                        <span className="sub">qty</span>
                      </span>
                    </div>
                  </button>
                )
              })
            )}
          </div>
        </section>

        <section className="pane pane-chart" aria-label="Chart">
          <Chart
            key={`${snap.symbol || symbol}:${tf}`}
            symbol={snap.symbol || symbol}
            tf={tf}
            onTfChange={setTf}
            ohlcv={ohlcv}
            lot={selected}
            partialStopPaused={Boolean(snap.partial_stop_paused)}
            dcaRoundsRemain={dcaRoundsRemain(selected, snap.hud)}
          />
        </section>

        <section className="pane" aria-label="HUD">
          <div className="pane-hd">
            <h2>HUD</h2>
            <span className="pane-meta">TA · Social · Memory</span>
          </div>
          <Hud
            hud={snap.hud}
            conflict={snap.conflict}
            reduceMotion={prefersReducedMotion}
          />
        </section>
      </main>

      <footer className="desk-footer">
        <strong>Next edge</strong>
        {' · '}
        {nextEdge}
      </footer>
    </div>
  )
}
