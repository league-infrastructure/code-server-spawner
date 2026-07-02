---
id: '007'
title: "Push-on-stop — commit and push student work on every code-host stop"
status: planning-docs
branch: sprint/007-push-on-stop-commit-and-push-student-work-on-every-code-host-stop
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
- SUC-008
- SUC-009
issues:
- push-on-host-stop.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 007: Push-on-stop — commit and push student work on every code-host stop

## Goals

Guarantee that any time a student's code-server host is stopped — for any
reason, through any path — the system first commits and pushes the
student's workspace to their GitHub fork, best-effort, before the Swarm
service is removed. Today only two of nine stop paths (`host purge`, and
`node rebalance`'s re-pin) do this; the rest silently drop the student's
uncommitted local changes.

## Problem

The commit+push machinery (`CodeHostRepo.push()`, `cspawn/cs_github/repo.py:73-116`)
exists and is proven (used by `host purge`), but every other stop path —
student UI stop, admin stop, the autoscale reaper's two zones, CLI `host
stop`, `sys shutdown`, admin user teardown, and class student removal —
calls `CSMService.stop()` (a thin `service.remove()` wrapper) directly and
deletes the `CodeHost` DB row, with no push step. A student whose host is
reaped, admin-stopped, or removed during a class-roster change loses any
work that was only committed locally (or not yet committed at all) and
never made it to GitHub.

## Solution

Add a single manager-level choke point, `CodeServerManager.stop_host()` in
`cspawn/cs_docker/csmanager.py`, that all nine stop paths funnel through.
`stop_host()` does three steps for every call, in order: (1) best-effort
push via `CodeHostRepo.push()` — any failure (GitHub outage, missing
token, MIA container, timeout) is logged loudly and swallowed, never
raised; (2) stop the Swarm service; (3) delete the `CodeHost` DB row.
Each step is independently best-effort so one failing step never blocks
the next. `CodeHostRepo.push()` gains a bounded subprocess timeout (new
config key `CODEHOST_PUSH_TIMEOUT_S`, default 30s) so a wedged
docker-exec-over-SSH call can never hang a caller indefinitely — required
because two of the nine call sites are synchronous web routes. CLI paths
that previously supported `--no-push` (`host purge`) keep it, refactored
onto the shared choke point; CLI paths that stop hosts but never had a
push option (`host stop`, `sys shutdown`) gain a new `--no-push` flag.
Test-fixture teardown (`cli/test.py`) routes through the same choke point
with `push=False`, since pushing throwaway test-student work is
meaningless.

See `architecture-update.md` for the full design, module boundaries,
diagrams, and rationale.

## Success Criteria

- Every stop path listed in `clasi/issues/push-on-host-stop.md` invokes
  `CodeServerManager.stop_host()` (directly or via `remove_all()`), not a
  bare `CSMService.stop()` + manual DB delete.
- A push failure (mocked GitHub outage) never prevents a stop from
  completing in any of the nine paths.
- A host whose container is already gone (MIA) skips the push cleanly —
  no exception, no hang — in every path.
- `host purge`'s existing push-then-stop behavior (including `--no-push`
  and dry-run output) is preserved byte-for-byte from the operator's
  point of view, now implemented via the shared choke point instead of
  its own inline block.
- `host stop` and `sys shutdown` gain working `--no-push` flags.
- Unit tests cover: push invoked on each migrated call site, push failure
  still results in a stopped/deleted host, MIA host skips push, push
  timeout is bounded and does not hang the test suite.

## Scope

### In Scope

- `CodeServerManager.stop_host()` orchestrator + `StopResult` value object
  (`cspawn/cs_docker/csmanager.py`).
- Timeout + `None`-service hardening of `CodeHostRepo.push()`
  (`cspawn/cs_github/repo.py`).
- Rewrite of the currently-broken `CodeServerManager.remove_all()`
  (references an undefined `self.repo` attribute today) to route through
  `stop_host()` per DB row.
- Migration of all nine stop paths onto `stop_host()`: student UI stop,
  admin stop, admin user teardown, class student removal, autoscale
  reaper (active-purge + dormant zones), CLI `host stop`, CLI `host
  purge`, CLI `sys shutdown`, CLI `test teardown`.
- New `--no-push` flags on `host stop` and `sys shutdown`; preserved
  `--no-push` on `host purge`.
- Unit tests for the orchestrator and each migrated call site.

### Out of Scope

- `node rebalance` — it re-pins a service (keeps the `CodeHost` row) rather
  than stopping/deleting it, so it does not use `stop_host()`. It keeps its
  own direct `CodeHostRepo.push()` call, unchanged, and gets the timeout
  hardening for free since it calls the same underlying method.
- Making the push asynchronous / adding a UI spinner for the synchronous
  web routes — deferred; this sprint bounds worst-case latency with a
  timeout instead (see architecture-update.md Open Questions).
- Subprocess-per-host isolation for bulk stop paths (mirroring `host push
  --all`) — deferred; this sprint relies on `stop_host()`'s per-host
  exception isolation instead (see architecture-update.md Design
  Rationale).
- Any change to `Class`/`CodeHost` schema, or to the reaper's zone
  classification logic itself (protected / active-purge / dormant)
  — only the reaper's *stop mechanism* changes, not its *decision* logic.
- NFS workspace retention/cleanup policy after a host is stopped.
- Fixing the unreachable dead-code `stop_cs()` call in `delete_class`
  (`main/routes/classes.py:172-186`) — noted as a pre-existing anomaly,
  not a stop path in practice (guarded by an earlier early-return).

## Test Strategy

Unit tests, no live Docker/GitHub required:

- `CodeServerManager.stop_host()`: mock `CodeHostRepo.push`, `CSMService`
  lookup/stop, and the DB session. Cover push-succeeds, push-raises,
  push-times-out, `push=False`, MIA host (clean skip), swarm-stop-fails
  (DB row still deleted), and the `StopResult` contents in each case.
- `CodeHostRepo.push()`: cover the new timeout parameter (mock
  `subprocess.run` to raise `TimeoutExpired`) and the `None`-service guard.
- Each migrated call site: extend or add tests following the existing
  per-module conventions (e.g. `test/test_autoscale.py`'s
  `TestApplyReaperZones` in-memory-SQLite pattern for the reaper;
  Flask test client for the two routes; Click `CliRunner` for CLI
  commands) to assert `stop_host()` (not raw `.stop()`) is invoked with
  the expected `push` value.
- No end-to-end test against a real Docker Swarm or GitHub — matches
  existing test suite conventions (`test/` has no live-infra tests).

## Architecture Notes

- Choke point lives at the manager level (`CodeServerManager.stop_host()`),
  not inside the low-level `CSMService.stop()` / `Service` /
  `ServicesManager` wrapper classes, to keep the generic Docker Swarm
  wrapper free of Flask/DB/GitHub knowledge. `CodeServerManager` already
  holds `self.app` and already imports `cs_github.repo` for
  `GithubOrg`/`StudentRepo`.
- Push is best-effort at every layer: a failure never raises out of
  `stop_host()`; it's captured in `StopResult` and logged at ERROR level.
- MIA hosts (`CodeHost.is_mia`) skip the push step with an INFO log, not
  an ERROR — there is nothing to push into.
- See `architecture-update.md` for the full 7-step design, diagrams, and
  design rationale (including the manager-vs-low-level tradeoff, the
  best-effort contract, the new timeout, and the bulk-path isolation
  decision).

## GitHub Issues

None. Tracked via `clasi/issues/push-on-host-stop.md`.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Core stop-with-push orchestrator (`stop_host`) + `CodeHostRepo.push()` hardening | — |
| 002 | Migrate web/admin/background stop paths onto `stop_host()` | 001 |
| 003 | Migrate CLI stop paths onto `stop_host()`, add `--no-push` flags, refactor `host purge` | 001 |

Tickets execute serially in the order listed (002 and 003 both depend only
on 001 and touch disjoint files, but are sequenced serially per this
sprint's single-lane execution).
