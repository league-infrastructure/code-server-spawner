---
id: '003'
title: Confirm node rebalance still relocates a create-time-pinned host
status: done
use-cases:
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: pin-codehosts-to-node-no-migration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Confirm node rebalance still relocates a create-time-pinned host

## Description

Once every new codehost is pinned at creation (Ticket 001), the fleet
`cspawnctl node rebalance` operates on is, by default, fully pinned rather
than unpinned. `rebalance()`/`plan_rebalance()` (`cspawn/cli/node.py:225-341`
and `342-406`) must continue to relocate hosts correctly against that
fully-pinned fleet: `plan_rebalance()` reads live placement
(`per_node`/`eligible`, derived from Swarm task state, not from any existing
constraint) and is therefore unaffected by pins either way; for each planned
move, `rebalance()` calls the existing, unchanged `_pin_service_to_node()`
(`cli/node.py:159-172`), which already replaces — not accumulates — any
prior `node.hostname==` constraint on the service, whether that prior
constraint came from an earlier rebalance or from Ticket 001's create-time
path.

**This ticket adds no production code.** Its job is to prove — with a new
regression test, not by re-reading the source — that `rebalance()` and
`plan_rebalance()` relocate a create-time-pinned host exactly as they
already relocate an unpinned or rebalance-pinned one: unpin → move → repin,
with no accumulation of constraints. If the test reveals that assumption is
wrong, that is a signal to reopen architecture review, not to patch around
it here.

## Acceptance Criteria

- [x] New regression test(s) added to `test/test_node_rebalance.py` — which
  today only exercises the pure `plan_rebalance()` algorithm against plain
  Python dicts/lists, not `rebalance()`'s actual docker-service interactions
  — covering `rebalance()`'s interaction with `_pin_service_to_node()`
  against a fleet where at least one host already carries a create-time
  `node.hostname==` pin (built the same way as Ticket 002's fixture: a real
  call to `_pin_service_to_node()` against a bare mock service, so the
  fixture's constraint shape is guaranteed accurate).
- [x] Test confirms `plan_rebalance()`'s move-planning output (which hosts
  move, from where, to where) is identical whether the input hosts are
  unpinned, rebalance-pinned, or create-time-pinned — it only consumes
  `per_node`/`eligible`, derived from live task placement, never from
  existing constraints.
- [x] Test confirms that after `rebalance()` moves a create-time-pinned
  host, the service's constraint list contains exactly one
  `node.hostname==` entry (the new target) — not two — i.e.
  `_pin_service_to_node()`'s existing replace-not-accumulate normalization
  holds correctly for a pin it did not originally set.
- [x] No changes made to `rebalance`, `plan_rebalance`, or
  `_pin_service_to_node` in this ticket — if making the new test pass
  requires modifying any of them, stop and flag it (Ticket 001's design
  assumption that these are reusable unchanged would be wrong, which is an
  architecture-level finding, not a ticket-level fix).

## Implementation Plan

**Approach:** Pure regression-test addition; no production code touched.
Extend `test/test_node_rebalance.py` with a `MagicMock`-based fixture
section (mirroring the mocking style already established in
`test/test_node_unpin.py` and `test/test_node_missing.py`, since this
file's existing tests only use plain dicts against `plan_rebalance()`
directly) representing several codehost services — at least one pre-pinned
via a real `_pin_service_to_node()` call, matching Ticket 001's output
shape — then invoke `rebalance()`'s move-and-pin logic (or
`plan_rebalance()` plus the per-move `_pin_service_to_node()` call it
makes) and assert the resulting constraint list on the moved service(s).

**Files to create/modify:**
- `test/test_node_rebalance.py` only — extend with the new
  `MagicMock`-based fixture(s) and test(s) described above.

**Documentation updates:** none required — the rebalance-compatibility
guarantee is already documented in `architecture-update.md`'s Impact on
Existing Components section; no user-facing doc needs updating.

**Depends on Ticket 001:** same reasoning as Ticket 002 — the "create-time
pin" framing only makes sense once Ticket 001 exists, so this ticket
sequences after it, even though the test itself exercises only
pre-existing, unchanged `rebalance()`/`plan_rebalance()` code against a
fixture shaped like Ticket 001's output.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_rebalance.py -v`
  during development; `uv run pytest --ignore=test/test_admin_coverage.py -q`
  for the full-suite gate (excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures).
- **New tests to write**: the create-time-pin rebalance regression
  described in Acceptance Criteria, added to `test/test_node_rebalance.py`.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
