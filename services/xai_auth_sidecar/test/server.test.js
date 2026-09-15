import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import path from "node:path";

import {
  createSidecar,
  credentialStatus,
  triggerAuthorized,
  flagOn,
  TRIGGER_TOKEN_VAR,
  LOGIN_ON_BOOT_VAR,
  KEEPALIVE_VAR,
  DEFAULT_REFRESH_MARGIN_MS,
  XAI_UPSTREAM_ORIGIN,
} from "../src/server.js";
import { save, load } from "../src/credentials.js";
import { credential, tmpDir, ACCESS, REFRESH, assertNoSecrets } from "./helpers.js";

const TRIGGER = "trigger-secret-0123456789";
const DEVICE_EVENT = {
  type: "device_code",
  userCode: "ABCD-EFGH",
  verificationUri: "https://auth.x.ai/activate?user_code=ABCD-EFGH",
  intervalSeconds: 5,
  expiresInSeconds: 600,
};

/** oauth fake whose login() emits a device code and resolves after `release()` is called. */
function gatedOauth(result = credential()) {
  let release;
  const gate = new Promise((r) => (release = r));
  const oauth = {
    login: async (i) => {
      i.notify(DEVICE_EVENT);
      await gate;
      if (result instanceof Error) throw result;
      return result;
    },
    refresh: async () => credential({ access: "NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz", expires: Date.now() + 3_600_000 }),
  };
  return { oauth, release: () => release() };
}

function makeSidecar(t, dir, { env = {}, oauth, notify, ...rest } = {}) {
  const logs = [];
  const sent = [];
  const sidecar = createSidecar({
    env: { [TRIGGER_TOKEN_VAR]: TRIGGER, ...env },
    credentialPath: path.join(dir, "xai_oauth.json"),
    oauthFactory: () => oauth,
    notifyOperator:
      notify === null
        ? null
        : async (text) => {
            sent.push(text);
            return true;
          },
    log: (m) => logs.push(m),
    setTimer: () => ({ unref() {} }),
    clearTimer: () => {},
    ...rest,
  });
  t.after(() => sidecar.stop());
  return { sidecar, logs, sent };
}

/** Run the sidecar behind a real loopback http server and return a tiny client. */
async function serve(t, sidecar) {
  const server = http.createServer((req, res) => sidecar.handleRequest(req, res));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const base = `http://127.0.0.1:${server.address().port}`;
  return async (method, p, headers = {}, body = undefined) => {
    const res = await fetch(base + p, { method, headers, body });
    const text = await res.text();
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
    return { status: res.status, body: parsed, text, headers: res.headers };
  };
}

/** Recording upstream fake for the /v1 proxy. */
function fakeUpstream(respond = () => new Response(JSON.stringify({ id: "resp_1", ok: true }), { status: 200, headers: { "content-type": "application/json" } })) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    const r = respond(String(url), init);
    if (r instanceof Error) throw r;
    return r;
  };
  return { calls, fetchImpl };
}

test("flagOn: defaults and truthy spellings", () => {
  assert.equal(flagOn(undefined, false), false);
  assert.equal(flagOn(undefined, true), true);
  assert.equal(flagOn("", true), true);
  for (const v of ["1", "true", "YES", " on "]) assert.equal(flagOn(v), true);
  for (const v of ["0", "false", "no", "off", "banana"]) assert.equal(flagOn(v, true), false);
});

test("triggerAuthorized: disabled when unset, bearer + x-auth-token accepted, mismatch rejected", () => {
  assert.deepEqual(triggerAuthorized({ authorization: `Bearer ${TRIGGER}` }, {}), { ok: false, reason: "disabled" });
  const env = { [TRIGGER_TOKEN_VAR]: TRIGGER };
  assert.deepEqual(triggerAuthorized({}, env), { ok: false, reason: "missing" });
  assert.deepEqual(triggerAuthorized({ authorization: "Bearer nope" }, env), { ok: false, reason: "mismatch" });
  assert.deepEqual(triggerAuthorized({ authorization: `bearer ${TRIGGER}` }, env), { ok: true });
  assert.deepEqual(triggerAuthorized({ "x-auth-token": TRIGGER }, env), { ok: true });
});

test("credentialStatus: absent, present (with expiry math), unreadable — never a token", async (t) => {
  const dir = await tmpDir(t);
  const p = path.join(dir, "c.json");
  assert.deepEqual(await credentialStatus(p), { present: false });
  const now = Date.UTC(2026, 8, 15, 12, 0, 0);
  await save(credential({ expires: now + 3_600_000 }), p);
  const st = await credentialStatus(p, { nowMs: now });
  assert.equal(st.present, true);
  assert.equal(st.expiresAt, "2026-09-15T13:00:00.000Z");
  assert.equal(st.expiresInSec, 3600);
  assert.equal(st.expiringSoon, false);
  const soon = await credentialStatus(p, { nowMs: now + 3_600_000 - DEFAULT_REFRESH_MARGIN_MS });
  assert.equal(soon.expiringSoon, true);
  assertNoSecrets(assert, JSON.stringify(st));

  const { writeFile } = await import("node:fs/promises");
  await writeFile(p, `{"type":"oauth","access":"${ACCESS}"`, "utf8");
  const bad = await credentialStatus(p);
  assert.equal(bad.present, false);
  assert.match(bad.error, /unreadable/);
  assertNoSecrets(assert, JSON.stringify(bad));
});

test("HTTP: /health and /status report token-free state; unknown route 404", async (t) => {
  const dir = await tmpDir(t);
  const { oauth } = gatedOauth();
  const { sidecar } = makeSidecar(t, dir, { oauth });
  const call = await serve(t, sidecar);

  const health = await call("GET", "/health");
  assert.equal(health.status, 200);
  assert.equal(health.body.ok, true);
  assert.equal(health.body.service, "xai-auth-sidecar");
  assert.equal(health.body.credential.present, false);
  assert.equal(health.body.login.running, false);
  assert.equal(health.body.triggerEnabled, true);
  assert.equal(health.body.telegram.configured, true);
  assert.equal(health.body.keepalive.enabled, true, "keepalive defaults on");
  assert.equal(health.body.loginOnBoot, false, "login-on-boot defaults off");

  await save(credential(), path.join(dir, "xai_oauth.json"));
  const status = await call("GET", "/status");
  assert.equal(status.body.credential.present, true);
  assertNoSecrets(assert, status.text);

  const nf = await call("GET", "/nope");
  assert.equal(nf.status, 404);
});

test("HTTP: POST /login — 403 when trigger unset, 401 on bad token, 202 start, 409 while running, then stored", async (t) => {
  const dir = await tmpDir(t);
  const cred = credential({ expires: Date.UTC(2026, 8, 15, 13, 0, 0) });
  const { oauth, release } = gatedOauth(cred);

  const disabled = makeSidecar(t, dir, { oauth, env: { [TRIGGER_TOKEN_VAR]: "" } });
  const callDisabled = await serve(t, disabled.sidecar);
  const r403 = await callDisabled("POST", "/login", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(r403.status, 403);
  assert.match(r403.body.error, /trigger disabled/);

  const { sidecar, sent, logs } = makeSidecar(t, dir, { oauth });
  const call = await serve(t, sidecar);
  assert.equal((await call("POST", "/login")).status, 401);
  assert.equal((await call("POST", "/login", { authorization: "Bearer wrong" })).status, 401);

  const started = await call("POST", "/login", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(started.status, 202);
  assert.deepEqual(started.body, { ok: true, started: true, running: true, telegram: true });

  const again = await call("POST", "/login", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(again.status, 409);
  assert.equal(again.body.running, true);
  assert.equal((await call("GET", "/status")).body.login.running, true);

  release();
  await sidecar.whenLoginSettled();

  const after = await call("GET", "/status");
  assert.equal(after.body.login.running, false);
  assert.equal(after.body.login.lastResult, "ok");
  assert.equal(after.body.credential.present, true);
  assert.equal(after.body.credential.expiresAt, "2026-09-15T13:00:00.000Z");
  assert.deepEqual(await load(path.join(dir, "xai_oauth.json")), cred);

  assert.equal(sent.length, 2, "device code + session stored went to the operator");
  assert.match(sent[0], /Code: ABCD-EFGH/);
  assert.match(sent[1], /session stored/);
  for (const s of [...sent, ...logs, after.text]) assertNoSecrets(assert, s);
  assert.ok(logs.some((l) => /login ok/.test(l)));
});

test("startLogin: failure is recorded (redacted), operator told, no credential written", async (t) => {
  const dir = await tmpDir(t);
  const { oauth, release } = gatedOauth(new Error(`denied for ${ACCESS}`));
  const { sidecar, sent, logs } = makeSidecar(t, dir, { oauth });
  // make the token "known" so the redaction path is exercised
  sidecar.startLogin();
  release();
  await sidecar.whenLoginSettled();
  const st = await sidecar.status();
  assert.equal(st.login.running, false);
  assert.equal(st.login.lastResult, "error");
  assert.match(st.login.lastError, /denied/);
  assert.equal(st.credential.present, false);
  assert.ok(sent.some((s) => /login failed/.test(s)));
  assert.ok(logs.some((l) => /login failed/.test(l)));
});

test("bootLoginIfNeeded: only with XAI_AUTH_LOGIN_ON_BOOT=1 and only when no credential exists", async (t) => {
  const dir = await tmpDir(t);
  const { oauth, release } = gatedOauth();

  const off = makeSidecar(t, dir, { oauth });
  assert.equal(await off.sidecar.bootLoginIfNeeded(), false);
  assert.equal(off.sidecar.loginState.running, false);

  const on = makeSidecar(t, dir, { oauth, env: { [LOGIN_ON_BOOT_VAR]: "1" } });
  assert.equal(await on.sidecar.bootLoginIfNeeded(), true);
  assert.equal(on.sidecar.loginState.running, true);
  release();
  await on.sidecar.whenLoginSettled();

  const already = makeSidecar(t, dir, { oauth, env: { [LOGIN_ON_BOOT_VAR]: "1" } });
  assert.equal(await already.sidecar.bootLoginIfNeeded(), false, "credential now on disk → no login");
});

test("keepaliveTick: no credential → skip; fresh → no refresh; expiring → refresh + persist; failure → backoff + one notify", async (t) => {
  const dir = await tmpDir(t);
  const p = path.join(dir, "xai_oauth.json");
  let nowMs = Date.UTC(2026, 8, 15, 12, 0, 0);
  let refreshCalls = 0;
  let failRefresh = false;
  const oauth = {
    refresh: async (cred) => {
      refreshCalls += 1;
      if (failRefresh) throw new Error(`invalid_grant ${cred.refresh}`);
      return credential({ access: "NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz", refresh: "NEW_REFRESH_TOKEN_SUPER_SECRET_yyyyyyy", expires: nowMs + 3_600_000 });
    },
  };
  const { sidecar, sent, logs } = makeSidecar(t, dir, { oauth, now: () => nowMs, env: { XAI_AUTH_KEEPALIVE_INTERVAL_MS: "60000" } });

  assert.deepEqual(await sidecar.keepaliveTick(), { skipped: "no credential" });

  await save(credential({ expires: nowMs + 3_600_000 }), p);
  const fresh = await sidecar.keepaliveTick();
  assert.equal(fresh.refreshed, false);
  assert.equal(refreshCalls, 0);

  nowMs += 3_600_000 - DEFAULT_REFRESH_MARGIN_MS + 1000; // inside the margin
  const refreshed = await sidecar.keepaliveTick();
  assert.equal(refreshed.refreshed, true);
  assert.equal(refreshCalls, 1);
  const onDisk = await load(p);
  assert.equal(onDisk.access, "NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz");
  assert.equal(sidecar.keepaliveState.failures, 0);
  assert.equal(sidecar.keepaliveState.lastRefreshAt, new Date(nowMs).toISOString());

  // Now make it expire again and let refresh fail: first failure notifies, second is in backoff.
  failRefresh = true;
  nowMs = onDisk.expires - 1000;
  const failed = await sidecar.keepaliveTick();
  assert.match(failed.error, /invalid_grant/);
  assert.equal(sidecar.keepaliveState.failures, 1);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /refresh failed/);
  assert.match(sent[0], /\/xai_login/);
  nowMs += 1000;
  const backoff = await sidecar.keepaliveTick();
  assert.equal(backoff.skipped, "backoff");
  assert.equal(refreshCalls, 2);
  assert.equal(sent.length, 1, "no second notification during backoff");
  // Past the backoff window (interval * 2^1 = 120 s) it retries.
  nowMs += 130_000;
  await sidecar.keepaliveTick();
  assert.equal(refreshCalls, 3);
  assert.equal(sidecar.keepaliveState.failures, 2);
  assert.equal(sent.length, 1, "still only the first failure is announced");

  for (const s of [...sent, ...logs, JSON.stringify(await sidecar.status())]) {
    assertNoSecrets(assert, s);
    assert.ok(!s.includes("NEW_REFRESH_TOKEN_SUPER_SECRET_yyyyyyy"), "rotated refresh token never leaks");
    assert.ok(!s.includes("NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz"), "rotated access token never leaks");
    assert.ok(!s.includes(REFRESH), "old refresh token never leaks");
  }
});

test("keepalive: skipped while a login is running; startKeepalive honours XAI_AUTH_KEEPALIVE=0", async (t) => {
  const dir = await tmpDir(t);
  const { oauth, release } = gatedOauth();
  const { sidecar } = makeSidecar(t, dir, { oauth });
  sidecar.startLogin();
  assert.deepEqual(await sidecar.keepaliveTick(), { skipped: "login running" });
  release();
  await sidecar.whenLoginSettled();

  const timers = [];
  const off = createSidecar({
    env: { [KEEPALIVE_VAR]: "0" },
    credentialPath: path.join(dir, "x.json"),
    oauthFactory: () => oauth,
    setTimer: (fn, ms) => {
      timers.push(ms);
      return { unref() {} };
    },
    clearTimer: () => {},
  });
  assert.equal(off.startKeepalive(), false);
  assert.equal(timers.length, 0);
  const on = createSidecar({
    env: { XAI_AUTH_KEEPALIVE_INTERVAL_MS: "1234" },
    credentialPath: path.join(dir, "x.json"),
    oauthFactory: () => oauth,
    setTimer: (fn, ms) => {
      timers.push(ms);
      return { unref() {} };
    },
    clearTimer: () => {},
  });
  assert.equal(on.startKeepalive(), true);
  assert.equal(on.startKeepalive(), false, "idempotent");
  assert.deepEqual(timers, [1234]);
  on.stop();
});

// ---------------------------------------------------------------------------
// Phase 2: /v1 proxy
// ---------------------------------------------------------------------------

test("proxy: 403 fail-closed when the trigger token is unset — nothing is forwarded", async (t) => {
  const dir = await tmpDir(t);
  await save(credential(), path.join(dir, "xai_oauth.json"));
  const { oauth } = gatedOauth();
  const { calls, fetchImpl } = fakeUpstream();
  const { sidecar, logs } = makeSidecar(t, dir, { oauth, fetchImpl, env: { [TRIGGER_TOKEN_VAR]: "" } });
  const call = await serve(t, sidecar);

  const r = await call("POST", "/v1/chat/completions", { authorization: `Bearer ${TRIGGER}`, "content-type": "application/json" }, "{}");
  assert.equal(r.status, 403);
  assert.equal(r.body.error, "trigger_disabled");
  assert.equal(calls.length, 0, "no upstream call without a configured trigger");
  assert.equal((await call("GET", "/status")).body.proxy.enabled, false);
  for (const l of logs) assertNoSecrets(assert, l);
});

test("proxy: 401 on missing/bad token; 405 on other methods; health/status/login are not proxied", async (t) => {
  const dir = await tmpDir(t);
  await save(credential(), path.join(dir, "xai_oauth.json"));
  const { oauth } = gatedOauth();
  const { calls, fetchImpl } = fakeUpstream();
  const { sidecar } = makeSidecar(t, dir, { oauth, fetchImpl });
  const call = await serve(t, sidecar);

  assert.equal((await call("POST", "/v1/chat/completions", {}, "{}")).status, 401);
  assert.equal((await call("GET", "/v1/models", { authorization: "Bearer wrong" })).status, 401);
  assert.equal((await call("GET", "/v1/models", { "x-auth-token": "wrong" })).status, 401);
  assert.equal((await call("PUT", "/v1/models", { authorization: `Bearer ${TRIGGER}` }, "{}")).status, 405);
  assert.equal(calls.length, 0);

  const health = await call("GET", "/health");
  assert.equal(health.status, 200);
  assert.equal(health.body.service, "xai-auth-sidecar");
  assert.deepEqual(
    { enabled: health.body.proxy.enabled, upstream: health.body.proxy.upstream, requests: health.body.proxy.requests, rejected: health.body.proxy.rejected },
    { enabled: true, upstream: XAI_UPSTREAM_ORIGIN, requests: 4, rejected: 4 },
  );
  assert.equal((await call("GET", "/v1status")).status, 404, "prefix must match a path segment");
  assert.equal(calls.length, 0, "health/status never reach the upstream");
});

test("proxy: 503 no_credential when the volume file is absent or unreadable", async (t) => {
  const dir = await tmpDir(t);
  const { oauth } = gatedOauth();
  const { calls, fetchImpl } = fakeUpstream();
  const { sidecar, logs } = makeSidecar(t, dir, { oauth, fetchImpl });
  const call = await serve(t, sidecar);

  const absent = await call("POST", "/v1/chat/completions", { authorization: `Bearer ${TRIGGER}` }, "{}");
  assert.equal(absent.status, 503);
  assert.deepEqual(absent.body, { ok: false, error: "no_credential" });

  const { writeFile } = await import("node:fs/promises");
  await writeFile(path.join(dir, "xai_oauth.json"), `{"type":"oauth","access":"${ACCESS}"`, "utf8");
  const broken = await call("GET", "/v1/models", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(broken.status, 503);
  assert.equal(broken.body.error, "no_credential");

  assert.equal(calls.length, 0, "never forwards without a credential");
  assert.equal((await call("GET", "/status")).body.proxy.noCredential, 2);
  for (const l of [...logs, absent.text, broken.text]) assertNoSecrets(assert, l);
});

test("proxy: happy path injects the stored access token, strips hop-by-hop + client auth, relays status/body, leaks nothing", async (t) => {
  const dir = await tmpDir(t);
  await save(credential({ expires: Date.now() + 3_600_000 }), path.join(dir, "xai_oauth.json"));
  const { oauth } = gatedOauth();
  const upstreamBody = JSON.stringify({ id: "resp_42", output: [{ type: "message", content: [{ text: "hi" }] }] });
  const { calls, fetchImpl } = fakeUpstream(
    () =>
      new Response(upstreamBody, {
        status: 201,
        headers: {
          "content-type": "application/json",
          "x-ratelimit-remaining-requests": "99",
          "content-encoding": "gzip",
          connection: "close",
          "transfer-encoding": "chunked",
        },
      }),
  );
  const { sidecar, logs } = makeSidecar(t, dir, { oauth, fetchImpl });
  const call = await serve(t, sidecar);

  const reqBody = JSON.stringify({ model: "grok-4", input: "ping" });
  const r = await call(
    "POST",
    "/v1/responses?foo=bar",
    {
      authorization: `Bearer ${TRIGGER}`,
      "x-auth-token": TRIGGER,
      "content-type": "application/json",
      "x-request-id": "abc-123",
      connection: "keep-alive",
      "accept-encoding": "gzip, br",
    },
    reqBody,
  );

  assert.equal(r.status, 201);
  assert.equal(r.text, upstreamBody);
  assert.equal(r.headers.get("content-type"), "application/json");
  assert.equal(r.headers.get("x-ratelimit-remaining-requests"), "99");
  assert.equal(r.headers.get("content-encoding"), null, "decoded body → no stale content-encoding");
  assert.equal(r.headers.get("cache-control"), "no-store");

  assert.equal(calls.length, 1);
  const [{ url, init }] = calls;
  assert.equal(url, `${XAI_UPSTREAM_ORIGIN}/v1/responses?foo=bar`);
  assert.equal(init.method, "POST");
  assert.equal(init.headers.authorization, `Bearer ${ACCESS}`, "stored access token injected");
  assert.equal(init.headers["x-auth-token"], undefined, "client trigger header never forwarded");
  assert.equal(init.headers["content-type"], "application/json");
  assert.equal(init.headers["x-request-id"], "abc-123", "ordinary end-to-end headers pass through");
  assert.equal(init.headers.connection, undefined, "hop-by-hop stripped");
  assert.equal(init.headers.host, undefined);
  assert.equal(init.headers["accept-encoding"], "identity");
  assert.ok(!Object.values(init.headers).some((v) => String(v).includes(TRIGGER)), "trigger token never reaches xAI");
  assert.equal(Buffer.from(init.body).toString("utf8"), reqBody);

  const st = await call("GET", "/status");
  assert.equal(st.body.proxy.requests, 1);
  assert.equal(st.body.proxy.lastUpstreamStatus, 201);
  assert.ok(logs.some((l) => /proxy: POST \/v1\/responses → 201/.test(l)));
  for (const l of [...logs, st.text]) {
    assertNoSecrets(assert, l);
    assert.ok(!l.includes(TRIGGER), "trigger token never logged");
  }
});

test("proxy: refreshes an expiring credential first and injects the NEW token; refresh failure → 503", async (t) => {
  const dir = await tmpDir(t);
  const p = path.join(dir, "xai_oauth.json");
  const nowMs = Date.UTC(2026, 8, 15, 12, 0, 0);
  await save(credential({ expires: nowMs + 60_000 }), p); // inside the 15-min margin
  let failRefresh = false;
  const oauth = {
    refresh: async () => {
      if (failRefresh) throw new Error(`invalid_grant ${REFRESH}`);
      return credential({ access: "NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz", refresh: "NEW_REFRESH_TOKEN_SUPER_SECRET_yyyyyyy", expires: nowMs + 3_600_000 });
    },
  };
  const { calls, fetchImpl } = fakeUpstream();
  const { sidecar, logs } = makeSidecar(t, dir, { oauth, fetchImpl, now: () => nowMs });
  const call = await serve(t, sidecar);

  const ok = await call("GET", "/v1/models", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(ok.status, 200);
  assert.equal(calls[0].init.headers.authorization, "Bearer NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz");
  assert.equal((await load(p)).access, "NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz", "refreshed credential persisted to the volume");

  // Make the stored one expire again and let refresh fail.
  await save(credential({ expires: nowMs + 60_000 }), p);
  failRefresh = true;
  const failed = await call("GET", "/v1/models", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(failed.status, 503);
  assert.equal(failed.body.error, "refresh_failed");
  assert.equal(calls.length, 1, "no upstream call with a stale token");

  for (const l of logs) {
    assertNoSecrets(assert, l);
    assert.ok(!l.includes("NEW_ACCESS_TOKEN_SUPER_SECRET_zzzzzzzz"));
    assert.ok(!l.includes("NEW_REFRESH_TOKEN_SUPER_SECRET_yyyyyyy"));
  }
});

test("proxy: upstream unreachable → 502 upstream_unreachable; upstream 4xx/5xx are relayed verbatim", async (t) => {
  const dir = await tmpDir(t);
  await save(credential(), path.join(dir, "xai_oauth.json"));
  const { oauth } = gatedOauth();
  let mode = "down";
  const { fetchImpl } = fakeUpstream(() => {
    if (mode === "down") return new TypeError("fetch failed");
    return new Response(JSON.stringify({ error: { message: "rate limited" } }), { status: 429, headers: { "content-type": "application/json", "retry-after": "7" } });
  });
  const { sidecar, logs } = makeSidecar(t, dir, { oauth, fetchImpl });
  const call = await serve(t, sidecar);

  const down = await call("GET", "/v1/models", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(down.status, 502);
  assert.deepEqual(down.body, { ok: false, error: "upstream_unreachable" });

  mode = "429";
  const limited = await call("GET", "/v1/models", { authorization: `Bearer ${TRIGGER}` });
  assert.equal(limited.status, 429, "xAI's own status is passed through untouched");
  assert.equal(limited.headers.get("retry-after"), "7");
  assert.match(limited.body.error.message, /rate limited/);

  const st = await call("GET", "/status");
  assert.equal(st.body.proxy.upstreamErrors, 1);
  assert.equal(st.body.proxy.lastUpstreamStatus, 429);
  for (const l of [...logs, st.text]) assertNoSecrets(assert, l);
});
