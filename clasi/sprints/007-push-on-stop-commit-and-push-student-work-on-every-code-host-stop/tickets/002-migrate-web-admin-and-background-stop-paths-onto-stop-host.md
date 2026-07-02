---
id: '002'
title: Migrate web, admin, and background stop paths onto stop_host
status: open
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-007
- SUC-008
depends-on:
- '001'
github-issue: ''
issue: push-on-host-stop.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Migrate web, admin, and background stop paths onto stop_host

## Description

Migrate the five non-CLI stop paths — student UI stop, admin stop, admin
user teardown, both zones of the autoscale reaper, and class student
removal — onto `CodeServerManager.stop_host()` (ticket 001). None of
these gain a user-facing opt-out; `push` defaults to `True` in every
case, matching `push-on-host-stop.md`'s requirement that these paths
"always push before stop." See `architecture-update.md` "What Changed"
for the per-file summary and Step 6 for why bulk loops here rely on
`stop_host()`'s own per-host exception isolation rather than
subprocess-per-host isolation.

## Acceptance Criteria

- [ ] `cspawn/main/routes/hosts.py` student `stop_host` route
      (`main_bp.route("/host/<host_id>/stop")`, currently lines 17-56)
      calls `ca.csm.stop_host(code_host)` instead of `s.stop()` +
      manual `db.session.delete()`/`commit()`. The flash message
      reflects `StopResult.push_error` (warning: stopped but work may
      not be fully saved) vs. a clean push (success).
- [ ] `cspawn/admin/routes.py` admin `stop_host` route
      (`admin_bp.route("/host/<int:host_id>/stop")`, currently lines
      112-133) calls `ca.csm.stop_host(code_host)` instead of `s.stop()`
      + manual delete. The existing "service not found in Swarm but
      present in DB" case still results in the DB row being removed (now
      via `stop_host()`'s own stop-failure tolerance rather than a
      separate branch).
- [ ] `cspawn/admin/teardown.py` `_stop_user_servers()` (currently lines
      34-55) calls `app.csm.stop_host(ch)` per host instead of manual
      `s.stop()` + delete. `TeardownReport.servers_stopped` /
      `.failures` are populated from each `StopResult` (a push or stop
      failure is recorded as a failure entry but does not stop the loop
      from processing the user's remaining hosts — the existing
      continue-and-collect contract is preserved).
- [ ] `cspawn/cs_docker/autoscale.py` `apply_reaper_zones()`'s
      active-purge zone loop (currently ~lines 859-906: `app.csm.get(ch)`
      + `s.stop()` + `db.session.delete(ch)`) calls
      `app.csm.stop_host(ch)` instead. Zone classification and the
      15-minute idle threshold above it are unchanged.
- [ ] `apply_reaper_zones()`'s dormant zone loop (currently ~lines
      811-857, force-remove-all) calls `app.csm.stop_host(ch)` per host
      instead of its manual sequence. `Class.purge_after` /
      `.purge_by` / `.target_nodes` clearing still happens regardless of
      individual host push/stop outcomes.
- [ ] `apply_reaper_zones()`'s `dry_run=True` path is preserved exactly:
      no `stop_host()` call is made when `dry_run` is `True` (matches
      existing dry-run print/log-only behavior).
- [ ] `cspawn/main/routes/classes.py` `remove_students()` (currently
      lines 326-350) calls `ca.csm.stop_host(host)` instead of
      `ca.csm.stop_cs(host.service_name)` + manual
      `db.session.delete(host)`.
- [ ] `main/routes/classes.py` `delete_class()` (lines 160-188) is left
      untouched — confirmed dead code (unreachable: the function returns
      early whenever `class_.students` is non-empty, so its
      `stop_cs(host.name)` loop — which also references a nonexistent
      `CodeHost.name` attribute — never executes). Out of scope per
      `architecture-update.md`.
- [ ] Unit/integration tests updated or added for each of the five call
      sites, asserting `stop_host()` (not the old raw sequence) is
      invoked, and that a mocked push failure does not abort processing
      of the remaining hosts in any multi-host loop (reaper, teardown).

## Implementation Plan

### Approach

1. `main/routes/hosts.py` (student stop) and `admin/routes.py` (admin
   stop): replace the `s.stop(); db.session.delete(code_host);
   db.session.commit()` block with `result = ca.csm.stop_host(code_host)`;
   branch the flash message on `result.push_error`.
2. `admin/teardown.py` `_stop_user_servers()`: replace the inner
   `try: s = app.csm.get(ch); if s: s.stop() ... db.session.delete(ch);
   db.session.commit() except ...` block with
   `result = app.csm.stop_host(ch)`; append to `report.failures` when
   `result.push_error or result.stop_error` is set (formatted the same
   way the existing `except Exception as e: report.failures.append(...)`
   lines do), else append `name` to `report.servers_stopped`.
3. `cs_docker/autoscale.py` `apply_reaper_zones()`: in both the dormant
   loop and the active-purge loop, replace the
   `s = app.csm.get(ch); if s: s.stop()` + `db.session.delete(ch)` pair
   with `result = app.csm.stop_host(ch)`; keep the existing `dry_run`
   short-circuit (log-only, no `stop_host()` call); the surrounding
   per-host `try/except` logging can be simplified since `stop_host()`
   itself never raises — keep a thin wrapper only if it adds log-message
   value beyond what `StopResult` already carries.
4. `main/routes/classes.py` `remove_students()`: replace
   `ca.csm.stop_cs(host.service_name); db.session.delete(host)` with
   `ca.csm.stop_host(host)`.

### Files to create / modify

- `cspawn/main/routes/hosts.py`
- `cspawn/admin/routes.py`
- `cspawn/admin/teardown.py`
- `cspawn/cs_docker/autoscale.py`
- `cspawn/main/routes/classes.py`
- Tests: extend `test/test_autoscale.py::TestApplyReaperZones` (assert
  `app.csm.stop_host` is invoked rather than `app.csm.get`/`s.stop`);
  add/extend Flask-test-client tests for the two routes; add a unit test
  for `_stop_user_servers()` (a minimal Flask+SQLite app similar to
  `_make_reaper_flask_app()` in `test/test_autoscale.py` is a reasonable
  base); add a unit test for `remove_students()`.

### Testing plan

- Reuse the in-memory-SQLite + `MagicMock` `app.csm` pattern already
  established in `test/test_autoscale.py::TestApplyReaperZones` for the
  reaper and teardown tests.
- Flask test client (`app.test_client()`) for the two HTTP routes, with
  `ca.csm`/`current_app.csm` mocked to avoid live Docker calls; assert
  the flash/redirect behavior and that `stop_host` was called with the
  expected `CodeHost`.
- For each multi-host loop (reaper zones, teardown), a test with ≥2
  hosts where one `stop_host()` call is mocked to return a `StopResult`
  with `push_error` set — assert the other host(s) are still processed
  and the loop/route completes without raising.
- Run `uv run pytest test/ -v`.

### Documentation updates

None required beyond this sprint's `architecture-update.md` (already
written); no CLI help text changes in this ticket (that is ticket 003).
