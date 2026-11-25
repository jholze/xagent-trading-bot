/**
 * Where a SuperGrok / X Premium OAuth access token is actually accepted.
 *
 * `https://api.x.ai` is the metered developer API. A subscription bearer presented
 * there is not on that team's model catalog, so both `POST /v1/chat/completions`
 * and `POST /v1/responses` come back HTTP 404 `not-found` with a string `error`
 * field ("The model … does not exist or your team does not have access to it").
 * That is the same status for either path, which is why switching the bot from
 * chat completions to the Responses API did not change the failures.
 *
 * The Grok CLI sends the same OAuth client (`b1a00492-…` in pi-ai) to
 * `https://cli-chat-proxy.grok.com` and identifies itself with the headers below.
 * The proxy returns 401 without a bearer and 426 when `x-grok-client-version` is
 * missing or below its floor. `@xai-official/grok@1.0.77` (2026-11-05) is above
 * the 1.0.13 floor observed in 2026-11. Override with `XAI_AUTH_GROK_CLIENT_VERSION`
 * if the floor moves; the access token is still only sent to this origin.
 */
export const XAI_DEVELOPER_ORIGIN = "https://api.x.ai";
export const XAI_SUBSCRIPTION_ORIGIN = "https://cli-chat-proxy.grok.com";
export const XAI_DEVELOPER_BASE_URL = `${XAI_DEVELOPER_ORIGIN}/v1`;
export const XAI_SUBSCRIPTION_BASE_URL = `${XAI_SUBSCRIPTION_ORIGIN}/v1`;

/** Published `@xai-official/grok` version that clears the proxy's client-version floor. */
export const DEFAULT_GROK_CLIENT_VERSION = "1.0.77";
export const GROK_CLIENT_VERSION_VAR = "XAI_AUTH_GROK_CLIENT_VERSION";

export function grokClientVersion(env = process.env) {
  const raw = typeof env?.[GROK_CLIENT_VERSION_VAR] === "string" ? env[GROK_CLIENT_VERSION_VAR].trim() : "";
  return raw || DEFAULT_GROK_CLIENT_VERSION;
}

/** Model id from a JSON request body, or "" when there is none / it is not header-safe. */
export function modelFromBody(body) {
  if (body == null) return "";
  let text = "";
  if (typeof body === "string") text = body;
  else if (Buffer.isBuffer(body) || body instanceof Uint8Array) text = Buffer.from(body).toString("utf8");
  else return "";
  if (!text.trim()) return "";
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return "";
  }
  const model = parsed && typeof parsed === "object" ? parsed.model : "";
  if (typeof model !== "string") return "";
  const trimmed = model.trim();
  if (!trimmed || trimmed.length > 128 || /[\r\n]/.test(trimmed)) return "";
  return trimmed;
}

/**
 * Identity headers for the subscription proxy. `model` sets `x-grok-model-override`
 * (the proxy routes on that header). Never includes the access token.
 */
export function subscriptionProxyHeaders(env = process.env, model = "") {
  const headers = {
    "x-xai-token-auth": "xai-grok-cli",
    "x-grok-client-version": grokClientVersion(env),
    "x-grok-client-identifier": "grok-shell",
    "x-grok-client-mode": "headless",
    "x-authenticateresponse": "authenticate-response",
  };
  if (model) headers["x-grok-model-override"] = model;
  return headers;
}
