---
status: in-progress
sprint: 008
tickets:
- 008-001
- 008-002
- 008-003
---

# Stale swarm node references break node-targeted host operations (push, sync, stop)

## Description

When a `CodeHost`'s underlying Swarm task references a node ID that no
longer exists in the cluster, any code path that resolves
`container.node` raises an uncaught `docker.errors.NotFound` (404 on
`GET /nodes/<id>`), rather than being handled as MIA/unreachable. The
host looks perfectly healthy in `host ls` (`running`/`ready`) right up
until an operation needs the node.

### Observed

Running `cspawnctl host push --all` against `local-prod`
(2026-07-02), 22/23 running hosts pushed successfully. One host,
`gavin-morris`, failed on every attempt (including retries) with:

```
docker.errors.NotFound: 404 Client Error for http+docker://ssh/v1.55/nodes/qz2s99p6zza5t7nc40wz6amdr: Not Found ("node qz2s99p6zza5t7nc40wz6amdr not found")
```

`cspawnctl node info` confirms the current cluster only has
`swarm1`, `swarm2`, `swarm4`, `swarm5` — `qz2s99p6zza5t7nc40wz6amdr`
isn't any of them. The stakeholder reports this class of failure has
come up repeatedly (a "persistent problem"), not a one-off.

### Where it breaks

- [repo.py:114](cspawn/cs_github/repo.py#L114) — `CodeHostRepo.push()`
  does `container.node.attrs["Description"]["Hostname"]` to build the
  `ssh://` exec target. No try/except around the node lookup.
- [csmanager.py:225-226](cspawn/cs_docker/csmanager.py#L225-L226) —
  `to_model()` does the same (`c.node.id`, `c.node.attrs[...]`) when
  building/refreshing a `CodeHost` row from a live container. The
  surrounding `try/except` only catches `KeyError`, `StopIteration`,
  `ConnectionError`, `OSError` — not `docker.errors.NotFound` — so a
  stale node reference can blow up sync too, not just push.

### Likely root cause

This project's autoscaler dynamically adds/destroys Swarm nodes
(DigitalOcean droplets). When a node is destroyed and the cluster
re-provisions, a task's historical `NodeID` can point at a node that
Swarm has since removed, even though the task/container is still
reported as running (possibly on a node that was replaced without the
task being properly rescheduled). Any code that assumes `container.node`
always resolves will 404 for these hosts.

### Suggested direction (for sprint planning)

- Treat `docker.errors.NotFound` on node lookup the same as
  MIA/unreachable in both `to_model()` and `CodeHostRepo.push()` —
  log loudly, skip cleanly, don't crash the caller or the batch.
- Investigate whether `node rebalance` / `node contract` / autoscale
  reaping can leave tasks pinned to a `NodeID` that no longer exists,
  and whether Swarm should be nudged to reschedule such tasks rather
  than leaving them orphaned.
- Confirm whether affected hosts (like `gavin-morris`) still have a
  reachable container at all, or whether they need to be force-rebuilt.
