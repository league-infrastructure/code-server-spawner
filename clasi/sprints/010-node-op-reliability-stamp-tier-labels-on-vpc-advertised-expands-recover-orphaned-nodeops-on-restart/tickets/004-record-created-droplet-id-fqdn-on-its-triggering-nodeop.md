---
id: '004'
title: Record created droplet id/fqdn on its triggering NodeOp
status: open
use-cases:
- SUC-004
depends-on:
- '002'
github-issue: ''
issue: nodeop-orphaned-on-container-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Record created droplet id/fqdn on its triggering NodeOp

## Description

Ticket 003's sweep can mark an interrupted `expand` op `interrupted`, but
without this ticket it has nothing specific to say — it can't name *which*
droplet might be an orphan. Confirmed live 2026-07-06: an interrupted
`expand` had already created droplet `swarm4.dojtl.net` in DigitalOcean
before the container died; nothing in the `NodeOp` row pointed at it. See
`clasi/issues/nodeop-orphaned-on-container-restart.md`.

**Fix:** thread an optional `node_op_id` through `expand()` and
`_create_droplet()` so the droplet's id/fqdn get recorded on the
triggering `NodeOp` row as soon as creation succeeds.

1. `_create_droplet(ctx, ..., node_op_id: str | None = None)`: immediately
   after the droplet is created and `fqdn` is computed (existing return
   point, `node.py:1381`), if `node_op_id` is set, best-effort (never
   raises; logs a warning on failure) load the `NodeOp` row by id — local
   import of `NodeOp, db` from `cspawn.models`, mirroring the local-import
   pattern `op_run` already uses — and set `droplet_id=droplet.id`,
   `target_fqdn=fqdn`, then commit. No-op when `node_op_id` is `None` (the
   default — every existing caller: bare CLI `expand`, the autoscaler's
   `apply_plan`, is unaffected).
2. `expand()`: new optional parameter `node_op_id: str | None = None`, not
   a `@click.option` (only reachable via `ctx.invoke(expand,
   node_op_id=...)`, never from CLI argument parsing — no change to
   `--help` output), threaded straight through to the `_create_droplet(...)`
   call.
3. `op_run()`: for `kind == 'expand'` only, wrap `ctx.invoke(expand,
   tier_name=tier, node_op_id=op_id)` in `with app.app_context():` (this DB
   write needs an active app context, which this specific `ctx.invoke`
   call doesn't have today). `remove`/`rebalance` invocations are
   unchanged — no `node_op_id` passed (those kinds don't create droplets).

This ticket depends on ticket 002 (`droplet_id` column/model attribute must
exist before this code can write to it).

See `architecture-update.md` Step 3 (M3), Step 5, and Step 6 ("Decision:
Thread `node_op_id` through `expand()`/`_create_droplet()` as a plain
optional parameter, not a callback") for the full design, including the
callback-based alternative considered and rejected.

## Acceptance Criteria

- [ ] `_create_droplet` accepts a new optional `node_op_id: str | None =
  None` parameter.
- [ ] When `node_op_id` is set and droplet creation succeeds, the matching
  `NodeOp` row's `droplet_id` and `target_fqdn` are updated and committed,
  using a local import of `NodeOp, db` (matching `op_run`'s existing
  pattern).
- [ ] When `node_op_id` is `None` (every existing caller: bare CLI
  `node expand`, the autoscaler's `apply_plan`), `_create_droplet`'s
  behavior is completely unchanged — no DB import attempted, no behavior
  difference from before this ticket.
- [ ] A failure during this best-effort DB write (e.g. the `NodeOp` row
  doesn't exist, or a DB error) is caught, logged as a warning, and never
  propagates — node creation succeeds normally regardless.
- [ ] `expand()` accepts a new optional `node_op_id: str | None = None`
  parameter, not exposed as a `--option`; passes it through to
  `_create_droplet`.
- [ ] `op_run()`, for `kind == 'expand'`, invokes `ctx.invoke(expand,
  tier_name=tier, node_op_id=op_id)` wrapped in `with
  app.app_context():`.
- [ ] `op_run()`'s `remove`/`rebalance` branches are unchanged (verified by
  running the existing `test/test_node_op_cli.py` suite with no
  modification needed for those branches).
- [ ] An interrupted `expand` op whose droplet was recorded by this ticket
  (i.e. `target_fqdn`/`droplet_id` are set) produces, via ticket 003's
  sweep, a message naming that droplet — end-to-end coverage may live in
  ticket 003's or this ticket's test suite (either is acceptable; avoid
  duplicating the same scenario in both).

## Implementation Plan

**Approach**: A narrow, optional, best-effort write inside
`_create_droplet`, gated entirely on a new parameter that defaults to
`None`/no-op. No change to any call site that doesn't explicitly pass
`node_op_id`. The DB write lives inside a `try/except` so a transient DB
issue degrades to "orphan not named" rather than blocking node creation
(consistent with this file's existing best-effort helper conventions,
e.g. `_ensure_node_labels`, `_wait_for_cloud_init`).

**Files to create/modify**:
- `cspawn/cli/node.py` — `_create_droplet`: add `node_op_id` parameter and
  the best-effort DB write near its existing return point. `expand()`: add
  `node_op_id` parameter, thread to `_create_droplet`. `op_run()`: wrap the
  `kind == 'expand'` invocation in `with app.app_context():` and pass
  `node_op_id=op_id`.
- `test/test_node_op_cli.py` — extend with new test cases (see below).

**Testing plan**:
- Follow `test/test_node_op_cli.py`'s `app_db`/`_make_op` fixture pattern
  and its existing `TestOpRunExpand` class conventions (mocked
  `ctx.invoke`/`expand` or a mocked `_create_droplet`, per that file's
  existing patching style for `op-run` lifecycle tests).
- New test: `_create_droplet` called with a `node_op_id` matching a
  pre-seeded `NodeOp(kind="expand")` row — assert the row's `droplet_id`/
  `target_fqdn` are updated after creation, using a mocked
  `digitalocean.Droplet`/`Manager` (following `test_node_labels.py`'s /
  ticket 009's cloud-init test conventions for mocking droplet creation).
- New test: `_create_droplet` called with `node_op_id=None` — assert no
  `NodeOp` query/import is attempted (or, if simpler to assert
  behaviorally: no `NodeOp` row is affected) and droplet creation succeeds
  identically to today.
- New test: the `NodeOp` row for `node_op_id` doesn't exist (or the DB
  write raises) — assert `_create_droplet` still returns its normal
  `(droplet, ip, fqdn, shortname)` tuple and does not raise.
- New test: `op_run()` with `kind='expand'` — assert `ctx.invoke` is called
  with `node_op_id=op_id` and that the call happens inside an app context
  (e.g. by asserting a DB operation performed during the mocked `expand`
  side effect succeeds without a "working outside of application context"
  RuntimeError).
- Run the full existing `test/test_node_op_cli.py` suite to confirm
  `remove`/`rebalance` lifecycle tests are unaffected.

**Documentation updates**: Update `_create_droplet`'s and `expand()`'s
docstrings to describe the new optional `node_op_id` parameter and its
best-effort, no-op-by-default contract.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_op_cli.py
  test/test_node_labels.py`
- **New tests to write**: extend `test/test_node_op_cli.py` with the five
  cases above (droplet recorded, no-op when `None`, best-effort on DB
  failure, `op_run` threading with app-context assertion, and
  `remove`/`rebalance` unaffected).
- **Verification command**: `uv run pytest`
