import { motion } from 'motion/react'
import type { DeskHud } from './api'
import { isNonEmptyString } from './api'

const STANCE_CLASS: Record<string, string> = {
  ARMED: 'chip-armed',
  BLOCK: 'chip-block',
  'SIZE↓': 'chip-size',
  IDLE: 'chip-idle',
  MISS: 'chip-miss',
}

function dash(value: unknown): string {
  if (value == null) return '—'
  const text = String(value).trim()
  return text.length ? text : '—'
}

function StanceChip({ stance }: { stance: string }) {
  const label = stance || 'IDLE'
  const cls = STANCE_CLASS[label] || 'chip-idle'
  return <span className={`chip ${cls}`}>{label}</span>
}

type HudProps = {
  hud?: DeskHud | null
  conflict?: string | null
  reduceMotion: boolean
}

export function Hud({ hud, conflict, reduceMotion }: HudProps) {
  const spring = reduceMotion
    ? { duration: 0 }
    : { type: 'spring' as const, stiffness: 380, damping: 32, mass: 0.6 }

  const cards = [
    {
      title: 'TA',
      stance: dash(hud?.ta?.stance),
      fields: [
        { k: 'Setup', v: dash(hud?.ta?.setup) },
        { k: 'Path', v: dash(hud?.ta?.path) },
        { k: 'Blocker', v: dash(hud?.ta?.blocker) },
      ],
    },
    {
      title: 'Social',
      stance: dash(hud?.social?.stance),
      fields: [
        { k: 'Lead', v: dash(hud?.social?.lead) },
        { k: 'Chorus', v: dash(hud?.social?.chorus) },
        { k: 'TTL', v: dash(hud?.social?.ttl) },
      ],
    },
    {
      title: 'Memory',
      stance: dash(hud?.memory?.stance),
      fields: [
        { k: 'Bias', v: dash(hud?.memory?.bias) },
        { k: 'Flag', v: dash(hud?.memory?.flag) },
        { k: 'Lesson', v: dash(hud?.memory?.lesson) },
      ],
    },
  ]

  const showConflict = isNonEmptyString(conflict)

  return (
    <div className="hud-stack">
      {showConflict ? (
        <div className="conflict" role="status">
          {conflict}
        </div>
      ) : null}
      {cards.map((card, i) => (
        <motion.article
          key={card.title}
          className="hud-card"
          initial={reduceMotion ? false : { opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={reduceMotion ? { duration: 0 } : { ...spring, delay: i * 0.05 }}
        >
          <div className="hud-card-hd">
            <h3>{card.title}</h3>
            <StanceChip stance={card.stance} />
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
  )
}
