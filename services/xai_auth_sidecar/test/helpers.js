import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export const ACCESS = "ACCESS_TOKEN_SUPER_SECRET_0123456789abcdef";
export const REFRESH = "REFRESH_TOKEN_SUPER_SECRET_fedcba9876543210";
export const API_KEY = "xai-API_KEY_SUPER_SECRET_zzzzzzzzzzzz";

export function credential(overrides = {}) {
  return { type: "oauth", access: ACCESS, refresh: REFRESH, expires: Date.now() + 3_600_000, ...overrides };
}

/** Temp dir outside the repo; removed in the returned cleanup. */
export async function tmpDir(t) {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), "xai-sidecar-test-"));
  t.after(async () => {
    await fsp.rm(dir, { recursive: true, force: true });
  });
  return dir;
}

export function assertNoSecrets(assert, text, secrets = [ACCESS, REFRESH, API_KEY]) {
  for (const s of secrets) {
    assert.ok(!text.includes(s), `secret leaked into output: ${s.slice(0, 6)}…`);
  }
}
