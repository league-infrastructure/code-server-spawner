---
id: 008
title: "Stale swarm node references — treat NotFound as MIA, detect and repair\
  \ orphaned tasks"
status: planning-docs
branch: sprint/008-stale-swarm-node-references-treat-notfound-as-mia-detect-and-repair-orphaned-tasks
use-cases: []
issues:
- stale-swarm-node-references-break-host-operations.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 008: Stale swarm node references — treat NotFound as MIA, detect and repair orphaned tasks

## Goals

- Never let a stale Swarm node reference (a `NodeID` the autoscaler has since
  destroyed) surface as an uncaught `docker.errors.NotFound` to any caller —
  push, sync, CLI inspection, or anything else that resolves a code host's
  container/node.
- Make a stale-node host **visible** as broken: `to_model()`/`sync_to_db()`
  must mark it `MIA` instead of leaving `state=running` forever, so `host ls`,
  the admin UI, and the existing purge/reap flows see the truth.
- Confirm (and document) that the **existing** `host purge` / reaper /
  `stop_host()` machinery (sprint 007) is sufficient to clean up a
  MIA-marked stale-node host — no new "repair" command is needed if
  detection is correct.
- Find and fix the root cause that lets a task survive its node's
  destruction with a permanently unresolvable `NodeID`, and apply a cheap,
  targeted prevention.

## Problem

See `clasi/issues/stale-swarm-node-references-break-host-operations.md`.
In production (`local-prod`, observed 2026-07-02), host `gavin-morris`'s
Swarm task references node `qz2s99p6zza5t7nc40wz6amdr`, which no longer
exists (`cspawnctl node info` shows only `swarm1/2/4/5`). Every code path
that resolves that task's node — `CodeHostRepo.push()`
(`cs_github/repo.py:113`), `CSMService.to_model()`
(`cs_docker/csmanager.py:225-226`), and, as this sprint's sweep confirmed,
several more (see Architecture Notes) — either raises a raw
`docker.errors.NotFound` or silently produces a wrong-but-plausible answer.
`host push --all` isolates the crash to one host per run (sprint 007's
`_push_all` subprocess-per-host design), so it is not catastrophic, but it
never resolves, is noisy, and — critically — `to_model()` never demotes the
host's `state` away from Swarm's stale "running" task status, so `host ls`
keeps reporting it healthy indefinitely. The stakeholder reports this is a
recurring class of failure, not a one-off.

**Root cause (confirmed by reading the code, not live clusters — see
Architecture Notes for the full trace):** `cli/node.py`'s
`_pin_service_to_node()` (used by `node rebalance`) sets a hard
`node.hostname==<fqdn>` Swarm placement constraint on a service. If that
specific node is later destroyed by `graceful_remove_node()` (used by
`node stop`, `node contract --force-drain`, and the automated autoscale
scale-down path) or by `node stop --force` (no drain at all), Swarm's
drain step cannot reschedule the hard-pinned task onto any other node — no
other node satisfies the constraint — so the task is never superseded by a
new one. Its last-known `Status.State` (`"running"`) and stale `NodeID`
persist in Swarm's task history forever, even after the droplet is gone.
The automated autoscale path is protected against this today (it only ever
removes nodes with a live, double-checked `running_hosts == 0`), so the
two exposed vectors are the manual `node contract --force-drain` and
`node stop --force` escape hatches.

## Solution

1. **Defensive handling at the source**: fix the one place in the codebase
   that calls `client.nodes.get(<id>)` (a single-node fetch, which is what
   404s) — `Service.containers` in `cspawn/cs_docker/proc.py` — to catch
   `docker.errors.NotFound`, log loudly, and skip that task instead of
   raising. Every consumer (`to_model()`, `CodeHostRepo`, `StudentRepo`,
   `HostS3Sync`, `cli host cont`) already goes through this one generator,
   so this single fix propagates safety everywhere without scattering
   try/excepts. Add a cheap, explicit `Service.node_missing` property
   (backed by `nodes.list()`, never `nodes.get()`, so it can't itself
   404) so callers can distinguish "task exists but its node is gone" from
   "no task yet" — the two cases must not be conflated, or a freshly
   starting host would be wrongly marked MIA.
2. **Detection**: `CSMService.to_model()` consults `node_missing` and,
   when true, marks the resulting `CodeHost` row `state=mia`,
   `app_state=mia` — overriding Swarm's stale "running" status — instead
   of leaving it looking healthy. `to_model()`/`sync_to_db()` is already
   called unconditionally by `host ls` for every live service and by
   `sync()` for not-yet-settled hosts, so this self-heals existing broken
   rows the next time an operator runs `host ls` or `host purge`; no
   backfill migration is needed.
3. **Repair**: once correctly marked MIA, the host is already picked up by
   `host purge`'s existing `is_mia or is_quiescent` filter and by the
   reaper — no new repair command is required. `stop_host()` (sprint 007)
   already skips the doomed push for a MIA host and removes the Swarm
   service via a pure service-ID delete, which does not require resolving
   the dead node — so cleanup Just Works once detection is correct. This
   sprint verifies and locks that chain in with tests; it documents
   "student/admin restarts the host" (a normal `new_cs()`, scheduled onto
   a live node) as the recovery path for the student, since the old
   container is unreachable and not recoverable in place.
4. **Root-cause prevention**: before a node-removal path
   (`graceful_remove_node()`, `node stop --force`) tears the node down,
   strip any `node.hostname==<fqdn>` constraint from services still
   pinned to it, so Swarm's scheduler is free to reschedule the task
   elsewhere instead of leaving it permanently stuck. Cheap, targeted,
   same file as the pinning logic it complements.

## Success Criteria

- `Service.containers` never raises `docker.errors.NotFound`; a stale
  node reference is logged and the affected task is skipped.
- A service whose only task's node no longer exists is marked
  `state=mia`, `app_state=mia` by `to_model()` — and a genuinely
  just-created service (no task yet) is *not* marked MIA.
- `CodeHostRepo.push()`/`pull()`, `StudentRepo`, and `HostS3Sync` raise a
  clear, typed `ValueError` naming the stale-node condition instead of a
  raw `docker.errors.NotFound` — verified for all named call sites.
- `cspawnctl host cont <name>` on a stale-node host prints a clean message,
  not an unhandled exception.
- `host purge` (and the reaper, and `stop_host()`/`remove_all()`) cleans
  up a MIA-marked stale-node host exactly like any other MIA host, with no
  code changes to the purge/reaper filters themselves.
- `graceful_remove_node()` and `node stop --force` clear any
  `node.hostname==<fqdn-being-removed>` pin from affected services before
  the node disappears.

## Scope

### In Scope

- `cspawn/cs_docker/proc.py`: `Service.containers` NotFound handling,
  new `Service.node_missing` and `Service.first_container()`.
- `cspawn/cs_docker/csmanager.py`: `CSMService.to_model()` MIA-marking.
- `cspawn/cs_github/repo.py`: `CodeHostRepo._get_service_container()`,
  `push()` (unchanged body, hardened transitively), `pull()` (also fixes
  a pre-existing unrelated bug — see architecture-update.md), `StudentRepo
  ._get_service_and_container()`.
- `cspawn/util/host_s3_sync.py`: `HostS3Sync.get_service_and_container()`.
- `cspawn/cli/host.py`: `cont` command.
- `cspawn/cli/node.py`: `graceful_remove_node()`, `stop_node --force` —
  clear stale hard-pins before node removal.
- Tests for all of the above, following the conventions in
  `test/test_stop_host.py`, `test/test_stop_call_sites.py`,
  `test/test_cli_stop_paths.py`, `test/test_autoscale.py`,
  `test/test_node_contract.py`, `test/test_node_rebalance.py`.

### Out of Scope

- Redesigning the autoscaler's node-selection/removal policy beyond the
  targeted unpin fix (e.g., preventing `--force-drain`/`--force` from ever
  touching a loaded node at all is a bigger policy change than "cheap
  prevention" calls for — flagged as an open question).
- A dedicated "repair"/"reschedule-in-place" CLI command. This sprint's
  finding is that in-place Swarm rescheduling is unnecessary: the existing
  `host purge`/reaper flow already fully removes the orphaned service once
  detection is correct, and the student's own next login re-creates a
  fresh, healthy host via `new_cs()`.
- Extending `sync()`'s "not-ready" filter to periodically re-verify
  already-`ready` hosts (would catch this drift without requiring an
  operator to run `host ls`, but changes the cost profile of a cron'd
  job) — flagged as an open question for stakeholder input.
- Pre-existing, unrelated bugs this sweep incidentally surfaced beyond
  `CodeHostRepo.pull()`'s `_get_container()` typo (which is fixed as a
  one-line side effect of hardening the same method group) — none others
  were found.

## Test Strategy

Unit tests only, no live Docker/Swarm/DigitalOcean access — following the
existing in-memory-SQLite + `MagicMock` pattern from `test_stop_host.py`
and the mocked-docker-client pattern from `test_autoscale.py` /
`test_node_contract.py` / `test_node_rebalance.py`.

- **`proc.py` (`Service.containers`, `node_missing`, `first_container`)**:
  mock `manager.client.nodes.get()` to raise `docker.errors.NotFound` for
  one task and succeed for another in the same service; assert the
  generator skips the stale one without raising, `node_missing` is `True`
  only when a task's `NodeID` isn't in `nodes.list()`, and
  `first_container()` raises a `ValueError` whose message distinguishes
  "node missing" from "no container yet" (empty `container_tasks`).
- **`csmanager.py` (`to_model`)**: mock a `CSMService` whose
  `container_tasks` yields one task but whose `containers` yields nothing
  (node gone) — assert `state`/`app_state` end up `mia`. Mock a service
  with zero `container_tasks` (fresh start) — assert `state` is *not*
  forced to `mia`.
- **`repo.py` (`CodeHostRepo`, `StudentRepo`)**: reuse
  `test_stop_host.py`'s Flask/SQLite fixture; mock `app.csm.get(...)` /
  `get_by_username(...)` to return a service whose `first_container()`
  raises the stale-node `ValueError`; assert `push()`/`pull()` surface it
  cleanly and `stop_host()`'s existing generic exception handling (sprint
  007) still isolates it per-host in a batch.
- **`host_s3_sync.py`**: same pattern, one test.
- **`cli/host.py` (`cont`)**: CliRunner invocation with a mocked `app.csm`;
  assert clean output, no traceback, on a stale-node service.
- **`cli/node.py` (`graceful_remove_node`, `stop_node --force`)**: mock
  `client.services.list()` returning a service with a
  `node.hostname==<fqdn>` constraint; assert the constraint is stripped
  (via `svc.update(constraints=...)`) before drain/destroy, and that a
  service with no pin or a different pin is left untouched.

## Architecture Notes

See `architecture-update.md` for full detail, including the confirmed
call-site sweep, the root-cause trace through `cli/node.py`, and design
rationale for each decision.

## GitHub Issues

(No linked GitHub issues at planning time — internal issue file only:
`stale-swarm-node-references-break-host-operations.md`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Safe node resolution in the cs_docker layer: `Service.containers`/`node_missing`/`first_container`, and MIA-marking in `to_model()` | none |
| 002 | Propagate safe container/node resolution to all consumers and prove the purge/reap repair path | 001 |
| 003 | Prevent permanently-orphaned tasks: clear stale `node.hostname` pins before node removal | none |

Tickets execute serially in the order listed.
