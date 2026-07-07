---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 014 Use Cases

## SUC-001: A New Codehost Is Pinned To Its Placement Node At Start
Parent: UC-003, UC-004

- **Actor**: System (spawner + Docker Swarm), on behalf of Student/Instructor (UC-003)
- **Preconditions**: `PIN_HOSTS_TO_NODE` is enabled (default); a class is
  running and the user has no existing host (UC-003 preconditions).
- **Main Flow**:
  1. User clicks "Start" (UC-003 step 1).
  2. Spawner creates the Swarm service with today's `node.role==worker`
     constraint, unchanged (UC-003 step 3 / UC-004 step 1-2 — Swarm's own
     scheduler places it).
  3. Spawner polls the newly created service's task until Docker assigns it
     a `NodeID`, bounded by a timeout.
  4. Spawner resolves that node's hostname and applies a
     `node.hostname==<node>` constraint via the existing
     `_pin_service_to_node()`, replacing/normalizing any prior
     `node.hostname==` constraint (there is none, for a fresh host).
  5. Swarm reschedules the task once, recreating the container on the same
     node the constraint names.
  6. The `CodeHost` row records the resolved node name immediately.
- **Postconditions**: The codehost service carries a `node.hostname==`
  constraint naming the node it is actually running on; the `CodeHost` DB
  row's `node_name` is populated without waiting for the next sync.
- **Acceptance Criteria**:
  - [ ] A newly started codehost's Swarm service constraints include exactly
    one `node.hostname==<node>` entry after creation completes.
  - [ ] The named node matches the node the container is actually running
    on (no mismatch between the constraint and the live task placement).
  - [ ] If the placement-node poll times out, host creation still succeeds
    (best-effort: a WARNING is logged, the host is not pinned, but the
    student is not blocked).

---

## SUC-002: A Node In Trouble Does Not Trigger Host Migration
Parent: UC-004

- **Actor**: System (Docker Swarm), reacting to a node health event
- **Preconditions**: A codehost is pinned to node N (SUC-001); node N
  becomes unavailable to Swarm (heartbeat timeout, crash, or is marked
  unreachable) without an operator deliberately draining/removing it.
- **Main Flow**:
  1. Swarm marks node N unreachable.
  2. Swarm evaluates the pinned service's task: the hard
     `node.hostname==N` constraint means no other node satisfies placement.
  3. Swarm leaves the task `Pending` rather than rescheduling it onto a
     different node.
  4. When node N returns/recovers, the task resumes on N.
- **Postconditions**: The student's session is unavailable while N is down,
  but no other node receives an unplanned extra host, and no cascade of
  migrations occurs.
- **Acceptance Criteria**:
  - [ ] With a mocked/faked Swarm: simulating node N becoming unavailable
    does not cause the pinned service's task to be reassigned to a
    different `NodeID`.
  - [ ] This behavior is the explicitly accepted trade-off (no failover for
    a pinned host) — documented, not silently different from expectations.

---

## SUC-003: Node Removal/Drain Clears The Pin So The Host Is Never Orphaned
Parent: UC-006

- **Actor**: Admin / Operator
- **Preconditions**: A codehost is pinned to node N (SUC-001); the operator
  runs `cspawnctl node stop <N>` (graceful path) or the op-run "remove" flow
  to deliberately retire node N.
- **Main Flow**:
  1. Operator initiates node removal/drain for N.
  2. `graceful_remove_node()` calls `_unpin_services_from_node()` first,
     stripping the `node.hostname==N` constraint from every codehost
     service pinned to N (whether pinned by SUC-001's create-time path or
     by a prior `node rebalance`).
  3. Node N is drained, its tasks are rescheduled by Swarm onto other
     eligible workers (now unconstrained), N is removed, its droplet is
     destroyed.
- **Postconditions**: No codehost service is left permanently pinned to a
  node that no longer exists; affected hosts land on another eligible
  worker instead of staying `Pending` forever.
- **Acceptance Criteria**:
  - [ ] A codehost pinned via SUC-001's path is unpinned by
    `_unpin_services_from_node`/`graceful_remove_node` exactly as a
    rebalance-pinned host already is (regression test).
  - [ ] The unpin happens before the node is drained/removed, not after.

---

## SUC-004: `node rebalance` Still Relocates A Pinned Host
Parent: UC-006

- **Actor**: Admin / Operator
- **Preconditions**: A fleet where every codehost was pinned at creation
  (SUC-001, the new default); load is uneven across nodes.
- **Main Flow**:
  1. Operator runs `cspawnctl node rebalance`.
  2. `plan_rebalance()` computes moves from live task placement (unaffected
     by any existing pin) toward the least-loaded eligible nodes.
  3. For each planned move, `_pin_service_to_node()` replaces the host's
     existing `node.hostname==` constraint with one naming the new target
     node — exactly the same call used for an unpinned host today.
  4. Swarm reschedules the task onto the new target node.
- **Postconditions**: The fleet is rebalanced; every moved host is now
  pinned to its new node (no accumulation of stale constraints).
- **Acceptance Criteria**:
  - [ ] `node rebalance` against an all-pinned fleet produces the same
    moves/outcome as against an unpinned fleet with identical load
    (regression test).
  - [ ] After a move, the service has exactly one `node.hostname==`
    constraint (the new target), not two.

---

## SUC-005: Operator Disables Node Pinning Via Config Toggle
Parent: UC-003, UC-004

- **Actor**: Admin / Operator
- **Preconditions**: `PIN_HOSTS_TO_NODE=false` is set in the deployment's
  config (`public.env`).
- **Main Flow**:
  1. A user starts a new codehost (UC-003).
  2. The spawner creates the service with `PLACEMENT_CONSTRAINTS` only
     (`node.role==worker`), exactly as before this sprint.
  3. No placement-node poll or pin call happens.
- **Postconditions**: Behavior is identical to pre-sprint 014: Swarm is
  free to place and later migrate the host.
- **Acceptance Criteria**:
  - [ ] With the flag off, a newly created codehost's service constraints
    contain no `node.hostname==` entry.
  - [ ] No additional Swarm reschedule/restart happens at creation when the
    flag is off.
