---
name: ticket-spec
description: Use when writing or revising a GitHub issue or spec for xagent-trading-bot. Follows the repo ticket standard. Does not replace it, open an issue by itself, or set legacy approval labels.
---

# Ticket and spec

Repository procedure. Not a global skill install. The rules are the documents below. This file only says when to open them and when to stop.

## When to use

- A new backlog issue, or a revision of acceptance criteria on an existing issue.
- A spec that will be frozen before code.

Do not use it to file an issue during a documentation-only release pass unless the owner asked for that issue.

## Read first

1. [`AGENTS.md`](../../../AGENTS.md) rule 0 (docs map, Hindsight, duplicate check) and rule 2 (English titles).
2. [`docs/domain/spec-writing.md`](../../domain/spec-writing.md) — numbered `AC1…ACn`, freeze, `Rev N`.
3. [`docs/requirements-engineering-guide.md`](../../requirements-engineering-guide.md) §1a, §4 (issue body), and §5 (how an AC changes).
4. [`docs/reference/settings-features-options.md`](../../reference/settings-features-options.md) when the work touches config, flags, or Telegram commands.
5. Hindsight bank `coding-agent::trading_bot` at `http://127.0.0.1:8888`. Cite `memory:<id>` or `doc:<id>`. If the call fails, write **`RECALL_FAILED`**. Do not invent a memory id.

There is no `.github/ISSUE_TEMPLATE`. The guide's §4 fence is the body. Suggested labels in that guide are suggestions. `omnigent:ready`, `review:lena`, `review:viktor`, and `veto:*` are retired for humans. Do not set them. `scripts/kanban_status.py` still reads them; that is recorded debt, not a reason to apply them.

## Do

Apply [`docs/requirements-engineering-guide.md`](../../requirements-engineering-guide.md) §1a item 7. The body is still §4. Revisions are still §5. The spec shape is still [`docs/domain/spec-writing.md`](../../domain/spec-writing.md). Do not keep a second checklist.

A raw request, including one from Jens, is quoted in Context and stays **not Ready**. Gather code, docs, open and closed issues, Hindsight or `RECALL_FAILED`, dependencies, the goal, measurable outcomes, cost, risk, test evidence, and non-goals before asking. Ask Jens only load-bearing unknowns that remain.

Ready and done mean what item 7, §4, and §6 already say. The legacy board column Ready to Work is not that Ready. A routine paper analysis that does not change code is not forced through a new owner gate; it still cannot skip the hypothesis protocol.

The manual dry run is [`docs/releases/2026-11-08-intake-tabletop.md`](../../releases/2026-11-08-intake-tabletop.md).

## Stop

- No build-ready issue from a raw sentence.
- No new issue when a closed issue already covers it. Do not reopen #568, #569, #570, or #573. Do not treat #412's close as testnet success.
- No rewrite of #694 or #529 that drops their original acceptance criteria. An addendum is a dated revision on top.
- No new feature filed as a hotfix. No activation ticket from a negative strategy result.
- No live trading, keys, orders, or deploy in the ticket's instructions.
- Stop if the Hindsight line is missing. A silent skip is a process violation.

## Evidence

The issue names the tests and the before/after metric. A behavior change is not ready with neither.
