# xAI subscription-auth sidecar (issue #397)

Goal of issue #397: let the bot's xAI calls (`grok_x_search.py`,
`intelligence/llm_client.py`, `services/watchlist_quality/ai_critic.py`)
authenticate with the operator's **SuperGrok / X Premium subscription** instead
of (or as a fallback to) a pay-per-use `XAI_API_KEY`.

Phase 1 is a *spike*: it reuses the device-code OAuth flow shipped in
[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi) (pinned
`0.85.1`) to obtain a subscription access token, then **measures live** what
that token is actually allowed to do against `https://api.x.ai/v1`.

Phase 1b (this document's [Railway section](#running-on-railway-xagent-xai-auth))
runs that login **on the staging Railway instance**: URL + user code go to the
operator Telegram, the session lives on a Railway Volume and is refreshed
before it expires, so it survives deploys.

Phase 2 (probe on the issue, 2026-09-15: Q1 `grok-4/4.3/4.5/4.6` all 200, Q2
`x_search` 200, Q3 no 429) adds the **`/v1` proxy** to this server and the
**`XAI_USE_SUBSCRIPTION` flag** to the bot — see
[Phase 2: `/v1` proxy + bot flag](#phase-2-v1-proxy--bot-flag). Default off; the
bot keeps using `XAI_API_KEY` until the operator sets the flag.

## Running on Railway (`xagent-xai-auth`)

This service has **its own Docker image** — `services/xai_auth_sidecar/Dockerfile`
(`FROM node:22-slim`, `npm ci --omit=dev` from the committed lockfile, `CMD bash
start.sh`). It is selected via `services/xai_auth_sidecar/railway.toml`
(`dockerfilePath`), the same pattern as `services/santiment/sidecar`. The
shared root `Dockerfile` that builds the bot, `xagent-mcp` and every Python
sidecar stays **Node-free** and is not involved; there is no build ARG to set.
(The first attempt baked Node into the shared image with a BuildKit
`RUN --mount=type=bind` — Railway's builder rejects that mount type and every
service on the shared Dockerfile failed to build. Do not bring it back.)

### Contract (all three are required)

| Piece | Setting | Why |
|---|---|---|
| Service | `xagent-xai-auth` from this repo, branch `staging`, **root directory = repo root** | the Dockerfile copies `services/xai_auth_sidecar/…` relative to the repo root |
| Config file | `services/xai_auth_sidecar/railway.toml` (service → Settings → Config-as-code) | sets `dockerfilePath = "services/xai_auth_sidecar/Dockerfile"` so this service builds the Node image, not the shared Python one. Health check `GET /health`. |
| **Volume** | mount at **`/data/grok`** | the credential is written to `$RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json` (mode `0600`, dir `0700`). Without a volume the session dies on every deploy — `start.sh` warns but still starts. |

`scripts/railway_start.sh` still contains a `RAILWAY_SERVICE_NAME=xagent-xai-auth`
/ `RUN_XAI_AUTH=1` branch. With the dedicated image it is **unused** (the
image's `CMD` is `start.sh` directly); it only remains as a safety net — if the
service were ever pointed at the shared Python image, `start.sh` fails fast
with "node not found" instead of starting the bot.

Variables on the `xagent-xai-auth` service:

| Variable | Default | Purpose |
|---|---|---|
| `XAI_OAUTH_CREDENTIAL_PATH` | `$RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json` | where the `{type:"oauth",access,refresh,expires}` file lives; paths inside the repo (`/app/...`) are refused |
| `GROK_HOME` | `$RAILWAY_VOLUME_MOUNT_PATH/grok-cli` | only relevant if you log in with the Grok CLI (`grok login --device-auth`) over `railway ssh` — keeps its session on the volume too |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | operator chat that receives the verification URL + code, "session stored, expires …", and refresh failures. Same values as the bot service. |
| `XAI_AUTH_TRIGGER_TOKEN` | — (trigger + proxy disabled) | shared secret for `POST /login` **and** the `/v1` proxy; set the **same** value on the bot service to enable `/xai_login` and `XAI_USE_SUBSCRIPTION` |
| `XAI_AUTH_PROXY_TIMEOUT_MS` | `180000` | upper bound for one relayed xAI call (the bot's own client timeout is normally the tighter one) |
| `XAI_AUTH_LOGIN_ON_BOOT` | `0` | `1`: start a device-code login automatically when the service boots without a credential (each boot without a login = one URL+code message) |
| `XAI_AUTH_KEEPALIVE` | `1` | refresh the stored session before it expires (`0` to disable) |
| `XAI_AUTH_KEEPALIVE_INTERVAL_MS` | `300000` | keepalive check interval |
| `XAI_AUTH_REFRESH_MARGIN_MS` | `900000` | refresh when less than this remains; on failure it backs off (`interval·2ⁿ`, max 6 h) and notifies Telegram once |
| `PORT` | `8787` locally | HTTP port. **On Railway it is whatever Railway injects — currently `8080`**, not 8787. The bot's `XAI_AUTH_SIDECAR_URL` must carry that port (`http://xagent-xai-auth.railway.internal:8080`); check `/health` from the bot's shell if in doubt. |
| `XAI_API_KEY` | — | **not needed here**; stays the bot's fallback and is untouched by this service |

HTTP surface (JSON on the sidecar's own routes, never a token):

- `GET /health`, `GET /status` — `credential.{present,expiresAt,expiresInSec,expiringSoon}`, `login.{running,lastResult,lastError}`, `keepalive.*`, `proxy.{enabled,requests,rejected,noCredential,upstreamErrors,lastUpstreamStatus}`, `triggerEnabled`, `telegram.configured`
- `POST /login` with `Authorization: Bearer $XAI_AUTH_TRIGGER_TOKEN` — `202 {started:true}`, `409` while a login runs, `403` when the trigger token is unset on the sidecar, `401` on mismatch
- `GET|POST /v1/*` with the same bearer — relayed to `https://api.x.ai/v1/*` with the stored access token (Phase 2, below). `/login`, `/health`, `/status` are never proxied; `/health` stays token-free.

### How jholze logs in

**A. From Telegram (operator only, `/xai_login`).** Requires `XAI_AUTH_SIDECAR_URL`
(e.g. `http://xagent-xai-auth.railway.internal:8080` — the port Railway injects) and `XAI_AUTH_TRIGGER_TOKEN`
on the **bot** service. The command uses the same gate as `/onboard`
(`TELEGRAM_CHAT_ID` only) and is a no-op explanation until those two variables
exist; tenants cannot fire it.

1. `/xai_login` → bot POSTs to the sidecar → sidecar sends "🔐 … Open this URL …
   Code: XXXX-XXXX (valid until …)" to the operator chat.
2. Open the URL on any device, sign in with the X / SuperGrok account, enter the code.
3. Sidecar polls, stores the credential on the volume, sends "✅ xAI SuperGrok
   session stored … expires …".
4. `/xai_login status` any time: present / expiry / login running / keepalive failures.

**B. From a shell (`railway ssh`).** No trigger token needed:

```sh
railway ssh --service xagent-xai-auth
cd services/xai_auth_sidecar && npm run login    # URL + code on stdout AND in Telegram
npm run probe                                    # Q1/Q2/Q3 probe against the stored session
```

Railway injects `RAILWAY_VOLUME_MOUNT_PATH` into that shell too, and the
credential location rule (below) puts the file on the volume whenever that
variable is set — `login`, `probe` and the running server agree on the path
without exporting anything. Grok CLI alternative in the same shell:
`grok login --device-auth` with `GROK_HOME` on the volume — the sidecar does
not read that session, it is only kept for the operator's own use.

**C. Automatic on boot** — set `XAI_AUTH_LOGIN_ON_BOOT=1` if you prefer the
code to arrive right after a deploy without a credential. Off by default.

Revoke / start over: `railway ssh` → `rm "$XAI_OAUTH_CREDENTIAL_PATH"` and log in
again. Redeploys keep the file (it is on the volume, not in the image).

## Operator commands (local)

Requires Node >= 22.19 and a browser on any device.

```sh
cd services/xai_auth_sidecar && npm ci && npm run login
```

`login` prints a verification URL and a short code. Open the URL yourself,
sign in with the X / SuperGrok account, enter the code. The tool polls until
xAI confirms, then stores the credential (see location rule below) and prints
the path and expiry. It never prints a token and never opens a browser itself.

```sh
npm run probe
```

`probe` loads the credential (refreshing it first if it is about to expire) and
answers the three open questions of the issue, printing a Markdown report
followed by a JSON blob (both token-redacted):

| | Question | What the probe does |
|---|---|---|
| Q1 | Does the token work as a Bearer key on the standard Responses API, and for which models? | `GET /v1/models`, then `responses.create` for each of `grok-4, grok-4.3, grok-4.5, grok-4.6` (override with `XAI_PROBE_MODELS=a,b,c`) capturing HTTP status and all `x-ratelimit-*` / `retry-after` headers |
| Q2 | Does it authorise the `x_search` tool? | `responses.create` with **exactly** `grok_x_search.py`'s tool shape (`{type:"x_search", allowed_x_handles:["xai"], from_date, to_date}`, last 7 days) on the first model that passed Q1. Same keys in the same order; the only deliberate difference is that `buildXSearchTool` strips a leading `@` from the handle while `grok_x_search.py` passes it verbatim (it has already stripped it upstream). |
| Q3 | What are the effective rate limits? | All headers collected above, plus one burst of 5 tiny sequential calls 1 s apart, stopping at the first 429 |

If `XAI_API_KEY` is set in the environment the probe repeats Q1 (one model) and
Q2 with the plain API key and prints both side by side, so "works with key,
401/403 with token" is conclusive. If it is unset the report says so.

Every failed call is reported with its status and an excerpt of the error
body — never as a blank cell. Save the output to the issue.

`node src/login.js --help` / `node src/probe.js --help` print usage offline.

## Credential location rule

The credential (`{type:"oauth", access, refresh, expires}`) is stored at, first
match wins:

1. `$XAI_OAUTH_CREDENTIAL_PATH`
2. `$RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json` (Railway: the attached volume, e.g. `/data/grok/xai_oauth.json`)
3. `$XDG_CONFIG_HOME/xagent-trading-bot/xai_oauth.json`
4. `~/.config/xagent-trading-bot/xai_oauth.json`

The file is written with mode `0600` inside a `0700` directory, atomically
(temp file + rename). Both `login` and the credential store **refuse any path
that resolves inside the repository / worktree** — credentials never live in
the repo tree and never go into git. Access-token lifetime is 3600 s (pi-ai
stores `expires` with a 5-minute safety skew); the probe refreshes
automatically via the pi-ai `refresh` grant when needed.

Revoke / start over: delete the credential file and run `npm run login` again.

## How pi-ai is used

Only the public entry point `@earendil-works/pi-ai/providers/xai` is imported;
`xaiProvider().auth.oauth` exposes `login(interaction)`, `refresh(credential,
signal)` and `toAuth(credential)` (the last returns `{ apiKey: access }`, i.e.
the token is a plain Bearer key). The flow itself is not copied.

## Development

```sh
npm ci
npm test          # node --test, no network, never touches the real credential path
npm start         # the Railway server locally: PORT=8787 XAI_OAUTH_CREDENTIAL_PATH=/tmp/x/xai.json
```

Files: `src/login.js` (device-code CLI), `src/server.js` (Railway service:
`/health`, `/status`, `POST /login`, `/v1/*` proxy, keepalive, login-on-boot), `src/telegram.js`
(operator notifications, fail-soft), `src/credentials.js` (location rule, 0600,
refuse-in-repo, refresh), `src/probe.js` + `src/probe_report.js` (Q1–Q3),
`start.sh` (Railway entry — `CMD` of `Dockerfile`), `Dockerfile` + `railway.toml`
(dedicated Node 22 image, see above).

Dependencies: `@earendil-works/pi-ai@0.85.1` (device-code flow) and
`openai@6.40.0` (same version pi-ai depends on; used by the probe). No build
step, no test framework, plain ESM.

## Phase 2: `/v1` proxy + bot flag

The probe (issue #397, 2026-09-15) answered the open questions: the
subscription token is accepted as a plain Bearer key on the Responses API for
`grok-4`, `grok-4.3`, `grok-4.5`, `grok-4.6` (Q1), it authorises the
`x_search` tool (Q2), and a small burst saw no 429 (Q3). So the default model
stays `grok-4`, and the bot's xAI calls can move behind one flag.

### Sidecar side: `GET|POST /v1/*`

`src/server.js` relays `GET`/`POST /v1/*` to `https://api.x.ai/v1/*`:

1. Bearer check with the **same** `XAI_AUTH_TRIGGER_TOKEN` as `POST /login`
   (constant-time compare). Token unset on the sidecar → `403
   {error:"trigger_disabled"}` and nothing is forwarded (fail-closed). Wrong or
   missing token → `401`. Other methods → `405`.
2. Loads the volume credential and runs `ensureFresh` (refreshes via pi-ai if
   inside the refresh margin, persists the rotated token). No file / unreadable →
   `503 {error:"no_credential"}`; refresh failed → `503 {error:"refresh_failed"}`.
   Both make the bot fall back to `XAI_API_KEY`.
3. Replaces the client's `Authorization` with `Bearer <access>` and forwards the
   request (path + query + body; hop-by-hop headers, `host`, `x-auth-token`,
   `accept-encoding` dropped). xAI's status, headers and body come back
   unchanged (`429`, `500`, … are the bot's to handle — the proxy does not
   retry). xAI unreachable → `502 {error:"upstream_unreachable"}`.

The access token exists only inside the sidecar process and in the request to
`api.x.ai`; the upstream origin is a constant, not configurable. Logs go
through `redactText` with the access, refresh **and** trigger token registered,
so a `proxy: POST /v1/responses → 200 (812 ms)` line is all you see. Requests
counters live in `GET /status` → `proxy`. No public Railway domain is needed or
wanted: the bot reaches the sidecar over private networking only.

### Bot side: `XAI_USE_SUBSCRIPTION`

One helper, `intelligence/xai_auth.py`, decides for every xAI call:

| Bot variable | Default | Effect |
|---|---|---|
| `XAI_USE_SUBSCRIPTION` | **off** (unset / `0` / `false`) | `1` routes `intelligence/llm_client.py` (xai backend — that includes WQE `ai_critic` via `ask_grok_json`) and `grok_x_search.py` (→ `x_data_provider.GrokXSearchProvider`) through the sidecar: `base_url = $XAI_AUTH_SIDECAR_URL/v1`, `api_key = $XAI_AUTH_TRIGGER_TOKEN`. With the flag on the trigger token is enough — `XAI_API_KEY` may be absent. |
| `XAI_AUTH_SIDECAR_URL` | — | `http://xagent-xai-auth.railway.internal:<PORT>`; trailing slash or an already-present `/v1` are tolerated. **Include the port Railway injects (currently 8080).** |
| `XAI_AUTH_TRIGGER_TOKEN` | — | same value as on the sidecar |
| `XAI_API_KEY` | — | metered fallback; unchanged |

Flag on but URL or token missing → warning, metered path. Flag off → exactly
today's behaviour (`https://api.x.ai/v1` + `XAI_API_KEY`). **Rollback = unset
`XAI_USE_SUBSCRIPTION`** (or set it to `0`) and redeploy the bot; nothing else
changes.

**Fallback.** When a call *via the sidecar* fails with `401`, `403`, `503` or a
connection error (sidecar down / no credential / refresh failed), the bot logs
`xai subscription fallback to XAI_API_KEY [llm_client|grok_x_search]: sidecar
HTTP 503` and repeats that one call against `https://api.x.ai/v1` with
`XAI_API_KEY`. No metered key → fails exactly as today (`XAI_API_KEY not set` /
empty post list). One fallback per call, never a loop. Timeouts and xAI's own
`4xx/5xx` relayed by the proxy are *not* fallback triggers.

`X_API_BEARER_TOKEN` (X API v2 provider), `LLM_BACKEND=openai_compat`,
`risk_manager.py`, `decision_engine.py`, `dca_sizing.py` are untouched.

### Operator rollout (after this PR is deployed)

1. Sidecar is up with the credential on `/data/grok/xai_oauth.json` and
   `XAI_AUTH_TRIGGER_TOKEN` set — `GET /health` shows `credential.present:true`,
   `proxy.enabled:true`.
2. Bot (`xagent-test` first) already has `XAI_AUTH_SIDECAR_URL` (with the
   Railway-injected port, currently `:8080`) and the same
   `XAI_AUTH_TRIGGER_TOKEN`. Set **`XAI_USE_SUBSCRIPTION=1`** on `xagent-test`
   and redeploy. (The lead does this; nothing in the repo sets the flag.)
3. Watch the bot log for `xai subscription fallback to XAI_API_KEY` and the
   sidecar `/status` → `proxy.requests` / `lastUpstreamStatus`. Steady `200`s
   and no fallback lines = the subscription carries the load.
4. Anything odd → unset `XAI_USE_SUBSCRIPTION`, redeploy.
