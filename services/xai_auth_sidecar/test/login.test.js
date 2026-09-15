import { test } from "node:test";
import assert from "node:assert/strict";
import fsp from "node:fs/promises";
import path from "node:path";

import { runLogin, formatDeviceCodeNotice, USAGE } from "../src/login.js";
import { load } from "../src/credentials.js";
import { redactText, secretsFrom } from "../src/redact.js";
import { credential, tmpDir, ACCESS, REFRESH, assertNoSecrets } from "./helpers.js";

const DEVICE_EVENT = {
  type: "device_code",
  userCode: "ABCD-EFGH",
  verificationUri: "https://auth.x.ai/activate?user_code=ABCD-EFGH",
  intervalSeconds: 5,
  expiresInSeconds: 600,
};

test("formatDeviceCodeNotice prints URL, code and expiry window", () => {
  const now = Date.UTC(2026, 8, 13, 0, 0, 0);
  const text = formatDeviceCodeNotice(DEVICE_EVENT, now);
  assert.match(text, /https:\/\/auth\.x\.ai\/activate\?user_code=ABCD-EFGH/);
  assert.match(text, /ABCD-EFGH/);
  assert.match(text, /valid for 10 min \(until 2026-09-13T00:10:00\.000Z\)/);
  assert.match(text, /polling every 5s/);
});

test("runLogin: shows device-code notice, dots while polling, saves credential, prints path + expiry — never a token", async (t) => {
  const dir = await tmpDir(t);
  const credentialPath = path.join(dir, "xai_oauth.json");
  const cred = credential();
  const timers = [];
  let output = "";
  const oauth = {
    login: async (interaction) => {
      assert.ok(interaction.signal instanceof AbortSignal);
      assert.equal(typeof interaction.notify, "function");
      assert.equal(typeof interaction.prompt, "function");
      interaction.notify(DEVICE_EVENT);
      // simulate two poll ticks
      for (const tm of timers) tm.fn();
      for (const tm of timers) tm.fn();
      return cred;
    },
  };
  const result = await runLogin({
    oauth,
    credentialPath,
    out: (s) => {
      output += redactText(s, secretsFrom(cred));
    },
    setTimer: (fn, ms) => {
      const handle = { fn, ms, cleared: false, unref() {} };
      timers.push(handle);
      return handle;
    },
    clearTimer: (h) => {
      h.cleared = true;
    },
    now: () => Date.UTC(2026, 8, 13),
  });

  assert.equal(result.credentialPath, credentialPath);
  assert.deepEqual(await load(credentialPath), cred);
  assert.equal((await fsp.stat(credentialPath)).mode & 0o777, 0o600);

  assert.match(output, /https:\/\/auth\.x\.ai\/activate\?user_code=ABCD-EFGH/);
  assert.match(output, /ABCD-EFGH/);
  assert.match(output, /valid for 10 min/);
  assert.match(output, /\.\./, "progress dots printed while polling");
  assert.match(output, new RegExp(`Credential saved to ${credentialPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
  assert.match(output, new RegExp(`expires .*${new Date(cred.expires).toISOString().slice(0, 16)}`));
  assertNoSecrets(assert, output);
  assert.ok(!output.includes(ACCESS.slice(0, 12)) && !output.includes(REFRESH.slice(0, 12)), "not even long token prefixes");

  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 5000);
  assert.equal(timers[0].cleared, true, "dot timer cleared after login resolves");
});

test("runLogin: raw output (before redaction) still contains no token — the tool never prints one", async (t) => {
  const dir = await tmpDir(t);
  let raw = "";
  await runLogin({
    oauth: {
      login: async (i) => {
        i.notify(DEVICE_EVENT);
        return credential();
      },
    },
    credentialPath: path.join(dir, "c.json"),
    out: (s) => {
      raw += s;
    },
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
  });
  assertNoSecrets(assert, raw);
});

test("runLogin: pi-ai denial/timeout errors propagate unchanged, nothing saved, timer cleared", async (t) => {
  const dir = await tmpDir(t);
  const credentialPath = path.join(dir, "c.json");
  let cleared = 0;
  await assert.rejects(
    runLogin({
      oauth: {
        login: async (i) => {
          i.notify(DEVICE_EVENT);
          throw new Error("xAI device authorization was denied");
        },
      },
      credentialPath,
      out: () => {},
      setTimer: () => ({ unref() {} }),
      clearTimer: () => {
        cleared += 1;
      },
    }),
    /xAI device authorization was denied/,
  );
  assert.equal(cleared, 1);
  await assert.rejects(fsp.stat(credentialPath), /ENOENT/);
});

test("runLogin: unknown notify events print only their type; prompt() rejects", async (t) => {
  const dir = await tmpDir(t);
  let out = "";
  await runLogin({
    oauth: {
      login: async (i) => {
        i.notify({ type: "info", secret: ACCESS });
        await assert.rejects(i.prompt({ type: "secret", message: "x" }), /not supported/);
        return credential();
      },
    },
    credentialPath: path.join(dir, "c.json"),
    out: (s) => {
      out += s;
    },
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
  });
  assert.match(out, /\[info\]/);
  assertNoSecrets(assert, out);
});

test("USAGE mentions the credential location rule and no browser auto-open", () => {
  assert.match(USAGE, /XAI_OAUTH_CREDENTIAL_PATH/);
  assert.match(USAGE, /xagent-trading-bot\/xai_oauth\.json/);
  assert.match(USAGE, /YOU open the URL/);
});

// ---- #397 Phase 1b: operator Telegram hook -------------------------------

test("runLogin: notifyOperator gets URL + code, then 'session stored' with expiry — never a token", async (t) => {
  const dir = await tmpDir(t);
  const credentialPath = path.join(dir, "xai_oauth.json");
  const cred = credential({ expires: Date.UTC(2026, 8, 15, 13, 0, 0) });
  const sent = [];
  await runLogin({
    oauth: {
      login: async (i) => {
        i.notify(DEVICE_EVENT);
        return cred;
      },
    },
    credentialPath,
    out: () => {},
    notifyOperator: async (text) => {
      sent.push(text);
      return true;
    },
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
    now: () => Date.UTC(2026, 8, 15, 12, 0, 0),
  });
  assert.equal(sent.length, 2);
  assert.match(sent[0], /https:\/\/auth\.x\.ai\/activate\?user_code=ABCD-EFGH/);
  assert.match(sent[0], /Code: ABCD-EFGH/);
  assert.match(sent[0], /until 2026-09-15T12:10:00\.000Z/);
  assert.match(sent[1], /session stored/);
  assert.match(sent[1], /expires: 2026-09-15T13:00:00\.000Z/);
  assert.match(sent[1], new RegExp(credentialPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  for (const s of sent) assertNoSecrets(assert, s);
});

test("runLogin: a failing/throwing notifyOperator never breaks the login", async (t) => {
  const dir = await tmpDir(t);
  const credentialPath = path.join(dir, "c.json");
  const { credential: got } = await runLogin({
    oauth: {
      login: async (i) => {
        i.notify(DEVICE_EVENT);
        return credential();
      },
    },
    credentialPath,
    out: () => {},
    notifyOperator: async () => {
      throw new Error("telegram down");
    },
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
  });
  assert.equal(got.type, "oauth");
  assert.deepEqual(await load(credentialPath), got);
});

test("runLogin: login failure is reported to the operator (message excerpt, no token) and still rethrown", async (t) => {
  const dir = await tmpDir(t);
  const sent = [];
  await assert.rejects(
    runLogin({
      oauth: {
        login: async (i) => {
          i.notify(DEVICE_EVENT);
          throw new Error("xAI device authorization was denied");
        },
      },
      credentialPath: path.join(dir, "c.json"),
      out: () => {},
      notifyOperator: async (text) => {
        sent.push(text);
        return true;
      },
      setTimer: () => ({ unref() {} }),
      clearTimer: () => {},
    }),
    /denied/,
  );
  assert.equal(sent.length, 2);
  assert.match(sent[1], /login failed: xAI device authorization was denied/);
  for (const s of sent) assertNoSecrets(assert, s);
});

test("runLogin without notifyOperator behaves exactly as before (no notifications attempted)", async (t) => {
  const dir = await tmpDir(t);
  const r = await runLogin({
    oauth: { login: async () => credential() },
    credentialPath: path.join(dir, "c.json"),
    out: () => {},
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
  });
  assert.equal(r.credentialPath, path.join(dir, "c.json"));
});

test("USAGE documents the Telegram delivery of URL + code", () => {
  assert.match(USAGE, /TELEGRAM_BOT_TOKEN \+ TELEGRAM_CHAT_ID/);
  assert.match(USAGE, /No token is ever printed or sent/);
});
