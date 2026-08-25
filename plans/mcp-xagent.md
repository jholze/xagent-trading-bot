# xagent MCP — Grok trading team

**Status:** shipped on Railway env **test** (paper). Not production / not `xagent-bot`.  
**Spec lock (DE):** [mcp-xagent-grok-v0.md](mcp-xagent-grok-v0.md)

A Grok client steers the **paper** bot over MCP: read lots, orders, memory, and *why* a coin was bought; optionally buy / sell / lock through the same `TradingService` + `RiskManager` path as Telegram.

Owner (Jens) can use every tenant (`default`, `henry`, `ctexp`). Anyone else is scoped to **one** tenant.

## Runtime

```
Grok TUI / team
    │  Streamable HTTP + Bearer
    ▼
xagent-mcp     Railway test, GET /health, POST /mcp
    │  authorize()
    ├─ read  → snapshot / lots / orders / memory / why   (Mongo/Redis, fail-open)
    └─ write → POST xagent-test /internal/mcp/execute
                    X-Exit-Ws-Token + tenant + actor
                    TradingService + RiskManager
```

Reads do **not** enter the bot price loop. Writes are a short HTTP call into `xagent-test`.

Start: `RAILWAY_SERVICE_NAME=xagent-mcp` → `python -m services.mcp_sidecar` (same Docker image as the bot; selector in `scripts/railway_start.sh`).

Live URLs (test):

| | |
|---|---|
| MCP | `https://xagent-mcp-test.up.railway.app/mcp` |
| Health | `GET https://xagent-mcp-test.up.railway.app/health` → `{ok:true, service:xagent-mcp}` |
| Paper bot | `https://xagent-test-test.up.railway.app` |

Grok TUI: remote MCP URL + `Authorization: Bearer <token>`. Reconnect after tool or token changes.

## Roles

| Role | Tenants | Caps |
|---|---|---|
| `owner` | `*` | `read`, `trade`, `lock`, `config_read`, `kill` |
| `operator` | exactly one | `read`, `trade`, `lock` |
| `observer` | exactly one | `read` |

`authorize(actor, action, tenant_id)` is fail-closed, in this order:

1. `mcp.enabled` else `mcp_disabled`
2. Actor present and `status=active` else `unauthorized`
3. `tenant_id` in actor tenants or `*` else `tenant_forbidden`
4. Action in caps else `forbidden`
5. Trade/lock and `allow_writes=false` else `writes_disabled`

Owner may pass `tenant=`. Operator tenant is **forced** to self (the argument is ignored). Extra actors from `MCP_ACTORS_JSON` **cannot** mint `owner` or `*` tenants; observer caps cannot include `trade`/`lock`.

Owner bootstrap: env `MCP_OWNER_TOKEN` (SHA-256 at rest in process memory, `hmac.compare_digest`). Optional extras: `MCP_ACTORS_JSON` (plaintext tokens only in Railway env, never Git).

```json
[
  {
    "token": "<henry-secret>",
    "actor_id": "henry-op",
    "role": "operator",
    "tenants": ["henry"],
    "caps": ["read", "trade", "lock"]
  }
]
```

`[ctexp]` on Jens’s Telegram is the **owner inbox**, not a ledger mix. Owner may still read/trade `ctexp` via MCP.

## Tools

| Tool | Cap | What it returns / does |
|---|---|---|
| `xagent_whoami` | — | `actor_id`, role, tenants, caps |
| `xagent_snapshot` | `read` | Desk snapshot (lots, HUD, next_edge, badges) |
| `xagent_lots` | `read` | Open lots, tenant-scoped (ledger fallback if RAM empty) |
| `xagent_orders` | `read` | Filled/rejected/failed: `source`, `signal`, risk, size |
| `xagent_memory` | `read` | CoinProfile, FactFlags, events, trade memory, lessons, RAG (no embeddings) |
| `xagent_why` | `read` | Per-coin pack: lot + HUD + orders/signals + memory + facts + RAG |
| `xagent_buy` | `trade` | `symbol`, `usdt`, `timeframe` — bot sizes or blocks |
| `xagent_sell` | `trade` | `pct` 0–100 or `amount` |
| `xagent_lock` / `xagent_unlock` | `lock` | Same path as Telegram `/lock` |

No RelVol cap writes, no Gate keys, no live-mode switch, no cap slider.

Filled orders carry `source=mcp:<actor_id>`.

CMC/Lunar HUD on the sidecar is often `IDLE`: that snapshot lives in the **bot process RAM**, not Mongo. Orders, memory, facts, and RAG come from the database.

## Config (`config.json`)

```json
"mcp": {
  "enabled": true,
  "allow_writes": true,
  "allow_live": false,
  "write_rate_per_min": 20,
  "tenants": ["default", "henry", "ctexp"]
}
```

| Flag | Effect |
|---|---|
| `enabled=false` | Tools return `mcp_disabled` (process still serves `/health`) |
| `allow_writes=false` | Reads only |
| `allow_live=false` | Blocks **real** Gate fills (`is_real_live_trading`). Staging simulated-live / dry-run still works |
| `write_rate_per_min` | Sidecar write cap per actor (default 20 / 60s) |
| `tenants` | Allowlist on the bot execute route |

## Railway env

| Variable | Where | Role |
|---|---|---|
| `MCP_OWNER_TOKEN` | `xagent-mcp` | Owner Bearer |
| `MCP_ACTORS_JSON` | `xagent-mcp` | Optional extra actors |
| `MCP_BOT_URL` | `xagent-mcp` | Paper bot base URL |
| `MCP_BOT_TOKEN` | **both** `xagent-mcp` and `xagent-test` | Execute secret. If set, Exit-WS token is **not** accepted on `/internal/mcp/execute` |
| `MCP_BOT_TIMEOUT_SEC` | `xagent-mcp` | Write timeout (default 45, min 5) |
| `DEMO_MODE=1` | both | Paper / demo ledger |
| `EXIT_WS_INTERNAL_TOKEN` | bot (and fallback only if `MCP_BOT_TOKEN` unset) | Exit-WS; do not reuse once `MCP_BOT_TOKEN` exists |

Rotate owner token in Railway, then update Grok `~/.grok/config.toml` `[mcp_servers.xagent.headers]`. Restart does **not** always pick up skipped deploys — **redeploy** after token changes.

## Write path (defense in depth)

Sidecar (after ACL):

- 30-second idempotency bucket (timeout + retry does not double-fill)
- 20 writes / minute / actor → `rate_limited`

Bot `POST /internal/mcp/execute` (not a public product API):

- HMAC compare of `MCP_BOT_TOKEN` (preferred) or Exit-WS fallback
- `mcp.enabled` / `allow_writes` / tenant allowlist
- `allow_live=false` rejects real Gate
- Passes `source=mcp:<actor>` and `idempotency_key` into `TradingService`
- Risk reject is HTTP 200 `{ok:false, executed:false, message}` — not a 500

Telegram `execute_buy` is unchanged (`source=manual` by default).

## Fail-open / fail-closed

| Layer | Policy |
|---|---|
| Authz / tokens | Fail-closed |
| Snapshot / Mongo / RAG read | Fail-open: empty lists + `errors[]` |
| Write HTTP | Error to Grok, no retry storm, 45s timeout |
| Bot risk reject | Success of the gate, not an MCP crash |

## Kill / rollback

1. `mcp.allow_writes=false` — reads only  
2. `mcp.enabled=false` — tools deaf  
3. Stop Railway service `xagent-mcp`  
4. Overwrite `MCP_OWNER_TOKEN` — old Bearers die after **redeploy**

## Out of scope (on purpose)

Live Gate trading, production `xagent-bot`, Railway private networking for execute, IP allowlists, short-lived tokens, RelVol cap writes, lesson editor, Desk token UI.

## Tests

```bash
./scripts/run_unit_tests.sh \
  tests/unit/test_mcp_authz.py \
  tests/unit/test_mcp_tokens.py \
  tests/unit/test_mcp_bot_http.py \
  tests/unit/test_mcp_tools.py \
  tests/unit/test_mcp_explain.py \
  tests/unit/test_mcp_sidecar_health.py
```
