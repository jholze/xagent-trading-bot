# CLAUDE.md

Coding-agent rules are in [AGENTS.md](AGENTS.md). That file is the source of truth for how to change this repo. Do not copy those rules here.

## Retired — do not follow

This file previously required all of the following. They are retired as of 2026-10-31:

- Claude plans and reviews; Grok (`grok-build`) writes the code.
- Claude merges non-money-path pull requests to `staging` without asking, then closes tickets and checks the deploy.
- Omnigent labels (`omnigent:ready`, `review:*`, `veto:*`) and a Ready-to-Work gate before coding.
- `rebuild/*` as the integration branch, with `staging` only at the end of a phase.

Agents open pull requests against `staging` and do not merge them. Money-path merges need the owner's `owner-approved` label (`AGENTS.md`). Do not set Omnigent labels.

## Background, not operating rules

`README.md`, `docs/umbau/README.md`, and `DOCUMENTATION.md` are project background. The spec-folder shape is `docs/requirements-engineering-guide.md` (section 1a is aligned with `AGENTS.md`; the Kay / Lena / Viktor / Omnigent chain in older copies of that section is retired). Domain thresholds and review gates are `docs/domain/`.
