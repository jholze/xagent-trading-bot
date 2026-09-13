#!/usr/bin/env node
/**
 * `npm run probe` — answer the three open questions of issue #397 live:
 *
 *   Q1  Does the subscription OAuth access token work as a Bearer key against
 *       https://api.x.ai/v1 (GET /models, responses.create) and for which models?
 *   Q2  Does it authorise the `x_search` tool with grok_x_search.py's exact shape?
 *   Q3  What rate-limit headers come back, and does a tiny 5-call burst trip 429?
 *
 * Plus a control run with XAI_API_KEY (if set) so "works with key, 401 with
 * token" is conclusive. Output: a Markdown report and a JSON blob, both
 * token-redacted. All network calls are isolated in `callWithResponse` so the
 * orchestrator (`runProbe`) is testable with a mock client.
 */
import { fileURLToPath } from "node:url";
import OpenAI from "openai";
import { XAI_BASE_URL } from "./oauth.js";
import {
  resolveCredentialPath,
  load,
  ensureFresh,
  detectRepoRoot,
  assertOutsideRepo,
  CredentialNotFoundError,
  ENV_PATH_VAR,
} from "./credentials.js";
import { redactSecret, redactText, secretsFrom } from "./redact.js";
import {
  BURST_GAP_MS,
  BURST_SIZE,
  PROBE_MODELS_ENV,
  TINY_PROMPT,
  X_SEARCH_HANDLE,
  buildXSearchPrompt,
  buildXSearchTool,
  errorToRecord,
  modelsListToRecord,
  parseCandidateModels,
  renderRedacted,
  successToRecord,
} from "./probe_report.js";

export const USAGE = `Usage: node src/probe.js [--help]

Loads the stored xAI subscription credential (refreshing it if needed), then
probes https://api.x.ai/v1 with the OAuth access token as Bearer key:

  Q1  GET /v1/models + responses.create for each candidate model
  Q2  responses.create with grok_x_search.py's exact x_search tool shape
  Q3  rate-limit headers + one 5-call burst (1 s apart, stops at first 429)
  Control: repeats Q1/Q2 with XAI_API_KEY when that env var is set

Environment:
  ${ENV_PATH_VAR}   credential file (default ~/.config/xagent-trading-bot/xai_oauth.json)
  ${PROBE_MODELS_ENV}             comma-separated model ids (default grok-4,grok-4.3,grok-4.5,grok-4.6)
  XAI_API_KEY                  optional; enables the side-by-side control run

Prints a Markdown report followed by a JSON blob. Tokens are never printed.
Run \`npm run login\` first if no credential exists.
`;

const REQUEST_TIMEOUT_MS = 120_000;

export function makeClient(apiKey, { baseURL = XAI_BASE_URL, fetch: fetchImpl } = {}) {
  return new OpenAI({
    apiKey,
    baseURL,
    maxRetries: 0, // we want to SEE 429s, not have the SDK hide them behind retries
    timeout: REQUEST_TIMEOUT_MS,
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });
}

/**
 * Run an SDK APIPromise via `.withResponse()` and reduce it to a report record.
 * `toRecord(data, response)` shapes the success case; errors go through
 * `errorToRecord`. Never throws for HTTP/transport failures.
 */
export async function callWithResponse(makePromise, toRecord = successToRecord, now = () => Date.now()) {
  const started = now();
  try {
    const promise = makePromise();
    const { data, response } = typeof promise?.withResponse === "function" ? await promise.withResponse() : await promise;
    return { ...toRecord(data, response), durationMs: now() - started };
  } catch (err) {
    return { ...errorToRecord(err), durationMs: now() - started };
  }
}

function mergeHeadersSeen(acc, headers) {
  for (const [k, v] of Object.entries(headers ?? {})) {
    if (!acc[k]) acc[k] = [];
    if (acc[k][acc[k].length - 1] !== v) acc[k].push(v);
  }
  return acc;
}

const defaultSleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function probeModelsList(client, label) {
  const rec = await callWithResponse(() => client.models.list(), modelsListToRecord);
  return { ...rec, label, model: "(list)" };
}

async function probeTiny(client, model, label) {
  const rec = await callWithResponse(() => client.responses.create({ model, input: TINY_PROMPT }));
  return { ...rec, label, model };
}

async function probeXSearch(client, model, label, nowMs) {
  const tool = buildXSearchTool(X_SEARCH_HANDLE, nowMs);
  const prompt = buildXSearchPrompt(X_SEARCH_HANDLE, tool);
  const rec = await callWithResponse(() =>
    client.responses.create({
      model,
      input: [{ role: "user", content: prompt }],
      tools: [tool],
    }),
  );
  return { ...rec, label, model, tool };
}

/**
 * The whole probe, parameterised on clients so tests can inject mocks.
 * Returns the un-redacted report object (secrets are only in-memory; the
 * report itself contains no tokens, redaction is a belt-and-braces pass).
 */
export async function runProbe({
  tokenClient,
  keyClient = null,
  candidateModels,
  credentialPath,
  tokenExpiresAt,
  refreshed = false,
  sleep = defaultSleep,
  now = () => Date.now(),
  log = () => {},
  burstSize = BURST_SIZE,
  burstGapMs = BURST_GAP_MS,
}) {
  const startedAt = now();
  const token = {
    modelsList: null,
    q1: [],
    passingModels: [],
    q2: null,
    rateLimitHeadersSeen: {},
    burst: [],
    burstStoppedOn429: false,
  };
  const seen = token.rateLimitHeadersSeen;

  // Q1a — GET /v1/models
  log("Q1: GET /v1/models …");
  token.modelsList = await probeModelsList(tokenClient, "token:models.list");
  mergeHeadersSeen(seen, token.modelsList.headers);

  // Q1b — tiny responses.create per candidate model
  for (const model of candidateModels) {
    log(`Q1: responses.create model=${model} …`);
    const rec = await probeTiny(tokenClient, model, "token:q1");
    mergeHeadersSeen(seen, rec.headers);
    token.q1.push(rec);
    if (rec.ok) token.passingModels.push(model);
  }
  const passing = token.passingModels[0] ?? null;

  // Q2 — x_search with the exact production tool shape
  if (passing) {
    log(`Q2: x_search model=${passing} …`);
    token.q2 = await probeXSearch(tokenClient, passing, "token:q2", now());
    mergeHeadersSeen(seen, token.q2.headers);
  }

  // Q3 — small burst, stop at first 429
  if (passing) {
    for (let i = 0; i < burstSize; i += 1) {
      if (i > 0) await sleep(burstGapMs);
      log(`Q3: burst ${i + 1}/${burstSize} model=${passing} …`);
      const rec = await probeTiny(tokenClient, passing, `token:burst${i + 1}`);
      mergeHeadersSeen(seen, rec.headers);
      token.burst.push(rec);
      if (rec.status === 429) {
        token.burstStoppedOn429 = true;
        break;
      }
    }
  }

  // Control — same checks with the plain API key
  let control = null;
  if (keyClient) {
    const model = passing ?? candidateModels[0];
    log(`Control: GET /v1/models, responses.create + x_search with XAI_API_KEY model=${model} …`);
    control = { model, modelsList: null, q1: null, q2: null };
    control.modelsList = await probeModelsList(keyClient, "key:models.list");
    control.q1 = await probeTiny(keyClient, model, "key:q1");
    control.q2 = await probeXSearch(keyClient, model, "key:q2", now());
  }

  return {
    issue: 397,
    phase: 1,
    generatedAt: new Date(startedAt).toISOString(),
    durationMs: now() - startedAt,
    baseUrl: XAI_BASE_URL,
    credentialPath,
    tokenExpiresAt,
    refreshed,
    candidateModels,
    token,
    control,
    controlNote: keyClient ? "XAI_API_KEY was set; control run executed." : "XAI_API_KEY not set; control run skipped.",
  };
}

/**
 * Every secret this process has ever seen (XAI_API_KEY from env, then the
 * loaded/refreshed credential). Module-scoped so the last-resort catch in the
 * bootstrap below can redact even when `run()` died before `credential` existed.
 */
let knownSecrets = [];

function rememberSecrets(...lists) {
  knownSecrets = [...new Set([...knownSecrets, ...lists.flat().filter((s) => typeof s === "string" && s.length > 0)])];
  return knownSecrets;
}

function describeError(err) {
  return String(err?.message ?? err ?? "unknown error");
}

/**
 * CLI body, parameterised on I/O and collaborators so it is unit-testable
 * without network. Returns the process exit code; never throws (every failure
 * is reported through the redacting `stderr`).
 */
export async function run(
  argv = process.argv.slice(2),
  env = process.env,
  {
    stdout = (s) => process.stdout.write(s),
    stderr = (s) => process.stderr.write(s),
    loadOAuth = async () => (await import("./oauth.js")).getXaiOAuth(),
    loadCredential = load,
    refreshCredential = ensureFresh,
    probe = runProbe,
    createClient = makeClient,
    repoRoot = undefined,
  } = {},
) {
  // Redaction is live: `knownSecrets` grows as we learn more, and every write goes through it.
  rememberSecrets(secretsFrom(null, env));
  const out = (s) => stdout(redactText(s, knownSecrets));
  const errLine = (s) => stderr(`${redactText(s, knownSecrets)}\n`);

  try {
    if (argv.includes("--help") || argv.includes("-h")) {
      stdout(USAGE);
      return 0;
    }
    if (argv.length > 0) {
      errLine(`Unknown argument(s): ${argv.join(" ")}\n\n${USAGE}`);
      return 2;
    }

    const credentialPath = resolveCredentialPath(env);
    const root = repoRoot ?? detectRepoRoot();
    try {
      assertOutsideRepo(credentialPath, root);
    } catch (err) {
      errLine(`Refusing to run: ${describeError(err)}`);
      return 2;
    }

    let credential;
    try {
      credential = await loadCredential(credentialPath);
    } catch (err) {
      if (err instanceof CredentialNotFoundError) {
        errLine(describeError(err));
        return 2;
      }
      // load() attaches token-looking values scraped from an unparseable file; register them
      // BEFORE printing so even a message that quotes file content comes out redacted.
      rememberSecrets(Array.isArray(err?.possibleSecrets) ? err.possibleSecrets : []);
      errLine(`Cannot load credential: ${describeError(err)}`);
      return 2;
    }
    // From here on the token strings are known: register them BEFORE anything else can fail.
    rememberSecrets(secretsFrom(credential, env));

    errLine(
      `Using credential ${credentialPath} (access ${redactSecret(credential.access)}, expires ${new Date(credential.expires).toISOString()})`,
    );

    const oauth = await loadOAuth();
    const before = credential;
    try {
      credential = await refreshCredential(credential, oauth, { credentialPath, repoRoot: root });
    } catch (err) {
      // The pi-ai/token-endpoint error body may echo the refresh_token form field → redacted.
      errLine(describeError(err));
      errLine("If the refresh token was revoked, run `npm run login` again.");
      return 1;
    }
    const refreshed = credential !== before;
    if (refreshed) {
      rememberSecrets(secretsFrom(credential, env));
      errLine(`Token refreshed; new expiry ${new Date(credential.expires).toISOString()}`);
    }

    const tokenClient = createClient(credential.access);
    const keyClient = env.XAI_API_KEY ? createClient(env.XAI_API_KEY) : null;
    if (!keyClient) errLine("XAI_API_KEY not set — control run will be skipped.");

    const report = await probe({
      tokenClient,
      keyClient,
      candidateModels: parseCandidateModels(env),
      credentialPath,
      tokenExpiresAt: new Date(credential.expires).toISOString(),
      refreshed,
      log: errLine,
    });

    const { markdown, json } = renderRedacted(report, knownSecrets);
    out(`${markdown}\n---\n\n\`\`\`json\n${json}\n\`\`\`\n`);
    return 0;
  } catch (err) {
    errLine(`Probe failed: ${describeError(err)}`);
    return 1;
  }
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  return run(argv, env);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().then(
    (code) => process.exit(code),
    (err) => {
      // Last resort (run() already catches everything); still redact with whatever we know.
      process.stderr.write(`Probe failed: ${redactText(describeError(err), knownSecrets)}\n`);
      process.exit(1);
    },
  );
}
