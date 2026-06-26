---
id: '005'
title: "Reaper three-zone retune — protected, active-purge, dormant force-remove"
status: open
use-cases:
  - SUC-003
  - SUC-004
depends-on:
  - '001'
  - '004'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Reaper three-zone retune — protected, active-purge, dormant force-remove

## Description

Add time-windowed reaper logic to the autoscaler orchestrator section in
`cspawn/cs_docker/autoscale.py`. The reaper classifies each class (and its
resources) into one of three zones based on the `purge_after` / `purge_by`
timestamps stamped by the `/cluster` route (ticket 002):

```
[ protected ]          [ active-purge ]         [ dormant ]
─────────────────────┬──────────────────────┬─────────────────►
                 purge_after             purge_by
  nothing reaped      15-min idle rules      force-remove all
```

**Scope (decision baked in):** Zone-awareness is added to the autoscaler
orchestrator (`run_autoscale`) ONLY. The `host purge` CLI command
(`node.py:1603`) remains a manual override and is NOT modified this sprint.

**Inert-by-default:** The reaper only executes when `AUTOSCALE_ENABLED=true`
(default: false). The kill-switch at `run_autoscale` line 917 gates everything.

### Three-zone logic (new function: `apply_reaper_zones`)

Add a new function in the orchestrator section of `autoscale.py`:

```python
def apply_reaper_zones(app, class_rows, host_rows, now):
    """
    Classify class resources by zone and apply appropriate reaping actions.

    Protected zone  (now < purge_after):
        - No action on any CodeHost or node for this class.
        - Students who step away keep their slot.

    Active-purge zone  (purge_after <= now < purge_by):
        - CodeHosts idle for >= 15 minutes are stopped and deleted.
        - Nodes that reach zero running CodeHosts are tracked in empty_since
          (existing Sprint 004 mechanism handles their removal via plan_scale_down).

    Dormant zone  (now >= purge_by):
        - ALL remaining CodeHosts for the class are force-removed immediately
          (no idle-timeout check).
        - Class.purge_after, Class.purge_by, Class.target_nodes are cleared
          (set to None) on the class row.
        - Node draining for emptied nodes is handled by plan_scale_down on the
          next cycle (existing mechanism).
    """
```

### CodeHost idle detection

The existing `CodeHost.is_quiescent` property or `modified_ago >= 15`
threshold (verify in `cspawn/models.py`) is the idle check for the active-purge
zone. If no such property exists, use:
```python
idle = (now - host.updated_at).total_seconds() / 60 >= 15
```

### Integration point in `run_autoscale`

Call `apply_reaper_zones` after `gather_cluster_state` and before `build_plan`,
passing the class_rows (which now have `purge_after`/`purge_by` from ticket 004)
and the Flask app for DB access.

### `plan_scale_down` zone gate

A node that carries running CodeHosts in the protected zone must NOT be selected
for removal even if it is technically "empty" of running tasks (edge case: host
starting up). Add a check: if any running class has `purge_after > now`, skip
nodes associated with that class from scale-down candidates. (If this is too
complex to wire, document the simplification as a known limitation.)

## Acceptance Criteria

- [ ] A class in the protected zone (`now < purge_after`): no CodeHosts are
      stopped, no nodes are selected for removal, regardless of idle state.
- [ ] A class in the active-purge zone (`purge_after <= now < purge_by`):
      CodeHosts idle for >= 15 minutes are stopped and their DB records deleted.
- [ ] A class in the dormant zone (`now >= purge_by`): ALL remaining CodeHosts
      for the class are force-removed (no idle check); `purge_after`, `purge_by`,
      and `target_nodes` are set to `None` on the `Class` row.
- [ ] After dormant force-remove, the class no longer appears in `gather_cluster_state`
      results (because `purge_after` and `purge_by` are now NULL).
- [ ] `plan_scale_down` does not remove nodes that belong to protected-zone classes.
- [ ] The `host purge` CLI command (`node.py:1603`) is unchanged.
- [ ] `AUTOSCALE_ENABLED` default remains `false`; the entire reaper path is
      gated by the kill-switch in `run_autoscale`.
- [ ] Unit tests cover all three zones for the `apply_reaper_zones` logic (inject
      `now` as a parameter; mock DB rows; no Docker/DO calls needed).

## Implementation Plan

**Files to modify:**
- `cspawn/cs_docker/autoscale.py` (orchestrator section):
  - Add `apply_reaper_zones(app, class_rows, host_rows, now)` function.
  - Call it from `run_autoscale` after `gather_cluster_state`, before `build_plan`.
  - Add zone guard to `plan_scale_down` or to the caller in `run_autoscale`.

**Files to check:**
- `cspawn/models.py` — verify `CodeHost` has an idle/quiescent field or
  `updated_at`/`modified_at` timestamp to compute idle duration.
- `cspawn/main/app.py` — verify `app.csm.sync(check_ready=True)` is the
  correct call for stopping idle hosts (used in `apply_plan` at line 812).

**Testing plan (pure/injected now — no Docker/DO needed):**
- Three unit tests for `apply_reaper_zones`:
  1. Protected zone: assert no DB mutations, no force-removes.
  2. Active-purge zone: assert idle hosts (>= 15 min) are stopped; non-idle
     hosts are untouched.
  3. Dormant zone: assert all hosts force-removed; `purge_after`/`purge_by`/
     `target_nodes` set to None on the Class row.

## Verification Command

```
uv run pytest tests/ -k "reaper or zone or autoscale" -v
```
