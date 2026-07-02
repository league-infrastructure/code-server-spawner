---
id: '001'
title: Core stop-with-push orchestrator (stop_host) and CodeHostRepo.push hardening
status: done
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
depends-on: []
github-issue: ''
issue: push-on-host-stop.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Core stop-with-push orchestrator (stop_host) and CodeHostRepo.push hardening

## Description

Add the `CodeServerManager.stop_host()` choke point and its `StopResult`
value object to `cspawn/cs_docker/csmanager.py`, rewrite the currently
broken `CodeServerManager.remove_all()` to route through it, and harden
`CodeHostRepo.push()` in `cspawn/cs_github/repo.py` with a bounded
subprocess timeout and a clean failure mode when the target Swarm service
is already gone.

This ticket is the foundation every other stop path (tickets 002 and
003) will call. It does not itself change any caller's observable
behavior — nothing calls `stop_host()` yet — so it is safe to ship
standalone. See `architecture-update.md` Step 3 (Modules M1/M2) and Step
6 (Design Rationale) for the full reasoning behind the manager-level
choke point, the best-effort push contract, and the new timeout.

Motivating problem (from `clasi/issues/push-on-host-stop.md`): the
commit+push machinery (`CodeHostRepo.push()`,
`cspawn/cs_github/repo.py:73-116`) exists and is proven in `host purge`,
but nine other stop paths bypass it entirely, silently dropping student
work that was never pushed to GitHub.

## Acceptance Criteria

- [x] `StopResult` `@dataclass` added to `cspawn/cs_docker/csmanager.py`
      with fields: `service_name: str`, `pushed: bool`, `push_error:
      Optional[str]`, `stopped: bool`, `stop_error: Optional[str]`,
      `deleted: bool`, `skipped_push_mia: bool`.
- [x] `CodeServerManager.stop_host(self, code_host: CodeHost, *, push:
      bool = True, branch: str = "master") -> StopResult` added,
      performing, in order: (1) push — best-effort, skipped cleanly with
      an INFO log if `code_host.is_mia`; (2) stop the Swarm service —
      best-effort, and a missing/already-gone service counts as a
      successful stop; (3) delete the `CodeHost` DB row — best-effort,
      with rollback on failure. No step ever raises out of `stop_host()`.
- [x] A mocked push failure (`CodeHostRepo.push` raising) does not
      prevent `stop_host()` from stopping the service and deleting the
      DB row; `StopResult.push_error` is populated; an ERROR-level log
      line is emitted (`cspawn.docker` logger).
- [x] A host with `code_host.is_mia == True` skips the push step
      entirely: `CodeHostRepo.push` is never invoked;
      `StopResult.skipped_push_mia == True`; an INFO-level (not ERROR)
      log line is emitted.
- [x] `stop_host(code_host, push=False)` never calls `CodeHostRepo.push`
      under any circumstance, regardless of `is_mia`.
- [x] `CodeServerManager.remove_all(self, *, push: bool = True) ->
      list[StopResult]` rewritten to iterate `CodeHost.query.all()` and
      call `stop_host(ch, push=push)` per row; the previous
      `self.repo.remove_by_id(...)` call (`self.repo` does not exist
      anywhere on `CodeServerManager` — confirmed by reading the full
      class and grepping `self\.repo\b` across `cspawn/`) is removed.
- [x] `CodeHostRepo.push()` (`cspawn/cs_github/repo.py:73-116`) accepts
      an optional `timeout` parameter (seconds), defaulting to
      `self.app.app_config.get("CODEHOST_PUSH_TIMEOUT_S", 30)`, passed
      to the existing `subprocess.run(argv, capture_output=True,
      text=True)` call.
- [x] A `subprocess.TimeoutExpired` raised by that subprocess call is
      caught and re-raised as `RuntimeError` naming the host and the
      timeout — matching the existing `RuntimeError` contract every
      current caller already handles via a bare `except Exception`.
      Verified in tests by mocking `subprocess.run` to raise
      `TimeoutExpired` (no real sleeping/hanging in the test).
- [x] `CodeHostRepo._get_service_container()` (`repo.py:53-60`) raises
      `ValueError(f"No service found for {self.service_name}")` when
      `self.app.csm.get(self.service_name)` returns `None`, instead of
      letting a bare `AttributeError` escape from `service.containers`.
- [x] New config key `CODEHOST_PUSH_TIMEOUT_S` is documented (a comment
      or config-loading note is sufficient; no config file changes are
      required for the default of 30 to apply).
- [x] Unit tests cover every criterion above with no live Docker,
      GitHub, or network access.

## Implementation Plan

### Approach

1. **`cspawn/cs_github/repo.py`**: add `timeout: Optional[float] = None`
   to `CodeHostRepo.push()`'s signature. Resolve the effective timeout as
   `timeout or self.app.app_config.get("CODEHOST_PUSH_TIMEOUT_S", 30)`.
   Pass `timeout=effective_timeout` into the existing
   `subprocess.run(argv, capture_output=True, text=True)` call (line
   ~109). Wrap it: `try: proc = subprocess.run(...) except
   subprocess.TimeoutExpired as e: raise RuntimeError(f"git push timed
   out after {effective_timeout}s for {self.username} on {node_host}")
   from e`. In `_get_service_container()` (lines 53-60), add `if service
   is None: raise ValueError(f"No service found for {self.service_name}")`
   immediately after `service = self.app.csm.get(self.service_name)`,
   before `list(service.containers)`.
2. **`cspawn/cs_docker/csmanager.py`**: add `from dataclasses import
   dataclass` and ensure `Optional` is imported from `typing` (already
   imported at the top of the file). Add `CodeHostRepo` to the existing
   `from cspawn.cs_github.repo import ...` import block (alongside
   `GithubOrg` at line 22, `StudentRepo` at line 27 — consolidate into
   one import if convenient). Define `StopResult` as a module-level
   dataclass placed near `CSMService`. Add `stop_host()` and rewrite
   `remove_all()` as methods on `CodeServerManager`, near the existing
   `stop_cs()` (line 670) and `remove_all()` (line 831).
3. **`stop_host()` internals**: use `code_host.is_mia` (existing hybrid
   property on `CodeHost`, `models.py:418-423`) for the clean-skip check.
   Construct `CodeHostRepo(code_host, self.app)` directly (the class's
   plain constructor, not the `new_codehostrepo` classmethod, which
   re-queries the DB by username — `stop_host` already has the row). Use
   `self.get(code_host)` (existing `CodeServerManager.get`, already
   handles `CodeHost -> service_id` resolution and the SSH semaphore) to
   fetch the live service for the stop step. Use the already-imported
   `db` from `cspawn.models` (line 23) for the delete/commit step, with
   `db.session.rollback()` on failure.

### Files to create / modify

- `cspawn/cs_github/repo.py` — `CodeHostRepo.push()`,
  `CodeHostRepo._get_service_container()`.
- `cspawn/cs_docker/csmanager.py` — new `StopResult`, new
  `CodeServerManager.stop_host()`, rewritten
  `CodeServerManager.remove_all()`, updated `cs_github.repo` import.
- New test file `test/test_stop_host.py` — unit tests per the acceptance
  criteria above.

### Testing plan

- Mock-based unit tests only (no live Docker/GitHub), following the
  patterns already established in this repo: the in-memory-SQLite +
  `MagicMock`-`app.csm` pattern from
  `test/test_autoscale.py::TestApplyReaperZones` (`_make_reaper_flask_app`,
  `_make_app_with_mock_csm`) for DB-backed `CodeHost`/`stop_host`
  round-trips, and `unittest.mock.patch` for `CodeHostRepo.push` /
  `subprocess.run`.
- Cases to cover: push succeeds; push raises; push times out (mocked
  `TimeoutExpired`, asserting no real delay); `push=False`; `is_mia=True`;
  swarm-stop raises; swarm service already gone (`self.get()` returns
  `None`, still counts as `stopped=True`); DB delete raises (rollback
  path, `deleted=False`); `remove_all()` calls `stop_host()` once per
  `CodeHost` row and returns one `StopResult` per row.
- Run `uv run pytest test/ -v` to confirm no regressions elsewhere.

### Documentation updates

None required — internal orchestrator with no user-facing surface yet.
Ticket 003 covers CLI `--help` text for the flags that consume `push=`.
