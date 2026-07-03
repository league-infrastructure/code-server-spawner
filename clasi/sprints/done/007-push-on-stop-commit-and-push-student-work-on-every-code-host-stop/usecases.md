---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 007 Use Cases — Push-on-stop

All nine use cases below share the same underlying mechanism
(`CodeServerManager.stop_host()`); each documents a distinct actor/trigger
combination. Two cross-cutting alternate flows — **push failure** and
**MIA host** — apply uniformly and are spelled out once in SUC-001 and
referenced (not repeated) in the rest.

## SUC-001: Student stops their own code host

**Actor**: Student

**Preconditions**: Student is logged in and owns a running `CodeHost`.

**Trigger**: Student clicks "Stop" (`GET /host/<host_id>/stop`).

**Main flow**:
1. Route resolves the student's `CodeHost` row and verifies ownership.
2. Route calls `ca.csm.stop_host(code_host)`.
3. `stop_host()` commits and pushes the workspace to the student's GitHub
   fork via `CodeHostRepo.push()`.
4. `stop_host()` removes the Swarm service.
5. `stop_host()` deletes the `CodeHost` DB row.
6. Route flashes a success message and redirects.

**Postconditions**: The student's latest work is on GitHub (unless the
push failed — see alternate flow); the Swarm service and DB row are gone.

**Alternate flow — push fails (GitHub outage, token issue, push timeout)**:
1a. `CodeHostRepo.push()` raises (or times out after `CODEHOST_PUSH_TIMEOUT_S`).
2a. `stop_host()` catches the exception, logs it at ERROR level, and
    records it on the returned `StopResult`; it does **not** re-raise.
3a. Steps 4–6 of the main flow proceed exactly as if the push had
    succeeded — the stop is never blocked by a push failure.
4a. Route flashes a warning that the host was stopped but work may not
    be fully saved to GitHub.

**Alternate flow — host is MIA (container already gone)**:
1b. `stop_host()` checks `code_host.is_mia` before attempting the push.
2b. If MIA, the push step is skipped entirely with an INFO log — no
    docker-exec attempt, no exception, no hang.
3b. Steps 4–6 of the main flow proceed normally (service removal is a
    no-op if already gone; the DB row is still deleted).

**Acceptance criteria**:
- [ ] Route calls `stop_host()`, not a bare `s.stop()` + manual delete.
- [ ] A mocked push failure does not prevent the host from stopping.
- [ ] A MIA host stops cleanly with no push attempt and no exception.

---

## SUC-002: Admin stops a student's code host

**Actor**: Admin

**Preconditions**: Admin is authenticated and holds admin role; target
`CodeHost` exists.

**Trigger**: Admin clicks "Stop" on the Code Hosts admin page
(`POST /admin/host/<host_id>/stop`).

**Main flow**:
1. Route loads the `CodeHost` row by ID.
2. Route calls `ca.csm.stop_host(code_host)`.
3–5. Same push → stop → delete sequence as SUC-001.
6. Route flashes a success message and redirects to the Code Hosts list.

**Postconditions**: Same as SUC-001. Push-failure and MIA alternate flows
apply identically (see SUC-001).

**Acceptance criteria**:
- [ ] Route calls `stop_host()`, not a bare `s.stop()` + manual delete.
- [ ] The existing "host not found in Swarm but present in DB" case still
      deletes the stale DB row (via `stop_host()`'s own stop-failure
      tolerance, not a separate branch in the route).

---

## SUC-003: Autoscale reaper stops an idle host (active-purge zone)

**Actor**: System (autoscale reaper, `cspawnctl node autoscale` cron)

**Preconditions**: `AUTOSCALE_ENABLED=true`; a class is in its
active-purge zone (`purge_after <= now < purge_by`); one of its hosts has
been idle ≥ 15 minutes.

**Trigger**: `run_autoscale()`'s scheduled cycle calls `apply_reaper_zones()`.

**Main flow**:
1. `apply_reaper_zones()` identifies the idle `CodeHost` row.
2. It calls `app.csm.stop_host(ch)` in place of the previous manual
   `app.csm.get(ch)` + `s.stop()` + `db.session.delete(ch)` sequence.
3–5. Same push → stop → delete sequence as SUC-001.
6. Zone summary logging continues as today.

**Postconditions**: The idle host's work is pushed before removal; the
class's target-node accounting is unaffected (unchanged from before this
sprint).

**Alternate flows**: Push-failure and MIA flows apply identically to
SUC-001 — a reaper cycle must never stall or abort on a GitHub outage or
a stale/MIA host.

**Acceptance criteria**:
- [ ] Active-purge idle-host removal calls `stop_host()`.
- [ ] A push failure for one host in a multi-host reaper pass does not
      abort the pass for the remaining hosts (per-host isolation).

---

## SUC-004: Autoscale reaper force-removes all hosts in a class's dormant zone

**Actor**: System (autoscale reaper)

**Preconditions**: `AUTOSCALE_ENABLED=true`; a class has reached
`purge_by` (dormant zone).

**Trigger**: `run_autoscale()`'s scheduled cycle calls `apply_reaper_zones()`.

**Main flow**:
1. `apply_reaper_zones()` fetches every remaining `CodeHost` row for the
   dormant class.
2. For each row it calls `app.csm.stop_host(ch)`.
3–5. Same push → stop → delete sequence as SUC-001, once per host.
6. The `Class` row's `purge_after` / `purge_by` / `target_nodes` fields
   are cleared as today.

**Postconditions**: Every student in the dormant class has their final
work pushed before their host is force-removed.

**Alternate flows**: Push-failure and MIA flows apply identically to
SUC-001, per host.

**Acceptance criteria**:
- [ ] Dormant-zone force-removal calls `stop_host()` per host.
- [ ] Class-row cleanup (`purge_after`/`purge_by`/`target_nodes`) still
      happens even if one or more hosts' pushes failed.

---

## SUC-005: Operator stops a host (or all hosts) via CLI

**Actor**: Admin / Operator

**Preconditions**: `cspawnctl` is configured against a live deployment.

**Trigger**: `cspawnctl host stop <name>` or `cspawnctl host stop --all`.

**Main flow**:
1. CLI resolves the target `CodeHost` row(s) (single name, or every row
   backing a live Swarm service for `--all`).
2. For each, CLI calls `app.csm.stop_host(ch, push=not no_push)`.
3–5. Same push → stop → delete sequence as SUC-001.
6. CLI prints a per-host result line.

**Postconditions**: Same as SUC-001, for each targeted host.

**Alternate flow — operator passes `--no-push`**:
1a. `push=False` is passed through to every `stop_host()` call.
2a. The push step is skipped entirely (not attempted, not logged as a
    failure) — the escape hatch from the issue's acceptance criteria.

**Acceptance criteria**:
- [ ] `host stop <name>` and `host stop --all` call `stop_host()`.
- [ ] `--no-push` skips the push step for every targeted host.
- [ ] A host with no matching `CodeHost` DB row (orphan Swarm service)
      still stops (falls back to a direct stop with a logged warning,
      since `stop_host()` requires a `CodeHost` row).

---

## SUC-006: Operator shuts down the whole system via CLI

**Actor**: Admin / Operator

**Preconditions**: `cspawnctl` is configured against a live deployment.

**Trigger**: `cspawnctl sys shutdown` (optionally `--no-push`).

**Main flow**:
1. CLI calls `app.csm.remove_all(push=not no_push)`.
2. `remove_all()` iterates every `CodeHost` DB row and calls
   `stop_host(ch, push=push)` for each.
3–5. Same push → stop → delete sequence as SUC-001, per host.

**Postconditions**: Every code host in the system is pushed (unless
`--no-push`), stopped, and its DB row deleted.

**Note**: `remove_all()`'s current implementation is broken (it
references an undefined `self.repo` attribute and would raise
`AttributeError` if invoked). This sprint replaces its body entirely; the
push-on-stop behavior is a net-new working implementation, not a
behavior change to something operators previously relied on.

**Acceptance criteria**:
- [ ] `sys shutdown` calls the rewritten `remove_all()`, which calls
      `stop_host()` per host.
- [ ] `--no-push` on `sys shutdown` skips the push step for every host.
- [ ] One host's push failure does not abort the shutdown of the rest.

---

## SUC-007: Admin fully deletes a user account (teardown)

**Actor**: Admin

**Preconditions**: Admin confirms full deletion of a user
(`POST /admin/user/<user_id>/delete`).

**Trigger**: Admin submits the delete-user confirmation form.

**Main flow**:
1. `teardown_user()` calls `_stop_user_servers()`, which now calls
   `app.csm.stop_host(ch)` for each of the user's `CodeHost` rows instead
   of a manual `s.stop()` + delete.
2–4. Same push → stop → delete sequence as SUC-001, per host.
5. `_delete_user_repos()` then deletes the user's GitHub forks (unchanged).
6. The user record is deleted once servers and repos are confirmed gone
   (or immediately if `force=True`).

**Postconditions**: Same as SUC-001 for each host, followed by the
existing repo- and user-deletion steps.

**Acceptance criteria**:
- [ ] `_stop_user_servers()` calls `stop_host()` per host and still
      populates `TeardownReport.servers_stopped` / `.failures` from the
      `StopResult`.
- [ ] A push or stop failure for one host does not abort teardown of the
      user's other hosts, repos, or the user record itself (existing
      continue-and-collect contract is preserved).

---

## SUC-008: Instructor removes students from a class

**Actor**: Instructor

**Preconditions**: Instructor is authenticated and assigned to the class.

**Trigger**: `POST /classes/students/remove` with one or more `student_ids`.

**Main flow**:
1. Route resolves each removed student's `CodeHost` row (if any).
2. Route calls `ca.csm.stop_host(host)` in place of the previous
   `ca.csm.stop_cs(host.service_name)` + manual `db.session.delete(host)`.
3–5. Same push → stop → delete sequence as SUC-001.
6. The student is removed from `class_.students` as today.

**Postconditions**: A removed student's last work is pushed before their
host disappears.

**Acceptance criteria**:
- [ ] `remove_students()` calls `stop_host()`, not `stop_cs()` +
      manual delete.
- [ ] Removing multiple students in one request isolates failures per
      student (one push/stop failure doesn't abort removal of the rest).

---

## SUC-009: Operator purges idle/MIA hosts via CLI (`host purge`)

**Actor**: Admin / Operator

**Preconditions**: `cspawnctl` is configured against a live deployment.

**Trigger**: `cspawnctl host purge` (optionally `--no-push`, `--dry-run`).

**Main flow**:
1. CLI syncs DB state, then iterates every `CodeHost` row that is MIA or
   quiescent.
2. For each, CLI calls `app.csm.stop_host(ch, push=not no_push)` — this
   replaces the command's own inline
   `CodeHostRepo.new_codehostrepo(...).push()` + `s.stop()` +
   `db.session.delete(ch)` block with a single call to the shared choke
   point.
3–5. Same push → stop → delete sequence as SUC-001.
6. CLI prints the same per-host `(pushed) Stopped and deleted: <name>` /
   `(push failed: ...)` style output as today; `--dry-run` prints the
   planned action without calling `stop_host()`.

**Postconditions**: Behavior is externally unchanged from the operator's
point of view (same flags, same dry-run output shape); internally it now
shares 100% of its push/stop/delete logic with every other stop path
instead of duplicating it.

**Acceptance criteria**:
- [ ] `host purge`'s push-then-stop-then-delete block is replaced by a
      call to `stop_host()`.
- [ ] `--no-push` and `--dry-run` behave exactly as before the refactor.
- [ ] Existing `host purge` tests (if any) continue to pass unmodified,
      or are updated only to reflect the internal call now going through
      `stop_host()` rather than a change in observable behavior.
