# Routine map

Status as of 8 November 2026. This file records who owns a trigger. It does not create, pause, resume, or reschedule anything. It is not a second scheduler.

The authority to fire a job is the scheduler that already owns that job. A release skill does not fire it. A merge of documentation does not change it.

## Owner snapshot, not re-probed

Supplied for this documentation pass. Not found as a cron in this repository. Not confirmed by reading a live scheduler in this pass.

| Name | Cadence in the snapshot | State in the snapshot | Where it lives |
| --- | --- | --- | --- |
| Old daily job | 09:15 | paused | outside this git tree |
| Ledger job | 09:30, Monday–Friday | active | outside this git tree |

Changing either job is out of scope. The snapshot is not a release approval and not permission to trade.

## Scheduler that is in git

| Workflow | Cron | What it is |
| --- | --- | --- |
| [`.github/workflows/gis-daily-monitor.yml`](../../.github/workflows/gis-daily-monitor.yml) | `15 6 * * *` (06:15 UTC) | GIS daily monitor. Also `workflow_dispatch`. |

That workflow is not the 09:15 job and not the 09:30 ledger job. This map does not edit it.

No other `cron:` entry was found under `.github/workflows/` on 8 November 2026.

## What not to add here

Do not paste a second copy of the release calendar into a scheduler. Decision slots are in [`docs/releases/2026-11-plan.md`](../releases/2026-11-plan.md). They do not run by themselves.
