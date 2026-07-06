---
id: '010'
title: "Node op reliability \u2014 stamp tier labels on VPC-advertised expands + recover\
  \ orphaned NodeOps on restart"
status: done
branch: sprint/010-node-op-reliability-stamp-tier-labels-on-vpc-advertised-expands-recover-orphaned-nodeops-on-restart
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues:
- expand-tier-labels-never-stamp.md
- nodeop-orphaned-on-container-restart.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 010: Node op reliability — stamp tier labels on VPC-advertised expands + recover orphaned NodeOps on restart

## Goals

Close out two live-confirmed node-operation reliability defects, both
diagnosed by direct code reading plus production observation on
2026-07-06:

1. `node expand` never stamps `cs.tier`/`cs.capacity` labels on any
   VPC-advertised swarm (i.e. every deployment today), because the
   labeling step matches the just-joined node by comparing `Status.Addr`
   to the droplet's *public* IP, when Docker Swarm always reports the
   node's *private* VPC address instead. The match can never succeed; the
   loop silently gives up after 90s inside a bare `except: pass`.
2. An admin-triggered `NodeOp` (expand/remove/rebalance) is stuck
   `status='running'` forever if the spawner container dies mid-run (deploy
   restart, OOM, reboot) — its `finally` block, which would write a
   terminal status, never executes. A container restart can also leave a
   DigitalOcean droplet created but never joined to the swarm — an
   orphaned, billed resource invisible to the cluster.

## Problem

**Tier labels:** `_join_swarm`'s cs.tier/cs.capacity block
(`cspawn/cli/node.py:1651-1678`) locates the just-joined node by IP
comparison against `Status.Addr`, which is always the node's VPC address
(`--advertise-addr <10.124.x.x>`, set at join time) — never the droplet's
public IP. The admin Nodes tab shows `---` for Tier/Capacity on every
expand; `tiers.py::node_capacity()` falls back to `DEFAULT_CAPACITY` (6),
under-counting large-tier nodes (true capacity 14) in the autoscaler until
an operator manually runs `node label-backfill --apply`. Confirmed live
twice on 2026-07-06 (swarm3, swarm4).

**Orphaned NodeOps:** `op-run` (`cspawn/cli/node.py:2842-2994`) sets
`node_ops.status='running'`, does the work, and writes the terminal status
in a `finally` block that never runs if the container is killed mid-op.
Confirmed live 2026-07-06: an `expand` op created droplet
`swarm4.dojtl.net` in DigitalOcean, then the container was force-restarted
before the droplet joined the swarm — the op stayed `running` for 40+
minutes (would have been forever), and the droplet sat orphaned with
nothing pointing an operator at it.

## Solution

**Tier labels:** Replace the IP-matching loop with a new
`_apply_labels_after_join()` helper that locates the joined node by
hostname (via the file's own pre-existing `_find_swarm_node`, already
proven by drain and `label-backfill`), and logs a `WARNING` naming the
node if the deadline expires without a match, instead of the current
silent `except: pass`. The adjacent `code-host-user` labeling block, which
has the same underlying defect but already self-heals via a name-guess
fallback, is deliberately left untouched this sprint (see
architecture-update.md Step 6 for the scoping rationale).

**Orphaned NodeOps:** Add a boot-time sweep
(`sweep_interrupted_node_ops()`) that marks every `NodeOp` row still
`status='running'` as `status='interrupted'` — always correct, since no
detached `op-run` subprocess can survive its container's restart. Gate
this sweep behind a new `sweep_node_ops` parameter on `init_app()`,
defaulting to `False`, enabled only at the one true process-boot call site
(`cspawn/app.py`, which Gunicorn's `preload_app` guarantees executes
exactly once per container start) — **not** at `cspawnctl`'s shared
`get_app()` bootstrap path used by every CLI subcommand (including
`op-run` itself), which would otherwise race with and falsely interrupt a
genuinely in-flight op. Separately, thread an optional `node_op_id`
through `expand()`/`_create_droplet()` so a created droplet's id/fqdn gets
recorded on its `NodeOp` row as soon as creation succeeds (one additive,
nullable `droplet_id` column via migration), so an interrupted `expand`'s
message names the droplet an operator should check for. Render
`interrupted` distinctly in the admin Nodes tab.

## Success Criteria

- A full `node expand --tier <t>` against a mocked VPC-advertised swarm
  (mismatched `Status.Addr` vs. public IP) results in `cs.tier`/
  `cs.capacity` being applied, with no manual `label-backfill` step.
- A label-application timeout logs a `WARNING` naming the node instead of
  failing silently.
- A `NodeOp` row stuck `status='running'` becomes `status='interrupted'`
  after a simulated app boot with `sweep_node_ops=True`; the same boot with
  the default `False` (every CLI call site) leaves it untouched.
- An interrupted `expand` whose droplet creation had already succeeded
  surfaces that droplet's id/fqdn in its message.
- The admin Nodes tab renders `interrupted` with a distinct badge and does
  not poll it as if it were still in-flight.

## Scope

### In Scope

- Hostname-based node matching for `cs.tier`/`cs.capacity` labeling after
  join (`cspawn/cli/node.py`).
- WARNING-level logging when label application times out.
- `node_ops.droplet_id` schema addition (migration + model).
- Boot-time sweep of stuck `running` `NodeOp` rows to `interrupted`,
  gated to the true process-boot path only.
- Recording a created droplet's id/fqdn on its triggering `NodeOp` row.
- Admin Nodes tab rendering for the new `interrupted` status.

### Out of Scope

- Refactoring the `code-host-user`/`SWARM_NODE_LABEL` labeling block
  (same underlying defect, but already works via its own name-guess
  fallback; flagged as a future cleanup candidate, not fixed here).
- Folding `cs.tier` presence into sprint 009's `_verify_node_provisioning`
  post-join check (the issue frames this as an optional "consider," not a
  requirement).
- Building "list/destroy orphaned droplet" tooling in the admin UI or CLI
  (this sprint records the data needed for that; building the action is
  deferred).
- Any automated re-verification of DigitalOcean/swarm state for an
  `interrupted` op (consistent with this codebase's established
  detect-and-make-safe, don't-auto-heal pattern from sprints 008/009).
- Re-labeling or re-verifying already-joined nodes (remains
  `label-backfill`'s job).

## Test Strategy

Unit tests only, following this codebase's existing convention for
`cli/node.py` and `models.py` (mocked Docker/DigitalOcean clients,
in-memory SQLite via the `app_db`-style fixture already used in
`test/test_node_op_cli.py` — no live provisioning, no live database).

- Tier-label fix: extend `test/test_node_labels.py` with a case where the
  mocked manager client's `Status.Addr` differs from the droplet's public
  IP (mirroring the live-confirmed scenario) and asserts labels are
  applied via hostname match; a timeout case asserts a `WARNING` is logged
  naming the node and no labels are applied.
- NodeOp schema/sweep: new tests for `sweep_interrupted_node_ops` covering
  running→interrupted transitions, untouched terminal/pending rows, and
  message composition with/without a recorded droplet.
- Boot-gating regression guard: `init_app(sweep_node_ops=True)` invokes the
  sweep; the default (`False`, every existing call site) does not — this
  is the single most safety-critical test in the sprint, since a
  regression here would reintroduce the false-positive race identified in
  architecture-update.md Step 6.
- Droplet recording: `_create_droplet(..., node_op_id=...)` writes
  `droplet_id`/`target_fqdn`; a `None` `node_op_id` (every existing caller)
  is a no-op; a DB-write failure is swallowed (node creation still
  succeeds).
- `op_run` threading: `kind='expand'` passes `node_op_id=op.id` into
  `expand(...)` inside an app context; `remove`/`rebalance` are unchanged.
- Admin UI: extend `test/test_admin_nodes_routes.py` and/or
  `test/test_admin_nodes_template.py` for the `interrupted` badge class and
  message visibility.

## Architecture Notes

See `architecture-update.md` for the full 7-step design, diagrams, and
design-rationale entries. The single most important constraint: the
`NodeOp` sweep must only run from `cspawn/app.py`'s one true process-boot
call site, never from `cspawn/cli/util.py::get_app()` (shared by every
`cspawnctl` subcommand, including `op-run` itself) — running it there
would falsely interrupt a genuinely in-flight op the moment an unrelated
CLI command happens to boot its own app instance.

## GitHub Issues

None linked yet. Source issues (this repo's CLASI issue tracker, not
GitHub): `clasi/issues/expand-tier-labels-never-stamp.md`,
`clasi/issues/nodeop-orphaned-on-container-restart.md`.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan (recorded 2026-07-06: Eric
  pre-authorized running both linked issues through a full sprint
  end-to-end; minimal-scope defaults accepted for all four flagged open
  questions)

## Tickets

All five tickets are created (`clasi/sprints/010-.../tickets/001`-`005`),
each `status: open`, each carrying its `issue:` back-reference in
frontmatter:

| # | Title | Depends On | Issue |
|---|-------|------------|-------|
| 001 | Fix cs.tier/cs.capacity label matching to use hostname instead of public IP | — | expand-tier-labels-never-stamp.md |
| 002 | Add `droplet_id` column to `node_ops` (schema + model) | — | nodeop-orphaned-on-container-restart.md |
| 003 | Boot-time sweep marks stuck `running` NodeOps as `interrupted` | 002 | nodeop-orphaned-on-container-restart.md |
| 004 | Record created droplet id/fqdn on its triggering NodeOp | 002 | nodeop-orphaned-on-container-restart.md |
| 005 | Admin Nodes tab renders `interrupted` status distinctly | 003, 004 | nodeop-orphaned-on-container-restart.md |

Tickets execute serially in the order listed.
