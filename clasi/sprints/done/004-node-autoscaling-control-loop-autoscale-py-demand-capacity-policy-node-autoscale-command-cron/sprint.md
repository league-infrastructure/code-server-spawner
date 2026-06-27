---
id: '004'
title: "Node autoscaling control loop \u2014 autoscale.py, demand/capacity policy,\
  \ node autoscale command + cron"
status: done
branch: sprint/004-node-autoscaling-control-loop-autoscale-py-demand-capacity-policy-node-autoscale-command-cron
use-cases:
- SUC-001
- SUC-002
- SUC-003
issues:
- node-autoscaling-control-loop
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 004: Node autoscaling control loop — autoscale.py, demand/capacity policy, node autoscale command + cron

## Goals

Deliver the autoscaling control loop that automatically sizes the swarm worker pool
to match class demand. The loop is pure decision functions + thin orchestrator,
triggered by `cspawnctl node autoscale` from cron. Ships inert by default
(`AUTOSCALE_ENABLED=false`, `AUTOSCALE_DRY_RUN=true`) so no nodes are created or
destroyed until an operator explicitly enables it.

## Problem

All node scaling today is manual: `node expand`, `node contract`, `node stop`. Idle
nodes accumulate cost, and class-start can under-provision. The `/cron/*` HTTP
endpoints are empty stubs with a 5s curl timeout — unsuitable for minutes-long
provisioning. The cron-driven `cspawnctl` pattern (shown by the commented `host reap`
line in `docker/crontab`) is the intended mechanism but has never been wired for
scaling.

## Solution

- New `cspawn/cs_docker/autoscale.py`: pure decision functions (unit-testable, no I/O)
  + thin orchestrator that calls the sprint 003 provisioning primitives.
- Demand signal: `max(live_load + pending, prescale_roster_estimate) + HEADROOM`, where
  prescale = `ceil(len(students) * ROSTER_FRACTION)` over running classes still before
  their stop time.
- Scale-up: greedy bin-pack across large/small tiers; scale-down: purge-first then
  empty-node removal with cooldown/hysteresis; never both in the same cycle.
- `node autoscale` CLI command with `--dry-run` / `--force` / `--up-only` / `--down-only`.
- `AUTOSCALE_*` config keys in all three public.env files; cron line added (commented
  out, matching the existing `host reap` pattern).
- Unit tests for all pure functions; thin mocked coverage for orchestrator.

## Success Criteria

- `cspawnctl node autoscale --dry-run` executes and prints a structured log line with
  demand / capacity / plan fields without touching Docker or DO.
- All pure functions (`estimate_demand`, `compute_deficit`, `plan_scale_up`,
  `plan_scale_down`, `build_plan`) pass unit tests with no Docker/DO/DB infrastructure.
- Merging to master with the default config causes zero provisioning or destruction.
- The cron line is present in `docker/crontab` but commented out.

## Scope

### In Scope

- `cspawn/cs_docker/autoscale.py` with dataclasses `NodeView`, `ClusterState`,
  `ScalePlan` and all pure decision functions and orchestrator functions.
- Extraction of `count_hosts_per_node` and `graceful_remove_node` from `node.py` so
  both `stop_node` and `apply_plan` share the same drain/remove/destroy path.
- `node autoscale` CLI command in `cspawn/cli/node.py`.
- `AUTOSCALE_*` config keys in `config/prod/public.env`, `config/local-prod/public.env`,
  `config/devel/public.env`.
- Cron wiring: commented-out line in `docker/crontab`.
- `test/test_autoscale.py` unit tests for pure functions; thin mocked coverage for
  orchestrator.
- Demand signal uses existing `Class.running` / `Class.students` / `Class.stops_at` and
  `CodeHost.is_mia` / `CodeHost.is_purgeable`.

### Out of Scope

- `Class.purge_after` / `Class.purge_by` timestamp fields (next sprint:
  instructor-cluster-presize).
- `POST /classes/<id>/cluster` route and instructor UI button.
- Replacing the `estimate_demand` signal with purge-window demand (next sprint).
- Consolidation drain (migrating live student sessions to pack nodes).
- Multi-manager HA lock mechanism — single-manager `flock` is sufficient for now.

## Test Strategy

Pure functions in `test/test_autoscale.py` need no infrastructure: plain dicts in,
plain dataclasses out. Cover all boundary conditions listed in the issue (D=1, D=7,
D=20; cooldown skipping; MIN_WORKER_NODES floor; `build_plan` never-both-up-and-down).
Thin mocked coverage for `gather_cluster_state` and `apply_plan` using
`unittest.mock.MagicMock` for docker and digitalocean clients. No live DO/Docker calls
in CI.

## Architecture Notes

- `estimate_demand` is designed as a clean, swappable function because the next sprint
  (instructor-cluster-presize) will re-point its demand source from `Class.running` to
  purge-window timestamps. The signature `estimate_demand(classes, hosts, cfg) -> int`
  keeps all I/O in the caller (`gather_cluster_state`), so the swap is a drop-in.
- Safety invariant: `AUTOSCALE_ENABLED=false` and `AUTOSCALE_DRY_RUN=true` are the
  shipped defaults. `run_autoscale` checks the kill-switch before gathering any state.
- Locking: `fcntl.flock` on a fixed path under the container's writable volume. Single
  manager, so no distributed locking is needed this sprint.
- Scale-down never drains a node that has running hosts. Only nodes with `running_hosts
  == 0` (live Swarm count, re-checked immediately before draining) are eligible.

## GitHub Issues

(None linked yet — issue tracked as `.clasi/issues/node-autoscaling-control-loop.md`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Config keys and node.py helper extraction | — |
| 002 | Pure decision functions and dataclasses | 001 |
| 003 | Unit tests for pure functions | 002 |
| 004 | Orchestrator: gather_cluster_state and apply_plan | 002 |
| 005 | node autoscale CLI command and cron wiring | 004 |

Tickets execute serially in the order listed.
