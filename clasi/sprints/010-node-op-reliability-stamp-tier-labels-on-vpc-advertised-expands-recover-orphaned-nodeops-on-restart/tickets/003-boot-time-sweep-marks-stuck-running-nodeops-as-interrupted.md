---
id: '003'
title: Boot-time sweep marks stuck running NodeOps as interrupted
status: done
use-cases:
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: nodeop-orphaned-on-container-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Boot-time sweep marks stuck running NodeOps as interrupted

## Description

Admin-triggered `NodeOp`s (expand/remove/rebalance) run as a detached
`op-run` subprocess (`cspawn/cli/node.py:2842-2994`) that sets
`status='running'` and writes a terminal status in a `finally` block. If
the spawner container dies mid-run — deploy restart, OOM, host reboot —
the subprocess is killed and the `finally` never executes: the row stays
`status='running'` forever. Confirmed live 2026-07-06: an `expand` op
stayed `running` for 40+ minutes (would have been forever) after a deploy
force-restarted the container mid-run. See
`clasi/issues/nodeop-orphaned-on-container-restart.md`.

**Fix:** add a boot-time sweep, `sweep_interrupted_node_ops(app) -> int`
(new, in `cspawn/models.py` next to `NodeOp`), that marks every `NodeOp`
row with `status='running'` as `status='interrupted'`
(`exit_code=1`, `finished_at=now()`, and a `message` explaining the
restart — naming the recorded droplet when `target_fqdn`/`droplet_id` are
present on the row, per ticket 002/004). This is always correct: no
detached `op-run` subprocess can survive its container's restart, so a
`running` row found at boot cannot reflect a genuinely in-flight op.

**The critical constraint (do not skip this):** this sweep must run
**only** at the one true process-boot call site — `cspawn/app.py`, which
Gunicorn's `preload_app = True` (`docker/gunicorn_config.py:90`) guarantees
executes exactly once per container start, before workers fork. It must
**not** run from `cspawn/cli/util.py::get_app()`, which every `cspawnctl`
subcommand calls (including `op-run` itself, and routine commands like
`node info`/cron-triggered `node autoscale`) — if it did, an unrelated CLI
invocation running while a real op-run is genuinely mid-flight (the file
lock at `node.py:2910-2928` only serializes *node operations* against each
other, not *all cspawnctl invocations* against the sweep) would falsely
mark that in-flight op `interrupted`. Implement this by adding a new
`sweep_node_ops: bool = False` parameter to `init_app()`
(`cspawn/init.py`), calling `sweep_interrupted_node_ops(app)` only when
`True`, and passing `sweep_node_ops=True` from **only**
`cspawn/app.py`'s `app = init_app(...)` call. Every other `init_app(...)`
call site (`cli/util.py`, `cli/devel.py`, `util/test_fixture.py`, every
test fixture) keeps the default `False` — no change needed at those sites.

See `architecture-update.md` Step 1 (the false-positive-race hazard, found
during planning, not in the original issue), Step 3 (M3), Step 5, and Step
6 ("Decision: Gate the NodeOp sweep behind an explicit `sweep_node_ops`
parameter...") for the full design and rationale, including alternatives
considered and rejected (unconditional sweep; implicit
`is_running_under_gunicorn()` detection).

## Acceptance Criteria

- [x] New `sweep_interrupted_node_ops(app) -> int` in `cspawn/models.py`:
  inside `app.app_context()`, finds every `NodeOp` with `status='running'`,
  sets `status='interrupted'`, `exit_code=1`, `finished_at=now()`, and a
  `message` of `"spawner restarted while op was in flight"` (optionally
  appended with an orphan note when `target_fqdn`/`droplet_id` are present
  — see ticket 004 for how those get populated; this ticket's tests may
  construct rows with those fields already set). Commits once; returns the
  count of rows updated.
- [x] Rows with `status` in `pending`, `done`, `failed`, or already
  `interrupted` are left completely untouched by the sweep (no field
  changes, including `finished_at`).
- [x] `init_app(..., sweep_node_ops: bool = False)`: when `True` (and only
  after `setup_database(app)` succeeds), calls
  `sweep_interrupted_node_ops(app)` and logs the returned count at `INFO`.
  When `False` (the default), the sweep is never called.
- [x] `cspawn/app.py` is the only call site passing `sweep_node_ops=True`.
- [x] Every other existing `init_app(...)` call site
  (`cspawn/cli/util.py::get_app`, `cspawn/cli/devel.py::run`,
  `cspawn/util/test_fixture.py`) is unmodified and therefore keeps the
  default `False` — verified by grepping for `init_app(` call sites and
  confirming only `cspawn/app.py` passes the new kwarg.
- [x] A regression test proves the boot-gating distinction end-to-end: a
  `NodeOp` row with `status='running'`, after `init_app(sweep_node_ops=True)`
  is called against an app pointed at that row's database, becomes
  `interrupted`; the same row, if `init_app` were called with the default
  `False`, is untouched.

## Implementation Plan

**Approach**: Keep `sweep_interrupted_node_ops` a small, pure,
independently-testable function taking only `app` (matching this file's
existing style — `ensure_database_exists(app)`, `export_dict()`,
`import_dict(data)`). Wire it into `init_app` behind the new parameter,
placed after `setup_database(app)` (so the table is guaranteed to exist)
and before the function returns. The one enabling call site is a one-line
change in `cspawn/app.py`.

**Files to create/modify**:
- `cspawn/models.py` — add `sweep_interrupted_node_ops(app) -> int`.
- `cspawn/init.py` — add `sweep_node_ops: bool = False` parameter to
  `init_app()`; call the sweep function when `True`, logging the count.
- `cspawn/app.py` — change `app = init_app()` to `app =
  init_app(sweep_node_ops=True)`.
- `test/test_node_op_cli.py` (or a new `test/test_node_op_sweep.py`) — new
  tests (see below).

**Testing plan**:
- Follow `test/test_node_op_cli.py`'s `app_db` fixture (in-memory SQLite
  Flask app, `db.create_all()`) for `sweep_interrupted_node_ops` unit
  tests: seed rows in each status (`pending`, `running`, `done`, `failed`,
  `interrupted`), call the sweep, assert only the `running` row's fields
  changed as specified and the returned count is correct.
- New test: a `running` row with `target_fqdn`/`droplet_id` already set
  produces a `message` naming them; a `running` row without those fields
  produces the generic message with no orphan claim.
- New test for `init_app`: construct the app with
  `sweep_node_ops=True` against a database containing a `running` row
  (reuse/extend `util/test_fixture.py`'s or `test/conftest.py`'s existing
  `init_app(...)` test-app construction, adding the new kwarg only in the
  test that specifically exercises it) and assert the row becomes
  `interrupted`. A parallel test with the default (`sweep_node_ops`
  omitted) asserts the row is untouched.
- Run the full existing suite to confirm no regression to any test that
  constructs an app via `init_app(...)` without the new kwarg (every
  existing call site keeps today's behavior unchanged).

**Documentation updates**: Add a docstring to
`sweep_interrupted_node_ops` explaining the "always correct because no
detached subprocess survives a container restart" invariant, and a comment
at the `cspawn/app.py` call site explaining why this is the *only* place
`sweep_node_ops=True` may ever be passed (cross-reference
`architecture-update.md` Step 6).

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_op_cli.py
  test/test_node_op_model.py`
- **New tests to write**: `sweep_interrupted_node_ops` status-transition
  tests (running→interrupted, others untouched, message composition
  with/without orphan data) and an `init_app(sweep_node_ops=...)` gating
  regression test, in `test/test_node_op_cli.py` or a new
  `test/test_node_op_sweep.py`.
- **Verification command**: `uv run pytest`
