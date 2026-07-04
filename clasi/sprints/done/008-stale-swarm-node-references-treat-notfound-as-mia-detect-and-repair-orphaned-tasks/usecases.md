---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 008 Use Cases

All six use cases below trace back to one underlying defect class: a Swarm
task whose `NodeID` no longer resolves. SUC-001 is the foundational
detection use case everything else builds on. Relates to the parent
use cases in `docs/design/usecases.md`: UC-003 (host start/scheduling),
UC-004 (host distributed across swarm nodes), UC-005 (instructor views
host state), and UC-006 (operator manages swarm nodes via CLI) — sprint
007's `usecases.md` set the precedent of not forcing a strict "Parent:"
line per SUC, followed here too.

## SUC-001: Sync detects and marks a stale-swarm-node host as MIA

- **Actor**: System (`CSMService.to_model()` / `sync_to_db()`), triggered
  by `host ls` (unconditional per-service `sync_to_db()`), `host purge`'s
  internal `sync(check_ready=True)`, `host dbsync`, or the autoscale
  reaper's purge-first step.
- **Preconditions**: A `CodeHost`'s Swarm service still exists, but its
  current task's `NodeID` refers to a node the autoscaler has destroyed.
  The service's last-known task `Status.State` is still `"running"`
  (Swarm never got to supersede it — see root-cause note in
  architecture-update.md).
- **Main Flow**:
  1. An operator or cron path invokes something that calls
     `CSMService.sync_to_db()` for this service.
  2. `to_model()` attempts `next(self.containers)`.
  3. `Service.containers` (proc.py) calls `self.manager.client.nodes.get(node_id)`
     for the task's `NodeID`; this raises `docker.errors.NotFound`.
  4. `Service.containers` catches the `NotFound`, logs an ERROR, and skips
     this task (does not yield a container) instead of propagating.
  5. `next(self.containers)` raises `StopIteration`; `to_model()`'s
     existing `except (KeyError, StopIteration)` sets `c = None` (unchanged
     from before this sprint).
  6. `to_model()` checks `self.node_missing` (a new property backed by a
     single `nodes.list()` call, comparing task `NodeID`s against current
     cluster membership) — `True`.
  7. `to_model()` sets `state=HostState.MIA.value` and
     `app_state=HostState.MIA.value` on the returned `CodeHost`, instead of
     trusting `self.status` (which would otherwise still report
     `"running"`).
  8. `sync_to_db()` commits this onto the existing `CodeHost` row (or
     inserts it pre-marked MIA, if this is the first sync of a newly
     discovered stale service).
- **Postconditions**: The `CodeHost` row's `state`/`app_state` are `mia`;
  `CodeHost.is_mia` is `True`; `host ls` / admin UI / `is_purgeable` all
  reflect reality instead of a stale "running" status.
- **Alternate flow — genuinely just-created service (no task yet)**:
  1a. `container_tasks` yields nothing (Swarm hasn't scheduled a container
      yet — normal immediately after `services.create()`).
  1b. `next(self.containers)` raises `StopIteration` for an unrelated,
      benign reason.
  1c. `self.node_missing` is `False` (no task `NodeID`s to compare at all),
      so `to_model()` does **not** force `state=mia` — the host proceeds
      through its normal starting → ready lifecycle.
- **Acceptance Criteria**:
  - [ ] A service whose only task's `NodeID` resolves to
        `docker.errors.NotFound` is marked `state=mia`, `app_state=mia`
        by `to_model()`.
  - [ ] A freshly created service with zero `container_tasks` is **not**
        marked MIA by this logic (existing starting-state behavior is
        preserved).
  - [ ] `Service.containers` never raises `docker.errors.NotFound`; it
        logs and skips the affected task.

---

## SUC-002: Operator pushes a host whose Swarm task references a destroyed node

- **Actor**: Admin/Operator (`cspawnctl host push <name>` or `--all`).
- **Preconditions**: Host's Swarm task's `NodeID` no longer exists.
- **Main Flow**:
  1. Operator runs `cspawnctl host push gavin-morris` (or `--all`, which
     isolates each host in its own subprocess per sprint 007's
     `_push_all`).
  2. `CodeHostRepo.push()` calls `self._get_service_container()`.
  3. `_get_service_container()` calls the new `service.first_container()`.
  4. `first_container()` finds `list(self.containers)` empty (the stale
     task was skipped per SUC-001) and checks `self.node_missing` — `True`.
  5. `first_container()` raises `ValueError("... Swarm task pinned to a
     stale/destroyed node ...")` — never a raw `docker.errors.NotFound`.
  6. The CLI prints a clean, actionable failure; for `--all`, the
     subprocess exits non-zero and is recorded as one `failed` entry —
     the batch continues with the remaining hosts.
- **Postconditions**: The operator sees a clear, typed error instead of a
  raw traceback; the rest of the batch is unaffected.
- **Acceptance Criteria**:
  - [ ] `CodeHostRepo.push()` on a stale-node host raises `ValueError`
        naming the stale-node condition, never a raw
        `docker.errors.NotFound`.
  - [ ] `host push --all` records this host as a clean failure line and
        continues pushing the remaining hosts.

---

## SUC-003: A stale-node host inside a multi-host stop/reaper batch

- **Actor**: System (autoscale reaper zones, `sys shutdown`, `host purge`
  — any `stop_host()`/`remove_all()` caller from sprint 007).
- **Preconditions**: A batch stop operation includes one host with a
  stale node reference among otherwise-healthy hosts.
- **Main Flow**:
  1. `stop_host()`'s push step calls `CodeHostRepo(...).push()`, which
     raises the clean `ValueError` from SUC-002.
  2. `stop_host()`'s existing generic `except Exception` (sprint 007,
     unchanged) catches it, records `push_error` on the `StopResult`, and
     proceeds — it never re-raises.
  3. `stop_host()`'s stop step calls `self.get(code_host)` (a pure
     service-ID lookup — `client.services.get(id)` — which does not
     resolve `.node` at all) and then `service.stop()`
     (`= service.remove()`, a control-plane `DELETE /services/{id}` that
     does not require contacting the task's node).
  4. `stop_host()`'s delete step removes the `CodeHost` DB row.
  5. The batch continues to the next host.
- **Postconditions**: The stale-node host is fully cleaned up (Swarm
  service removed, DB row deleted) in the same pass as every other host;
  no crash, no batch abort — this already holds structurally from sprint
  007's design, and this sprint adds a test that proves it holds for this
  specific failure mode (a real `docker.errors.NotFound`, not a generic
  mocked exception).
- **Acceptance Criteria**:
  - [ ] A stale-node host in a multi-host `stop_host()` batch is stopped
        and deleted like any other push-failure case; the rest of the
        batch is unaffected.
  - [ ] `service.stop()` on a service whose task's node is gone still
        succeeds (verified with a mock that only fails `.nodes.get()`,
        not `.services.get()`/`.remove()`).

---

## SUC-004: Operator inspects a stale-node host via `host cont`

- **Actor**: Admin/Operator.
- **Preconditions**: Host's Swarm task's `NodeID` no longer exists.
- **Main Flow**:
  1. Operator runs `cspawnctl host cont gavin-morris`.
  2. CLI resolves the service via `app.csm.get(service_name)`.
  3. CLI calls `s.first_container()` instead of the current
     `list(s.containers)[0]`.
  4. `first_container()` raises `ValueError` (no live container, node
     missing).
  5. CLI catches `ValueError` and prints a clear message, distinct from
     the existing "service not found" (`NotFound`) message.
- **Postconditions**: No unhandled exception (previously this would have
  raised `IndexError` once `Service.containers` stopped yielding the
  stale task, or a raw `docker.errors.NotFound` before that fix).
- **Acceptance Criteria**:
  - [ ] `host cont` on a stale-node host prints a clear "no resolvable
        container" message, not an unhandled exception.
  - [ ] `host cont` on a genuinely nonexistent service still prints the
        existing "service not found" message.

---

## SUC-005: Operator repairs a stale-node host via the existing purge/reap flow

- **Actor**: Admin/Operator (`cspawnctl host purge`, `host reap`) or
  System (autoscale reaper).
- **Preconditions**: Host has been marked MIA per SUC-001 (via a prior
  `host ls` or `host purge`'s internal sync).
- **Main Flow**:
  1. Operator runs `cspawnctl host purge` (or the automated reaper's
     `is_mia`-gated sweep runs on schedule).
  2. `sync(check_ready=True)` (already part of purge's existing flow)
     re-confirms state — no change needed to this call.
  3. `ch.is_mia` is `True`, so the existing `is_mia or is_quiescent` filter
     selects it, unchanged.
  4. `stop_host(ch, push=not no_push)` is called; because `code_host.is_mia`
     is `True`, the push step is skipped cleanly
     (`skipped_push_mia=True`, sprint 007 behavior, no change).
  5. `stop_host()` removes the (already-orphaned) Swarm service — a pure
     service-ID delete, per SUC-003 — and deletes the DB row.
- **Postconditions**: The stale-node host and its orphaned Swarm service
  are fully gone from both Swarm and the DB. The student (or admin) can
  start a fresh host normally via `new_cs()`, which schedules onto a live
  node through Docker's default spread scheduler (UC-004).
- **Acceptance Criteria**:
  - [ ] A MIA-marked stale-node host is picked up by `host purge`'s
        existing `is_mia or is_quiescent` filter with **no code change**
        to the filter itself.
  - [ ] `stop_host()` on an already-MIA stale-node host skips the push
        step and still successfully removes the orphaned Swarm service
        and DB row.
  - [ ] This chain (detect → purge → clean) is documented in
        architecture-update.md as the primary remediation path; no new
        "repair" command is introduced.

---

## SUC-006: Node removal no longer permanently orphans a pinned host's task

- **Actor**: Admin/Operator (`cspawnctl node stop --force`,
  `cspawnctl node contract --force-drain`) or System (autoscale automated
  scale-down, which also calls `graceful_remove_node()`).
- **Preconditions**: A service was previously pinned to the
  node-under-removal via a `node.hostname==<fqdn>` constraint (set by a
  prior `cspawnctl node rebalance`).
- **Main Flow**:
  1. Operator or the autoscaler triggers node removal
     (`graceful_remove_node()`, or the `--force` branch of `node stop`).
  2. Before draining (or, for `--force`, before destroying the droplet),
     the removal path lists `jtl.codeserver` services and finds any whose
     placement constraints include `node.hostname==<fqdn-being-removed>`
     (matching both FQDN and short-name forms, mirroring
     `_pin_service_to_node`'s own constraint format).
  3. The matching constraint (and only that one — other constraints, e.g.
     `node.role != manager`, are preserved) is stripped via
     `svc.update(constraints=...)`.
  4. Drain / wait-for-drain / node-remove / droplet-destroy proceeds
     exactly as before.
- **Postconditions**: A previously pinned service is no longer
  permanently unreschedulable once its node is gone — Swarm's scheduler
  is free to place its replacement task on another eligible node (subject
  to normal spread scheduling) instead of leaving a stale task record
  with a now-nonexistent `NodeID` forever.
- **Alternate flow — `node stop --force` (no drain at all)**:
  1a. The constraint is still stripped, best-effort, immediately before
      `droplet.destroy()` — even without a drain wait, this prevents the
      *permanent* unreschedulability once Swarm later detects the node is
      gone; a transient reschedule delay is expected and acceptable for
      this explicitly-unsafe escape hatch.
  2a. If the Docker manager itself is unreachable (the scenario `--force`
      exists for), the unpin attempt is caught and logged as a warning;
      it never blocks the droplet destroy.
- **Acceptance Criteria**:
  - [ ] `graceful_remove_node()` strips any
        `node.hostname==<fqdn-being-removed>` constraint from affected
        services before/while draining.
  - [ ] `node stop --force` also strips such constraints, best-effort,
        before destroying the droplet.
  - [ ] A service with no pin, or a pin to a *different* node, is left
        untouched.
