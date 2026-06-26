---
id: '003'
title: Unit tests for pure decision functions
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
depends-on:
  - '002'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Unit tests for pure decision functions

## Description

Create `test/test_autoscale.py` with unit tests for all pure functions in
`cspawn/cs_docker/autoscale.py`. No Docker, DO, or DB infrastructure required — all tests
use plain dicts and dataclasses. This provides confidence in the scaling math before any
live infrastructure is involved, and guards against regressions when `estimate_demand` is
re-pointed in the next sprint.

## Acceptance Criteria

- [ ] `test/test_autoscale.py` exists and is discovered by `uv run pytest`.
- [ ] `capacity_for_node` tests:
      - Label present: returns label value as int.
      - Label absent, slug matches tier: returns tier capacity.
      - Label absent, slug unknown: returns `AUTOSCALE_DEFAULT_CAPACITY` from config.
- [ ] `assess_cluster` tests:
      - Managers excluded from `total_capacity`.
      - `total_load` sums across all nodes.
      - `excess_capacity` equals `total_capacity - total_load`.
- [ ] `estimate_demand` tests:
      - Pre-scale > continuous: returns pre-scale + headroom.
      - Continuous > pre-scale: returns continuous + headroom.
      - MIA hosts excluded from `live_load`.
      - Purgeable hosts excluded from `live_load`.
      - Classes past `stops_at` excluded from pre-scale.
      - Non-running classes excluded from pre-scale.
      - `ROSTER_FRACTION` is applied (e.g., 10 students * 0.8 = ceil(8) = 8).
      - Headroom is always added.
- [ ] `compute_deficit` tests:
      - Demand <= capacity: returns 0.
      - Demand > capacity: returns correct deficit.
- [ ] `plan_scale_up` tests (use `NODE_TIERS` with small=6, large=14 throughout):
      - `D=1` → `(0, 1)` (one small).
      - `D=6` → `(0, 1)` (exactly one small).
      - `D=7` → `(1, 0)` (remainder 7 > cap_small=6, so one more large instead).
      - `D=14` → `(1, 0)` (exactly one large).
      - `D=20` → `(1, 1)` (one large + one small for remainder 6).
      - `D=21` → `(2, 0)` (one large + remainder 7, round up to second large).
      - Cap clamp: `D=50, MAX_ADD_PER_CYCLE=2` → total adds clamped to 2.
- [ ] `plan_scale_down` tests:
      - Node with `running_hosts > 0`: skipped.
      - Node empty but cooldown not met: skipped.
      - Manager node: skipped.
      - Leader node: skipped.
      - Respects `MIN_WORKER_NODES` floor (stops removing when floor would be breached).
      - Respects excess dead-band (skips when `excess <= capacity + headroom`).
      - Selects highest-serial node first.
      - Returns at most `MAX_REMOVE_PER_CYCLE` nodes.
- [ ] `build_plan` tests:
      - Deficit > 0: returns plan with adds only, no removes.
      - Deficit == 0 and eligible scale-down candidates: returns plan with removes only, no adds.
      - Deficit == 0 and no eligible candidates: returns hold plan.
      - Never returns a plan with both `add_large + add_small > 0` AND `remove_nodes` non-empty.
- [ ] All tests pass: `uv run pytest test/test_autoscale.py -v`.
- [ ] Full suite still passes: `uv run pytest`.

## Implementation Plan

### Approach

Use `pytest` fixtures to build minimal `ClusterState` and `ScalePlan` objects and mock
config dicts. No `unittest.mock` needed for pure function tests — just call functions
directly with constructed inputs.

### Files to Create

- `test/test_autoscale.py`

### Config fixture

```python
@pytest.fixture
def cfg():
    return {
        "NODE_TIERS": '[{"name":"small","slug":"s-4vcpu-8gb-amd","capacity":6},'
                      '{"name":"large","slug":"s-8vcpu-16gb-amd","capacity":14}]',
        "DEFAULT_TIER": "small",
        "DEFAULT_CAPACITY": "6",
        "AUTOSCALE_HEADROOM": "2",
        "AUTOSCALE_ROSTER_FRACTION": "0.8",
        "AUTOSCALE_MAX_ADD_PER_CYCLE": "2",
        "AUTOSCALE_MAX_REMOVE_PER_CYCLE": "1",
        "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN": "30",
        "AUTOSCALE_MIN_WORKER_NODES": "1",
        "AUTOSCALE_DEFAULT_CAPACITY": "6",
    }
```

### Testing Plan

All tests are in `test/test_autoscale.py`. Run with `uv run pytest test/test_autoscale.py -v`
during development, then `uv run pytest` for the full suite before marking done.

### Documentation Updates

None — tests are self-documenting.
