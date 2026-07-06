---
id: '011'
title: "Fix push-on-stop transport \u2014 ssh node docker exec instead of missing\
  \ docker CLI"
status: done
branch: sprint/011-fix-push-on-stop-transport-ssh-node-docker-exec-instead-of-missing-docker-cli
use-cases: []
issues:
- push-transport-ssh-node-docker-exec.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 011: Fix push-on-stop transport — ssh node docker exec instead of missing docker CLI

## Goals

Make push-on-stop actually work when triggered from the deployed spawner
container. Today, `CodeHostRepo.push()` shells out to the `docker` CLI
(`docker -H ssh://root@<node> exec ...`), and the deployed spawner image
never installs that CLI — every container-initiated push fails with
`[Errno 2] No such file or directory: 'docker'`. Rewrite `push()`'s
transport to use `ssh` (already in the image) to run `docker exec`
**locally on the node**, which needs no spawner image change and also
removes the SSH-tunneled Docker API hijack (`BrokenPipeError`) that
motivated the original, broken `docker` CLI workaround.

## Problem

`CodeHostRepo.push()` (`cspawn/cs_github/repo.py:72-131`) commits and
pushes a student's workspace to GitHub. It currently invokes the `docker`
CLI over an SSH transport (`docker -H ssh://root@<node> exec -u vscode -e
GITHUB_TOKEN=... <cid> sh -c "<git commands>"`). The spawner image
(`docker/Dockerfile`, `python:3.11-slim`) never installs a `docker` CLI —
confirmed live 2026-07-06 (`which docker` inside the running
`codeserver_codeserver` container returns nothing). Push-on-stop has
therefore **never worked from the deployed spawner**; it only ever
appeared to work when triggered from an operator's laptop (which has the
CLI), which is how this slipped past sprint 007's own (correctly written,
but transport-agnostic-mocked) tests. See
`clasi/issues/push-transport-ssh-node-docker-exec.md` for the full
confirmed diagnosis.

## Solution

Rewrite `push()`'s transport, and only its transport:

- Verify `ssh` is on `PATH` before doing anything else; raise a named,
  clear `RuntimeError` if it is not (fail loud, no bare `[Errno 2]`).
- Build the existing git script (`cd "$WORKSPACE_FOLDER"`,
  `GIT_TERMINAL_PROMPT=0`, `git commit -a -m"Automated commit" || true &&
  git push "<remote>"<refspec>`, remote from `JTL_REPO`) unchanged, with a
  new first line, `export GITHUB_TOKEN="<token>"`, prepended.
- Pipe that script to the subprocess's **stdin** — never place the token
  (or the script) in `argv`.
- Run `ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o
  BatchMode=yes root@<node-fqdn> docker exec -u vscode -i <container_id>
  sh -s`, wrapped in the existing `CODEHOST_PUSH_TIMEOUT_S` timeout, with
  the existing `RuntimeError`-on-failure contract (trimmed stderr on
  non-zero exit, named message on timeout) unchanged.

`docker exec` then runs against the node's own local Docker socket — no
SSH-tunneled Docker API call, no dependency on a `docker` CLI binary
inside the spawner image. See `architecture-update.md` for the full
7-step design and rationale.

## Success Criteria

- `push()`'s subprocess argv is `ssh ... docker exec ... sh -s` — no
  `docker` CLI invocation on the spawner side, no `-H ssh://` transport.
- `GITHUB_TOKEN` never appears in any process's argv (verified by test).
- Existing timeout / non-zero-exit `RuntimeError` contract, and every
  existing `test_stop_host.py` case, still pass (updated only where they
  assert the old argv shape).
- A missing `ssh` binary raises a clear, named error instead of a bare
  `[Errno 2]`.
- Manual smoke test after deploy: stop one real host, confirm the commit
  actually lands on GitHub (push-on-stop has never succeeded from the
  deployed spawner before this fix — this is the first real end-to-end
  validation).

## Scope

### In Scope

- Rewriting `CodeHostRepo.push()`'s transport (`cspawn/cs_github/repo.py`)
  to use `ssh <node-fqdn> docker exec ...` with token/script delivered via
  stdin.
- A fail-loud `ssh`-availability preflight inside `push()`.
- Updating/adding unit tests in `test/test_stop_host.py` (or a new file)
  covering the new argv shape, token-safety, and the preflight.

### Out of Scope

- Unifying push behavior/observability across every stop path — that
  remains the separate, still-open sprint-007 `push-on-host-stop` issue.
- Any S3/rclone backup on the stop path; no secondary save mechanism is
  added if `push()` still fails for a real reason (network partition,
  GitHub outage).
- Any change to `pull()` or `StudentRepo` (both use docker-py's
  `exec_run` against a local Docker socket, never SSH — they don't share
  this defect) or `HostS3Sync`.
- Any spawner image / `Dockerfile` change — Option 3 needs none; `ssh` and
  its non-interactive config (`docker/ssh-config`) are already present.

## Test Strategy

Unit tests only, no live Docker/GitHub/network access, following the
existing `test/test_stop_host.py::TestCodeHostRepoPush` mock pattern
(`subprocess.run` mocked; in-memory-SQLite Flask app + `MagicMock`
service/container). New/updated cases:

- Argv shape: `ssh` (not `docker`) is the executed binary; `docker exec`
  appears in the remote command; no `-H ssh://` transport.
- Token safety: the literal token value and the string `GITHUB_TOKEN=`
  never appear in the `argv` passed to `subprocess.run`; the token is
  present in the `input=`/stdin payload instead.
- Preflight: `shutil.which` mocked to return `None` raises a `RuntimeError`
  naming `ssh`, before `subprocess.run` is ever called.
- Regression: existing timeout, non-zero-return-code, and
  `_get_service_container` missing-service cases from
  `test/test_stop_host.py` continue to pass against the rewritten
  internals.
- Full suite: `uv run pytest test/ -v` green (excluding the known
  pre-existing `test_admin_coverage.py` PRODUCTION-env failures).

## Architecture Notes

See `architecture-update.md` for the full analysis. Summary: this is a
single-module change confined to `CodeHostRepo.push()` — no new file, no
schema change, no new intra-codebase dependency. The only dependency
shift is at the external-binary level (trading a `docker` CLI the image
never shipped for the `ssh` binary it already has), removing the
SSH-tunneled Docker API hijack that caused the original `BrokenPipeError`
motivating the broken CLI workaround. `push()`'s external contract
(signature, return value, exceptions raised) is unchanged, so
`CodeServerManager.stop_host()` (sprint 007) and all nine of its callers
need no changes.

## GitHub Issues

None yet filed for this sprint (internal `.clasi/issues` tracking only:
`push-transport-ssh-node-docker-exec.md`).

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (`architecture_review` gate recorded: passed, 2026-07-06)
- [x] Stakeholder has approved the sprint plan (`stakeholder_approval` gate
      recorded by the team-lead, 2026-07-06; sprint advanced to `ticketing`)

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Rewrite `CodeHostRepo.push()` to use ssh-based node-local docker exec transport | — |

Tickets execute serially in the order listed.
