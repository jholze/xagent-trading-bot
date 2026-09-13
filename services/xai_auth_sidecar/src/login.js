#!/usr/bin/env node
/**
 * `npm run login` — run the pi-ai xAI device-code login (RFC 8628) and store
 * the resulting credential (see credentials.js for the location rule).
 *
 * Prints ONLY: verification URL, user code, expiry window, progress dots, and
 * on success the credential path + expiry time. Never prints a token and never
 * opens a browser on its own — the human operator does that.
 */
import { fileURLToPath } from "node:url";
import { resolveCredentialPath, save, isInsideRepo, detectRepoRoot, ENV_PATH_VAR } from "./credentials.js";
import { redactText, secretsFrom } from "./redact.js";

export const USAGE = `Usage: node src/login.js [--help]

Signs in to xAI with your SuperGrok / X Premium subscription using the
device-code flow shipped in @earendil-works/pi-ai. Steps:

  1. This tool prints a verification URL and a short user code.
  2. YOU open the URL in a browser, sign in to X/xAI, enter the code.
  3. The tool polls until xAI confirms, then stores the credential at
       \${${ENV_PATH_VAR}:-\${XDG_CONFIG_HOME:-~/.config}/xagent-trading-bot/xai_oauth.json}
     with mode 0600 (dir 0700). Paths inside the repository are refused.

No token is ever printed. Press Ctrl-C to cancel.
`;

/** Human-readable notice for the pi-ai `device_code` event. */
export function formatDeviceCodeNotice(event, nowMs = Date.now()) {
  const expiresIn = Number.isFinite(event.expiresInSeconds) ? event.expiresInSeconds : undefined;
  const lines = [
    "",
    "Open this URL in your browser and sign in with your X / SuperGrok account:",
    `  ${event.verificationUri}`,
    "",
    "Enter this code when asked:",
    `  ${event.userCode}`,
    "",
  ];
  if (expiresIn !== undefined) {
    const deadline = new Date(nowMs + expiresIn * 1000).toISOString();
    lines.push(`Code valid for ${Math.round(expiresIn / 60)} min (until ${deadline}).`);
  }
  lines.push(`Waiting for confirmation (polling every ${event.intervalSeconds ?? 5}s)…`);
  return lines.join("\n");
}

/**
 * Orchestrates the login against an injected oauth object.
 * Returns { credential, credentialPath }. Throws the pi-ai error on failure.
 *
 * `out` receives every string we print; callers wrap it with redaction.
 */
export async function runLogin({
  oauth,
  credentialPath,
  out = (s) => process.stdout.write(s),
  signal = new AbortController().signal,
  persist = save,
  now = () => Date.now(),
  dotIntervalMs = 5_000,
  setTimer = setInterval,
  clearTimer = clearInterval,
}) {
  let dots;
  const stopDots = () => {
    if (dots !== undefined) {
      clearTimer(dots);
      dots = undefined;
    }
  };

  const interaction = {
    signal,
    notify(event) {
      if (event && event.type === "device_code") {
        out(`${formatDeviceCodeNotice(event, now())}\n`);
        stopDots();
        const every = Math.max(1000, Math.min(dotIntervalMs, (event.intervalSeconds ?? 5) * 1000));
        dots = setTimer(() => out("."), every);
        if (dots && typeof dots.unref === "function") dots.unref();
      } else if (event && typeof event.type === "string") {
        out(`[${event.type}]\n`);
      }
    },
    async prompt() {
      throw new Error("Interactive prompts are not supported by this login tool (xAI device-code flow should not need one)");
    },
  };

  let credential;
  try {
    credential = await oauth.login(interaction);
  } finally {
    stopDots();
  }
  out("\n");
  await persist(credential, credentialPath);
  out(`Credential saved to ${credentialPath}\n`);
  out(`Access token expires (with pi-ai's 5-min skew applied): ${new Date(credential.expires).toISOString()}\n`);
  return { credential, credentialPath };
}

/** Every secret this process has seen; module-scoped so the bootstrap catch can redact too. */
let knownSecrets = [];

function rememberSecrets(list) {
  knownSecrets = [...new Set([...knownSecrets, ...list.filter((s) => typeof s === "string" && s.length > 0)])];
  return knownSecrets;
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  if (argv.includes("--help") || argv.includes("-h")) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (argv.length > 0) {
    process.stderr.write(`Unknown argument(s): ${argv.join(" ")}\n\n${USAGE}`);
    return 2;
  }

  const credentialPath = resolveCredentialPath(env);
  const repoRoot = detectRepoRoot();
  if (isInsideRepo(credentialPath, repoRoot)) {
    process.stderr.write(
      `Refusing to run: credential path ${credentialPath} is inside the repository (${repoRoot}). ` +
        `Set ${ENV_PATH_VAR} to a path outside the repo.\n`,
    );
    return 2;
  }

  const { getXaiOAuth } = await import("./oauth.js");
  const oauth = getXaiOAuth();

  const controller = new AbortController();
  const onSigint = () => controller.abort();
  process.once("SIGINT", onSigint);

  rememberSecrets(secretsFrom(null, env));
  const out = (s) => process.stdout.write(redactText(s, knownSecrets));
  try {
    process.stdout.write(`Credential will be stored at ${credentialPath}\n`);
    const { credential } = await runLogin({
      oauth,
      credentialPath,
      out,
      signal: controller.signal,
      persist: async (cred, p) => {
        // Register the tokens BEFORE save() can fail, so a save error message is redacted.
        rememberSecrets(secretsFrom(cred));
        return save(cred, p, { repoRoot });
      },
    });
    rememberSecrets(secretsFrom(credential));
    return 0;
  } catch (err) {
    process.stderr.write(`\nLogin failed: ${redactText(err?.message ?? String(err), knownSecrets)}\n`);
    return 1;
  } finally {
    process.off("SIGINT", onSigint);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().then(
    (code) => process.exit(code),
    (err) => {
      process.stderr.write(`Login failed: ${redactText(err?.message ?? String(err), knownSecrets)}\n`);
      process.exit(1);
    },
  );
}
