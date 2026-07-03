---
status: in-progress
sprint: '007'
tickets:
- 007-001
- 007-002
- 007-003
---

# Commit and push student work on every code-host stop

## Summary

Any time a code host is stopped — for **any** reason — the system must first
commit the student's workspace and push it to the upstream (the student's
GitHub fork). Today the commit+push machinery exists
(`CodeHostRepo.push()`) but it is only invoked by two callers
(`host purge` and `node rebalance`). Every other stop path removes the
Swarm service without saving the student's work.

## Current state

The commit+push utility is **`CodeHostRepo.push(branch)`**
([repo.py:73-116](cspawn/cs_github/repo.py#L73-L116)): it runs
`git commit -a -m"Automated commit" || true && git push` inside the
container as the `vscode` user via `docker -H ssh://<node> exec`,
authenticated with the container's `GITHUB_TOKEN`, remote derived from the
service's `JTL_REPO` env var.

All stops funnel to **`CSMService.stop()`**
([csmanager.py:39-42](cspawn/cs_docker/csmanager.py#L39-L42)) →
service removal, and nearly every call site also deletes the `CodeHost`
DB row. There is no on-stop lifecycle hook.

### Stop paths that do NOT push today

- **Student UI stop** — `stop_host` in
  [main/routes/hosts.py:17-56](cspawn/main/routes/hosts.py#L17-L56)
- **Admin stop** — `stop_host` in
  [admin/routes.py:112-133](cspawn/admin/routes.py#L112-L133)
- **Autoscale reaper** (dormant-zone force purge and active-purge idle
  reaping) — `apply_reaper_zones` in
  [autoscale.py:811-906](cspawn/cs_docker/autoscale.py#L811-L906)
- **CLI `host stop [name] / --all`** —
  [cli/host.py:111-132](cspawn/cli/host.py#L111-L132)
- **CLI `sys shutdown`** → `CodeServerManager.remove_all()` —
  [cli/sys.py:13-19](cspawn/cli/sys.py#L13-L19),
  [csmanager.py:831-836](cspawn/cs_docker/csmanager.py#L831-L836)
- **Admin full user teardown** — `_stop_user_servers` in
  [admin/teardown.py:34-55](cspawn/admin/teardown.py#L34-L55)
- **Remove students from class** — `remove_students` in
  [main/routes/classes.py:326-350](cspawn/main/routes/classes.py#L326-L350)
  (uses `CodeServerManager.stop_cs`)
- **Test-fixture teardown** — [cli/test.py](cspawn/cli/test.py)

### Stop paths that already push (keep, but unify)

- **CLI `host purge`** — pushes before stopping unless `--no-push`
  ([cli/host.py:187-240](cspawn/cli/host.py#L187-L240))
- **CLI `node rebalance`** — pushes before re-pinning (not a stop, but
  same pattern; [cli/node.py:161-181](cspawn/cli/node.py#L161-L181))

## Scope / acceptance criteria

- [ ] A single choke-point ("stop with push") through which **all**
  code-host stop paths funnel, so future stop paths get push-on-stop for
  free. Candidates: extend `CSMService.stop()` itself, or add a
  `CodeServerManager.stop_host(...)`-style orchestrator that pushes then
  stops then handles the DB row.
- [ ] Every stop path listed above commits and pushes before the service
  is removed: student UI stop, admin stop, autoscale reaper (both zones),
  CLI `host stop`, `sys shutdown`/`remove_all`, user teardown, class
  student removal.
- [ ] Push failures must not strand the stop: if the commit/push fails
  (GitHub outage, MIA container, missing token), log it loudly and
  proceed with the stop — best-effort, but with clear visibility
  (log + where applicable UI flash message).
- [ ] Hosts whose container is already gone (MIA) skip the push cleanly
  (nothing to exec into) — no crash, no hang.
- [ ] An explicit escape hatch remains for operators (e.g. `--no-push`
  flags preserved on CLI commands) and for test teardown where pushing
  is meaningless.
- [ ] Existing `host purge` push behavior is refactored onto the shared
  choke point rather than duplicated.
- [ ] Tests cover: push invoked on each stop path, push failure still
  stops the host, MIA host skips push.

## Open questions (for sprint planning)

- **Blocking vs best-effort**: recommendation is best-effort (never let a
  GitHub outage prevent reaping/scale-down), since the workspace itself
  lives on shared NFS and is not destroyed by service removal. Confirm.
- **Where the hook lives**: inside `CSMService.stop()` (lowest level,
  catches everything, but the docker layer would need access to
  app/config/repo machinery) vs a manager-level orchestrator that all
  routes/CLI/reaper call (cleaner layering, requires touching each call
  site once). Planner to decide with layering in mind.
- **Latency**: UI stop routes become slower (git commit+push over
  docker-ssh exec). Decide whether that's acceptable synchronously or
  needs a spinner/async pattern for the user-facing route.
- **`remove_all` / bulk paths**: per-host isolation so one failing push
  doesn't abort the batch (mirror `host push --all`'s
  subprocess-isolation approach if needed).
