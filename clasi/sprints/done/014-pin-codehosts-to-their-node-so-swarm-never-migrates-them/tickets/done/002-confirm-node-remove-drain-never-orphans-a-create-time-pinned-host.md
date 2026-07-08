---
id: '002'
title: Confirm node remove/drain never orphans a create-time-pinned host
status: done
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: pin-codehosts-to-node-no-migration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Confirm node remove/drain never orphans a create-time-pinned host

## Description

A hard `node.hostname==` pin (Ticket 001) must never permanently orphan a
host: if the node it's pinned to is deliberately removed or drained by an
operator, the pin must be cleared first so Swarm is free to reschedule the
task elsewhere. This mechanism already exists and is already proven in
production for `node rebalance`-set pins: `graceful_remove_node()`
(`cspawn/cli/node.py:2604-2710`) unconditionally calls
`_unpin_services_from_node()` (`cli/node.py:175-214`) at `node.py:2681`,
*before* the `if node_obj:` drain/remove branch at `node.py:2683` — running
regardless of whether the swarm-side node object was even found, and
regardless of whether the pin came from `node rebalance` or from Ticket
001's new create-time path.

**This ticket adds no production code.** Its job is to prove — with a new
regression test, not by re-reading the source — that `_unpin_services_from_node()`
and `graceful_remove_node()` treat a pin applied by Ticket 001's path
identically to one applied by `node rebalance`. If the test reveals that
assumption is wrong, that is a signal to reopen architecture review, not to
patch around it here.

**Confirmed policy distinction** (architecture-update.md Step 6, reconfirmed
by the team-lead at `stakeholder_approval`): *uncontrolled* node trouble
(heartbeat timeout, crash, overload — Swarm's own health-driven
rescheduling) leaves a pinned host `Pending` on its node — no failover, the
sprint's core accepted trade-off. *Deliberate* operator maintenance
(`cspawnctl node stop`, i.e. `graceful_remove_node`) unpins the host first
and lets it move, exactly as an operator draining a node for maintenance
would want. This ticket's tests exercise only the second case.

## Acceptance Criteria

- [x] New regression test(s) added to `test/test_node_unpin.py`, alongside
  the existing `TestGracefulRemoveNodeUnpinOrdering` class, proving: a
  codehost service whose only `node.hostname==` constraint was set via
  Ticket 001's create-time path (i.e., a pin with no prior `node rebalance`
  history) is stripped by `_unpin_services_from_node()`/
  `graceful_remove_node()` identically to a rebalance-set pin. Build the
  fixture by calling the real (unchanged) `_pin_service_to_node()` against
  a bare mock service first, so the fixture's constraint shape is
  guaranteed accurate rather than hand-typed.
- [x] Test confirms the unpin call happens unconditionally, before the node
  is drained/removed — i.e. `_unpin_services_from_node(...)` is called
  regardless of whether `_find_swarm_node(...)` resolves a swarm-side node
  object for the target.
- [x] Test confirms a pinned-then-unpinned host has no stale or duplicate
  `node.hostname==` constraint left behind after unpin — a single clean
  removal, constraint list otherwise unchanged (e.g. any `node.role`
  constraint is preserved).
- [x] No changes made to `_unpin_services_from_node`, `graceful_remove_node`,
  `_pin_service_to_node`, or `_service_constraints` in this ticket — if
  making the new test pass requires modifying any of them, stop and flag it
  (Ticket 001's design assumption that these are reusable unchanged would be
  wrong, which is an architecture-level finding, not a ticket-level fix).
- [x] The policy distinction above (uncontrolled trouble → stays `Pending`;
  deliberate drain/remove → unpin-and-move) is captured in this ticket's own
  description (done) — no separate doc file needs updating.

## Implementation Plan

**Approach:** Pure regression-test addition; no production code touched.
Build a `MagicMock`-based fixture representing a codehost service pinned by
Ticket 001's path: construct a bare mock service (same shape as
`test_node_missing.py`'s `_make_raw_service` / `test_node_unpin.py`'s
existing service doubles), call the real `_pin_service_to_node()` against
it once to get an accurate `node.hostname==<fqdn>` constraint on the mock's
`Spec.TaskTemplate.Placement.Constraints`, then run that fixture through
the existing, already-tested `_unpin_services_from_node()` /
`graceful_remove_node()` call paths.

**Files to create/modify:**
- `test/test_node_unpin.py` only — extend with the new fixture/test
  class(es) described above, following the file's existing `MagicMock`
  style; no new test utilities need to be invented, the file already has
  everything needed for a `docker.DockerClient`/`Service`/node object
  double.

**Documentation updates:** none beyond this ticket's own Description — the
policy distinction is already documented in `architecture-update.md` Step 6
and reconfirmed in the `stakeholder_approval` gate notes; no user-facing
doc describes node drain/remove behavior in enough detail to need updating.

**Depends on Ticket 001:** needs Ticket 001's `_pin_service_to_node()` call
site to exist conceptually (the fixture is built by calling
`_pin_service_to_node` directly, which is unchanged and already merged
independently of Ticket 001's call site — but this ticket's framing, "a
create-time pin," only makes sense once Ticket 001 exists) so the fixture
accurately represents "a pin applied by the new code path," even though the
test itself exercises only pre-existing, unchanged unpin/drain code.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_unpin.py -v`
  during development; `uv run pytest --ignore=test/test_admin_coverage.py -q`
  for the full-suite gate (excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures).
- **New tests to write**: the create-time-pin unpin-ordering regression
  described in Acceptance Criteria, added to `test/test_node_unpin.py`.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
