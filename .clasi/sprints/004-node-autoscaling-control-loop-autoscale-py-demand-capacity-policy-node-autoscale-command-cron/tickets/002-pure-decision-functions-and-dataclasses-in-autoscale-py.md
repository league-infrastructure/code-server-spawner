---
id: '002'
title: Pure decision functions and dataclasses in autoscale.py
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Pure decision functions and dataclasses in autoscale.py

## Description

Create `cspawn/cs_docker/autoscale.py` with the three dataclasses and all seven pure
decision functions. These functions contain all the scaling math. They take plain Python
data (dicts, lists, dataclasses, config) and return plain data; they have no side effects
and require no Docker, DO, or DB infrastructure. This makes them fully unit-testable in
ticket 003.

`estimate_demand` is designed as a clean seam: all I/O happens in the caller
(`gather_cluster_state`, ticket 004); this function only does math. The next sprint
(instructor-cluster-presize) will swap the demand source (from `Class.running` to
purge-window timestamps) by changing only what the caller fetches and how this function
interprets it — the function signature and call site do not change.

## Acceptance Criteria

- [x] `cspawn/cs_docker/autoscale.py` is created with `__all__` listing all public names.
- [x] `NodeView` dataclass has fields: `short: str`, `fqdn: str`, `size_slug: str | None`,
      `capacity: int`, `running_hosts: int`, `is_manager: bool`, `is_leader: bool`,
      `serial: int | None`.
- [x] `ClusterState` dataclass has fields `nodes: list[NodeView]`, `pending_hosts: int`,
      and computed properties `total_capacity` (sum of non-manager node capacities),
      `total_load` (sum of `running_hosts` across all nodes), `excess_capacity`
      (`total_capacity - total_load`). Managers are excluded from `total_capacity`.
- [x] `ScalePlan` dataclass has fields: `add_large: int`, `add_small: int`,
      `remove_nodes: list[str]` (fqdns), `purge_first: bool`, `reason: str`.
      Also has a `summary() -> str` method returning a one-line structured log string.
- [x] `capacity_for_node(node_attrs: dict, cfg) -> int` reads `Spec.Labels["cs.capacity"]`;
      falls back to `node_capacity(node_attrs, cfg)` from `cspawn.cs_docker.tiers`.
- [x] `assess_cluster(node_dicts: list[dict], host_counts: dict[str, int], pending: int, cfg) -> ClusterState`
      builds a `ClusterState` from raw swarm node attrs and a pre-fetched host-count map.
      Both workers and managers are included in `nodes`; `is_manager`/`is_leader` flags
      are set correctly from `Spec.Role` and `ManagerStatus.Leader`. Managers are excluded
      from `total_capacity`.
- [x] `estimate_demand(classes: list[dict], hosts: list[dict], cfg) -> int` implements:
      - `live_load = count of host dicts where not is_mia and not is_purgeable`
      - `pending = count of host dicts where app_state not in ('ready',) and not is_mia`
      - `prescale = sum(ceil(len(c['students']) * ROSTER_FRACTION) for c in classes
          if c['running'] and c['stops_at'] > now)` where `now = datetime.now(timezone.utc)`
      - Returns `max(live_load + pending, prescale) + HEADROOM`
      - Reads `AUTOSCALE_HEADROOM` (int, default 2), `AUTOSCALE_ROSTER_FRACTION` (float, default 0.8).
- [x] `compute_deficit(state: ClusterState, demand: int, cfg) -> int` returns
      `max(0, demand - state.total_capacity)`.
- [x] `plan_scale_up(deficit: int, cfg) -> tuple[int, int]` implements greedy bin-pack:
      - Resolve capacities from `load_tiers(cfg)`: large = tier with max capacity; small = min.
      - `add_large = deficit // cap_large`; `rem = deficit % cap_large`.
      - If `rem == 0`: `add_small = 0`. Elif `rem <= cap_small`: `add_small = 1`.
        Else (`cap_small < rem < cap_large`): `add_large += 1` (cheaper per slot than two nodes).
      - Clamp total adds to `AUTOSCALE_MAX_ADD_PER_CYCLE` (reduce `add_small` first).
      - Returns `(add_large, add_small)`.
- [x] `plan_scale_down(state, demand, cfg, now, empty_since: dict[str, datetime]) -> list[NodeView]`
      implements safe removal:
      - Considers only non-manager, non-leader `NodeView`s with `running_hosts == 0`.
      - Requires `(now - empty_since[fqdn]).total_seconds() / 60 >= AUTOSCALE_SCALEDOWN_COOLDOWN_MIN`.
      - Removes only while `state.excess_capacity > candidate.capacity + AUTOSCALE_HEADROOM`
        and `(total_workers - removed_so_far) > AUTOSCALE_MIN_WORKER_NODES`.
      - Selects highest-serial eligible node first.
      - Returns at most `AUTOSCALE_MAX_REMOVE_PER_CYCLE` nodes.
- [x] `build_plan(state, demand, cfg, now, empty_since) -> ScalePlan` never returns both
      add and remove in the same plan:
      - If `compute_deficit > 0`: return scale-up plan (no removes).
      - Elif `plan_scale_down` returns candidates: return scale-down plan (no adds).
      - Else: return hold plan `ScalePlan(0, 0, [], False, "hold: within dead-band")`.
- [x] No imports of `docker`, `paramiko`, `digitalocean`, or Flask at module top level.
- [x] `uv run pytest` passes (existing tests must not regress).

## Implementation Plan

### Approach

Create `cspawn/cs_docker/autoscale.py` as a new file. Import `load_tiers`, `node_capacity`
from `cspawn.cs_docker.tiers`. Define a small `_cfg_int(cfg, key, default)` and
`_cfg_float(cfg, key, default)` helper at module top to avoid scattered parsing patterns.

Tier resolution for `plan_scale_up`: `load_tiers(cfg)` returns `list[Tier]`. Sort by
`capacity` to identify large (max) and small (min). If only one tier exists, both use
the same tier.

### Files to Create

- `cspawn/cs_docker/autoscale.py`

### Testing Plan

Unit tests are in ticket 003. This ticket only needs the module to import cleanly and
`uv run pytest` to pass with no regressions. The programmer should verify a few cases
manually at the REPL before marking done.

### Documentation Updates

Module docstring in `autoscale.py` describing the pure-function/orchestrator split, the
demand signal formula, and the `estimate_demand` seam note for the next sprint.
