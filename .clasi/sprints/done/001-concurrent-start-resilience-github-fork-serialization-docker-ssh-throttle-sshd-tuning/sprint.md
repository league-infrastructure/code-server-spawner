---
id: '001'
title: "Concurrent Start Resilience — GitHub fork serialization, Docker SSH throttle,\
  \ sshd tuning"
status: planning-docs
branch: sprint/001-concurrent-start-resilience-github-fork-serialization-docker-ssh-throttle-sshd-tuning
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
issues:
  - load-test-20-test-students-across-swarm-nodes.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 001: Concurrent Start Resilience — GitHub fork serialization, Docker SSH throttle, sshd tuning

## Goals

Make the application absorb 20 concurrent student host starts without failure.
The `cspawnctl test start --concurrency 20` load test already exists as a
harness; this sprint fixes the two application bottlenecks and one infra
config gap it exposed so the test passes end-to-end.

## Problem

A 20-wide concurrent start (`cspawnctl test start --concurrency 20`) exposes
two failure modes and one configuration gap:

1. **GitHub fork serialization gap**: `GithubOrg.fork` issues a bare
   `create_fork` API call with no concurrency guard. GitHub returns
   `403 "Repository is already being forked"` for all but the first request
   when 20 threads try to fork the same upstream (`Python-Apprentice`)
   simultaneously, and `429` secondary rate-limit errors on rapid retries.

2. **SSH overrun**: All Docker Swarm operations share a single
   `ssh://root@swarm1` transport. 20 concurrent threads saturate the SSH
   channel, causing `kex_exchange_identification: Connection reset` and
   `BrokenPipe` errors. sshd's default `MaxStartups 10:30:100` drops
   handshakes once 10 are in progress.

3. **sshd baseline too conservative**: The OS default `MaxStartups 10:30:100`
   makes the kernel the throttle rather than the application, and is not
   written into node provisioning, so existing and new nodes remain at the
   low default.

## Solution

Three targeted fixes, each confined to its natural boundary:

- **GitHub layer**: Add a module-level per-upstream `threading.Lock` and a
  bounded retry/backoff loop around `create_fork` in `GithubOrg.fork`. Forks
  of different upstreams remain concurrent; only same-upstream forks are
  serialized.

- **Docker/SSH layer**: Add a `threading.BoundedSemaphore(4)` (configurable
  via `DOCKER_SSH_CONCURRENCY`) to `CodeServerManager`. Guard `new_cs`,
  `list`, `get`, and `get_by_username` with the semaphore. Extract `_list_raw`
  to break the reentrant-deadlock path (`new_cs` → `get_by_username` → `list`).

- **Infra config**: Write `MaxStartups 30:60:100` into sshd_config on both
  new nodes (via cloud-init) and the existing manager (via
  `manager-setup-swarm.sh`). Confirm UFW port-22 rule is a plain allow (not
  a rate-limit rule) — no change needed there.

## Success Criteria

- `cspawnctl test start --concurrency 20` completes with all 20 hosts
  reaching `ok=True`; no GitHub 403/429 or SSH connection errors in logs.
- Single-student web Start via `/class/{id}/start` still works correctly and
  is not noticeably slower.
- Both `swarm-node-init-v2.yaml` and `manager-setup-swarm.sh` contain
  `MaxStartups 30:60:100`.

## Scope

### In Scope

- `cspawn/cs_github/repo.py`: per-upstream fork lock + retry loop in
  `GithubOrg.fork`.
- `cspawn/cs_docker/csmanager.py`: `BoundedSemaphore` on `CodeServerManager`,
  `_list_raw` deadlock prevention, `DOCKER_SSH_CONCURRENCY` config key.
- `config/cloud-init/swarm-node-init-v2.yaml`: sshd `MaxStartups` write +
  restart step.
- `config/host-scripts/manager-setup-swarm.sh`: sshd `MaxStartups` block +
  comment about existing `swarm1`.
- `config/host-scripts/firewall.sh`: clarifying comment confirming no
  port-22 rate-limit rule.

### Out of Scope

- Building or modifying the `cspawnctl test` CLI group — that is already
  implemented (prior work from the source issue's main body).
- Node distribution testing (requires ≥2 swarm nodes; only `swarm1` exists).
  Use `cspawnctl node expand` separately before distribution testing.
- Port collision fix for `get_unused_port` under concurrent load — this is
  a known contention point but is explicitly deferred; failures surface in
  the load-test report.
- Reaper/cron semaphore interaction tuning — the `sync` job may be slightly
  delayed by the semaphore but this is acceptable (cron delay is non-critical).
- Per-call `DockerClient` instances (rejected: adds SSH handshake overhead on
  every `list` / `get` call).

## Test Strategy

All acceptance verification is performed against the `local-prod` deploy (real
Docker Swarm, real GitHub API):

1. `cspawnctl -d local-prod test setup` — create class + 20 test students
   (idempotent).
2. `cspawnctl -d local-prod test start --concurrency 20` — parallel start;
   expect all 20 `ok=True`, latency table showing queueing effect.
3. Inspect logs for absence of 403/429/SSH errors.
4. Single web Start in browser for one non-test student to confirm no
   regression.
5. `cspawnctl -d local-prod test teardown` — clean up hosts, forks, students.
6. `uv run pytest` — run unit tests to catch import or instantiation regressions.

## Architecture Notes

- The fork lock is module-level (not instance-level) because `GithubOrg` is
  instantiated per-call inside `new_cs`. Module scope ensures all threads in
  the process share the same lock dictionary.
- The semaphore is instance-level on `CodeServerManager` because it guards a
  single shared `DockerClient`; `app.csm` is the singleton shared across all
  requests.
- `MaxStartups 30:60:100` means: start probabilistic rejection at 30 in-flight
  handshakes, drop 60% at the threshold, cap at 100. With the app semaphore
  at 4, the app is always well below 30; the raised limit is headroom for other
  SSH users of the manager (operators, cron).

## GitHub Issues

None linked at sprint level. Ticket 001 is linked to issue
`load-test-20-test-students-across-swarm-nodes.md`.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Serialize GitHub forks of the same upstream | — |
| 002 | App-level semaphore on Docker/SSH manager calls | 001 |
| 003 | Raise sshd MaxStartups in cloud-init and manager setup | — |

Tickets execute serially in the order listed (001 → 002 → 003).
