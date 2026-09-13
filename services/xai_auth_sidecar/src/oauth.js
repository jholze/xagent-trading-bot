/**
 * Single place that knows how to obtain the pi-ai xAI OAuth object.
 *
 * `dist/auth/oauth/xai.js` is not in the package `exports` map, but
 * `@earendil-works/pi-ai/providers/xai` is, and `xaiProvider().auth.oauth`
 * is a lazy wrapper (see pi-ai `auth/helpers.js#lazyOAuth`) that dynamically
 * imports the device-code flow on first `login`/`refresh`/`toAuth` call.
 *
 * Shape (pi-ai 0.85.1):
 *   login(interaction)             -> Promise<{type:"oauth", access, refresh, expires}>
 *   refresh(credential, signal)    -> Promise<credential>
 *   toAuth(credential)             -> Promise<{ apiKey: credential.access }>
 */
import { xaiProvider } from "@earendil-works/pi-ai/providers/xai";

export const XAI_BASE_URL = "https://api.x.ai/v1";

export function getXaiOAuth() {
  const oauth = xaiProvider().auth?.oauth;
  if (!oauth || typeof oauth.login !== "function" || typeof oauth.refresh !== "function") {
    throw new Error(
      "pi-ai xaiProvider().auth.oauth does not expose login/refresh — unexpected @earendil-works/pi-ai version?",
    );
  }
  return oauth;
}
