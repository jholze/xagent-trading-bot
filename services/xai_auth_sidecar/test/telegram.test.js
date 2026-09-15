import { test } from "node:test";
import assert from "node:assert/strict";

import {
  telegramConfigFrom,
  formatDeviceCodeMessage,
  formatSessionStoredMessage,
  sendTelegramMessage,
  createOperatorNotifier,
} from "../src/telegram.js";
import { credential, ACCESS, REFRESH, assertNoSecrets } from "./helpers.js";

const BOT_TOKEN = "123456:BOT_TOKEN_SUPER_SECRET_abcdefghijklmnop";
const ENV = { TELEGRAM_BOT_TOKEN: BOT_TOKEN, TELEGRAM_CHAT_ID: " 987654321 " };
const EVENT = {
  type: "device_code",
  userCode: "ABCD-EFGH",
  verificationUri: "https://auth.x.ai/activate?user_code=ABCD-EFGH",
  intervalSeconds: 5,
  expiresInSeconds: 600,
};

function fakeFetch(status = 200, body = '{"ok":true}') {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, text: async () => body };
  };
  fn.calls = calls;
  return fn;
}

test("telegramConfigFrom: needs both vars, trims, else null", () => {
  assert.deepEqual(telegramConfigFrom(ENV), { token: BOT_TOKEN, chatId: "987654321" });
  assert.equal(telegramConfigFrom({ TELEGRAM_BOT_TOKEN: BOT_TOKEN }), null);
  assert.equal(telegramConfigFrom({ TELEGRAM_CHAT_ID: "1" }), null);
  assert.equal(telegramConfigFrom({ TELEGRAM_BOT_TOKEN: "  ", TELEGRAM_CHAT_ID: "1" }), null);
  assert.equal(telegramConfigFrom({}), null);
});

test("formatDeviceCodeMessage: URL, code, validity window — nothing else", () => {
  const now = Date.UTC(2026, 8, 15, 12, 0, 0);
  const text = formatDeviceCodeMessage(EVENT, now);
  assert.match(text, /https:\/\/auth\.x\.ai\/activate\?user_code=ABCD-EFGH/);
  assert.match(text, /Code: ABCD-EFGH/);
  assert.match(text, /Valid for 10 min \(until 2026-09-15T12:10:00\.000Z\)/);
  assertNoSecrets(assert, text);
});

test("formatSessionStoredMessage: path + expiry, no token", () => {
  const cred = credential({ expires: Date.UTC(2026, 8, 15, 13, 0, 0) });
  const text = formatSessionStoredMessage(cred, "/data/grok/xai_oauth.json");
  assert.match(text, /session stored/);
  assert.match(text, /\/data\/grok\/xai_oauth\.json/);
  assert.match(text, /expires: 2026-09-15T13:00:00\.000Z/);
  assertNoSecrets(assert, text);
});

test("sendTelegramMessage: posts to Bot API with chat_id and redacted text; never rejects", async () => {
  const fetchImpl = fakeFetch();
  const r = await sendTelegramMessage(`hello ${ACCESS} and ${BOT_TOKEN}`, {
    env: ENV,
    fetchImpl,
    secrets: [ACCESS, REFRESH],
  });
  assert.deepEqual(r, { ok: true, status: 200 });
  assert.equal(fetchImpl.calls.length, 1);
  const { url, init } = fetchImpl.calls[0];
  assert.equal(url, `https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`);
  assert.equal(init.method, "POST");
  const payload = JSON.parse(init.body);
  assert.equal(payload.chat_id, "987654321");
  assert.equal(payload.disable_web_page_preview, true);
  assertNoSecrets(assert, payload.text);
  assert.ok(!payload.text.includes(BOT_TOKEN), "bot token redacted from message text");
  assert.match(payload.text, /hello ACCESS…/);
});

test("sendTelegramMessage: not configured -> ok:false without calling fetch", async () => {
  const fetchImpl = fakeFetch();
  const r = await sendTelegramMessage("x", { env: {}, fetchImpl });
  assert.equal(r.ok, false);
  assert.match(r.error, /not configured/);
  assert.equal(fetchImpl.calls.length, 0);
});

test("sendTelegramMessage: HTTP error and thrown fetch both resolve ok:false with bot token redacted", async () => {
  const bad = await sendTelegramMessage("x", { env: ENV, fetchImpl: fakeFetch(403, `forbidden ${BOT_TOKEN}`) });
  assert.equal(bad.ok, false);
  assert.equal(bad.status, 403);
  assert.ok(!bad.error.includes(BOT_TOKEN));
  assert.match(bad.error, /HTTP 403/);

  const thrown = await sendTelegramMessage("x", {
    env: ENV,
    fetchImpl: async () => {
      throw new Error(`ECONNRESET calling bot${BOT_TOKEN}`);
    },
  });
  assert.equal(thrown.ok, false);
  assert.equal(thrown.status, 0);
  assert.ok(!thrown.error.includes(BOT_TOKEN));
});

test("createOperatorNotifier: null when unconfigured; logs failures; returns boolean", async () => {
  assert.equal(createOperatorNotifier({}), null);
  const logs = [];
  const okNotify = createOperatorNotifier(ENV, { fetchImpl: fakeFetch(), log: (m) => logs.push(m) });
  assert.equal(await okNotify("hi"), true);
  assert.equal(logs.length, 0);
  const failNotify = createOperatorNotifier(ENV, { fetchImpl: fakeFetch(500, "boom"), log: (m) => logs.push(m) });
  assert.equal(await failNotify("hi"), false);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /Telegram notify failed: HTTP 500/);
});
