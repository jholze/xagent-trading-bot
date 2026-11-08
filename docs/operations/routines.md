# Routine map

Status as of 8 November 2026. This file records who owns a trigger. It does not create, pause, resume, or reschedule anything. It is not a second scheduler.

The authority to fire a job is the scheduler that already owns that job. A release skill does not fire it. A merge of documentation does not change it.

## Owner status evidence, 8 November 2026

Supplied as the authoritative status for this documentation pass. Not found as a cron in this repository. Not re-read from the live scheduler in this pass. This map does not change either job. Reliability is not claimed.

| Job | Cadence | State on 8 November 2026 | Last recorded run | Where it lives |
| --- | --- | --- | --- | --- |
| Old daily job | 09:15 | paused | Last success 30 October 2026, 09:20:28 MEZ | outside this git tree |
| Ledger job | 09:30, Monday–Friday | reactivated 7 November 2026 | Last displayed run 29 October 2026, 09:31:31 MEZ, failed. No subsequent weekday run yet. | outside this git tree |

The 09:30 job is therefore not shown to be reliable. A reactivation date is not a successful run. Neither row is a release approval or permission to trade.

## Scheduler that is in git

| Workflow | Cron | What it is |
| --- | --- | --- |
| [`.github/workflows/gis-daily-monitor.yml`](../../.github/workflows/gis-daily-monitor.yml) | `15 6 * * *` (06:15 UTC) | GIS daily monitor. Also `workflow_dispatch`. |

That workflow is not the 09:15 job and not the 09:30 ledger job. This map does not edit it.

No other `cron:` entry was found under `.github/workflows/` on 8 November 2026.

## What not to add here

Do not paste a second copy of the release calendar into a scheduler. Decision slots are in [`docs/releases/2026-11-plan.md`](../releases/2026-11-plan.md). They do not run by themselves.
