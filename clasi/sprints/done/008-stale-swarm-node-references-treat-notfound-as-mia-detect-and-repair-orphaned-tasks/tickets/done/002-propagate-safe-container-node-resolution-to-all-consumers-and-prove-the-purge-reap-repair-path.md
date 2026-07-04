---
id: '002'
title: Propagate safe container/node resolution to all consumers and prove the purge/reap
  repair path
status: done
use-cases:
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '001'
github-issue: ''
issue: stale-swarm-node-references-break-host-operations.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Propagate safe container/node resolution to all consumers and prove the purge/reap repair path

## Description

Wire the four remaining call sites that resolve a service's container
onto `Service.first_container()` (added in ticket 001):
`CodeHostRepo._get_service_container()` and `CodeHostRepo.pull()`
(`cspawn/cs_github/repo.py`), `StudentRepo._get_service_and_container()`
(same file), and `HostS3Sync.get_service_and_container()`
(`cspawn/util/host_s3_sync.py`). Also fix `cli/host.py`'s `cont` command,
which would otherwise regress from a coincidentally-caught
`docker.errors.NotFound` today to an unhandled `IndexError` once ticket
001 stops `Service.containers` from raising. Along the way, fix a
pre-existing, unrelated bug: `CodeHostRepo.pull()` currently calls
`self._get_container()`, a method that does not exist anywhere on the
class (confirmed by grep) — every call to `pull()` raises `AttributeError`
today, before ever touching node resolution. This is bundled in here
because the fix is a one-line correction to `_get_service_container()`
(the exact method this ticket is already hardening) — see
`architecture-update.md` Step 6, "Decision: Bundle the
`CodeHostRepo.pull()` `_get_container()` bugfix into this sprint."

Finally, add tests proving the whole chain works end-to-end for a
mocked stale-node host: `CodeHostRepo.push()` now raises a clean
`ValueError` instead of a raw `docker.errors.NotFound`; `stop_host()`
(sprint 007) still stops and deletes it because `service.stop()` is a
pure `DELETE /services/{id}` call that never touches `.node`; and once
`to_model()` (ticket 001) marks it `mia`, `host purge`'s existing
`is_mia or is_quiescent` filter picks it up with zero filter changes.
This proves the sprint's central claim from `sprint.md`: detection +
the existing sprint-007 purge/reap machinery is sufficient repair, with
no new "repair" command needed.

Depends on ticket 001 for `Service.node_missing`/`first_container()` to
exist.

Motivating problem:
`clasi/issues/stale-swarm-node-references-break-host-operations.md`. See
`architecture-update.md` Step 3 (Module M2), Step 5 ("What Changed" /
"Impact on Existing Components"), and Step 6 (Design Rationale, decisions
3-4 and "Repair = correct detection + existing purge/reap") for full
reasoning.

## Acceptance Criteria

- [x] `CodeHostRepo._get_service_container()` (`cspawn/cs_github/repo.py:53-62`)
      returns `(service, service.first_container())` after the existing
      `service is None` guard; `push()` (`repo.py:75-134`) itself is
      **not** modified — its safety is fully inherited from ticket 001.
- [x] `CodeHostRepo.pull()` (`repo.py:136-169`) calls
      `_, container = self._get_service_container()` instead of the
      non-existent `self._get_container()`.
- [x] `StudentRepo._get_service_and_container()` (`repo.py:312-324`)
      returns `(service, service.first_container())` after its existing
      `service` guards.
- [x] `HostS3Sync.get_service_and_container()`
      (`cspawn/util/host_s3_sync.py:23-30`) returns
      `(service, service.first_container())` after its existing `service`
      guard.
- [x] `cli/host.py`'s `cont` command (`cspawn/cli/host.py:74-95`): guards
      `s is None` (prints "Service {name} not found" and returns) before
      use; replaces `list(s.containers)[0].o` with `s.first_container().o`;
      adds an `except ValueError as e:` branch alongside the existing
      `except NotFound:` that prints a message distinct from "service not
      found" (e.g. naming that the container/node is unresolvable).
- [x] A test proves: given a service whose `first_container()` raises the
      stale-node `ValueError`, `CodeHostRepo.push()` propagates a
      `ValueError` (never a raw `docker.errors.NotFound`), and
      `CodeServerManager.stop_host()` (sprint 007, unmodified) still sets
      `stopped=True, deleted=True` with `push_error` populated — proving
      composition with sprint 007's best-effort contract for this
      specific exception type (not just a generic mocked exception).
- [x] A test proves: a `CodeHost` row with `state=mia, app_state=mia`
      (as ticket 001's `to_model()` would produce) is selected by `host
      purge`'s existing `is_mia or is_quiescent` filter with no changes
      to `cli/host.py`'s `purge` command, and `stop_host()` on it sets
      `skipped_push_mia=True` (push never attempted) while still removing
      the service and deleting the row.
- [x] Unit tests cover every criterion above with no live Docker/GitHub/
      network access.

## Implementation Plan

### Approach

1. **`cspawn/cs_github/repo.py`**: in `_get_service_container()` (lines
   53-62), replace the `containers = list(service.containers); if not
   containers: raise ValueError(...); return service, containers[0]` tail
   with `return service, service.first_container()`. In `pull()` (line
   ~143), replace `container = self._get_container()` with `_, container
   = self._get_service_container()`. In
   `StudentRepo._get_service_and_container()` (lines 312-324), apply the
   same replacement as `_get_service_container()`.
2. **`cspawn/util/host_s3_sync.py`**: in `get_service_and_container()`
   (lines 23-30), same replacement.
3. **`cspawn/cli/host.py`**: in the `cont` command (lines 74-95), add `if
   s is None: print(f"Service {service_name} not found"); return`
   immediately after `s = app.csm.get(service_name)`; replace
   `print(list(s.containers)[0].o)` with `print(s.first_container().o)`;
   add `except ValueError as e: print(f"Cannot resolve container for
   {service_name}: {e}")` alongside the existing `except NotFound:`.
4. **Tests**: extend `test/test_stop_host.py`'s Flask/SQLite +
   `MagicMock` fixture (or add a new `test/test_stale_node_consumers.py`)
   to mock `app.csm.get(...)` / `get_by_username(...)` returning a
   `MagicMock` service whose `.first_container.side_effect` is the
   stale-node `ValueError`; drive `CodeHostRepo.push()` and
   `CodeServerManager.stop_host()` through it. Add a `CliRunner` test for
   `host cont` against a mocked `app.csm`.

### Files to create / modify

- `cspawn/cs_github/repo.py` — `CodeHostRepo._get_service_container()`,
  `CodeHostRepo.pull()`, `StudentRepo._get_service_and_container()`.
- `cspawn/util/host_s3_sync.py` — `HostS3Sync.get_service_and_container()`.
- `cspawn/cli/host.py` — `cont` command.
- New or extended test file(s): `test/test_stale_node_consumers.py`
  (or additions to `test/test_stop_host.py`), plus a `cont`-command
  addition alongside the existing CLI-focused tests in
  `test/test_cli_stop_paths.py`'s style.

### Testing plan

- Mock-based unit tests only, no live Docker/GitHub/network access,
  reusing `test_stop_host.py`'s in-memory-SQLite Flask app + `MagicMock`
  `app.csm` pattern.
- Cases: (1) `_get_service_container()`/`_get_service_and_container()`/
  `get_service_and_container()` each propagate `first_container()`'s
  `ValueError` unchanged; (2) `pull()` no longer raises `AttributeError`
  and reaches the same container-resolution path as `push()`; (3)
  `push()` on a stale-node host raises `ValueError`, not
  `docker.errors.NotFound`; (4) `stop_host()` on that same host still
  stops+deletes with `push_error` set; (5) an already-MIA `CodeHost` row
  is selected by `host purge`'s filter and `stop_host()` skips its push
  (`skipped_push_mia=True`); (6) `host cont` CliRunner invocation on a
  mocked stale-node service prints a clean message and exits 0 (no
  traceback); (7) `host cont` on a genuinely-missing service still prints
  the existing "service not found" message.
- Run `uv run pytest test/ -v` to confirm no regressions, in particular
  `test/test_stop_host.py`, `test/test_stop_call_sites.py`,
  `test/test_cli_stop_paths.py`.

### Documentation updates

None required — internal error-message improvements and a bugfix; no new
CLI flags or commands.
