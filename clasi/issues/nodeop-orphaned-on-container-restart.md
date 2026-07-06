---
status: pending
---

# NodeOps orphaned as "running" forever when the spawner container restarts

## Summary

Admin-UI node operations (`expand`, `remove`, `rebalance`) execute as a
**detached subprocess inside the spawner container**
(`op-run`, [node.py:2842-2990](cspawn/cli/node.py#L2842-L2990)). The worker
sets `status='running'` in the `node_ops` table, does the work, and writes the
final status (`done`/`failed`) in a `finally` block. If the container dies
mid-run — deploy restart, OOM, node reboot — the subprocess is killed, the
`finally` never executes, and the op row stays **`running` forever**. There is
no PID tracking, heartbeat, or liveness check, so nothing ever corrects it and
the admin UI shows a phantom in-flight operation indefinitely.

Observed live 2026-07-06: op `633ad23d-f1ca-404c-9a21-51d9e5ea52c7`
(`expand`, tier=large) started 01:39:29 UTC; the sprint-009 deploy
force-restarted `codeserver_codeserver` at ~01:40; the worker died after
creating droplet `swarm4.dojtl.net` but before configure/join. Result: op
stuck "running" for 40+ minutes (would have been forever), plus an **orphaned
bare droplet** that never joined the swarm (costing money, invisible to the
cluster).

## Suggested fix

1. **Startup sweep**: at app boot (or container entrypoint), mark every
   `node_ops` row with `status='running'` as
   `status='interrupted'` (`exit_code=1`,
   `message='spawner restarted while op was in flight'`,
   `finished_at=now()`). Any op actually running cannot survive a container
   restart, so this is always correct.
2. **Orphan droplet awareness**: an interrupted `expand` may have left a
   droplet that exists in DO but never joined the swarm. At minimum, include
   the created droplet id/fqdn in the op record as soon as creation returns,
   so the interrupted-op message can name what to clean up. Optionally, the
   Nodes tab could list DO droplets matching the swarm name pattern that are
   not swarm members, with a "destroy orphan" action (the same
   list-droplets-vs-nodes diff `node info --all` already computes).
3. Consider recording the worker PID and started_at so a future
   watchdog/cron can detect stale "running" ops even without a restart
   (e.g. op running > N hours).

## Acceptance criteria (draft)

- [ ] Container/app start marks all `running` node_ops as `interrupted`.
- [ ] Interrupted expand ops surface the orphaned droplet (id/fqdn) in their
  message/log when droplet creation had already happened.
- [ ] Admin UI renders `interrupted` distinctly (not spinning).
- [ ] Tests: op stuck in `running` + app boot → row transitions to
  `interrupted`; ops in terminal states are untouched.

## Notes

- Related hardening shipped in sprint 009 (post-join verification, fail-loud
  cloud-init) does not cover this: the kill happened between droplet creation
  and join, which no in-process check can catch — only an out-of-process
  sweep can.
