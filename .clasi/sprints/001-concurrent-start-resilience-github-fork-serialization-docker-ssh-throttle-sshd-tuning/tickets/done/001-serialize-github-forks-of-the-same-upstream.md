---
id: '001'
title: Serialize GitHub forks of the same upstream
status: done
use-cases:
- SUC-001
depends-on: []
issue: load-test-20-test-students-across-swarm-nodes.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Serialize GitHub forks of the same upstream

## Description

`GithubOrg.fork` in `cspawn/cs_github/repo.py` calls
`upstream_repo.create_fork(organization=self.org)` without any concurrency
guard. When 20 students click "Start" simultaneously and all share the same
upstream repo (`Python-Apprentice`), GitHub returns
`403 "Repository is already being forked"` for every thread that arrives
while the first fork is still in progress, and `429` for rapid retries.

Add a per-upstream `threading.Lock` (keyed by upstream URL) and a bounded
retry loop around the `create_fork` call so concurrent requests for the
same upstream are serialized. Forks of different upstreams continue in
parallel. The existing `_wait_repo_ready` and `_rename_with_retry` helpers
are not changed; only the initial fork POST needs the lock.

## Acceptance Criteria

- [x] `GithubOrg.fork` acquires a per-upstream `threading.Lock` before
      calling `create_fork` and releases it after (even on exception, via
      context manager).
- [x] A module-level `_fork_locks: dict[str, threading.Lock]` and a
      `_fork_locks_mu: threading.Lock` guard are added to `repo.py`;
      a helper `_get_fork_lock(upstream_url)` creates the lock on first
      access under `_fork_locks_mu`.
- [x] The retry loop around `create_fork` retries up to 8 times with
      exponential backoff (starting at 2 s, capped at 30 s) on:
      - HTTP 403 with body containing `"already being forked"`
      - HTTP 429 (secondary rate limit)
- [x] All other exceptions from `create_fork` propagate immediately
      without retry.
- [x] Forks of different upstream URLs are NOT serialized; they proceed
      concurrently.
- [ ] `cspawnctl test start --concurrency 20` produces 0 GitHub 403/429
      errors; all 20 students receive uniquely named forks.
- [ ] A single-student web Start via `/class/{id}/start` still completes
      successfully without noticeable added latency.

## Implementation Plan

### Approach

Add two module-level globals to `cspawn/cs_github/repo.py`:

```python
import threading
import time  # already used in helpers; ensure it is at module top

_fork_locks: dict[str, threading.Lock] = {}
_fork_locks_mu = threading.Lock()

def _get_fork_lock(upstream_url: str) -> threading.Lock:
    with _fork_locks_mu:
        if upstream_url not in _fork_locks:
            _fork_locks[upstream_url] = threading.Lock()
        return _fork_locks[upstream_url]
```

Inside `GithubOrg.fork`, replace the bare `upstream_repo.create_fork(...)`
call with a lock-guarded retry loop. The `_wait_repo_ready` and
`_rename_with_retry` calls remain outside the lock — they are polling
or per-student operations that do not conflict across threads.

The PyGithub `GithubException` carries a `.status` (int) and `.data`
(dict or str). Match on `status == 403` with `"already being forked"` in
the string representation of `.data`, and on `status == 429`.

### Files to Modify

- `cspawn/cs_github/repo.py`
  - Add `_fork_locks`, `_fork_locks_mu`, `_get_fork_lock` at module scope.
  - Modify `GithubOrg.fork` (~line 358): wrap `create_fork` call with the
    per-upstream lock and retry loop.
  - Ensure `import threading` is present at the top of the file.
  - Ensure `import time` is at module top (currently inside helper methods;
    move to module level).

### Files to Create

None.

### Testing Plan

- Run `cspawnctl -d local-prod test setup` then
  `cspawnctl -d local-prod test start --concurrency 20`; verify:
  - No 403/429 errors in application logs.
  - All 20 result rows in the output table show `ok=True`.
  - The League-Students GitHub org has 20 repos named
    `Python-Apprentice-teststudentNN`.
- Run a single web Start for one student; confirm it succeeds.
- Run `cspawnctl -d local-prod test teardown` to clean up.

### Documentation Updates

None. The change is internal to `GithubOrg.fork`; no public interface changes.
