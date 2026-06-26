---
status: pending
---

# Node Autoscaling: control loop & policy

**Audience:** an engineer or agent working on the code-server-spawner's swarm
node autoscaling. This issue defines the **control loop** — how the cluster
decides to add or remove worker nodes each cycle — and its default policy.

> **Provenance:** distilled from the Plan-agent transcript
> `.clasi/log/018-Plan.md` (2026-06-22). That log is an ephemeral planning
> transcript; this issue is the durable artifact.
>
> **Depends on** [[multi-size-node-provisioning]] — the autoscaler needs the
> `NODE_TIERS` tiers, the `cs.capacity` node labels, and the tier-aware
> `_create_droplet`/`graceful_remove_node` primitives that issue provides.
> **Related:** [[instructor-cluster-presize]] revises the *demand signal* (the
> scale-up/down trigger) to use instructor-stamped purge windows instead of
> `Class.running`; this issue is the mechanism that consumes that signal.

---

## TL;DR

A new `cspawn/cs_docker/autoscale.py` controller, split into **pure decision
functions** (unit-testable, no I/O) and a **thin orchestrator** that calls the
existing `node.py` provisioning primitives. Triggered by a `cspawnctl node
autoscale` command run from cron (every ~2 min) — **not** inline in the Flask
`/cron/*` endpoints (those are curled with a 5s timeout and no auth; provisioning
takes minutes). Phased rollout behind a kill-switch, starting read-only/dry-run.

---

## Problem / current state

- All node scaling today is **manual**: `node expand`, `node contract`,
  `node drain`, `host purge`/`reap`. Nothing sizes the cluster automatically, so
  idle nodes linger (cost) and class-start can under-provision.
- The `/cron/*` HTTP endpoints (`cspawn/main/routes/cron.py`) are empty stubs
  hit by `curl -m 5` (5s) with no auth — unsuitable for minutes-long provisioning.
- The commented `*/5 * * * * cspawnctl -d prod host reap` line in `docker/crontab`
  shows cron-driven `cspawnctl` is the intended trigger mechanism.

---

## Design

### 1. New controller `cspawn/cs_docker/autoscale.py`

**Pure decision functions (unit-test, no side effects):**

- `estimate_demand(classes, hosts, cfg) -> int` — combine signals (Policy §1).
- `capacity_for_node(labels, size_slug, cfg) -> int` — read `cs.capacity` label,
  else size→capacity map, else `AUTOSCALE_DEFAULT_CAPACITY`.
- `assess_cluster(node_dicts, host_counts, pending) -> ClusterState`.
- `compute_deficit(state, demand, cfg) -> int`.
- `plan_scale_up(deficit, cfg) -> (add_large, add_small)` — greedy bin-pack.
- `plan_scale_down(state, demand, cfg, now) -> list[NodeView]` — empty,
  cooled-down, non-manager nodes only.
- `build_plan(state, demand, cfg, now) -> ScalePlan` — decides up vs. down vs.
  hold; **never both up and down in one cycle.**

Dataclasses: `NodeView` (short/fqdn/size_slug/capacity/running_hosts/is_manager/
is_leader/serial), `ClusterState` (nodes + pending_hosts + total_capacity/
total_load/excess_capacity), `ScalePlan` (add_large/add_small/remove_nodes/
purge_first/reason).

**Orchestrator (thin, side-effecting):**

- `gather_cluster_state(app, manager_client, cfg)` — live reads only
  (read-only): per-node host counts (reuse `count_hosts_per_node`), swarm node
  list, `CodeHost`/`Class` queries.
- `apply_plan(ctx, plan, cfg, *, dry_run)` — scale-down: `host purge` +
  `app.csm.sync` first, then `graceful_remove_node` per fqdn (re-check
  `running_hosts==0` immediately before draining); scale-up: `_get_next_serial`
  → `_create_droplet(tier slug)` → `_configure_node` → `_join_swarm`. Caps per
  cycle.
- `run_autoscale(ctx, *, dry_run, force)` — single entry point: kill-switch →
  lock → gather → build_plan → apply → structured log → release.

This split keeps all math infrastructure-free; only `gather_cluster_state` and
`apply_plan` touch Docker/DO.

### 2. Refactor in `cspawn/cli/node.py` (minimal)

- Extract `count_hosts_per_node(client) -> dict[str,int]` from the `hosts`
  command body (~48-96).
- Extract the graceful stop sequence from `stop_node` into
  `graceful_remove_node(ctx, manager_client, mgr, fqdn, *, dry_run, log)` so both
  `stop_node` and `apply_plan` share the drain→remove→destroy path.
- Use the tier-aware `_create_droplet`/`_join_swarm` from
  [[multi-size-node-provisioning]] so scale-up can request large vs. small.

### 3. New CLI command `node autoscale`

```python
@node.command(name="autoscale")
@click.option("-N", "--dry-run", is_flag=True)
@click.option("--force", is_flag=True)         # bypass cooldown
@click.option("--up-only/--down-only", ...)
def autoscale_cmd(ctx, dry_run, force, ...):
    from cspawn.cs_docker.autoscale import run_autoscale
    click.echo(run_autoscale(ctx, dry_run=dry_run, force=force).summary())
```

### 4. Trigger (recommended)

Cron-driven `cspawnctl node autoscale` every ~2 min, **not** the Flask `/cron/*`
endpoints. Add alongside the existing commented `host reap` line in
`docker/crontab`:

```
*/2 * * * * cspawnctl -d prod node autoscale >/proc/1/fd/1 2>/proc/1/fd/2
```

The continuous loop already sees demand within one cron interval, so explicit
class-start pre-scale is optional (the `class_run_state` route may later fire a
detached `subprocess.Popen([... "node","autoscale"])` kick — flag as a decision).
**Note:** [[instructor-cluster-presize]] proposes replacing the `Class.running`
demand source with instructor-stamped `purge_after`/`purge_by` windows; this
controller's `estimate_demand` should consume whichever signal that issue lands.

---

## Policy (defaults)

New config keys in `config/*/public.env`:

| Key | Default | Meaning |
|---|---|---|
| `AUTOSCALE_ENABLED` | `false` → `true` after rollout | Kill-switch |
| `AUTOSCALE_DRY_RUN` | `true` initially | Global dry-run override |
| `AUTOSCALE_HEADROOM` | `2` | Spare host slots kept above demand |
| `AUTOSCALE_ROSTER_FRACTION` | `0.8` | Fraction of roster expected to attend (pre-scale) |
| `AUTOSCALE_MAX_ADD_PER_CYCLE` | `2` | Cap nodes added per run |
| `AUTOSCALE_MAX_REMOVE_PER_CYCLE` | `1` | Cap nodes removed per run |
| `AUTOSCALE_SCALEDOWN_COOLDOWN_MIN` | `30` | Min minutes empty before removal (hysteresis) |
| `AUTOSCALE_MIN_WORKER_NODES` | `1` | Never shrink below this |
| `AUTOSCALE_DEFAULT_CAPACITY` | `6` | Fallback when a node has no `cs.capacity` label |

(Size slugs and per-tier capacities come from `NODE_TIERS` —
[[multi-size-node-provisioning]].)

- **§1 Demand:** `demand = max(continuous_live_load + pending,
  prescale_roster_estimate) + HEADROOM`. Continuous = non-MIA `CodeHost` rows
  (live Swarm per-node count is authoritative for placed load; DB for
  pending/starting). Pre-scale = `Σ ceil(len(students)*ROSTER_FRACTION)` over
  running classes still before their stop time. Max-of-the-two pre-provisions at
  class start, then live count dominates.
- **§2 Capacity:** per-node from `cs.capacity` label → size map → default.
  Exclude managers. Deficit `D = max(0, demand - total_capacity)`; excess
  `E = total_capacity - total_load`.
- **§3 Scale-up:** greedy bin-pack — `add_large = D // CAP_LARGE`, remainder to a
  small node (or one more large if remainder > CAP_SMALL); clamp to
  `MAX_ADD_PER_CYCLE`, carry the rest to next cycle.
- **§4 Scale-down:** **purge idle hosts first** (`host purge` + sync), then
  remove only nodes with `running_hosts==0` that have been empty ≥ cooldown,
  while `E > capacity(node)+HEADROOM` and workers ≥ `MIN_WORKER_NODES`, highest
  serial first, never manager/leader. **Never drain a node with live hosts** to
  consolidate. The headroom gap between up-trigger (`D>0`) and down-trigger
  (`E > cap+HEADROOM`) plus the cooldown forms an anti-flap dead-band.
- **§5 Safety:** a `flock` file lock in `run_autoscale` (cron fires every 2 min,
  a run can exceed that) — abort if not acquired. Every cycle recomputes from
  live state; create/join are idempotent.
- **§6 Observability:** one structured log line per cycle (demand,
  total_capacity, total_load, deficit, excess, chosen plan, reason, per-node
  state) via `get_logger(ctx)` so cron output lands in the docker log.

---

## Phased rollout

- **Phase 0** — config keys + extract `count_hosts_per_node` /
  `graceful_remove_node`. No behavior change.
- **Phase 1** — pure functions + `gather_cluster_state` + `node autoscale
  --dry-run`; cron runs dry, watch logs ~1 week to validate the math.
- **Phase 2** — scale-up only (`--up-only`, `MAX_ADD=1`). Requires the
  cloud-init docker pin ([[multi-size-node-provisioning]] §7) so joins don't
  abort on version mismatch.
- **Phase 3** — scale-down: purge-first + empty-node removal, conservative
  cooldown (start 60 / `MIN_WORKER_NODES=2`), then tighten.
- **Phase 4** — optional class-start pre-scale kick if cron latency is too slow.

---

## Unit-test seams (`test/test_autoscale.py`, no Docker/DO/DB)

`capacity_for_node` (label / size-map / default); `estimate_demand` (pre-scale vs
continuous, headroom, MIA/quiescent excluded); `compute_deficit` boundaries;
`plan_scale_up` (D=1→(0,1); D=7→(1,0); D=20→(1,1); cap clamps); `plan_scale_down`
(skips non-empty, respects cooldown/`MIN_WORKER_NODES`, never manager, highest
serial); `build_plan` (never both add+remove; dead-band prevents flapping).
`gather_cluster_state`/`apply_plan` get thin coverage with Docker/DO mocked.

---

## Implementation touch points / critical files

- `cspawn/cs_docker/autoscale.py` — **new** controller.
- `cspawn/cli/node.py` — extract `count_hosts_per_node` + `graceful_remove_node`;
  add `node autoscale`; reuse tier-aware create/join.
- `cspawn/cli/host.py` — reuse `purge`/`reap` + `app.csm.sync` for purge-first.
- `cspawn/models.py` — demand queries (`Class.running`/`students`/`stops_at` or
  the [[instructor-cluster-presize]] purge-window fields; `CodeHost.is_mia`/
  `is_quiescent`/`class_id`/`node_name`).
- `docker/crontab` — add the `node autoscale` cron line.
- `config/*/public.env` — `AUTOSCALE_*` keys.

## Open / to confirm

- Manager (`swarm1`) reserved from hosting students? (Recommend: yes.)
- Scale-up remainder rule (7-13 hosts → one extra large vs. one small).
- Pre-scale tightness: accept ≤2-min cron latency vs. a `subprocess` kick from
  `class_run_state`.
- Lock mechanism (`flock` vs. DB advisory vs. swarm-native) if multi-manager HA
  is anticipated.
- `ROSTER_FRACTION` (0.8) and `HEADROOM` (2) tuning against observed attendance.
