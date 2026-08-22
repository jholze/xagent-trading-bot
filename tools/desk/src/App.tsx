import { useState } from 'react'
import { motion, useReducedMotion } from 'motion/react'

type Tenant = 'default' | 'henry'
type Stance = 'ARMED' | 'BLOCK' | 'SIZE↓' | 'IDLE' | 'MISS'

type Lot = {
  symbol: string
  tf: string
  pnlPct: number
  amount: number
  avg: number
  dca: string
  source: string
}

type HudCard = {
  title: string
  stance: Stance
  fields: { k: string; v: string }[]
}

const TENANTS: Tenant[] = ['default', 'henry']

const BADGES = {
  fusion: 'NEUTRAL',
  cash: 'DEPLOY',
  relvol: '8 / 8',
} as const

const NEXT_EDGE =
  'TA: dip miss; next edge is DCA when RSI<40 (RelVol cap is a different path).'

const LOTS: Lot[] = [
  {
    symbol: 'LAB/USDT',
    tf: '1h',
    pnlPct: -40.0,
    amount: 1,
    avg: 0.132,
    dca: '1/2',
    source: 'grid',
  },
  {
    symbol: 'BTC/USDT',
    tf: '4h',
    pnlPct: 2.4,
    amount: 0.01,
    avg: 108420,
    dca: '0/2',
    source: 'sensor',
  },
  {
    symbol: 'ETH/USDT',
    tf: '1h',
    pnlPct: -6.1,
    amount: 0.4,
    avg: 4120,
    dca: '0/2',
    source: 'grid',
  },
]

const HUD: HudCard[] = [
  {
    title: 'TA',
    stance: 'MISS',
    fields: [
      { k: 'Setup', v: 'dip miss · RSI 37.7, not lower BB' },
      { k: 'Path', v: 'DCA round 1/2' },
      { k: 'Blocker', v: 'not at lower BB' },
    ],
  },
  {
    title: 'Social',
    stance: 'ARMED',
    fields: [
      { k: 'Lead', v: 'CMC 83×72 → 60' },
      { k: 'Chorus', v: 'Santiment muted (Fusion NEUTRAL)' },
      { k: 'TTL', v: 'quotes fallback' },
    ],
  },
  {
    title: 'Memory',
    stance: 'IDLE',
    fields: [
      { k: 'Bias', v: 'neutral' },
      { k: 'Flag', v: '—' },
      { k: 'Lesson', v: 'size ×1.00' },
    ],
  },
]

const STANCE_CLASS: Record<Stance, string> = {
  ARMED: 'chip-armed',
  BLOCK: 'chip-block',
  'SIZE↓': 'chip-size',
  IDLE: 'chip-idle',
  MISS: 'chip-miss',
}

function formatPct(n: number): string {
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(1)}%`
}

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

export default function App() {
  const prefersReducedMotion = useReducedMotion() === true
  const [tenant, setTenant] = useState<Tenant>('default')
  const [selected, setSelected] = useState('LAB/USDT')
  const spring = prefersReducedMotion
    ? { duration: 0 }
    : { type: 'spring' as const, stiffness: 380, damping: 32, mass: 0.6 }

  return (
    <div className="desk" data-tenant={tenant}>
      <header className="desk-header">
        <div className="brand">
          <h1>xagent desk</h1>
          <p>
            paper · read-only · {tenant}
          </p>
        </div>
        <div className="header-right">
          <div className="badges" aria-label="Desk badges">
            <Badge
              label="Fusion"
              value={BADGES.fusion}
              tone="accent"
              reduceMotion={prefersReducedMotion}
            />
            <Badge
              label="Cash×mode"
              value={BADGES.cash}
              tone="ok"
              reduceMotion={prefersReducedMotion}
            />
            <Badge
              label="RelVol"
              value={BADGES.relvol}
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
            <span className="pane-meta">{LOTS.length}</span>
          </div>
          <div className="lot-list">
            {LOTS.map((lot) => (
              <button
                key={lot.symbol}
                type="button"
                className={`lot-card${selected === lot.symbol ? ' selected' : ''}`}
                onClick={() => setSelected(lot.symbol)}
              >
                <div className="lot-top">
                  <div className="sym">
                    {lot.symbol}
                    <span className="tf">
                      {lot.tf} · {lot.source} · DCA {lot.dca}
                    </span>
                  </div>
                  <div className={`num ${lot.pnlPct >= 0 ? 'up' : 'dn'}`}>
                    {formatPct(lot.pnlPct)}
                    <span className="sub">avg {lot.avg}</span>
                  </div>
                  <span className="num">
                    {lot.amount}
                    <span className="sub">qty</span>
                  </span>
                </div>
              </button>
            ))}
          </div>
        </section>

        <section className="pane" aria-label="Chart">
          <div className="pane-hd">
            <h2>{selected}</h2>
            <span className="pane-meta">15m / 1h / 4h</span>
          </div>
          {/* Placeholder only — Task 7 binds lightweight-charts. */}
          <div className="chart-pane" aria-label="chart">
            <span className="chart-label">chart</span>
          </div>
        </section>

        <section className="pane" aria-label="HUD">
          <div className="pane-hd">
            <h2>HUD</h2>
            <span className="pane-meta">TA · Social · Memory</span>
          </div>
          <div className="hud-stack">
            {HUD.map((card, i) => (
              <motion.article
                key={card.title}
                className="hud-card"
                initial={prefersReducedMotion ? false : { opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={
                  prefersReducedMotion ? { duration: 0 } : { ...spring, delay: i * 0.05 }
                }
              >
                <div className="hud-card-hd">
                  <h3>{card.title}</h3>
                  <span className={`chip ${STANCE_CLASS[card.stance]}`}>
                    {card.stance}
                  </span>
                </div>
                <div className="fields">
                  {card.fields.map((field) => (
                    <div className="field" key={field.k}>
                      <span className="k">{field.k}</span>
                      <span className="v">{field.v}</span>
                    </div>
                  ))}
                </div>
              </motion.article>
            ))}
          </div>
        </section>
      </main>

      <footer className="desk-footer">
        <strong>Next edge</strong>
        {' · '}
        {NEXT_EDGE}
      </footer>
    </div>
  )
}
