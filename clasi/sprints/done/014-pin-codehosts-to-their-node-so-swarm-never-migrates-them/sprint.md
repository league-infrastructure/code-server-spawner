---
id: '014'
title: Pin codehosts to their node so Swarm never migrates them
status: done
branch: sprint/014-pin-codehosts-to-their-node-so-swarm-never-migrates-them
use-cases: []
issues:
- pin-codehosts-to-node-no-migration.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 014: Pin codehosts to their node so Swarm never migrates them

## Goals

Stop Docker Swarm from ever migrating a running codehost between nodes.
Every newly started codehost should carry a hard `node.hostname==<node>`
placement constraint so Swarm cannot reschedule it onto another worker when
its current node has trouble — eliminating the disconnect+cascade pattern
behind the 2026-07-06 incident (swarm2 overloaded, its ~14 hosts stampeded
onto swarm4).

## Problem

Codehost services are created with `constraints=["node.role==worker"]`
(`PLACEMENT_CONSTRAINTS`, `cspawn/cs_docker/csmanager.py:444-453`) — Swarm
may place *and later reschedule* them onto any worker, forever. When a node
gets into trouble (overload, heartbeat timeout, reboot), Swarm migrates its
hosts elsewhere. Every migration disconnects that student's session, and a
node dropping many hosts at once can overload the node they land on,
cascading further migrations.

## Solution

Apply the existing, already-proven `node.hostname==` pinning mechanism
(`_pin_service_to_node()` / `_unpin_services_from_node()`,
`cspawn/cli/node.py`, already used by `node rebalance` and node
remove/drain) to every *newly created* codehost, not just ones a rebalance
touches. The one design fork — pin the node up-front (Approach A) vs. let
Swarm place it then pin where it landed (Approach B) — is decided:
**Approach B**, per the linked issue's recorded decision and reconfirmed by
the team-lead for this planning pass (not reopened). Create with today's
`node.role==worker` constraint unchanged, read back which node Swarm's own
scheduler assigned the task to, then pin it there with the existing
`_pin_service_to_node()`. See `architecture-update.md` for the full
reasoning (Step 6), including why Approach A's "reuse the rebalance path's
capacity-aware selection" premise does not hold up against the actual code,
and why a client-side node selection would be race-prone for concurrent
host starts (e.g. many students starting at once).

Gated behind a config toggle, `PIN_HOSTS_TO_NODE` (default on), so the
behavior can be disabled without a code change if it ever misbehaves.

## Success Criteria

- A newly started codehost's Swarm service carries exactly one
  `node.hostname==<node>` constraint matching the node it actually runs on.
- Simulating a node going unavailable (mocked Swarm) does not cause a
  pinned codehost's task to be reassigned elsewhere.
- Node remove/drain still clears the pin (`_unpin_services_from_node`) so a
  pinned host is never permanently orphaned — regression-tested against
  create-time pins, not just rebalance-time pins.
- `node rebalance` still relocates a fully-pinned fleet correctly
  (unpin → move → repin), regression-tested.
- `PIN_HOSTS_TO_NODE=false` restores today's `node.role==worker`-only
  behavior exactly.
- Full suite green (excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures).

## Scope

### In Scope

- Pinning newly created codehost services to their Swarm-assigned node
  (Approach B, per the architecture recommendation).
- The `PIN_HOSTS_TO_NODE` config toggle (default on) and a
  `PIN_HOST_PLACEMENT_TIMEOUT_S` tunable for the placement-node poll.
- Regression test coverage proving node remove/drain and `node rebalance`
  already handle create-time-pinned hosts correctly (no production code
  change expected for either).
- Populating `CodeHost.node_name` at creation time instead of waiting for
  the next sync (small, free improvement riding along with the pin).

### Out of Scope

- Capacity/tier policy itself (separate, already-improved concern — see
  the `new-node-cold-image-pull-503-herd` issue).
- Retroactively pinning hosts that already exist before this sprint ships
  (they get pinned the next time they're recreated or explicitly
  rebalanced — flagged as an open question for the stakeholder, not
  silently dropped).
- Any change to `_pin_service_to_node`, `_unpin_services_from_node`,
  `_service_constraints`, `plan_rebalance`, `rebalance`, or
  `graceful_remove_node` — all reused completely unchanged.
- Approach A (spawner-side node selection) — evaluated and not recommended;
  see `architecture-update.md` Step 6 for the full reasoning, kept in
  enough detail to revisit if the stakeholder prefers it.

## Test Strategy

Unit tests with a mocked Docker/Swarm client, matching the existing style
of `test/test_node_unpin.py` and `test/test_node_rebalance.py` (no live
Docker/DigitalOcean access): the new placement-node resolution and pin call
site in `csmanager.py`'s host-creation path; the config toggle on/off; the
best-effort timeout/failure path (never blocks host creation); the
409-recovery path not double-pinning; and regression coverage proving
`graceful_remove_node`/`_unpin_services_from_node` and
`rebalance`/`plan_rebalance` behave identically against create-time-pinned
hosts as they already do against rebalance-time-pinned ones. No new
integration/system-level testing beyond what these mocked-client tests
already cover — the underlying Swarm pin/unpin mechanism itself is already
production-proven via `node rebalance`.

## Architecture Notes

See `architecture-update.md` for the full design, including the A-vs-B
design fork, module boundaries, diagrams, and design rationale. Headline
decision: **Approach B** (pin after Swarm places it, confirmed — not
reopened this pass), reusing `_pin_service_to_node` via a lazy,
function-local import from `cspawn.cli.node` into
`cspawn/cs_docker/csmanager.py` — matching an already-established codebase
convention (`cspawn/admin/routes.py:458`, `cspawn/cs_docker/autoscale.py`
lines 649/970/1150 do the same). The architecture self-review passed
(APPROVE); remaining open questions needing stakeholder input before
implementation are listed in `architecture-update.md`, Step 7 (none block
ticketing).

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (self-review verdict: APPROVE; see sprint-planner's report)
- [x] Stakeholder has approved the sprint plan (`stakeholder_approval` gate recorded 2026-07-08)

## Tickets

`stakeholder_approval` recorded 2026-07-08; all three tickets created and
`open`. Tickets execute serially in the order listed (002 and 003 both
depend only on 001 and may run in either order relative to each other):

| # | Title | Depends On | File |
|---|-------|------------|------|
| 001 | Pin new codehosts to their placement node at creation | — | `tickets/001-pin-new-codehosts-to-their-placement-node-at-creation.md` |
| 002 | Confirm node remove/drain never orphans a create-time-pinned host | 001 | `tickets/002-confirm-node-remove-drain-never-orphans-a-create-time-pinned-host.md` |
| 003 | Confirm `node rebalance` still relocates a create-time-pinned host | 001 | `tickets/003-confirm-node-rebalance-still-relocates-a-create-time-pinned-host.md` |
