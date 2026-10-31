# Requirements Engineering Guide

Version: 1.1 · Date: 2026-10-23 · Status: approved for use · Owner: Jens Holze

This guide covers how to go from an idea to tested, agent-ready tickets in this repo.
It is generic. One short section at the end covers trading-specific prompts.
Assistants and coding agents follow it. Humans may follow it.

---

## 1. Core principles

1. **Never invent facts.** If something is unknown, log it as `[OPEN Q-n]`. If you guess, tag it `[ASSUMPTION A-n]` and name an owner to confirm it.
2. **Stop after every stage.** Each stage ends with a short summary and an explicit "OK to continue?". Do not start the next stage without the owner's OK.
3. **Ask little, ask well.** Ask at most 3–5 prioritized questions per round, highest impact first. Give a proposed default for each: "If you don't know: I'd assume X."
4. **Separate what from how.** Requirements say what and why. Design says how.
5. **Everything has a stable ID and traces end to end:** goal → story → requirement → test → task.
6. **Non-goals are explicit.** Every spec and every ticket states what is out of scope and which areas must not be touched.
7. **Mark the existing system.** Every story or requirement is `exists` (cite file/module), `adapt` (say what changes) or `new`.
8. **Specs are versioned Markdown in the repo** under `specs/<feature>/`.
9. **Write for agents.** Requirements must be self-contained: no "as above", concrete paths, stated constraints.

---

## 1a. How this repo applies the guide

These adaptations are binding for this repo. Where they differ from the generic text below, this section wins.

1. **Owner OK per stage.** For work that targets `staging`, the lead (Kay) gives the stage OK. Only these need Jens' explicit yes: money, API keys, the live switch, merges to `main`, the henry account and the small-live config. Agents must not ask Jens for routine stage approvals.
2. **Reviews.** The "two independent reviews" in the merge gate (section 6) are:
   - Lena: backtest/validation (label `review:lena` or `veto:lena`).
   - Viktor: risk/execution (label `review:viktor` or `veto:viktor`).

   Reviews start only after CI is green. The first review is one combined pass that lists all blockers; after that, delta reviews only. At most two spec revisions; anything still open is settled in the Daily.
3. **Ready chain.** Marcus writes the spec with Given/When/Then acceptance tests from the start, each with a fixture and a mutant that must turn the test red. Then `review:lena`, then `review:viktor`, then Kay sets `omnigent:ready`.
4. **Bundles.** Bugs with the same cause may ship in one PR. Each bug still needs its own test that is red without the fix, and one mutant per bug.
5. **Larger strategy or logic changes** need measurement first: a backtest or shadow run against the current logic, with costs and walk-forward, plus PASS from Lena and Viktor. Repair existing functions instead of building parallel paths. No hardcoded values or coin names.
6. **Explicit exclusions from section 8.** This project deliberately has **no daily-loss cap**, **no emergency brake / kill switch** for automatic trading halts, and **no fixed max-open-positions number**. Do not propose them as NFRs.

---

## 2. Process overview

| Stage | Output file | Purpose | Exit (needs owner OK) |
|---|---|---|---|
| 0 Intake | `00-intake.md` | Understand the problem, roles, sources, scope edges | Problem confirmed; roles + sources known; rough scope/non-goals/appetite known or logged |
| 1 Goals | `01-goals.md` | Vision, goals `G-n` with metrics, non-goals | Goals measurable; non-goals agreed |
| 2 Story map | `02-story-map.md` | Backbone, stories, release slices | All roles walked; R0 + R1 defined and approved |
| 3 Domain | `03-domain.md` | Events, glossary, states, example maps | No conflicting terms; every Must story has rule + example |
| 4 Requirements | `04-requirements.md` | FR/NFR/AC with IDs, traceability | All Must FRs have ACs incl. error case |
| 5 Design | `05-design.md`, `adr/NNNN-*.md` | Architecture, data, interfaces, ADRs | Each significant decision has an ADR |
| 6 Review | `06-review.md` | Quality review with findings | Verdict Approve / Approve with minor fixes |
| 7 Tasks | `07-tasks.md` | Waves of small, agent-ready tasks | Every Must FR/AC covered; owner OK before creating issues |

**Quick path for small features:** 0 → 4 → 6 → 7.
Use it when there is one role, no non-trivial domain rules and no architecture decision.
In the quick path, the intake carries goals and non-goals. The requirements file carries a short traceability table.
If a hidden domain question or design decision shows up, go back and add stage 3 or 5.

**Fuzzy ideas:** before stage 0, run a short "grill" interview (§3.1).

**Stop protocol after each stage:**
```
Stage <n> done: <file> v<x>.
Knowns: …   Assumptions: A-…   Open (blocking): Q-…
Next: stage <n+1>. OK to continue?
```

---

## 3. Stage guidance

### 3.0 Spec folder structure

```
specs/<feature>/
  README.md            # index: files, stage, version, status; decisions log; changelog
  00-intake.md
  01-goals.md
  02-story-map.md
  03-domain.md
  04-requirements.md
  05-design.md
  adr/0001-<decision>.md
  06-review.md
  07-tasks.md
```
Use `NN-` prefixes consistently. Quick-path specs contain only the files they use.
`specs/README.md` lists all features.

Every file starts with this header:
```
# <Feature> – <Artifact>
Version: 0.x · Date: YYYY-MM-DD · Status: draft | in review | approved · Owner: <name>
```
Every file ends with a changelog (§5).

### 3.1 Grill (optional, before stage 0)
Use this for ideas that are still vague. Do not use it for one-line tasks that are already specified.
- Ask one or two questions at a time. Do not dump a questionnaire.
- Cover only what is missing: what done looks like and by when; what is out of scope; who decides, who executes, what stays human; what must never happen (money, live systems, external parties); what number or experiment proves it worked.
- Push on vagueness. Ask for the concrete example, the number, the name.
- Finish with a 5–8 line shared picture: goal, non-goal, owner, first cut, hard stops. Get it confirmed, then stop.

### 3.2 Stage 0 – Intake
1. Restate the request in 2–3 sentences and ask for confirmation.
2. Ask by area, and skip what is already known:
   - *Trigger & problem:* Why now? What happens if we do nothing?
   - *Outcome:* Which success signal or metric? Deadline or appetite?
   - *People:* Roles, decision makers, affected third parties.
   - *Current state:* Existing code, modules, data, integrations, earlier attempts.
   - *Scope edges:* What is explicitly out? What must not break?
   - *Constraints:* Stack, hosting, compliance, budget, volumes.
   - *Quality:* Rough numbers for performance, availability, security.
   - *Risks:* Biggest worry, known rabbit holes.
3. Summarize after each round.

```markdown
# Intake: <Feature> — v0.1 (<date>, status: draft)
## Problem (confirmed)
## Desired outcome / success signals
## Appetite / deadline
## Roles & stakeholders
| Role | Goal | Decision power | Contact |
## Current state & sources
| Source | Type (repo/doc/system/person) | Access | Notes |
## Scope (rough) / Non-goals
## Constraints
## Quality expectations (rough)
## Risks
## Assumptions
- [ASSUMPTION] A-1 … (confirm: <owner>)
## Open questions
| ID | Question | Owner | Blocks | Due | Answer |
## Glossary (seed)
| Term | Meaning |
```

### 3.3 Stage 1 – Goals
```markdown
# <Feature> – Goals
## Vision (1–2 sentences)
## Goals
| ID | Goal | Metric / target | Measured how |
| G-1 | … | … | … |
## Non-goals
- NG-1 … (why it is out)
## Open questions / assumptions
```

### 3.4 Stage 2 – Story map
1. List the roles. Start with the role that matters most to success.
2. Write the backbone left to right in narrative order: **activities** (`A1…`) → **user tasks** (`T1.1…`, verb phrases).
3. Walk the flow again for every other role. Mark hand-offs and ask: "Which role is missing here?"
4. Hang stories under tasks, including variants, error cases and admin needs. Format: `As a <role> I want <capability> so that <benefit>`. Mark each story `exists` / `adapt` / `new`.
5. Order the stories top-down by necessity.
6. Slice the map. **R0 walking skeleton** is the thinnest end-to-end flow for the primary role. Each slice after it (R1, R2…) has a named outcome. Check every slice for role coverage.
7. Give each story an ID: `ST-<activity>.<task>.<n>`.

```markdown
## Map
| Slice | T1.1 | T1.2 | T2.1 |
|---|---|---|---|
| **R0** – outcome: … | ST-1.1.1 … (new) | ST-1.2.1 … (exists) | ST-2.1.1 … |
| **R1** – outcome: … | ST-1.1.2 … | | ST-2.1.2 … (adapt) |
## Role coverage per slice
| Role | R0 | R1 | Later | Deferred needs |
```

### 3.5 Stage 3 – Domain (event storming light + example mapping)
1. List **domain events** in the past tense, in timeline order. Mark unclear spots with ⚠.
2. For each event, add the **command**, the **actor or policy** ("whenever X then Y"), the **external system** and the **information** needed to decide.
3. Find the **boundaries**: entities or aggregates, and contexts. Mark each `exists` / `adapt` / `new`.
4. Write a **glossary**: one meaning per term, plus synonyms to avoid.
5. Draw a **state diagram** (`stateDiagram-v2`) for each key entity. Every transition is a command or event.
6. Build an **example map** for each key story: rules → concrete examples with data → questions. Many questions mean the story is not ready. Many rules mean the story should be split.
7. Turn the examples into Given-When-Then drafts for stage 4.

### 3.6 Stage 4 – Requirements
Work one section group at a time and get an OK per group on large specs.

**IDs (stable, never reused):**
`G-` goal · `ST-` story · `FR-nnn` functional · `NFR-nnn` non-functional · `AC-nnn<letter>` acceptance criterion bound to an FR (e.g. `AC-012b (FR-012)`) · `C-` constraint · `A-` assumption · `Q-` question · `R-` risk · `ADR-NNNN` decision · `T-nnn` task.

**Writing rules**
- One requirement is one testable statement. Use "shall".
- Avoid these words: fast, easy, user-friendly, flexible, robust, appropriate, usually, etc., and/or, some, many, TBD, should, may, support (unspecified).
- Write FRs with **EARS** patterns:
  - Ubiquitous: `The <system> shall <response>.`
  - Event: `When <trigger>, the <system> shall <response>.`
  - State: `While <state>, the <system> shall <response>.`
  - Optional: `Where <feature is included>, the <system> shall <response>.`
  - Unwanted: `If <unwanted condition>, then the <system> shall <response>.`
  - A section with no `If…then` requirement is probably missing error handling.
- Write ACs as **Given-When-Then** with concrete data. Every Must needs at least one happy path and at least one edge or error case.
- Write NFRs per **ISO/IEC 25010:2023**: functional suitability, performance efficiency, compatibility, interaction capability, reliability, security, maintainability, flexibility, safety. Each NFR needs a metric, threshold, condition and verification method.
- Set priority with **MoSCoW**. Keep Must at or below ~60 % of the effort. Use RICE only to rank Should/Could items.

~~~markdown
# <Feature> – Requirements Spec
Version: 0.x · Date: … · Status: … · Owner: …
## 1. Context & problem
## 2. Goals & success metrics
## 3. Non-goals
## 4. Roles
## 5. Scope by release
| Release | Outcome | Stories |
## 6. Functional requirements
| ID | Requirement (EARS) | Story | Prio | Status | Notes |
|---|---|---|---|---|---|
| FR-001 | When …, the system shall … | ST-1.1.1 | Must | new | |
| FR-002 | If …, then the system shall … | ST-1.1.1 | Must | adapt (`path/module.py`) | |

**AC-001a (FR-001)**
```gherkin
Given <precondition with concrete data>
When <action>
Then <observable result>
```

## 7. Non-functional requirements
| ID | Characteristic | Requirement | Metric & threshold | Verification |
## 8. Constraints & interfaces
## 9. Data & migration
## 10. Traceability
| Goal | Story | FR/NFR | AC / test | Task |
## 11. Assumptions
## 12. Open questions
| ID | Question | Owner | Blocks | Due | Answer |
## 13. Risks
| ID | Risk | Likelihood | Impact | Mitigation |
## Changelog
~~~

**Traceability:** the matrix in §10 must have no orphans in either direction. That means no goal without a story, no story without an FR, no FR without an AC, no AC without a test, and no task without a requirement (setup and polish tasks are the only exceptions). The "AC / test" column names the test, e.g. `tests/test_x.py::test_ac_001a_<slug>`.

### 3.7 Stage 5 – Design + ADRs
```markdown
# <Feature> – Design · v0.x
## Context & scope   ## Goals / non-goals (link G-, FR-)
## Overview (diagram)   ## Components & responsibilities (FR mapping)
## Data model   ## APIs / interfaces   ## Error handling
## Cross-cutting: security, privacy, observability, performance
## Alternatives considered (trade-offs)
## Test strategy   ## Rollout & migration   ## Open questions
```
Write one ADR per significant decision:
```markdown
# ADR-NNNN: <title>
Status: proposed | accepted | superseded by ADR-XXXX · Date:
## Context (forces, constraints, linked FR/NFR)
## Decision ("We will …")
## Consequences (positive, negative, neutral)
```
When you score alternatives, use a small decision matrix: criteria × options, with weights and a short rationale. Say which criteria are the owner's call.

### 3.8 Stage 6 – Quality review
The reviewer's job is to find defects, not to quietly rewrite the spec. Every finding cites an ID and proposes a fix.
1. **Inventory:** count the goals, stories, FRs, NFRs, ACs and open questions. Note the version reviewed.
2. **Check each requirement against ISO/IEC/IEEE 29148:** necessary, appropriate, unambiguous, complete, singular, feasible, verifiable, correct, conforming.
3. **Check the whole set:** complete (all roles, error paths, NFR categories, no TBD without an owner and due date), consistent (no conflicts, one term means one thing, consistent units), feasible against the appetite, validatable against the goals.
4. **Check the stories against INVEST.**
5. **Run these scans:** ambiguity words, contradictions, testability, traceability gaps, exists/adapt items without a concrete location, agent readiness (non-goals, paths, no "see above"), assumptions without an owner.
6. **Classify each finding:** Critical (blocks implementation or makes wrong behavior likely), Major (rework likely), Minor (clarity).
7. **Give a verdict:** Approve / Approve with minor fixes / Revise. Apply fixes only after the owner agrees, then bump the version.

```markdown
# Review of <spec> v0.x — <date>
Verdict: …
| # | Severity | Item | Criterion | Finding | Proposed fix |
## Traceability gaps   ## Blocking open questions   ## Strengths (keep)
```

### 3.9 Stage 7 – Tasks
1. Confirm the inputs: the approved spec and design versions, and the target slice (start with R0).
2. Phase the work in dependency order:
   - Setup
   - Foundational: shared models, migrations, auth, interfaces
   - One vertical slice per story: data → logic → API/UI → tests
   - Polish: cross-cutting NFRs, docs
3. Size each task as about one PR, reviewable in under 30 minutes, with a bounded set of files. Split any task with more than ~5 acceptance checks or one that crosses contexts.
4. List the dependencies. Mark tasks that can run in parallel with `[P]`. Group the tasks into **waves**.
5. Coverage check: every Must FR/AC has at least one task.
6. Put repo-wide guidance (commands, conventions, boundaries) in `AGENTS.md`, not in every task.
7. Present the list. Create issues only after an explicit OK.

```markdown
## Waves
| Wave | Tasks |
| 1 | T-001 |
| 2 | T-002 [P], T-003 [P] |
## Coverage
| FR/AC | Tasks |
```

---

## 4. Ticket style (GitHub issues)

Write tickets in **English**. Each ticket must stand on its own, because agents may not open the spec.

~~~markdown
Part of #<epic>
Stable ID: T-nnn · Epic: <name> · Phase/wave: <…> · Parallel: [P] yes/no · Depends on: T-…
Implements: FR-…, AC-…, NFR-… · Marker: exists|adapt|new · UI: yes|no

## Context
Why the ticket exists and where it fits in the flow (2–4 sentences). Related or superseded issues.

## Technical context
Relevant files/modules with markers, interfaces, data, constraints, prior decisions (ADR-…).

## Scope
- In scope: `path/to/module` (adapt), `path/to/new_file` (new)
- Non-goals / do not touch: …

## Spec references
Source: specs/<feature>/07-tasks.md · FR-…/NFR-…/AC-… · mockups

## Acceptance criteria
*AC-004a (FR-004)*
```gherkin
Scenario: <happy path>
  Given <concrete data>
  When <action>
  Then <observable result>
```
*AC-004b (FR-004)*
```gherkin
Scenario: <error / edge case>
  Given …
  When …
  Then …
```

## Verification
- Tests to add: `tests/test_x.py::test_ac_004a_<slug>`, …
- Run: `<lint/test command>` → expected: all green
- UI only: check the viewports (e.g. 1280×800 desktop, 390×844 mobile), with no overflow and contrast ≥ 4.5:1

## Definition of done
- [ ] Every AC passes as an automated test (one AC → one test where possible, no skips for core ACs)
- [ ] No changes outside scope
- [ ] Spec/traceability updated if behavior deviates (flag it, don't decide it)

## Changelog
| Version | Date | AC | Change | Reason | Approved by |
~~~
Each ticket should have many concrete scenarios: the happy path, each error path, the boundary values and the permission cases.
Each scenario links to a requirement ID.
Name each test after its AC ID.

Suggested labels: `spec:<feature>`, `release:R0`, `type:feature|chore|test`, `agent-ready`.

---

## 5. Changing acceptance criteria (mandatory)

When an AC changes after it was first written:
- **Changed:** `AC-004a [CHANGED v1.3 2026-10-22: one-line reason – what shifted and why]`, with the old text struck through (`~~…~~`) on the line below.
- **New:** `AC-004c [NEW v1.3 2026-10-22: reason]`.
- **Removed:** keep the ID, strike the text, add `[REMOVED v1.3 2026-10-22: reason]`.
- **Never reuse an ID.** A reason is always required.
- Every spec and every ticket ends with this table:
  `| Version | Date | AC | Change | Reason | Approved by |`
- Markers stay until the next approved release. After that they live only in the changelog.
- A `[CHANGED]` or `[NEW]` AC counts as **untested**. It needs a passing test on the final PR head before merge, and reviewers must flag any marked AC that has none.
- On GitHub tickets, also post a short comment: `AC change: AC-xxx …`.

Spec files also keep a simple file changelog: `| Version | Date | Change |`. Bump the version on every approved change.

---

## 6. Merge gate

A PR merges only when **all** of these hold on the **final head SHA**:
- [ ] Linked issue (`Closes #n`, or `Part of #n`); one issue per PR; the PR template is fully filled.
- [ ] **CI green** (all required checks).
- [ ] **Two independent reviews from different models** both PASS on that SHA. Any unresolved BLOCK means no merge.
- [ ] A **tester agent** ran the named test commands on that SHA and reported the counts. **"Not run" counts as fail.** Core ACs may not be skipped. Any AC that is still open is listed explicitly.
- [ ] Every `[CHANGED]` / `[NEW]` AC has a passing test.
- [ ] UI changes: viewport checks green.
- [ ] Scope respected; spec/traceability updated or the deviation flagged.
- [ ] Branch up to date with base, and generated artifacts (lockfiles, schemas) regenerated.
- [ ] Human test instructions included in the PR.
- [ ] Merges to the default branch happen only with the owner's explicit yes. Never push to the default branch directly.
- [ ] Merges to `staging` follow section 1a: Kay merges after Lena + Viktor PASS and green CI. Merges to `main` need Jens' explicit yes.

---

## 7. Asking questions

- Ask 3–5 questions per round at most, ordered by how much each answer would block or change.
- Format each one as `Q-n: <question> — Default if no answer: <proposal> (blocks: stage/FR)`.
- Log every question in the open-questions table with an owner and due date. Do not lose questions in chat.
- Don't re-ask anything already settled in the spec, the repo docs or earlier answers. Read first.
- If an answer is missing, carry the default as `[ASSUMPTION A-n]` and say so in the stage summary.

---

## 8. Trading-specific prompts

> **Repo exclusions:** no daily-loss cap, no kill switch / emergency brake, no fixed max-open-positions number (see section 1a, item 6). Skip these prompts below.

> **[SUGGESTION]** These prompts are checklists to consider during stages 0, 4 and 6. They are not established requirements or facts about this project. Turn an item into an NFR only after the owner confirms it, with concrete thresholds. Until then, log it as `[OPEN]` or `[ASSUMPTION]`.

| Topic | Questions to ask | Example NFR shape (thresholds are placeholders) |
|---|---|---|
| Risk limits | Max position size, max exposure per instrument/account, max daily loss, max order count/rate? | `If a new order would exceed the max position of <X>, then the system shall reject it and log the reason.` |
| Kill switch | Who can stop all trading, how fast, and what happens to open orders and positions? | `When the kill switch is activated, the system shall cancel open orders and stop new orders within <N> s.` |
| Paper before live | Required paper-trading period or criteria before live? Who approves going live? | `The system shall not send live orders unless live mode is enabled by <role> and paper-mode criteria <…> are met.` |
| Idempotent orders | How are retries, timeouts and restarts handled without duplicate orders? Client order IDs? | `If an order request times out, then the system shall reconcile by client order ID before any resend.` |
| Latency | End-to-end signal-to-order budget? Does it really matter for the strategy? | `p95 signal-to-order latency < <N> ms under <load>`, verified by measurement |
| Audit log | What must be recorded (signals, decisions, orders, fills, config changes, manual actions)? Retention? Tamper evidence? | `The system shall append every order event with timestamp, ID and cause to an append-only log.` |
| Backtest reproducibility | Same data + same config + same code version → same result? Data versioning, seeds, fees/slippage model? | `Given dataset <hash> and config <hash>, a backtest shall produce identical trades on rerun.` |
| Secrets handling | Where do API keys live? Least-privilege keys (e.g. no withdrawal rights)? Rotation? Never in logs or repo? | `The system shall never write API secrets to logs, specs, tickets or the repository.` |
| Also consider | Exchange/broker outages and rate limits, clock sync, partial fills, market hours, data feed gaps, reconciliation of positions vs broker, alerting, regulatory/tax constraints | — |

Make sure every trading spec contains `If…then` requirements for at least these cases: a broker or exchange error, missing or stale market data, a breached risk limit, and a restart while orders are open.

---

## Changelog
| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-10-22 | Initial guide distilled from internal spec workflow skills and agent dev standard |
| 1.1 | 2026-10-23 | Repo adaptations (section 1a), staging base |
