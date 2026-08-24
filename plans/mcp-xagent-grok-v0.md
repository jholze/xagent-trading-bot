# xagent MCP v0 — Grok Trading Team

**Status:** Spec lock (operator 2026-08-23)  
**Branch:** `feat/mcp-xagent-grok`  
**Nicht:** Production/`xagent-bot`. Nicht Exchange direkt. Nicht RelVol-Cap ändern. Nicht `correlated_tier.enabled=true`. Nicht Jesse als Engine.

## Ziel

Ein **MCP-Server**, den ein Grok-Trading-Team an den Paper-Bot hängt. Team-Mitglieder steuern mit — inkl. eigenständiger Buys/Sells — über denselben Risk/Ledger-Pfad wie Telegram.

**Owner** (Jens) steuert **alle** Tenants (default, henry, ctexp). **Andere** nur den eigenen.

## Locked

| | |
|---|---|
| Deploy | Railway env **test**, eigenes Service **`xagent-mcp`** (nicht im `xagent-test`-Prozess) |
| Performance | Reads treffen den Bot **nicht** (Mongo/Redis + `services.desk.snapshot`). Writes = kurzer POST an `xagent-test` |
| Authz | Eine Funktion `authorize(actor, action, tenant_id)` vor jedem Tool |
| Writes | `TradingService` + `RiskManager` unter `tenant_context` |
| Paper | `DEMO_MODE=1` / ledger paper. Kein Live. |
| Kill | `mcp.enabled=false` und/oder Service aus; `mcp.allow_writes=false` → nur Reads |
| Tenants | Owner `*`. Operator genau einer. ctexp ist für Owner sichtbar (Telegram `[ctexp]` ist Owner-Inbox, kein Ledger-Mix). |

## Berechtigungsschicht

**Principal** = Token → `Actor {actor_id, role, tenants[], caps[], status}`.  
Token nur **SHA-256-Hash** speichern, Compare via `hmac.compare_digest`.

| Rolle | Tenants | Caps |
|---|---|---|
| `owner` | `*` | `read`, `trade`, `lock`, `config_read`, `kill` |
| `operator` | genau ein `tenant_id` | `read`, `trade`, `lock` |
| `observer` | genau ein `tenant_id` | `read` |

`authorize` Reihenfolge:

1. `mcp.enabled` sonst `mcp_disabled`
2. Actor vorhanden und `status=active` sonst `unauthorized`
3. `tenant_id` in `actor.tenants` oder `*` sonst `tenant_forbidden` (kein Leak)
4. `action` in `caps` sonst `forbidden`
5. Trade/Lock und `allow_writes=false` → `writes_disabled`

Owner darf Tool-Arg `tenant=` setzen. Operator: Tenant **forced** auf self (Arg ignoriert).

Bootstrap v0: Env `MCP_OWNER_TOKEN` → owner-Actor. Weitere Actors: Mongo `mcp_actors` oder Env `MCP_ACTORS_JSON` (Tests injecten ein Dict). Keine Klartext-Tokens in Git.

## Runtime

```
Grok TUI / Team
    │  MCP Streamable HTTP + Bearer
    ▼
xagent-mcp  (Railway test, PORT, /health)
    │  authorize()
    ├─ read  → build_snapshot / lots / fusion  (Mongo, fail-open)
    └─ write → POST xagent-test /internal/mcp/execute
                    X-Exit-Ws-Token + tenant_id + actor_id
                    TradingService.execute_buy/sell + set_position_lock
```

Start: `RAILWAY_SERVICE_NAME=xagent-mcp` → `python -m services.mcp_sidecar`  
Gleiches Docker-Image wie der Bot (`railway_start.sh` Selector, wie Santiment/Radar).

Health: `GET /health` → `{ok:true, service:xagent-mcp}`. Railway healthcheck muss auf diesem Service `/health` treffen (nicht den Bot-Boot).

## Tools (v0)

| Tool | Action | Notes |
|---|---|---|
| `xagent_whoami` | — | actor_id, role, tenants, caps |
| `xagent_snapshot` | `read` | Desk-Snapshot (lots, HUD, next_edge, badges) |
| `xagent_lots` | `read` | Offene Lots, tenant-scoped |
| `xagent_buy` | `trade` | `symbol`, `usdt`, `timeframe`; Bot sized/blocked |
| `xagent_sell` | `trade` | `symbol`, `pct` 0–100 oder `amount`; Bot sized/blocked |
| `xagent_lock` / `xagent_unlock` | `lock` | Position lock wie Telegram `/lock` |

Kein Cap-Slider, kein RelVol-Set, kein Gate-Key, kein Live-Switch.

`source` am Order: `mcp:<actor_id>`.

## Bot-Endpoint

`POST /internal/mcp/execute` auf **xagent-test** (nicht öffentlich dokumentieren).

- Token unset → 503 `not_configured`
- Token mismatch → 401
- Body: `{action, tenant_id, symbol, timeframe, usdt?, pct?, amount?, reason?, actor_id}`
- `tenant_context(tenant_id)` dann `execute_buy` / `execute_sell` / `set_position_lock`
- Risk reject → 200 `{ok:false, executed:false, message}` (kein 500)

MCP-Sidecar setzt `X-Exit-Ws-Token` aus `EXIT_WS_INTERNAL_TOKEN` oder `MCP_BOT_TOKEN`. URL: `MCP_BOT_URL` (z.B. `https://xagent-test-test.up.railway.app`).

## Fail-open / Fail-closed

| Layer | Policy |
|---|---|
| Authz / Token | **Fail-closed** |
| Snapshot/Mongo read | **Fail-open** leere Lots + error string |
| Write HTTP | Fehler an Grok zurück, **kein** Retry-Storm; Timeout 8s |
| Bot Risk | Reject ist Erfolg des Gates, nicht MCP-Crash |

## Test / Abnahme

- Owner-Token + `tenant=henry` → henry-Snapshot.
- Henry-Operator-Token + `tenant=default` → `tenant_forbidden`, kein default-Lot in der Response.
- Observer + `xagent_buy` → `forbidden`.
- `mcp.allow_writes=false` + owner buy → `writes_disabled`.
- `mcp.enabled=false` → alle Tools `mcp_disabled`.
- Buy auf Paper: Order `source=mcp:<actor_id>`, RiskManager kann size↓ oder block.
- Sidecar `/health` 200 ohne Bot.
- Load: 20 parallele `xagent_snapshot` ändern **keine** Bot-CPU (kein POST an `/internal/mcp/execute`).

## Nicht v0

Live-Trading, RelVol-Cap schreiben, Lesson-Editor, Radar/Cortex iframe, Token-UI im Desk, Production-Service.
