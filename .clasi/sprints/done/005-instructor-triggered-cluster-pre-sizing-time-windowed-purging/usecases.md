---
status: draft
sprint: '005'
---
<!-- CLASI: Sprint-level use cases for sprint 005 -->

# Sprint 005 — Use Cases

## SUC-001: Instructor pre-sizes cluster before class

**Actor:** Instructor (logged-in user with instructor role on the class)

**Trigger:** Instructor navigates to the class detail page and clicks
"Create my cluster" before the class session begins.

**Preconditions:**
- The instructor is logged in and owns or instructs the class.
- The class has at least one enrolled student.
- The class's `end_date` is set (time-of-day used for `purge_after`).

**Main Flow:**
1. Instructor clicks "Create my cluster" on the class detail page.
2. The browser POSTs to `POST /classes/<id>/cluster` (non-blocking).
3. The server computes `target_nodes = ceil(len(students) / tier_capacity)`.
4. The server stamps `purge_after` and `purge_by` on the `Class` row
   and stores `target_nodes`.
5. The server returns JSON immediately (HTTP 200) without provisioning anything.
6. The page shows a status indicator reflecting the new pre-sized state.

**Idempotent Re-arm:**
- If a cluster window is already active (`purge_after` and `purge_by` are set
  and `now < purge_by`), a second click re-arms: new `purge_after`/`purge_by`
  are computed from the new click time and the row is updated. `target_nodes`
  is recomputed from the current roster size.

**Postconditions:**
- `Class.purge_after`, `Class.purge_by`, `Class.target_nodes` are set.
- No nodes have been provisioned; the autoscaler reads these fields next cycle.

**Acceptance Criteria:**
- [ ] POST /classes/<id>/cluster returns 200 JSON immediately (no inline provisioning).
- [ ] `purge_after = max(today @ time-of-day(end_date), click_time + 1h)`.
- [ ] `purge_by = purge_after + 1h`.
- [ ] `target_nodes = ceil(len(class.students) / tier_capacity)` using NODE_TIERS.
- [ ] Second click re-arms timestamps and recomputes target_nodes.
- [ ] Route requires instructor role; non-instructors receive 403.

---

## SUC-002: Autoscaler provisions cluster in response to purge-window signal

**Actor:** Autoscale cron job (system)

**Trigger:** `cspawnctl node autoscale` runs and finds one or more `Class`
rows with `purge_after <= now < purge_by`.

**Preconditions:**
- `AUTOSCALE_ENABLED=true` in operator config (off by default — nothing runs
  without explicit operator enablement).
- At least one class is in the active-purge window.

**Main Flow:**
1. `gather_cluster_state` queries `Class` rows where `purge_after <= now < purge_by`.
2. For each such class, a class dict is built with `students` list and
   `purge_after`/`purge_by` (replacing the old `running`/`stops_at` shape).
3. `estimate_demand` sums `ceil(len(students) * ROSTER_FRACTION)` across all
   active-window classes, adds headroom, returns the demand figure.
4. `build_plan` computes deficit and returns a `ScalePlan`.
5. `apply_plan` provisions required nodes (only if kill-switch is enabled).

**Safety gate:** `AUTOSCALE_ENABLED` defaults to `false`. Merging this sprint
does NOT auto-provision anything. An operator must explicitly enable it.

**Postconditions:**
- New nodes are added to the Swarm if deficit > 0 and kill-switch is enabled.

**Acceptance Criteria:**
- [ ] `gather_cluster_state` queries by purge window, not by `Class.running`.
- [ ] `estimate_demand` produces correct demand from purge-window classes.
- [ ] Unit tests cover `estimate_demand` with mocked purge-window class rows.
- [ ] `AUTOSCALE_ENABLED=false` (default) causes early exit — no mutations.

---

## SUC-003: Idle CodeHost is reclaimed during the active-purge window

**Actor:** Reaper cron job (system) — `cspawnctl host purge`

**Trigger:** Cron runs during `purge_after <= now < purge_by` for a class.

**Preconditions:**
- A CodeHost belongs to a class with an active purge window.
- The host has been idle for >= 15 minutes.

**Main Flow:**
1. Reaper iterates CodeHost rows, checking each host's class membership.
2. For hosts whose class has `purge_after <= now < purge_by`, the idle
   threshold is **15 minutes** (`modified_ago >= 15`).
3. Idle hosts are stopped and their DB records deleted.

**Protected window behaviour:**
- Before `purge_after` (protected zone), hosts are NEVER reaped regardless
  of idle time. Students can step away without losing their session.

**Postconditions:**
- Idle CodeHosts in the active-purge window are stopped and removed from the DB.

**Acceptance Criteria:**
- [ ] Hosts in protected zone (now < purge_after) are never reaped.
- [ ] Hosts in active-purge zone idle >= 15 min are stopped and deleted.
- [ ] Existing `is_quiescent` threshold (15-min `modified_ago`) satisfies the
      active-purge zone requirement; no threshold change needed for this zone.

---

## SUC-004: All remaining resources force-removed at purge_by (dormant)

**Actor:** Reaper cron job (system) — `cspawnctl host purge`

**Trigger:** Cron runs at or after `purge_by` for a class.

**Preconditions:**
- A class has `purge_by` set and `now >= purge_by`.
- CodeHosts and/or nodes still exist for that class.

**Main Flow:**
1. Reaper identifies classes in the dormant zone (`now >= purge_by`).
2. All remaining CodeHosts for the class are force-removed immediately
   (no idle-timeout check).
3. `purge_after` and `purge_by` are cleared on the Class row.
4. Emptied nodes are drained and removed by the autoscaler on the next cycle.

**Postconditions:**
- No CodeHosts remain for the dormant class.
- Class purge fields are cleared; class is no longer tracked for scaling.

**Acceptance Criteria:**
- [ ] At or after `purge_by`, all CodeHosts for the class are force-removed.
- [ ] Class `purge_after`, `purge_by`, `target_nodes` are cleared after force-remove.
- [ ] Autoscaler no longer counts dormant class toward demand.

---

## SUC-005: Instructor views cluster status on the class page

**Actor:** Instructor (logged-in)

**Trigger:** Instructor visits the class detail page.

**Main Flow:**
1. Page renders cluster status section reflecting the current zone:
   - No purge window set: "Create my cluster" button is shown.
   - Protected zone (`now < purge_after`): "Cluster pre-sized — N nodes
     requested. Class starts in [time]."
   - Active-purge zone (`purge_after <= now < purge_by`): "Cluster active —
     idle reclaim enabled. Expires at [purge_by]."
   - Dormant (`now >= purge_by`): "Cluster expired. Create a new cluster for
     the next session."
2. Status is read from `purge_after`, `purge_by`, `target_nodes` on the class
   object — no separate polling endpoint required.

**Postconditions:**
- Instructor has accurate visibility into the cluster lifecycle zone.

**Acceptance Criteria:**
- [ ] Class detail page shows "Create my cluster" button when no purge window is set.
- [ ] Page shows correct status for each of the four zone states.
- [ ] Button is only shown/enabled for instructors of the class.
- [ ] Status display does not require a page reload after clicking the button
      (JS updates the display from the JSON response).
