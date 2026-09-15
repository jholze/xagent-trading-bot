/**
 * Pure, network-free helpers for the probe: request shaping, response
 * summarisation, header extraction and report rendering. Everything here is
 * unit-tested with plain objects; nothing imports the OpenAI SDK.
 */
import { redactText } from "./redact.js";

export const DEFAULT_CANDIDATE_MODELS = ["grok-4", "grok-4.3", "grok-4.5", "grok-4.6"];
export const PROBE_MODELS_ENV = "XAI_PROBE_MODELS";
export const X_SEARCH_HANDLE = "xai";
export const X_SEARCH_DAYS = 7;
export const BURST_SIZE = 5;
export const BURST_GAP_MS = 1_000;
export const TINY_PROMPT = "Reply with the single word OK.";
export const BODY_EXCERPT_CHARS = 400;
export const TEXT_EXCERPT_CHARS = 600;

/** Candidate model list: `XAI_PROBE_MODELS=a,b,c` overrides the default. */
export function parseCandidateModels(env = {}) {
  const raw = env[PROBE_MODELS_ENV];
  if (typeof raw !== "string" || raw.trim().length === 0) return [...DEFAULT_CANDIDATE_MODELS];
  const list = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return list.length > 0 ? list : [...DEFAULT_CANDIDATE_MODELS];
}

function isoDate(d) {
  return d.toISOString().slice(0, 10);
}

/**
 * EXACTLY grok_x_search.py's tool shape:
 *   {"type": "x_search", "allowed_x_handles": [handle], "from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD"}
 */
export function buildXSearchTool(handle = X_SEARCH_HANDLE, nowMs = Date.now(), days = X_SEARCH_DAYS) {
  const to = new Date(nowMs);
  const from = new Date(nowMs - Math.max(days, 1) * 86_400_000);
  return {
    type: "x_search",
    allowed_x_handles: [handle.replace("@", "").trim()],
    from_date: isoDate(from),
    to_date: isoDate(to),
  };
}

export function buildXSearchPrompt(handle = X_SEARCH_HANDLE, tool) {
  return (
    `Find up to 3 recent original posts from @${handle} between ${tool.from_date} and ${tool.to_date}.\n\n` +
    "Return ONLY a JSON array with exactly up to 3 items. Each item must have:\n" +
    '- "post_id": tweet id string if known, else "grok_{index}"\n' +
    '- "text": full post text\n' +
    '- "created_at": ISO8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)\n'
  );
}

/** Mirrors grok_x_search.py::_extract_response_text (first message text). */
export function extractOutputText(response) {
  if (!response || typeof response !== "object") return "";
  if (typeof response.output_text === "string" && response.output_text.trim()) return response.output_text.trim();
  const output = Array.isArray(response.output) ? response.output : [];
  for (const item of output) {
    if (item && item.type === "message" && Array.isArray(item.content)) {
      for (const part of item.content) {
        if (part && typeof part.text === "string" && part.text) return part.text.trim();
      }
    }
  }
  return "";
}

/** Output item types plus whether any suggests a tool / x_search invocation. */
export function detectToolInvocation(response) {
  const output = response && Array.isArray(response.output) ? response.output : [];
  const types = output.map((item) => (item && typeof item.type === "string" ? item.type : "unknown"));
  const toolTypes = types.filter((t) => /x_search|tool/i.test(t));
  return { outputTypes: types, toolInvoked: toolTypes.length > 0, toolTypes };
}

function headerEntries(headers) {
  if (!headers) return [];
  if (typeof headers.entries === "function" && typeof headers.get === "function") {
    return Array.from(headers.entries());
  }
  if (typeof headers === "object") return Object.entries(headers);
  return [];
}

/** Lower-cased `x-ratelimit-*` and `retry-after` headers, nothing else. */
export function extractRateLimitHeaders(headers) {
  const out = {};
  for (const [name, value] of headerEntries(headers)) {
    const key = String(name).toLowerCase();
    if (key.startsWith("x-ratelimit-") || key === "retry-after") {
      out[key] = Array.isArray(value) ? value.join(", ") : String(value);
    }
  }
  return out;
}

export function excerpt(value, max = BODY_EXCERPT_CHARS) {
  let text;
  if (value === undefined || value === null) return "";
  if (typeof value === "string") text = value;
  else {
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
  }
  text = text.replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/**
 * Normalise an SDK error (openai APIError has .status/.headers/.error; a
 * connection error has neither) into a report record. Never blank: when there
 * is no HTTP status we say so explicitly.
 */
export function errorToRecord(err) {
  const status = typeof err?.status === "number" ? err.status : null;
  const body = err?.error ?? err?.body ?? null;
  return {
    ok: false,
    status,
    statusText: status === null ? "no HTTP response (network/transport error)" : String(status),
    headers: extractRateLimitHeaders(err?.headers),
    error: {
      name: (err?.name && err.name !== "Error" ? err.name : err?.constructor?.name) ?? "Error",
      message: String(err?.message ?? err ?? "unknown error"),
      bodyExcerpt: excerpt(body) || excerpt(err?.message) || "(empty error body)",
      code: err?.code ?? err?.cause?.code ?? null,
    },
  };
}

/** Build a success record from `{ data, response }` (SDK `.withResponse()`). */
export function successToRecord(data, response) {
  const { outputTypes, toolInvoked, toolTypes } = detectToolInvocation(data);
  return {
    ok: true,
    status: typeof response?.status === "number" ? response.status : 200,
    statusText: String(typeof response?.status === "number" ? response.status : 200),
    headers: extractRateLimitHeaders(response?.headers),
    responseId: typeof data?.id === "string" ? data.id : null,
    model: typeof data?.model === "string" ? data.model : null,
    outputTypes,
    toolInvoked,
    toolTypes,
    text: excerpt(extractOutputText(data), TEXT_EXCERPT_CHARS),
    usage: data?.usage ?? null,
  };
}

export function modelsListToRecord(data, response) {
  const list = Array.isArray(data?.data) ? data.data : Array.isArray(data?.body?.data) ? data.body.data : [];
  return {
    ok: true,
    status: typeof response?.status === "number" ? response.status : 200,
    statusText: String(typeof response?.status === "number" ? response.status : 200),
    headers: extractRateLimitHeaders(response?.headers),
    modelIds: list.map((m) => (m && typeof m.id === "string" ? m.id : String(m))),
  };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function fmtHeaders(headers) {
  const entries = Object.entries(headers ?? {});
  if (entries.length === 0) return "(none)";
  return entries.map(([k, v]) => `${k}=${v}`).join("; ");
}

function fmtStatus(rec) {
  if (!rec) return "not run";
  if (rec.status === null || rec.status === undefined) return `ERR (${rec.statusText})`;
  return rec.ok ? `${rec.status} OK` : `${rec.status} FAIL`;
}

function fmtDetail(rec) {
  if (!rec) return "not run";
  if (rec.ok) return rec.text ? `"${rec.text}"` : "(no text)";
  return `${rec.error?.name ?? "Error"}: ${rec.error?.bodyExcerpt ?? rec.error?.message ?? "(no body)"}`;
}

function cell(v) {
  return String(v ?? "").replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

function renderCallTable(rows) {
  const lines = ["| # | model | status | ms | rate-limit headers | detail |", "|---|---|---|---|---|---|"];
  rows.forEach((r, i) => {
    lines.push(
      `| ${i + 1} | ${cell(r.model)} | ${cell(fmtStatus(r))} | ${cell(r.durationMs ?? "")} | ${cell(fmtHeaders(r.headers))} | ${cell(fmtDetail(r))} |`,
    );
  });
  return lines.join("\n");
}

function renderModelsList(rec) {
  if (!rec) return "not run";
  if (rec.ok) return `${rec.status} OK — ${rec.modelIds.length} models: ${rec.modelIds.join(", ") || "(empty list)"}`;
  return `${fmtStatus(rec)} — ${fmtDetail(rec)}`;
}

function renderXSearch(rec) {
  if (!rec) return "not run (no model passed Q1, so x_search was not attempted)";
  const lines = [
    `- model: ${rec.model}`,
    `- status: ${fmtStatus(rec)}`,
    `- tool shape sent: \`${JSON.stringify(rec.tool)}\``,
    `- rate-limit headers: ${fmtHeaders(rec.headers)}`,
  ];
  if (rec.ok) {
    lines.push(`- output item types: ${rec.outputTypes.join(", ") || "(none)"}`);
    lines.push(`- tool invocation detected: ${rec.toolInvoked ? `YES (${rec.toolTypes.join(", ")})` : "no"}`);
    lines.push(`- text: ${rec.text ? `"${rec.text}"` : "(no text)"}`);
  } else {
    lines.push(`- error: ${fmtDetail(rec)}`);
  }
  return lines.join("\n");
}

export function renderMarkdown(report) {
  const t = report.token;
  const lines = [
    "# xAI subscription-token probe (issue #397, Phase 1)",
    "",
    `- generated: ${report.generatedAt}`,
    `- base URL: ${report.baseUrl}`,
    `- credential: ${report.credentialPath} (access token expires ${report.tokenExpiresAt}; refreshed before run: ${report.refreshed ? "yes" : "no"})`,
    `- candidate models: ${report.candidateModels.join(", ")}`,
    "",
    "## Q1 — Responses API with the subscription token",
    "",
    `**GET /v1/models:** ${renderModelsList(t.modelsList)}`,
    "",
    renderCallTable(t.q1),
    "",
    `Passing model(s): ${t.passingModels.length ? t.passingModels.join(", ") : "NONE"}`,
    "",
    "## Q2 — x_search tool with the subscription token",
    "",
    renderXSearch(t.q2),
    "",
    "## Q3 — rate limits",
    "",
    "Headers seen across all token calls above:",
    "",
    fmtHeaders(t.rateLimitHeadersSeen) === "(none)"
      ? "- (no x-ratelimit-* / retry-after headers were returned by any call)"
      : Object.entries(t.rateLimitHeadersSeen)
          .map(([k, v]) => `- \`${k}\`: ${Array.isArray(v) ? v.join(" → ") : v}`)
          .join("\n"),
    "",
    t.burst.length
      ? `Burst of ${t.burst.length} sequential tiny calls (${BURST_GAP_MS} ms apart${t.burstStoppedOn429 ? ", stopped at first 429" : ""}):\n\n${renderCallTable(t.burst)}`
      : "Burst: not run (no model passed Q1).",
    "",
    "## Control run — plain XAI_API_KEY",
    "",
  ];
  const c = report.control;
  if (!c) {
    lines.push("XAI_API_KEY is not set in the environment; control run skipped. Set it and re-run to get a side-by-side.");
  } else {
    lines.push("| check | subscription token | API key |", "|---|---|---|");
    lines.push(`| GET /v1/models | ${cell(renderModelsList(t.modelsList))} | ${cell(renderModelsList(c.modelsList))} |`);
    const tq1 = t.q1.find((r) => r.model === c.model) ?? t.q1[0];
    lines.push(`| responses.create (${cell(c.model)}) | ${cell(fmtStatus(tq1))} ${cell(fmtDetail(tq1))} | ${cell(fmtStatus(c.q1))} ${cell(fmtDetail(c.q1))} |`);
    lines.push(
      `| x_search | ${cell(fmtStatus(t.q2))}${t.q2?.ok ? ` tool=${t.q2.toolInvoked ? "yes" : "no"}` : ` ${cell(fmtDetail(t.q2))}`} | ${cell(fmtStatus(c.q2))}${c.q2?.ok ? ` tool=${c.q2.toolInvoked ? "yes" : "no"}` : ` ${cell(fmtDetail(c.q2))}`} |`,
    );
  }
  lines.push("");
  return lines.join("\n");
}

export function renderJson(report) {
  return JSON.stringify(report, null, 2);
}

/** Final safety net: both renderings pass through here before printing. */
export function renderRedacted(report, secrets) {
  return {
    markdown: redactText(renderMarkdown(report), secrets),
    json: redactText(renderJson(report), secrets),
  };
}
