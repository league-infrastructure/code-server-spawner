---
id: '004'
title: "Re-point estimate_demand to purge-window classes in autoscale.py"
status: open
use-cases:
  - SUC-002
  - SUC-004
depends-on:
  - '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Re-point estimate_demand to purge-window classes in autoscale.py

## Description

Change two functions in `cspawn/cs_docker/autoscale.py` to read
`purge_after`/`purge_by` instead of `Class.running`/`stops_at` as the demand
prescale signal. This is the "swappable seam" the Sprint 004 architecture
explicitly designed for (`autoscale.py:18-25`).

**Two touch points:**

### 1. `gather_cluster_state` (autoscale.py:638-645)

Change the DB query from:
```python
class_rows = [
    {
        "running": bool(getattr(c, "running", False)),
        "stops_at": getattr(c, "stops_at", None),
        "students": list(getattr(c, "students", []) or []),
    }
    for c in Class.query.filter_by(running=True).all()
]
```

To:
```python
class_rows = [
    {
        "purge_after": getattr(c, "purge_after", None),
        "purge_by": getattr(c, "purge_by", None),
        "students": list(getattr(c, "students", []) or []),
    }
    for c in Class.query.filter(
        Class.purge_after.isnot(None),
        Class.purge_by.isnot(None),
    ).all()
]
```

(Fetch all classes with a purge window set, regardless of zone; let
`estimate_demand` determine which are active.)

### 2. `estimate_demand` (autoscale.py:270-326)

Change the `prescale` loop from reading `c['running']`/`c['stops_at']` to
reading `c['purge_after']`/`c['purge_by']`:

```python
prescale = 0
for c in classes:
    purge_after = c.get("purge_after")
    purge_by = c.get("purge_by")
    if purge_after is None or purge_by is None:
        continue
    # Normalize to timezone-aware datetime
    if isinstance(purge_after, str):
        purge_after = datetime.fromisoformat(purge_after)
    if isinstance(purge_by, str):
        purge_by = datetime.fromisoformat(purge_by)
    if purge_after.tzinfo is None:
        purge_after = purge_after.replace(tzinfo=timezone.utc)
    if purge_by.tzinfo is None:
        purge_by = purge_by.replace(tzinfo=timezone.utc)
    # Only count classes in the active-purge window (purge_after <= now < purge_by)
    if purge_after <= now < purge_by:
        student_count = len(c.get("students") or [])
        prescale += ceil(student_count * roster_fraction)
```

**Update the docstring** of `estimate_demand` to reflect the new signal.
Remove references to `Class.running` / `stops_at` from the docstring.

**Inert-by-default:** `estimate_demand` is a pure function; it makes no
infrastructure calls. `run_autoscale` still exits early if
`AUTOSCALE_ENABLED=false` (the default). This change only affects what the
autoscaler *would* do when enabled — it does not enable it.

## Acceptance Criteria

- [ ] `gather_cluster_state` queries `Class` by `purge_after IS NOT NULL AND
      purge_by IS NOT NULL`; the resulting dicts have `purge_after` and
      `purge_by` keys, not `running` / `stops_at`.
- [ ] `estimate_demand` counts only classes where `purge_after <= now < purge_by`
      toward `prescale`; classes in the protected zone (`now < purge_after`) or
      dormant zone (`now >= purge_by`) contribute 0 to `prescale`.
- [ ] `estimate_demand` still adds `AUTOSCALE_HEADROOM` and returns
      `max(live_load + pending, prescale) + headroom` (formula unchanged).
- [ ] The function signature of `estimate_demand` is unchanged: `(classes, hosts, cfg)`.
- [ ] Unit test: inject class rows with `purge_after` in the past and `purge_by`
      in the future → confirm demand includes prescale.
- [ ] Unit test: inject class rows with `purge_after` in the future (protected
      zone) → confirm prescale contribution is 0.
- [ ] Unit test: inject class rows with `purge_by` in the past (dormant zone)
      → confirm prescale contribution is 0.
- [ ] Unit test: inject class rows with `purge_after=None` → confirm skipped.
- [ ] All existing `estimate_demand` tests are updated or replaced (old tests
      using `running`/`stops_at` dict keys must be ported to the new keys).
- [ ] `AUTOSCALE_ENABLED` default remains `false`; the kill-switch in
      `run_autoscale` (line 917) is not modified.

## Implementation Plan

**Files to modify:**
- `cspawn/cs_docker/autoscale.py`:
  - `gather_cluster_state` function (orchestrator section, around line 638-645):
    change query and dict shape.
  - `estimate_demand` function (pure section, lines 270-326):
    update prescale loop and docstring.

**Files to check for existing tests:**
- `tests/` directory — search for any existing tests that call `estimate_demand`
  with `running`/`stops_at` keys; update those mocked dicts to use
  `purge_after`/`purge_by`.

**Testing plan (pure function — no DB or Docker needed):**
- Call `estimate_demand` directly with mocked `classes` lists and `hosts` lists.
- Cover all four cases: active-window class, protected-zone class, dormant class,
  no-window class.
- Verify `prescale` arithmetic: `ceil(n_students * roster_fraction)`.
- Verify `headroom` is always added.

## Verification Command

```
uv run pytest tests/ -k "autoscale or estimate_demand" -v
```
