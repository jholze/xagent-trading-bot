# Working agreement for this project

Project docs: see `README.md`, `ARCHITECTURE_PLAN.md`, `DOCUMENTATION.md` / `DOCUMENTATION.en.md`, and `docs/hermes/HERMES_DOCUMENTATION.md` for background before making non-trivial changes.

## Delegation policy: Claude plans & reviews, Grok writes code

This project uses the `grok-cc` plugin (`/plugin install grok-cc@grok-plugin-claude-code`) to delegate implementation
work to the Grok CLI. The intended split:

- **Claude (this session):** understands the request, reads the relevant code, breaks it into concrete, well-scoped
  coding tasks, decides what to delegate vs. do directly, and reviews/tests everything that comes back before it's
  considered done.
- **Grok (`/grok-cc:rescue "<task>"`):** does the actual code-writing for tasks that are substantial and well-specified
  — e.g. a multi-file feature, a large refactor, writing a new module or a batch of tests, or anything long/mechanical
  that doesn't need step-by-step judgment calls.

Keep small, exploratory, or judgment-heavy edits (a one-line fix, deciding *what* the strategy should do, anything
where the spec is still fuzzy) in Claude directly — only hand Grok tasks once they're concrete enough to describe in
a paragraph.

### Safety guardrail — this bot trades with real state

`aria_bot.py`, `execution/`, `risk/`, and the `*.live.json` files (`positions.live.json`, `orders.live.json`, ...)
touch live trading. For any Grok-delegated change that lands in those areas:

1. Never apply it directly to `main`/the running config — review the diff yourself first.
2. Run the test suite (`pytest`, config in `pytest.ini`) before accepting the change.
3. Prefer running new/changed logic against `*.demo.json` or paper/backtest paths first, not the live files.
4. If a change affects order sizing, risk limits, or execution logic, treat Grok's output as a draft to audit line by
   line, not as ready-to-merge.

Lower-stakes areas (backtesting, data fetching, notifications, docs, tests, tooling) can be delegated more freely.

### How to delegate a task

1. In Claude Code, describe the task normally — plan it out, confirm scope.
2. Hand it to Grok: `/grok-cc:rescue "<precise task description, including files/modules involved>"`
3. Review the returned diff, run `pytest`, and integrate manually if anything needs adjustment.
4. For read-only investigation/analysis instead of code changes, use `--read`.

See `/grok-cc:setup` to verify the Grok CLI is authenticated and reachable before relying on this workflow.
