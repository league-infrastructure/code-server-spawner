---
status: done
sprint: '011'
tickets:
- 011-001
---

# push-on-stop is broken in the deployed spawner: `push()` shells out to a `docker` CLI that isn't in the image — switch to `ssh <node> docker exec`

## Summary

`CodeHostRepo.push()` ([repo.py:114-118](cspawn/cs_github/repo.py#L114-L118))
commits and pushes student work by invoking the **`docker` CLI**:
`docker -H ssh://root@<node> exec -u vscode -e GITHUB_TOKEN=… <cid> sh -c "git commit && git push"`.
But the spawner image ([docker/Dockerfile](docker/Dockerfile), `FROM python:3.11-slim`)
installs git/ssh/cron/etc. and **never installs the docker CLI**. So the
subprocess fails with `[Errno 2] No such file or directory: 'docker'`, surfaced
to the student as *"Host stopped, but your work may not have been fully saved
(push failed: …)"* ([hosts.py:39-44](cspawn/main/routes/hosts.py#L39)).

Confirmed live 2026-07-06: `which docker` inside the running
`codeserver_codeserver` container returns nothing. **Push-on-stop has never
worked from the deployed spawner** — it only ever worked when triggered from a
machine that happens to have the docker CLI (an operator's laptop via
`cspawnctl host purge`), which is why this slipped past sprint 007 (that sprint's
`push-on-host-stop` issue is still `in-progress`).

There is **no S3/backup on the stop path**: `CSMService`/`stop_host`
([csmanager.py:720-762](cspawn/cs_docker/csmanager.py#L720)) only calls
`CodeHostRepo.push()`; `HostS3Sync` is wired only into `cli/host.py`, not stop.
So when push fails, the student's work is genuinely not saved on that stop.

## Root cause

`push()` was switched to the `docker -H ssh://` CLI in commit `692537f`
("reliable git push") to avoid `BrokenPipeError` from docker-py's `exec_run`
over the SSH docker transport (the exec stream/hijack dies over SSH). But the
CLI it depends on was never added to the image, and the `docker -H ssh://`
transport still performs the same fragile SSH docker-API hijack the CLI was
meant to tolerate.

## Fix — Option 3: `ssh <node> docker exec` (stakeholder-selected)

Rewrite `push()` to shell out to **`ssh`** (already present in the image, with
`/root/.ssh/id_rsa` and `/etc/ssh/ssh_config.d/dojtl.conf`) and run
`docker exec` **locally on the node**:

```
ssh root@<node-fqdn> docker exec -u vscode -i <container_id> sh -c '<git cmd>'
```

Because `docker exec` now runs against the node's **local** docker socket, there
is no SSH docker-API hijack — this fixes both the missing-CLI error *and* the
`BrokenPipeError` root cause that motivated the CLI in the first place, with **no
spawner image change**. Proven live 2026-07-06: `ssh root@swarm2.dojtl.net
"docker ps …"` executed successfully from inside the deployed spawner container.

Requirements / constraints:
- **Token handling:** pass `GITHUB_TOKEN` to the container without putting it in
  any process's argv. Prefer piping the git script (and/or the token) via
  `ssh … docker exec -i … sh -s` on **stdin**, or `--env-file /dev/stdin`. Do
  not regress the current `-e GITHUB_TOKEN=` argv exposure into the ssh argv
  (it would then appear on both the spawner's ssh process and the node's sshd
  command line).
- Preserve existing behavior: `cd "$WORKSPACE_FOLDER"`, `GIT_TERMINAL_PROMPT=0`,
  `git commit -a -m"Automated commit" || true && git push "<remote>"<refspec>`,
  remote built from `JTL_REPO`, run as user `vscode`.
- Keep the `CODEHOST_PUSH_TIMEOUT_S` timeout (wrap the `ssh` subprocess) so a
  wedged connection never hangs the caller; keep raising `RuntimeError` on
  non-zero return with trimmed stderr.
- Node FQDN from `NODE_HOSTNAME_TEMPLATE.format(nodename=<hostname>)` and the
  container's node hostname, exactly as today.
- Use the same non-interactive ssh options the image already relies on
  (`DOCKER_SSH_CMD`/ssh_config: `StrictHostKeyChecking=no`, batch mode).

## Also in scope

- **Fail loud:** add a lightweight preflight/health check (matching sprint 009's
  fail-loud pattern) — e.g. verify `ssh` is on PATH at push time (or app boot)
  and raise a clear, named error instead of a bare `[Errno 2]`.

## Out of scope

- Unifying push across every stop path (the separate, still-open sprint-007
  `push-on-host-stop` issue). This issue only fixes the push **mechanism** so
  that issue's callers actually work.
- Re-introducing an S3/rclone backup on the stop path.
- Any change to `pull()` / `HostS3Sync` (they use docker-py `exec_run`; leave
  them unless a test proves them broken).

## Acceptance criteria (draft)

- [ ] `push()` invokes `ssh <node> docker exec …` (no `docker` CLI dependency in
  the spawner image); unit test asserts the argv shape and that `GITHUB_TOKEN`
  never appears in any argv (passed via stdin/env-file).
- [ ] Non-zero ssh/exec return raises `RuntimeError` with trimmed stderr; a
  timeout raises the existing timeout `RuntimeError`.
- [ ] Token/commit/push semantics unchanged (remote from `JTL_REPO`, `vscode`
  user, `WORKSPACE_FOLDER`, `GIT_TERMINAL_PROMPT=0`).
- [ ] A missing `ssh` binary fails loudly with a clear message, not a bare
  `[Errno 2]`.
- [ ] Existing push/host tests updated; suite green (excluding the known
  pre-existing `test_admin_coverage.py` PRODUCTION-env failures).
