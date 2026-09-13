import { test } from "node:test";
import assert from "node:assert/strict";
import fsp from "node:fs/promises";
import path from "node:path";

import {
  resolveCredentialPath,
  detectRepoRoot,
  isInsideRepo,
  assertOutsideRepo,
  save,
  load,
  isExpiringSoon,
  ensureFresh,
  CredentialNotFoundError,
  scrapePossibleSecrets,
  DEFAULT_REFRESH_MARGIN_MS,
  ENV_PATH_VAR,
} from "../src/credentials.js";
import { credential, tmpDir, ACCESS, REFRESH } from "./helpers.js";

const HOME = "/home/op";

test("resolveCredentialPath: env override wins and is made absolute", () => {
  assert.equal(resolveCredentialPath({ [ENV_PATH_VAR]: "/secure/xai.json" }, { homedir: HOME }), "/secure/xai.json");
  assert.equal(
    resolveCredentialPath({ [ENV_PATH_VAR]: "~/vault/xai.json", XDG_CONFIG_HOME: "/ignored" }, { homedir: HOME }),
    path.join(HOME, "vault/xai.json"),
  );
  // whitespace-only override is ignored
  assert.equal(
    resolveCredentialPath({ [ENV_PATH_VAR]: "   " }, { homedir: HOME }),
    path.join(HOME, ".config/xagent-trading-bot/xai_oauth.json"),
  );
});

test("resolveCredentialPath: XDG_CONFIG_HOME then ~/.config fallback", () => {
  assert.equal(
    resolveCredentialPath({ XDG_CONFIG_HOME: "/xdg" }, { homedir: HOME }),
    "/xdg/xagent-trading-bot/xai_oauth.json",
  );
  assert.equal(resolveCredentialPath({}, { homedir: HOME }), path.join(HOME, ".config/xagent-trading-bot/xai_oauth.json"));
});

test("detectRepoRoot finds the worktree root (a .git file or dir) above the sidecar", async () => {
  const root = detectRepoRoot();
  const stat = await fsp.stat(path.join(root, ".git"));
  assert.ok(stat.isDirectory() || stat.isFile());
  assert.ok(path.resolve(import.meta.dirname).startsWith(root));
});

test("isInsideRepo / assertOutsideRepo refuse in-repo paths, allow outside", async (t) => {
  const dir = await tmpDir(t);
  const fakeRepo = path.join(dir, "repo");
  await fsp.mkdir(path.join(fakeRepo, "services", "x"), { recursive: true });
  await fsp.writeFile(path.join(fakeRepo, ".git"), "gitdir: elsewhere\n");

  assert.equal(isInsideRepo(path.join(fakeRepo, "xai_oauth.json"), fakeRepo), true);
  assert.equal(isInsideRepo(path.join(fakeRepo, "services", "x", "does-not-exist-yet", "c.json"), fakeRepo), true);
  assert.equal(isInsideRepo(fakeRepo, fakeRepo), true);
  assert.equal(isInsideRepo(path.join(dir, "outside.json"), fakeRepo), false);
  assert.equal(isInsideRepo(path.join(dir, "repo-sibling", "c.json"), fakeRepo), false, "prefix-sharing sibling is outside");

  assert.throws(() => assertOutsideRepo(path.join(fakeRepo, "c.json"), fakeRepo), /Refusing to store credentials inside the repository/);
  assert.doesNotThrow(() => assertOutsideRepo(path.join(dir, "c.json"), fakeRepo));

  // the real repo root is refused too
  assert.equal(isInsideRepo(path.join(import.meta.dirname, "..", "xai_oauth.json")), true);
  await assert.rejects(save(credential(), path.join(import.meta.dirname, "..", "xai_oauth.json")), /Refusing to store/);
});

test("save writes 0600 file in 0700 dir atomically and load round-trips", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "cfg", "xagent-trading-bot", "xai_oauth.json");
  const cred = credential({ extra: "dropped" });

  await save(cred, target);

  const fileStat = await fsp.stat(target);
  assert.equal(fileStat.mode & 0o777, 0o600);
  const dirStat = await fsp.stat(path.dirname(target));
  assert.equal(dirStat.mode & 0o777, 0o700);

  const leftovers = (await fsp.readdir(path.dirname(target))).filter((n) => n !== "xai_oauth.json");
  assert.deepEqual(leftovers, [], "no temp files left behind");

  const loaded = await load(target);
  assert.deepEqual(loaded, { type: "oauth", access: ACCESS, refresh: REFRESH, expires: cred.expires });
  assert.equal("extra" in loaded, false, "only the four credential fields are persisted");

  // overwrite keeps mode and replaces content atomically
  await save(credential({ access: `${ACCESS}-v2` }), target);
  assert.equal((await fsp.stat(target)).mode & 0o777, 0o600);
  assert.equal((await load(target)).access, `${ACCESS}-v2`);
});

test("save cleans up its temp file when rename fails", async (t) => {
  const dir = await tmpDir(t);
  // target's parent is a *file*, so mkdir fails -> error with context, nothing left behind
  const blocker = path.join(dir, "blocker");
  await fsp.writeFile(blocker, "x");
  await assert.rejects(save(credential(), path.join(blocker, "xai_oauth.json")), /ENOTDIR|EEXIST|not a directory/i);
  assert.deepEqual(await fsp.readdir(dir), ["blocker"]);
});

test("save rejects malformed credentials before touching the filesystem", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  await assert.rejects(save({ type: "oauth", access: ACCESS }, target), /missing "refresh"/);
  await assert.rejects(save({ type: "api_key", access: ACCESS, refresh: REFRESH, expires: 1 }, target), /type must be "oauth"/);
  await assert.rejects(save(credential({ expires: "soon" }), target), /"expires" must be a finite/);
  await assert.rejects(fsp.stat(target), /ENOENT/);
});

test("load: missing file -> CredentialNotFoundError pointing at `npm run login`", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "missing.json");
  await assert.rejects(load(target), (err) => {
    assert.ok(err instanceof CredentialNotFoundError);
    assert.equal(err.code, "ENOENT");
    assert.match(err.message, /npm run login/);
    assert.ok(err.message.includes(target));
    return true;
  });
});

test("load: invalid JSON and invalid shape produce contextual errors", async (t) => {
  const dir = await tmpDir(t);
  const bad = path.join(dir, "bad.json");
  await fsp.writeFile(bad, "{not json");
  await assert.rejects(load(bad), /not valid JSON/);
  await fsp.writeFile(bad, JSON.stringify({ type: "oauth", access: "a" }));
  await assert.rejects(load(bad), /missing "refresh"/);
});

test("load: JSON-parse error never quotes file content, exposes possibleSecrets for redaction", async (t) => {
  const dir = await tmpDir(t);
  const bad = path.join(dir, "bad.json");
  // Truncated file: V8's own SyntaxError message would quote a snippet of this text.
  await fsp.writeFile(bad, `{"type":"oauth","access":"${ACCESS}","refresh":"${REFRESH}",`);
  await assert.rejects(load(bad), (err) => {
    assert.match(err.message, /not valid JSON/);
    assert.ok(!err.message.includes(ACCESS) && !err.message.includes(REFRESH), "message leaks file content");
    assert.ok(!err.message.includes(ACCESS.slice(0, 10)), "message leaks a token prefix");
    assert.deepEqual(err.possibleSecrets, [ACCESS, REFRESH]);
    assert.ok(err.cause instanceof SyntaxError);
    return true;
  });
  assert.deepEqual(scrapePossibleSecrets('{"access": "short", "refresh":"LONG_ENOUGH_VALUE"'), ["LONG_ENOUGH_VALUE"]);
  assert.deepEqual(scrapePossibleSecrets(""), []);
});

test("isExpiringSoon boundaries", () => {
  const now = 1_000_000_000_000;
  const m = DEFAULT_REFRESH_MARGIN_MS;
  assert.equal(isExpiringSoon({ expires: now + m + 1 }, now), false, "just outside margin -> fresh");
  assert.equal(isExpiringSoon({ expires: now + m }, now), true, "exactly at margin -> expiring");
  assert.equal(isExpiringSoon({ expires: now + m - 1 }, now), true);
  assert.equal(isExpiringSoon({ expires: now }, now), true);
  assert.equal(isExpiringSoon({ expires: now - 1 }, now), true, "already expired");
  assert.equal(isExpiringSoon({ expires: now + 10 }, now, 0), false, "custom margin 0");
  assert.equal(isExpiringSoon({ expires: now }, now, 0), true);
  assert.equal(isExpiringSoon({}, now), true, "missing expires is treated as expiring");
  assert.equal(isExpiringSoon(null, now), true);
});

test("ensureFresh: fresh credential -> no refresh, no save, same object", async () => {
  let refreshCalls = 0;
  let saveCalls = 0;
  const oauth = {
    refresh: async () => {
      refreshCalls += 1;
      return credential();
    },
  };
  const cred = credential();
  const result = await ensureFresh(cred, oauth, {
    credentialPath: "/tmp/never-used.json",
    persist: async () => {
      saveCalls += 1;
    },
  });
  assert.equal(result, cred);
  assert.equal(refreshCalls, 0);
  assert.equal(saveCalls, 0);
});

test("ensureFresh: expiring credential -> refresh via oauth, persist, return fresh", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  const stale = credential({ expires: Date.now() - 1 });
  const fresh = credential({ access: `${ACCESS}-fresh`, expires: Date.now() + 3_600_000 });
  const seen = [];
  const oauth = {
    refresh: async (cred, signal) => {
      seen.push({ cred, signal });
      return fresh;
    },
  };
  const result = await ensureFresh(stale, oauth, { credentialPath: target });
  assert.equal(result, fresh);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].cred, stale);
  assert.ok(seen[0].signal instanceof AbortSignal);
  assert.deepEqual(await load(target), fresh);
  assert.equal((await fsp.stat(target)).mode & 0o777, 0o600);
});

test("ensureFresh: refresh errors propagate with context and nothing is persisted", async (t) => {
  const dir = await tmpDir(t);
  const target = path.join(dir, "xai_oauth.json");
  const oauth = {
    refresh: async () => {
      throw new Error("xAI OAuth token refresh failed (HTTP 400): invalid_grant");
    },
  };
  await assert.rejects(ensureFresh(credential({ expires: 0 }), oauth, { credentialPath: target }), (err) => {
    assert.match(err.message, /xAI OAuth token refresh failed/);
    assert.match(err.message, /invalid_grant/);
    assert.ok(err.cause instanceof Error);
    return true;
  });
  await assert.rejects(fsp.stat(target), /ENOENT/);
});

test("ensureFresh: malformed refresh result is rejected", async () => {
  const oauth = { refresh: async () => ({ type: "oauth", access: "" }) };
  await assert.rejects(ensureFresh(credential({ expires: 0 }), oauth, {}), /Invalid xAI OAuth refreshed credential/);
});
