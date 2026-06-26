---
id: '002'
title: "POST /classes/id/cluster route \u2014 timestamp stamping and roster sizing"
status: done
use-cases:
- SUC-001
- SUC-005
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# POST /classes/id/cluster route — timestamp stamping and roster sizing

## Description

Add two new routes to `cspawn/main/routes/classes.py` that implement the
instructor "Create my cluster" action:

1. `POST /classes/<id>/cluster` — stamps `purge_after`, `purge_by`, and
   `target_nodes` on the `Class` row; returns JSON immediately; never
   provisions inline.
2. `GET /classes/<id>/cluster/status` — returns the current zone string for
   use by the class detail page JavaScript.

**Inert-by-default:** This route only writes to the DB. It does not call
`run_autoscale`, `expand_node`, or any Docker/DO API. The autoscaler reads the
timestamps on its next cycle — only if `AUTOSCALE_ENABLED=true` (default: false).

**Pattern anchor:** Mirror the `class_run_state` route at `classes.py:353-378` —
`@main_bp.route`, `@instructor_required`, immediate `db.session.commit()`,
return `jsonify(...)`, 200.

### Timestamp computation (decisions baked in)

```python
from datetime import datetime, timezone, timedelta
from math import ceil

click_time = datetime.now(timezone.utc)

# end_date is nullable — fall back to click_time + 1h when None
if class_.end_date is not None:
    # Use only the time-of-day component of end_date, applied to today (UTC)
    end_tod = class_.end_date.astimezone(timezone.utc).time()
    today_cutoff = datetime.combine(click_time.date(), end_tod, tzinfo=timezone.utc)
    purge_after = max(today_cutoff, click_time + timedelta(hours=1))
else:
    purge_after = click_time + timedelta(hours=1)

purge_by = purge_after + timedelta(hours=1)
```

Click-fallback duration is `click_time + 1h` (canonical; 90-min figure superseded).

### Node sizing (decisions baked in)

```python
from cspawn.cs_docker.tiers import load_tiers

tiers = load_tiers(current_app.config)
tier_capacity = tiers[0].capacity  # use default (first) tier
target_nodes = ceil(len(class_.students) / tier_capacity)
```

`target_nodes` is ALWAYS recomputed from the current roster on every POST,
including re-arms (a student who dropped since the first click is reflected).

### Idempotent re-arm

If `class_.purge_after` is already set and `now < class_.purge_by`, a second
POST re-arms: recompute all three fields from the new `click_time` and current
roster. Do not reject the request.

### Status endpoint zones

`GET /classes/<id>/cluster/status` returns JSON with a `status` key:
- `{"status": "unarmed"}` — `purge_after` is None
- `{"status": "provisioning"}` — `now < purge_after` (protected zone)
- `{"status": "active"}` — `purge_after <= now < purge_by` (active-purge zone)
- `{"status": "expired"}` — `now >= purge_by` (dormant zone)

(Distinguishing "provisioning" vs "ready" via live node count requires Docker
access; defer that to a future sprint. "provisioning" covers the entire protected
zone for now.)

## Acceptance Criteria

- [x] `POST /classes/<id>/cluster` returns HTTP 200 JSON `{"success": true}`
      immediately without any Docker or DO API calls.
- [x] After the POST, `class_.purge_after` equals
      `max(today @ time-of-day(end_date), click_time + 1h)` when `end_date` is
      set, or `click_time + 1h` when `end_date` is None.
- [x] After the POST, `class_.purge_by` equals `purge_after + 1h`.
- [x] After the POST, `class_.target_nodes` equals
      `ceil(len(class_.students) / tier_capacity)`.
- [x] A second POST while the window is active recomputes all three fields
      from the new click time and current roster (idempotent re-arm).
- [x] Route is gated by `@instructor_required`; a non-instructor receives 403.
- [x] `GET /classes/<id>/cluster/status` returns the correct zone string for
      each of the four states (`unarmed`, `provisioning`, `active`, `expired`).
- [x] No call to `run_autoscale`, `expand_node`, or any Docker/DO primitive
      occurs in either route.

## Implementation Plan

**Files to modify:**
- `cspawn/main/routes/classes.py` — add two new route functions after
  `class_run_state` (after line 378). Add `from math import ceil` and
  `from cspawn.cs_docker.tiers import load_tiers` to imports if not present.

**Testing plan (Flask test client — no live DO needed):**
- `POST /classes/<id>/cluster` with `end_date` set:
  assert 200, correct `purge_after` (>= click_time + 1h), correct `purge_by`,
  correct `target_nodes`.
- `POST /classes/<id>/cluster` with `end_date=None`:
  assert `purge_after` approximately equals `click_time + 1h`.
- Re-arm: POST twice; assert second response updates all three fields.
- Auth: POST as non-instructor; assert 403.
- `GET /classes/<id>/cluster/status` for each of the four zone states.

## Verification Command

```
uv run pytest tests/ -k "cluster" -v
```
