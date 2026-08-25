# Desk v0 — React + Interior (Jesse 3-pane)

**Status:** Spec lock (operator 2026-08-22)  
**Nicht:** Telegram. Nicht Jesse als Engine. Nicht RelVol-Cap ändern. Nicht Radar/Cortex iframe.

## Ziel

Ein **read-only** Paper-Desk, der auf einem Coin **zeigt was der Bot denkt und was als Nächstes feuern würde** — Chart + TA + Social + Memory in einem Blick.

**8-Sekunden-Frage:** *Warum ist LAB HOLD — und was wäre der nächste Add/Stop?*

## Locked

| | |
|---|---|
| App | Neu: `tools/desk/` Vite + React + Tailwind |
| Craft | Interior.dev copy-paste + `motion` (eine Runtime-Dep) |
| Layout | Jesse-klassisch: Lots \| Chart \| HUD |
| Tenants | Toggle default \| henry (ctexp out) |
| Mode | Paper, keine Orders vom UI |
| Deploy | Bot `/desk/` (kein split `xagent-desk` Service), Token wie Exit-WS |
| Kill | Flag `desk.enabled=false` |

## Chart (Mitte)

- Kerzen 15m / 1h / 4h, BB(20) auf dem Preis, RSI-Pane darunter.
- **Max 4 Strategy-Linien:** Avg-Entry (solid steel), Partial-Stop (amber solid wenn armed, **dashed + dim + ⏸** wenn pausiert), Next-DCA (violet, nur wenn Runden übrig), Live-Preis.
- Marker: letzte ≤3 Buy/DCA-Fills (`B`, `D1`).
- RSI: Guides 30/70; Soft-Band 0–40 wenn DCA-Runde offen.
- **Nicht zeichnen:** voller SL, Trail, Peak, Volume-OB, Ghost-Future, Sell-Pfeile.

## HUD (Rechts) — 3 Karten × ≤4 Felder

Stance-Chips: `ARMED` | `BLOCK` | `SIZE↓` | `IDLE` | `MISS`

| Karte | Felder |
|---|---|
| TA | Setup, Path (sensor/DCA/RelVol/grid), erster Blocker, Stance |
| Social | Lead `CMC 83×72 → 60`, Chorus (San muted bei Fusion NEUTRAL), TTL, Stance |
| Memory | Bias, worst Fact-Flag, Lesson oder size×, Stance |

Konflikt-Ribbon nur wenn Social ARMED und Memory BLOCK. Next-edge nennt **eine** Quelle.

**Next-edge Grammatik:** `{SOURCE}: {condition} → {action}.`  
LAB-Beispiel: `TA: dip miss; next edge is DCA when RSI<40 (RelVol cap is a different path).`

## Shell

- Oben: Fusion / Cash×mode / RelVol `n / 8` — **reservierte Badge-Breite**, `tabular-nums`, Interior springs interruptible, `prefers-reduced-motion` setzt Endzustand ohne Trip.
- Unten: eine Next-edge-Zeile (kein Tape-Roman).
- RelVol 8/8 ist **Anzeige**, kein Slider.

## Daten (Bot intern)

- `GET /internal/desk/snapshot?tenant=&symbol=` Lots, fusion, cash, relvol counts, last DE rationale, social (cmc/lc/chorus), memory profile+facts.
- `GET /internal/desk/ohlcv?symbol=&tf=` Kerzen + BB/RSI series.
- Auth: `EXIT_WS_INTERNAL_TOKEN` (oder `DESK_TOKEN`).
- Fail-open: leerer Chart + `no snapshot`, nie Fake-Fills.

## Nicht v0

Order-Buttons, Cap-Editoren, Radar/Cortex iframe, Lesson-Editor, RAG-Chat, Live-Switch, ctexp.

## Test / Abnahme

Fixture LAB: −40 %, RSI 37.7, DCA 1/2, PS paused, CMC quotes armed, Fusion NEUTRAL, RelVol 8/8.  
Operator sieht in 3s: underwater, Stop absichtlich pausiert, eine DCA-Runde offen, Social würde kaufen, RelVol-Pfad ist ein anderes Tor.

## Deploy

v0 is served from the bot at `/desk/` (not a split `xagent-desk` Railway service).
Kill: `desk.enabled=false` in `config.json`.
Open: `https://xagent-test-test.up.railway.app/desk/?token=`
Auth: `EXIT_WS_INTERNAL_TOKEN` or `DESK_TOKEN`, header `X-Exit-Ws-Token`.
Paper, read-only, tenants default+henry; RelVol is display-only.
APIs return 503 if the token is unset.
Dist is baked in Docker (`node:22-slim` stage → `/app/tools/desk/dist`).
Do not commit `dist/` or `node_modules`.

## Agenten-Konsens (2026-08-22)

1. Chart-Grammatik > Badge-Spam (Jesse).
2. Interior: Breite reservieren, Spring retargeten, ohne Motion trotzdem Info.
3. Memory/Social actionable (Stance + Konflikt), kein Cortex-Dump.
4. Ops: nicht HOLD-Telegram spiegeln; Cap-Zustand zeigen, Caps nicht in dieser UI ändern.
