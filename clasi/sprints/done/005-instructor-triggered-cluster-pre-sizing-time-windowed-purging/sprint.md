---
id: '005'
title: Instructor-triggered cluster pre-sizing + time-windowed purging
status: done
branch: sprint/005-instructor-triggered-cluster-pre-sizing-time-windowed-purging
use-cases: []
issues:
- instructor-cluster-presize.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 005: Instructor-triggered cluster pre-sizing + time-windowed purging

## Goals

Replace `Class.running` as the autoscale demand signal with an explicit
instructor action that stamps self-expiring purge-window timestamps on the
class. The cluster pre-sizes from the roster, stays protected during class,
idles down automatically, and force-removes itself at a hard cutoff — with
no instructor "stop" action required.

## Problem

`Class.running` is a sticky flag: instructors start a class and never stop
it, so it has no falling edge and is useless as a scale-down signal. The
autoscaler built in Sprint 004 has a clean demand-signal seam (`estimate_demand`)
that currently reads `Class.running`; this sprint re-points that seam to
instructor-stamped purge-window timestamps.

## Solution

1. Add `purge_after`, `purge_by`, and `target_nodes` fields to `Class` with a
   DB migration.
2. Add `POST /classes/<id>/cluster` — a non-blocking JSON route that stamps the
   timestamps and computes `target_nodes` from the roster. It only records
   intent; it never provisions inline.
3. Add a "Create my cluster" button to the class detail page with live status
   read-back.
4. Re-point `estimate_demand` in `autoscale.py` to use classes with a live
   purge window (`purge_after <= now < purge_by`) instead of `Class.running`.
5. Retune the reaper/scale-down logic with three zones: protected (before
   `purge_after`), active-purge (between the two timestamps), and dormant (at
   `purge_by`, force-remove remainder).

The autoscaler remains behind `AUTOSCALE_ENABLED=false` (the shipped default).
The `/cluster` route only stamps timestamps; it never provisions inline.
Everything is inert-by-default until an operator enables the kill-switch.

## Success Criteria

- `Class` model has `purge_after`, `purge_by`, and `target_nodes` columns with
  a passing Alembic migration.
- `POST /classes/<id>/cluster` returns JSON immediately, stamps correct
  timestamps, is idempotent on re-arm, and never touches Docker/DigitalOcean.
- The class detail page shows a "Create my cluster" button for instructors and
  reflects cluster status.
- `estimate_demand` uses purge-window classes and no longer reads `Class.running`.
- The reaper respects all three zones with 15-minute grace windows.
- Unit tests pass for all new logic (mocked DB rows, Flask test client).
- `AUTOSCALE_ENABLED` default remains `false`; merging does not auto-provision.

## Scope

### In Scope

- `Class` schema change: `purge_after`, `purge_by`, `target_nodes` + Alembic migration
- `POST /classes/<id>/cluster` route (non-blocking, idempotent, JSON)
- Class detail page button + status read-back
- `estimate_demand` re-point from `Class.running` to purge-window
- Reaper three-zone logic (protected / active-purge / dormant) in autoscale.py
  and/or node.py as appropriate
- Unit tests: schema/migration, route, demand re-point, reaper zones

### Out of Scope

- Live DO provisioning verification (DO_TOKEN expired; deferred)
- WebSocket or real-time push for cluster status (polling is sufficient)
- Changing `AUTOSCALE_ENABLED` default or enabling autoscaling in production

## Test Strategy

No live DigitalOcean available (DO_TOKEN expired 401). All tests use:
- Flask test client for route tests
- SQLAlchemy in-memory SQLite for schema/migration tests
- Mocked `Class` row dicts for `estimate_demand` unit tests
- Pure-function tests for reaper zone logic (inject `now` as a parameter)

## Architecture Notes

- `estimate_demand` is a pure function with an injected `now` parameter — the
  caller (`gather_cluster_state`) is responsible for DB I/O. Re-pointing the
  demand signal requires changing only what `gather_cluster_state` fetches.
- The reaper zones are orthogonal to scale-up; `build_plan` is extended with
  purge-window awareness only for scale-down gating.
- `Class.start_date` / `Class.end_date` are the term span (enrollment period),
  NOT a daily window. Only time-of-day from `end_date` is used for the daily
  cutoff; the `click_time + 1h` floor covers classes with a meaningless
  `end_date` time-of-day (midnight).

## GitHub Issues

(None linked yet.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Class schema — add purge_after, purge_by, target_nodes + Alembic migration | — |
| 002 | POST /classes/id/cluster route — timestamp stamping and roster sizing | 001 |
| 003 | Class detail page — Create my cluster button and status display | 001, 002 |
| 004 | Re-point estimate_demand to purge-window classes in autoscale.py | 001 |
| 005 | Reaper three-zone retune — protected, active-purge, dormant force-remove (apply_reaper_zones in autoscale.py) | 001, 004 |

Tickets execute serially in the order listed. Tickets 002, 003, and 004 all depend on 001 and can be worked after it; 005 depends on 001 and 004.
