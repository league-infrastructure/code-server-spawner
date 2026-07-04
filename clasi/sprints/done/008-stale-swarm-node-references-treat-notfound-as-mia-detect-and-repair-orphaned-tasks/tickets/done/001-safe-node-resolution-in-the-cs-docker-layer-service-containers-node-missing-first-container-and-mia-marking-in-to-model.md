---
id: '001'
title: 'Safe node resolution in the cs_docker layer: Service.containers/node_missing/first_container,
  and MIA-marking in to_model()'
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: stale-swarm-node-references-break-host-operations.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Safe node resolution in the cs_docker layer: Service.containers/node_missing/first_container, and MIA-marking in to_model()

## Description

Harden `Service.containers` (`cspawn/cs_docker/proc.py:154-230`) — the
single place in the codebase that calls `client.nodes.get(<id>)` (a
single-node fetch, line 159) — to catch `docker.errors.NotFound` and skip
the affected task instead of raising. Add two new `Service` members:
`node_missing` (a cheap, `nodes.list()`-based signal that a task's
`NodeID` no longer exists in the cluster — never itself raises
`NotFound`) and `first_container()` (returns the first live container or
raises a diagnostic `ValueError`). Update `CSMService.to_model()`
(`cspawn/cs_docker/csmanager.py:164-231`) to mark a `CodeHost` row
`state=mia`, `app_state=mia` when `node_missing` is true, instead of
trusting Swarm's stale `"running"` task status.

This is the foundation ticket every other stop/push/inspect path relies
on (tickets 002 and 003 build on or are independent of it, but the
consumer wiring in ticket 002 requires `first_container()`/`node_missing`
to exist first). No caller is wired to the new methods yet — `push()`,
`pull()`, `StudentRepo`, `HostS3Sync`, and `cli host cont` are unchanged
in this ticket — so this ships safely standalone: it only changes
behavior for a condition (`docker.errors.NotFound` on node lookup) that
currently crashes every caller anyway.

Motivating problem: `clasi/issues/stale-swarm-node-references-break-host-operations.md`
— a `CodeHost`'s Swarm task can reference a node the autoscaler has
destroyed, so any path resolving `container.node` raises an uncaught
`docker.errors.NotFound`, and the host looks healthy in `host ls`
(`state=running`) forever because nothing ever demotes it. See
`architecture-update.md` Step 1 (root-cause trace), Step 3 (Module M1),
and Step 6 (Design Rationale, decisions 1-2) for full reasoning.

## Acceptance Criteria

- [x] `Service.containers` (`cspawn/cs_docker/proc.py:154-230`) never
      raises `docker.errors.NotFound`. The `self.manager.client.nodes.get(node_id)`
      call (line 159) is wrapped in `try/except NotFound`; on catch, log
      at ERROR (naming the task ID, service name, and stale `NodeID`) and
      `continue` to the next task — the same treatment the generator
      already gives a missing `container_id` or a `ConnectionError`/`OSError`
      a few lines above it.
- [x] New `Service.node_missing` property: returns `True` only if at
      least one entry in `self.container_tasks` has a `NodeID` that is
      **not** present in `{n.id for n in self.manager.client.nodes.list()}`.
      Returns `False` when `container_tasks` is empty (a freshly created
      service with no task yet must not be flagged). Uses `nodes.list()`
      only — never `nodes.get()` — so this property itself can never
      raise `NotFound`.
- [x] New `Service.first_container()` method: returns
      `list(self.containers)[0]` when non-empty. When empty, raises
      `ValueError` with a message that names the stale-node condition
      when `self.node_missing` is `True`, and a distinct generic "no
      containers found" message when it is `False`.
- [x] `CSMService.to_model()` (`cspawn/cs_docker/csmanager.py:164-231`):
      when the existing container-resolution `try/except` leaves `c =
      None` (any of its existing branches) **and** `self.node_missing` is
      `True`, the returned `CodeHost`'s constructor kwargs include
      `state=HostState.MIA.value` and `app_state=HostState.MIA.value`.
- [x] `to_model()`: when `c = None` **and** `self.node_missing` is
      `False` (e.g. a fresh service with no task yet, or the existing
      `ConnectionError`/`OSError` transient-blip branch), `state` is left
      as `self.status` exactly as today, and `app_state` is **not**
      included in the constructor kwargs at all — preserving today's
      behavior of never touching an existing row's `app_state` on a
      routine resync (must not clobber an existing `ready` `app_state`
      back to `None`).
- [x] Unit tests cover every criterion above using mocked
      `manager.client.nodes.get` / `nodes.list()` / `container_tasks` —
      no live Docker daemon.

## Implementation Plan

### Approach

1. **`cspawn/cs_docker/proc.py`**: add `from docker.errors import
   NotFound` to the imports. In `Service.containers` (around line 159),
   wrap `node = self.manager.client.nodes.get(node_id)` in
   `try/except NotFound as e: logger.error(f"Node {node_id} for task
   {t['ID']} in service {self.name} no longer exists in the Swarm
   (likely destroyed by the autoscaler): {e}"); continue`. Add
   `node_missing` as a `@property` below `container_tasks`/`containers`:
   collect `{t.get("NodeID") for t in self.container_tasks if
   t.get("NodeID")}`; if empty return `False`; else compare against
   `{n.id for n in self.manager.client.nodes.list()}` and return whether
   the task-ID set has any member not in the known-node set. Add
   `first_container()` as a plain method: `containers =
   list(self.containers); if containers: return containers[0]`; else
   branch on `self.node_missing` to pick the `ValueError` message.
2. **`cspawn/cs_docker/csmanager.py`**: in `to_model()`, after the
   existing `try/except (KeyError, StopIteration)` /
   `except (ConnectionError, OSError)` block leaves `c` set (or `None`),
   build the `CodeHost(...)` constructor call via a `dict` of kwargs
   (`model_kwargs = {...}` with the fields already there today) instead
   of inline keyword arguments, so `app_state` can be conditionally
   added. After building the base kwargs, add: `if c is None and not
   no_container and self.node_missing: logger.error(...); model_kwargs["state"]
   = HostState.MIA.value; model_kwargs["app_state"] = HostState.MIA.value`.
   Return `CodeHost(**model_kwargs)`.

### Files to create / modify

- `cspawn/cs_docker/proc.py` — `Service.containers`, new `node_missing`,
  new `first_container()`.
- `cspawn/cs_docker/csmanager.py` — `CSMService.to_model()`.
- New test file `test/test_node_missing.py`.

### Testing plan

- Mock-based unit tests only, no live Docker — build a fake
  `manager`/`client` (`MagicMock`) whose `.nodes.get(id)` raises
  `docker.errors.NotFound` for one task's `NodeID` and returns a normal
  mock `Node` for another, and whose `.nodes.list()` returns a fixed set
  of live node objects.
- Cases: (1) `Service.containers` skips the stale-node task without
  raising and still yields the healthy one; (2) `node_missing` is `True`
  when a task's `NodeID` isn't in `nodes.list()`; (3) `node_missing` is
  `False` when `container_tasks` is empty; (4) `first_container()` raises
  `ValueError` with the node-missing message vs. the generic message in
  each configuration; (5) `to_model()` sets `state=mia`/`app_state=mia`
  when `node_missing=True`; (6) `to_model()` leaves `state`/`app_state`
  untouched (existing behavior) for a fresh service with no task, and for
  the pre-existing `ConnectionError` transient-blip branch.
- Run `uv run pytest test/ -v` to confirm no regressions elsewhere
  (particularly `test/test_stop_host.py`, which exercises `to_model()`
  transitively via `sync_to_db()`).

### Documentation updates

None required — internal hardening, no user-facing surface yet.
