# xagent MCP v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a tenant-scoped MCP server (`xagent-mcp`) so a Grok trading team can read paper state and trigger buys/sells through RiskManager.

**Architecture:** Authz is a pure `authorize()` gate. Reads call `build_snapshot` (Mongo/Redis, bot not involved). Writes POST to `xagent-test` `/internal/mcp/execute`. Sidecar is a second Railway process from the same image (`RAILWAY_SERVICE_NAME=xagent-mcp`).

**Tech Stack:** Python 3.13, pytest, Flask (bot execute route), MCP Python SDK (`mcp` FastMCP streamable HTTP), existing `services.desk.snapshot`, `TradingService`.

**Spec:** `plans/mcp-xagent-grok-v0.md`

---

## File map

| Path | Role |
|------|------|
| `services/mcp_authz.py` | `Actor`, `authorize()`, cap/role constants |
| `services/mcp_tokens.py` | SHA-256 hash, lookup, env bootstrap |
| `services/mcp_client.py` | HTTP write client to bot execute |
| `services/mcp_tools.py` | Tool bodies (whoami, snapshot, lots, buy, sell, lock) |
| `services/mcp_sidecar/__init__.py` | package |
| `services/mcp_sidecar/__main__.py` | FastMCP + `/health` |
| `services/mcp_bot_http.py` | Flask `POST /internal/mcp/execute` on the bot |
| `config.json` | `mcp.enabled`, `allow_writes` |
| `aria_bot.py` | register execute route |
| `scripts/railway_start.sh` | sidecar selector |
| `requirements.txt` | `mcp` |
| `tests/unit/test_mcp_authz.py` | ACL |
| `tests/unit/test_mcp_tokens.py` | hash/lookup |
| `tests/unit/test_mcp_tools.py` | tools with fakes |
| `tests/unit/test_mcp_bot_http.py` | execute route token + tenant_context |

**Out of this plan:** RelVol caps, live trading, production `xagent-bot`, Desk UI, new Mongo collection UI.

---

### Task 1: Config kill switch

**Files:**
- Modify: `config.json` (top-level `mcp` next to `desk`)
- Create: `services/mcp_authz.py` (`mcp_enabled` / `mcp_writes_enabled` only)
- Test: `tests/unit/test_mcp_authz.py`

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_mcp_authz.py
from services.mcp_authz import mcp_enabled, mcp_writes_enabled

def test_mcp_disabled_by_default_without_flag():
    assert mcp_enabled({}) is False
    assert mcp_writes_enabled({}) is False

def test_mcp_flags():
    raw = {"mcp": {"enabled": True, "allow_writes": True}}
    assert mcp_enabled(raw) is True
    assert mcp_writes_enabled(raw) is True
    assert mcp_writes_enabled({"mcp": {"enabled": True, "allow_writes": False}}) is False
```

- [ ] **Step 2:** `./scripts/run_unit_tests.sh tests/unit/test_mcp_authz.py -q` → FAIL import

- [ ] **Step 3:**

```python
# services/mcp_authz.py
def mcp_enabled(config_raw: dict | None) -> bool:
    mcp = (config_raw or {}).get("mcp")
    if not isinstance(mcp, dict):
        return False
    return bool(mcp.get("enabled"))

def mcp_writes_enabled(config_raw: dict | None) -> bool:
    mcp = (config_raw or {}).get("mcp")
    if not isinstance(mcp, dict):
        return False
    return bool(mcp.get("enabled")) and bool(mcp.get("allow_writes"))
```

`config.json` after `desk`:

```json
"mcp": {
  "enabled": true,
  "allow_writes": true,
  "tenants": ["default", "henry", "ctexp"],
  "_doc": "Grok MCP sidecar xagent-mcp. Kill: enabled=false (deaf) or allow_writes=false (reads only)."
}
```

Non-dict `mcp` fail-closed.

- [ ] **Step 4:** tests pass

- [ ] **Step 5:** Commit `feat(mcp): enabled and allow_writes kill switches`

---

### Task 2: authorize()

**Files:**
- Modify: `services/mcp_authz.py`
- Modify: `tests/unit/test_mcp_authz.py`

- [ ] **Step 1: Failing tests**

```python
from services.mcp_authz import Actor, authorize

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))
OBS = Actor("henry-obs", "observer", ("henry",), ("read",))

def test_owner_can_trade_ctexp():
    ok, err = authorize(OWNER, "trade", "ctexp", writes_enabled=True)
    assert ok and err == ""

def test_operator_cannot_read_default():
    ok, err = authorize(HENRY, "read", "default")
    assert ok is False and err == "tenant_forbidden"

def test_observer_cannot_buy():
    ok, err = authorize(OBS, "trade", "henry", writes_enabled=True)
    assert ok is False and err == "forbidden"

def test_writes_kill():
    ok, err = authorize(OWNER, "trade", "default", writes_enabled=False)
    assert ok is False and err == "writes_disabled"

def test_missing_actor():
    ok, err = authorize(None, "read", "default")
    assert ok is False and err == "unauthorized"
```

Also: `enabled=False` via `authorize(..., enabled=False)` → `mcp_disabled`.

- [ ] **Step 2:** run → FAIL `authorize` missing

- [ ] **Step 3:** Implement

```python
from dataclasses import dataclass

ROLES = ("owner", "operator", "observer")
ACTIONS = ("read", "trade", "lock", "config_read", "kill")

@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    tenants: tuple[str, ...]
    caps: tuple[str, ...]
    status: str = "active"

def authorize(
    actor: Actor | None,
    action: str,
    tenant_id: str,
    *,
    enabled: bool = True,
    writes_enabled: bool = True,
) -> tuple[bool, str]:
    if not enabled:
        return False, "mcp_disabled"
    if actor is None or actor.status != "active":
        return False, "unauthorized"
    tid = str(tenant_id or "").strip()
    if "*" not in actor.tenants and tid not in actor.tenants:
        return False, "tenant_forbidden"
    if action not in actor.caps:
        return False, "forbidden"
    if action in ("trade", "lock") and not writes_enabled:
        return False, "writes_disabled"
    return True, ""
```

Owner tenants must include `"*"`. Do not special-case `ctexp` by name.

- [ ] **Step 4:** tests pass (plus Task 1)

- [ ] **Step 5:** Commit `feat(mcp): tenant-scoped authorize gate`

---

### Task 3: Token hash + bootstrap

**Files:**
- Create: `services/mcp_tokens.py`
- Test: `tests/unit/test_mcp_tokens.py`

- [ ] **Step 1:**

```python
from services.mcp_tokens import hash_token, actor_from_bearer, bootstrap_from_env

def test_hash_is_sha256_hex():
    h = hash_token("secret")
    assert len(h) == 64 and h != "secret"

def test_owner_env_token():
    actors = bootstrap_from_env(owner_token="owner-secret", extras=[])
    a = actor_from_bearer("owner-secret", actors)
    assert a is not None and a.role == "owner" and "*" in a.tenants
    assert actor_from_bearer("wrong", actors) is None

def test_operator_extra():
    extras = [{
        "token": "henry-secret",
        "actor_id": "henry-op",
        "role": "operator",
        "tenants": ["henry"],
        "caps": ["read", "trade", "lock"],
    }]
    actors = bootstrap_from_env(owner_token="owner-secret", extras=extras)
    a = actor_from_bearer("henry-secret", actors)
    assert a.tenants == ("henry",)
    assert actor_from_bearer("owner-secret", actors).role == "owner"
```

Index the map by **hash**, never by plaintext.

- [ ] **Step 2:** FAIL import

- [ ] **Step 3:**

```python
import hashlib, hmac
from services.mcp_authz import Actor

OWNER_CAPS = ("read", "trade", "lock", "config_read", "kill")

def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()

def actor_from_bearer(raw: str, actors_by_hash: dict[str, Actor]) -> Actor | None:
    if not raw:
        return None
    h = hash_token(raw)
    for stored, actor in (actors_by_hash or {}).items():
        if hmac.compare_digest(stored, h):
            return actor
    return None

def bootstrap_from_env(*, owner_token: str, extras: list[dict] | None = None) -> dict[str, Actor]:
    out: dict[str, Actor] = {}
    if owner_token:
        out[hash_token(owner_token)] = Actor(
            "owner", "owner", ("*",), OWNER_CAPS,
        )
    for row in extras or []:
        tok = str(row.get("token") or "")
        if not tok:
            continue
        caps = tuple(row.get("caps") or ("read",))
        tenants = tuple(row.get("tenants") or ())
        out[hash_token(tok)] = Actor(
            str(row.get("actor_id") or "actor"),
            str(row.get("role") or "observer"),
            tenants,
            caps,
        )
    return out
```

Live load extras from env `MCP_ACTORS_JSON` (JSON list) inside sidecar later — not required in this unit.

- [ ] **Step 4:** tests pass

- [ ] **Step 5:** Commit `feat(mcp): hashed bearer tokens and owner bootstrap`

---

### Task 4: Read tools (injectable snapshot)

**Files:**
- Create: `services/mcp_tools.py`
- Test: `tests/unit/test_mcp_tools.py`

- [ ] **Step 1:** Tools take `actor` + injectable `snapshot_fn`. Henry operator asking `tenant=default` returns `{ok:false, error:tenant_forbidden}` and **must not** call `snapshot_fn`.

```python
from services.mcp_authz import Actor, authorize
from services.mcp_tools import tool_snapshot, tool_lots, tool_whoami

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))

def test_whoami():
    w = tool_whoami(HENRY)
    assert w["actor_id"] == "henry-op" and w["tenants"] == ["henry"]

def test_operator_forced_tenant(monkeypatch):
    calls = []
    def fake_snap(**kw):
        calls.append(kw)
        return {"ok": True, "tenant_id": kw["tenant_id"], "lots": [{"symbol": "AAA/USDT"}]}
    out = tool_snapshot(HENRY, tenant="default", symbol="LAB/USDT", snapshot_fn=fake_snap)
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"  # forced
    assert calls[0]["tenant_id"] == "henry"

def test_operator_cannot_see_default():
    def boom(**kw):
        raise AssertionError("must not load foreign ledger")
    # Direct authorize path: if someone bypasses force, still deny
    from services.mcp_authz import authorize
    ok, err = authorize(HENRY, "read", "default")
    assert not ok and err == "tenant_forbidden"

def test_snapshot_deny_does_not_call():
    called = []
    def fake(**kw):
        called.append(1)
        return {"ok": True}
    obs = Actor("o", "observer", ("henry",), ("read",))
    # observer read henry is ok — use missing actor instead
    out = tool_snapshot(None, tenant="henry", symbol="X", snapshot_fn=fake)
    assert out["ok"] is False and out["error"] == "unauthorized"
    assert called == []
```

`tool_snapshot` for operator: `effective_tenant = actor.tenants[0] if "*" not in actor.tenants else (requested or "default")`.

- [ ] **Step 2:** FAIL

- [ ] **Step 3:** Implement `tool_whoami`, `tool_snapshot`, `tool_lots` wrapping `authorize` then `snapshot_fn` (default `services.desk.snapshot.build_snapshot`). Fail-open on snapshot exceptions: `{ok:false, error:snapshot_failed}`.

- [ ] **Step 4:** tests pass

- [ ] **Step 5:** Commit `feat(mcp): read tools tenant-forced snapshot`

---

### Task 5: Bot execute route

**Files:**
- Create: `services/mcp_bot_http.py`
- Modify: `aria_bot.py` (try/except register next to desk)
- Test: `tests/unit/test_mcp_bot_http.py` (tiny Flask, never `aria_bot.app`)

- [ ] **Step 1:**

```python
def test_execute_503_without_token(monkeypatch):
    monkeypatch.delenv("EXIT_WS_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    rv = client.post("/internal/mcp/execute", json={"action": "buy", "tenant_id": "default", "symbol": "LAB/USDT"})
    assert rv.status_code == 503

def test_execute_401_bad_token(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    rv = client.post("/internal/mcp/execute", json={...}, headers={"X-Exit-Ws-Token": "nope"})
    assert rv.status_code == 401

def test_execute_buy_calls_trading_service(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    # monkeypatch TradingService.execute_buy to record tenant from tenant_snapshot()
    ...
    rv = client.post(..., headers={"X-Exit-Ws-Token": "secret"}, json={
        "action": "buy", "tenant_id": "henry", "symbol": "LAB/USDT", "usdt": 50, "timeframe": "1h", "actor_id": "jens",
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert "executed" in body
```

Token expected = `MCP_BOT_TOKEN` or `EXIT_WS_INTERNAL_TOKEN`. Unset → 503. Same header family as fire_http.

- [ ] **Step 2:** FAIL

- [ ] **Step 3:** `register_mcp_bot_routes(app)`:

```python
@app.route("/internal/mcp/execute", methods=["POST"])
def mcp_execute():
    ...
    with tenant_context(tenant_id):
        if action == "buy":
            result = TradingService().execute_buy(symbol, timeframe, price=0, usdt=usdt)
        elif action == "sell":
            ...
        elif action == "lock":
            set_position_lock(...)
```

`source` / request_extra include `mcp:<actor_id>`. Price 0: TradingService/risk should refresh mark — if execute_buy requires price, fetch via existing price helper fail-closed with message. If messy, require `price` in body from MCP tool (last close from snapshot). Prefer: tool sends `price` from snapshot last/live; bot still re-evaluates risk.

GET none. POST only this path.

Register in `aria_bot.py` try/except WARNING.

- [ ] **Step 4:** tests pass; `test_desk_http.py` still green if run

- [ ] **Step 5:** Commit `feat(mcp): token-gated bot execute route`

---

### Task 6: Write tools (HTTP client)

**Files:**
- Create: `services/mcp_client.py`
- Modify: `services/mcp_tools.py` (`tool_buy`, `tool_sell`, `tool_lock`, `tool_unlock`)
- Test: `tests/unit/test_mcp_tools.py` (fake `execute_fn`)

- [ ] **Step 1:** Henry buy on default denied without HTTP. Owner buy calls execute_fn with tenant henry when requested.

```python
def test_buy_denied_for_observer():
    obs = Actor("o", "observer", ("henry",), ("read",))
    called = []
    out = tool_buy(obs, tenant="henry", symbol="LAB/USDT", usdt=10, execute_fn=lambda **k: called.append(k) or {"ok": True})
    assert out["error"] == "forbidden" and called == []

def test_owner_buy_posts_tenant():
    called = []
    out = tool_buy(OWNER, tenant="henry", symbol="LAB/USDT", usdt=25, execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True})
    assert out["ok"] is True
    assert called[0]["tenant_id"] == "henry"
    assert called[0]["action"] == "buy"
```

- [ ] **Step 2:** FAIL

- [ ] **Step 3:** `execute_fn` default posts JSON to `MCP_BOT_URL` + `/internal/mcp/execute` with token, timeout 8s. On network error `{ok:false, error:bot_unreachable}`.

- [ ] **Step 4:** tests pass

- [ ] **Step 5:** Commit `feat(mcp): buy sell lock tools via bot HTTP`

---

### Task 7: Sidecar FastMCP + health

**Files:**
- Create: `services/mcp_sidecar/__init__.py`
- Create: `services/mcp_sidecar/__main__.py`
- Create: `services/mcp_sidecar/app.py` (Starlette/FastMCP + `/health`)
- Modify: `requirements.txt` add `mcp>=1.2.0`
- Test: `tests/unit/test_mcp_sidecar_health.py` (ASGI test client or Flask-like)

If `mcp` FastMCP streamable HTTP is awkward in unit tests, extract `health_payload()` and a tiny Starlette route; test health without full MCP handshake.

- [ ] **Step 1:** `GET /health` → 200 `{ok:true, service:"xagent-mcp"}` even when `mcp.enabled=false` (process up, tools deaf).

- [ ] **Step 2:** FAIL

- [ ] **Step 3:** Bind FastMCP tools to `tool_*`. Auth: Bearer from MCP HTTP headers → `actor_from_bearer`. Port `PORT` default 8080. `DEMO_MODE=1`.

Do not start price loop. Do not import `aria_bot`.

- [ ] **Step 4:** health test pass; `python -m services.mcp_sidecar` locally listens (manual)

- [ ] **Step 5:** Commit `feat(mcp): FastMCP sidecar with health`

---

### Task 8: Railway start + docs

**Files:**
- Modify: `scripts/railway_start.sh` — selector `xagent-mcp` / `RUN_MCP_SIDECAR=1`
- Modify: `plans/mcp-xagent-grok-v0.md` Deploy section (8 lines): URL, tokens, kill, Grok TUI remote MCP
- Optional: `services/mcp_sidecar/railway.toml` if Radar/Santiment style is required — prefer **same Dockerfile** + start selector so one image.

```bash
if [[ "${RAILWAY_SERVICE_NAME:-}" == "xagent-mcp" || "${RUN_MCP_SIDECAR:-}" == "1" ]]; then
  echo "=== xagent MCP sidecar (Grok team, no price loop) ==="
  export PYTHONUNBUFFERED=1
  export DEMO_MODE="${DEMO_MODE:-1}"
  export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"
  export MONGODB_DB="${MONGODB_DB:-xagent_test}"
  export DEMO_ALLOW_REMOTE_MONGO="${DEMO_ALLOW_REMOTE_MONGO:-1}"
  exec python3 -m services.mcp_sidecar
fi
```

Place **before** the main `aria_bot.py --demo` block.

Docs: Grok TUI remote MCP `https://<xagent-mcp-domain>/mcp` (exact path = FastMCP streamable HTTP path — document whatever `__main__` mounts). Token `MCP_OWNER_TOKEN`. `MCP_BOT_URL` = paper bot public URL.

Do **not** create the Railway service in this PR (operator adds `xagent-mcp` in dashboard, same image, env `test`). Note that in docs.

- [ ] **Step 1:** unit tests still pass (`test_mcp_authz`, `test_mcp_tokens`, `test_mcp_tools`, `test_mcp_bot_http`)

- [ ] **Step 2:** Commit `feat(mcp): railway sidecar selector and deploy notes`

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| authorize / roles / caps | 2 |
| hashed tokens / owner bootstrap | 3 |
| kill enabled / allow_writes | 1, 2 |
| Reads without bot | 4 |
| Writes via TradingService | 5–6 |
| Operator tenant force | 4, 6 |
| Sidecar process | 7–8 |
| Railway test not prod | 8 |
| Paper | 5–8 DEMO_MODE |
| ctexp owner-visible, not hardcoded deny | 2 (`*`) |
| No RelVol knobs | tools list |

## Placeholders

None. Creating the Railway service in the dashboard is **operator**, not code.
