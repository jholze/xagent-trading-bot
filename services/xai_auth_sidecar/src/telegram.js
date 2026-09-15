/**
 * Operator Telegram notifications for the device-code login (issue #397).
 *
 * The sidecar runs headless on Railway; the operator has no terminal attached
 * when a login starts. Verification URL + user code (and the "session stored"
 * confirmation) therefore also go to the operator chat (`TELEGRAM_CHAT_ID`)
 * via the Bot API. Only these fields are ever sent — never an access or
 * refresh token, never `TELEGRAM_BOT_TOKEN` itself.
 *
 * Everything here is fail-soft: a Telegram outage must never abort a login.
 */
import { redactText } from "./redact.js";

export const TELEGRAM_TOKEN_VAR = "TELEGRAM_BOT_TOKEN";
export const TELEGRAM_CHAT_VAR = "TELEGRAM_CHAT_ID";
export const DEFAULT_TIMEOUT_MS = 10_000;

/** `{ token, chatId }` when both env vars are set, otherwise `null`. Pure. */
export function telegramConfigFrom(env = process.env) {
  const token = typeof env[TELEGRAM_TOKEN_VAR] === "string" ? env[TELEGRAM_TOKEN_VAR].trim() : "";
  const chatId = typeof env[TELEGRAM_CHAT_VAR] === "string" ? env[TELEGRAM_CHAT_VAR].trim() : "";
  if (!token || !chatId) return null;
  return { token, chatId };
}

/** Operator-facing text for the pi-ai `device_code` event. Plain text, no tokens. */
export function formatDeviceCodeMessage(event, nowMs = Date.now()) {
  const lines = [
    "🔐 xAI SuperGrok login (device code)",
    "",
    "Open this URL and sign in with the X / SuperGrok account:",
    String(event.verificationUri ?? ""),
    "",
    `Code: ${event.userCode ?? "?"}`,
  ];
  if (Number.isFinite(event.expiresInSeconds)) {
    const deadline = new Date(nowMs + event.expiresInSeconds * 1000).toISOString();
    lines.push("", `Valid for ${Math.round(event.expiresInSeconds / 60)} min (until ${deadline}).`);
  }
  return lines.join("\n");
}

/** Operator-facing confirmation once the credential is on disk. Path + expiry only. */
export function formatSessionStoredMessage(credential, credentialPath) {
  const expires = new Date(credential.expires).toISOString();
  return [
    "✅ xAI SuperGrok session stored",
    `Path: ${credentialPath}`,
    `Access token expires: ${expires} (auto-refresh via refresh grant)`,
  ].join("\n");
}

/**
 * Send one plain-text message via the Bot API. Resolves `{ ok, status, error }`
 * and never rejects. `secrets` are redacted from the text before sending.
 */
export async function sendTelegramMessage(
  text,
  { env = process.env, fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS, secrets = [] } = {},
) {
  const cfg = telegramConfigFrom(env);
  if (!cfg) return { ok: false, status: 0, error: "telegram not configured" };
  if (typeof fetchImpl !== "function") return { ok: false, status: 0, error: "fetch unavailable" };

  const body = JSON.stringify({
    chat_id: cfg.chatId,
    text: redactText(text, [...secrets, cfg.token]),
    disable_web_page_preview: true,
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(`https://api.telegram.org/bot${cfg.token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
      signal: controller.signal,
    });
    if (!res.ok) {
      let excerpt = "";
      try {
        excerpt = (await res.text()).slice(0, 200);
      } catch {
        /* body unreadable — status is enough */
      }
      return { ok: false, status: res.status, error: redactText(`HTTP ${res.status} ${excerpt}`.trim(), [cfg.token]) };
    }
    return { ok: true, status: res.status };
  } catch (err) {
    const msg = err?.name === "AbortError" ? `timeout after ${timeoutMs}ms` : String(err?.message ?? err);
    return { ok: false, status: 0, error: redactText(msg, [cfg.token]) };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Build a `notifyOperator(text)` function for login/server. Returns `null` when
 * Telegram is not configured so callers can log "operator notify: off" once.
 * The returned function never throws; failures go to `log`.
 */
export function createOperatorNotifier(env = process.env, { fetchImpl, log = () => {}, secrets = () => [] } = {}) {
  const cfg = telegramConfigFrom(env);
  if (!cfg) return null;
  return async (text) => {
    const result = await sendTelegramMessage(text, { env, fetchImpl, secrets: secrets() });
    if (!result.ok) log(`Telegram notify failed: ${result.error}`);
    return result.ok;
  };
}
