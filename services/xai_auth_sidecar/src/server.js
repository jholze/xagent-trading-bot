#!/usr/bin/env node
/**
 * Long-running Railway entry point for the xAI subscription-auth sidecar
 * (issue #397, Phase 1b: login ON the instance, session on a volume).
 *
 * What it does
 *   - serves `GET /health` + `GET /status` (Railway healthcheck; credential
 *     present / expiry / login state — never a token),
 *   - `POST /login` (bearer `XAI_AUTH_TRIGGER_TOKEN`) starts the device-code
 *     login; URL + user code go to stdout and operator Telegram,
 *   - optionally starts that login at boot when no credential exists
 *     (`XAI_AUTH_LOGIN_ON_BOOT=1`, default off),
 *   - keeps the stored session alive by refreshing it before expiry
 *     (`XAI_AUTH_KEEPALIVE`, default on) so it survives deploys on the volume.
 *
 * What it does NOT do (yet): proxy `/v1` for the Python bot. That is Phase 2,
 * after the probe results are on the issue.
 */
import http from "node:http";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  resolveCredentialPath,
  detectRepoRoot,
  isInsideRepo,
  load,
  save,
  ensureFresh,
  isExpiringSoon,
  ENV_PATH_VAR,
} from "./credentials.js";
import { runLogin } from "./login.js";
import { redactText, secretsFrom } from "./redact.js";
import { createOperatorNotifier, telegramConfigFrom } from "./telegram.js";

export const SERVICE_NAME = "xai-auth-sidecar";
export const TRIGGER_TOKEN_VAR = "XAI_AUTH_TRIGGER_TOKEN";
export const LOGIN_ON_BOOT_VAR = "XAI_AUTH_LOGIN_ON_BOOT";
export const KEEPALIVE_VAR = "XAI_AUTH_KEEPALIVE";
export const KEEPALIVE_INTERVAL_VAR = "XAI_AUTH_KEEPALIVE_INTERVAL_MS";
export const REFRESH_MARGIN_VAR = "XAI_AUTH_REFRESH_MARGIN_MS";
export const DEFAULT_PORT = 8787;
export const DEFAULT_KEEPALIVE_INTERVAL_MS = 5 * 60_000;
/** Refresh once less than this remains (pi-ai's 5-min skew is already inside `expires`). */
export const DEFAULT_REFRESH_MARGIN_MS = 15 * 60_000;
export const MAX_BACKOFF_MS = 6 * 60 * 60_000;

export function flagOn(value, dflt = false) {
  if (value === undefined || value === null || String(value).trim() === "") return dflt;
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

function positiveInt(value, dflt) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : dflt;
}

/** Constant-time bearer comparison; `false` when the trigger token is unset (trigger disabled). */
export function triggerAuthorized(headers, env) {
  const expected = typeof env[TRIGGER_TOKEN_VAR] === "string" ? env[TRIGGER_TOKEN_VAR].trim() : "";
  if (!expected) return { ok: false, reason: "disabled" };
  const raw = String(headers?.authorization ?? "");
  const m = /^Bearer\s+(.+)$/i.exec(raw.trim());
  const presented = m ? m[1].trim() : String(headers?.["x-auth-token"] ?? "").trim();
  if (!presented) return { ok: false, reason: "missing" };
  const a = Buffer.from(presented);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return { ok: false, reason: "mismatch" };
  return { ok: true };
}

/** Token-free view of the stored credential. */
export async function credentialStatus(credentialPath, { nowMs = Date.now(), marginMs = DEFAULT_REFRESH_MARGIN_MS, loadImpl = load } = {}) {
  try {
    const cred = await loadImpl(credentialPath);
    return {
      present: true,
      expiresAt: new Date(cred.expires).toISOString(),
      expiresInSec: Math.round((cred.expires - nowMs) / 1000),
      expiringSoon: isExpiringSoon(cred, nowMs, marginMs),
    };
  } catch (err) {
    if (err && err.code === "ENOENT") return { present: false };
    return { present: false, error: `unreadable: ${String(err?.message ?? err).slice(0, 200)}` };
  }
}

/**
 * Build the sidecar core with injectable collaborators (all network/pi-ai/
 * timers can be faked in tests). `oauthFactory()` must return the pi-ai oauth
 * object lazily so tests never touch pi-ai.
 */
export function createSidecar({
  env = process.env,
  credentialPath,
  oauthFactory,
  notifyOperator = null,
  log = () => {},
  now = () => Date.now(),
  runLoginImpl = runLogin,
  loadImpl = load,
  saveImpl = save,
  ensureFreshImpl = ensureFresh,
  setTimer = setInterval,
  clearTimer = clearInterval,
  repoRoot = undefined,
} = {}) {
  if (!credentialPath) throw new Error("createSidecar: credentialPath is required");
  if (typeof oauthFactory !== "function") throw new Error("createSidecar: oauthFactory is required");

  const keepaliveIntervalMs = positiveInt(env[KEEPALIVE_INTERVAL_VAR], DEFAULT_KEEPALIVE_INTERVAL_MS);
  const refreshMarginMs = positiveInt(env[REFRESH_MARGIN_VAR], DEFAULT_REFRESH_MARGIN_MS);
  const keepaliveEnabled = flagOn(env[KEEPALIVE_VAR], true);
  const loginOnBoot = flagOn(env[LOGIN_ON_BOOT_VAR], false);

  let knownSecrets = [];
  const remember = (list) => {
    knownSecrets = [...new Set([...knownSecrets, ...list.filter((s) => typeof s === "string" && s.length > 0)])];
  };
  remember(secretsFrom(null, env));
  const redact = (s) => redactText(String(s ?? ""), knownSecrets);
  const safeLog = (m) => log(redact(m));
  const notify = async (text) => {
    if (typeof notifyOperator !== "function") return false;
    try {
      return Boolean(await notifyOperator(redact(text)));
    } catch (err) {
      safeLog(`operator notify threw: ${err?.message ?? err}`);
      return false;
    }
  };
  const persist = async (cred, p) => {
    remember(secretsFrom(cred));
    return saveImpl(cred, p, repoRoot ? { repoRoot } : {});
  };

  const login = { running: false, startedAt: null, finishedAt: null, lastResult: null, lastError: null };
  const keepalive = { enabled: keepaliveEnabled, failures: 0, backoffUntil: 0, lastRefreshAt: null, lastError: null, timer: undefined };

  async function status() {
    const nowMs = now();
    const credential = await credentialStatus(credentialPath, { nowMs, marginMs: refreshMarginMs, loadImpl });
    return {
      ok: true,
      service: SERVICE_NAME,
      credentialPath,
      credential,
      login: { running: login.running, startedAt: login.startedAt, finishedAt: login.finishedAt, lastResult: login.lastResult, lastError: login.lastError },
      keepalive: { enabled: keepalive.enabled, intervalMs: keepaliveIntervalMs, refreshMarginMs, failures: keepalive.failures, lastRefreshAt: keepalive.lastRefreshAt, lastError: keepalive.lastError },
      loginOnBoot,
      telegram: { configured: typeof notifyOperator === "function" },
      triggerEnabled: Boolean(typeof env[TRIGGER_TOKEN_VAR] === "string" && env[TRIGGER_TOKEN_VAR].trim()),
    };
  }

  let loginPromise = null;
  /** Start the device-code login in the background. Returns `{ started, running }`. */
  function startLogin({ signal } = {}) {
    if (login.running) return { started: false, running: true };
    login.running = true;
    login.startedAt = new Date(now()).toISOString();
    login.finishedAt = null;
    login.lastResult = null;
    login.lastError = null;
    safeLog("device-code login started");
    loginPromise = (async () => {
      try {
        const { credential } = await runLoginImpl({
          oauth: oauthFactory(),
          credentialPath,
          out: (s) => log(redact(s)),
          notifyOperator: typeof notifyOperator === "function" ? notify : null,
          persist,
          signal: signal ?? new AbortController().signal,
          now,
        });
        remember(secretsFrom(credential));
        login.lastResult = "ok";
        keepalive.failures = 0;
        keepalive.backoffUntil = 0;
        safeLog(`login ok, credential stored at ${credentialPath}, expires ${new Date(credential.expires).toISOString()}`);
      } catch (err) {
        login.lastResult = "error";
        login.lastError = redact(String(err?.message ?? err)).slice(0, 300);
        safeLog(`login failed: ${login.lastError}`);
      } finally {
        login.running = false;
        login.finishedAt = new Date(now()).toISOString();
      }
    })();
    return { started: true, running: true };
  }

  /** One keepalive step: refresh the stored credential if it is about to expire. */
  async function keepaliveTick() {
    if (login.running) return { skipped: "login running" };
    const nowMs = now();
    let cred;
    try {
      cred = await loadImpl(credentialPath);
    } catch (err) {
      if (err && err.code === "ENOENT") return { skipped: "no credential" };
      safeLog(`keepalive: credential unreadable: ${err?.message ?? err}`);
      return { error: "unreadable" };
    }
    remember(secretsFrom(cred));
    if (!isExpiringSoon(cred, nowMs, refreshMarginMs)) return { refreshed: false, expiresAt: new Date(cred.expires).toISOString() };
    if (nowMs < keepalive.backoffUntil) return { skipped: "backoff", until: new Date(keepalive.backoffUntil).toISOString() };
    try {
      const fresh = await ensureFreshImpl(cred, oauthFactory(), { nowMs, marginMs: refreshMarginMs, credentialPath, persist, repoRoot });
      remember(secretsFrom(fresh));
      keepalive.failures = 0;
      keepalive.backoffUntil = 0;
      keepalive.lastError = null;
      keepalive.lastRefreshAt = new Date(nowMs).toISOString();
      safeLog(`keepalive: session refreshed, expires ${new Date(fresh.expires).toISOString()}`);
      return { refreshed: true, expiresAt: new Date(fresh.expires).toISOString() };
    } catch (err) {
      keepalive.failures += 1;
      keepalive.lastError = redact(String(err?.message ?? err)).slice(0, 300);
      keepalive.backoffUntil = nowMs + Math.min(keepaliveIntervalMs * 2 ** keepalive.failures, MAX_BACKOFF_MS);
      safeLog(`keepalive: refresh failed (#${keepalive.failures}): ${keepalive.lastError}`);
      if (keepalive.failures === 1) {
        await notify(
          `⚠️ xAI SuperGrok session refresh failed: ${keepalive.lastError}\n` +
            "If it keeps failing the session is gone — start a new login with /xai_login or `railway ssh` → `npm run login`.",
        );
      }
      return { error: keepalive.lastError };
    }
  }

  function startKeepalive() {
    if (!keepalive.enabled || keepalive.timer !== undefined) return false;
    keepalive.timer = setTimer(() => {
      keepaliveTick().catch((err) => safeLog(`keepalive tick crashed: ${err?.message ?? err}`));
    }, keepaliveIntervalMs);
    if (keepalive.timer && typeof keepalive.timer.unref === "function") keepalive.timer.unref();
    return true;
  }

  function stop() {
    if (keepalive.timer !== undefined) {
      clearTimer(keepalive.timer);
      keepalive.timer = undefined;
    }
  }

  function sendJson(res, code, body) {
    const payload = redact(JSON.stringify(body));
    res.writeHead(code, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
    res.end(payload);
  }

  async function handleRequest(req, res) {
    const url = new URL(req.url ?? "/", "http://localhost");
    try {
      if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/status" || url.pathname === "/")) {
        return sendJson(res, 200, await status());
      }
      if (req.method === "POST" && url.pathname === "/login") {
        const auth = triggerAuthorized(req.headers, env);
        if (!auth.ok) {
          if (auth.reason === "disabled") return sendJson(res, 403, { ok: false, error: `trigger disabled: set ${TRIGGER_TOKEN_VAR} on the sidecar service` });
          return sendJson(res, 401, { ok: false, error: "unauthorized" });
        }
        const r = startLogin();
        return sendJson(res, r.started ? 202 : 409, { ok: r.started, ...r, telegram: typeof notifyOperator === "function" });
      }
      return sendJson(res, 404, { ok: false, error: "not found" });
    } catch (err) {
      safeLog(`request failed: ${err?.message ?? err}`);
      return sendJson(res, 500, { ok: false, error: "internal error" });
    }
  }

  async function bootLoginIfNeeded() {
    if (!loginOnBoot) return false;
    const st = await credentialStatus(credentialPath, { nowMs: now(), marginMs: refreshMarginMs, loadImpl });
    if (st.present) return false;
    safeLog(`${LOGIN_ON_BOOT_VAR}=1 and no credential at ${credentialPath} — starting device-code login`);
    return startLogin().started;
  }

  return {
    status,
    startLogin,
    keepaliveTick,
    startKeepalive,
    bootLoginIfNeeded,
    handleRequest,
    stop,
    /** test hook: await the in-flight login */
    whenLoginSettled: () => loginPromise ?? Promise.resolve(),
    get loginState() {
      return { ...login };
    },
    get keepaliveState() {
      const { timer, ...rest } = keepalive;
      return rest;
    },
  };
}

export async function main(env = process.env) {
  const log = (m) => process.stdout.write(`[${SERVICE_NAME}] ${m}\n`);
  const credentialPath = resolveCredentialPath(env);
  const repoRoot = detectRepoRoot();
  if (isInsideRepo(credentialPath, repoRoot)) {
    process.stderr.write(
      `Refusing to start: credential path ${credentialPath} is inside the repository (${repoRoot}). ` +
        `Set ${ENV_PATH_VAR} to a volume path (e.g. /data/grok/xai_oauth.json).\n`,
    );
    return 2;
  }

  const { getXaiOAuth } = await import("./oauth.js");
  let oauth;
  const oauthFactory = () => (oauth ??= getXaiOAuth());

  const notifyOperator = createOperatorNotifier(env, { log: (m) => log(m) });
  const sidecar = createSidecar({ env, credentialPath, oauthFactory, notifyOperator, log, repoRoot });

  const port = positiveInt(env.PORT, DEFAULT_PORT);
  const server = http.createServer((req, res) => {
    sidecar.handleRequest(req, res);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "0.0.0.0", resolve);
  });

  const st = await sidecar.status();
  const tg = telegramConfigFrom(env);
  log(`listening on :${port} (GET /health, GET /status, POST /login)`);
  log(`credential path: ${credentialPath}`);
  log(
    st.credential.present
      ? `credential present, expires ${st.credential.expiresAt}${st.credential.expiringSoon ? " (expiring soon — keepalive will refresh)" : ""}`
      : `no credential yet${st.credential.error ? ` (${st.credential.error})` : ""} — trigger a login via POST /login, /xai_login or \`npm run login\``,
  );
  log(tg ? `operator Telegram: chat ${tg.chatId}` : "operator Telegram: off (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)");
  log(`login trigger: ${st.triggerEnabled ? "enabled" : `disabled (set ${TRIGGER_TOKEN_VAR})`}; keepalive: ${st.keepalive.enabled ? `every ${st.keepalive.intervalMs} ms` : "off"}`);

  sidecar.startKeepalive();
  await sidecar.bootLoginIfNeeded();

  const shutdown = () => {
    log("shutting down");
    sidecar.stop();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
  return 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().then(
    (code) => {
      if (code !== 0) process.exit(code);
    },
    (err) => {
      process.stderr.write(`${SERVICE_NAME} failed to start: ${err?.message ?? err}\n`);
      process.exit(1);
    },
  );
}
