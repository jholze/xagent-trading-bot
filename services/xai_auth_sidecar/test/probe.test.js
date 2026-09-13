import { test } from "node:test";
import assert from "node:assert/strict";

import { runProbe, callWithResponse, makeClient } from "../src/probe.js";
import { renderRedacted, TINY_PROMPT } from "../src/probe_report.js";
import { ACCESS, REFRESH, API_KEY, assertNoSecrets } from "./helpers.js";

/** Minimal stand-in for an OpenAI client: every method returns an APIPromise-like with withResponse(). */
function mockClient({ apiKey, handlers }) {
  const calls = [];
  const wrap = (kind, args) => {
    calls.push({ kind, args });
    const p = Promise.resolve().then(() => handlers(kind, args, apiKey));
    p.withResponse = () => p;
    return p;
  };
  return {
    calls,
    models: { list: () => wrap("models.list", {}) },
    responses: { create: (body) => wrap("responses.create", body) },
  };
}

function httpError(status, body, headers = {}) {
  return Object.assign(new Error(`${status} status code`), {
    name: "APIError",
    status,
    headers: new Headers(headers),
    error: body,
  });
}

function okResponse(model, text = "OK", headers = {}, extraOutput = []) {
  return {
    data: { id: `resp_${model}`, model, output: [...extraOutput, { type: "message", content: [{ type: "output_text", text }] }] },
    response: { status: 200, headers: new Headers({ "x-ratelimit-remaining-requests": "42", ...headers }) },
  };
}

test("runProbe: Q1/Q2/Q3 + control flow, stops burst at 429, report redacts tokens", async () => {
  let burstCount = 0;
  const tokenHandler = (kind, body) => {
    if (kind === "models.list") throw httpError(403, { error: `token ${ACCESS} may not list models` });
    if (body.tools) {
      assert.deepEqual(Object.keys(body.tools[0]), ["type", "allowed_x_handles", "from_date", "to_date"]);
      assert.equal(body.tools[0].type, "x_search");
      assert.deepEqual(body.tools[0].allowed_x_handles, ["xai"]);
      return okResponse(body.model, '[{"post_id":"1"}]', {}, [{ type: "x_search_call" }]);
    }
    if (body.model === "grok-4") throw httpError(404, { error: "The model grok-4 does not exist" });
    if (body.model === "grok-4.5") throw httpError(401, { error: `Incorrect API key ${ACCESS}` }, { "x-ratelimit-limit-requests": "5" });
    if (body.model === "grok-4.3") {
      assert.equal(body.input, TINY_PROMPT);
      burstCount += 1;
      if (burstCount === 4) throw httpError(429, { error: "Too many requests" }, { "retry-after": "3" });
      return okResponse("grok-4.3", "OK", { "x-ratelimit-remaining-requests": String(50 - burstCount) });
    }
    return okResponse(body.model);
  };
  const keyHandler = (kind, body) => {
    if (kind === "models.list") return { data: { data: [{ id: "grok-4.3" }, { id: "grok-4.6" }] }, response: { status: 200, headers: new Headers() } };
    if (body.tools) return okResponse(body.model, "[]", {}, [{ type: "x_search_call" }]);
    return okResponse(body.model);
  };

  const tokenClient = mockClient({ apiKey: ACCESS, handlers: tokenHandler });
  const keyClient = mockClient({ apiKey: API_KEY, handlers: keyHandler });
  const sleeps = [];
  const report = await runProbe({
    tokenClient,
    keyClient,
    candidateModels: ["grok-4", "grok-4.3", "grok-4.5"],
    credentialPath: "/x/xai_oauth.json",
    tokenExpiresAt: "2026-01-01T00:00:00.000Z",
    sleep: async (ms) => sleeps.push(ms),
    now: (() => {
      let t = 1_000;
      return () => (t += 5);
    })(),
  });

  const t = report.token;
  assert.equal(t.modelsList.ok, false);
  assert.equal(t.modelsList.status, 403);
  assert.deepEqual(t.q1.map((r) => [r.model, r.status]), [["grok-4", 404], ["grok-4.3", 200], ["grok-4.5", 401]]);
  assert.deepEqual(t.passingModels, ["grok-4.3"]);
  assert.equal(t.q1[2].headers["x-ratelimit-limit-requests"], "5", "headers captured on error responses too");
  assert.ok(t.q1.every((r) => typeof r.durationMs === "number"));

  assert.equal(t.q2.model, "grok-4.3");
  assert.equal(t.q2.ok, true);
  assert.equal(t.q2.toolInvoked, true);
  assert.deepEqual(t.q2.toolTypes, ["x_search_call"]);
  assert.match(t.q2.text, /post_id/);
  assert.equal(t.q2.tool.type, "x_search");

  // burst: calls 2,3 ok, call 4 is 429 -> stop (burst 1 was the Q1 call)
  assert.deepEqual(t.burst.map((r) => r.status), [200, 200, 429]);
  assert.equal(t.burstStoppedOn429, true);
  assert.equal(t.burst[2].headers["retry-after"], "3");
  assert.deepEqual(sleeps, [1000, 1000], "1 s gap between burst calls, none before the first");
  assert.deepEqual(t.rateLimitHeadersSeen["x-ratelimit-remaining-requests"], ["49", "42", "48", "47"]);

  assert.equal(report.control.model, "grok-4.3");
  assert.deepEqual(report.control.modelsList.modelIds, ["grok-4.3", "grok-4.6"]);
  assert.equal(report.control.q1.ok, true);
  assert.equal(report.control.q2.toolInvoked, true);
  assert.equal(keyClient.calls.length, 3);

  const { markdown, json } = renderRedacted(report, [ACCESS, REFRESH, API_KEY]);
  assertNoSecrets(assert, markdown);
  assertNoSecrets(assert, json);
  assert.match(markdown, /Incorrect API key ACCESS…/);
  assert.match(markdown, /\| GET \/v1\/models \| 403 FAIL/);
  assert.match(markdown, /200 OK — 2 models: grok-4.3, grok-4.6/);
});

test("runProbe: nothing passes Q1 -> Q2/burst skipped, control still runs on first candidate, no XAI_API_KEY -> control null", async () => {
  const failing = mockClient({
    apiKey: ACCESS,
    handlers: () => {
      throw Object.assign(new Error("Connection error."), { name: "APIConnectionError" });
    },
  });
  const report = await runProbe({ tokenClient: failing, candidateModels: ["grok-4.6"], credentialPath: "/x", tokenExpiresAt: "t", sleep: async () => {} });
  assert.equal(report.token.modelsList.status, null);
  assert.match(report.token.modelsList.statusText, /no HTTP response/);
  assert.deepEqual(report.token.passingModels, []);
  assert.equal(report.token.q2, null);
  assert.deepEqual(report.token.burst, []);
  assert.equal(report.control, null);
  assert.match(report.controlNote, /not set/);
  assert.equal(failing.calls.length, 2);

  const key = mockClient({ apiKey: API_KEY, handlers: (kind, body) => (kind === "models.list" ? { data: { data: [] }, response: {} } : okResponse(body.model)) });
  const report2 = await runProbe({ tokenClient: failing, keyClient: key, candidateModels: ["grok-4.6"], credentialPath: "/x", tokenExpiresAt: "t", sleep: async () => {} });
  assert.equal(report2.control.model, "grok-4.6");
  assert.equal(report2.control.q1.ok, true);
});

// --- real OpenAI SDK, mocked fetch ---------------------------------------

function fetchMock(routes) {
  const seen = [];
  const impl = async (url, init) => {
    seen.push({ url: String(url), init });
    const u = new URL(String(url));
    const route = routes[u.pathname];
    if (!route) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: { "content-type": "application/json" } });
    const r = typeof route === "function" ? route(init) : route;
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "content-type": "application/json", ...(r.headers ?? {}) } });
  };
  impl.seen = seen;
  return impl;
}

test("callWithResponse with the real SDK: Bearer token sent, status + rate-limit headers captured on success and on 429", async () => {
  const fetchImpl = fetchMock({
    "/v1/responses": (init) => {
      const body = JSON.parse(init.body);
      if (body.model === "limited") {
        return { status: 429, headers: { "retry-after": "9", "x-ratelimit-remaining-requests": "0" }, body: { error: "Too many requests", code: "rate_limited" } };
      }
      return {
        status: 200,
        headers: { "x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-requests": "59" },
        body: { id: "resp_1", object: "response", model: body.model, output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "OK" }] }] },
      };
    },
    "/v1/models": { status: 200, headers: { "x-ratelimit-limit-requests": "60" }, body: { object: "list", data: [{ id: "grok-4.3", object: "model" }] } },
  });
  const client = makeClient(ACCESS, { fetch: fetchImpl });

  const ok = await callWithResponse(() => client.responses.create({ model: "grok-4.3", input: TINY_PROMPT }));
  assert.equal(ok.ok, true);
  assert.equal(ok.status, 200);
  assert.deepEqual(ok.headers, { "x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-requests": "59" });
  assert.equal(ok.text, "OK");

  const limited = await callWithResponse(() => client.responses.create({ model: "limited", input: TINY_PROMPT }));
  assert.equal(limited.ok, false);
  assert.equal(limited.status, 429);
  assert.deepEqual(limited.headers, { "retry-after": "9", "x-ratelimit-remaining-requests": "0" });
  assert.match(limited.error.bodyExcerpt, /Too many requests/);

  const { modelsListToRecord } = await import("../src/probe_report.js");
  const list = await callWithResponse(() => client.models.list(), modelsListToRecord);
  assert.equal(list.ok, true);
  assert.deepEqual(list.modelIds, ["grok-4.3"]);
  assert.deepEqual(list.headers, { "x-ratelimit-limit-requests": "60" });

  assert.equal(fetchImpl.seen.length, 3, "maxRetries=0: the 429 was not retried");
  for (const { url, init } of fetchImpl.seen) {
    assert.ok(url.startsWith("https://api.x.ai/v1/"), url);
    const auth = new Headers(init.headers).get("authorization");
    assert.equal(auth, `Bearer ${ACCESS}`);
  }
});
