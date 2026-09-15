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
before it expires, so it survives deploys. The Python bot still uses
`XAI_API_KEY`; nothing points `base_url` at this sidecar yet (Phase 2, after
the probe results are on the issue).

## Running on Railway (`xagent-xai-auth`)

Same monorepo image and `scripts/railway_start.sh` selector as the other
sidecars, but Node 22 is baked into the image **only** for this service.

### Contract (all three are required)

| Piece | Setting | Why |
|---|---|---|
| Service | `xagent-xai-auth` from this repo, branch `staging` | selector in `scripts/railway_start.sh` matches `RAILWAY_SERVICE_NAME=xagent-xai-auth` **or** `RUN_XAI_AUTH=1` |
| Variable | `RUN_XAI_AUTH=1` | read as a Docker **build ARG** → Dockerfile copies Node 22 + npm from the `desk` stage and runs `npm ci --omit=dev` in this directory. Every other service leaves it unset and stays Node-free. `start.sh` fails fast with a hint if Node is missing. |
| **Volume** | mount at **`/data/grok`** | the credential is written to `$RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json` (mode `0600`, dir `0700`). Without a volume the session dies on every deploy — `start.sh` warns but still starts. |

Variables on the `xagent-xai-auth` service:

| Variable | Default | Purpose |
|---|---|---|
| `RUN_XAI_AUTH` | — | `1`: build Node in + select this entry point (see above) |
| `XAI_OAUTH_CREDENTIAL_PATH` | `$RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json` | where the `{type:"oauth",access,refresh,expires}` file lives; paths inside the repo (`/app/...`) are refused |
| `GROK_HOME` | `$RAILWAY_VOLUME_MOUNT_PATH/grok-cli` | only relevant if you log in with the Grok CLI (`grok login --device-auth`) over `railway ssh` — keeps its session on the volume too |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | operator chat that receives the verification URL + code, "session stored, expires …", and refresh failures. Same values as the bot service. |
| `XAI_AUTH_TRIGGER_TOKEN` | — (trigger disabled) | shared secret for `POST /login`; set the **same** value on the bot service to enable `/xai_login` |
| `XAI_AUTH_LOGIN_ON_BOOT` | `0` | `1`: start a device-code login automatically when the service boots without a credential (each boot without a login = one URL+code message) |
| `XAI_AUTH_KEEPALIVE` | `1` | refresh the stored session before it expires (`0` to disable) |
| `XAI_AUTH_KEEPALIVE_INTERVAL_MS` | `300000` | keepalive check interval |
| `XAI_AUTH_REFRESH_MARGIN_MS` | `900000` | refresh when less than this remains; on failure it backs off (`interval·2ⁿ`, max 6 h) and notifies Telegram once |
| `PORT` | `8787` | HTTP port (Railway sets it) |
| `XAI_API_KEY` | — | **not needed here**; stays the bot's fallback and is untouched by this service |

HTTP surface (all JSON, never a token):

- `GET /health`, `GET /status` — `credential.{present,expiresAt,expiresInSec,expiringSoon}`, `login.{running,lastResult,lastError}`, `keepalive.*`, `triggerEnabled`, `telegram.configured`
- `POST /login` with `Authorization: Bearer $XAI_AUTH_TRIGGER_TOKEN` — `202 {started:true}`, `409` while a login runs, `403` when the trigger token is unset on the sidecar, `401` on mismatch

### How jholze logs in

**A. From Telegram (operator only, `/xai_login`).** Requires `XAI_AUTH_SIDECAR_URL`
(e.g. `http://xagent-xai-auth.railway.internal:8787`) and `XAI_AUTH_TRIGGER_TOKEN`
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
`/health`, `/status`, `POST /login`, keepalive, login-on-boot), `src/telegram.js`
(operator notifications, fail-soft), `src/credentials.js` (location rule, 0600,
refuse-in-repo, refresh), `src/probe.js` + `src/probe_report.js` (Q1–Q3),
`start.sh` (Railway entry called by `scripts/railway_start.sh`).

Dependencies: `@earendil-works/pi-ai@0.85.1` (device-code flow) and
`openai@6.40.0` (same version pi-ai depends on; used by the probe). No build
step, no test framework, plain ESM.

## What comes next (Phase 2, separate PR)

Once the live probe (run via `railway ssh` against the volume session, output
pasted into #397) shows what the subscription token can do, Phase 2 adds a
private-network `/v1` proxy to this server that injects the kept-alive token,
plus the Python fallback wiring so `grok_x_search.py` / `llm_client.py` can
point `base_url` at it when `XAI_API_KEY` is absent or exhausted. Model
defaults (`grok-4` today) may need to move to whatever id the probe shows the
token accepts. `XAI_API_KEY` and `X_API_BEARER_TOKEN` stay as they are.
