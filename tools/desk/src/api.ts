export type Tenant = 'default' | 'henry'
export type Timeframe = '15m' | '1h' | '4h'
export type Stance = 'ARMED' | 'BLOCK' | 'SIZE↓' | 'IDLE' | 'MISS'

export type DeskLot = {
  symbol: string
  timeframe?: string
  tf?: string
  amount?: number
  average_entry?: number
  entry_price?: number
  side?: 'long' | 'short' | string
  leverage?: number | null
  pnl_pct?: number
  dca_rounds?: number
  dca_max_rounds?: number
  source?: string
  entry_source?: string
  /** Draw only when the snapshot provides a price — never invent a % stop. */
  partial_stop_price?: number | null
  partial_stop?: number | null
  /** Violet line only when a price is present and DCA rounds remain. */
  next_dca_price?: number | null
  next_dca?: number | null
}

export type HudTa = {
  setup?: string
  path?: string
  blocker?: string
  stance?: string
}

export type HudSocial = {
  lead?: string
  chorus?: string
  ttl?: string
  stance?: string
}

export type HudMemory = {
  bias?: string
  flag?: string | null
  lesson?: string
  stance?: string
}

export type DeskHud = {
  ta?: HudTa
  social?: HudSocial
  memory?: HudMemory
}

export type DeskBadges = {
  fusion?: string | number
  size_mult?: string | number
  cash?: string | number
  relvol?: string | number
}

export type DeskSnapshot = {
  ok: boolean
  error?: string
  tenant_id?: string
  symbol?: string
  badges?: DeskBadges
  lots?: DeskLot[]
  hud?: DeskHud
  conflict?: string | null
  next_edge?: string
  partial_stop_paused?: boolean
  /** DEV-only. Production never sets this. */
  dev_fixture?: boolean
}

export type OhlcvBar = {
  ts?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  close?: number | null
}

export type OhlcvPack = {
  ok: boolean
  error?: string
  bars?: OhlcvBar[]
  rsi?: Array<number | null>
  bb_upper?: Array<number | null>
  bb_middle?: Array<number | null>
  bb_lower?: Array<number | null>
  last_rsi?: number | null
  at_lower_bb?: boolean | null
  /** DEV-only. Production never sets this. */
  dev_fixture?: boolean
}

export type FetchSnapshotArgs = {
  tenant: string
  symbol: string
  token?: string
  tf?: string
  signal?: AbortSignal
}

export type FetchOhlcvArgs = {
  symbol: string
  tf: string
  token?: string
  signal?: AbortSignal
}

const SNAPSHOT_PATH = '/internal/desk/snapshot'
const OHLCV_PATH = '/internal/desk/ohlcv'

export function emptySnapshot(tenant: string, symbol: string): DeskSnapshot {
  return {
    ok: false,
    error: 'no snapshot',
    tenant_id: tenant,
    symbol,
    badges: { fusion: '—', cash: '—', relvol: '—' },
    lots: [],
    hud: {
      ta: { setup: '—', path: '—', blocker: '—', stance: 'IDLE' },
      social: { lead: '—', chorus: '—', ttl: '—', stance: 'IDLE' },
      memory: { bias: '—', flag: null, lesson: '—', stance: 'IDLE' },
    },
    conflict: null,
    next_edge: '',
    partial_stop_paused: false,
  }
}

export function emptyOhlcv(): OhlcvPack {
  return { ok: false, error: 'ohlcv_unavailable', bars: [] }
}

export function deskToken(explicit?: string): string {
  if (explicit && explicit.trim()) return explicit.trim()
  if (typeof window !== 'undefined') {
    const fromQuery = new URLSearchParams(window.location.search).get('token')
    if (fromQuery && fromQuery.trim()) return fromQuery.trim()
  }
  const fromEnv = import.meta.env.VITE_DESK_TOKEN
  return fromEnv && String(fromEnv).trim() ? String(fromEnv).trim() : ''
}

function deskHeaders(token?: string): HeadersInit {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const value = deskToken(token)
  if (value) headers['X-Exit-Ws-Token'] = value
  return headers
}

function withTimeout(signal: AbortSignal | undefined, ms: number): AbortSignal {
  const timeout = AbortSignal.timeout(ms)
  if (!signal) return timeout
  const any = (AbortSignal as unknown as { any?: (s: AbortSignal[]) => AbortSignal }).any
  if (typeof any === 'function') return any([signal, timeout])
  return timeout
}

function isAbort(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === 'AbortError') ||
    (typeof err === 'object' &&
      err !== null &&
      (err as { name?: string }).name === 'AbortError')
  )
}

async function readJson<T>(res: Response): Promise<T | null> {
  try {
    return (await res.json()) as T
  } catch {
    return null
  }
}

async function fallbackSnapshot(args: FetchSnapshotArgs): Promise<DeskSnapshot> {
  if (import.meta.env.DEV) {
    const { labSnapshot } = await import('./labFixture')
    return labSnapshot(args)
  }
  return emptySnapshot(args.tenant, args.symbol)
}

async function fallbackOhlcv(args: FetchOhlcvArgs): Promise<OhlcvPack> {
  if (import.meta.env.DEV) {
    const { labOhlcv } = await import('./labFixture')
    return labOhlcv(args)
  }
  return emptyOhlcv()
}

export async function fetchSnapshot(args: FetchSnapshotArgs): Promise<DeskSnapshot> {
  const params = new URLSearchParams()
  params.set('tenant', args.tenant)
  params.set('symbol', args.symbol)
  if (args.tf) params.set('tf', args.tf)
  const url = `${SNAPSHOT_PATH}?${params.toString()}`
  try {
    const res = await fetch(url, {
      headers: deskHeaders(args.token),
      signal: withTimeout(args.signal, 4000),
    })
    if (!res.ok) return await fallbackSnapshot(args)
    const body = await readJson<DeskSnapshot>(res)
    if (body && body.ok === true) return { ...body, dev_fixture: false }
    return await fallbackSnapshot(args)
  } catch (err) {
    if (isAbort(err) && args.signal?.aborted) throw err
    return await fallbackSnapshot(args)
  }
}

export async function fetchOhlcv(args: FetchOhlcvArgs): Promise<OhlcvPack> {
  const params = new URLSearchParams()
  params.set('symbol', args.symbol)
  params.set('tf', args.tf)
  const url = `${OHLCV_PATH}?${params.toString()}`
  try {
    const res = await fetch(url, {
      headers: deskHeaders(args.token),
      signal: withTimeout(args.signal, 8000),
    })
    if (!res.ok) return await fallbackOhlcv(args)
    const body = await readJson<OhlcvPack>(res)
    if (body && body.ok === true && (body.bars?.length ?? 0) > 0) {
      return { ...body, dev_fixture: false }
    }
    return await fallbackOhlcv(args)
  } catch (err) {
    if (isAbort(err) && args.signal?.aborted) throw err
    return await fallbackOhlcv(args)
  }
}

export function asFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return null
}

export function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

export function lotTimeframe(lot: DeskLot | null | undefined): string {
  if (!lot) return '—'
  return lot.timeframe || lot.tf || '—'
}

export function lotSource(lot: DeskLot | null | undefined): string {
  if (!lot) return '—'
  return lot.source || lot.entry_source || '—'
}

export function lotAverageEntry(lot: DeskLot | null | undefined): number | null {
  if (!lot) return null
  return asFiniteNumber(lot.average_entry) ?? asFiniteNumber(lot.entry_price)
}

export function lotPartialStopPrice(lot: DeskLot | null | undefined): number | null {
  if (!lot) return null
  return asFiniteNumber(lot.partial_stop_price) ?? asFiniteNumber(lot.partial_stop)
}

export function lotNextDcaPrice(lot: DeskLot | null | undefined): number | null {
  if (!lot) return null
  return asFiniteNumber(lot.next_dca_price) ?? asFiniteNumber(lot.next_dca)
}

export function dcaRemaining(lot: DeskLot | null | undefined): boolean {
  if (!lot || lot.side === 'short') return false
  const used = asFiniteNumber(lot.dca_rounds)
  const max = asFiniteNumber(lot.dca_max_rounds)
  if (used == null || max == null) return false
  return used < max
}

/** Live lots may omit dca_max_rounds; HUD path is `DCA used/max` from the API. */
export function dcaRemainingFromHud(hud?: DeskHud | null): boolean | null {
  const path = hud?.ta?.path
  if (!isNonEmptyString(path)) return null
  const match = /^DCA\s+(\d+)\s*\/\s*(\d+)/i.exec(path.trim())
  if (!match) return null
  return Number(match[1]) < Number(match[2])
}

export function dcaRoundsRemain(
  lot: DeskLot | null | undefined,
  hud?: DeskHud | null,
): boolean {
  if (!lot || lot.side === 'short') return false
  if (dcaRemaining(lot)) return true
  const used = asFiniteNumber(lot?.dca_rounds)
  const max = asFiniteNumber(lot.dca_max_rounds)
  if (used != null && max != null) return false
  return dcaRemainingFromHud(hud) === true
}

export function formatDca(lot: DeskLot | null | undefined): string {
  if (!lot) return '—'
  const used = asFiniteNumber(lot.dca_rounds)
  const max = asFiniteNumber(lot.dca_max_rounds)
  if (used == null && max == null) return '—'
  if (max == null) return used == null ? '—' : String(used)
  return `${used ?? 0}/${max}`
}

export function formatPx(value: unknown): string {
  const n = asFiniteNumber(value)
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1000) return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (abs >= 1) return n.toFixed(2)
  if (abs >= 0.01) return n.toFixed(4)
  return n.toPrecision(3)
}

export function formatPct(value: unknown): string | null {
  const n = asFiniteNumber(value)
  if (n == null) return null
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(1)}%`
}

export function badgeText(value: unknown): string {
  if (value == null || value === '') return '—'
  return String(value)
}
