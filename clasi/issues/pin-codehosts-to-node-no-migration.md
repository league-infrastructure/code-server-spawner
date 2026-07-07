---
status: pending
sprint: '014'
---

# Pin codehosts to their node so Swarm never migrates them

## Summary

**Stakeholder priority: VERY IMPORTANT.** Codehost services are created with
`constraints=["node.role==worker"]` ([csmanager.py:444-453](cspawn/cs_docker/csmanager.py#L444)) —
i.e. Swarm may place *and reschedule* them on **any** worker. So when a node
has trouble (overload, heartbeat timeout, reboot), Swarm migrates its tasks to
other workers. Every migration drops that student's VNC/code-server session,
and a mass migration **cascades** (one overloaded node stampedes its hosts onto
the next, overloading it too). This was the 2026-07-06 class-time disconnect
storm (swarm2 overloaded → its ~14 hosts stampeded onto swarm4).

Goal: **pin each codehost to a single node** via a `node.hostname==<node>`
placement constraint, so Swarm cannot migrate it. A pinned host stays put; if
its node has trouble it waits (rather than migrating and disconnecting), and an
overloaded node can no longer cascade onto its neighbours.

## Accepted trade-off (stakeholder already weighed this)
A hard-pinned host does **NOT** fail over: if its node genuinely dies, the host
stays `Pending`/down until that node returns, instead of rescheduling onto a
healthy node. This is the deliberate trade — no migration churn / no cascade, in
exchange for "stuck if your node dies." (Mitigating context: `/workspace` is on
a shared mount that travels with a move, so migration today is a *disconnect*,
not necessarily work-loss — but it's still the disconnect + cascade we're
eliminating.)

## Existing mechanism to reuse
`_pin_service_to_node(svc, node_fqdn)` and `_unpin_services_from_node(client, node_fqdn)`
in [node.py:145-207](cspawn/cli/node.py#L145) already add/remove a
`node.hostname==` constraint (used by `node rebalance` and cleared by node
remove/drain). Reuse these — do NOT reinvent. Note `_pin_service_to_node`'s
`svc.update(constraints=...)` **triggers a task reschedule** (recreates the
container), which matters for the approach choice below.

## Design — two approaches (planner: evaluate + recommend; team-lead will confirm with stakeholder)

**A. Pick the node up-front, create already-pinned (no post-create restart).**
At host start, choose the target worker (reuse the capacity-aware selection the
rebalance path already has — `count_hosts_per_node`, least-loaded eligible
worker respecting `cs.capacity`) and create the service with
`node.hostname==<node>` in its constraints from the start. Pro: no extra
restart; host is pinned from birth. Con: the spawner must replicate Swarm's
placement (capacity accounting, "all nodes full" handling, eligible-worker
filtering) instead of letting Swarm schedule.

**B. Let Swarm place it, then pin where it landed (one restart per new host).**
Create with `node.role==worker` (Swarm schedules), read the placed node from the
task, then `_pin_service_to_node(svc, <that-node>)`. Pro: Swarm handles
placement/capacity/full correctly; minimal new logic. Con: the pin update
reschedules the just-created task once (~a brief restart at startup; container
recreates on the same node).

Recommendation to evaluate: **A** if the capacity-aware selection is cleanly
reusable (best UX, no restart); otherwise **B** (simpler, robust, one restart).

## Constraints / must-handle
- **Config toggle:** gate the behavior behind a config flag (e.g.
  `PIN_HOSTS_TO_NODE`, default on) so it can be disabled without a code change
  if it ever misbehaves.
- **Node removal/drain must clear the pin** — already handled by
  `_unpin_services_from_node` in the remove/drain path; confirm the pinned hosts
  don't become permanently-orphaned when their node is removed (the unpin runs
  before removal). If a pinned host's node is drained for maintenance, decide
  whether to unpin+let-it-move or leave-it-pending (document the choice).
- **Rebalance still works** — `node rebalance` unpins/repins to move hosts; a
  default-pinned fleet must still be rebalanceable (rebalance replaces the pin).
- **Idempotent / restart-safe** — re-running host start or the pin must not
  accumulate conflicting `node.hostname==` constraints (the helper already
  normalizes this).
- Apply only to codehost services (`jtl.codeserver=true`), not infra services.

## Acceptance criteria (draft)
- [ ] A newly started codehost carries a `node.hostname==<node>` constraint and
  Swarm does not migrate it off that node when other nodes change state (test
  with a mocked/faked swarm: simulate a node going unavailable → the pinned
  service is NOT rescheduled elsewhere).
- [ ] The behavior is gated behind a config flag (default on); disabling it
  restores today's `node.role==worker`-only placement.
- [ ] Chosen approach (A or B) implemented per the team-lead's confirmed
  decision; if B, the single post-create reschedule is confirmed to land the
  host on the same node it was pinned to.
- [ ] Node remove/drain clears the pin (`_unpin_services_from_node`) so a pinned
  host is never permanently orphaned; test.
- [ ] `node rebalance` still relocates a pinned host (unpin→move→repin); test.
- [ ] Suite green (excluding the known pre-existing `test_admin_coverage.py`
  PRODUCTION-env failures).

## Notes
- Pinning stops migration churn/cascades but NOT a single overloaded node from
  thrashing its own pinned hosts — that's the capacity story (tier caps, enough
  nodes), which is separate and already improved. See
  [[new-node-cold-image-pull-503-herd]] for the capacity-shortfall incident.
