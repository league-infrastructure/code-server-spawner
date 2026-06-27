---
sprint: "001"
title: Concurrent Start Resilience
status: closed
---

# Sprint 001 Close Report — Concurrent Start Resilience

## Goal

Make the spawner absorb ~20 students starting code-server hosts simultaneously
(real classroom load) without failing. A prior 20-wide `cspawnctl test start`
run failed 18/20 due to two application bottlenecks; this sprint fixed them in
the app rather than throttling the test.

## Tickets (3/3 done)

| # | Title | Change |
|---|-------|--------|
| 001 | Serialize GitHub forks of the same upstream | Per-upstream `threading.Lock` + bounded retry/backoff (8×, 2→30s) on GitHub 403 "already being forked" / 429 in `GithubOrg.fork` (`cspawn/cs_github/repo.py`). |
| 002 | App-level semaphore on Docker/SSH manager calls | `threading.BoundedSemaphore(DOCKER_SSH_CONCURRENCY=4)` guarding `new_cs`/`list`/`get`/`get_by_username` in `CodeServerManager`; `_list_raw`/`_get_by_username_raw`/`_new_cs_inner` raw helpers avoid reentrant deadlock on the 409 recovery path (`cspawn/cs_docker/csmanager.py`). |
| 003 | Raise sshd MaxStartups | `MaxStartups 30:60:100` via `99-swarm.conf` in cloud-init + manager setup; UFW reviewed (plain allow, no change) (`config/cloud-init/swarm-node-init-v2.yaml`, `config/host-scripts/`). |

## Verification (live, 3-node swarm)

Ran `cspawnctl -d local-prod test start` (concurrency 20) against a real swarm
(swarm1/2/3, all Docker 27.4.1):
- **Zero** GitHub 403/429 — forks serialized cleanly.
- **Zero** SSH `kex_exchange_identification` / BrokenPipe — semaphore held; no deadlock.
- Hosts **distributed across all three nodes** and reached HTTP 200.

## Out-of-band changes folded in

- **Test-only DB-pool fix** (`cspawn/cli/test.py`): the parallel start worker now
  releases its SQLAlchemy session before the readiness poll (and in `finally`) so
  20 concurrent workers don't exhaust the QueuePool (size 5 + overflow 10). This
  was surfaced by the live test, not a planned ticket.
- CLASI was initialized this sprint (first sprint): minimal design docs under
  `docs/design/` + `.clasi/design/`.

## Secrets note

`config/secrets/local-prod.env` and `config/secrets/prod.env` carry an
uncommitted local DO_TOKEN rotation that was deliberately **not** committed.
Backlog issue `purge-secrets-from-git-history` will remove secrets from git.

## Follow-up findings (filed / to file as issues)

- Cold Docker image-pull dominates host start latency on a fresh node.
- Swarm spread distribution is uneven (favors the emptier node).
- Provisioning installs latest Docker (29.x) → join-version mismatch vs the 27.x
  swarm; pin Docker version in cloud-init.
- `_node_manager` should fall back to the node's swarm IP when its
  `NODE_HOSTNAME_TEMPLATE` hostname doesn't resolve (DNS-before-introspection).
- Backlog: `purge-secrets-from-git-history`, `dotconfig-migration`.
