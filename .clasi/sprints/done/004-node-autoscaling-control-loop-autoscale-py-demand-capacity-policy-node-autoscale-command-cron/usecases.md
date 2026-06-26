---
status: draft
sprint: '004'
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 004 Use Cases

## SUC-001: Operator runs a dry-run autoscale cycle

**Actor:** Operator (via CLI or cron)
**Trigger:** `cspawnctl node autoscale --dry-run` is executed.
**Preconditions:** The spawner container is running with valid config. `AUTOSCALE_ENABLED`
may be `false`; `--dry-run` is an additional read-only override that does not bypass the
kill-switch — if the kill-switch is off, the command logs "disabled" and exits.

**Main Flow:**
1. `run_autoscale` checks `AUTOSCALE_ENABLED`; if `false`, logs "autoscale disabled" and exits.
2. `run_autoscale` acquires the `flock` lock; if already held, logs "previous cycle running" and exits.
3. `gather_cluster_state` reads the swarm node list, per-node host counts (via `count_hosts_per_node`),
   pending `CodeHost` rows, and running `Class` roster sizes.
4. `build_plan` is called with the resulting `ClusterState` and demand; returns a `ScalePlan`.
5. Because `dry_run=True`, `apply_plan` logs the plan but performs no Docker or DO operations.
6. A single structured log line is emitted: demand, total_capacity, total_load, deficit, excess,
   `ScalePlan` fields, reason, per-node state.
7. Lock is released; command exits 0.

**Postconditions:** No cluster state has changed. Operator can read the log to validate the
demand/capacity math before enabling live scaling.

**Acceptance Criteria:**
- [ ] Command runs to completion with no errors when `AUTOSCALE_ENABLED=true` and `--dry-run` is set.
- [ ] Structured log line contains all required fields (demand, capacity, deficit, excess, plan, reason).
- [ ] No Docker API calls are made beyond `gather_cluster_state` reads.
- [ ] No DO API calls are made at all (apply is suppressed).
- [ ] When `AUTOSCALE_ENABLED=false`, command logs "disabled" and exits cleanly without gathering state.

---

## SUC-002: Cron triggers a scale-up at class start

**Actor:** Cron daemon (every 2 minutes)
**Trigger:** The `*/2 * * * * cspawnctl -d prod node autoscale` cron line fires while
`AUTOSCALE_ENABLED=true` and one or more classes have `running=True` with upcoming stop times.

**Preconditions:** `AUTOSCALE_ENABLED=true`, `AUTOSCALE_DRY_RUN=false`. An instructor has
started a class. Total worker capacity is less than the roster pre-scale estimate plus headroom.
DO_TOKEN is valid.

**Main Flow:**
1. `run_autoscale` checks kill-switch (enabled), acquires lock, calls `gather_cluster_state`.
2. `gather_cluster_state` queries `Class.running == True` with `stops_at > now`; computes
   `prescale_roster_estimate = sum(ceil(len(c.students) * AUTOSCALE_ROSTER_FRACTION) for c in classes)`.
3. `estimate_demand` returns `max(live_load + pending, prescale_roster_estimate) + HEADROOM`.
4. `compute_deficit` finds `D = max(0, demand - total_capacity) > 0`.
5. `plan_scale_up` returns `(add_large, add_small)` clamped to `AUTOSCALE_MAX_ADD_PER_CYCLE`.
6. `build_plan` returns a `ScalePlan` with add counts set, `remove_nodes=[]`.
7. `apply_plan` calls `_get_next_serial`, `_create_droplet(tier=...)`, `_configure_node`,
   `_join_swarm` for each node to add (up to the per-cycle cap). Logs each step.
8. Structured log line emitted. Lock released.

**Postconditions:** Cluster has gained capacity. Next cron cycle sees reduced deficit. Once
students are running, live load dominates the pre-scale estimate.

**Acceptance Criteria:**
- [ ] When deficit > 0, `plan_scale_up` returns nonzero add counts.
- [ ] Add counts are clamped to `AUTOSCALE_MAX_ADD_PER_CYCLE`.
- [ ] `build_plan` never returns a plan with both add and remove in the same cycle.
- [ ] `apply_plan` calls `_create_droplet` / `_configure_node` / `_join_swarm` for each addition.
- [ ] `apply_plan` is suppressed entirely when `dry_run=True`.

---

## SUC-003: Scale-down reclaims an empty node after class ends

**Actor:** Cron daemon (every 2 minutes)
**Trigger:** Cron fires while `AUTOSCALE_ENABLED=true`. The last class has ended, students
have purged their hosts, and a worker node has had `running_hosts == 0` for longer than
`AUTOSCALE_SCALEDOWN_COOLDOWN_MIN` minutes. Excess capacity exceeds that node's capacity
plus `AUTOSCALE_HEADROOM`.

**Preconditions:** `AUTOSCALE_ENABLED=true`, `AUTOSCALE_DRY_RUN=false`. At least
`AUTOSCALE_MIN_WORKER_NODES + 1` workers remain so one can be removed without going below
the floor. No running classes.

**Main Flow:**
1. `run_autoscale` acquires lock, calls `gather_cluster_state`.
2. Demand is low (no running classes, few or zero live hosts); `estimate_demand` returns a
   value near `AUTOSCALE_HEADROOM`.
3. `plan_scale_down` finds worker nodes with `running_hosts == 0` and empty for >= cooldown
   minutes. Checks `excess_capacity > candidate_capacity + HEADROOM` and
   `remaining_workers > AUTOSCALE_MIN_WORKER_NODES`. Selects highest-serial eligible node.
4. `build_plan` returns a `ScalePlan` with `purge_first=True` and one node in `remove_nodes`
   (`add_large=0, add_small=0`). Never both add and remove in the same cycle.
5. `apply_plan` (scale-down path):
   a. Calls `host purge` logic + `app.csm.sync` (idempotent cleanup of MIA/quiescent hosts).
   b. Re-checks `running_hosts == 0` live from Swarm immediately before draining (idempotency guard).
   c. Calls `graceful_remove_node(ctx, manager_client, mgr, fqdn, dry_run=False, log=log)`:
      drain → wait tasks drained → remove swarm node → destroy droplet.
6. Structured log line emitted. Lock released.

**Postconditions:** One empty node is removed. DO billing stops for that droplet.
The cooldown + per-cycle cap + headroom dead-band prevents the node from being removed
and immediately re-added in the next cycle.

**Acceptance Criteria:**
- [ ] `plan_scale_down` skips nodes with `running_hosts > 0`.
- [ ] `plan_scale_down` skips nodes empty for fewer than `cooldown` minutes.
- [ ] `plan_scale_down` skips the manager/leader node.
- [ ] `plan_scale_down` respects `AUTOSCALE_MIN_WORKER_NODES` floor.
- [ ] `graceful_remove_node` re-checks `running_hosts == 0` before draining; aborts if hosts appeared.
- [ ] `apply_plan` calls `purge_first` logic before node removal.
- [ ] When `dry_run=True`, no drain/remove/destroy actions are executed.
