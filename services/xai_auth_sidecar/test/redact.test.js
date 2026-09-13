import { test } from "node:test";
import assert from "node:assert/strict";

import { redactSecret, redactText, secretsFrom } from "../src/redact.js";
import { ACCESS, REFRESH, API_KEY, credential } from "./helpers.js";

test("redactSecret shows only the first 6 chars", () => {
  assert.equal(redactSecret(ACCESS), "ACCESS…");
  assert.equal(redactSecret("short"), "…");
  assert.equal(redactSecret(""), "…");
  assert.equal(redactSecret(undefined), "…");
});

test("redactText replaces every occurrence of every secret, longest first", () => {
  const text = `a=${ACCESS} r=${REFRESH} again=${ACCESS} k=${API_KEY}`;
  const out = redactText(text, [ACCESS, REFRESH, API_KEY]);
  assert.ok(!out.includes(ACCESS));
  assert.ok(!out.includes(REFRESH));
  assert.ok(!out.includes(API_KEY));
  assert.equal(out, "a=ACCESS… r=REFRES… again=ACCESS… k=xai-AP…");
  // a secret embedded in a longer secret does not leave fragments
  const long = `${ACCESS}TAIL`;
  assert.equal(redactText(long, [ACCESS, long]), "ACCESS…");
});

test("redactText ignores short/non-string secrets and non-string input", () => {
  assert.equal(redactText("keep ok", ["ok", null, 42]), "keep ok");
  assert.equal(redactText(undefined, [ACCESS]), "");
});

test("secretsFrom collects access, refresh and XAI_API_KEY", () => {
  assert.deepEqual(secretsFrom(credential(), { XAI_API_KEY: API_KEY }), [ACCESS, REFRESH, API_KEY]);
  assert.deepEqual(secretsFrom(null, {}), []);
});
