import { test } from "node:test";
import assert from "node:assert/strict";
import fsp from "node:fs/promises";
import path from "node:path";

import { run, USAGE } from "../src/probe.js";
import { ENV_PATH_VAR } from "../src/credentials.js";
import { credential, tmpDir, ACCESS, REFRESH, API_KEY, assertNoSecrets } from "./helpers.js";

/** Drive `run()` with captured stdio and safe defaults (no network, no pi-ai import). */
async function drive({ env = {}, deps = {}, argv = [] } = {}) {
  let stdout = "";
  let stderr = "";
  const code = await run(argv, env, {
    stdout: (s) => {
      stdout += s;
    },
    stderr: (s) => {
      stderr += s;
    },
    loadOAuth: async () => ({ refresh: async () => credential() }),
    refreshCredential: async (cred) => cred,
    probe: async () => {
      throw new Error("probe must not run in this test");
    },
    createClient: (apiKey) => ({ apiKey }),
    ...deps,
  });
  return { code, stdout, stderr, all: stdout + stderr };
}

test("run --help prints usage, exit 0, nothing else touched", async () => {
  let loaded = false;
  const r = await drive({ argv: ["--help"], deps: { loadCredential: async () => (loaded = true) } });
  assert.equal(r.code, 0);
  assert.equal(r.stdout, USAGE);
  assert.equal(loaded, false);
});

test("run: missing credential -> exit 2 with `npm run login` hint", async (t) => {
  const dir = await tmpDir(t);
  const r = await drive({ env: { [ENV_PATH_VAR]: path.join(dir, "none.json") } });
  assert.equal(r.code, 2);
  assert.match(r.stderr, /No xAI OAuth credential found .*Run `npm run login` first/);
});

test("run: credential path inside the repo is refused on the read side, exit 2, file never read", async () => {
  let loaded = false;
  const r = await drive({
    env: { [ENV_PATH_VAR]: path.join(import.meta.dirname, "..", "xai_oauth.json") },
    deps: { loadCredential: async () => (loaded = true) },
  });
  assert.equal(r.code, 2);
  assert.match(r.stderr, /Refusing to run: Refusing to store credentials inside the repository/);
  assert.equal(loaded, false);
});

test("(a) run: credential load failure whose message quotes file content never leaks the token", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  // Real file with broken JSON that contains the token, run through the real loader …
  await fsp.writeFile(target, `{"type":"oauth","access":"${ACCESS}","refresh":"${REFRESH}",`);
  const real = await drive({ env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY } });
  assert.notEqual(real.code, 0);
  assert.match(real.stderr, /Cannot load credential: .*not valid JSON/);
  assertNoSecrets(assert, real.all);

  // … and an injected loader whose error message definitely quotes the content
  // (carrying `possibleSecrets` the way the real load() does).
  const injected = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY },
    deps: {
      loadCredential: async () => {
        throw Object.assign(
          new Error(`credential is not valid JSON: Unexpected token in '{"access":"${ACCESS}","refresh":"${REFRESH}"' (key ${API_KEY})`),
          { possibleSecrets: [ACCESS, REFRESH] },
        );
      },
    },
  });
  assert.equal(injected.code, 2);
  assert.match(injected.stderr, /Cannot load credential/);
  assert.match(injected.stderr, /ACCESS…/);
  assertNoSecrets(assert, injected.all);
});

test("(b) run: ensureFresh failure echoing the refresh_token form field is redacted, exit 1", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  const r = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY },
    deps: {
      loadCredential: async () => credential({ expires: 0 }),
      refreshCredential: async () => {
        throw new Error(
          `xAI OAuth token refresh failed: xAI OAuth token refresh failed (HTTP 400): invalid_grant: refresh_token=${REFRESH} client_id=b1a0 access=${ACCESS} key=${API_KEY}`,
        );
      },
    },
  });
  assert.equal(r.code, 1);
  assert.match(r.stderr, /token refresh failed/);
  assert.match(r.stderr, /refresh_token=REFRES…/);
  assert.match(r.stderr, /run `npm run login` again/);
  assertNoSecrets(assert, r.all);
});

test("(c) run: unexpected throw at top level is reported redacted with non-zero exit", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  const boom = async () => {
    throw new Error(`boom access=${ACCESS} refresh=${REFRESH} key=${API_KEY}`);
  };

  // thrown by the probe orchestrator, after the credential is known
  const fromProbe = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY },
    deps: { loadCredential: async () => credential(), probe: boom },
  });
  assert.equal(fromProbe.code, 1);
  assert.match(fromProbe.stderr, /Probe failed: boom access=ACCESS… refresh=REFRES… key=xai-AP…/);
  assertNoSecrets(assert, fromProbe.all);

  // thrown while loading pi-ai, i.e. before `credential` was refreshed
  const fromOAuth = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY },
    deps: { loadCredential: async () => credential(), loadOAuth: boom },
  });
  assert.equal(fromOAuth.code, 1);
  assertNoSecrets(assert, fromOAuth.all);

  // thrown before any credential exists at all: XAI_API_KEY from env is still redacted
  const early = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY },
    deps: {
      loadCredential: async () => {
        throw Object.assign(new Error(`disk on fire key=${API_KEY}`), { code: "EIO" });
      },
    },
  });
  assert.notEqual(early.code, 0);
  assertNoSecrets(assert, early.all);
});

test("run: happy path with a mocked probe prints redacted Markdown + JSON, exit 0", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  const fresh = credential({ access: `${ACCESS}-fresh` });
  let probeArgs;
  const r = await drive({
    env: { [ENV_PATH_VAR]: target, XAI_API_KEY: API_KEY, XAI_PROBE_MODELS: "grok-4.6" },
    deps: {
      loadCredential: async () => credential({ expires: 0 }),
      refreshCredential: async () => fresh,
      probe: async (args) => {
        probeArgs = args;
        return {
          generatedAt: "g",
          baseUrl: "b",
          credentialPath: args.credentialPath,
          tokenExpiresAt: args.tokenExpiresAt,
          refreshed: args.refreshed,
          candidateModels: args.candidateModels,
          token: {
            modelsList: { ok: false, status: 400, statusText: "400", headers: {}, error: { name: "E", bodyExcerpt: `Incorrect API key ${fresh.access}` } },
            q1: [],
            passingModels: [],
            q2: null,
            rateLimitHeadersSeen: {},
            burst: [],
            burstStoppedOn429: false,
          },
          control: null,
        };
      },
    },
  });
  assert.equal(r.code, 0);
  assert.equal(probeArgs.tokenClient.apiKey, fresh.access);
  assert.equal(probeArgs.keyClient.apiKey, API_KEY);
  assert.deepEqual(probeArgs.candidateModels, ["grok-4.6"]);
  assert.equal(probeArgs.refreshed, true);
  assert.match(r.stderr, /Token refreshed/);
  assert.match(r.stdout, /# xAI subscription-token probe/);
  assert.match(r.stdout, /```json/);
  assert.match(r.stdout, /Incorrect API key ACCESS…/);
  assertNoSecrets(assert, r.all, [ACCESS, REFRESH, API_KEY, fresh.access]);
});
