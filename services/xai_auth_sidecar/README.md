# xAI subscription-auth sidecar — Phase 1 spike (issue #397)

Goal of issue #397: let the bot's xAI calls (`grok_x_search.py`,
`intelligence/llm_client.py`, `services/watchlist_quality/ai_critic.py`)
authenticate with the operator's **SuperGrok / X Premium subscription** instead
of (or as a fallback to) a pay-per-use `XAI_API_KEY`.

Phase 1 is a *spike*: it reuses the device-code OAuth flow shipped in
[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi) (pinned
`0.85.1`) to obtain a subscription access token, then **measures live** what
that token is actually allowed to do against `https://api.x.ai/v1`. Nothing in
the Python bot changes in this phase.

## Operator commands

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
2. `$XDG_CONFIG_HOME/xagent-trading-bot/xai_oauth.json`
3. `~/.config/xagent-trading-bot/xai_oauth.json`

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
```

Dependencies: `@earendil-works/pi-ai@0.85.1` (device-code flow) and
`openai@6.40.0` (same version pi-ai depends on; used by the probe). No build
step, no test framework, plain ESM.

## What comes next (Phase 2, separate PR)

Once the live probe shows what the subscription token can do, Phase 2 adds a
loopback `/v1` proxy (this sidecar, long-running) that injects/refreshes the
token, plus the Python fallback wiring so `grok_x_search.py` /
`llm_client.py` can point `base_url` at it when `XAI_API_KEY` is absent or
exhausted. Model defaults (`grok-4` today) may need to move to whatever id the
probe shows the token accepts.
