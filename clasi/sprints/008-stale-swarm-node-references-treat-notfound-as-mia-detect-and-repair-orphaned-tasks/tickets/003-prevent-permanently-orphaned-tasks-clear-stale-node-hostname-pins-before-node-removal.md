---
id: '003'
title: 'Prevent permanently-orphaned tasks: clear stale node.hostname pins before
  node removal'
status: open
use-cases:
- SUC-006
depends-on: []
github-issue: ''
issue: stale-swarm-node-references-break-host-operations.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Prevent permanently-orphaned tasks: clear stale node.hostname pins before node removal

## Description

Add `_unpin_services_from_node(client, node_fqdn, *, log=None) -> int` to
`cspawn/cli/node.py`, next to the existing `_pin_service_to_node()`/
`_service_constraints()` (lines 137-158). Call it from
`graceful_remove_node()` (lines 1993-2085) before draining, and from
`node stop --force`'s branch (lines 2088-2132), best-effort, before
destroying the droplet.

This is the root-cause fix, independent of tickets 001/002 (different
subsystem — node lifecycle in `cli/node.py`, not container/node
resolution in `cs_docker`/`cs_github`). Root cause, confirmed by reading
the code: `_pin_service_to_node()` (used by `node rebalance`) sets a
*hard* `node.hostname==<fqdn>` Swarm placement constraint. When that node
is later removed via `graceful_remove_node()` (shared by `node stop`,
`node contract --force-drain`, and the automated autoscale scale-down
path) or `node stop --force`, Swarm cannot reschedule the hard-pinned
task onto any other node — no node satisfies the constraint — so after a
drain-timeout warning (or, for `--force`, with no drain attempt at all)
the node is removed and the droplet destroyed anyway, leaving the task's
last-known `"running"` status and now-permanently-invalid `NodeID`
stuck forever. The automated autoscale path is not exposed (it only ever
removes double-checked, verified-empty nodes); the two exposed vectors
are the manual `--force-drain`/`--force` escape hatches. Stripping the
specific stale pin before removal frees Swarm's scheduler to reschedule
the task elsewhere instead.

Motivating problem:
`clasi/issues/stale-swarm-node-references-break-host-operations.md`
("investigate whether graceful_remove_node/drain can leave tasks pinned
to dead NodeIDs, and document findings + any cheap prevention"). See
`architecture-update.md` Step 1 (root-cause trace), Step 3 (Module M3),
and Step 6 (Design Rationale, "Decision: Unpin before drain/destroy, not
a broader policy change").

## Acceptance Criteria

- [ ] `_unpin_services_from_node(client, node_fqdn, *, log=None) -> int`
      lists `client.services.list(filters={"label": "jtl.codeserver=true"})`,
      and for each service whose `_service_constraints(svc)` includes
      `node.hostname==<fqdn>` or `node.hostname==<short-name>` (matching
      both forms, mirroring `_pin_service_to_node`'s own normalization),
      strips **only** that constraint via `svc.update(constraints=kept)`
      — any other constraints (e.g. `node.role != manager`) are
      preserved. Returns the count of services unpinned.
- [ ] A service with no `node.hostname==` constraint, or one pinned to a
      *different* node, is left untouched — `svc.update` is not called
      for it.
- [ ] Failures updating an individual service are caught and logged as a
      warning; they do not raise out of `_unpin_services_from_node()` and
      do not prevent checking/unpinning the remaining services.
- [ ] `graceful_remove_node()` (`cli/node.py:1993-2085`) calls
      `_unpin_services_from_node(manager_client, resolved_fqdn, log=log)`
      immediately after `node_obj = _find_swarm_node(...)` resolves
      (whether or not `node_obj` is found — a stale pin is meaningful
      even if the swarm-side node object is already gone) and before
      `_drain_swarm_node()`.
- [ ] `stop_node`'s `--force` branch (`cli/node.py:2122-2132`), which
      currently constructs no `manager_client` at all, builds one (same
      `docker.DockerClient(base_url=docker_uri, use_ssh_client=True)`
      pattern the non-force branch already uses) when `DOCKER_URI` is
      configured, and calls `_unpin_services_from_node()` in its own
      try/except immediately before `droplet.destroy()`. Any failure
      (Docker unreachable, construction error) is caught and logged as a
      warning; `droplet.destroy()` still runs unconditionally afterward —
      the unpin attempt never blocks the force-destroy escape hatch.
- [ ] Unit tests cover every criterion above with a mocked
      `docker.DockerClient` — no live Docker/DigitalOcean access.

## Implementation Plan

### Approach

1. **`cspawn/cli/node.py`**: add `_unpin_services_from_node()` directly
   below `_pin_service_to_node()` (after line 158). Implementation:
   `short = node_fqdn.split(".")[0]`; for each `svc` in
   `client.services.list(filters={"label": "jtl.codeserver=true"})`,
   compute `constraints = _service_constraints(svc)`, find entries
   matching `f"node.hostname=={node_fqdn}"` or
   `f"node.hostname=={short}"` (after stripping whitespace, matching
   `_pin_service_to_node`'s own `.replace(" ", "")` normalization); if
   any match, `kept = [c for c in constraints if c not in matching]`,
   call `svc.update(constraints=kept)` inside try/except logging a
   warning on failure, increment a counter on success. Return the
   counter.
2. In `graceful_remove_node()`, insert the call right after `node_obj =
   _find_swarm_node(manager_client, resolved_fqdn, short)` (around line
   2042), before the `if dry_run:` branch's actions list is built (so
   `--dry-run` can optionally report it too — printing an informational
   "would check/clear pins" line is a reasonable addition but not
   required by acceptance criteria) and before the real
   `_drain_swarm_node()` call (around line 2059).
3. In `stop_node`'s `force` branch (around line 2122-2131), before the
   existing `droplet.destroy()` try block, add: `if docker_uri: try: _mc
   = docker.DockerClient(base_url=docker_uri, use_ssh_client=True); n =
   _unpin_services_from_node(_mc, fqdn, log=log); if n: log.info(f"[stop]
   Cleared {n} node pin(s) before force-destroying {fqdn}") except
   Exception as e: log.warning(f"[stop] Could not clear node pins before
   force-destroy (proceeding anyway): {e}")`.

### Files to create / modify

- `cspawn/cli/node.py` — new `_unpin_services_from_node()`,
  `graceful_remove_node()`, `stop_node` (`--force` branch).
- New test file `test/test_node_unpin.py` (or additions to
  `test/test_node_contract.py`, which already establishes the
  `MagicMock`-docker-client convention for this file).

### Testing plan

- Mock-based unit tests only, no live Docker/DigitalOcean access,
  following `test/test_node_contract.py`'s / `test/test_node_rebalance.py`'s
  mocked-`docker.DockerClient` convention.
- Cases: (1) a service with `node.hostname==<fqdn>` gets unpinned
  (`svc.update` called with the constraint removed, others preserved);
  (2) a service with `node.hostname==<short-name>` (short-form) also gets
  unpinned; (3) a service with no pin, or pinned to a different node, is
  untouched (`svc.update` not called); (4) an `svc.update` failure for
  one service is logged and does not prevent unpinning others; (5)
  `graceful_remove_node()` calls `_unpin_services_from_node()` before
  `_drain_swarm_node()` (mock both, assert call order); (6) `stop_node
  --force` attempts unpin before `droplet.destroy()` when `DOCKER_URI` is
  set, and still calls `droplet.destroy()` when the unpin attempt raises
  (mocked Docker-unreachable case).
- Run `uv run pytest test/ -v` to confirm no regressions, in particular
  `test/test_node_contract.py` and `test/test_node_rebalance.py`.

### Documentation updates

One-line addition to `graceful_remove_node()`'s and `stop_node`'s
docstrings noting the new unpin step (content already drafted in
`architecture-update.md` Step 5, "What Changed"). No separate user-facing
documentation needed — this is an internal safety improvement to
existing commands, not a new flag or command.
