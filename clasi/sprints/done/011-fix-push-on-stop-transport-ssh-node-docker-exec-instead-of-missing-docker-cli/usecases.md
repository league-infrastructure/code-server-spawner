---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 011 Use Cases — Fix push-on-stop transport

Sprint 007 established the `CodeServerManager.stop_host()` choke point and
enumerated nine caller-specific use cases (SUC-001..009 in
`clasi/sprints/done/007-.../usecases.md`) for *who* triggers a push and
*how the orchestrator tolerates failure*. This sprint does not change any
of those callers or that tolerance contract — it replaces the *transport*
`CodeHostRepo.push()` uses to actually reach the container, which every one
of sprint 007's nine callers depends on transitively. The three use cases
below are scoped to that transport, independent of which caller invoked it.

## SUC-001: A stopped host's work is actually pushed to GitHub from the deployed spawner

**Actor**: System (any `stop_host()` caller — student stop, admin stop,
autoscale reaper, CLI `host stop`/`sys shutdown`/`host purge`, user
teardown, class student removal; see sprint 007 SUC-001..009)

**Preconditions**: A `CodeHost` row backs a live Swarm service; the
service's container is reachable via SSH from its assigned node's FQDN
(`NODE_HOSTNAME_TEMPLATE.format(nodename=<node hostname>)`).

**Trigger**: Any code path calls `CodeHostRepo.push()` (directly, or via
`CodeServerManager.stop_host()`).

**Main flow**:
1. `push()` resolves the target container and its node hostname via the
   existing `_get_service_container()` (unchanged).
2. `push()` builds the node's FQDN from `NODE_HOSTNAME_TEMPLATE` (unchanged).
3. `push()` runs `ssh root@<node-fqdn> docker exec -u vscode -i <container_id>
   sh -s`, piping a script (`cd "$WORKSPACE_FOLDER"`, `git commit -a -m"Automated
   commit" || true`, `git push <remote><refspec>`) on **stdin** — not as a
   `docker` CLI argument, and not through docker-py's SSH exec transport.
4. `docker exec` runs against the node's own **local** Docker socket — no
   SSH hijack of the Docker API, no dependency on a `docker` binary existing
   inside the spawner container.
5. On success, `push()` returns the subprocess return code (`0`).

**Postconditions**: The student's latest commit is pushed to their GitHub
fork's remote branch, from every stop path, when run from the deployed
spawner image — not only from an operator's laptop (today's actual,
confirmed-broken behavior).

**Error flows**:
- Non-zero `ssh`/`docker exec` exit: `push()` raises `RuntimeError` with the
  trimmed stderr (existing contract, unchanged) — surfaced by
  `stop_host()`/`hosts.py` exactly as today.
- Subprocess exceeds `CODEHOST_PUSH_TIMEOUT_S`: `push()` raises the existing
  timeout `RuntimeError` (unchanged).

**Acceptance criteria**:
- [ ] `push()`'s subprocess argv is `ssh ... root@<fqdn> docker exec -u vscode
      -i <cid> sh -s` (or equivalent) — no `docker` CLI invocation on the
      spawner side, no `-H ssh://` transport.
- [ ] A unit test mocking `subprocess.run` asserts the argv shape and that
      the call succeeds (rc=0) without any real SSH/Docker access.
- [ ] Existing timeout and non-zero-exit `RuntimeError` behavior is
      unchanged (regression-tested against the existing `test_stop_host.py`
      cases).

---

## SUC-002: A missing `ssh` binary (or ssh preflight failure) fails loudly with a named, actionable error

**Actor**: Admin / Operator (indirectly — receives the surfaced error via
logs or the stop-host flash message)

**Preconditions**: The spawner image's `ssh` binary is missing, unexecutable,
or otherwise unavailable (a hypothetical regression this check guards
against — the image installs `ssh` today, but nothing currently verifies
that at push time).

**Trigger**: `push()` is invoked (any caller from SUC-001).

**Main flow (guard case)**:
1. Before constructing the `ssh` subprocess call, `push()` verifies `ssh` is
   on `PATH` (e.g. via `shutil.which("ssh")`).
2. If absent, `push()` raises a clear, named `RuntimeError` (e.g. `"ssh
   binary not found on PATH — cannot push via docker exec"`) instead of
   letting a bare `subprocess.run(...)` call fail with an opaque `[Errno 2]
   No such file or directory: 'ssh'`.

**Postconditions**: The operator sees an unambiguous, named cause in logs /
the flash message, matching sprint 009's fail-loud precedent
(`_verify_node_provisioning` — named failure strings, never a bare OS
error) instead of the current bug's actual symptom, `[Errno 2] No such file
or directory: 'docker'`.

**Error flows**: N/A — this *is* the error flow for SUC-001; it exists to
give that failure a clear name.

**Acceptance criteria**:
- [ ] A missing `ssh` binary raises a `RuntimeError` whose message names
      `ssh` explicitly and does not surface as a bare `[Errno 2]`.
- [ ] A unit test patches `shutil.which` to return `None` and asserts the
      named error is raised before any `subprocess.run` call is attempted.

---

## SUC-003: `GITHUB_TOKEN` is never visible in any process's argument list

**Actor**: System (security property, verified by test — no human-facing
trigger)

**Preconditions**: A push is in progress (any caller from SUC-001).

**Trigger**: `push()` constructs and runs its `ssh`/`docker exec` subprocess.

**Main flow**:
1. `push()` builds the git script (commit + push commands) as a string that
   references `${GITHUB_TOKEN}` as a shell variable, exactly as today.
2. `push()` writes the token (as an `export GITHUB_TOKEN=...` line prepended
   to that script) into the subprocess's **stdin**, not into `argv` — the
   `ssh`/`docker exec` command line itself contains no `-e
   GITHUB_TOKEN=...` and no literal token value anywhere in the argument
   list passed to `subprocess.run`.
3. The remote container's shell (`sh -s`, reading the piped script) exports
   the token into its own environment and uses it when `git push` expands
   `${GITHUB_TOKEN}` in the remote URL — identical git/push semantics to
   today, different delivery channel.

**Postconditions**: `GITHUB_TOKEN` appears in neither the spawner's `ssh`
process argv nor the node's `sshd`/`docker exec` command line (visible via
`ps aux` on either host) — closing the exposure the issue calls out as a
requirement, not a regression to carry forward from the old `-e
GITHUB_TOKEN=...` argv approach.

**Error flows**: None specific to this use case.

**Acceptance criteria**:
- [ ] A unit test inspects the exact `argv` list passed to the mocked
      `subprocess.run` call and asserts the literal token value (and the
      string `GITHUB_TOKEN=`) does not appear anywhere in it.
- [ ] The token is instead present in the `input=`/stdin payload passed to
      `subprocess.run`, verified by the same test.
