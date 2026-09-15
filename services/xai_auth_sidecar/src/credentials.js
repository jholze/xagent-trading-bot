/**
 * Credential storage for the xAI subscription OAuth token.
 *
 * Location rule (first match wins):
 *   1. $XAI_OAUTH_CREDENTIAL_PATH
 *   2. $RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json   (Railway: the attached volume — #397)
 *   3. $XDG_CONFIG_HOME/xagent-trading-bot/xai_oauth.json
 *   4. ~/.config/xagent-trading-bot/xai_oauth.json
 *
 * Rule 2 makes `railway ssh` + `npm run login` and the long-running server
 * agree on the file without anyone exporting the path by hand.
 *
 * The file is written 0600 inside a 0700 directory, atomically (temp+rename),
 * and `save()` refuses any path that resolves inside the repo/worktree.
 * Nothing here ever logs a token.
 */
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

export const APP_DIR_NAME = "xagent-trading-bot";
export const CREDENTIAL_FILE_NAME = "xai_oauth.json";
export const ENV_PATH_VAR = "XAI_OAUTH_CREDENTIAL_PATH";
export const RAILWAY_VOLUME_VAR = "RAILWAY_VOLUME_MOUNT_PATH";

/** Extra margin on top of pi-ai's baked-in 5-minute skew before we refresh. */
export const DEFAULT_REFRESH_MARGIN_MS = 60_000;

const SIDE_CAR_ROOT = path.resolve(import.meta.dirname, "..");

function expandHome(p, homedir) {
  if (p === "~") return homedir;
  if (p.startsWith("~/")) return path.join(homedir, p.slice(2));
  return p;
}

/** Resolve the credential path from env (pure; no filesystem access). */
export function resolveCredentialPath(env = process.env, { homedir = os.homedir() } = {}) {
  const override = env[ENV_PATH_VAR];
  if (typeof override === "string" && override.trim().length > 0) {
    return path.resolve(expandHome(override.trim(), homedir));
  }
  const volume = env[RAILWAY_VOLUME_VAR];
  if (typeof volume === "string" && volume.trim().length > 0) {
    return path.join(path.resolve(expandHome(volume.trim(), homedir)), CREDENTIAL_FILE_NAME);
  }
  const xdg = env.XDG_CONFIG_HOME;
  const configHome =
    typeof xdg === "string" && xdg.trim().length > 0
      ? path.resolve(expandHome(xdg.trim(), homedir))
      : path.join(homedir, ".config");
  return path.join(configHome, APP_DIR_NAME, CREDENTIAL_FILE_NAME);
}

/**
 * Walk up from the sidecar directory until a `.git` entry (dir for a normal
 * checkout, file for a linked worktree) is found. Falls back to the sidecar
 * package root so we always refuse at least that.
 */
export function detectRepoRoot(startDir = SIDE_CAR_ROOT) {
  let dir = path.resolve(startDir);
  for (;;) {
    if (fs.existsSync(path.join(dir, ".git"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return path.resolve(startDir);
    dir = parent;
  }
}

/**
 * realpath for a path that may not exist yet: resolve the closest existing
 * ancestor (so symlinked parents such as macOS /var -> /private/var are seen
 * through) and re-append the non-existent tail.
 */
function realpathBestEffort(p) {
  const abs = path.resolve(p);
  const tail = [];
  let dir = abs;
  for (;;) {
    try {
      return path.join(fs.realpathSync(dir), ...tail.reverse());
    } catch {
      const parent = path.dirname(dir);
      if (parent === dir) return abs;
      tail.push(path.basename(dir));
      dir = parent;
    }
  }
}

/** True if `candidate` (which may not exist yet) lives under `repoRoot`. */
export function isInsideRepo(candidate, repoRoot = detectRepoRoot()) {
  const root = realpathBestEffort(repoRoot);
  const target = realpathBestEffort(candidate);
  const rel = path.relative(root, target);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

export function assertOutsideRepo(candidate, repoRoot = detectRepoRoot()) {
  if (isInsideRepo(candidate, repoRoot)) {
    throw new Error(
      `Refusing to store credentials inside the repository: ${candidate} is under ${repoRoot}. ` +
        `Set ${ENV_PATH_VAR} to a location outside the repo (default: ~/.config/${APP_DIR_NAME}/${CREDENTIAL_FILE_NAME}).`,
    );
  }
}

export function validateCredential(credential, context = "credential") {
  if (!credential || typeof credential !== "object") {
    throw new Error(`Invalid xAI OAuth ${context}: not an object`);
  }
  if (credential.type !== "oauth") {
    throw new Error(`Invalid xAI OAuth ${context}: type must be "oauth"`);
  }
  for (const field of ["access", "refresh"]) {
    if (typeof credential[field] !== "string" || credential[field].length === 0) {
      throw new Error(`Invalid xAI OAuth ${context}: missing "${field}"`);
    }
  }
  if (typeof credential.expires !== "number" || !Number.isFinite(credential.expires)) {
    throw new Error(`Invalid xAI OAuth ${context}: "expires" must be a finite epoch-ms number`);
  }
  return credential;
}

export class CredentialNotFoundError extends Error {
  constructor(credentialPath) {
    super(`No xAI OAuth credential found at ${credentialPath}. Run \`npm run login\` first.`);
    this.name = "CredentialNotFoundError";
    this.code = "ENOENT";
    this.credentialPath = credentialPath;
  }
}

/** Load and validate the stored credential. Throws CredentialNotFoundError if absent. */
export async function load(credentialPath) {
  let raw;
  try {
    raw = await fsp.readFile(credentialPath, "utf8");
  } catch (err) {
    if (err && err.code === "ENOENT") throw new CredentialNotFoundError(credentialPath);
    throw new Error(`Failed to read xAI OAuth credential at ${credentialPath}: ${err.message}`, { cause: err });
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    // Deliberately NOT embedding err.message: V8 quotes a snippet of the source text, which
    // for this file is the token itself. Report only the position, and hand callers any
    // token-looking values we can scrape so they can redact them before printing anything.
    const position = /position (\d+)/.exec(err?.message ?? "")?.[1];
    const wrapped = new Error(
      `xAI OAuth credential at ${credentialPath} is not valid JSON${position ? ` (parse error at position ${position})` : ""}`,
      { cause: err },
    );
    wrapped.possibleSecrets = scrapePossibleSecrets(raw);
    throw wrapped;
  }
  return validateCredential(parsed, `credential at ${credentialPath}`);
}

/** Best-effort: pull `"access"/"refresh": "<value>"` strings out of unparseable text. */
export function scrapePossibleSecrets(raw) {
  const out = [];
  const re = /"(?:access|refresh)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)/g;
  for (const m of String(raw ?? "").matchAll(re)) {
    if (m[1] && m[1].length >= 8) out.push(m[1]);
  }
  return out;
}

/**
 * Persist the credential: parent dir 0700, file 0600, atomic temp+rename.
 * Refuses paths inside the repo/worktree.
 */
export async function save(credential, credentialPath, { repoRoot = detectRepoRoot() } = {}) {
  validateCredential(credential);
  assertOutsideRepo(credentialPath, repoRoot);

  const dir = path.dirname(credentialPath);
  await fsp.mkdir(dir, { recursive: true, mode: 0o700 });
  await fsp.chmod(dir, 0o700);

  const tmp = path.join(dir, `.${path.basename(credentialPath)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`);
  const payload = JSON.stringify(
    { type: "oauth", access: credential.access, refresh: credential.refresh, expires: credential.expires },
    null,
    2,
  );
  try {
    await fsp.writeFile(tmp, `${payload}\n`, { mode: 0o600, flag: "wx" });
    await fsp.chmod(tmp, 0o600);
    await fsp.rename(tmp, credentialPath);
  } catch (err) {
    // Best-effort temp-file cleanup only; the real write error is rethrown (with context) on the next line.
    await fsp.rm(tmp, { force: true }).catch(() => {});
    throw new Error(`Failed to write xAI OAuth credential to ${credentialPath}: ${err.message}`, { cause: err });
  }
  return credentialPath;
}

/**
 * True when the token must be refreshed before use. pi-ai already subtracts a
 * 5-minute skew from `expires`; `marginMs` adds a little more headroom.
 * Boundary: expiring iff `expires - nowMs <= marginMs`.
 */
export function isExpiringSoon(credential, nowMs = Date.now(), marginMs = DEFAULT_REFRESH_MARGIN_MS) {
  if (!credential || typeof credential.expires !== "number" || !Number.isFinite(credential.expires)) return true;
  return credential.expires - nowMs <= marginMs;
}

/**
 * Return a usable credential: unchanged if still fresh, otherwise refreshed via
 * the pi-ai oauth object, persisted, and returned. Refresh errors propagate
 * with context.
 */
export async function ensureFresh(
  credential,
  oauth,
  {
    signal = undefined,
    nowMs = Date.now(),
    marginMs = DEFAULT_REFRESH_MARGIN_MS,
    credentialPath,
    persist = save,
    repoRoot = undefined,
  } = {},
) {
  validateCredential(credential);
  if (!isExpiringSoon(credential, nowMs, marginMs)) return credential;
  if (!oauth || typeof oauth.refresh !== "function") {
    throw new Error("Cannot refresh xAI OAuth token: oauth object has no refresh()");
  }
  let fresh;
  try {
    fresh = await oauth.refresh(credential, signal ?? new AbortController().signal);
  } catch (err) {
    throw new Error(`xAI OAuth token refresh failed: ${err?.message ?? err}`, { cause: err });
  }
  validateCredential(fresh, "refreshed credential");
  if (credentialPath) {
    await persist(fresh, credentialPath, repoRoot ? { repoRoot } : {});
  }
  return fresh;
}
