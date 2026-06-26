# Instructor-triggered cluster pre-sizing + time-windowed purging

**Audience:** an engineer or agent working on the code-server-spawner's node
autoscaling. This issue defines *when* the cluster scales up and down. It builds
on two prior issues and replaces part of their demand model:

- [[node-autoscaling-control-loop]] — Node Autoscaling: control loop & policy
- [[multi-size-node-provisioning]] — Multi-Size Node Provisioning + `NODE_TIERS` config

This document is self-contained for the *scaling-signal* decision. See the two
issues above for the provisioning/reaping/contraction mechanics it reuses.
(Both were originally captured only as Plan-agent transcripts at
`.clasi/log/018-Plan.md` / `.clasi/log/019-Plan.md`; they are now tracked issues.)

---

## TL;DR

`Class.running` is a **sticky** flag: instructors start a class and never stop
it, so it has no falling edge and is useless as a scale-**down** signal. Replace
it with an **explicit instructor action** plus **two purge timestamps** on the
class that make teardown automatic and self-expiring.

1. Instructor clicks **"Create my cluster"** on the class page just before class.
   This provisions nodes sized from the roster, and stamps two timestamps.
2. Resources are **protected** until `purge_after`, **gently idle-purged** from
   `purge_after` to `purge_by`, then **force-removed** at `purge_by`.
3. No instructor "stop" action is ever needed — idleness reclaims everything,
   bottom-up: per-user hosts first, then emptied nodes.

---

## Problem

The current/earlier plan keys scaling off `Class.running`. In practice
instructors flip a class to running and never flip it back, so:

- there is no clear moment to scale **down**;
- classes are effectively "always running," so demand never falls.

Stopping the class manually was considered and rejected — instructors won't
reliably do it. The reliable signal we *do* have is the instructor showing up
and starting the session before class.

---

## Design

### 1. Instructor action — "Create my cluster"

- A button on the **class page** (tied to a specific `Class`, so it knows the
  roster). Status read-back: provisioning / N nodes ready / idle-expiring.
- `POST /classes/<id>/cluster` — **non-blocking**, returns JSON immediately (same
  pattern as `class_run_state`, `cspawn/main/routes/classes.py:367-378`). It only
  records intent + stamps timestamps; it never provisions inline.
- **Idempotent:** a second click on an active cluster re-arms the window rather
  than duplicating.
- **Sizing is auto from roster:** `target_nodes = ceil(len(class.students) /
  tier_capacity)`, tiers from `NODE_TIERS` ([[multi-size-node-provisioning]]). No size input from the
  instructor.

### 2. Two new `Class` timestamp fields gate purging

- **`purge_after`** = `max( today @ time-of-day(end_date), click_time + 1h )`
  Before this time, nothing for the class is reaped.
- **`purge_by`** = `purge_after + 1h` (fixed offset)
  Hard cutoff: anything still alive is force-removed and the class goes dormant.

(Plus a `target_nodes` field to record the requested capacity.)

### 3. Three zones per class

```
   [ protected ]        [ idle-purge active ]      [ dormant ]
──────────────────────┬──────────────────────────┬─────────────────►
                  purge_after                  purge_by
  nothing reaped       15-min idle rules apply     force-remove
  (students can         to hosts AND nodes          remainder,
   step away)                                        stop tracking
```

| Resource | Protected (< `purge_after`) | Active-purge (`purge_after`..`purge_by`) | At `purge_by` |
|---|---|---|---|
| **CodeHost** (per-user code-server) | never reaped | 15 min idle → shut down | force-removed |
| **Node** (swarm worker droplet) | never reaped, even if empty | 15 min at zero hosts → drain & remove | force-removed |

All grace windows are **15 minutes**.

---

## Key schema gotcha (verified)

`Class.start_date` / `Class.end_date` (`cspawn/models.py:166-167`) are the
**term span** — the whole enrollment period, not a daily meeting window.
`Class.update()` (`models.py:188-198`) deactivates the class once `now` is
outside `[start_date, end_date]`. So you must use only the **time-of-day** off
`end_date` for the daily window, never the full datetime. The `click_time + 1h`
floor in `purge_after` covers classes whose `end_date` time-of-day is a
meaningless artifact (midnight / created-at).

`recurrence_rule` (RRULE) exists on `Class` but is an unparsed string; not used
here. The historical per-session window lived in `running_at` / `stops_at`;
`running` is dropped as a scaling input.

---

## Implementation touch points

- **`cspawn/models.py`** — add `purge_after`, `purge_by`, `target_nodes` to
  `Class`; DB migration.
- **`cspawn/main/routes/classes.py`** — new `POST /classes/<id>/cluster`:
  roster→nodes sizing, stamp timestamps, idempotent re-arm.
- **Class template** — "Create my cluster" button + status read-back.
- **Reaper** (`cspawn/cli/node.py` `host reap` / `purge`) — retune idle
  threshold to 15 min; gate reaping by `now > purge_after`; force-remove past
  `purge_by`.
- **`cspawn/cs_docker/autoscale.py`** ([[node-autoscaling-control-loop]]) —
  `estimate_demand` reads classes with a live purge window instead of
  `Class.running`; node contraction gated by the same window logic.
- Tier sizing from `NODE_TIERS` per [[multi-size-node-provisioning]].

This collapses the previously-proposed `ClusterRequest` table into three fields
on `Class`.

---

## Open / to confirm

- **Click fallback duration:** spec says `click_time + 1h`. (A "90 minutes"
  answer was given earlier for a session-length question; treated as superseded
  by the explicit 1h. Confirm if 90 min was intended.)
- **`end_date` time-of-day quality:** confirm against prod data that class
  records carry a meaningful time on `end_date`; the 1h floor covers us if not.
- **During-class idle hosts:** by design, idle CodeHosts are NOT reaped inside
  the protected window (a student stepping away keeps their slot). Accepted cost.
