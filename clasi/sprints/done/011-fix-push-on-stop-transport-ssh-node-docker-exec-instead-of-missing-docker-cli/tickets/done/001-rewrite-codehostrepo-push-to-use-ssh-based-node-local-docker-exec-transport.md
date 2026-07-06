---
id: '001'
title: Rewrite CodeHostRepo.push() to use ssh-based node-local docker exec transport
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on: []
github-issue: ''
issue: push-transport-ssh-node-docker-exec.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Rewrite CodeHostRepo.push() to use ssh-based node-local docker exec transport

## Description

`CodeHostRepo.push()` (`cspawn/cs_github/repo.py:72-131`) commits and
pushes a student's workspace to GitHub by shelling out to the **`docker`
CLI** over an SSH transport: `docker -H ssh://root@<node> exec -u vscode
-e GITHUB_TOKEN=... <cid> sh -c "<git commands>"`. The deployed spawner
image (`docker/Dockerfile`, `python:3.11-slim`) never installs a `docker`
CLI — confirmed live 2026-07-06 (`which docker` inside the running
`codeserver_codeserver` container returns nothing). Every
container-initiated push therefore fails with `[Errno 2] No such file or
directory: 'docker'`, surfaced to the student as *"Host stopped, but your
work may not have been fully saved (push failed: ...)"*
(`cspawn/main/routes/hosts.py:39-44`). Push-on-stop has **never actually
worked from the deployed spawner** — it only appeared to work when
triggered from an operator's laptop (which has the CLI), which is why it
slipped past sprint 007's orchestrator work (`CodeServerManager.stop_host()`),
whose own tests mock `subprocess.run` and never exercise the real binary.

**Root cause**: the `docker -H ssh://` form was adopted in commit
`692537f` ("reliable git push") to avoid a `BrokenPipeError` from
docker-py's `exec_run` over an SSH-tunneled Docker API call. That fix
addressed the wrong layer — the `docker` CLI's own `-H ssh://` transport
still performs the same SSH-tunneled Docker API hijack, and additionally
depends on a `docker` CLI binary the image never shipped.

**Selected fix (Option 3, stakeholder-approved,
`clasi/issues/push-transport-ssh-node-docker-exec.md`)**: reach the node
over a plain `ssh` connection (already present in the image, keyed via
`/root/.ssh/id_rsa`, non-interactively configured for `*.dojtl.net` hosts
via `docker/ssh-config` → `/etc/ssh/ssh_config.d/dojtl.conf`), and run
`docker exec` **on the node itself**, against its own local Docker socket.
This removes both the missing-CLI failure and the `BrokenPipeError` root
cause, with no spawner image change. Proven live 2026-07-06: `ssh
root@swarm2.dojtl.net "docker ps"` succeeds from inside the deployed
spawner container today.

See `../architecture-update.md` (single module: M1 — Push transport) for
the full design, diagrams, and rationale — in particular Step 6's three
decisions: token delivered via a stdin-piped `export GITHUB_TOKEN=` line
(not `--env-file`), explicit non-interactive `ssh -o` flags in the argv
(defense-in-depth alongside `docker/ssh-config`), and the `ssh`-availability
preflight living inside `push()` itself rather than at app boot.

This ticket touches exactly one method's internals
(`CodeHostRepo.push()`) plus the new preflight check it needs. `pull()`,
`StudentRepo`, `HostS3Sync`, `CodeServerManager.stop_host()`, and every
other stop-path caller are unchanged — `push()`'s external contract
(signature, return value, exceptions raised) is preserved.

## Acceptance Criteria

- [x] `push()`'s subprocess argv invokes `ssh` (not `docker`) as the
      executed binary, targeting `root@<node-fqdn>` with a non-interactive
      `docker exec -u vscode -i <container_id> sh -s` remote command — no
      `docker -H ssh://` transport anywhere in the argv.
- [x] `ssh` is invoked with non-interactive options
      (`-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
      -o BatchMode=yes`), mirroring the image's existing `DOCKER_SSH_CMD`
      posture.
- [x] `GITHUB_TOKEN` never appears in any element of the `argv` list
      passed to `subprocess.run` (no `-e GITHUB_TOKEN=...`, no literal
      token value). It is instead delivered as an `export
      GITHUB_TOKEN="..."` line prepended to the git script and passed via
      `subprocess.run(..., input=script, ...)` (stdin), consumed by `sh
      -s` on the remote end.
- [x] Git/push semantics are byte-for-byte unchanged: `cd
      "$WORKSPACE_FOLDER"`, `GIT_TERMINAL_PROMPT=0`, `git commit -a
      -m"Automated commit" || true && git push "<remote>"<refspec>`,
      remote built from `JTL_REPO`'s owner/repo via
      `x-access-token:${GITHUB_TOKEN}@github.com/...`, executed as user
      `vscode` inside the container.
- [x] Node FQDN resolution is unchanged:
      `NODE_HOSTNAME_TEMPLATE.format(nodename=<node hostname from
      container.node.attrs>)`.
- [x] The existing `CODEHOST_PUSH_TIMEOUT_S`-bounded timeout still wraps
      the subprocess call; a `subprocess.TimeoutExpired` is still caught
      and re-raised as `RuntimeError` naming the host and timeout value
      (unchanged contract, already covered by existing tests in
      `test/test_stop_host.py::TestCodeHostRepoPush`).
- [x] A non-zero `ssh`/`docker exec` return code still raises
      `RuntimeError(f"git push failed (rc={proc.returncode}):
      {err[-500:]}")` with trimmed stderr (unchanged contract).
- [x] A missing `ssh` binary (`shutil.which("ssh") is None`) raises a
      clear, named `RuntimeError` (e.g. `"ssh binary not found on PATH —
      cannot push via docker exec"`) **before** any container lookup or
      `subprocess.run` call is attempted — not a bare `[Errno 2] No such
      file or directory`.
- [x] Every existing case in `test/test_stop_host.py` (timeout handling,
      default/explicit/config-driven timeout value, non-zero-returncode
      `RuntimeError`, `_get_service_container` missing-service
      `ValueError`) continues to pass against the rewritten internals,
      updated only where a test's mock/assertion assumed the old
      `docker -H ssh://` argv shape.
- [x] New unit tests (no live SSH/Docker/GitHub/network access) cover:
      argv shape (ssh + docker exec, no `-H`/`ssh://`), token-never-in-argv,
      token-present-in-stdin-input, and the missing-`ssh` preflight.
- [x] Full suite green: `uv run pytest test/ -v` (excluding the known
      pre-existing `test_admin_coverage.py` PRODUCTION-env failures).

## Implementation Plan

### Approach

1. **`cspawn/cs_github/repo.py` — imports**: add `import shutil` near the
   existing local `import subprocess` inside `push()` (or promote both to
   module-level imports if that better matches the file's existing style
   — check the file's current import block before deciding).
2. **Preflight**: at the very top of `push()`, before
   `_get_service_container()` is called, add:
   ```python
   if shutil.which("ssh") is None:
       raise RuntimeError("ssh binary not found on PATH — cannot push via docker exec")
   ```
3. **Keep unchanged**: `effective_timeout` resolution from
   `CODEHOST_PUSH_TIMEOUT_S`, `_get_service_container()` call,
   `_git_environment()` call, `remote`/`refspec` construction from
   `JTL_REPO` via `_parse_repo()`, and the node hostname lookup
   (`container.node.attrs["Description"]["Hostname"]`).
4. **Node FQDN**: rename the existing `node_uri` variable to `node_fqdn`
   (it's now a bare SSH target, not an `ssh://` URI) —
   `self.app.app_config["NODE_HOSTNAME_TEMPLATE"].format(nodename=node_host)`,
   dropping the `ssh://root@` prefix (that prefix moves into the `ssh`
   argv's target argument, `root@{node_fqdn}`).
5. **Script construction**: build the piped script as:
   ```python
   script = (
       f'export GITHUB_TOKEN="{env["GITHUB_TOKEN"]}"\n'
       f'cd "$WORKSPACE_FOLDER" && export GIT_TERMINAL_PROMPT=0 && '
       f'git commit -a -m"Automated commit" || true && git push "{remote}"{refspec}\n'
   )
   ```
   (The `cd`/`export`/`commit`/`push` line is the existing `cmd` string,
   unchanged in content — only the new `export GITHUB_TOKEN=` line is
   prepended, and the whole thing becomes stdin instead of a `sh -c`
   argument.)
6. **Argv construction**: replace the existing
   `["docker", "-H", node_uri, "exec", "-u", "vscode", "-e",
   f"GITHUB_TOKEN={env['GITHUB_TOKEN']}", container.id, "sh", "-c", cmd]`
   with:
   ```python
   argv = [
       "ssh",
       "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null",
       "-o", "BatchMode=yes",
       f"root@{node_fqdn}",
       "docker", "exec", "-u", "vscode", "-i", container.id, "sh", "-s",
   ]
   ```
7. **Subprocess call**: change
   `subprocess.run(argv, capture_output=True, text=True, timeout=effective_timeout)`
   to
   `subprocess.run(argv, input=script, capture_output=True, text=True, timeout=effective_timeout)`.
   Keep the existing `try/except subprocess.TimeoutExpired` and
   non-zero-`proc.returncode` handling exactly as-is (no change needed —
   both operate on `proc`/the caught exception, independent of how stdin
   was supplied).
8. **Docstring/comment**: update the comment above the argv construction
   (currently explains the `BrokenPipeError`/docker-CLI-over-SSH
   rationale from commit `692537f`) to describe the new
   ssh-to-node-then-local-docker-exec rationale, referencing this ticket
   and `../architecture-update.md` instead of the now-superseded rationale.
9. **Logging**: update the existing `self.app.logger.info(f"Executing git
   push for {self.username} on {node_host} ({node_uri})")` line to use
   `node_fqdn` in place of `node_uri`.

### Files to create / modify

- `cspawn/cs_github/repo.py` — `CodeHostRepo.push()` only (the sole
  production code file this ticket touches).
- `test/test_stop_host.py` — update `TestCodeHostRepoPush` and
  `_make_repo_with_mock_service` as needed for the new argv shape; add
  new test cases (see Testing below). No other test file requires
  changes — `TestStopHost`/`TestRemoveAll` mock `CodeHostRepo.push`
  itself via `patch.object`, so they are insulated from this internal
  rewrite.

### Testing plan

- **Existing tests to run**: `uv run pytest test/test_stop_host.py -v`
  (all of `TestStopHost`, `TestRemoveAll`, `TestCodeHostRepoPush`,
  `TestGetServiceContainer`) to confirm no regressions; then the full
  suite.
- **New tests to write** (in `test/test_stop_host.py::TestCodeHostRepoPush`,
  following its existing `_make_repo_with_mock_service` + mocked
  `subprocess.run` pattern):
  - `test_push_argv_uses_ssh_not_docker_cli` — mock `subprocess.run`,
    call `push()`, assert `mock_run.call_args`'s `argv[0] == "ssh"`, that
    `"docker"` and `"exec"` appear later in the argv, and that no argv
    element is `"-H"` or contains `"ssh://"`.
  - `test_push_token_never_appears_in_argv` — assert the literal token
    value (`"test-token"` from the fixture's `GITHUB_TOKEN`) and the
    substring `"GITHUB_TOKEN="` do not appear in any argv element.
  - `test_push_token_delivered_via_stdin` — assert `mock_run.call_args`'s
    `kwargs["input"]` contains both `"GITHUB_TOKEN"` and the token value.
  - `test_push_missing_ssh_raises_named_error` — `patch("shutil.which",
    return_value=None)`, assert `push()` raises `RuntimeError` matching
    `"ssh"`, and assert `subprocess.run` is never called (no container
    lookup side effects either, if easily assertable).
  - Regression-check (no new test needed, just confirm unchanged): the
    existing timeout, default-timeout, explicit-timeout-override, and
    non-zero-returncode tests continue to pass since they assert on
    `kwargs["timeout"]` / the raised exception, not on argv shape.
- **Verification command**: `uv run pytest test/ -v`

### Documentation updates

None required beyond the in-code docstring/comment update on `push()`
itself (step 8 above) — this is an internal transport change with no
user-facing CLI/API/docs surface.
