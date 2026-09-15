import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_CANDIDATE_MODELS,
  PROBE_MODELS_ENV,
  parseCandidateModels,
  buildXSearchTool,
  extractOutputText,
  detectToolInvocation,
  extractRateLimitHeaders,
  errorToRecord,
  successToRecord,
  modelsListToRecord,
  excerpt,
  renderMarkdown,
  renderRedacted,
} from "../src/probe_report.js";
import { ACCESS, REFRESH, API_KEY, assertNoSecrets } from "./helpers.js";

test("parseCandidateModels: default list and env override", () => {
  assert.deepEqual(parseCandidateModels({}), ["grok-4", "grok-4.3", "grok-4.5", "grok-4.6"]);
  assert.deepEqual(parseCandidateModels({ [PROBE_MODELS_ENV]: " grok-4.6, grok-3 ,, " }), ["grok-4.6", "grok-3"]);
  assert.deepEqual(parseCandidateModels({ [PROBE_MODELS_ENV]: " , " }), DEFAULT_CANDIDATE_MODELS);
});

test("buildXSearchTool matches grok_x_search.py's exact shape for the last 7 days", () => {
  const now = Date.UTC(2026, 8, 13, 12, 0, 0); // 2026-09-13
  const tool = buildXSearchTool("@xai", now);
  assert.deepEqual(tool, {
    type: "x_search",
    allowed_x_handles: ["xai"],
    from_date: "2026-09-06",
    to_date: "2026-09-13",
  });
  assert.deepEqual(Object.keys(tool), ["type", "allowed_x_handles", "from_date", "to_date"]);
});

test("extractOutputText mirrors _extract_response_text and prefers output_text", () => {
  assert.equal(extractOutputText({ output_text: "  OK " }), "OK");
  assert.equal(
    extractOutputText({
      output: [
        { type: "reasoning", summary: [] },
        { type: "message", content: [{ type: "output_text", text: " hello " }] },
      ],
    }),
    "hello",
  );
  assert.equal(extractOutputText({ output: [{ type: "reasoning" }] }), "");
  assert.equal(extractOutputText(null), "");
});

test("detectToolInvocation flags x_search / *tool* output items", () => {
  const r = detectToolInvocation({
    output: [{ type: "x_search_call" }, { type: "reasoning" }, { type: "message" }],
  });
  assert.deepEqual(r, { outputTypes: ["x_search_call", "reasoning", "message"], toolInvoked: true, toolTypes: ["x_search_call"] });
  assert.equal(detectToolInvocation({ output: [{ type: "custom_tool_call" }] }).toolInvoked, true);
  assert.equal(detectToolInvocation({ output: [{ type: "message" }] }).toolInvoked, false);
  assert.deepEqual(detectToolInvocation({}).outputTypes, []);
});

test("extractRateLimitHeaders accepts Headers and plain objects, keeps only rate-limit keys", () => {
  const h = new Headers({
    "X-RateLimit-Limit-Requests": "60",
    "x-ratelimit-remaining-requests": "59",
    "Retry-After": "7",
    "content-type": "application/json",
    authorization: `Bearer ${ACCESS}`,
  });
  assert.deepEqual(extractRateLimitHeaders(h), {
    "x-ratelimit-limit-requests": "60",
    "x-ratelimit-remaining-requests": "59",
    "retry-after": "7",
  });
  assert.deepEqual(extractRateLimitHeaders({ "X-RateLimit-Reset": ["1", "2"], other: "x" }), { "x-ratelimit-reset": "1, 2" });
  assert.deepEqual(extractRateLimitHeaders(undefined), {});
});

test("errorToRecord: HTTP error keeps status, headers, body excerpt; transport error is explicit", () => {
  const httpErr = Object.assign(new Error("401 Unauthorized"), {
    name: "AuthenticationError",
    status: 401,
    headers: new Headers({ "x-ratelimit-limit-requests": "10" }),
    error: { error: "Incorrect API key provided", code: "invalid_api_key" },
  });
  const rec = errorToRecord(httpErr);
  assert.equal(rec.ok, false);
  assert.equal(rec.status, 401);
  assert.deepEqual(rec.headers, { "x-ratelimit-limit-requests": "10" });
  assert.match(rec.error.bodyExcerpt, /Incorrect API key provided/);
  assert.equal(rec.error.name, "AuthenticationError");

  const netErr = Object.assign(new Error("Connection error."), { name: "APIConnectionError", cause: { code: "ECONNRESET" } });
  const rec2 = errorToRecord(netErr);
  assert.equal(rec2.status, null);
  assert.match(rec2.statusText, /no HTTP response/);
  assert.equal(rec2.error.bodyExcerpt, "Connection error.");
  assert.equal(rec2.error.code, "ECONNRESET");

  assert.equal(errorToRecord(undefined).error.bodyExcerpt, "(empty error body)");
});

test("successToRecord / modelsListToRecord capture status, headers, text and ids", () => {
  const response = { status: 200, headers: new Headers({ "x-ratelimit-remaining-tokens": "999" }) };
  const rec = successToRecord(
    { id: "resp_1", model: "grok-4.3", output: [{ type: "message", content: [{ text: "OK" }] }], usage: { total_tokens: 5 } },
    response,
  );
  assert.equal(rec.ok, true);
  assert.equal(rec.status, 200);
  assert.deepEqual(rec.headers, { "x-ratelimit-remaining-tokens": "999" });
  assert.equal(rec.text, "OK");
  assert.equal(rec.responseId, "resp_1");
  assert.equal(rec.toolInvoked, false);

  const list = modelsListToRecord({ data: [{ id: "grok-4.3" }, { id: "grok-4.6" }] }, response);
  assert.deepEqual(list.modelIds, ["grok-4.3", "grok-4.6"]);
  assert.deepEqual(modelsListToRecord({ body: { data: [{ id: "a" }] } }, undefined).modelIds, ["a"]);
});

test("excerpt collapses whitespace and truncates", () => {
  assert.equal(excerpt("a   b\n c"), "a b c");
  assert.equal(excerpt({ a: 1 }), '{"a":1}');
  assert.equal(excerpt("x".repeat(10), 4), "xxxx…");
  assert.equal(excerpt(null), "");
});

function sampleReport({ withControl = true } = {}) {
  const ok = (model, text = "OK") => ({
    ok: true,
    status: 200,
    statusText: "200",
    headers: { "x-ratelimit-remaining-requests": "58" },
    model,
    text,
    outputTypes: ["message"],
    toolInvoked: false,
    toolTypes: [],
    durationMs: 12,
  });
  const fail = (model, status, body) => ({
    ok: false,
    status,
    statusText: String(status),
    headers: {},
    model,
    error: { name: "APIError", message: body, bodyExcerpt: body },
    durationMs: 8,
  });
  return {
    issue: 397,
    phase: 1,
    generatedAt: "2026-09-13T00:00:00.000Z",
    baseUrl: "https://api.x.ai/v1",
    credentialPath: "/home/op/.config/xagent-trading-bot/xai_oauth.json",
    tokenExpiresAt: "2026-09-13T01:00:00.000Z",
    refreshed: false,
    candidateModels: ["grok-4", "grok-4.3"],
    token: {
      modelsList: fail("(list)", 403, `{"error":"forbidden for token ${ACCESS}"}`),
      q1: [fail("grok-4", 404, "model not found"), ok("grok-4.3")],
      passingModels: ["grok-4.3"],
      q2: { ...ok("grok-4.3", "[]"), tool: { type: "x_search", allowed_x_handles: ["xai"], from_date: "a", to_date: "b" }, toolInvoked: true, toolTypes: ["x_search_call"], outputTypes: ["x_search_call", "message"] },
      rateLimitHeadersSeen: { "x-ratelimit-remaining-requests": ["59", "58"] },
      burst: [ok("grok-4.3"), fail("grok-4.3", 429, `rate limited; retry token ${REFRESH}`)],
      burstStoppedOn429: true,
    },
    control: withControl
      ? { model: "grok-4.3", modelsList: { ok: true, status: 200, statusText: "200", headers: {}, modelIds: ["grok-4.3"] }, q1: ok("grok-4.3"), q2: { ...ok("grok-4.3", "[]"), toolInvoked: true, tool: {} } }
      : null,
    controlNote: withControl ? "set" : "unset",
  };
}

test("renderMarkdown: failures are never blank cells, sections present, control side-by-side", () => {
  const md = renderMarkdown(sampleReport());
  assert.match(md, /## Q1 — Responses API/);
  assert.match(md, /## Q2 — x_search/);
  assert.match(md, /## Q3 — rate limits/);
  assert.match(md, /## Control run/);
  assert.match(md, /\| 1 \| grok-4 \| 404 FAIL \| 8 \| \(none\) \| APIError: model not found \|/);
  assert.match(md, /\| 2 \| grok-4\.3 \| 200 OK \| 12 \| x-ratelimit-remaining-requests=58 \| "OK" \|/);
  assert.match(md, /403 FAIL — APIError: .*forbidden/);
  assert.match(md, /tool invocation detected: YES \(x_search_call\)/);
  assert.match(md, /stopped at first 429/);
  assert.match(md, /`x-ratelimit-remaining-requests`: 59 → 58/);
  assert.match(md, /\| GET \/v1\/models \| 403 FAIL/);
  assert.match(md, /\| x_search \| 200 OK tool=yes \| 200 OK tool=yes \|/);
  // no empty table cells anywhere
  assert.ok(!/\|[ \t]*\|/.test(md), "found an empty table cell");
});

test("renderMarkdown: control skipped message when XAI_API_KEY unset; Q2/burst not-run messages", () => {
  const report = sampleReport({ withControl: false });
  report.token.q2 = null;
  report.token.burst = [];
  report.token.passingModels = [];
  const md = renderMarkdown(report);
  assert.match(md, /XAI_API_KEY is not set in the environment; control run skipped/);
  assert.match(md, /Passing model\(s\): NONE/);
  assert.match(md, /not run \(no model passed Q1, so x_search was not attempted\)/);
  assert.match(md, /Burst: not run/);
});

test("renderRedacted: raw tokens embedded in error bodies never reach the rendered report", () => {
  const { markdown, json } = renderRedacted(sampleReport(), [ACCESS, REFRESH, API_KEY]);
  assertNoSecrets(assert, markdown);
  assertNoSecrets(assert, json);
  assert.match(markdown, /forbidden for token ACCESS…/);
  assert.match(json, /retry token REFRES…/);
  assert.doesNotThrow(() => JSON.parse(json));
});
