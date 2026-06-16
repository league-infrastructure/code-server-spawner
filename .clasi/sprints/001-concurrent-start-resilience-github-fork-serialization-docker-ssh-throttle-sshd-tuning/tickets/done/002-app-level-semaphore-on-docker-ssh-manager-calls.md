---
id: '002'
title: App-level semaphore on Docker/SSH manager calls
status: done
use-cases:
- SUC-002
depends-on:
- '001'
issue: ''
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# App-level semaphore on Docker/SSH manager calls

## Description

All host operations go over a single `DockerClient` connected via
`ssh://root@swarm1` (`DOCKER_URI`). When 20 threads call `CodeServerManager`
methods concurrently, the SSH channel is saturated and the sshd `MaxStartups`
default (10) drops connections, producing `kex_exchange_identification:
Connection reset` and `BrokenPipe` errors.

Add a `threading.BoundedSemaphore` as an instance attribute on
`CodeServerManager`. Its capacity is read from the app config key
`DOCKER_SSH_CONCURRENCY` (default `4`). Guard `new_cs`, `list`, `get`, and
`get_by_username` with this semaphore using `try/finally`.

Deadlock risk: `new_cs` calls `get_by_username` on a 409 error path, and
`get_by_username` calls `list`. To prevent reentrant deadlock, extract the
raw Docker `list` call into a private `_list_raw` method that does not
acquire the semaphore. The public semaphore-guarded `list` and
`get_by_username` methods delegate to `_list_raw`.

## Acceptance Criteria

- [x] `CodeServerManager.__init__` creates a `threading.BoundedSemaphore`
      with capacity from `app.app_config.get("DOCKER_SSH_CONCURRENCY", 4)`.
- [x] `new_cs`, `list`, `get`, and `get_by_username` each acquire the
      semaphore on entry and release it on exit via `try/finally`.
- [x] A private `_list_raw` method (not semaphore-guarded) contains the
      underlying `super().list(filters=...)` call; the public `list` and
      `get_by_username` methods call `_list_raw` inside their semaphore
      guard.
- [x] `new_cs` calls `_list_raw`-based `_get_by_username_raw` (or the same
      pattern) on its internal 409 recovery path, not the semaphore-guarded
      public `get_by_username`.
- [ ] `cspawnctl test start --concurrency 20` shows no SSH
      `kex_exchange_identification` / `BrokenPipe` errors.
- [ ] All 20 `CodeHost` records are committed with state `starting` or better.
- [ ] Single-student web Start via `/class/{id}/start` still completes
      successfully and is not noticeably slower.
- [ ] No deadlock occurs when `new_cs` triggers its internal 409 recovery
      branch under concurrent load.

## Implementation Plan

### Approach

In `CodeServerManager.__init__` (around line 394 in `csmanager.py`), after
the `DockerClient` is created, add:

```python
concurrency = int(self.config.get("DOCKER_SSH_CONCURRENCY", 4))
self._docker_sem = threading.BoundedSemaphore(concurrency)
```

Add a private raw-list helper that calls the parent class `list` without
acquiring the semaphore:

```python
def _list_raw(self, filters={"label": "jtl.codeserver"}):
    return super().list(filters=filters)
```

Wrap the four public methods with the semaphore:

```python
def list(self, filters={"label": "jtl.codeserver"}):
    self._docker_sem.acquire()
    try:
        return self._list_raw(filters=filters)
    finally:
        self._docker_sem.release()

def get(self, service_id):
    if isinstance(service_id, CodeHost):
        service_id = service_id.service_id
    self._docker_sem.acquire()
    try:
        return super().get(service_id)
    finally:
        self._docker_sem.release()

def get_by_username(self, username):
    username = slugify(username)
    self._docker_sem.acquire()
    try:
        for service in self._list_raw():
            if service.username == username:
                return service
        return None
    finally:
        self._docker_sem.release()

def new_cs(self, user, proto, class_):
    self._docker_sem.acquire()
    try:
        return self._new_cs_inner(user, proto, class_)
    finally:
        self._docker_sem.release()
```

Move the body of the existing `new_cs` into `_new_cs_inner`. Inside
`_new_cs_inner`, on the 409 recovery path (line ~560 in csmanager.py):
replace `self.get_by_username(username)` with a call that uses `_list_raw`
directly (not the semaphore-guarded public method), since the semaphore is
already held.

`make_user_dir` uses a separate Paramiko connection and is not guarded;
it remains unchanged.

### Files to Modify

- `cspawn/cs_docker/csmanager.py`
  - Add `import threading` at the top.
  - In `CodeServerManager.__init__`: create `self._docker_sem`.
  - Add `_list_raw` private method.
  - Refactor `new_cs` into `new_cs` (semaphore wrapper) + `_new_cs_inner`
    (body), updating the internal 409 recovery call.
  - Wrap `list`, `get`, `get_by_username` with semaphore guard.

### Files to Create

None.

### Testing Plan

- Run `cspawnctl -d local-prod test start --concurrency 20`; verify:
  - No SSH errors in application logs.
  - All 20 result rows show `ok=True`.
  - Latency table shows the expected queueing effect (groups of ~4 starting
    near-simultaneously due to the semaphore).
- Confirm single web Start still works end-to-end.
- If a test suite exists for `csmanager`, run it; otherwise run
  `uv run pytest` to catch any import or basic instantiation errors.

### Documentation Updates

Add a comment above `self._docker_sem` in `__init__` explaining the
deadlock avoidance pattern, so future maintainers understand why `_list_raw`
exists.
