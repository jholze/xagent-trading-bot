/**
 * Token redaction helpers. Nothing in this sidecar may print a full access or
 * refresh token; everything that goes to stdout passes through `redactText`.
 */

const VISIBLE_PREFIX = 6;

/** "abcdef…" for any secret; short/empty values become "…". */
export function redactSecret(secret) {
  if (typeof secret !== "string" || secret.length === 0) return "…";
  if (secret.length <= VISIBLE_PREFIX) return "…";
  return `${secret.slice(0, VISIBLE_PREFIX)}…`;
}

/**
 * Replace every occurrence of each secret inside `text` with its redacted
 * form. Secrets are processed longest-first so a short secret that happens to
 * be a substring of a longer one cannot leave fragments behind.
 */
export function redactText(text, secrets) {
  let out = String(text ?? "");
  const list = [...new Set((secrets ?? []).filter((s) => typeof s === "string" && s.length >= 8))].sort(
    (a, b) => b.length - a.length,
  );
  for (const secret of list) {
    out = out.split(secret).join(redactSecret(secret));
  }
  return out;
}

/** Collect the secret strings from a credential + env so they can be redacted. */
export function secretsFrom(credential, env = {}) {
  const out = [];
  if (credential) {
    if (typeof credential.access === "string") out.push(credential.access);
    if (typeof credential.refresh === "string") out.push(credential.refresh);
  }
  if (typeof env.XAI_API_KEY === "string" && env.XAI_API_KEY) out.push(env.XAI_API_KEY);
  return out;
}
