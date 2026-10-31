# Domain

Knowledge from the retired review roles. Numbers are the handover values. Anything marked **open** or **unknown** was not decided; do not invent it.

| Doc | Role | What it pins |
| --- | --- | --- |
| [spec-writing.md](spec-writing.md) | Spec writer | Spec before code, AC1…n, frozen tests, Rev N |
| [validation.md](validation.md) | Backtest / validation | Gates G1–G5, T4, mutation, cost and trial pointers |
| [risk-execution.md](risk-execution.md) | Risk / execution | R4 and the exit-review checks that were specified |
| [hypothesis-protocol.md](hypothesis-protocol.md) | Strategy scout | Kill rules, cost model, pre-registered trials |
| [exit-cause-rules.md](exit-cause-rules.md) | Cause rules v1.3 | How a realized loss is labeled |
| [prevention-rules.md](prevention-rules.md) | Owner, 2026-08-24 | Denylist and DCA guards |
| [../observability/observe-streams.md](../observability/observe-streams.md) | Observe | Mongo collections for shadow and observe JSONL |

Operating rules for agents are [AGENTS.md](../../AGENTS.md). The coded hypothesis checks are `tools/hypothesis_protocol.py`. The merge gate is `scripts/ci/review_gates.py`.
